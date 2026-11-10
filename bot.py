# -*- coding: utf-8 -*-
"""بوت تفريغ المحاضرات: Telegram (Local Bot API) + Groq Whisper + Gemini."""
import asyncio, html, logging, os, re, shutil, sys, time, traceback, uuid
from dataclasses import dataclass, field

from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter, TelegramNetworkError
from aiogram.filters import Command
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
                           Message, ReplyKeyboardMarkup, ReplyParameters, BotCommand)

import media, pipeline, subs
from config import Config
from groq_stt import GroqError
from gemini_client import GeminiError
from store import Store

log = logging.getLogger('bot')

LANGS = {'ar': '🇪🇬 عربي', 'en': '🇬🇧 English', 'auto': '🌐 Auto'}
MORE_LANGS = {'fr': '🇫🇷 Français', 'de': '🇩🇪 Deutsch', 'es': '🇪🇸 Español', 'it': '🇮🇹 Italiano',
              'tr': '🇹🇷 Türkçe', 'ru': '🇷🇺 Русский', 'ur': '🇵🇰 اردو', 'fa': '🇮🇷 فارسی'}
FMTS = {'srt': 'SRT', 'txt': 'TXT', 'both': 'SRT + TXT'}
AUDIO_EXT = {'.mp3', '.m4a', '.aac', '.wav', '.flac', '.ogg', '.oga', '.opus', '.wma', '.amr', '.aiff', '.aif', '.ac3',
             '.mka', '.weba', '.mp4', '.m4v', '.mov', '.mkv', '.avi', '.webm', '.flv', '.wmv', '.3gp', '.3g2', '.ts',
             '.mts', '.m2ts', '.mpg', '.mpeg', '.vob', '.ogv', '.asf', '.rm', '.rmvb', '.caf', '.m4b', '.mp2', '.mpga'}

BTN_STATUS, BTN_QUOTA, BTN_QUEUE, BTN_STOP = '📊 الحالة', '🔑 الكوتة', '📋 الطابور', '⛔ إيقاف الحالي'


@dataclass
class Job:
    id: str
    uid: int
    chat_id: int
    src_msg_id: int
    file_id: str
    uniq: str
    name: str
    size: int
    dur_hint: int
    lang: str = 'ar'
    fmt: str = 'srt'
    topics: bool = True
    pdf: bool = False
    status: str = 'choosing'     # choosing|queued|running|done|cancelled|error
    menu_id: int = 0
    meta_text: str = ''
    meta_event: asyncio.Event = field(default_factory=asyncio.Event)
    file_task: asyncio.Task = None
    task: asyncio.Task = None
    post_tasks: list = field(default_factory=list)
    src_path: str = ''
    created: float = field(default_factory=time.time)


class Allow(BaseMiddleware):
    def __init__(self, ids):
        self.ids = set(ids)

    async def __call__(self, handler, event, data):
        u = data.get('event_from_user')
        if u and u.id in self.ids:
            return await handler(event, data)
        if u:
            log.info('تجاهلت مستخدم غير مسموح: %s', u.id)
        return None


def esc(s):
    return html.escape(str(s), quote=False)


def hsize(n):
    n = float(n or 0)
    for u in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or u == 'GB':
            return ('%.0f %s' if u in ('B', 'KB') else '%.1f %s') % (n, u)
        n /= 1024


def btn(text, data, style=None):
    kw = dict(text=text, callback_data=data)
    if style:
        kw['style'] = style      # primary / success / danger  (Bot API 9.4+)
    return InlineKeyboardButton(**kw)


def main_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BTN_STATUS), KeyboardButton(text=BTN_QUOTA)],
                                         [KeyboardButton(text=BTN_QUEUE), KeyboardButton(text=BTN_STOP)]],
                               resize_keyboard=True, is_persistent=True)


