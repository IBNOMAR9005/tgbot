# -*- coding: utf-8 -*-
"""المواضيع والتوقيتات: schema لـ Gemini + تنضيف/تصحيح التوقيتات + تنسيق رسائل تليجرام (HTML)."""
import bisect, html, json, re
from subs import hms, hms_to_sec

SCHEMA = {
    'type': 'OBJECT',
    'properties': {
        'lecture_title': {'type': 'STRING'},
        'sections': {'type': 'ARRAY', 'items': {
            'type': 'OBJECT',
            'properties': {
                'title': {'type': 'STRING'},
                'emoji': {'type': 'STRING'},
                'items': {'type': 'ARRAY', 'items': {
                    'type': 'OBJECT',
                    'properties': {'time': {'type': 'STRING'}, 'label': {'type': 'STRING'}, 'key': {'type': 'BOOLEAN'}},
                    'required': ['time', 'label', 'key']}},
            },
            'required': ['title', 'emoji', 'items']}},
    },
    'required': ['lecture_title', 'sections'],
}

_KEYCAPS = {'0': '0️⃣', '1': '1️⃣', '2': '2️⃣', '3': '3️⃣', '4': '4️⃣', '5': '5️⃣', '6': '6️⃣', '7': '7️⃣', '8': '8️⃣', '9': '9️⃣'}
_BANNED = re.compile(r'\b(axis|axes|section|part|chapter)\b\s*\d*\s*[:\-–—.]?\s*', re.I)


def keycap(n):
    return ''.join(_KEYCAPS[c] for c in str(n))


def parse_json(text):
    text = text.strip()
    text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text)
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r'\{.*\}', text, re.S)
        if not m:
            raise
        return json.loads(m.group(0))


def _clean_title(t):
    t = _BANNED.sub('', (t or '').strip())
    t = re.sub(r'^\s*[\d]+\s*[.):\-–]\s*', '', t)
    return t.strip(' -–—:') or 'Topic'


def _first_emoji(e):
    e = (e or '').strip()
    m = re.match(r'(?:[\U0001F000-\U0001FFFF\u2600-\u27BF\u2B00-\u2BFF][\uFE0F\u200D]?)+', e)
    return (m.group(0) if m else '📌')[:8]


def normalize(js, cue_starts, duration):
    """يصلّح التوقيتات (snap لأقرب سطر في التفريغ)، يرتب، يشيل المكرر. يرجّع (title, sections)."""
    starts = sorted(set(cue_starts))
    secs_out = []
    for sec in js.get('sections', []):
        items = []
        for it in sec.get('items', []):
            t = hms_to_sec(it.get('time', ''))
            label = re.sub(r'\s+', ' ', str(it.get('label', ''))).strip(' -–—•▸')
            if t is None or not label:
                continue
            if duration and t > duration + 5:
                continue
            if starts:
                i = bisect.bisect_left(starts, t)
                near = [starts[j] for j in (i - 1, i) if 0 <= j < len(starts)]
                best = min(near, key=lambda s: abs(s - t))
                if abs(best - t) <= 60:
                    t = best
            items.append((t, label, bool(it.get('key'))))
        if not items:
            continue
        items.sort(key=lambda x: x[0])
        secs_out.append(dict(title=_clean_title(sec.get('title')), emoji=_first_emoji(sec.get('emoji')), items=items))
    secs_out.sort(key=lambda s: s['items'][0][0])
    seen, final = set(), []
    for s in secs_out:
        its = []
        for t, label, key in s['items']:
            k = (round(t), label.lower())
            if k in seen:
                continue
            seen.add(k)
            its.append((t, label, key))
        if its:
            s['items'] = its
            final.append(s)
    title = re.sub(r'\s+', ' ', str(js.get('lecture_title', '') or '')).strip()
    return title, final


def stats(sections):
    return len(sections), sum(len(s['items']) for s in sections)


MAX_TOPICS = 9
MAX_LINES = 40
MSG_LIMIT = 3900      # حد تليجرام 4096؛ سايبين هامش


def tfmt(sec):
    """صيغة بيتعرّف عليها تليجرام كتوقيت قابل للضغط: MM:SS تحت الساعة، و H:MM:SS فوقها (من غير <code> عشان ما يبقاش نسخ)."""
    sec = max(0, int(round(float(sec))))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return '%d:%02d:%02d' % (h, m, s) if h else '%02d:%02d' % (m, s)


def _block(sec):
    lines = ['<blockquote>%s <b>%s</b></blockquote>' % (sec['emoji'], html.escape(sec['title']))]
    for t, label, key in sec['items']:
        lab = html.escape(label)
        lines.append('%s ▸ %s' % (tfmt(t), ('⭐ <b>%s</b>' % lab) if key else lab))
    return '\n'.join(lines)


def _compose(title, sections, duration):
    n_sec, n_items = stats(sections)
    head = '🎓 <b>%s</b>' % html.escape(title) if title else '🎓 <b>Lecture Index</b>'
    head += '\n<i>⏱ %s  ·  %d topics  ·  tap any time to jump</i>' % (tfmt(duration), n_sec)
    return head + '\n\n' + '\n\n'.join(_block(s) for s in sections)


def _shrink(sections, limit):
    """يصغّر الفهرس لحد ما يدخل في رسالة واحدة: يشيل أضعف عناصر مش مهمة (مش ⭐) من أكبر قسم."""
    secs = [dict(s, items=list(s['items'])) for s in sections]
    # حد أقصى للعدد الكلي
    while sum(len(s['items']) for s in secs) > MAX_LINES:
        if not _drop_one(secs, min_keep=2):
            break
    return secs


def _drop_one(secs, min_keep=1):
    cands = [(len(s['items']), i) for i, s in enumerate(secs) if len(s['items']) > min_keep]
    if not cands:
        return False
    _, i = max(cands)
    items = secs[i]['items']
    non_key = [k for k, it in enumerate(items) if not it[2]]
    # لو كله ⭐ شيل أي واحد من النص، غير كده شيل غير المهم من النص
    pool = non_key or list(range(len(items)))
    pool = [k for k in pool if 0 < k < len(items) - 1] or pool
    items.pop(pool[len(pool) // 2])
    return True


def render_messages(title, sections, duration, limit=MSG_LIMIT):
    """بيرجّع قايمة فيها رسالة واحدة (لازم تتبعت كـ reply على الملف الصوتي/الفيديو عشان التوقيتات تبقى قابلة للضغط)."""
    secs = sections[:MAX_TOPICS]
    secs = _shrink(secs, limit)
    msg = _compose(title, secs, duration)
    guard = 0
    while len(msg) > limit and guard < 200:
        guard += 1
        if not _drop_one(secs, 1):
            if len(secs) > 1:
                secs.pop()          # آخر حاجة: شيل آخر قسم
            else:
                break
        msg = _compose(title, secs, duration)
    return [msg[:4096]]
