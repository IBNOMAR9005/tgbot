# -*- coding: utf-8 -*-
"""الملزمة: Gemini بالبرومبت بتاعك (+ بروتوكول الاستكمال) ثم lecture_to_pdf.py."""
import asyncio, contextlib, importlib.util, io, os, re, sys

_DONE_RE = re.compile(r'^@@TOPIC_END\s*\|.*\bDONE\b', re.I)


def load_pdf_module(here):
    spec = importlib.util.spec_from_file_location('lecture_to_pdf', os.path.join(here, 'lecture_to_pdf.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _strip_preamble(text):
    """يشيل أي كلام قبل أول @@ (زي 'هكمل من الموضوع..')."""
    lines = text.replace('\r', '').split('\n')
    for i, ln in enumerate(lines):
        if ln.strip().startswith('@@'):
            return '\n'.join(lines[i:])
    return ''


def trim_to_complete(text):
    """يقص الناتج عند آخر موضوع مقفول بـ DONE (+ سطر CHECKPOINT اللي بعده)، ويتجاهل الموضوع الناقص."""
    lines = text.replace('\r', '').split('\n')
    last = -1
    for i, ln in enumerate(lines):
        if _DONE_RE.match(ln.strip()):
            last = i
    if last >= 0:
        end = last
        for j in range(last + 1, min(last + 4, len(lines))):
            s = lines[j].strip()
            if s.startswith('@@CHECKPOINT'):
                end = j
                break
            if s:
                break
        return '\n'.join(lines[:end + 1]).rstrip() + '\n'
    for i, ln in enumerate(lines):
        if ln.strip().startswith('@@PLAN_END'):
            return '\n'.join(lines[:i + 1]).rstrip() + '\n'
    return ''


def progress_of(text):
    """(آخر id موضوع مكتمل، آخر سطر SRT اتغطى، توقيته)"""
    tid, last_srt, tm = 0, 0, ''
    for m in re.finditer(r'^@@TOPIC_START\s*\|(.*)$', text, re.M):
        pass
    cur = None
    for ln in text.split('\n'):
        s = ln.strip()
        if s.startswith('@@TOPIC_START'):
            cur = s
        elif _DONE_RE.match(s) and cur:
            m = re.search(r'id=(\d+)', cur)
            if m:
                tid = max(tid, int(m.group(1)))
            m = re.search(r'srt=(\d+)\s*[-–—]\s*(\d+)', cur)
            if m:
                last_srt = max(last_srt, int(m.group(2)))
            m = re.search(r'time=[\d:]+\s*[-–—]\s*([\d:]+)', cur)
            if m:
                tm = m.group(1)
    return tid, last_srt, tm


def is_finished(text):
    return '@@FINAL' in text


def count_done(text):
    return sum(1 for ln in text.split('\n') if _DONE_RE.match(ln.strip()))


def first_user_message(meta_text, srt_text):
    return ('إجابات المرحلة 0 (من المستخدم):\n%s\n\n'
            'ملف الـ SRT الكامل للمحاضرة:\n=====SRT_START=====\n%s\n=====SRT_END=====\n\n'
            'ابدأ المرحلة 1 فوراً ثم المرحلة 2 ثم المرحلة 3 حسب التعليمات، بدون أسئلة وبدون أي كلام خارج الـ markers.'
            % (meta_text.strip() or 'غير محدد', srt_text))


def continuation_message(tid, last_srt, tm):
    return ('كمّل حسب بروتوكول الاستكمال. آخر موضوع مكتمل هو %d (آخر سطر SRT مغطى: %s%s). '
            'ابدأ من أول الموضوع %d من غير ما تعيد الخطة أو أي موضوع اتكتب، وطبّق نفس القاموس والأسلوب. '
            'اكتب بس الـ markers. لو خلصت كل الـ SRT اكتب المرحلة 3 (@@HIGHYIELD_START ... @@HIGHYIELD_END ثم @@FINAL).'
            % (tid, last_srt or '؟', (' · ' + tm) if tm else '', tid + 1))


async def generate_notes(session, pool, system, srt_text, meta_text, status=None, max_calls=16):
    """يرجّع (نص الـ markers الكامل، عدد الاستدعاءات)."""
    u1 = first_user_message(meta_text, srt_text)
    acc, calls, stalls = '', 0, 0

    async def on_wait(sec):
        if status:
            await status('⏳ كوتة Gemini مشغولة، هستنى حوالي %d دقيقة…' % (sec // 60 + 1))

    while calls < max_calls:
        calls += 1
        contents = [('user', u1)]
        if acc:
            tid, ls, tm = progress_of(acc)
            contents += [('model', acc), ('user', continuation_message(tid, ls, tm))]
        text, fin, model = await pool.generate(session, system, contents, temperature=0.3, on_wait=on_wait)
        before = count_done(acc)
        if not acc:
            new = text
        else:
            new = _strip_preamble(text)
        merged = (acc + '\n' + new) if acc else new
        finished = is_finished(merged)
        # لو لسه ما خلصش: قص عند آخر موضوع كامل
        acc = merged if finished else (trim_to_complete(merged) or merged)
        done_now = count_done(acc)
        if status:
            tid, ls, tm = progress_of(acc)
            await status('✍️ بكتب الملزمة… (%d موضوع%s)' % (done_now, (' · وصلت لـ ' + tm) if tm else ''))
        if finished:
            break
        if done_now <= before:
            stalls += 1
            if stalls >= 2:
                break
        else:
            stalls = 0
    return acc, calls


def build_pdf_sync(here, text, out_pdf, srt_path, fonts_dir):
    """يشغّل lecture_to_pdf (blocking) ويرجّع (warnings, meta, n_topics, pages_info)."""
    mod = load_pdf_module(here)
    buf = io.StringIO()
    meta, n_topics = {}, 0
    with contextlib.redirect_stdout(buf):
        try:
            N = mod.parse(text)
            meta, n_topics = dict(N.meta), len(N.topics)
            mod.build_pdf(text, out_pdf, srt_path, fonts_dir)
        except SystemExit as e:
            raise RuntimeError(str(e.code) if e.code else 'lecture_to_pdf أنهى التنفيذ')
    out = buf.getvalue()
    warns = [ln[1:].strip() for ln in out.splitlines() if ln.startswith('⚠')]
    return warns, meta, n_topics, out
