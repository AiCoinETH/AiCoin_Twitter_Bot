# twitter_bot.py
import os
import io
import re
import sys
import time
import json
import hashlib
import logging
import tempfile
import mimetypes
from dataclasses import dataclass
from typing import Optional, Tuple

import requests
from PIL import Image

import tweepy
from telegram import InlineKeyboardMarkup, InlineKeyboardButton, InputFile
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters

import ai_client  # наш модуль выше

# --- ЛОГГЕРЫ ---
log = logging.getLogger("twitter_bot")
logging.basicConfig(level=os.getenv("LOGLEVEL", "INFO"))

# --- ГЛОБАЛЬНОЕ СОСТОЯНИЕ ---
STATE = {
    "last_text": None,
    "last_image_url": None,
    "last_image_bytes": None,
}

# --- ТWITTER AUTH ---
def _twitter_client():
    api_key = os.getenv("TWITTER_API_KEY")
    api_secret = os.getenv("TWITTER_API_SECRET")
    access_token = os.getenv("TWITTER_ACCESS_TOKEN")
    access_secret = os.getenv("TWITTER_ACCESS_TOKEN_SECRET")
    auth = tweepy.OAuth1UserHandler(api_key, api_secret, access_token, access_secret)
    return tweepy.API(auth)

# --- УТИЛИТЫ ИЗОБРАЖЕНИЙ ---

SIGS = {
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"GIF87a": "image/gif",
    b"GIF89a": "image/gif",
    b"RIFF": "image/webp",  # с доп.проверкой "WEBP" на байтах 8:12
}

def _looks_like_image(b: bytes) -> bool:
    if not b or len(b) < 64:
        return False
    for sig, ctype in SIGS.items():
        if b.startswith(sig):
            return True
    if b.startswith(b"RIFF") and b[8:12] == b"WEBP":
        return True
    return False

def _pil_probe(b: bytes) -> Tuple[bool, Optional[str], Optional[Tuple[int,int]]]:
    try:
        with Image.open(io.BytesIO(b)) as im:
            im.verify()
        with Image.open(io.BytesIO(b)) as im2:
            return True, Image.MIME.get(im2.format), im2.size
    except Exception:
        return False, None, None

def robust_fetch_image(url: str, timeout: int = 20, max_tries: int = 3) -> Optional[bytes]:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; AiCoinBot/1.0)",
        "Accept": "image/*,application/octet-stream;q=0.9,*/*;q=0.8",
        "Cache-Control": "no-cache",
    }
    last_exc = None
    for i in range(max_tries):
        try:
            r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
            if r.status_code != 200:
                time.sleep(0.8)
                continue
            b = r.content or b""
            # Бывает, что raw.githubusercontent.com отдаёт text/plain (120 байт) до прогрева.
            if not _looks_like_image(b):
                ok, mime, sz = _pil_probe(b)
                if not ok:
                    time.sleep(0.8)
                    continue
            return b
        except Exception as e:
            last_exc = e
            time.sleep(0.8)
    log.warning("Failed to fetch image from url=%s err=%s", url, last_exc)
    return None

def save_temp_image(image_bytes: bytes, suffix: str = ".png") -> str:
    fd, path = tempfile.mkstemp(prefix="aicoin_", suffix=suffix)
    os.close(fd)
    with open(path, "wb") as f:
        f.write(image_bytes)
    return path

async def send_photo_safely(bot, chat_id: int, *, raw_bytes: Optional[bytes] = None, url: Optional[str] = None, caption: Optional[str] = None):
    """
    Сначала пытаемся использовать байты, если нет — пытаемся скачать URL, потом шлём файл.
    """
    b = raw_bytes
    if not b and url:
        b = robust_fetch_image(url)
    if not b:
        # фолбэк — текстом
        await bot.send_message(chat_id, text=f"{caption or ''}\n\n(image_fallback_local)".strip())
        return

    ok, mime, sz = _pil_probe(b)
    if not ok:
        # последняя попытка — дать PIL декодировать и пересохранить как PNG
        try:
            with Image.open(io.BytesIO(b)) as im:
                bio = io.BytesIO()
                im.convert("RGB").save(bio, format="PNG")
                b = bio.getvalue()
        except Exception:
            await bot.send_message(chat_id, text=f"{caption or ''}\n\n(image_fallback_local)".strip())
            return

    filename = "preview.png"
    await bot.send_photo(chat_id, photo=InputFile(io.BytesIO(b), filename), caption=caption)

# --- UI / TELEGRAM ---

APPROVAL_CHAT_ID = int(os.getenv("TELEGRAM_APPROVAL_CHAT_ID", "0"))
BOT_TOKEN_APPROVAL = os.getenv("TELEGRAM_BOT_TOKEN_APPROVAL")

def _kb_main():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("ПОСТ!", callback_data="post_both")],
            [InlineKeyboardButton("Пост в Twitter", callback_data="post_twitter"),
             InlineKeyboardButton("Пост в Telegram", callback_data="post_telegram")],
            [InlineKeyboardButton("✏️ Править текст", callback_data="ai_text_edit"),
             InlineKeyboardButton("🖼️ Изменить медиа", callback_data="ai_image_edit")],
        ]
    )

# -- Генерация текста
async def on_ai_generate_text(app, chat_id: int, topic: str):
    text = ai_client.generate_text_for_topic(topic)
    STATE["last_text"] = text
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Текст ок", callback_data="ai_text_ok"),
             InlineKeyboardButton("🔁 Ещё вариант", callback_data="ai_text_regen")],
            [InlineKeyboardButton("✏️ Править текст", callback_data="ai_text_edit")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="cancel_to_main")],
        ]
    )
    await app.bot.send_message(
        chat_id,
        text=f"ИИ сгенерировал текст\n\n{text}\n\nWebsite | Twitter X\n\nПодходит ли текст?",
        reply_markup=kb,
        disable_web_page_preview=False,
    )

