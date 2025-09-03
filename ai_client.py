# -*- coding: utf-8 -*-
"""
ai_client.py — генерация текста/картинок для twitter_bot.py

Приоритет:
1) Gemini (новый SDK google-genai) — если установлен и есть ключ (GEMINI_API_KEY).
2) Старый пакет google-generativeai — если установлен.
3) Локальный фолбэк — всегда.

Функции:
- ai_generate_text(topic) -> (text: str, warn: str|None)
- ai_generate_image(topic) -> (image_path: str, warn: str|None)
- ai_suggest_hashtags(text) -> List[str]

Длина текста:
- По умолчанию TARGET_CHAR_LEN=757 и TARGET_CHAR_TOL=20 (можно переопределить через ENV).
- Также можно задать в теме: "len=757" / "len:757" / "#len=757".
"""

from __future__ import annotations
import os
import re
import io
import time
import uuid
import base64
import random
import logging
import tempfile
from typing import Tuple, List, Optional

log = logging.getLogger("ai_client")
if not log.handlers:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")

# -----------------------------------------------------------------------------
# Конфиг длины: по умолчанию 757 ± 20
# -----------------------------------------------------------------------------
_LEN_RE = re.compile(r"(?:^|\b)(?:len\s*[:=]\s*|#len\s*=\s*)(\d{2,5})\b", re.I)

def _get_target_len_cfg() -> Tuple[int, int]:
    def _int(env, default):
        try:
            return int(os.getenv(env, str(default)) or str(default))
        except Exception:
            return default
    # Значения по умолчанию: 757 символов ± 20
    return max(0, _int("TARGET_CHAR_LEN", 757)), max(0, _int("TARGET_CHAR_TOL", 20))

def _extract_len_from_topic(topic: str) -> Tuple[str, Optional[int]]:
    m = _LEN_RE.search(topic or "")
    desired = None
    if m:
        try:
            desired = int(m.group(1))
        except Exception:
            desired = None
        topic = _LEN_RE.sub("", topic).strip()
    return topic, desired

# -----------------------------------------------------------------------------
# SDK detection / ключи
# -----------------------------------------------------------------------------
# Приоритет ключей: GEMINI_API_KEY -> GOOGLE_API_KEY -> GOOGLE_GENAI_API_KEY
_API_KEY = (
    os.getenv("GEMINI_API_KEY")
    or os.getenv("GOOGLE_API_KEY")
    or os.getenv("GOOGLE_GENAI_API_KEY")
)

# Новый SDK: google-genai
_GENAI_NEW = False
try:
    from google import genai as genai_new
    from google.genai import types as genai_types  # может пригодиться, не обязателен
    _GENAI_NEW = True
except Exception as _e:
    log.info("google-genai not available: %s", _e)

# Старый SDK: google-generativeai (fallback)
_GENAI_OLD = False
try:
    import google.generativeai as genai_old
    _GENAI_OLD = True
except Exception as _e:
    log.info("google-generativeai not available: %s", _e)

_client_new = None
if _GENAI_NEW and _API_KEY:
    try:
        _client_new = genai_new.Client(api_key=_API_KEY)
        log.info("Gemini (google-genai) client initialized (key from env).")
    except Exception as e:
        log.warning("Failed to init google-genai client: %s", e)
        _client_new = None

if _GENAI_OLD and _API_KEY:
    try:
        genai_old.configure(api_key=_API_KEY)
        log.info("Gemini (google-generativeai) configured.")
    except Exception as e:
        log.warning("Failed to configure google-generativeai: %s", e)

# -----------------------------------------------------------------------------
# Локальный синтетический генератор текста (фолбэк)
# -----------------------------------------------------------------------------
_SENTENCE_BANK = [
    "Here’s a quick take on the topic.",
    "Let’s keep it concise and actionable.",
    "Key point:",
    "What this means in practice:",
    "In short,",
    "The upside:",
    "The downside:",
    "A practical example:",
    "Pro tip:",
    "Bottom line:",
    "TL;DR:",
]

