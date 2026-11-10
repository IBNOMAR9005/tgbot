# -*- coding: utf-8 -*-
"""ffmpeg/ffprobe: فحص الملف، سحب الصوت (من أي صيغة)، التقسيم عند أقرب سكون."""
import asyncio, json, os, re


class MediaError(Exception):
    """رسالة مفهومة للمستخدم."""


async def run(cmd, timeout=None):
    p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(p.communicate(), timeout)
    except BaseException:
        try:
            p.kill()
        except ProcessLookupError:
            pass
        await p.wait()
        raise
    return p.returncode, out.decode('utf-8', 'replace'), err.decode('utf-8', 'replace')


async def probe(path):
    cmd = ['ffprobe', '-v', 'error', '-analyzeduration', '200M', '-probesize', '200M',
           '-print_format', 'json', '-show_format', '-show_streams', path]
    rc, out, err = await run(cmd, 120)
    if rc != 0 or not out.strip():
        low = err.lower()
        if 'moov atom not found' in low or 'invalid data' in low or 'end of file' in low:
            raise MediaError('الملف تالف أو ناقص (الرفع ما اكتملش). ابعته تاني.')
        raise MediaError('مش قادر أقرا الملف ده كصوت/فيديو.\n' + err.strip()[-200:])
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        raise MediaError('ffprobe رجّع ناتج غير مفهوم.')


def _fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def pick_audio_stream(info):
    """يرجّع (رقم الستريم الصوتي النسبي، stream dict). أطول ستريم، وتجنب commentary."""
    auds = [s for s in info.get('streams', []) if s.get('codec_type') == 'audio']
    if not auds:
        raise MediaError('الملف ده مفيهوش مسار صوت خالص.')
    fdur = _fnum(info.get('format', {}).get('duration'))

    def score(item):
        _, s = item
        dur = _fnum(s.get('duration')) or _fnum(s.get('tags', {}).get('DURATION')) or fdur
        d = s.get('disposition', {}) or {}
        penal = -1e6 if d.get('comment') or d.get('hearing_impaired') else 0
        return (penal + dur, _fnum(s.get('bit_rate')), -item[0])

    best = max(enumerate(auds), key=score)
    return best[0], best[1]


def duration_of(info):
    d = _fnum(info.get('format', {}).get('duration'))
    if d <= 0:
        d = max([_fnum(s.get('duration')) for s in info.get('streams', [])] + [0])
    return d


def _filters(normalize, pan=None):
    f = []
    if pan:
        f.append(pan)
    f.append('aresample=16000:async=1:first_pts=0')
    if normalize:
        f += ['highpass=f=70', 'dynaudnorm=f=250:g=15:m=12:p=0.9']
    return ','.join(f)


async def extract_master(src, dst, a_index, normalize=True, progress=None):
    """يحوّل أي صوت/فيديو لـ FLAC mono 16kHz. يجرب أكتر من محاولة للملفات الغريبة،
    ويعالج مشكلة القنوات المتعاكسة (phase) اللي بتخلي الـ mono صامت."""
    attempts = [
        ['-fflags', '+genpts+discardcorrupt', '-err_detect', 'ignore_err'],
        ['-fflags', '+genpts+igndts+discardcorrupt', '-err_detect', 'ignore_err', '-analyzeduration', '200M', '-probesize', '200M'],
    ]
    last_err = ''
    for flags in attempts:
        cmd = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y'] + flags + [
            '-i', src, '-map', '0:a:%d' % a_index, '-vn', '-sn', '-dn', '-ac', '1',
            '-af', _filters(normalize), '-c:a', 'flac', '-compression_level', '5', dst]
        rc, _, err = await run(cmd, 3 * 3600)
        if rc == 0 and os.path.exists(dst) and os.path.getsize(dst) > 1000:
            break
        last_err = err
    else:
        raise MediaError('ffmpeg فشل في استخراج الصوت.\n' + last_err.strip()[-300:])

    # لو الصوت شبه صامت جرّب قناة واحدة (ملفات stereo فيها قناة مقلوبة الطور)
    vol = await volume_stats(dst)
    if vol is not None and vol[1] < -55:
        for ch in ('c0', 'c1'):
            alt = dst + '.%s.flac' % ch
            cmd = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-fflags', '+genpts',
                   '-err_detect', 'ignore_err', '-i', src, '-map', '0:a:%d' % a_index, '-vn', '-sn', '-dn',
                   '-af', _filters(normalize, pan='pan=mono|c0=%s' % ch), '-c:a', 'flac', alt]
            rc, _, _ = await run(cmd, 3 * 3600)
            if rc == 0 and os.path.exists(alt):
                v2 = await volume_stats(alt)
                if v2 is not None and v2[1] > vol[1] + 10:
                    os.replace(alt, dst)
                    break
            if os.path.exists(alt):
                os.remove(alt)
    return dst