# -- Генерация картинки
async def on_ai_generate_image(app, chat_id: int, topic: str):
    gen = ai_client.generate_image_for_topic(topic)
    STATE["last_image_url"] = gen.url
    STATE["last_image_bytes"] = gen.png_bytes
    await send_photo_safely(app.bot, chat_id, raw_bytes=gen.png_bytes, url=gen.url, caption=None)

# --- ПУБЛИКАЦИЯ ---
async def publish_to_twitter(text: str, image_url: Optional[str], image_bytes: Optional[bytes]) -> Tuple[bool, str]:
    try:
        api = _twitter_client()
        media_path = None
        b = image_bytes
        if not b and image_url:
            b = robust_fetch_image(image_url)
        if b:
            media_path = save_temp_image(b)
        if media_path:
            media = api.media_upload(media_path)
            api.update_status(status=text, media_ids=[media.media_id_string])
            os.remove(media_path)
        else:
            api.update_status(status=text)
        return True, "OK"
    except Exception as e:
        log.error("TW|publish failed: %s", e)
        return False, str(e)

async def publish_to_telegram(app, text: str, image_url: Optional[str], image_bytes: Optional[bytes]) -> Tuple[bool, str]:
    try:
        if image_url or image_bytes:
            await send_photo_safely(app.bot, APPROVAL_CHAT_ID, raw_bytes=image_bytes, url=image_url, caption=text)
        else:
            await app.bot.send_message(APPROVAL_CHAT_ID, text=text)
        return True, "OK"
    except Exception as e:
        log.error("TG|publish failed: %s", e)
        return False, str(e)

# --- ХЕНДЛЕРЫ КНОПОК (минимум для примера) ---

async def cb_ai_text_ok(update, context):
    await update.callback_query.answer()
    # спросим про картинку
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🖼 Сгенерировать изображение", callback_data="ai_img_gen")],
            [InlineKeyboardButton("📤 Загрузить свою картинку/видео", callback_data="ai_img_upload")],
            [InlineKeyboardButton("🚫 Без изображения", callback_data="ai_img_skip")],
            [InlineKeyboardButton("↩️ Назад к тексту", callback_data="ai_img_back_to_text")],
        ]
    )
    await context.bot.send_message(APPROVAL_CHAT_ID, "🖼 Нужна картинка к посту?", reply_markup=kb)

async def cb_ai_img_gen(update, context):
    await update.callback_query.answer()
    topic = STATE.get("last_text") or "AiCoin"
    await on_ai_generate_image(context.application, APPROVAL_CHAT_ID, topic)
    # показать превью с кнопками
    await context.bot.send_message(APPROVAL_CHAT_ID, "Предпросмотр (текст согласован; изображение сгенерировано)", reply_markup=_kb_main())

async def cb_post_twitter(update, context):
    await update.callback_query.answer()
    ok, msg = await publish_to_twitter(STATE.get("last_text") or "", STATE.get("last_image_url"), STATE.get("last_image_bytes"))
    if not ok:
        await context.bot.send_message(APPROVAL_CHAT_ID, "❌ Не удалось отправить в X (Twitter).")
        await context.bot.send_message(APPROVAL_CHAT_ID, "❌ X/Twitter: ошибка загрузки. Проверь права app (Read+Write) и соответствие медиа требованиям.",
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Старт воркера", url=os.getenv("TRIGGER_URL","https://example.com"))]]))

async def cb_post_telegram(update, context):
    await update.callback_query.answer()
    ok, msg = await publish_to_telegram(context.application, STATE.get("last_text") or "", STATE.get("last_image_url"), STATE.get("last_image_bytes"))
    if not ok:
        await context.bot.send_message(APPROVAL_CHAT_ID, f"❌ Ошибка публикации в Telegram: {msg}")
        await context.bot.send_message(APPROVAL_CHAT_ID, "❌ Не удалось отправить в Telegram.")

# --- БАЗОВЫЙ РОУТИНГ (минимальный) ---
async def start(update, context):
    await context.bot.send_message(APPROVAL_CHAT_ID, "Предпросмотр (ручной режим)\nWebsite | Twitter X\n\nХэштеги: —")

async def cb_ai_generate(update, context):
    await update.callback_query.answer()
    await context.bot.send_message(APPROVAL_CHAT_ID, "🧠 Введите тему поста (EN/RU/UA). Можно приложить картинку/видео или URL. У меня есть 5 минут.")

async def on_message(update, context):
    # считаем это темой
    topic = (update.message.text or "").strip()
    if not topic:
        return
    await on_ai_generate_text(context.application, APPROVAL_CHAT_ID, topic)

def main():
    token = BOT_TOKEN_APPROVAL or os.getenv("TELEGRAM_BOT_TOKEN_CHANNEL") or os.getenv("TELEGRAM_BOT_TOKEN_APPROVAL")
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(cb_ai_text_ok, pattern=r"^ai_text_ok$"))
    app.add_handler(CallbackQueryHandler(cb_ai_img_gen, pattern=r"^ai_img_gen$"))
    app.add_handler(CallbackQueryHandler(cb_post_twitter, pattern=r"^post_twitter$"))
    app.add_handler(CallbackQueryHandler(cb_post_telegram, pattern=r"^post_telegram$"))
    app.add_handler(CallbackQueryHandler(cb_ai_generate, pattern=r"^ai_generate$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    log.info("Planner module loaded")
    app.run_polling()

if __name__ == "__main__":
    main()