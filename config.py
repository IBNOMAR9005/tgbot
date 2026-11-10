# -*- coding: utf-8 -*-
"""كل الإعدادات من متغيرات البيئة (الخلية 5 في الـ notebook بتجهزها من Colab Secrets)."""
import os, re
from dataclasses import dataclass, field


def _list(name, sep=r'[,\n;\s]+'):
    raw = os.environ.get(name, '') or ''
    return [x.strip() for x in re.split(sep, raw) if x.strip()]


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


HERE = os.path.dirname(os.path.abspath(__file__))


@dataclass
class Config:
    bot_token: str = field(default_factory=lambda: os.environ.get('BOT_TOKEN', ''))
    allowed_ids: list = field(default_factory=lambda: [int(x) for x in _list('ALLOWED_IDS') if x.lstrip('-').isdigit()])
    local_api: str = field(default_factory=lambda: os.environ.get('LOCAL_API_URL', 'http://127.0.0.1:8081'))

    groq_keys: list = field(default_factory=lambda: _list('GROQ_KEYS'))
    gemini_keys: list = field(default_factory=lambda: _list('GEMINI_KEYS'))          # المواضيع والتوقيتات
    gemini_keys_pdf: list = field(default_factory=lambda: _list('GEMINI_KEYS_PDF'))  # الملزمة (MD/PDF)

    groq_base: str = field(default_factory=lambda: os.environ.get('GROQ_BASE', 'https://api.groq.com/openai/v1'))
    gemini_base: str = field(default_factory=lambda: os.environ.get('GEMINI_BASE', 'https://generativelanguage.googleapis.com/v1beta'))
    groq_model: str = field(default_factory=lambda: os.environ.get('GROQ_MODEL', 'whisper-large-v3'))
    # بالترتيب: يجرب الأول، ولو مش متاح/خلصت كوتته ينزل للتاني
    gemini_models: list = field(default_factory=lambda: _list('GEMINI_MODELS') or ['gemini-3.5-flash', 'gemini-2.5-pro', 'gemini-2.5-flash'])
    gemini_models_pdf: list = field(default_factory=lambda: _list('GEMINI_MODELS_PDF') or _list('GEMINI_MODELS') or ['gemini-3.5-flash', 'gemini-2.5-pro', 'gemini-2.5-flash'])
    gemini_max_out: int = field(default_factory=lambda: _int('GEMINI_MAX_OUT', 65536))

    data_dir: str = field(default_factory=lambda: os.environ.get('DATA_DIR', os.path.join(HERE, 'data')))
    work_dir: str = field(default_factory=lambda: os.environ.get('WORK_DIR', '/tmp/tgbot_work'))
    fonts_dir: str = field(default_factory=lambda: os.environ.get('FONTS_DIR', os.path.join(HERE, 'fonts')))

    # ليميتات Groq (الـ free tier) - عدلها لو Groq غيّرتها
    groq_ash: int = field(default_factory=lambda: _int('GROQ_ASH', 7200))      # ثواني صوت في الساعة
    groq_asd: int = field(default_factory=lambda: _int('GROQ_ASD', 28800))     # ثواني صوت في اليوم
    groq_rpm: int = field(default_factory=lambda: _int('GROQ_RPM', 20))
    groq_rpd: int = field(default_factory=lambda: _int('GROQ_RPD', 2000))
    groq_margin: float = field(default_factory=lambda: _float('GROQ_MARGIN', 0.97))  # هامش أمان
    max_keys_per_job: int = field(default_factory=lambda: _int('MAX_KEYS_PER_JOB', 2))

    chunk_seconds: int = field(default_factory=lambda: _int('CHUNK_SECONDS', 900))
    chunk_mp3_kbps: int = field(default_factory=lambda: _int('CHUNK_MP3_KBPS', 48))
    normalize_audio: bool = field(default_factory=lambda: os.environ.get('NORMALIZE_AUDIO', '1') != '0')
    topics_review: bool = field(default_factory=lambda: os.environ.get('TOPICS_REVIEW', '1') != '0')
    whisper_prompt: str = field(default_factory=lambda: os.environ.get(
        'WHISPER_PROMPT',
        'محاضرة طبية بالعامية المصرية فيها مصطلحات إنجليزي زي: pathophysiology, diagnosis, '
        'treatment, hypertension, myocardial infarction, ECG, differential diagnosis.'))

    def problems(self):
        p = []
        if not self.bot_token:
            p.append('BOT_TOKEN فاضي')
        if not self.allowed_ids:
            p.append('ALLOWED_IDS فاضي (لازم رقم الـ user id بتاعك)')
        if not self.groq_keys:
            p.append('GROQ_KEYS فاضي')
        if not self.gemini_keys:
            p.append('GEMINI_KEYS فاضي (المواضيع والتوقيتات مش هتشتغل)')
        return p
