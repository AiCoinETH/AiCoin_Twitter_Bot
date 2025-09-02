# ai_client.py
# -*- coding: utf-8 -*-
"""
Мини-клиент ИИ для twitter_bot.py
- ai_generate_text(topic) -> (text, warning)
- ai_generate_image(topic) -> (local_image_path, warning)
- ai_suggest_hashtags(text) -> list[str]
"""

from __future__ import annotations

import os
import io
import logging
import tempfile
import urllib.parse as _up

log = logging.getLogger("ai_client")

# ========= Опционально: Google Gemini для текста =========
_GEMINI_KEY = (os.getenv("GEMINI_API_KEY") or "").strip()
_USE_GEMINI = bool(_GEMINI_KEY)

def _try_import_gemini():
    if not _USE_GEMINI:
        return None
    try:
        import google.generativeai as genai  # type: ignore
        genai.configure(api_key=_GEMINI_KEY)
        return genai
    except Exception as e:
        log.warning("Gemini import/config failed: %s", e)
        return None

_GENAI = _try_import_gemini()

# ========= Вспомогательно: безопасное имя файла =========
def _safe_temp_path(suffix: str = ".png") -> str:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.close()
    return tmp.name

# ========= Публичные функции =========
def ai_generate_text(topic: str) -> tuple[str, str]:
    """
    Возвращает (text, warning). Если warning != "", генерация была в fallback-режиме.
    """
    topic = (topic or "").strip()
    if not topic:
        return "", "Пустая тема."

    # --- Попытка через Gemini ---
    if _GENAI:
        try:
            m = _GENAI.GenerativeModel("gemini-1.5-flash")
            prompt = (
                "Write a concise, punchy post for Twitter/X in English about:\n"
                f"\"{topic}\"\n"
                "- No emojis at the beginning\n"
                "- One to three short sentences\n"
                "- No hashtags and no links\n"
                "- Concrete and helpful tone\n"
            )
            resp = m.generate_content(prompt)
            txt = (getattr(resp, "text", "") or "").strip()
            if not txt:
                raise RuntimeError("Gemini returned empty text")
            return txt, ""
        except Exception as e:
            log.warning("Gemini text gen failed: %s", e)

    # --- Локальный фолбэк (без внешнего ИИ) ---
    stub = (
        f"{topic}. Quick take: why it matters and how it helps real users. "
        "Actionable insight in a nutshell."
    )
    return stub, "⚠️ Gemini отключён или недоступен — использую шаблонный текст."

def ai_suggest_hashtags(text: str) -> list[str]:
    """
    Простой локальный генератор хэштегов.
    """
    base = {"#AI", "#crypto", "$AI", "#AiCoin"}
    tl = (text or "").lower()
    if "eth" in tl or "ethereum" in tl:
        base.add("#ETH")
    if "token" in tl or "coin" in tl:
        base.add("#altcoins")
    if "defi" in tl:
        base.add("#DeFi")
    if "nft" in tl:
        base.add("#NFT")
    return list(base)[:6]

def ai_generate_image(topic: str) -> tuple[str, str]:
    """
    Возвращает (local_image_path, warning).
    Создаёт локальный PNG 1200x675 с текстом темы.
    Порядок:
      1) Пытаемся сгенерировать через Pillow (локально, без сети).
      2) Если Pillow нет — качаем плейсхолдер с dummyimage.com.
    """
    topic = (topic or "").strip()
    if not topic:
        return "", "Пустая тема."

    text = topic.replace("\n", " ").strip()
    if len(text) > 120:
        text = text[:120] + "…"

    # --- Вариант 1: Pillow (предпочтительно, без внешней сети) ---
    try:
        from PIL import Image, ImageDraw, ImageFont  # type: ignore

        W, H = 1200, 675
        bg = (10, 10, 10)
        fg = (255, 255, 255)

        img = Image.new("RGB", (W, H), bg)
        draw = ImageDraw.Draw(img)

        # Шрифт: пробуем DejaVuSans, иначе дефолт
        try:
            font = ImageFont.truetype("DejaVuSans.ttf", 44)
        except Exception:
            font = ImageFont.load_default()

        # Многострочная разметка по ширине
        max_width = int(W * 0.88)
        words = text.split()
        lines, cur = [], ""
        for w in words:
            test = (cur + " " + w).strip()
            if draw.textlength(test, font=font) <= max_width:
                cur = test
            else:
                if cur:
                    lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        if len(lines) > 5:
            lines = lines[:5]
            lines[-1] = (lines[-1] + " …").strip()

        # Рисуем с центровкой
        total_h = sum(int(font.getbbox(l)[3] - font.getbbox(l)[1]) for l in lines) + (len(lines) - 1) * 10
        y = (H - total_h) // 2
        for l in lines:
            w = int(draw.textlength(l, font=font))
            x = (W - w) // 2
            draw.text((x, y), l, font=font, fill=fg)
            y += int(font.getbbox(l)[3] - font.getbbox(l)[1]) + 10

        path = _safe_temp_path(".png")
        img.save(path, format="PNG", optimize=True)
        return path, ""
    except Exception as e:
        log.warning("Pillow image gen failed, fallback to dummyimage.com: %s", e)

    # --- Вариант 2: dummyimage.com (требует интернет) ---
    try:
        import requests  # type: ignore

        q = _up.quote_plus(text)
        url = f"https://dummyimage.com/1200x675/0a0a0a/ffffff.png&text={q}"
        r = requests.get(url, timeout=20, headers={"User-Agent": "ai-client/1.0"})
        r.raise_for_status()
        path = _safe_temp_path(".png")
        with open(path, "wb") as f:
            f.write(r.content)
        return path, "🖼️ Сгенерирован простой баннер по теме (плейсхолдер)."
    except Exception as e:
        log.error("Image placeholder download failed: %s", e)
        return "", "⚠️ Не удалось создать изображение."
