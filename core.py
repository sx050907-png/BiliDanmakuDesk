# coding: utf-8
# Copyright (c) 2026 霜星. All rights reserved.
"""Bilibili metadata, current danmaku download and portable exports."""
import csv
import json
import math
import re
import os
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path


class ApiError(RuntimeError):
    pass


class Cancelled(RuntimeError):
    pass


class Client:
    def __init__(self, cancelled=lambda: False):
        self.cancelled = cancelled

    def check(self):
        if self.cancelled():
            raise Cancelled('已取消；已下载的分段仍保留在缓存中。')

    def pause(self, seconds):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            self.check()
            time.sleep(min(0.1, max(0, until - time.monotonic())))

    def get(self, url):
        last = None
        for attempt in range(4):
            self.check()
            try:
                req = urllib.request.Request(url, headers={
                    'User-Agent': 'Mozilla/5.0',
                    'Referer': 'https://www.bilibili.com/'})
                with urllib.request.urlopen(req, timeout=20) as response:
                    body = response.read()
                if body.lstrip().startswith(b'{'):
                    obj = json.loads(body)
                    if obj.get('code', 0) != 0:
                        code = obj.get('code')
                        message = obj.get('message', str(code))
                        if code in (-352, -412, -509):
                            raise ApiError(f'B站暂时限制请求（{code}），请稍后重试。')
                        raise ValueError(f'B站接口返回 {code}：{message}')
                return body
            except (OSError, ApiError) as exc:
                last = exc
                if attempt < 3:
                    self.pause((3, 8, 18)[attempt])
        raise ApiError(str(last))

    def metadata(self, text):
        text = text.strip()
        if re.match(r'https?://b23\.tv/', text):
            req = urllib.request.Request(text, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=20) as response:
                text = response.geturl()
        kind, value = parse_link(text)
        if kind in ('ep', 'ss'):
            param = 'ep_id' if kind == 'ep' else 'season_id'
            obj = json.loads(self.get(f'https://api.bilibili.com/pgc/view/web/season?{param}={value}'))
            data = obj['result']
            episodes = list(data.get('episodes', []))
            for section in data.get('section', []):
                episodes.extend(section.get('episodes', []))
            seen = set()
            items = []
            for e in episodes:
                cid = e.get('cid')
                if not cid or cid in seen:
                    continue
                seen.add(cid)
                label = ' '.join(str(e.get(k, '')) for k in ('title', 'long_title')).strip()
                items.append({'cid': cid, 'title': label or '正片',
                              'duration': e.get('duration', 0) / 1000,
                              'url': e.get('link') or f"https://www.bilibili.com/bangumi/play/ep{e['id']}",
                              'ep_id': e.get('id')})
            selected = next((i for i, e in enumerate(items) if kind == 'ep' and str(e.get('ep_id')) == value), 0)
            return {'title': data.get('title', 'B站影视'), 'items': items, 'selected': selected}
        query = 'bvid' if kind == 'BV' else 'aid'
        obj = json.loads(self.get(f'https://api.bilibili.com/x/web-interface/view?{query}={value}'))
        data = obj['data']
        items = [{'cid': p['cid'], 'title': f"P{p['page']} {p['part']}",
                  'duration': p.get('duration', 0),
                  'url': f"https://www.bilibili.com/video/{data['bvid']}?p={p['page']}"}
                 for p in data.get('pages', [])]
        page = re.search(r'[?&]p=(\d+)', text)
        selected = min(max(int(page.group(1)) - 1, 0), len(items) - 1) if page else 0
        return {'title': data['title'], 'items': items, 'selected': selected}

    def download(self, item, cache, progress, refresh=False):
        cid = int(item['cid'])
        # Some view responses advertise a fixed capacity (e.g. 100), not the
        # video's actual six-minute segment count. Duration is authoritative.
        total = math.ceil(item.get('duration', 0) / 360)
        if not total:
            view = self.get(f'https://api.bilibili.com/x/v2/dm/web/view?type=1&oid={cid}')
            for field, value in protobuf(view):
                if field == 4 and isinstance(value, bytes):
                    total = int(dict(protobuf(value)).get(2, total))
        if not 1 <= total <= 1000:
            raise ApiError('无法确定弹幕分段数量，请重新解析链接。')
        cache = Path(cache) / str(cid)
        cache.mkdir(parents=True, exist_ok=True)
        rows = {}
        for index in range(1, total + 1):
            self.check()
            path = cache / f'{index}.bin'
            body = None
            if path.exists() and not refresh:
                try:
                    body = path.read_bytes()
                    part = decode_segment(body)
                except (ValueError, IndexError, UnicodeError):
                    body = None
            if body is None:
                self.pause(1.0)
                body = self.get(f'https://api.bilibili.com/x/v2/dm/web/seg.so?type=1&oid={cid}&segment_index={index}')
                part = decode_segment(body)
                tmp = path.with_suffix('.tmp')
                tmp.write_bytes(body)
                tmp.replace(path)
            for row in part:
                rows[row['id']] = row
            progress(index, total, len(rows))
        return sorted(rows.values(), key=lambda r: (r['progress_ms'], int(r['id'])))


