# -*- coding: utf-8 -*-
"""Groq Whisper: توزيع الـ chunks على المفاتيح مع حساب الكوتة (ثواني/ساعة و ثواني/يوم و RPM)."""
import asyncio, json, os, re, time
import aiohttp


class GroqError(Exception):
    pass


class Quota(GroqError):
    def __init__(self, msg, retry_after=60):
        super().__init__(msg)
        self.retry_after = retry_after


class GroqPool:
    def __init__(self, cfg, store, log=print):
        self.cfg, self.store, self.log = cfg, store, log
        self.labels = {}                       # label -> key
        for i, k in enumerate(cfg.groq_keys, 1):
            self.labels['K%d…%s' % (i, k[-4:])] = k
        led = store.data.setdefault('groq_ledger', {})
        self.ledger = {l: [tuple(x) for x in led.get(l, [])] for l in self.labels}
        self.reqs = {l: [] for l in self.labels}      # أوقات الطلبات (RPM)
        self.blocked = {l: 0.0 for l in self.labels}  # لحد امتى المفتاح موقوف (429)
        self.dead = set()
        self._lock = asyncio.Lock()
        self.ash = int(cfg.groq_ash * cfg.groq_margin)
        self.asd = int(cfg.groq_asd * cfg.groq_margin)

    # ───────── الحسابات ─────────
    def _prune(self, l, now):
        self.ledger[l] = [(t, s) for t, s in self.ledger[l] if now - t < 86400]
        self.reqs[l] = [t for t in self.reqs[l] if now - t < 60]

    def used(self, l, now=None):
        now = now or time.time()
        self._prune(l, now)
        h = sum(s for t, s in self.ledger[l] if now - t < 3600)
        d = sum(s for t, s in self.ledger[l])
        return h, d

    def left(self, l, now=None):
        h, d = self.used(l, now)
        return max(0, self.ash - h), max(0, self.asd - d)

    def capacity(self, l, now=None):
        now = now or time.time()
        if l in self.dead or self.blocked[l] > now:
            return 0
        a, b = self.left(l, now)
        return min(a, b)

    def wait_for(self, l, need, now=None):
        """كام ثانية لحد ما المفتاح يستوعب `need` ثانية صوت."""
        now = now or time.time()
        if l in self.dead:
            return 1e9
        w = max(0.0, self.blocked[l] - now)
        self._prune(l, now)
        for window, limit in ((3600, self.ash), (86400, self.asd)):
            entries = sorted((t, s) for t, s in self.ledger[l] if now - t < window)
            used = sum(s for _, s in entries)
            if used + need <= limit:
                continue
            if need > limit:
                return 1e9
            freed = 0
            for t, s in entries:
                freed += s
                if used - freed + need <= limit:
                    w = max(w, t + window - now + 1)
                    break
        return w

    def estimate_wait(self, keys, total):
        """محاكاة: كام ثانية انتظار (لتجديد الكوتة) لحد ما الـ keys دي تستوعب `total` ثانية صوت."""
        now = time.time()
        led = {l: [(t, s) for t, s in self.ledger[l] if now - t < 86400] for l in keys}
        t, remaining, guard, first_wait = now, float(total), 0, None
        while remaining > 1 and guard < 5000:
            guard += 1
            took = 0.0
            for l in keys:
                if l in self.dead:
                    continue
                h = sum(s for tt, s in led[l] if t - tt < 3600)
                d = sum(s for tt, s in led[l] if t - tt < 86400)
                cap = max(0.0, min(self.ash - h, self.asd - d))
                take = min(cap, remaining)
                if take > 0:
                    led[l].append((t, take))
                    remaining -= take
                    took += take
            if remaining <= 1:
                break
            nxt = [tt + w for l in keys for tt, _ in led[l] for w in (3600, 86400) if tt + w > t]
            if not nxt:
                return 1e9
            t = min(nxt) + 1
        return max(0.0, t - now)

    def plan_keys(self, total_secs, max_keys=2):
        now = time.time()
        live = [l for l in self.labels if l not in self.dead]
        caps = sorted(live, key=lambda l: -self.capacity(l, now))
        chosen = caps[:max(1, max_keys)]
        cap_sum = sum(self.capacity(l, now) for l in chosen)
        wait = 0.0 if cap_sum >= total_secs else (self.estimate_wait(chosen, total_secs) if chosen else 1e9)
        return chosen, cap_sum, wait

    def _persist(self):
        self.store.data['groq_ledger'] = {l: [list(x) for x in v] for l, v in self.ledger.items()}
        self.store.save()

    async def _reserve(self, allowed, secs, on_wait=None):
        billed = max(10.0, secs)
        waited_since = None
        while True:
            async with self._lock:
                now = time.time()
                best, best_cap = None, -1
                for l in allowed:
                    self._prune(l, now)
                    if l in self.dead:
                        continue
                    if len(self.reqs[l]) >= self.cfg.groq_rpm - 1:
                        continue
                    cap = self.capacity(l, now)
                    if cap >= billed and cap > best_cap:
                        best, best_cap = l, cap
                if best:
                    self.ledger[best].append((now, billed))
                    self.reqs[best].append(now)
                    self._persist()
                    return best, billed, now
                live = [l for l in allowed if l not in self.dead]
                if not live:
                    raise GroqError('كل مفاتيح Groq المختارة مش شغالة.')
                w = min(self.wait_for(l, billed, now) for l in live)
                if any(len(self.reqs[l]) >= self.cfg.groq_rpm - 1 for l in live):
                    w = min(w, 8)
            if on_wait and (waited_since is None or time.time() - waited_since > 45):
                waited_since = time.time()
                await on_wait(w)
            await asyncio.sleep(min(max(w, 2), 30))

    def _refund(self, label, entry_ts, billed):
        try:
            self.ledger[label].remove((entry_ts, billed))
        except ValueError:
            pass
        self._persist()

    # ───────── الطلب الفعلي ─────────
    async def transcribe(self, session, path, dur, allowed, language=None, prompt=None, on_wait=None):
        """يرجّع dict(segments=[...], label=...). بيعيد المحاولة ويبدّل المفتاح لوحده."""
        attempts = 0
        while True:
            attempts += 1
            label, billed, ts = await self._reserve(allowed, dur, on_wait)
            key = self.labels[label]
            try:
                data = await self._call(session, key, path, language, prompt)
                return dict(segments=data.get('segments') or [], text=data.get('text', ''), label=label)
            except Quota as e:
                self._refund(label, ts, billed)
                self.blocked[label] = time.time() + min(e.retry_after, 6 * 3600)
                self.log('Groq %s quota -> blocked %ds (%s)' % (label, e.retry_after, str(e)[:120]))
                if attempts > 12:
                    raise
            except GroqError as e:
                self._refund(label, ts, billed)
                if 'invalid_api_key' in str(e) or '401' in str(e):
                    self.dead.add(label)
                    self.log('Groq %s dead' % label)
                    continue
                if attempts >= 5:
                    raise
                await asyncio.sleep(2 ** attempts)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                self._refund(label, ts, billed)
                if attempts >= 5:
                    raise GroqError('مشكلة شبكة مع Groq: %r' % e)
                await asyncio.sleep(2 ** attempts)

    async def _call(self, session, key, path, language, prompt):
        form = aiohttp.FormData()
        form.add_field('model', self.cfg.groq_model)
        form.add_field('response_format', 'verbose_json')
        form.add_field('temperature', '0')
        form.add_field('timestamp_granularities[]', 'segment')
        if language and language != 'auto':
            form.add_field('language', language)
        if prompt:
            form.add_field('prompt', prompt[:800])
        with open(path, 'rb') as fh:
            form.add_field('file', fh.read(), filename=os.path.basename(path), content_type='audio/mpeg')
        async with session.post(self.cfg.groq_base.rstrip('/') + '/audio/transcriptions', data=form,
                                headers={'Authorization': 'Bearer ' + key},
                                timeout=aiohttp.ClientTimeout(total=900)) as r:
            body = await r.text()
            if r.status == 200:
                return json.loads(body)
            if r.status == 429:
                ra = r.headers.get('retry-after')
                try:
                    ra = float(ra)
                except (TypeError, ValueError):
                    m = re.search(r'try again in\s*(?:(\d+)h)?(?:(\d+)m)?([\d.]+)?s?', body)
                    ra = 120.0
                    if m and any(m.groups()):
                        ra = int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + float(m.group(3) or 0)
                raise Quota('429: ' + body[:200], ra)
            if r.status in (401, 403):
                raise GroqError('401 invalid_api_key: ' + body[:150])
            if r.status == 413:
                raise GroqError('413 الملف كبير: ' + body[:150])
            raise GroqError('HTTP %d: %s' % (r.status, body[:200]))

    async def check_keys(self, session):
        """فحص سريع للمفاتيح (GET /models مش بيستهلك كوتة صوت)."""
        res = {}
        for l, k in self.labels.items():
            try:
                async with session.get(self.cfg.groq_base.rstrip('/') + '/models', headers={'Authorization': 'Bearer ' + k},
                                       timeout=aiohttp.ClientTimeout(total=20)) as r:
                    res[l] = r.status == 200
                    if r.status in (401, 403):
                        self.dead.add(l)
            except Exception:
                res[l] = None
        return res