async def volume_stats(path):
    rc, _, err = await run(['ffmpeg', '-nostdin', '-hide_banner', '-i', path, '-af', 'volumedetect', '-f', 'null', '-'], 3600)
    m1 = re.search(r'mean_volume:\s*(-?[\d.]+|-inf)', err)
    m2 = re.search(r'max_volume:\s*(-?[\d.]+|-inf)', err)
    if not m1 or not m2:
        return None
    f = lambda s: -99.0 if s == '-inf' else float(s)
    return f(m1.group(1)), f(m2.group(1))


async def detect_silences(path, noise_db, min_dur):
    rc, _, err = await run(['ffmpeg', '-nostdin', '-hide_banner', '-i', path, '-af',
                            'silencedetect=noise=%ddB:d=%s' % (noise_db, min_dur), '-f', 'null', '-'], 3600)
    sil, start = [], None
    for line in err.splitlines():
        m = re.search(r'silence_start:\s*(-?[\d.]+)', line)
        if m:
            start = max(0.0, float(m.group(1)))
        m = re.search(r'silence_end:\s*([\d.]+)', line)
        if m and start is not None:
            sil.append((start, float(m.group(1))))
            start = None
    return sil


def plan_cuts(total, silences, target, lo_frac=0.55, hi_frac=1.25):
    """نقط القطع: لكل حد target دوّر على أطول سكون قريب (قبله/بعده) وقص في نصه."""
    if total <= target * hi_frac:
        return []
    cuts, pos = [], 0.0
    while total - pos > target * hi_frac:
        want = pos + target
        win_lo, win_hi = pos + target * lo_frac, pos + target * hi_frac
        cands = [(a, b) for a, b in silences if win_lo <= (a + b) / 2 <= min(win_hi, total - 5)]
        if cands:
            a, b = min(cands, key=lambda ab: abs((ab[0] + ab[1]) / 2 - want) - (ab[1] - ab[0]) * 8)
            cut = (a + b) / 2
        else:
            cut = want
        cuts.append(cut)
        pos = cut
    return cuts


async def make_chunks(master, total, outdir, target, kbps, silences):
    os.makedirs(outdir, exist_ok=True)
    cuts = plan_cuts(total, silences, target)
    bounds = [0.0] + cuts + [total]
    chunks = []
    for i in range(len(bounds) - 1):
        a, b = bounds[i], bounds[i + 1]
        dst = os.path.join(outdir, 'c%03d.mp3' % i)
        cmd = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-ss', '%.3f' % a, '-t', '%.3f' % (b - a),
               '-i', master, '-ac', '1', '-ar', '16000', '-c:a', 'libmp3lame', '-b:a', '%dk' % kbps, dst]
        rc, _, err = await run(cmd, 1800)
        if rc != 0 or not os.path.exists(dst):
            raise MediaError('فشل تقطيع الصوت: ' + err.strip()[-200:])
        chunks.append(dict(index=i, start=a, end=b, dur=b - a, path=dst, size=os.path.getsize(dst)))
    return chunks


async def prepare(src, workdir, chunk_seconds, kbps, normalize=True, on_stage=None):
    """الدالة الشاملة: src → قائمة chunks جاهزة للرفع على Groq + معلومات."""
    os.makedirs(workdir, exist_ok=True)
    info = await probe(src)
    a_idx, a_stream = pick_audio_stream(info)
    n_aud = len([s for s in info['streams'] if s.get('codec_type') == 'audio'])
    has_video = any(s.get('codec_type') == 'video' and s.get('disposition', {}).get('attached_pic') != 1
                    for s in info['streams'])
    master = os.path.join(workdir, 'master.flac')
    if on_stage:
        await on_stage('convert')
    await extract_master(src, master, a_idx, normalize)
    total = duration_of(await probe(master))
    if total < 0.5:
        raise MediaError('الصوت قصير جداً أو فاضي.')
    vol = await volume_stats(master)
    if on_stage:
        await on_stage('split')
    sil = []
    if total > chunk_seconds * 1.25:
        sil = await detect_silences(master, -35, 0.6)
        if len(sil) < total / chunk_seconds:
            sil = await detect_silences(master, -28, 0.35)
    chunks = await make_chunks(master, total, os.path.join(workdir, 'chunks'), chunk_seconds, kbps, sil)
    return dict(duration=total, chunks=chunks, audio_tracks=n_aud, has_video=has_video,
                codec=a_stream.get('codec_name'), volume=vol, master=master)
