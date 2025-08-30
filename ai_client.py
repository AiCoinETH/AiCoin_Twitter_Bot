# ai_client.py
# -*- coding: utf-8 -*-
import os
import logging

log = logging.getLogger("ai_client")

_GEMINI_KEY = os.getenv("GEMINI_API_KEY", "").strip()
_USE_GEMINI = bool(_GEMINI_KEY)

def _try_import_gemini():
    if not _USE_GEMINI: 
        return None
    try:
        import google.generativeai as genai
        genai.configure(api_key=_GEMINI_KEY)
        return genai
    except Exception as e:
        log.warning("Gemini import/config failed: %s", e)
        return None

_GENAI = _try_import_gemini()

# ---------- Публичные функции ----------
def ai_generate_text(topic: str) -> tuple[str, str]:
    """
    Возвращает (text, warning). Если warning != "", генерация была в fallback-режиме.
    """
    topic = (topic or "").strip()
    if not topic:
        return "", "Пустая тема."

    if _GENAI:
        try:
            m = _GENAI.GenerativeModel("gemini-1.5-flash")
            prompt = (
                "Напиши краткий, цепкий пост на английском для Twitter/X на тему:\n"
                f"\"{topic}\"\n"
                "- без эмодзи в начале\n"
                "- одно-три коротких предложения\n"
                "- без хэштегов и ссылок\n"
                "- живой тон, конкретика, польза\n"
            )
            resp = m.generate_content(prompt)
            txt = (resp.text or "").strip()
            if not txt:
                raise RuntimeError("Gemini вернул пустой текст")
            return txt, ""
        except Exception as e:
            log.warning("Gemini text gen failed: %s", e)

    # ---- Fallback (без внешнего ИИ) ----
    stub = (
        f"{topic}. Quick take: Here’s why it matters — and how it helps real users. "
        "Actionable insight in one thread."
    )
    return stub, "⚠️ Gemini отключён или недоступен — использую шаблонный текст."

def ai_suggest_hashtags(text: str) -> list[str]:
    """
    Простой генератор хэштегов (локальный): безопасен, без внешних вызовов.
    """
    base = {"#AI", "#crypto", "$AI", "#AiCoin"}
    text_l = (text or "").lower()
    if "eth" in text_l or "ethereum" in text_l:
        base.add("#ETH")
    if "token" in text_l or "coin" in text_l:
        base.add("#altcoins")
    # коротко: 3-6 тегов
    out = list(base)[:6]
    return out