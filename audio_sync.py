# coding: utf-8
# Copyright (c) 2026 霜星. All rights reserved.
"""Offline acoustic alignment. A reference file must use the Bilibili timeline."""
import itertools
import math
import os
import subprocess
import tempfile
import threading
import numpy as np

STEP, RATE, FRAME = .05, 8000, 400


class SyncCancelled(Exception):
    pass


def features(filename, ffmpeg, cancel, progress, limit=None):
    chunks = []
    window = np.hanning(FRAME)
    edges = np.unique(np.geomspace(4, 480, 17).astype(int))
    with tempfile.TemporaryFile() as errors:
        remote = str(filename).startswith('https://')
        network = ['-rw_timeout', '20000000', '-headers',
                   'Referer: https://www.bilibili.com/\r\nUser-Agent: Mozilla/5.0\r\n'] if remote else []
        maximum = ['-t', str(limit)] if limit else []
        p = subprocess.Popen([str(ffmpeg), '-v', 'error', '-nostdin']+network+['-i', str(filename)]+maximum+[
            '-map', '0:a:0', '-vn', '-ac', '1', '-ar', str(RATE), '-f', 'f32le', 'pipe:1'],
            stdout=subprocess.PIPE, stderr=errors,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        done = threading.Event()
        def watch():
            while not done.wait(.1):
                if cancel.is_set():
                    if p.poll() is None:
                        p.terminate()
                    return
        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        carry = b''
        total = 0
        try:
            while True:
                data = p.stdout.read(FRAME*4*256)
                if cancel.is_set():
                    raise SyncCancelled()
                if not data:
                    break
                data = carry+data
                length = len(data)//(FRAME*4)*(FRAME*4)
                carry = data[length:]
                if not length:
                    continue
                audio = np.frombuffer(data[:length], dtype='<f4').reshape(-1, FRAME)
                power = np.abs(np.fft.rfft(audio*window, n=1024, axis=1))**2
                bands = np.stack([np.log(np.maximum(power[:,a:b].mean(axis=1), 1e-10))
                                  for a,b in zip(edges[:-1],edges[1:])], axis=1)
                silent = (audio**2).mean(axis=1) < 1e-8
                chunks.append((bands.astype(np.float32), silent))
                total += len(audio)
                progress(f'已分析 {total*STEP:.0f} 秒音频…')
                if total*STEP > 8*3600:
                    raise ValueError('当前校准支持 8 小时以内的音频。')
            code = p.wait()
            if cancel.is_set():
                raise SyncCancelled()
            if code or not chunks:
                errors.seek(0)
                detail = errors.read()[-600:].decode('utf-8', 'replace')
                raise ValueError('无法读取 B 站参考音轨，请稍后重试。' if remote else '无法读取音轨：'+detail)
        finally:
            if p.poll() is None:
                p.terminate()
            p.wait()
            done.set()
            watcher.join(timeout=1)
            p.stdout.close()
    data = np.concatenate([x[0] for x in chunks])
    silent = np.concatenate([x[1] for x in chunks])
    voiced = data[~silent]
    if len(voiced) < 400:
        raise ValueError('有效声音过少，无法可靠校准。')
    data = np.clip((data-voiced.mean(axis=0))/np.maximum(voiced.std(axis=0), .1), -4, 4)
    data[silent] = 0
    return data


def fit_matches(matches):
    if len(matches) < 4:
        raise ValueError('可靠声音匹配少于 4 个，可能是音轨、语言或剪辑不同。未应用校准。')
    x = np.array([m['reference'] for m in matches])
    y = np.array([m['local'] for m in matches])
    best = None
    for i,j in itertools.combinations(range(len(matches)), 2):
        if abs(x[j]-x[i]) < 10:
            continue
        rate = (y[j]-y[i])/(x[j]-x[i])
        if not .9 <= rate <= 1.1:
            continue
        offset = y[i]-rate*x[i]
        good = np.abs(y-rate*x-offset) <= .5
        score = (int(good.sum()), float(np.ptp(x[good])))
        if best is None or score > best[0]:
            best = score, good
    if best is None or best[0][0] < max(4, math.ceil(len(matches)*.8)):
        raise ValueError('各时段的声音偏移不一致，可能有删减或插入片段；未套用整体校准。')
    good = best[1]
    rate, offset = np.polyfit(x[good], y[good], 1)
    errors = np.abs(y[good]-rate*x[good]-offset)
    return {'rate':float(rate), 'offset':float(offset), 'matches':matches,
            'inliers':int(good.sum()), 'error':float(errors.max()),
            'span':float(np.ptp(x[good]))}


def match_features(reference, local, cancel, progress):
    length = min(280, len(reference)//12)
    if length < 80 or len(local) < length:
        raise ValueError('音频太短，建议提供至少一分钟、包含多个相同时段的素材。')
    nfft = 1 << (len(local)+length*2-1).bit_length()
    spectrum = np.fft.rfft(local, n=nfft, axis=0)
    energies = np.concatenate(([0.], np.cumsum((local**2).sum(axis=1))))
    matches = []
    for index, start in enumerate(np.linspace(0, len(reference)-length, 9).astype(int)):
        if cancel.is_set():
            raise SyncCancelled()
        progress(f'正在比对声音片段 {index+1}/9…')
        sample = reference[start:start+length]
        if (sample**2).mean() < .08:
            continue
        best = None
        for stretch in (1., .96, 1.04):
            size = round(length*stretch)
            positions = np.arange(size)/stretch
            template = np.stack([np.interp(positions, np.arange(length), sample[:,k])
                                 for k in range(sample.shape[1])], axis=1)
            energy = (template**2).sum()
            correlation = np.fft.irfft(spectrum*np.fft.rfft(template[::-1], n=nfft, axis=0),
                                       n=nfft, axis=0).sum(axis=1)[size-1:len(local)]
            norms = np.sqrt(np.maximum((energies[size:]-energies[:-size])*energy, 1e-12))
            scores = correlation/norms
            peak = int(scores.argmax())
            score = float(scores[peak])
            masked = scores.copy()
            masked[max(0,peak-60):peak+61] = -1
            margin = score-float(masked.max())
            if best is None or score > best[0]:
                best = score, margin, peak, size
        score, margin, peak, size = best
        if score >= .65 and margin >= .04:
            matches.append({'reference':(start+length/2)*STEP,
                            'local':(peak+size/2)*STEP, 'score':round(score,3)})
    if len(matches) < 6:
        raise ValueError(f'9 个时段中只有 {len(matches)} 个可靠匹配，未应用校准。请检查版本、音轨和剪辑是否一致。')
    result = fit_matches(matches)
    result['tested'] = 9
    if result['span'] < min(len(reference)*STEP*.5, 120.):
        raise ValueError('匹配时段过于集中，无法确认整部影片的同步关系。未应用校准。')
    return result


def align(reference, local, ffmpeg, cancel, progress):
    ref = features(reference, ffmpeg, cancel, lambda s:progress('参考版本：'+s))
    movie = features(local, ffmpeg, cancel, lambda s:progress('本地版本：'+s))
    return match_features(ref, movie, cancel, progress)