def parse_link(text):
    for pattern, kind in ((r'(?:/|\b)ep(\d+)', 'ep'), (r'(?:/|\b)ss(\d+)', 'ss'),
                          (r'(BV[0-9A-Za-z]{10})', 'BV'), (r'(?:/|\b)av(\d+)', 'av')):
        match = re.search(pattern, text, re.I if kind != 'BV' else 0)
        if match:
            return kind, match.group(1)
    raise ValueError('请输入 B站 ep、ss、BV、av 链接或编号，也支持 b23.tv 短链接。')


def varint(data, pos):
    result = shift = 0
    while pos < len(data) and shift <= 63:
        value = data[pos]
        pos += 1
        result |= (value & 127) << shift
        if value < 128:
            return result, pos
        shift += 7
    raise ValueError('弹幕数据损坏：无效整数。')


def protobuf(data):
    pos = 0
    while pos < len(data):
        tag, pos = varint(data, pos)
        field, wire = tag >> 3, tag & 7
        if not field:
            raise ValueError('弹幕数据损坏：无效字段。')
        if wire == 0:
            value, pos = varint(data, pos)
        elif wire in (1, 2, 5):
            if wire == 2:
                length, pos = varint(data, pos)
            else:
                length = 8 if wire == 1 else 4
            if pos + length > len(data):
                raise ValueError('弹幕数据不完整。')
            value = data[pos:pos + length]
            pos += length
        else:
            raise ValueError('无法解析弹幕数据。')
        yield field, value


def decode_segment(body):
    if body.lstrip().startswith(b'{'):
        raise ValueError('收到接口错误信息，未收到弹幕。')
    result = []
    for field, value in protobuf(body):
        if field != 1 or not isinstance(value, bytes):
            continue
        data = dict(protobuf(value))
        result.append({'id': str(data.get(1, 0)), 'progress_ms': data.get(2, 0),
                       'mode': data.get(3, 1), 'fontsize': data.get(4, 25),
                       'color': data.get(5, 16777215),
                       'content': data.get(7, b'').decode('utf-8'),
                       'ctime': data.get(8, 0), 'pool': data.get(11, 0),
                       'mid_hash': data.get(6, b'').decode('utf-8')})
    return result


