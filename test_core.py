import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import (ApiError, Cancelled, Client, calibrate, decode_segment, export_all,
                  mapped_time, parse_link, parse_time, protobuf, read_xml, safe_name,
                  validate_options, factory_ass, visible_rows)

HERE = Path(__file__).parent
OPTIONS = {'offset': 0., 'rate': 1., 'size': 36, 'opacity': 80, 'travel': 8.,
           'area': .72, 'nonoverlap': True, 'segments': []}
ROW = {'id': '1234', 'progress_ms': 3000, 'mode': 1, 'fontsize': 25,
       'color': 16777215, 'content': '测试 {\\pos(1,1)}', 'ctime': 1720000000,
       'pool': 0, 'mid_hash': 'abcdef'}


def vi(n):
    output = bytearray()
    while n > 127:
        output.append(n & 127 | 128)
        n >>= 7
    output.append(n)
    return bytes(output)


def number(field, value):
    return vi(field << 3) + vi(value)


def string(field, value):
    return vi(field << 3 | 2) + vi(len(value)) + value


def segment():
    elem = number(1, 1234) + number(2, 3000) + number(3, 1) + string(7, '你好'.encode())
    return string(1, elem) + number(3, 42)


class CoreTests(unittest.TestCase):
    def test_opacity_boundaries_and_translucent_ass(self):
        for percent,alpha in ((1,'FC'),(35,'A6'),(100,'00')):
            ass,_=factory_ass([{**ROW,'content':'不透明度测试'}],{'cid':99},{**OPTIONS,'opacity':percent},HERE/'vendor/DanmakuFactory.exe')
            styles=[line.split(',') for line in ass.splitlines() if line.startswith('Style:') and 'message_box' not in line and 'Style: SP' not in line]
            self.assertTrue(styles)
            self.assertTrue(all(style[3]==f'&H{alpha}FFFFFF' for style in styles))

    def test_chinese_font_names_produce_readable_utf8_ass(self):
        for font in ('黑体','华文宋体'):
            ass,_=factory_ass([{**ROW,'content':'中文弹幕字体测试'}],{'cid':99},{**OPTIONS,'font':font,'size':48,'area':.5},HERE/'vendor/DanmakuFactory.exe')
            expected='SimHei' if font=='黑体' else font
            styles=[line.split(',') for line in ass.splitlines() if line.startswith('Style:')]
            self.assertTrue(styles)
            self.assertTrue(all(style[1]==expected for style in styles))
            self.assertTrue(all(style[2]=='48' for style in styles if style[0].strip()!='Style: message_box'))
            self.assertIn('中文弹幕字体测试',ass)

    def test_block_words_font_and_speed_in_real_export_preserve_raw_rows(self):
        rows=[{**ROW,'id':'1','content':'广告 SPAM'},
              {**ROW,'id':'2','content':'正常保留','progress_ms':20000}]
        options={**OPTIONS,'font':'SimHei','travel':4.,'block_words':['spam','广告']}
        ass,stats=factory_ass(rows,{'cid':99},options,HERE/'vendor/DanmakuFactory.exe')
        self.assertEqual(stats['blocked'],1)
        self.assertIn('SimHei',ass)
        self.assertNotIn('SPAM',ass)
        self.assertIn('正常保留',ass)
        dialogue=next(line for line in ass.splitlines() if line.startswith('Dialogue:'))
        self.assertIn('0:00:20.00,0:00:24.00',dialogue)
        with tempfile.TemporaryDirectory() as folder:
            destination,_=export_all(folder,'过滤测试',rows,{'cid':99},options,HERE/'vendor/DanmakuFactory.exe')
            restored,_=read_xml(destination/'原始弹幕.xml')
            self.assertEqual(len(restored),2)
            self.assertNotIn('SPAM',(destination/'弹幕.csv').read_text(encoding='utf-8-sig'))
            self.assertEqual(len(json.loads((destination/'项目.json').read_text(encoding='utf-8'))['rows']),2)
        self.assertEqual(len(visible_rows(rows,{**options,'block_words':[]})),2)

    def test_blocking_all_comments_still_produces_valid_empty_ass(self):
        ass,stats=factory_ass([ROW],{'cid':99},{**OPTIONS,'block_words':['测试']},HERE/'vendor/DanmakuFactory.exe')
        self.assertIn('[Events]',ass)
        self.assertEqual(stats['rendered'],0)
        self.assertEqual(stats['blocked'],1)

    def test_links_and_pages(self):
        self.assertEqual(parse_link('https://www.bilibili.com/bangumi/play/ep829623?theme=movie'), ('ep', '829623'))
        self.assertEqual(parse_link('ss123'), ('ss', '123'))
        self.assertEqual(parse_link('BV1ow4m1a71b'), ('BV', 'BV1ow4m1a71b'))
        with self.assertRaises(ValueError):
            parse_link('无效链接')

    def test_protocol_and_truncation(self):
        row = decode_segment(segment())[0]
        self.assertEqual((row['id'], row['progress_ms'], row['content']), ('1234', 3000, '你好'))
        with self.assertRaises(ValueError):
            list(protobuf(b'\x0a\x05ab'))
        with self.assertRaises(ValueError):
            decode_segment(b'{"code":-352}')

    def test_linear_and_piecewise_sync(self):
        rate, offset = calibrate('1:00', '1:10', '20:00', '20:29')
        self.assertAlmostEqual(60 * rate + offset, 70)
        self.assertAlmostEqual(1200 * rate + offset, 1229)
        options = {**OPTIONS, 'offset': 10, 'segments': [{'start': 600, 'offset': -15}, {'start': 1000, 'offset': -30}]}
        self.assertEqual(mapped_time(599, options), 609)
        self.assertEqual(mapped_time(600, options), 595)
        self.assertEqual(mapped_time(1000, options), 980)
        with self.assertRaises(ValueError):
            calibrate('1:00', '1:00', '1:00', '2:00')

    def test_validation(self):
        for bad in ('nan', 'inf', '-1'):
            with self.assertRaises(ValueError):
                parse_time(bad)
        with self.assertRaises(ValueError):
            validate_options({**OPTIONS, 'rate': float('nan')})
        with self.assertRaises(ValueError):
            validate_options({**OPTIONS, 'segments': [{'start': 5, 'offset': 1}, {'start': 5, 'offset': 2}]})
        self.assertEqual(safe_name('CON'), '_CON')
        self.assertEqual(safe_name('生化:死亡岛?'), '生化_死亡岛_')

    def test_cache_invalid_data_is_refetched(self):
        calls = []
        client = Client()
        def fetch(url):
            calls.append(url)
            if '/view?' in url:
                return string(4, number(1, 360000) + number(2, 1))
            return segment()
        client.get = fetch
        client.pause = lambda _: None
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '99'
            path.mkdir()
            (path / '1.bin').write_bytes(b'{"code":-352}')
            rows = client.download({'cid': 99, 'duration': 360}, folder, lambda *_: None)
            self.assertEqual(len(rows), 1)
            self.assertEqual((path / '1.bin').read_bytes(), segment())
            calls.clear()
            client.download({'cid': 99, 'duration': 360}, folder, lambda *_: None)
            self.assertEqual(len(calls), 0)  # cached segment
            client.download({'cid': 99, 'duration': 360}, folder, lambda *_: None, refresh=True)
            self.assertEqual(len(calls), 1)

    def test_duration_bounds_segment_count(self):
        client = Client()
        client.pause = lambda _: None
        urls = []
        def fetch(url):
            urls.append(url)
            if '/view?' in url:
                return string(4, number(2, 100))
            return segment()
        client.get = fetch
        with tempfile.TemporaryDirectory() as folder:
            client.download({'cid': 99, 'duration': 5412}, folder, lambda *_: None)
        self.assertEqual(len(urls), 16)
        self.assertTrue(all('segment_index=' in u for u in urls))

    def test_cancel_before_network(self):
        with self.assertRaises(Cancelled):
            Client(lambda: True).get('https://example.com')

    def test_retry_on_rate_limit(self):
        class Response:
            def __init__(self, data): self.data = data
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return self.data
        client = Client()
        waits = []
        client.pause = waits.append
        with patch('urllib.request.urlopen', side_effect=[Response(b'{"code":-352}'), Response(b'{"code":0}')]):
            self.assertEqual(client.get('https://api.bilibili.com/test'), b'{"code":0}')
        self.assertEqual(waits, [3])

    def test_real_engine_export_preserves_original_and_adjusts_ass(self):
        row = copy.deepcopy(ROW)
        second = {**ROW, 'id': '1235', 'progress_ms': 100, 'content': '在视频开始前'}
        options = {**OPTIONS, 'offset': -1}
        with tempfile.TemporaryDirectory(prefix='弹幕测试_') as folder:
            destination, stats = export_all(folder, '测试电影', [row, second], {'cid': 99, 'movie': 'my.movie.mp4'}, options, HERE / 'vendor/DanmakuFactory.exe')
            self.assertEqual(stats['rendered'], 1)
            text = (destination / 'my.movie.ass').read_text(encoding='utf-8-sig')
            event = next(s for s in text.splitlines() if s.startswith('Dialogue:'))
            self.assertIn(',0:00:02.00,', event)
            self.assertIn('｛＼pos(1,1)｝', event)
            loaded, cid = read_xml(destination / '原始弹幕.xml')
            self.assertEqual(cid, '99')
            self.assertEqual(loaded[-1]['content'], ROW['content'])
            self.assertEqual(row, ROW)
            record = json.loads((destination / '项目.json').read_text(encoding='utf-8'))
            self.assertEqual(record['options']['offset'], -1)
            another, _ = export_all(folder, '测试电影', [row], {'cid': 99}, OPTIONS, HERE / 'vendor/DanmakuFactory.exe')
            self.assertNotEqual(destination, another)


if __name__ == '__main__':
    unittest.main()
