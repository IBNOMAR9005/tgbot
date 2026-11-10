# -*- coding: utf-8 -*-
"""حالة صغيرة على Drive (تفضل بعد ما Colab يفصل): تفضيلات المستخدم + سجل استهلاك مفاتيح Groq."""
import json, os, threading


class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.data = {'prefs': {}, 'groq_ledger': {}}
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, encoding='utf-8') as f:
                self.data.update(json.load(f))
        except (OSError, ValueError):
            pass

    def save(self):
        with self.lock:
            tmp = self.path + '.tmp'
            try:
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(self.data, f, ensure_ascii=False)
                os.replace(tmp, self.path)
            except OSError:
                pass

    def prefs(self, uid):
        p = self.data['prefs'].setdefault(str(uid), {})
        p.setdefault('lang', 'ar')
        p.setdefault('fmt', 'srt')
        p.setdefault('topics', True)
        return p
