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
    Ограничение длины поста до 777 символов. Допускаются кештеги с символом $.
    """
    topic = (topic or "").strip()
    if not topic:
        return "", "Пустая тема."

    # --- Попытка через Gemini ---
    if _GENAI:
        try:
            m = _GENAI.GenerativeModel("gemini-1.5-flash")
            prompt = (
                "Write a concise, engaging post for Twitter/X in English about:\n"
                f"\"{topic}\"\n"
                "- Max 777 characters including spaces.\n" # Новое ограничение
                "- Can include relevant '$' prefixed crypto hashtags (e.g., $ETH, $BTC).\n" # Разрешены кештеги с $
                "- Avoid emojis at the beginning of the post.\n"
                "- Aim for a clear, informative, and slightly punchy tone.\n"
                "- No external links.\n"
            )
            resp = m.generate_content(prompt)
            txt = (getattr(resp, "text", "") or "").strip()

            # Дополнительная обрезка на случай, если Gemini превысит лимит
            if len(txt) > 777:
                txt = txt[:774] + "..." # Обрезаем, оставляя место для многоточия

            if not txt:
                raise RuntimeError("Gemini returned empty text")
            return txt, ""
        except Exception as e:
            log.warning("Gemini text gen failed: %s", e)

    # --- Локальный фолбэк (без внешнего ИИ) ---
    stub = (
        f"{topic}. Quick take: why it matters and how it helps real users. "
        "Actionable insight in a nutshell. $AI $crypto"
    )
    if len(stub) > 777:
        stub = stub[:774] + "..."
    return stub, "⚠️ Gemini отключён или недоступен — использую шаблонный текст."

def ai_suggest_hashtags(text: str) -> list[str]:
    """
    Простой локальный генератор хэштегов, теперь с поддержкой $.
    """
    base = {"#AI", "#crypto", "$AI", "$AiCoin"} # Изменил #AiCoin на $AiCoin
    tl = (text or "").lower()
    if "eth" in tl or "ethereum" in tl:
        base.add("$ETH") # Изменил #ETH на $ETH
    if "token" in tl or "coin" in tl:
        base.add("$altcoins") # Изменил #altcoins на $altcoins
    if "defi" in tl:
        base.add("$DeFi") # Изменил #DeFi на $DeFi
    if "nft