def _local_synthesize(topic: str, target_len: int, tol: int) -> Tuple[str, Optional[str]]:
    random.seed(hash((topic, target_len, tol, int(time.time() // 60))) & 0xFFFFFFFF)
    bullets = [
        f"{topic} matters because it aligns incentives and improves measurable outcomes.",
        f"The trend around {topic} is shifting from hype to practical delivery.",
        "Focus on fundamentals, track real metrics, and iterate quickly.",
        "Avoid over-engineering. Ship small increments and learn fast.",
        "Define a metric to move in 7 days and report progress.",
    ]
    spice = random.choice([
        "No magic — just compounding small wins.",
        "Data first, narrative later.",
        "Edge comes from consistency.",
        "Simple beats complex when stakes are high.",
        "If it’s not measured, it didn’t happen.",
    ])
    text = topic.strip().rstrip(".") + ": " + bullets[0]
    for b in bullets[1:]:
        text += " " + random.choice(_SENTENCE_BANK) + " " + b
    text += " " + spice

    if target_len > 0:
        low, high = max(0, target_len - tol), target_len + tol
        if len(text) < low:
            filler = (" Keep it human-centric, verify with real users, and document the path."
                      " Small experiments reduce risk and reveal compounding effects over time.")
            while len(text) + len(filler) <= high:
                text += filler
            if len(text) < low:
                text += " Iterate, measure, improve."
        elif len(text) > high:
            text = text[:high].rstrip()
        warn = None if (low <= len(text) <= high) else f"target_len={target_len}±{tol}, got={len(text)}"
        return text.strip(), warn
    return text.strip(), None

# -----------------------------------------------------------------------------
# Текст: Gemini -> фолбэк
# -----------------------------------------------------------------------------
def _gemini_generate_text(topic: str, target_len: int, tol: int) -> Tuple[str, Optional[str]]:
    """
    Новый SDK: client.models.generate_content(model="gemini-2.5-flash", contents="...").
    Старый SDK: GenerativeModel(...).generate_content(prompt).
    """
    # Если цель не задана, всё равно просим близи 757±20 по умолчанию
    if target_len <= 0:
        target_len, tol = _get_target_len_cfg()

    prompt = (
        f"Write an English social-media post about: {topic!s}.\n"
        f"Requirements:\n"
        f" - Target length: around {target_len} characters (±{tol}).\n"
        f" - Plain text only (no markdown, no hashtags, no links).\n"
        f" - 1–3 short paragraphs, energetic but factual.\n"
        f"Return ONLY the post text."
    ).strip()

    # Новый SDK
    if _client_new:
        try:
            resp = _client_new.models.generate_content(
                model=os.getenv("GEMINI_TEXT_MODEL", "gemini-2.5-flash"),
                contents=prompt,
            )
            text = (getattr(resp, "text", None) or "").strip()
            if text:
                return text, None
            # Попытка собрать из частей
            try:
                parts = resp.candidates[0].content.parts
                buf = []
                for p in parts:
                    if getattr(p, "text", None):
                        buf.append(p.text)
                text = " ".join(buf).strip()
                if text:
                    return text, None
            except Exception:
                pass
            return "", "empty_text_from_gemini"
        except Exception as e:
            log.warning("Gemini(new SDK) text fail: %s", e)

    # Старый SDK
    if _GENAI_OLD and _API_KEY:
        try:
            model_name = os.getenv("GEMINI_TEXT_MODEL_OLD", "gemini-1.5-flash")
            model = genai_old.GenerativeModel(model_name)
            resp = model.generate_content(prompt)
            text = (getattr(resp, "text", None) or "").strip()
            if text:
                return text, None
            return "", "empty_text_from_old_sdk"
        except Exception as e:
            log.warning("Gemini(old SDK) text fail: %s", e)

    # Фолбэк локальный
    return _local_synthesize(topic, target_len, tol)

def ai_generate_text(topic: str) -> Tuple[str, Optional[str]]:
    topic = (topic or "").strip()
    topic, inline_len = _extract_len_from_topic(topic)
    env_len, env_tol = _get_target_len_cfg()
    target_len = inline_len or env_len
    tol = env_tol

    if not topic:
        return "Draft post.", "empty_topic_autofill"

    text, warn = _gemini_generate_text(topic, target_len, tol)

    # Мягкая доводка до коридора (757±20 по умолчанию, либо inline/env)
    if target_len > 0 and text:
        low, high = max(0, target_len - tol), target_len + tol
        if len(text) < low:
            pad = " Iterate, measure, improve."
            while len(text) + len(pad) <= high:
                text += pad
        elif len(text) > high:
            text = text[:high].rstrip()
        if not (low <= len(text) <= high):
            warn = (warn or "") + (f" (post={len(text)} chars, target {target_len}±{tol})")
    return text, (warn or None)

# -----------------------------------------------------------------------------
# Изображение: Gemini (image preview) -> локальный фолбэк
# -----------------------------------------------------------------------------
_TRY_PIL = True
try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:
    _TRY_PIL = False

_RED_DOT_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAAHklEQVR4nO3BMQEAAADCoPVPbQhPoAAAAAAAAAAAwE8G1gAAeQy1NwAAAABJRU5ErkJggg=="
)

def _save_bytes_to_temp_png(data: bytes) -> str:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    with open(tmp.name, "wb") as f:
        f.write(data)
    return tmp.name

def _local_placeholder_png(topic: str) -> str:
    if _TRY_PIL:
        try:
            W, H = 1200, 675
            img = Image.new("RGB", (W, H), (24, 26, 32))
            d = ImageDraw.Draw(img)
            for y in range(H):
                shade = int(24 + (y / H) * 56)
                d.line([(0, y), (W, y)], fill=(shade, shade, shade))
            d.rectangle([(8, 8), (W-8, H-8)], outline=(90, 180, 255), width=3)
            title = (topic or "AiCoin").strip()[:160]
            try:
                font = ImageFont.truetype("DejaVuSans-Bold.ttf", 46)
                font2 = ImageFont.truetype("DejaVuSans.ttf", 28)
            except Exception:
                font = ImageFont.load_default(); font2 = ImageFont.load_default()
            tw, th = d.textsize(title, font=font)
            d.text(((W - tw)//2, (H - th)//2 - 20), title, fill=(240, 240, 240), font=font)
            sw, sh = d.textsize("placeholder", font=font2)
            d.text(((W - sw)//2, (H - sh)//2 + 36), "placeholder", fill=(160, 200, 255), font=font2)
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
            img.save(tmp.name, format="PNG"); tmp.close()
            return tmp.name
        except Exception:
            pass
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    with open(tmp.name, "wb") as f:
        f.write(_RED_DOT_PNG)
    return tmp.name

def _gemini_generate_image(topic: str) -> Tuple[str, Optional[str]]:
    """
    Новый SDK: client.models.generate_content(model="gemini-2.5-flash-image-preview", contents=[prompt]).
    Возвращаем первый part с inline_data (bytes/base64).
    """
    prompt = (
        f"Create a clean, brand-safe illustration related to: {topic}. "
        f"Neutral background, no text overlay, suitable for social post. "
        f"Prefer 16:9 composition."
    )

    if _client_new:
        try:
            resp = _client_new.models.generate_content(
                model=os.getenv("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image-preview"),
                contents=[prompt],
            )
            try:
                parts = resp.candidates[0].content.parts
                for p in parts:
                    if getattr(p, "inline_data", None) and getattr(p.inline_data, "data", None):
                        data = p.inline_data.data
                        if isinstance(data, str):
                            data = base64.b64decode(data)
                        return _save_bytes_to_temp_png(data), None
            except Exception as pe:
                log.warning("Gemini image parse parts failed: %s", pe)
            return "", "empty_image_from_gemini"
        except Exception as e:
            log.warning("Gemini(new SDK) image fail: %s", e)

    # Старый SDK не генерит изображение напрямую — фолбэк локальный
    return _local_placeholder_png(topic), "image_fallback_local"

def ai_generate_image(topic: str) -> Tuple[str, Optional[str]]:
    path, warn = _gemini_generate_image(topic)
    if not path:
        path = _local_placeholder_png(topic)
        warn = (warn or "image_fallback_local")
    return path, warn

# -----------------------------------------------------------------------------
# Хэштеги — локальная эвристика
# -----------------------------------------------------------------------------
def _tok(s: str) -> List[str]:
    return re.findall(r"[A-Za-z0-9$#]{2,32}", s or "")

def ai_suggest_hashtags(text: str) -> List[str]:
    base = ["#AiCoin", "#AI", "$Ai", "#crypto"]
    extra = []
    tl = (text or "").lower()
    if "eth" in tl or "ethereum" in tl:
        extra += ["#Ethereum"]
    if "btc" in tl or "bitcoin" in tl:
        extra += ["#Bitcoin"]
    if "nft" in tl:
        extra += ["#NFT"]

    for w in _tok(text)[:6]:
        if w.lower() in ("http", "https"):
            continue
        if not (w.startswith("#") or w.startswith("$")):
            w = "#" + w
        extra.append(w)

    seen, out = set(), []
    for t in base + extra:
        k = t.lower()
        if k in seen:
            continue
        seen.add(k); out.append(t)
    return out[:12]