# -*- coding: utf-8 -*-
"""تحويل segments بتاعة Whisper لـ SRT / TXT / نص مضغوط للـ Gemini، مع تنضيف الهلوسة."""
import re, textwrap

_HALLU = re.compile(
    r'(اشتركوا في القناة|اشترك في القناة|لا تنسوا الاشتراك|ترجمة نانسي|ترجمة\s*:|'
    r'thanks for watching|thank you for watching|subscribe to|please subscribe|amara\.org|'
    r'subtitles by|شكرا لمشاهدتكم|شكراً لمشاهدتكم)', re.I)


def ts_srt(sec):
    sec = max(0.0, float(sec))
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return '%02d:%02d:%02d,%03d' % (h, m, s, ms)


def hms(sec):
    sec = max(0, int(round(float(sec))))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return '%02d:%02d:%02d' % (h, m, s)


def hms_to_sec(t):
    t = str(t).strip().replace(',', '.')
    parts = t.split(':')
    try:
        parts = [float(p) for p in parts]
    except ValueError:
        return None
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 1:
        return parts[0]
    return None


def human_dur(sec):
    sec = int(sec)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    if h:
        return '%d:%02d:%02d' % (h, m, s)
    return '%d:%02d' % (m, s)


def clean_segments(segs):
    """segs: list of dict(start,end,text,[no_speech_prob,avg_logprob,compression_ratio]) بتوقيت مطلق."""
    out = []
    for s in sorted(segs, key=lambda x: x['start']):
        text = re.sub(r'\s+', ' ', (s.get('text') or '')).strip()
        if not text or not re.search(r'\w', text):
            continue
        nsp = s.get('no_speech_prob') or 0
        lp = s.get('avg_logprob')
        cr = s.get('compression_ratio') or 0
        if nsp > 0.85 and (lp is None or lp < -0.8):
            continue
        if cr > 2.8 and (lp is not None and lp < -1.0):
            continue
        if _HALLU.search(text) and nsp > 0.3:
            continue
        out.append(dict(start=float(s['start']), end=float(s['end']), text=text))
    # تكرار حرفي ≥3 مرات ورا بعض = هلوسة، نسيب واحدة
    res, run = [], 0
    for s in out:
        if res and s['text'] == res[-1]['text']:
            run += 1
            if run >= 2:
                continue
        else:
            run = 0
        res.append(s)
    return res


def _split_text(text, max_chars):
    """يقسم النص لقطع ≤ max_chars على علامات الترقيم أو المسافات."""
    if len(text) <= max_chars:
        return [text]
    pieces, cur = [], ''
    for tok in re.findall(r'[^\s]+\s*', text):
        if cur and len(cur) + len(tok) > max_chars:
            pieces.append(cur.strip())
            cur = ''
        cur += tok
        if re.search(r'[.!؟?،,؛:…]\s*$', tok) and len(cur) > max_chars * 0.55:
            pieces.append(cur.strip())
            cur = ''
    if cur.strip():
        pieces.append(cur.strip())
    return pieces


def to_cues(segs, max_dur=7.5, max_chars=84):
    cues = []
    for s in segs:
        dur = max(0.2, s['end'] - s['start'])
        if dur <= max_dur and len(s['text']) <= max_chars:
            cues.append([s['start'], s['end'], s['text']])
            continue
        n_by_dur = int(dur // max_dur) + (1 if dur % max_dur > 0.01 else 0)
        target = max(24, min(max_chars, int(len(s['text']) / max(1, n_by_dur)) + 12))
        parts = _split_text(s['text'], target)
        total = sum(len(p) for p in parts) or 1
        t, acc = s['start'], 0
        for i, p in enumerate(parts):
            acc += len(p)
            end = s['start'] + dur * acc / total if i < len(parts) - 1 else s['end']
            cues.append([t, max(end, t + 0.3), p])
            t = end
    # ما يتداخلوش
    for i in range(len(cues) - 1):
        if cues[i][1] > cues[i + 1][0]:
            cues[i][1] = max(cues[i][0] + 0.2, cues[i + 1][0] - 0.01)
    return [tuple(c) for c in cues]


def _wrap(text, width=42):
    lines = textwrap.wrap(text, width=width, break_long_words=False, break_on_hyphens=False)
    if len(lines) > 2:
        lines = textwrap.wrap(text, width=max(width, len(text) // 2 + 6), break_long_words=False)
    return '\n'.join(lines) if lines else text


def build_srt(cues):
    out = []
    for i, (a, b, t) in enumerate(cues, 1):
        out.append('%d\n%s --> %s\n%s\n' % (i, ts_srt(a), ts_srt(b), _wrap(t)))
    return '\n'.join(out)


def build_txt(segs, para_gap=2.0, max_para_segs=7):
    paras, cur, last_end = [], [], None
    for s in segs:
        if cur and ((last_end is not None and s['start'] - last_end > para_gap) or len(cur) >= max_para_segs):
            paras.append(' '.join(cur))
            cur = []
        cur.append(s['text'])
        last_end = s['end']
    if cur:
        paras.append(' '.join(cur))
    return '\n\n'.join(paras) + '\n'


def compact_transcript(cues, merge_gap=0.0):
    """سطر لكل cue: [HH:MM:SS] نص  — أخف بكتير من SRT للـ Gemini."""
    return '\n'.join('[%s] %s' % (hms(a), t) for a, _, t in cues)


_SRT_RE = re.compile(r'(\d+)\s*\n(\d\d:\d\d:\d\d[,.]\d{3})\s*-->\s*(\d\d:\d\d:\d\d[,.]\d{3})\s*\n(.*?)(?=\n\s*\n|\Z)', re.S)


def parse_srt(text):
    cues = []
    for m in _SRT_RE.finditer(text.replace('\r', '')):
        a, b = hms_to_sec(m.group(2)), hms_to_sec(m.group(3))
        cues.append((a, b, ' '.join(m.group(4).split())))
    return cues
