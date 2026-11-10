#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lecture_to_pdf.py
=================
يحوّل ناتج الـ AI (صيغة @@ markers) إلى ملزمة مذاكرة PDF بصفحات A4.

الاستخدام:
    python lecture_to_pdf.py notes.txt -o lecture.pdf
    python lecture_to_pdf.py part1.txt part2.txt -o lecture.pdf --srt lecture.srt --fonts ./fonts

المتطلبات: reportlab فقط  (pip install reportlab)
    - الكود فيه محرك عربي خاص (تشكيل الحروف + ترتيب عربي/إنجليزي لكل سطر)
      فمش محتاج arabic-reshaper ولا python-bidi.
    - أفضل خطوط: حط في فولدر fonts/ جنب السكريبت أي زوج من:
      Amiri-Regular/Amiri-Bold  أو  Cairo-Regular/Cairo-Bold  أو  NotoNaskhArabic-Regular/Bold
      (لازم TTF ثابت مش variable). لو مفيش، بياخد Arial/Tahoma/DejaVu من السيستم.

لو الناتج جه على أكتر من رد (انقطاع API): الصق الملفات ورا بعض أو مرّرها كلها
للسكريبت. لو موضوع اتكرر، بياخد النسخة المكتملة (DONE) وبيتجاهل الناقصة.
"""
import os, re, sys, copy, math, argparse, unicodedata
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor, white
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (BaseDocTemplate, PageTemplate, Frame, Flowable,
                                Table, TableStyle, Spacer, CondPageBreak, KeepTogether)

HERE = os.path.dirname(os.path.abspath(__file__))

# ════════════════════════════════════════════════════════════════════
#  1) الخطوط
# ════════════════════════════════════════════════════════════════════
FONT_PAIRS = {
    'ar': [('Amiri-Regular', 'Amiri-Bold'), ('Cairo-Regular', 'Cairo-Bold'),
           ('Tajawal-Regular', 'Tajawal-Bold'), ('NotoNaskhArabic-Regular', 'NotoNaskhArabic-Bold'),
           ('NotoSansArabic-Regular', 'NotoSansArabic-Bold'), ('arial', 'arialbd'),
           ('tahoma', 'tahomabd'), ('segoeui', 'segoeuib'), ('DejaVuSans', 'DejaVuSans-Bold')],
    'la': [('Carlito-Regular', 'Carlito-Bold'), ('calibri', 'calibrib'), ('segoeui', 'segoeuib'),
           ('LiberationSans-Regular', 'LiberationSans-Bold'), ('arial', 'arialbd'),
           ('DejaVuSans', 'DejaVuSans-Bold')],
    'sym': [('DejaVuSans', 'DejaVuSans-Bold'), ('seguisym', 'seguisym'), ('arial', 'arialbd')],
}
F = {}     # 'AR' -> (regular, bold)
COV = {}   # 'AR' -> set of covered codepoints


def _index_fonts(extra):
    dirs = [extra, os.path.join(HERE, 'fonts'), HERE, os.path.expanduser('~/.fonts'),
            os.path.expanduser('~/Library/Fonts'), '/usr/share/fonts', '/usr/local/share/fonts',
            '/Library/Fonts', '/System/Library/Fonts/Supplemental', 'C:\\Windows\\Fonts', '/content']
    idx = {}
    for d in dirs:
        if d and os.path.isdir(d):
            for root, _, files in os.walk(d):
                for fn in files:
                    if fn.lower().endswith('.ttf'):
                        idx.setdefault(fn[:-4].lower(), os.path.join(root, fn))
    return idx


def _covers(path, cps):
    try:
        face = TTFont('_probe', path).face.charToGlyph
        return all(cp in face for cp in cps)
    except Exception:
        return False


def setup_fonts(extra=None):
    idx = _index_fonts(extra)
    need_ar = [0xFE8D, 0xFEDF, 0xFEFB, 0x0640, 0x061F, 0x060C]
    chosen = {}
    for fam, key, need in (('ar', 'AR', need_ar), ('la', 'LA', [0x41, 0x61, 0x30]), ('sym', 'SYM', [0x2192])):
        got = None
        for reg, bold in FONT_PAIRS[fam]:
            r = idx.get(reg.lower())
            b = idx.get(bold.lower()) or r
            if r and _covers(r, need):
                got = (reg, r, b)
                break
        if not got:
            if key == 'AR':
                sys.exit('✗ مفيش خط عربي مناسب. حط Amiri أو Cairo (TTF) في فولدر fonts/ جنب السكريبت.')
            F[key] = F['LA']
            COV[key] = COV['LA']
            continue
        pdfmetrics.registerFont(TTFont(key, got[1]))
        pdfmetrics.registerFont(TTFont(key + '-B', got[2]))
        F[key] = (key, key + '-B')
        COV[key] = set(pdfmetrics.getFont(key).face.charToGlyph.keys())
        chosen[key] = got[0]
    print('الخطوط:', ', '.join('%s=%s' % kv for kv in chosen.items()))


def neutral_family(t):
    for fam in ('AR', 'LA', 'SYM'):
        cov = COV[fam]
        if all(ord(ch) in cov for ch in t):
            return fam
    return 'AR'


# ════════════════════════════════════════════════════════════════════
#  2) محرك العربي: تشكيل الحروف
# ════════════════════════════════════════════════════════════════════
DUAL = set('بتثجحخسشصضطظعغفقكلمنهيىئ\u0640')
RIGHT = set('اأإآٱدذرزوؤة')
NONJOIN = set('ء')
_FORMS, _LIG = {}, {}
for _cp in range(0xFE70, 0xFEFF):
    _d = unicodedata.decomposition(chr(_cp))
    if not _d.startswith('<'):
        continue
    _tag, _rest = _d.split(' ', 1)
    _tag = _tag.strip('<>')
    _parts = _rest.split()
    if _tag not in ('isolated', 'final', 'initial', 'medial'):
        continue
    if len(_parts) == 1:
        _FORMS.setdefault(chr(int(_parts[0], 16)), {}).setdefault(_tag, chr(_cp))
    elif len(_parts) == 2 and _parts[0] == '0644':
        _LIG.setdefault((chr(0x644), chr(int(_parts[1], 16))), {})[_tag] = chr(_cp)

_MARKS = re.compile('[\u064B-\u065F\u0670\u06D6-\u06ED]')


def shape(s):
    s = _MARKS.sub('', s)
    out, n, i, prev_dual = [], len(s), 0, False
    while i < n:
        c = s[i]
        if c not in DUAL and c not in RIGHT and c not in NONJOIN:
            out.append(c); prev_dual = False; i += 1
            continue
        if c == 'ل' and i + 1 < n and s[i + 1] in 'آأإا':
            lig = _LIG.get(('ل', s[i + 1]), {}).get('final' if prev_dual else 'isolated')
            if lig:
                out.append(lig); prev_dual = False; i += 2
                continue
        nxt = i + 1 < n and (s[i + 1] in DUAL or s[i + 1] in RIGHT)
        if c in DUAL:
            f = 'medial' if (prev_dual and nxt) else 'final' if prev_dual else 'initial' if nxt else 'isolated'
            prev_dual = True
        elif c in RIGHT:
            f = 'final' if prev_dual else 'isolated'; prev_dual = False
        else:
            f = 'isolated'; prev_dual = False
        out.append(c if c == '\u0640' else _FORMS.get(c, {}).get(f, c))
        i += 1
    return ''.join(out)


# ════════════════════════════════════════════════════════════════════
#  3) محرك النص: chunks + ترتيب ثنائي الاتجاه لكل سطر + قصّ السطور
# ════════════════════════════════════════════════════════════════════
AR = '\u0621-\u064A\u0640\u0671-\u06D3'
LA = 'A-Za-z0-9\u00C0-\u024F\u0370-\u03FF'
TOKEN_RE = re.compile(
    r'(?P<sp>[ \t\n\r\xa0]+)'
    rf'|(?P<ar>[{AR}]+)'
    rf"|(?P<la>[<>=~≥≤±×]?[{LA}]+(?:[-'’./:+#&_]+[{LA}]+)*%?)"
    rf'|(?P<ne>[^ \t\n\r\xa0{AR}{LA}]+)')
_ADIG = str.maketrans('٠١٢٣٤٥٦٧٨٩٪٫', '0123456789%.')
_MIRROR = str.maketrans('()[]{}«»', ')(][}{»«')


def H(x):
    return HexColor(x)


INK, MUTED, LINE = H('#1F2937'), H('#6B7280'), H('#D7DDE8')
LATIN, NUMC, QCOL, BOLDC = H('#0B5FA5'), H('#B45309'), H('#E67E22'), H('#0F2A4A')


def PS(size=9.7, lead=None, color=INK, bold=False, align='auto', justify=False,
       la=LATIN, num=NUMC, boldc=BOLDC, sb=0, sa=0, lascale=1.0):
    return dict(size=size, lead=lead or size * 1.5, color=color, bold=bold, align=align,
                justify=justify, la=la, num=num, boldc=boldc, sb=sb, sa=sa, lascale=lascale)


class Chunk:
    __slots__ = ('t', 'kind', 'bold', 'special', 'font', 'size', 'color', 'w', 'vis', 'shaped')

    def __init__(self, t, kind, bold=False, special=None):
        self.t, self.kind, self.bold, self.special = t, kind, bold, special
        self.vis = t


def style_chunk(ch, st):
    b = ch.bold or st['bold']
    if ch.kind == 'R':
        ch.shaped, fam = shape(ch.t), 'AR'
    elif ch.kind in ('L', 'D'):
        ch.shaped, fam = ch.t, 'LA'
    else:
        ch.shaped, fam = ch.t, neutral_family(ch.t)
    ch.font = F[fam][1 if b else 0]
    ch.size = st['size'] * (st['lascale'] if fam == 'LA' else 1.0)
    if ch.special == 'q':
        ch.color = QCOL
    elif ch.kind == 'L':
        ch.color = st['la'] or st['color']
    elif ch.kind == 'D':
        ch.color = st['num'] or st['color']
    elif ch.kind == 'R' and ch.bold and st['boldc'] is not None:
        ch.color = st['boldc'] if st['color'] is INK else st['color']
    else:
        ch.color = st['color']
    ch.w = pdfmetrics.stringWidth(ch.shaped, ch.font, ch.size)
    ch.vis = ch.shaped


def parse_inline(text):
    parts, bold = [], False
    for tok in re.split(r'(\*\*|\[؟\]|\[\?\])', text):
        if tok == '**':
            bold = not bold
        elif tok in ('[؟]', '[?]'):
            parts.append(('؟', True, 'q'))
        elif tok:
            parts.append((tok, bold, None))
    return parts


def make_chunks(text, st):
    chunks, has_ar = [], False
    for txt, bold, special in parse_inline(text):
        txt = txt.translate(_ADIG)
        for m in TOKEN_RE.finditer(txt):
            g, s = m.lastgroup, m.group()
            if g == 'sp':
                if chunks and chunks[-1].kind == 'S':
                    continue
                ch = Chunk(' ', 'S')
            elif g == 'ar':
                ch = Chunk(s, 'R', bold); has_ar = True
            elif g == 'la':
                first = re.search(f'[{LA}]', s).group()
                ch = Chunk(s, 'D' if first.isdigit() else 'L', bold)
            else:
                ch = Chunk(s, 'N', bold, special)
            style_chunk(ch, st)
            chunks.append(ch)
    while chunks and chunks[0].kind == 'S':
        chunks.pop(0)
    while chunks and chunks[-1].kind == 'S':
        chunks.pop()
    return chunks, has_ar


def reorder(line, rtl):
    """ترتيب مرئي لسطر واحد (UBA مبسّط على مستوى الـ chunks)."""
    n = len(line)
    if n == 0:
        return []
    e = 1 if rtl else 0
    sos = 'R' if rtl else 'L'
    t, last = [], sos
    for ch in line:
        k = ch.kind
        if k in ('R', 'L'):
            last = k; t.append(k)
        elif k == 'D':
            t.append('L' if last == 'L' else 'D')
        else:
            t.append('N')

    def sd(k):
        return 'L' if k == 'L' else 'R'
    res, i = t[:], 0
    while i < n:
        if t[i] == 'N':
            j = i
            while j < n and t[j] == 'N':
                j += 1
            left = sd(t[i - 1]) if i > 0 else sos
            right = sd(t[j]) if j < n else sos
            d = left if left == right else sos
            for k in range(i, j):
                res[k] = d
            i = j
        else:
            i += 1
    levels = []
    for k in res:
        if k == 'L':
            levels.append(e if e % 2 == 0 else e + 1)
        elif k == 'R':
            levels.append(e if e % 2 == 1 else e + 1)
        else:
            levels.append(e + 2 if e % 2 == 0 else e + 1)
    order = list(range(n))
    odds = [l for l in levels if l % 2 == 1]
    if odds:
        for lvl in range(max(levels), min(odds) - 1, -1):
            i = 0
            while i < n:
                if levels[order[i]] >= lvl:
                    j = i
                    while j < n and levels[order[j]] >= lvl:
                        j += 1
                    order[i:j] = reversed(order[i:j])
                    i = j
                else:
                    i += 1
    out = []
    for idx in order:
        ch = line[idx]
        if levels[idx] % 2 == 1:
            if ch.kind == 'R':
                ch.vis = ch.shaped[::-1]
            elif ch.kind == 'N':
                ch.vis = ch.shaped[::-1].translate(_MIRROR)
            else:
                ch.vis = ch.shaped
        else:
            ch.vis = ch.shaped
        out.append(ch)
    return out


def _space(spw):
    s = Chunk(' ', 'S')
    s.w, s.shaped, s.vis, s.font, s.size, s.color = spw, ' ', ' ', F['AR'][0], 0, INK
    return s


def break_lines(chunks, width, first_indent, spw):
    words, cur = [], []
    for ch in chunks:
        if ch.kind == 'S':
            if cur:
                words.append(cur); cur = []
        else:
            cur.append(ch)
    if cur:
        words.append(cur)
    lines, line, lw, avail = [], [], 0.0, width - first_indent
    for w in words:
        ww = sum(c.w for c in w)
        if line and lw + spw + ww > avail + 0.5:
            lines.append(line); line = list(w); lw = ww; avail = width
        else:
            if line:
                line.append(_space(spw)); lw += spw
            line.extend(w); lw += ww
    if line:
        lines.append(line)
    return lines


def _draw_visual(c, vis, x, y, extra=0.0):
    for ch in vis:
        if ch.kind != 'S':
            c.setFont(ch.font, ch.size)
            c.setFillColor(ch.color)
            c.drawString(x, y, ch.vis)
            x += ch.w
        else:
            x += ch.w + extra


def text_width(text, st):
    chunks, _ = make_chunks(text, st)
    return sum(ch.w for ch in chunks)


def draw_text(c, text, x, y, st, align='R', maxw=None):
    """يرسم سطر واحد. align: R (x = الحافة اليمنى) / L / C. بيصغّر الخط لو زاد عن maxw."""
    cur = st
    for sc in (1, .93, .86, .8, .74, .68):
        cur = dict(st, size=st['size'] * sc)
        chunks, rtl = make_chunks(text, cur)
        w = sum(ch.w for ch in chunks)
        if maxw is None or w <= maxw:
            break
    if maxw is not None and w > maxw:
        while len(text) > 4 and w > maxw:
            text = text[:-2]
            chunks, rtl = make_chunks(text + '…', cur)
            w = sum(ch.w for ch in chunks)
    vis = reorder(chunks, rtl)
    x0 = x - w if align == 'R' else x - w / 2 if align == 'C' else x
    _draw_visual(c, vis, x0, y)
    return w


# ════════════════════════════════════════════════════════════════════
#  4) RichPara: فقرة عربي/إنجليزي قابلة للتقسيم بين الصفحات
# ════════════════════════════════════════════════════════════════════
class RichPara(Flowable):
    def __init__(self, text, st, deco=None):
        Flowable.__init__(self)
        self.text, self.st, self.deco = text, st, deco or {}
        self.chunks, self.rtl = make_chunks(text, st)
        self.spw = pdfmetrics.stringWidth(' ', F['AR'][0], st['size'])
        self._lines, self._sliced = None, False
        self.is_first = self.is_last = True
        self.spaceBefore, self.spaceAfter = st['sb'], st['sa']
        k = self.deco.get('kind')
        self.pl = self.pr = self.pt = self.pb = self.gut = 0
        if k == 'box':
            self.pl, self.pr, self.pt, self.pb = 7, 11.5, 5, 5
        elif k == 'bullet':
            self.gut = 12
        elif k == 'num':
            self.gut = 18
        elif k == 'h2':
            self.pr, self.pt, self.pb = 9, 3, 3.5
        self.chip_w = 0
        if k == 'box' and self.deco.get('label'):
            self.chip_st = PS(7.3, bold=True, color=white, la=white, num=white, boldc=None)
            self.chip_w = text_width(self.deco['label'], self.chip_st) + 12

    def getSpaceBefore(self):
        return self.spaceBefore

    def getSpaceAfter(self):
        return self.spaceAfter

    def natural(self):
        return sum(c.w for c in self.chunks)

    def minw(self):
        best = cur = 0
        for ch in self.chunks:
            if ch.kind == 'S':
                best = max(best, cur); cur = 0
            else:
                cur += ch.w
        return max(best, cur)

    def wrap(self, aw, ah):
        self.aw = aw
        self.tw = aw - self.pl - self.pr - self.gut
        if not self._sliced:
            fi = self.chip_w + 6 if self.chip_w else 0
            self._lines = break_lines(self.chunks, self.tw, fi, self.spw)
        self.width = aw
        self.height = self.pt + self.pb + len(self._lines) * self.st['lead']
        return aw, self.height

    def split(self, aw, ah):
        if self._lines is None or self.aw != aw:
            self.wrap(aw, ah)
        n, lead = len(self._lines), self.st['lead']
        fit = int((ah - self.pt - self.pb) // lead)
        if fit >= n or n < 3 or fit < 2:
            return []
        if n - fit < 2:
            fit = n - 2
        if fit < 2:
            return []
        a, b = copy.copy(self), copy.copy(self)
        a._lines, b._lines = self._lines[:fit], self._lines[fit:]
        a._sliced = b._sliced = True
        a.is_last, b.is_first = False, False
        a.spaceAfter, b.spaceBefore = 0, 0
        return [a, b]

    def draw(self):
        c, st, d = self.canv, self.st, self.deco
        W, Hh, k = self.width, self.height, d.get('kind')
        size, lead = st['size'], st['lead']
        if k == 'box':
            c.setFillColor(d['bg']); c.setStrokeColor(d['bd']); c.setLineWidth(.6)
            c.roundRect(0, 0, W, Hh, 3.2, stroke=1, fill=1)
            c.setFillColor(d['bd'])
            c.roundRect(W - 4.4, 0, 4.4, Hh, 2.2, stroke=0, fill=1)
        elif k == 'h2':
            c.setFillColor(d['bd']); c.rect(W - 3.4, 2, 3.4, Hh - 4, stroke=0, fill=1)
            c.setStrokeColor(d['line']); c.setLineWidth(.7); c.line(0, .6, W - 9, .6)
        x_l, x_r = self.pl, W - self.pr - self.gut
        align = st['align'] if st['align'] != 'auto' else ('R' if self.rtl else 'L')
        for i, line in enumerate(self._lines):
            base = Hh - self.pt - i * lead - (lead - size) / 2 - size * .82
            first = (i == 0 and self.is_first)
            fi = (self.chip_w + 6) if (first and self.chip_w) else 0
            avail = self.tw - fi
            vis = reorder(line, self.rtl)
            lw = sum(ch.w for ch in vis)
            nsp = sum(1 for ch in vis if ch.kind == 'S')
            extra = 0.0
            last = (i == len(self._lines) - 1) and self.is_last
            if st['justify'] and not last and nsp and avail * .72 < lw < avail:
                extra = (avail - lw) / nsp
                lw = avail
            if align == 'R':
                x = x_r - fi - lw
            elif align == 'L':
                x = x_l
            else:
                x = x_l + (self.tw - lw) / 2
            _draw_visual(c, vis, x, base, extra)
            if first:
                yc = base + size * .32
                if self.chip_w:
                    c.setFillColor(d['bd'])
                    c.roundRect(x_r - self.chip_w, yc - 6.2, self.chip_w, 12.4, 3.4, stroke=0, fill=1)
                    draw_text(c, d['label'], x_r - self.chip_w / 2, yc - 2.5, self.chip_st, 'C')
                if k == 'bullet':
                    bx = W - self.pr - 5
                    c.setFillColor(d['bd']); c.setStrokeColor(d['bd'])
                    if d.get('mk') == 'tick':
                        c.setLineWidth(1.5)
                        c.line(bx - 3.4, yc, bx - 1, yc - 2.4); c.line(bx - 1, yc - 2.4, bx + 3.4, yc + 2.6)
                    else:
                        c.circle(bx, yc, 2.2, stroke=0, fill=1)
                elif k == 'num':
                    bx = W - self.pr - 8
                    c.setFillColor(d['bd']); c.circle(bx, yc, 6.2, stroke=0, fill=1)
                    nst = PS(7.6, bold=True, color=white, la=white, num=white, boldc=None)
                    draw_text(c, str(d.get('n', '')), bx, yc - 2.6, nst, 'C')


# ════════════════════════════════════════════════════════════════════
#  5) ألوان وأنواع البلوكات
# ════════════════════════════════════════════════════════════════════
ACCENTS = [('#1E3A5F', '#E9F0F9', '#7FA2CC'), ('#0F766E', '#E5F4F2', '#6DBDB5'),
           ('#3730A3', '#ECEBFA', '#8C86D6'), ('#9D174D', '#FBE8F1', '#D98AAE'),
           ('#166534', '#E6F4EA', '#7BBF92'), ('#7C2D12', '#FBEDE4', '#D69A78')]
BLOCKS = {
    'DEF': ('تعريف', '#1D4ED8', '#EAF1FF'),
    'KEY': ('نقطة أساسية', '#C2410C', '#FFF1E6'),
    'EMPH': ('الدكتور شدد عليها', '#6D28D9', '#F2ECFE'),
    'WARN': ('تحذير', '#B91C1C', '#FEECEC'),
    'CLIN': ('حالة إكلينيكية', '#047857', '#E6F6EF'),
    'NOTE': ('ملحوظة', '#475569', '#F0F3F7'),
}


def box_deco(kind):
    label, bd, bg = BLOCKS[kind]
    return dict(kind='box', bg=H(bg), bd=H(bd), label=label)


# ════════════════════════════════════════════════════════════════════
#  6) فلوابلز مخصّصة
# ════════════════════════════════════════════════════════════════════
class TitleBlock(Flowable):
    def __init__(self, meta, acc):
        Flowable.__init__(self)
        self.meta, self.acc = meta, acc

    def wrap(self, aw, ah):
        self.width, self.height = aw, 84
        return aw, 84

    def draw(self):
        c, W, h, m = self.canv, self.width, self.height, self.meta
        dark, mid = H(self.acc[0]), H(self.acc[2])
        c.setFillColor(dark); c.roundRect(0, 0, W, h, 7, stroke=0, fill=1)
        c.saveState(); c.setFillAlpha(.07); c.setFillColor(white)
        c.circle(W * .52, h + 6, 58, stroke=0, fill=1); c.circle(W * .40, -14, 44, stroke=0, fill=1)
        c.restoreState()
        c.setFillColor(H('#FFD66B')); c.roundRect(0, 0, 96, h, 7, stroke=0, fill=1)
        c.rect(60, 0, 40, h, stroke=0, fill=1)
        c.setFillColor(dark); c.rect(96, 0, 3, h, stroke=0, fill=1)
        num = str(m.get('number', '')).strip() or '--'
        draw_text(c, 'LECTURE', 48, h - 20, PS(8, color=dark, bold=True, la=dark, num=dark, boldc=None), 'C')
        draw_text(c, num, 48, h / 2 - 15, PS(38, color=dark, bold=True, la=dark, num=dark, boldc=None), 'C', maxw=84)
        draw_text(c, 'محاضرة', 48, 10, PS(8.5, color=dark, bold=True, la=dark, num=dark, boldc=None), 'C')
        draw_text(c, 'ملزمة مذاكرة  ·  STUDY NOTES', W - 16, h - 20,
                  PS(8, color=H('#FFD66B'), bold=True, la=H('#FFD66B'), num=H('#FFD66B'), boldc=None), 'R')
        draw_text(c, m.get('title', ''), W - 16, h / 2 - 7,
                  PS(21, color=white, bold=True, la=white, num=white, boldc=None), 'R', maxw=W - 140)
        sub = '  ·  '.join(x for x in (m.get('doctor', ''), m.get('course', '')) if x)
        if sub:
            draw_text(c, sub, W - 16, 11, PS(9.2, color=H('#D5E3F5'), la=H('#D5E3F5'), num=H('#D5E3F5'), boldc=None),
                      'R', maxw=W - 140)


class TopicBanner(Flowable):
    is_banner = True

    def __init__(self, t, acc):
        Flowable.__init__(self)
        self.tid, self.title, self.en = t['id'], t['title'], t['en']
        self.time, self.srt, self.accent = t['time'], t['srt'], acc

    def wrap(self, aw, ah):
        self.width, self.height = aw, 38
        return aw, 38

    def draw(self):
        c, W, h = self.canv, self.width, self.height
        dark, mid = H(self.accent[0]), H(self.accent[2])
        c.setFillColor(dark); c.roundRect(0, 0, W, h, 5, stroke=0, fill=1)
        c.setFillColor(mid); c.rect(0, 0, 6, h, stroke=0, fill=1)
        c.roundRect(0, 0, 8, h, 3, stroke=0, fill=1)
        c.setFillColor(dark); c.rect(5, 0, 6, h, stroke=0, fill=1)
        c.setFillColor(white); c.circle(W - 21, h / 2, 12.6, stroke=0, fill=1)
        draw_text(c, str(self.tid), W - 21, h / 2 - 4.6, PS(13.5, color=dark, bold=True, la=dark, num=dark, boldc=None), 'C')
        light = H('#D5E3F5')
        ty = h - 17 if self.en else h / 2 - 5
        draw_text(c, self.title, W - 42, ty, PS(14.2, color=white, bold=True, la=white, num=white, boldc=None),
                  'R', maxw=W - 42 - 150)
        if self.en:
            draw_text(c, self.en, W - 42, 7.5, PS(8.4, color=light, la=light, num=light, boldc=None), 'R', maxw=W - 200)
        sm = PS(7.8, color=light, la=light, num=light, boldc=None)
        if self.time:
            draw_text(c, self.time, 18, h - 15, sm, 'L')
        if self.srt:
            draw_text(c, 'SRT ' + self.srt, 18, 7.5, sm, 'L')
        key = 'topic%s' % self.tid
        c.bookmarkPage(key)
        try:
            c.addOutlineEntry('%s. %s' % (self.tid, self.title), key, 0)
        except Exception:
            pass


class TocBlock(Flowable):
    def __init__(self, entries, acc, links):
        Flowable.__init__(self)
        self.entries, self.acc, self.links = entries, acc, links

    def wrap(self, aw, ah):
        n = len(self.entries)
        self.cols = 2 if n > 5 else 1
        self.rows = max(1, math.ceil(n / self.cols))
        self.rh = 15.2
        self.width, self.height = aw, 26 + self.rows * self.rh + 5
        return aw, self.height

    def draw(self):
        c, W, h = self.canv, self.width, self.height
        dark, light, mid = [H(x) for x in self.acc]
        c.setFillColor(light); c.setStrokeColor(mid); c.setLineWidth(.6)
        c.roundRect(0, 0, W, h, 5, stroke=1, fill=1)
        c.setFillColor(dark); c.rect(W - 4, 6, 4, h - 12, stroke=0, fill=1)
        draw_text(c, 'الفهرس  ·  Contents', W - 14, h - 17, PS(10, color=dark, bold=True, la=dark, num=dark, boldc=None), 'R')
        c.setStrokeColor(mid); c.setLineWidth(.5); c.line(10, h - 22, W - 12, h - 22)
        pad, gap = 12, 16
        colw = (W - 2 * pad - gap * (self.cols - 1)) / self.cols
        for i, (tid, title, page, acc) in enumerate(self.entries):
            col, row = divmod(i, self.rows)
            xr = W - pad - col * (colw + gap)
            y = h - 26 - (row + 1) * self.rh + 4.6
            c.setFillColor(H(acc[0])); c.circle(xr - 6, y + 3.1, 5.6, stroke=0, fill=1)
            draw_text(c, str(tid), xr - 6, y + .6, PS(7.2, color=white, bold=True, la=white, num=white, boldc=None), 'C')
            draw_text(c, title, xr - 17, y, PS(8.8, color=INK, boldc=None), 'R', maxw=colw - 17 - 26)
            draw_text(c, str(page), xr - colw, y, PS(8.6, color=dark, bold=True, la=dark, num=dark, boldc=None), 'L')
            c.setStrokeColor(LINE); c.setLineWidth(.4); c.setDash(1, 2)
            c.line(xr - colw + 20, y - 2.6, xr - 17, y - 2.6)
            c.setDash()
            if self.links:
                c.linkRect('', 'topic%s' % tid, (xr - colw, y - 4, xr, y + 9), relative=1, thickness=0)


class FlowChart(Flowable):
    def __init__(self, steps, acc):
        Flowable.__init__(self)
        self.steps, self.acc = steps, acc
        self.paras = [RichPara(s, PS(8.8, 12.2, align='C', boldc=None)) for s in steps]

    def wrap(self, aw, ah):
        n = len(self.steps)
        per = n if n <= 4 else (3 if n in (5, 6, 9) else 4)
        self.per, self.gap = per, 17
        self.bw = (aw - (per - 1) * self.gap) / per
        for p in self.paras:
            p.wrap(self.bw - 14, 999)
        self.rows = [list(range(i, min(i + per, n))) for i in range(0, n, per)]
        self.rowh = [max(30, max(self.paras[i].height for i in r) + 12) for r in self.rows]
        self.vgap = 15
        self.width = aw
        self.height = sum(self.rowh) + self.vgap * (len(self.rows) - 1) + 8
        self.spaceBefore, self.spaceAfter = 1, 6
        return aw, self.height

    def _arrow(self, c, x1, y1, x2, y2):
        ang = math.atan2(y2 - y1, x2 - x1)
        c.line(x1, y1, x2, y2)
        p = c.beginPath()
        p.moveTo(x2, y2)
        p.lineTo(x2 - 5 * math.cos(ang - .45), y2 - 5 * math.sin(ang - .45))
        p.lineTo(x2 - 5 * math.cos(ang + .45), y2 - 5 * math.sin(ang + .45))
        p.close()
        c.drawPath(p, stroke=0, fill=1)

    def draw(self):
        c = self.canv
        dark, light, mid = [H(x) for x in self.acc]
        y_top = self.height - 4
        geo = {}
        for r, idxs in enumerate(self.rows):
            rh = self.rowh[r]
            y0 = y_top - rh
            for k, i in enumerate(idxs):
                x = self.width - (k + 1) * self.bw - k * self.gap
                geo[i] = (x, y0, rh)
                c.setFillColor(light); c.setStrokeColor(mid); c.setLineWidth(.9)
                c.roundRect(x, y0, self.bw, rh, 4, stroke=1, fill=1)
                p = self.paras[i]
                p.canv = c
                c.saveState(); c.translate(x + 7, y0 + (rh - p.height) / 2); p.draw(); c.restoreState()
                c.setFillColor(dark); c.circle(x + self.bw - 1, y0 + rh - 1, 7, stroke=0, fill=1)
                draw_text(c, str(i + 1), x + self.bw - 1, y0 + rh - 3.7,
                          PS(8, color=white, bold=True, la=white, num=white, boldc=None), 'C')
            y_top = y0 - self.vgap
        c.setStrokeColor(dark); c.setFillColor(dark); c.setLineWidth(1.3)
        for i in range(len(self.steps) - 1):
            x, y, rh = geo[i]
            if (i + 1) in geo and geo[i + 1][1] == y:
                self._arrow(c, x - 2, y + rh / 2, x - self.gap + 2.5, y + rh / 2)
            else:
                nx, ny, nrh = geo[i + 1]
                xm, mid_y = x + self.bw / 2, y - self.vgap / 2
                c.line(xm, y, xm, mid_y)
                c.line(xm, mid_y, nx + self.bw / 2, mid_y)
                self._arrow(c, nx + self.bw / 2, mid_y, nx + self.bw / 2, ny + nrh + 1.5)


class MnemoBox(Flowable):
    def __init__(self, word, meaning):
        Flowable.__init__(self)
        self.word = word
        items = [x.strip() for x in re.split(r'[;؛]', meaning) if x.strip()]
        self.rtl = bool(re.search(f'[{AR}]', word))
        self.tiles = []
        letters = [ch for ch in word if ch.isalnum()]
        for i, it in enumerate(items):
            m = re.match(r'^\s*(\S)\s*[=:：\-–]\s*(.+)$', it)
            if m:
                lt, tx = m.group(1), m.group(2)
            else:
                lt, tx = (letters[i] if i < len(letters) else ''), it
            self.tiles.append((lt, RichPara(tx, PS(8.4, 11.6, align='C', boldc=None))))

    def wrap(self, aw, ah):
        n = max(1, len(self.tiles))
        per = min(n, 6)
        self.per, self.g = per, 6
        self.tw = (aw - 16 - (per - 1) * self.g) / per
        for _, p in self.tiles:
            p.wrap(self.tw - 8, 999)
        self.rows = [self.tiles[i:i + per] for i in range(0, n, per)]
        self.rowh = [26 + max(p.height for _, p in r) + 6 for r in self.rows]
        self.width = aw
        self.height = 24 + sum(self.rowh) + 6
        self.spaceBefore, self.spaceAfter = 1, 6
        return aw, self.height

    def draw(self):
        c, W, h = self.canv, self.width, self.height
        gold, bg = H('#A16207'), H('#FFF8DB')
        c.setFillColor(bg); c.setStrokeColor(H('#D9B400')); c.setLineWidth(.7)
        c.roundRect(0, 0, W, h, 4, stroke=1, fill=1)
        c.setFillColor(H('#D9B400')); c.roundRect(W - 4.4, 0, 4.4, h, 2.2, stroke=0, fill=1)
        chip = PS(7.6, bold=True, color=white, la=white, num=white, boldc=None)
        label = 'Mnemonic  ·  ' + self.word
        cw = text_width(label, chip) + 14
        c.setFillColor(gold); c.roundRect(W - 12 - cw, h - 19, cw, 13, 3.5, stroke=0, fill=1)
        draw_text(c, label, W - 12 - cw / 2, h - 15.3, chip, 'C')
        y_top = h - 24
        for r, row in enumerate(self.rows):
            rh = self.rowh[r]
            for k, (lt, p) in enumerate(row):
                x = (W - 8 - (k + 1) * self.tw - k * self.g) if self.rtl else (8 + k * (self.tw + self.g))
                c.setFillColor(white); c.setStrokeColor(H('#E4C94A')); c.setLineWidth(.6)
                c.roundRect(x, y_top - rh, self.tw, rh, 3, stroke=1, fill=1)
                draw_text(c, lt, x + self.tw / 2, y_top - 20, PS(17, color=gold, bold=True, la=gold, num=gold, boldc=None), 'C')
                p.canv = c
                c.saveState(); c.translate(x + 4, y_top - rh + 4); p.draw(); c.restoreState()
            y_top -= rh


# ════════════════════════════════════════════════════════════════════
#  7) جداول وصناديق
# ════════════════════════════════════════════════════════════════════
def build_table(t, W, acc):
    dark, light, mid = [H(x) for x in acc]
    kind = t['kind']
    rows = [r for r in t['rows'] if r]
    if not rows:
        return []
    nc = max(len(r) for r in rows)
    rows = [r + [''] * (nc - len(r)) for r in rows]
    hdr = {'compare': dark, 'classify': H('#0F766E'), 'values': H('#334155')}.get(kind, dark)
    paras = []
    for ri, r in enumerate(rows):
        pr = []
        for ci, cell in enumerate(r):
            if ri == 0:
                st = PS(8.8, 12, color=white, bold=True, la=white, num=white, boldc=None, align='C')
            else:
                bold = ci == 0 and kind in ('compare', 'classify')
                st = PS(8.7, 12.4, bold=bold, align='C' if kind == 'values' and ci > 0 else 'auto')
            pr.append(RichPara(cell, st))
        paras.append(pr)
    pad = 11
    cols = list(zip(*paras))
    mins = [max(p.minw() for p in col) + pad + 2 for col in cols]
    prefs = [max(p.natural() for p in col) + pad + 2 for col in cols]
    if sum(prefs) <= W:
        widths = [p * W / sum(prefs) for p in prefs]
    elif sum(mins) >= W:
        widths = [m * W / sum(mins) for m in mins]
    else:
        spare, need = W - sum(mins), [p - m for p, m in zip(prefs, mins)]
        tot = sum(need) or 1
        widths = [m + spare * nd / tot for m, nd in zip(mins, need)]
    data = [list(reversed(r)) for r in paras]
    widths = widths[::-1]
    ts = [('BACKGROUND', (0, 0), (-1, 0), hdr), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
          ('LEFTPADDING', (0, 0), (-1, -1), 5), ('RIGHTPADDING', (0, 0), (-1, -1), 5),
          ('TOPPADDING', (0, 0), (-1, -1), 3.6), ('BOTTOMPADDING', (0, 0), (-1, -1), 3.6),
          ('INNERGRID', (0, 0), (-1, -1), .4, LINE), ('BOX', (0, 0), (-1, -1), .9, hdr)]
    for r in range(1, len(rows)):
        if r % 2 == 0:
            ts.append(('BACKGROUND', (0, r), (-1, r), H('#F6F8FB')))
    if kind in ('compare', 'classify'):
        ts.append(('BACKGROUND', (nc - 1, 1), (nc - 1, -1), light))
    tbl = Table(data, colWidths=widths, repeatRows=1)
    tbl.setStyle(TableStyle(ts))
    tbl.spaceAfter = 6
    label = {'compare': 'جدول مقارنة', 'classify': 'جدول تصنيف', 'values': 'جدول قيم'}.get(kind, 'جدول')
    tdeco = dict(kind='box', bg=light, bd=hdr, label=label)
    title = RichPara(t['title'] or label, PS(9.4, 13, bold=True, color=dark, boldc=None, sa=2), deco=tdeco)
    approx = sum(p.height if p.height else 14 for p in [title]) + 16 * len(rows)
    if approx < 260:
        return [CondPageBreak(60), KeepTogether([title, tbl])]
    return [CondPageBreak(90), title, tbl]


def list_box(items, title, W, acc, numbered=False, head=None):
    dark, light, mid = [H(x) for x in acc]
    head = head or dark
    rows = [[RichPara(title, PS(9.8, 13.6, color=white, bold=True, la=white, num=white, boldc=None))]]
    for i, it in enumerate(items):
        deco = dict(kind='num' if numbered else 'bullet', bd=dark, mk='tick', n=i + 1)
        rows.append([RichPara(it, PS(9.2, 13.6, justify=True), deco=deco)])
    tbl = Table(rows, colWidths=[W])
    tbl.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (0, 0), head), ('BACKGROUND', (0, 1), (0, -1), light),
        ('BOX', (0, 0), (-1, -1), .9, head), ('LEFTPADDING', (0, 0), (-1, -1), 9), ('RIGHTPADDING', (0, 0), (-1, -1), 9),
        ('TOPPADDING', (0, 0), (-1, -1), 2.2), ('BOTTOMPADDING', (0, 0), (-1, -1), 2.2),
        ('TOPPADDING', (0, 0), (0, 0), 5), ('BOTTOMPADDING', (0, 0), (0, 0), 4.5),
        ('LINEAFTER', (-1, 0), (-1, -1), 4, head)]))
    tbl.spaceBefore, tbl.spaceAfter = 3, 9
    return tbl


# ════════════════════════════════════════════════════════════════════
#  8) الـ Parser
# ════════════════════════════════════════════════════════════════════
TEXT_MARKERS = {'P', 'BUL', 'NUM', 'H2', 'DEF', 'KEY', 'EMPH', 'WARN', 'CLIN', 'NOTE', 'MNEMO', 'FLOW'}


def kv(fields):
    d = {}
    for f in fields:
        if '=' in f:
            k, v = f.split('=', 1)
            d[k.strip().lower()] = v.strip()
    return d


class Notes:
    def __init__(self):
        self.meta, self.topics, self.hy, self.audio, self.final, self.plan = {}, [], [], [], {}, []


def parse(text):
    N, cur, mode, last, table, raw = Notes(), None, None, None, None, []
    for line in text.replace('\r', '').split('\n'):
        line = line.strip().lstrip('\ufeff')
        if not line or line.startswith('```') or line.startswith('# '):
            continue
        m = re.match(r'^@@([A-Z0-9_]+)\s*(.*)$', line)
        if not m:
            if mode == 'summary' and cur is not None:
                cur['summary'].append(re.sub(r'^[-•*\u2022]\s*', '', line))
            elif last is not None and last['f']:
                last['f'][-1] = (last['f'][-1] + ' ' + line).strip()
            continue
        name, rest = m.group(1), m.group(2).strip()
        if rest.startswith('|'):
            rest = rest[1:]
        fields = [x.strip() for x in rest.split('|')] if rest else []
        if name == 'LECTURE':
            N.meta.update(kv(fields)); last = None
        elif name == 'TOPIC_PLAN':
            N.plan.append(kv(fields)); last = None
        elif name in ('PLAN_START', 'PLAN_END', 'DICT', 'STRUCT', 'CHECKPOINT'):
            last = None
        elif name == 'FINAL':
            N.final = kv(fields); last = None
        elif name == 'TOPIC_START':
            if cur is not None:
                raw.append(cur)
            k = kv(fields)
            try:
                tid = int(k.get('id', '0'))
            except ValueError:
                tid = 0
            cur = dict(id=tid, title=k.get('title', ''), en=k.get('en', ''), srt=k.get('srt', ''),
                       time=k.get('time', ''), blocks=[], summary=[], done=False)
            mode, last = None, None
        elif name == 'TOPIC_END':
            if cur is not None:
                cur['done'] = any(x.upper() == 'DONE' for x in fields)
                raw.append(cur); cur = None
            mode, last = None, None
        elif name == 'SUMMARY':
            mode, last = 'summary', None
        elif name == 'HIGHYIELD_START':
            mode, last = 'hy', None
        elif name == 'HIGHYIELD_END':
            mode, last = None, None
        elif name == 'HY':
            last = {'f': [' | '.join(fields)]}; N.hy.append(last)
        elif name == 'AUDIO':
            last = {'f': fields}; N.audio.append(last)
        elif name == 'TABLE_START':
            table = dict(type='TABLE', title=fields[0] if fields else '',
                         kind=kv(fields[1:]).get('type', 'compare'), rows=[])
            mode, last = None, None
        elif name == 'TR':
            if table is not None:
                table['rows'].append(fields); last = {'f': table['rows'][-1]} if fields else None
        elif name == 'TABLE_END':
            if table is not None and cur is not None:
                cur['blocks'].append(table)
            table, last = None, None
        elif name in TEXT_MARKERS:
            item = {'type': name, 'f': fields}
            if cur is not None:
                cur['blocks'].append(item)
            mode, last = None, item
    if cur is not None:
        raw.append(cur)
    byid = {}
    for t in raw:
        prev = byid.get(t['id'])
        if prev is None or t['done'] or not prev['done']:
            byid[t['id']] = t
    N.topics = [byid[k] for k in sorted(byid)]
    return N


def srt_total(path):
    t = open(path, encoding='utf-8-sig').read().replace('\r', '')
    idx = [int(m.group(1)) for m in re.finditer(r'(?m)^\s*(\d+)\s*\n\s*\d\d:\d\d:\d\d[,.]\d{3}\s*-->', t)]
    return max(idx) if idx else 0


def validate(N, total=0):
    warns = []
    ids = [t['id'] for t in N.topics]
    if ids:
        miss = [i for i in range(1, max(ids) + 1) if i not in ids]
        if miss:
            warns.append('مواضيع ناقصة في الترقيم: %s' % miss)
    for t in N.topics:
        if not t['done']:
            warns.append('الموضوع %s ما اتقفلش بـ DONE (ناقص/مقطوع)' % t['id'])
        if not t['blocks']:
            warns.append('الموضوع %s فاضي' % t['id'])
    rng = []
    for t in N.topics:
        m = re.match(r'(\d+)\s*[-–—]\s*(\d+)', t['srt'])
        if m:
            rng.append((int(m.group(1)), int(m.group(2)), t['id']))
    rng.sort()
    prev = 0
    for a, b, i in rng:
        if a > prev + 1:
            warns.append('فجوة في تغطية الـ SRT: %d-%d (قبل الموضوع %s)' % (prev + 1, a - 1, i))
        elif a <= prev:
            warns.append('تداخل في الـ SRT عند الموضوع %s' % i)
        prev = max(prev, b)
    if total and prev < total:
        warns.append('الـ SRT فيه %d سطر، والمكتوب وصل لـ %d' % (total, prev))
    return warns


# ════════════════════════════════════════════════════════════════════
#  9) تجميع الـ story
# ════════════════════════════════════════════════════════════════════
def accent_of(tid):
    return ACCENTS[(tid - 1) % len(ACCENTS)] if tid > 0 else ACCENTS[0]


def topic_story(t, W):
    acc = accent_of(t['id'])
    dark, light, mid = [H(x) for x in acc]
    s = [CondPageBreak(125), TopicBanner(t, acc), Spacer(1, 6)]
    blocks, n, numc = t['blocks'], len(t['blocks']), 0
    for i, b in enumerate(blocks):
        typ = b.get('type')
        nxt = blocks[i + 1].get('type') if i + 1 < n else None
        if typ == 'TABLE':
            s.extend(build_table(b, W, acc)); numc = 0
            continue
        f = b['f']
        txt = ' | '.join(f)
        if not txt.strip():
            continue
        if typ != 'NUM':
            numc = 0
        if typ == 'H2':
            s.append(CondPageBreak(62))
            s.append(RichPara(txt, PS(11.4, 16, color=dark, bold=True, boldc=None, sb=5, sa=3),
                              deco=dict(kind='h2', bd=dark, line=mid)))
        elif typ == 'P':
            s.append(RichPara(txt, PS(sa=4.5, justify=True)))
        elif typ == 'BUL':
            s.append(RichPara(txt, PS(sa=2 if nxt == 'BUL' else 5, justify=True), deco=dict(kind='bullet', bd=dark)))
        elif typ == 'NUM':
            numc += 1
            s.append(RichPara(txt, PS(sa=2.4 if nxt == 'NUM' else 5, justify=True),
                              deco=dict(kind='num', bd=dark, n=numc)))
        elif typ == 'DEF':
            term = f[0]
            body = ' | '.join(f[1:])
            s.append(RichPara('**%s**%s' % (term, (': ' + body) if body else ''),
                              PS(9.5, 14, sa=5.5, justify=True), deco=box_deco('DEF')))
        elif typ in ('KEY', 'EMPH', 'WARN', 'CLIN', 'NOTE'):
            s.append(RichPara(txt, PS(9.5, 14, sa=5.5, justify=True), deco=box_deco(typ)))
        elif typ == 'MNEMO':
            s.append(MnemoBox(f[0], ' | '.join(f[1:])))
        elif typ == 'FLOW':
            steps = [x.strip() for x in re.split(r'→|->|⟶|⟵|←', txt) if x.strip()]
            s.append(FlowChart(steps, acc) if len(steps) > 1 else RichPara(txt, PS(sa=4.5, justify=True)))
    if t['summary']:
        s.append(list_box(t['summary'], 'ملخص الموضوع  ·  Topic summary', W, acc))
    else:
        s.append(Spacer(1, 6))
    return s


def audio_story(N, W):
    if not N.audio:
        return []
    grey, light = H('#475569'), H('#F1F4F8')
    hdr = PS(8.8, 12, color=white, bold=True, la=white, num=white, boldc=None, align='C')
    rows = [[RichPara('الوقت', hdr), RichPara('الجملة / المشكلة (اتحاولت من السياق ومتأكدش منها)', hdr)]]
    for a in N.audio:
        f = a['f'] + ['', '']
        rows.append([RichPara(f[0], PS(8.4, 12, align='C')), RichPara(' | '.join(f[1:]).strip(' |'), PS(8.4, 12))])
    tbl = Table([[r[1], r[0]] for r in rows], colWidths=[W - 62, 62], repeatRows=1)
    ts = [('BACKGROUND', (0, 0), (-1, 0), grey), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
          ('INNERGRID', (0, 0), (-1, -1), .4, LINE), ('BOX', (0, 0), (-1, -1), .8, grey),
          ('TOPPADDING', (0, 0), (-1, -1), 2.6), ('BOTTOMPADDING', (0, 0), (-1, -1), 2.6)]
    for r in range(2, len(rows), 2):
        ts.append(('BACKGROUND', (0, r), (-1, r), light))
    tbl.setStyle(TableStyle(ts))
    title = RichPara('نقاط تتراجع من الصوت  ·  Review against audio', PS(10.4, 14.5, bold=True, color=grey, boldc=None, sb=4, sa=3),
                     deco=dict(kind='h2', bd=grey, line=LINE))
    return [CondPageBreak(110), title, tbl]


def full_story(N, W, pages, links):
    s = []
    acc0 = accent_of(1)
    s.append(TitleBlock(N.meta, acc0))
    s.append(Spacer(1, 7))
    entries = [(t['id'], t['title'], pages.get(t['id'], '·'), accent_of(t['id'])) for t in N.topics]
    if entries:
        s.append(TocBlock(entries, acc0, links))
        s.append(Spacer(1, 9))
    for t in N.topics:
        s.extend(topic_story(t, W))
    if N.hy:
        s.append(CondPageBreak(150))
        gold = ('#7A4B00', '#FFF6DA', '#E4C94A')
        s.append(list_box([h['f'][0] for h in N.hy], 'High-Yield Review  ·  أهم النقاط للمراجعة السريعة', W, gold,
                          numbered=True, head=H('#7A4B00')))
    s.extend(audio_story(N, W))
    return s


# ════════════════════════════════════════════════════════════════════
#  10) الصفحة: header / footer / "Page X of Y"
# ════════════════════════════════════════════════════════════════════
class Ctx:
    def __init__(self, meta):
        self.meta, self.topic_pages, self.page_topic, self.cur = meta, {}, {}, None


class NotesDoc(BaseDocTemplate):
    def __init__(self, path, ctx, **kw):
        BaseDocTemplate.__init__(self, path, **kw)
        self.ctx = ctx

    def afterFlowable(self, f):
        if getattr(f, 'is_banner', False):
            self.ctx.topic_pages[f.tid] = self.page
            self.ctx.cur = (f.tid, f.title, f.accent)
        self.ctx.page_topic.setdefault(self.page, self.ctx.cur)


def make_canvas_class(ctx):
    class NumberedCanvas(rl_canvas.Canvas):
        def __init__(self, *a, **k):
            rl_canvas.Canvas.__init__(self, *a, **k)
            self._saved = []

        def showPage(self):
            self._saved.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._saved)
            for state in self._saved:
                self.__dict__.update(state)
                chrome(self, self._pageNumber, total, ctx)
                rl_canvas.Canvas.showPage(self)
            rl_canvas.Canvas.save(self)
    return NumberedCanvas


def chrome(c, pn, total, ctx):
    PW, PH = A4
    ML = 14 * mm
    m = ctx.meta
    topic = ctx.page_topic.get(pn)
    acc = topic[2] if topic else ACCENTS[0]
    dark, mid = H(acc[0]), H(acc[2])
    c.saveState()
    c.setFillColor(dark); c.rect(0, PH - 3.4 * mm, PW, 3.4 * mm, stroke=0, fill=1)
    c.setFillColor(mid); c.rect(0, PH - 3.4 * mm, PW * .18, 1 * mm, stroke=0, fill=1)
    small = PS(8, color=MUTED, boldc=None, la=MUTED, num=MUTED)
    if pn > 1:
        yh = PH - 9.6 * mm
        left = PS(8.4, color=dark, bold=True, boldc=None, la=dark, num=dark)
        if topic:
            c.setFillColor(dark); c.circle(ML + 3, yh + 2.6, 2.6, stroke=0, fill=1)
            draw_text(c, topic[1], ML + 10, yh, left, 'L', maxw=PW * .5)
        ttl = 'LECTURE %s  ·  %s' % (m.get('number', ''), m.get('title', '')) if m.get('number') else m.get('title', '')
        draw_text(c, ttl, PW - ML, yh, small, 'R', maxw=PW * .42)
        c.setStrokeColor(dark); c.setLineWidth(.7); c.line(ML, yh - 5, PW - ML, yh - 5)
    yf = 8.6 * mm
    c.setStrokeColor(dark); c.setLineWidth(.7); c.line(ML, yf + 12, PW - ML, yf + 12)
    c.setStrokeColor(mid); c.setLineWidth(.7); c.line(ML, yf + 10.5, PW - ML, yf + 10.5)
    pill = 'Page %d of %d' % (pn, total)
    pst = PS(8.4, color=white, bold=True, boldc=None, la=white, num=white)
    pw = text_width(pill, pst) + 20
    c.setFillColor(dark); c.roundRect(PW / 2 - pw / 2, yf - 3.4, pw, 14.6, 7, stroke=0, fill=1)
    draw_text(c, pill, PW / 2, yf + .8, pst, 'C')
    right_txt = m.get('title', '')
    if m.get('number'):
        right_txt = 'Lecture %s  ·  %s' % (m['number'], right_txt)
    draw_text(c, right_txt, PW - ML, yf + .8, small, 'R', maxw=PW * .34)
    left_txt = '  ·  '.join(x for x in (m.get('doctor', ''), m.get('course', '')) if x) or 'Study notes'
    draw_text(c, left_txt, ML, yf + .8, small, 'L', maxw=PW * .30)
    c.restoreState()


def build_pdf(text, out, srt=None, fonts=None):
    setup_fonts(fonts)
    N = parse(text)
    if not N.topics:
        sys.exit('✗ مفيش مواضيع (@@TOPIC_START) في الملف.')
    for w in validate(N, srt_total(srt) if srt else 0):
        print('⚠', w)
    ML, MR, MT, MB = 14 * mm, 14 * mm, 17 * mm, 17.5 * mm
    PW, PH = A4
    W = PW - ML - MR

    def run(path, pages, links):
        ctx = Ctx(N.meta)
        doc = NotesDoc(path, ctx, pagesize=A4, leftMargin=ML, rightMargin=MR, topMargin=MT, bottomMargin=MB,
                       title='%s - Lecture %s' % (N.meta.get('title', ''), N.meta.get('number', '')),
                       author=N.meta.get('doctor', '') or 'Study notes')
        doc.addPageTemplates([PageTemplate(id='main', frames=[Frame(ML, MB, W, PH - MT - MB, 0, 0, 0, 0, id='f')])])
        doc.build(full_story(N, W, pages, links), canvasmaker=make_canvas_class(ctx))
        return ctx

    tmp = out + '.pass1.pdf'
    ctx1 = run(tmp, {}, False)
    ctx2 = run(out, ctx1.topic_pages, True)
    try:
        os.remove(tmp)
    except OSError:
        pass
    total = max(ctx2.page_topic) if ctx2.page_topic else 0
    print('✓ %s  |  %d مواضيع  |  %d صفحة  |  %d نقطة للمراجعة من الصوت' % (out, len(N.topics), total, len(N.audio)))


def main():
    ap = argparse.ArgumentParser(description='@@markers → PDF')
    ap.add_argument('inputs', nargs='+')
    ap.add_argument('-o', '--out', default='lecture_notes.pdf')
    ap.add_argument('--srt', help='ملف الـ SRT الأصلي للتحقق من التغطية (اختياري)')
    ap.add_argument('--fonts', help='فولدر فيه خطوط TTF (اختياري)')
    a = ap.parse_args()
    text = '\n'.join(open(p, encoding='utf-8-sig').read() for p in a.inputs)
    build_pdf(text, a.out, a.srt, a.fonts)


if __name__ == '__main__':
    main()
