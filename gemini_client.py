# -*- coding: utf-8 -*-
"""Gemini REST: تدوير المفاتيح والموديلات تلقائياً عند 429/503/خطأ مفتاح."""
import asyncio, json, re, time
import aiohttp


class GeminiError(Exception):
    pass


SAFETY = [{'category': c, 'threshold': 'BLOCK_NONE'} for c in (
    'HARM_CATEGORY_HARASSMENT', 'HARM_CATEGORY_HATE_SPEECH',
    'HARM_CATEGORY_SEXUALLY_EXPLICIT', 'HARM_CATEGORY_DANGEROUS_CONTENT')]


class GeminiPool:
    def __init__(self, keys, models, base, max_out=65536, name='gemini', log=print):
        self.labels = {'%s%d…%s' % (name[0].upper(), i, k[-4:]): k for i, k in enumerate(keys, 1)}
        self.models, self.base, self.max_out, self.log, self.name = list(models), base.rstrip('/'), max_out, log, name
        self.cool = {}          # (label, model) -> time
        self.dead = set()
        self.last_used = {l: 0.0 for l in self.labels}
        self.avail_models = None
        self.last_err = ''
        self.errs = []

    async def check(self, session):
        """يفحص المفاتيح ويجيب الموديلات المتاحة فعلاً (ويفلتر القايمة المطلوبة)."""
        ok, avail = {}, set()
        for l, k in self.labels.items():
            try:
                async with session.get(self.base + '/models?pageSize=200', headers={'x-goog-api-key': k},
                                       timeout=aiohttp.ClientTimeout(total=25)) as r:
                    if r.status == 200:
                        js = await r.json()
                        ok[l] = True
                        for m in js.get('models', []):
                            if 'generateContent' in m.get('supportedGenerationMethods', []):
                                avail.add(m['name'].split('/')[-1])
                    else:
                        ok[l] = False
                        if r.status in (400, 401, 403):
                            self.dead.add(l)
            except Exception:
                ok[l] = None
        if avail:
            self.avail_models = avail
            keep = [m for m in self.models if m in avail]
            self.missing_models = [m for m in self.models if m not in avail]
            if keep:
                self.models = keep
        return ok

    def _note(self, label, model, msg):
        self.last_err = ('%s/%s: %s' % (label, model, msg))[:230]
        self.errs.append(self.last_err)
        self.errs = self.errs[-20:]
        self.log('Gemini ' + self.last_err)

    def _pick(self, now):
        """(label, model) التالي المتاح بالترتيب: موديل بالأولوية، ومفتاح الأقل استخداماً."""
        soonest = None
        for m in self.models:
            ready = [l for l in self.labels if l not in self.dead and self.cool.get((l, m), 0) <= now]
            if ready:
                l = min(ready, key=lambda x: self.last_used[x])
                return l, m, 0
            ts = [self.cool.get((l, m), 0) for l in self.labels if l not in self.dead]
            if ts:
                soonest = min(soonest, min(ts)) if soonest else min(ts)
        return None, None, max(1, (soonest or now + 30) - now)

    async def generate(self, session, system, contents, schema=None, temperature=0.25, max_wait=1500, on_wait=None,
                       want_json=False):
        """contents: list of (role, text). يرجّع (text, finish_reason, model)."""
        t0 = time.time()
        fails = 0
        while True:
            now = time.time()
            l, m, wait = self._pick(now)
            if l is None:
                if not [x for x in self.labels if x not in self.dead]:
                    raise GeminiError('كل مفاتيح Gemini (%s) مش شغالة. آخر سبب: %s' % (self.name, self.last_err or '؟'))
                if now - t0 + wait > max_wait:
                    raise GeminiError('كوتة Gemini (%s) مش متاحة دلوقتي (أقرب مفتاح بعد ~%d دقيقة). آخر سبب: %s'
                                      % (self.name, wait // 60 + 1, self.last_err or '؟'))
                if on_wait:
                    await on_wait(wait)
                await asyncio.sleep(min(wait, 30))
                continue
            self.last_used[l] = now
            try:
                text, fin = await self._call(session, self.labels[l], m, system, contents, schema, temperature, want_json)
                return text, fin, m
            except _Cool as e:
                self._note(l, m, e.msg)
                self.cool[(l, m)] = time.time() + e.seconds
                self.log('Gemini %s/%s cooldown %ds: %s' % (l, m, e.seconds, e.msg[:100]))
            except _Dead as e:
                self._note(l, m, e.msg)
                self.dead.add(l)
                self.log('Gemini %s dead: %s' % (l, e.msg[:100]))
            except _ModelBad as e:
                self._note(l, m, e.msg)
                self.cool[(l, m)] = time.time() + 3600
                self.log('Gemini model %s bad: %s' % (m, e.msg[:100]))
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                fails += 1
                self.cool[(l, m)] = time.time() + 5 * fails
                if fails > 8:
                    raise GeminiError('مشكلة شبكة مع Gemini: %r' % e)

    async def _call(self, session, key, model, system, contents, schema, temperature, want_json):
        gc = {'temperature': temperature, 'maxOutputTokens': self.max_out}
        if schema is not None:
            gc['responseMimeType'] = 'application/json'
            gc['responseSchema'] = schema
        elif want_json:
            gc['responseMimeType'] = 'application/json'
        body = {'contents': [{'role': r, 'parts': [{'text': t}]} for r, t in contents],
                'generationConfig': gc, 'safetySettings': SAFETY}
        if system:
            body['systemInstruction'] = {'parts': [{'text': system}]}
        url = '%s/models/%s:generateContent' % (self.base, model)
        async with session.post(url, json=body, headers={'x-goog-api-key': key},
                                timeout=aiohttp.ClientTimeout(total=1500)) as r:
            raw = await r.text()
            if r.status == 200:
                js = json.loads(raw)
                cands = js.get('candidates') or []
                if not cands:
                    fb = js.get('promptFeedback', {})
                    raise _Cool(120, 'no candidates: %s' % json.dumps(fb)[:150])
                c = cands[0]
                parts = (c.get('content') or {}).get('parts') or []
                text = ''.join(p.get('text', '') for p in parts if not p.get('thought'))
                fin = c.get('finishReason', '')
                if not text.strip() and fin not in ('MAX_TOKENS',):
                    raise _Cool(60, 'empty (%s)' % fin)
                return text, fin
            low = raw.lower()
            if r.status == 429:
                m = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', raw)
                secs = float(m.group(1)) + 2 if m else 60
                qids = ' '.join(re.findall(r'"quotaId":\s*"([^"]+)"', raw))
                if 'perday' in qids.lower():
                    secs = max(secs, 1800)
                if '"quotaValue": "0"' in raw or 'limit: 0' in low:
                    secs = 6 * 3600   # الموديل ده مش مسموح على الـ free tier للمفتاح ده
                raise _Cool(secs, '429 %s | %s' % (qids[:90] or 'quota', _brief(raw)))
            if r.status in (500, 502, 503, 504):
                raise _Cool(20, '%d %s' % (r.status, raw[:100]))
            if r.status == 404:
                raise _ModelBad('404 ' + raw[:120])
            if r.status in (401, 403) or 'api key not valid' in low or 'api_key_invalid' in low:
                raise _Dead('%d %s' % (r.status, raw[:150]))
            if r.status == 400 and ('max_output_tokens' in low or 'maxoutputtokens' in low or 'output token' in low):
                self.max_out = max(8192, self.max_out // 2)
                raise _Cool(1, 'max tokens too big -> %d' % self.max_out)
            raise GeminiError('Gemini HTTP %d: %s' % (r.status, raw[:300]))


def _brief(raw):
    try:
        js = json.loads(raw)
        return str(js.get('error', {}).get('message', raw))[:150].replace('\n', ' ')
    except ValueError:
        return raw[:150].replace('\n', ' ')


class _Cool(Exception):
    def __init__(self, seconds, msg=''):
        super().__init__(msg)
        self.seconds, self.msg = seconds, msg


class _Dead(Exception):
    def __init__(self, msg=''):
        super().__init__(msg)
        self.msg = msg


class _ModelBad(Exception):
    def __init__(self, msg=''):
        super().__init__(msg)
        self.msg = msg