def safe_name(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(' .')[:100] or '弹幕'
    if name.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL'} | {f'{p}{i}' for p in ('COM', 'LPT') for i in range(1, 10)}:
        name = '_' + name
    return name


def timestamp(seconds, ass=False):
    cs = max(0, round(seconds * 100))
    if ass:
        return f'{cs//360000}:{cs//6000%60:02d}:{cs//100%60:02d}.{cs%100:02d}'
    return f'{cs//360000:02d}:{cs//6000%60:02d}:{cs//100%60:02d}.{cs%100:02d}'


def ass_text(text):
    return text.replace('\\', '＼').replace('{', '｛').replace('}', '｝').replace('\r', ' ').replace('\n', ' ')


def read_xml(path):
    root = ET.parse(path).getroot()
    rows = []
    for index, node in enumerate(root.findall('d')):
        p = node.get('p', '').split(',')
        if len(p) < 8:
            raise ValueError('XML 不是完整的 B站弹幕格式。')
        rows.append({'id': p[7] or str(index), 'progress_ms': round(float(p[0]) * 1000),
                     'mode': int(p[1]), 'fontsize': int(p[2]), 'color': int(p[3]),
                     'ctime': int(p[4]), 'pool': int(p[5]), 'mid_hash': p[6],
                     'content': node.text or ''})
    return sorted(rows, key=lambda r: r['progress_ms']), root.findtext('chatid', '0')


def parse_time(text):
    parts = str(text).strip().split(':')
    if len(parts) > 3:
        raise ValueError('时间格式应为秒、分:秒或时:分:秒。')
    numbers = [float(p) for p in parts]
    if any(not math.isfinite(p) or p < 0 for p in numbers):
        raise ValueError('时间应为有限的非负数字。')
    if len(numbers) > 1 and any(p >= 60 for p in numbers[1:]):
        raise ValueError('分、秒应小于 60。')
    seconds = 0
    for p in numbers:
        seconds = seconds * 60 + p
    return seconds


def calibrate(b1, local1, b2, local2):
    b1, local1, b2, local2 = [parse_time(s) for s in (b1, local1, b2, local2)]
    if b2 <= b1 or local2 <= local1:
        raise ValueError('第二个对应点须晚于第一个对应点。')
    rate = (local2 - local1) / (b2 - b1)
    if not 0.1 <= rate <= 10:
        raise ValueError('校准倍率超出范围，请检查对应时间。')
    return rate, local1 - b1 * rate


def validate_options(options):
    rate, offset = float(options['rate']), float(options['offset'])
    if not math.isfinite(offset) or not math.isfinite(rate) or not 0.1 <= rate <= 10:
        raise ValueError('偏移须为有效数字，时间倍率应在 0.1–10 之间。')
    if not (20 <= int(options['size']) <= 64 and 1 <= int(options['opacity']) <= 100
            and 3 <= float(options['travel']) <= 20 and 0.2 <= float(options['area']) <= 1):
        raise ValueError('字号 20–64，显示强度 1–100，滚动时间 3–20 秒，区域 20–100%。')
    last = -1
    font = options.get('font', 'Microsoft YaHei')
    if not isinstance(font, str) or not font.strip() or any(c in font for c in ',\r\n'):
        raise ValueError('请选择有效的字体名称。')
    words = options.get('block_words', [])
    if not isinstance(words, list) or any(not isinstance(word, str) or not word.strip() for word in words):
        raise ValueError('屏蔽词须为非空文本，每行一个。')
    for rule in options.get('segments', []):
        start, delta = rule['start'], rule['offset']
        if not math.isfinite(start) or not math.isfinite(delta) or start < 0 or start <= last:
            raise ValueError('分段时间须递增且不能重复；偏移须为有效数字。')
        last = start


def mapped_time(seconds, options):
    delta = 0
    for rule in options.get('segments', []):
        if seconds < rule['start']:
            break
        delta = rule['offset']
    return seconds * options['rate'] + options['offset'] + delta


def visible_rows(rows, options):
    words = [word.casefold() for word in options.get('block_words', [])]
    return [row for row in rows if not any(word in row['content'].casefold() for word in words)]


def make_xml(rows, cid, options=None):
    root = ET.Element('i')
    ET.SubElement(root, 'chatserver').text = 'chat.bilibili.com'
    ET.SubElement(root, 'chatid').text = str(cid)
    ET.SubElement(root, 'maxlimit').text = str(len(rows))
    for row in rows:
        seconds = row['progress_ms'] / 1000
        text = row['content']
        if options is not None:
            seconds = mapped_time(seconds, options)
            if seconds < 0:
                continue
            text = ass_text(text)
        params = ','.join([f'{seconds:.3f}', str(row['mode']), str(row['fontsize']),
                           str(row['color']), str(row['ctime']), str(row['pool']),
                           row['mid_hash'], row['id']])
        ET.SubElement(root, 'd', {'p': params}).text = text
    return ET.ElementTree(root)


def factory_ass(rows, metadata, options, engine):
    validate_options(options)
    filtered = visible_rows(rows, options)
    eligible = [row for row in filtered if mapped_time(row['progress_ms']/1000, options) >= 0]
    if not eligible:
        ass = ('[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\n\n'
               '[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n')
        return ass, {'rendered':0, 'blocked':len(rows)-len(filtered), 'skipped':len(rows),
                     'engine':'DanmakuFactory 1.70 CLI'}
    with tempfile.TemporaryDirectory(prefix='bili_danmaku_') as temporary:
        folder = Path(temporary)
        make_xml(eligible, metadata['cid'], options).write(folder / 'input.xml', encoding='utf-8', xml_declaration=True)
        command = [str(Path(engine).resolve()), '-o', 'output.ass', '-i', 'input.xml',
                   '-r', '1920x1080', '-s', str(options['travel']), '-f', '4',
                   '-d', '-1' if options.get('nonoverlap', True) else '0',
                   '-S', str(options['size']), '-N', 'Microsoft YaHei',
                   '-O', str(max(1, round(options['opacity'] * 2.55))), '-L', '1', '-D', '0',
                   '--displayarea', str(options['area']), '--scrollarea', str(options['area']),
                   '--ignore-warnings']
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        process = subprocess.run(command, cwd=folder, capture_output=True, timeout=120, creationflags=flags)
        output = folder / 'output.ass'
        if process.returncode or not output.exists():
            raise RuntimeError('ASS 转换失败：' + (process.stdout + process.stderr).decode('utf-8', errors='replace')[-500:])
        ass = output.read_text(encoding='utf-8-sig')
    # The CLI uses Windows ANSI argv for font names. Write Unicode names in Python
    # after conversion, so mixed ANSI/UTF-8 cannot corrupt the generated ASS.
    selected_font={'黑体':'SimHei','宋体':'SimSun'}.get(options.get('font'),options.get('font','Microsoft YaHei'))
    lines=[]
    for line in ass.splitlines():
        if line.startswith('Style:'):
            fields=line.split(',')
            fields[1]=selected_font
            line=','.join(fields)
        lines.append(line)
    ass='\n'.join(lines)+'\n'
    rendered = sum(line.startswith('Dialogue:') for line in ass.splitlines())
    return ass, {'rendered': rendered, 'blocked':len(rows)-len(filtered), 'skipped': len(rows) - rendered, 'engine': 'DanmakuFactory 1.70 CLI'}


def export_all(folder, name, rows, metadata, options, engine):
    ass, stats = factory_ass(rows, metadata, options, engine)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    # Each export gets a new directory, preserving previous adjustments.
    base = safe_name(name) + '_' + time.strftime('%Y%m%d_%H%M%S')
    destination = folder / base
    suffix = 2
    while destination.exists():
        destination = folder / f'{base}_{suffix}'
        suffix += 1
    destination.mkdir()
    movie = metadata.get('movie', '')
    ass_name = safe_name(Path(movie).stem) + '.ass' if movie else '弹幕.ass'
    (destination / ass_name).write_text(ass, encoding='utf-8-sig')
    make_xml(rows, metadata['cid']).write(destination / '原始弹幕.xml', encoding='utf-8', xml_declaration=True)
    with (destination / '弹幕.csv').open('w', encoding='utf-8-sig', newline='') as file:
        writer = csv.writer(file)
        writer.writerow(['原始视频时间', '调整后时间秒', '弹幕内容', '模式', '颜色', 'ID'])
        for r in visible_rows(rows, options):
            writer.writerow([timestamp(r['progress_ms']/1000), round(mapped_time(r['progress_ms']/1000, options), 3), r['content'], r['mode'], r['color'], r['id']])
    with (destination / '弹幕.txt').open('w', encoding='utf-8-sig') as file:
        file.write('当前弹幕池，不代表全部历史弹幕。以下为调整后的时间。\n\n')
        for r in visible_rows(rows, options):
            t = mapped_time(r['progress_ms']/1000, options)
            if t >= 0:
                file.write(f"[{timestamp(t)}] {r['content']}\n")
    record = {'version': 1, 'metadata': metadata, 'options': options, 'stats': stats, 'rows': rows}
    (destination / '项目.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    (destination / '加载方法.txt').write_text(
        f'用 PotPlayer 打开本地视频，然后将 {ass_name} 拖入播放窗口。\n'
        '暂停、快进时弹幕会跟随视频进度。也可以把 ASS 复制到视频目录，同名字幕可自动识别。\n'
        '有独立对白字幕时，需在播放器启用第二字幕；画面内已经压制的字幕不受影响。\n'
        '原始 XML 未调整时间；ASS、CSV、TXT 使用本次同步设置。\n'
        '导出文件不会自动修改本地视频或其旁边的字幕。\n'
        '项目.json 可重新导入本工具，不联网调整并导出。\n', encoding='utf-8-sig')
    return destination, stats