class Progress:
    """رسالة تقدّم واحدة بتتعدّل (مع throttle) وفيها زرار إيقاف."""

    def __init__(self, bot, chat_id, msg_id, job_id):
        self.bot, self.chat_id, self.msg_id, self.job_id = bot, chat_id, msg_id, job_id
        self.last, self.last_t, self.stop_btn = '', 0.0, True

    def kb(self):
        if not self.stop_btn:
            return None
        return InlineKeyboardMarkup(inline_keyboard=[[btn('⛔ إيقاف', 'j:%s:x' % self.job_id, 'danger')]])

    async def update(self, text, force=False):
        if text == self.last or (not force and time.time() - self.last_t < 3.0):
            return
        self.last, self.last_t = text, time.time()
        try:
            await self.bot.edit_message_text(text, chat_id=self.chat_id, message_id=self.msg_id, reply_markup=self.kb())
        except TelegramRetryAfter as e:
            self.last_t = time.time() + e.retry_after
        except TelegramBadRequest:
            pass
        except TelegramNetworkError:
            pass


class BotApp:
    def __init__(self, cfg):
        self.cfg = cfg
        os.makedirs(cfg.data_dir, exist_ok=True)
        os.makedirs(cfg.work_dir, exist_ok=True)
        self.store = Store(os.path.join(cfg.data_dir, 'state.json'))
        self.core = pipeline.App(cfg, self.store, log.info)
        api = TelegramAPIServer.from_base(cfg.local_api, is_local=True)
        session = AiohttpSession(api=api, timeout=7200)
        self.bot = Bot(cfg.bot_token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        self.dp = Dispatcher()
        self.jobs = {}
        self.queue = asyncio.Queue()
        self.current = None
        self.awaiting_meta = {}
        self.started = time.time()
        self._register()

    # ───────────────────────── واجهة ─────────────────────────
    def menu_text(self, j):
        lang = {**LANGS, **MORE_LANGS}.get(j.lang, j.lang)
        t = ['📥 <b>%s</b>' % esc(j.name)]
        meta = []
        if j.dur_hint:
            meta.append('⏱ ' + subs.human_dur(j.dur_hint))
        if j.size:
            meta.append('📦 ' + hsize(j.size))
        if meta:
            t.append('  ·  '.join(meta))
        t.append('')
        t.append('🌍 اللغة: <b>%s</b>   📝 الصيغة: <b>%s</b>   🕒 مواضيع: <b>%s</b>' % (
            lang, FMTS[j.fmt], 'شغالة' if j.topics else 'مقفولة'))
        if j.dur_hint:
            keys, cap, wait = self.core.groq.plan_keys(j.dur_hint, self.cfg.max_keys_per_job)
            if cap < j.dur_hint:
                t.append('⚠️ كوتة Groq المتاحة دلوقتي (%s) أقل من مدة الملف؛ هستنى تجديدها لو لزم.' % subs.human_dur(cap))
        t.append('')
        t.append('عدّل الإعدادات (اختياري) وبعدين دوس الزرار الأساسي 👇')
        return '\n'.join(t)

    def menu_kb(self, j, sub=None):
        i = j.id
        if sub == 'more':
            rows, row = [], []
            for code, label in MORE_LANGS.items():
                row.append(btn(('✅ ' if j.lang == code else '') + label, 'j:%s:lang:%s' % (i, code)))
                if len(row) == 2:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)
            rows.append([btn('⬅️ رجوع', 'j:%s:menu' % i)])
            return InlineKeyboardMarkup(inline_keyboard=rows)
        other = j.lang not in LANGS
        rows = [
            [btn(('✅ ' if j.lang == c else '') + l, 'j:%s:lang:%s' % (i, c)) for c, l in LANGS.items()]
            + [btn(('✅ ' + MORE_LANGS[j.lang]) if other else '🌍 غيرها', 'j:%s:more' % i)],
            [btn(('✅ ' if j.fmt == c else '') + l, 'j:%s:fmt:%s' % (i, c)) for c, l in FMTS.items()],
            [btn(('✅' if j.topics else '⬜') + ' 🕒 المواضيع والتوقيتات في رسالة', 'j:%s:tp' % i)],
            [btn('✅ ابدأ: تفريغ + مواضيع وتوقيتات', 'j:%s:go' % i, 'success')],
            [btn('📄 ابدأ + ملزمة PDF كاملة', 'j:%s:gopdf' % i, 'primary')],
            [btn('❌ إلغاء', 'j:%s:x' % i, 'danger')],
        ]
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def show_menu(self, j, sub=None):
        try:
            await self.bot.edit_message_text(self.menu_text(j), chat_id=j.chat_id, message_id=j.menu_id,
                                             reply_markup=self.menu_kb(j, sub))
        except TelegramBadRequest:
            pass

    async def say(self, j_or_chat, text, **kw):
        chat = j_or_chat.chat_id if isinstance(j_or_chat, Job) else j_or_chat
        for _ in range(3):
            try:
                return await self.bot.send_message(chat, text, **kw)
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
        return None

    def reply_to(self, j):
        return ReplyParameters(message_id=j.src_msg_id, allow_sending_without_reply=True)

    async def send_doc(self, j, path, filename, caption=''):
        for _ in range(3):
            try:
                return await self.bot.send_document(j.chat_id, FSInputFile(path, filename=filename), caption=caption,
                                                    reply_parameters=self.reply_to(j), request_timeout=1800)
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)

    # ───────────────────────── المعالجات ─────────────────────────
    def _register(self):
        dp = self.dp
        dp.update.outer_middleware(Allow(self.cfg.allowed_ids))
        dp.message(Command('start', 'help'))(self.h_start)
        dp.message(Command('status'))(self.h_status)
        dp.message(Command('quota'))(self.h_quota)
        dp.message(Command('queue'))(self.h_queue)
        dp.message(Command('stop', 'cancel'))(self.h_stop)
        dp.message(Command('skip'))(self.h_skip_meta_cmd)
        dp.message(F.audio | F.voice | F.video | F.video_note | F.document)(self.h_media)
        dp.message(F.text == BTN_STATUS)(self.h_status)
        dp.message(F.text == BTN_QUOTA)(self.h_quota)
        dp.message(F.text == BTN_QUEUE)(self.h_queue)
        dp.message(F.text == BTN_STOP)(self.h_stop)
        dp.message(F.text)(self.h_text)
        dp.callback_query(F.data.startswith('j:'))(self.h_cb)

    async def h_start(self, m: Message):
        await m.answer(
            '👋 أهلاً! ابعتلي أي <b>صوت أو فيديو أو ريكورد</b> (أي حجم لحد 2GB) وهطلعلك:\n'
            '📝 SRT (أو TXT)  ·  🕒 المواضيع والتوقيتات المهمة  ·  📄 ملزمة PDF (اختياري)\n\n'
            'اللغة الافتراضية عربي، وتقدر تغيّرها من الأزرار بعد ما تبعت الملف.',
            reply_markup=main_kb())

    def _extract(self, m: Message):
        if m.audio:
            o = m.audio
            return o, (o.file_name or 'audio_%s' % (o.title or o.file_unique_id)), o.duration
        if m.voice:
            return m.voice, 'voice_%s' % time.strftime('%Y%m%d_%H%M%S'), m.voice.duration
        if m.video:
            return m.video, (m.video.file_name or 'video_%s' % m.video.file_unique_id), m.video.duration
        if m.video_note:
            return m.video_note, 'videonote_%s' % time.strftime('%Y%m%d_%H%M%S'), m.video_note.duration
        if m.document:
            d = m.document
            ext = os.path.splitext(d.file_name or '')[1].lower()
            mime = (d.mime_type or '').lower()
            if mime.startswith(('audio/', 'video/')) or ext in AUDIO_EXT or mime in ('application/ogg', 'application/octet-stream') and ext in AUDIO_EXT:
                return d, (d.file_name or 'file_%s' % d.file_unique_id), 0
        return None, None, 0

    async def h_media(self, m: Message):
        obj, name, dur = self._extract(m)
        if obj is None:
            await m.reply('الملف ده مش صوت ولا فيديو، ابعتلي ريكورد أو فيديو 🎧')
            return
        p = self.store.prefs(m.from_user.id)
        j = Job(id=uuid.uuid4().hex[:8], uid=m.from_user.id, chat_id=m.chat.id, src_msg_id=m.message_id,
                file_id=obj.file_id, uniq=obj.file_unique_id, name=name, size=getattr(obj, 'file_size', 0) or 0,
                dur_hint=dur or 0, lang=p['lang'], fmt=p['fmt'], topics=p['topics'])
        self.jobs[j.id] = j
        j.file_task = asyncio.create_task(self.bot.get_file(j.file_id, request_timeout=7200))   # بيحمّله على السيرفر المحلي وإنت بتختار
        msg = await m.reply(self.menu_text(j), reply_markup=self.menu_kb(j))
        j.menu_id = msg.message_id

    async def h_cb(self, cb: CallbackQuery):
        try:
            _, jid, act, *rest = cb.data.split(':')
        except ValueError:
            await cb.answer()
            return
        j = self.jobs.get(jid)
        if not j or j.uid != cb.from_user.id:
            await cb.answer('الطلب ده خلص أو اتلغى.', show_alert=True)
            return
        arg = rest[0] if rest else None
        if act == 'x':
            await self.cancel_job(j)
            await cb.answer('اتلغى')
            return
        if act == 'skipmeta':
            j.meta_text = 'غير محدد (المستخدم تخطّى الأسئلة؛ استنتج كل حاجة من السياق)'
            j.meta_event.set()
            self.awaiting_meta.pop(j.uid, None)
            await cb.answer('تمام، هكمّل من غير بيانات')
            try:
                await cb.message.edit_text('⏭ اتخطّت بيانات المحاضرة.')
            except TelegramBadRequest:
                pass
            return
        if j.status != 'choosing':
            await cb.answer('الطلب شغال بالفعل.')
            return
        p = self.store.prefs(j.uid)
        if act == 'lang':
            j.lang = p['lang'] = arg
            self.store.save()
            await self.show_menu(j)
        elif act == 'more':
            await self.show_menu(j, 'more')
        elif act == 'menu':
            await self.show_menu(j)
        elif act == 'fmt':
            j.fmt = p['fmt'] = arg
            self.store.save()
            await self.show_menu(j)
        elif act == 'tp':
            j.topics = p['topics'] = not j.topics
            self.store.save()
            await self.show_menu(j)
        elif act in ('go', 'gopdf'):
            j.pdf = act == 'gopdf'
            await self.start_job(j)
        await cb.answer()

    async def h_text(self, m: Message):
        jid = self.awaiting_meta.get(m.from_user.id)
        j = self.jobs.get(jid) if jid else None
        if j and not j.meta_event.is_set():
            j.meta_text = m.text.strip()
            j.meta_event.set()
            self.awaiting_meta.pop(m.from_user.id, None)
            await m.reply('✅ استلمت بيانات المحاضرة، هستخدمها في الملزمة.')
            return
        await m.answer('ابعتلي ريكورد أو فيديو 🎧', reply_markup=main_kb())

    async def h_skip_meta_cmd(self, m: Message):
        jid = self.awaiting_meta.pop(m.from_user.id, None)
        j = self.jobs.get(jid) if jid else None
        if j:
            j.meta_text = 'غير محدد (المستخدم تخطّى الأسئلة؛ استنتج كل حاجة من السياق)'
            j.meta_event.set()
            await m.reply('⏭ تمام.')

    # ───────────────────────── الشغل ─────────────────────────
    async def start_job(self, j):
        j.status = 'queued'
        pos = self.queue.qsize() + (1 if self.current else 0)
        prog = Progress(self.bot, j.chat_id, j.menu_id, j.id)
        j.prog = prog
        await prog.update('⏳ في الطابور (قبلك %d)…' % pos if pos else '🚀 بدأت…', force=True)
        if j.pdf:
            q = await self.say(j, '📚 <b>بيانات المحاضرة للملزمة</b> (رسالة واحدة، كل معلومة في سطر):\n'
                                  '1) رقم المحاضرة\n2) عنوان المحاضرة\n3) اسم الدكتور\n4) المادة والدفعة\n'
                                  '5) مصطلحات/أمراض/أدوية متوقعة (بتحسّن التصحيح)\n\n'
                                  'أو دوس تخطّي وهستنتجها من التفريغ.',
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=[[btn('⏭ تخطّي', 'j:%s:skipmeta' % j.id)]]))
            self.awaiting_meta[j.uid] = j.id
        await self.queue.put(j)

    async def cancel_job(self, j):
        was = j.status
        j.status = 'cancelled'
        for t in [j.task, j.file_task] + j.post_tasks:
            if t and not t.done():
                t.cancel()
        j.meta_event.set()
        self.awaiting_meta.pop(j.uid, None)
        try:
            await self.bot.edit_message_text('❌ اتلغى.', chat_id=j.chat_id, message_id=j.menu_id, reply_markup=None)
        except TelegramBadRequest:
            pass
        if was == 'choosing':
            self.jobs.pop(j.id, None)

    async def worker(self):
        while True:
            j = await self.queue.get()
            if j.status == 'cancelled':
                continue
            self.current = j
            j.task = asyncio.create_task(self.run_job(j))
            try:
                await j.task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception('worker')
            self.current = None

    async def run_job(self, j):
        prog = j.prog
        j.status = 'running'
        try:
            await prog.update('📥 بجهّز الملف على السيرفر…', force=True)
            f = await asyncio.wait_for(j.file_task, 7200)
            j.src_path = f.file_path
            if not j.src_path or not os.path.exists(j.src_path):
                raise media.MediaError('السيرفر المحلي ما نزّلش الملف (المسار مش موجود). تأكد إن telegram-bot-api شغال بـ --local.')
            res = await pipeline.transcribe(self.core, j.id, j.uniq, j.src_path, j.lang, prog.update)
            await self.deliver(j, res)
        except asyncio.CancelledError:
            raise
        except (media.MediaError, GroqError, GeminiError) as e:
            j.status = 'error'
            prog.stop_btn = False
            await prog.update('❌ %s' % esc(e), force=True)
        except Exception as e:
            log.exception('job failed')
            j.status = 'error'
            prog.stop_btn = False
            await prog.update('❌ حصل خطأ غير متوقع: %s\n%s' % (type(e).__name__, esc(str(e)[:300])), force=True)
        finally:
            self.cleanup_source(j)

    def cleanup_source(self, j):
        p = j.src_path
        if p and os.path.isfile(p):
            try:
                os.remove(p)
            except OSError:
                pass
        shutil.rmtree(os.path.join(self.cfg.work_dir, j.id), ignore_errors=True)

    async def deliver(self, j, res):
        prog = j.prog
        cues, segs, dur = res['cues'], res['segs'], res['duration']
        base = pipeline.safe_name(os.path.splitext(j.name)[0], 'lecture')
        out = os.path.join(self.cfg.work_dir, j.id, 'out')
        os.makedirs(out, exist_ok=True)
        srt_text = subs.build_srt(cues)
        if j.fmt in ('srt', 'both'):
            p = os.path.join(out, base + '.srt')
            open(p, 'w', encoding='utf-8').write(srt_text)
            await self.send_doc(j, p, base + '.srt', '📝 SRT · ⏱ %s · %d سطر' % (subs.human_dur(dur), len(cues)))
        if j.fmt in ('txt', 'both'):
            p = os.path.join(out, base + '.txt')
            open(p, 'w', encoding='utf-8').write(subs.build_txt(segs))
            await self.send_doc(j, p, base + '.txt', '📄 TXT · ⏱ %s' % subs.human_dur(dur))
        note = '✅ التفريغ خلص (%s)' % subs.human_dur(dur)
        if j.topics or j.pdf:
            note += ' — شغال على %s…' % ' و'.join(x for x, on in (('المواضيع', j.topics), ('الملزمة', j.pdf)) if on)
        prog.stop_btn = False
        await prog.update(note, force=True)
        j.status = 'done'
        if j.topics:
            j.post_tasks.append(asyncio.create_task(self.post_topics(j, cues, dur)))
        if j.pdf:
            j.post_tasks.append(asyncio.create_task(self.post_pdf(j, srt_text, base)))
        # لو مفيش مهام تانية نظّف الملف المؤقت دلوقتي
        self.cleanup_source(j)
        # نحتفظ بالـ out لحد ما المهام تخلص

    async def _post_progress(self, j, text):
        msg = await self.say(j, text, reply_parameters=self.reply_to(j))
        pr = Progress(self.bot, j.chat_id, msg.message_id, j.id) if msg else None
        if pr:
            pr.stop_btn = True
        return pr

    async def post_topics(self, j, cues, dur):
        pr = await self._post_progress(j, '🧠 جاري استخراج المواضيع والتوقيتات…')
        try:
            msgs, (ns, ni) = await pipeline.make_topics(self.core, cues, dur, (pr.update if pr else self._noop))
            for k, text in enumerate(msgs):
                for _ in range(3):
                    try:
                        await self.bot.send_message(j.chat_id, text, reply_parameters=self.reply_to(j) if k == 0 else None,
                                                    link_preview_options={'is_disabled': True})
                        break
                    except TelegramRetryAfter as e:
                        await asyncio.sleep(e.retry_after + 1)
                await asyncio.sleep(0.4)
            if pr:
                await self.bot.delete_message(j.chat_id, pr.msg_id)
        except asyncio.CancelledError:
            raise
        except (GeminiError, media.MediaError) as e:
            if pr:
                pr.stop_btn = False
                await pr.update('⚠️ المواضيع: %s' % esc(e), force=True)
        except Exception as e:
            log.exception('topics failed')
            if pr:
                pr.stop_btn = False
                await pr.update('⚠️ المواضيع فشلت: %s %s' % (type(e).__name__, esc(str(e)[:200])), force=True)

    async def _noop(self, *_a, **_k):
        return None

    async def post_pdf(self, j, srt_text, base):
        pr = await self._post_progress(j, '📚 تجهيز الملزمة…')
        try:
            if not j.meta_event.is_set():
                if pr:
                    await pr.update('📚 مستني بيانات المحاضرة منك (أو تخطّي)…', force=True)
                try:
                    await asyncio.wait_for(j.meta_event.wait(), 900)
                except asyncio.TimeoutError:
                    j.meta_text = 'غير محدد (انتهى وقت الانتظار؛ استنتج كل حاجة من السياق)'
                    self.awaiting_meta.pop(j.uid, None)
            outdir = os.path.join(self.cfg.work_dir, j.id, 'pdf')
            r = await pipeline.make_pdf(self.core, srt_text, j.meta_text, outdir, pr.update if pr else self._noop)
            cap = '📄 الملزمة · %d موضوع' % r['topics']
            if r['warnings']:
                cap += ' · ⚠️ %d تنبيه' % len(r['warnings'])
            await self.send_doc(j, r['pdf'], os.path.basename(r['pdf']), cap)
            await self.send_doc(j, r['md'], os.path.basename(r['md']), '🧾 نص الـ markers (لو حبيت تعدّل وتعيد التحويل)')
            if r['warnings']:
                await self.say(j, '⚠️ <b>تنبيهات الملزمة:</b>\n' + '\n'.join('• ' + esc(w) for w in r['warnings'][:10]))
            if pr:
                await self.bot.delete_message(j.chat_id, pr.msg_id)
        except asyncio.CancelledError:
            raise
        except (GeminiError, RuntimeError, media.MediaError) as e:
            if pr:
                pr.stop_btn = False
                await pr.update('⚠️ الملزمة: %s' % esc(e), force=True)
        except Exception as e:
            log.exception('pdf failed')
            if pr:
                pr.stop_btn = False
                await pr.update('⚠️ الملزمة فشلت: %s %s' % (type(e).__name__, esc(str(e)[:200])), force=True)
        finally:
            shutil.rmtree(os.path.join(self.cfg.work_dir, j.id), ignore_errors=True)

    # ───────────────────────── أوامر الحالة ─────────────────────────
    def quota_text(self):
        g = self.core.groq
        lines = ['🔑 <b>Groq Whisper</b> (الحد/ساعة %s · /يوم %s)' % (subs.human_dur(g.ash), subs.human_dur(g.asd))]
        for l in g.labels:
            h, d = g.used(l)
            tag = ' ☠️' if l in g.dead else (' ⏸' if g.blocked[l] > time.time() else '')
            lines.append('<code>%s</code>  ساعة %s/%s · يوم %s/%s%s' % (
                esc(l), subs.human_dur(h), subs.human_dur(g.ash), subs.human_dur(d), subs.human_dur(g.asd), tag))
        for name, pool in (('المواضيع', self.core.gem_t), ('الملزمة', self.core.gem_n)):
            now = time.time()
            cool = sum(1 for (l, m), t in pool.cool.items() if t > now)
            lines.append('\n🧠 <b>Gemini · %s</b>: %d مفتاح · موديلات: <code>%s</code>%s' % (
                name, len(pool.labels), esc(', '.join(pool.models)), (' · ⏸ %d مؤقتاً' % cool) if cool else ''))
        return '\n'.join(lines)

    async def h_quota(self, m: Message):
        await m.answer(self.quota_text())

    async def h_status(self, m: Message):
        up = time.time() - self.started
        free = shutil.disk_usage(self.cfg.work_dir).free
        cur = self.current
        lines = ['🟢 <b>شغال</b> · من %s' % subs.human_dur(up),
                 '⚙️ حالياً: %s' % (esc(cur.name) if cur else 'مفيش'),
                 '📋 في الطابور: %d' % self.queue.qsize(),
                 '💾 مساحة فاضية: %s' % hsize(free), '', self.quota_text()]
        await m.answer('\n'.join(lines), reply_markup=main_kb())

    async def h_queue(self, m: Message):
        items = list(self.queue._queue)
        lines = []
        if self.current:
            lines.append('▶️ <b>%s</b>' % esc(self.current.name))
        for i, j in enumerate(items, 1):
            lines.append('%d. %s' % (i, esc(j.name)))
        await m.answer('\n'.join(lines) if lines else 'الطابور فاضي ✨')

    async def h_stop(self, m: Message):
        if self.current:
            await self.cancel_job(self.current)
            await m.answer('⛔ وقفت الشغل الحالي.')
        else:
            await m.answer('مفيش حاجة شغالة دلوقتي.')

    # ───────────────────────── التشغيل ─────────────────────────
    async def startup_report(self):
        await self.core.start()
        gk = await self.core.groq.check_keys(self.core.session)
        tk = await self.core.gem_t.check(self.core.session)
        nk = await self.core.gem_n.check(self.core.session) if self.cfg.gemini_keys_pdf else {}
        me = await self.bot.get_me()
        ffv = (await media.run(['ffmpeg', '-version'], 20))[1].split('\n')[0][:40]
        try:
            fonts_ok = await asyncio.to_thread(self._fonts_ok)
        except Exception as e:
            fonts_ok = 'خطأ: %s' % e
        mark = lambda v: '✅' if v else ('⚠️' if v is None else '❌')
        L = ['✅ <b>البوت اشتغل</b> — @%s' % me.username, '',
             '🎙 Groq: %s (%d مفتاح)' % (' '.join(mark(v) for v in gk.values()) or '—', len(gk)),
             '🧠 Gemini مواضيع: %s · %s' % (' '.join(mark(v) for v in tk.values()) or '—', esc(', '.join(self.core.gem_t.models))),
             '📄 Gemini ملزمة: %s · %s' % (' '.join(mark(v) for v in nk.values()) or '(نفس مفاتيح المواضيع)', esc(', '.join(self.core.gem_n.models))),
             '🎞 ffmpeg: %s' % esc(ffv), '🔤 الخطوط: %s' % esc(fonts_ok)]
        miss = getattr(self.core.gem_t, 'missing_models', [])
        if miss:
            L.append('⚠️ موديلات مش متاحة بالمفاتيح: %s' % esc(', '.join(miss)))
        text = '\n'.join(L)
        for uid in self.cfg.allowed_ids:
            try:
                await self.bot.send_message(uid, text, reply_markup=main_kb())
            except Exception as e:
                log.warning('مقدرتش أبعت رسالة البداية لـ %s: %r (ابعت /start للبوت الأول)', uid, e)
        print('\n' + re.sub(r'<[^>]+>', '', text) + '\n', flush=True)

    def _fonts_ok(self):
        mod = __import__('notes').load_pdf_module(os.path.dirname(os.path.abspath(__file__)))
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                mod.setup_fonts(self.cfg.fonts_dir)
            except SystemExit as e:
                return 'ناقصة (%s)' % (e.code,)
        return buf.getvalue().strip().replace('الخطوط:', '').strip() or 'تمام'

    async def run(self):
        await self.bot.set_my_commands([BotCommand(command='status', description='حالة البوت والكوتة'),
                                        BotCommand(command='queue', description='الطابور'),
                                        BotCommand(command='stop', description='إيقاف الشغل الحالي')])
        await self.startup_report()
        wk = asyncio.create_task(self.worker())
        try:
            await self.dp.start_polling(self.bot, polling_timeout=30, allowed_updates=['message', 'callback_query'])
        finally:
            wk.cancel()
            await self.core.close()
            await self.bot.session.close()


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    cfg = Config()
    bad = cfg.problems()
    if bad:
        print('❌ إعدادات ناقصة:\n  - ' + '\n  - '.join(bad))
        sys.exit(2)
    asyncio.run(BotApp(cfg).run())


if __name__ == '__main__':
    main()
