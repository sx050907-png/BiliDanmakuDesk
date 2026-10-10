# coding: utf-8
# Copyright (c) 2026 霜星. All rights reserved.
"""Request normally accessible reference audio. No login scraping or DRM handling."""
import json
import urllib.parse
import urllib.request
import statistics
from core import Client, mapped_time
from audio_sync import features, match_features, SyncCancelled


def reference_stream(metadata, cancel):
    cid = int(metadata['cid'])
    ep = metadata.get('ep_id')
    link = metadata.get('url') or metadata.get('source_input')
    if not link and not ep:
        raise ValueError('请先在主窗口填写 B 站链接并解析；XML 不包含对应视频链接。')
    if cancel.is_set():
        raise SyncCancelled()
    if not ep:
        catalog = Client(cancel.is_set).metadata(link)
        item = next((x for x in catalog['items'] if int(x['cid']) == cid), None)
        if not item:
            raise ValueError('当前弹幕与链接的视频分集不一致，请重新选择对应分集。')
        ep, link = item.get('ep_id'), item.get('url', link)
    if ep:
        endpoint = 'https://api.bilibili.com/pgc/player/web/playurl'
        query = {'ep_id':ep,'qn':16,'fnval':16}
    else:
        from core import parse_link
        kind, value = parse_link(link)
        endpoint = 'https://api.bilibili.com/x/player/playurl'
        query = {'cid':cid,'qn':16,'fnval':16,'bvid' if kind=='BV' else 'avid':value}
    request = urllib.request.Request(endpoint+'?'+urllib.parse.urlencode(query),
        headers={'User-Agent':'Mozilla/5.0','Referer':'https://www.bilibili.com/'})
    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.load(response)
    if cancel.is_set():
        raise SyncCancelled()
    result = data.get('result') or data.get('data') or {}
    if data.get('code') != 0 or result.get('is_drm'):
        raise ValueError('当前视频没有可直接使用的公开参考音轨。请使用拖动或一键对齐核对。')
    audio = (result.get('dash') or {}).get('audio') or []
    chunks = result.get('durl') or []
    if audio:
        url = audio[0].get('baseUrl') or audio[0].get('base_url')
    elif len(chunks) == 1:
        url = chunks[0].get('url')
    else:
        url = None
    if not url or not url.startswith('https://'):
        raise ValueError('当前视频没有可用参考音轨；不会要求下载文件或录音。')
    return url, bool(result.get('is_preview'))


def align_bilibili(metadata, movie, ffmpeg, cancel, progress, options=None):
    progress('正在从当前 B 站链接检查可用参考音轨…')
    url, preview = reference_stream(metadata, cancel)
    reference = features(url,ffmpeg,cancel,lambda s:progress('B 站参考：'+s),limit=360)
    local = features(movie,ffmpeg,cancel,lambda s:progress('本地电影：'+s))
    result = match_features(reference,local,cancel,progress)
    result['preview'] = preview
    result['reference_seconds'] = len(reference)*.05
    options = options or {'rate':1.,'offset':0.,'segments':[]}
    corrections = [m['local']-mapped_time(m['reference'],options) for m in result['matches']]
    correction = statistics.median(corrections)
    if max(abs(x-correction) for x in corrections) > .75:
        raise ValueError('匹配片段与当前倍率或分段设置不一致，无法仅调整体偏移。请用画面核对或先检查当前倍率。')
    result['estimated_rate'] = result['rate']
    result['rate'] = options['rate']
    result['offset'] = options['offset']+correction
    result['error'] = max(abs(x-correction) for x in corrections)
    return result
