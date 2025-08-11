# -*- coding: utf-8 -*-
"""
AiCoin Twitter & Telegram Bot
Оптимизированная версия с логированием, кнопками, публикацией, историей постов, GitHub, OpenAI.
"""

# --- ИМПОРТЫ ---
import os
import re
import asyncio
import hashlib
import logging
import random
import sys
import tempfile
import uuid
import base64
from datetime import datetime, timedelta, time as dt_time
from unicodedata import normalize
from zoneinfo import ZoneInfo

import tweepy
import requests
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    Bot,
    InputMediaPhoto
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters
)
import aiosqlite
from github import Github
from openai import OpenAI  # openai>=1.35.0

# --- ЛОГИРОВАНИЕ ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(funcName)s %(message)s"
)

# --- КОНФИГ ---
TELEGRAM_BOT_TOKEN_APPROVAL = os.getenv("TELEGRAM_BOT_TOKEN_APPROVAL")
TELEGRAM_APPROVAL_CHAT_ID = int(os.getenv("TELEGRAM_APPROVAL_CHAT_ID"))
TELEGRAM_BOT_TOKEN_CHANNEL = os.getenv("TELEGRAM_BOT_TOKEN_CHANNEL")
TELEGRAM_CHANNEL_USERNAME_ID = os.getenv("TELEGRAM_CHANNEL_USERNAME_ID")

TWITTER_API_KEY = os.getenv("TWITTER_API_KEY")
TWITTER_API_SECRET = os.getenv("TWITTER_API_SECRET")
TWITTER_ACCESS_TOKEN = os.getenv("TWITTER_ACCESS_TOKEN")
TWITTER_ACCESS_SECRET = os.getenv("TWITTER_ACCESS_SECRET")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

GITHUB_TOKEN = os.getenv("ACTION_PAT_GITHUB")
GITHUB_REPO = os.getenv("ACTION_REPO_GITHUB")
GITHUB_IMAGE_PATH = "images"

DISABLE_WEB_PREVIEW = True

# --- КЛИЕНТЫ ---
approval_bot = Bot(token=TELEGRAM_BOT_TOKEN_APPROVAL)
channel_bot = Bot(token=TELEGRAM_BOT_TOKEN_CHANNEL)

twitter_client = tweepy.Client(
    consumer_key=TWITTER_API_KEY,
    consumer_secret=TWITTER_API_SECRET,
    access_token=TWITTER_ACCESS_TOKEN,
    access_token_secret=TWITTER_ACCESS_SECRET
)

client_oa = OpenAI(api_key=OPENAI_API_KEY)

github_client = Github(GITHUB_TOKEN)
github_repo = github_client.get_repo(GITHUB_REPO)

# --- УТИЛИТЫ ---

async def send_photo_with_download(bot, chat_id, url_or_file_id, caption=None, reply_markup=None):
    """Отправка фото или сообщения с проверкой и подгрузкой"""
    def is_valid_image_url(url):
        try:
            resp = requests.head(url, timeout=5)
            ct = resp.headers.get('Content-Type', '')
            if not ct:
                return True
            return ct.startswith('image/')
        except Exception:
            return True

    try:
        if not str(url_or_file_id).startswith("http"):
            url = await process_telegram_photo(url_or_file_id, bot)
            msg = await bot.send_photo(
                chat_id=chat_id,
                photo=url,
                caption=caption,
                parse_mode="HTML",
                reply_markup=reply_markup
            )
            return msg, url.split('/')[-1]
        else:
            if not is_valid_image_url(url_or_file_id):
                pass
            try:
                response = requests.get(url_or_file_id, timeout=10)
                response.raise_for_status()
                tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
                tmp_file.write(response.content)
                tmp_file.close()
                with open(tmp_file.name, "rb") as img:
                    msg = await bot.send_photo(
                        chat_id=chat_id,
                        photo=img,
                        caption=caption,
                        parse_mode="HTML",
                        reply_markup=reply_markup
                    )
                os.remove(tmp_file.name)
                return msg, None
            except Exception:
                await bot.send_message(
                    chat_id=chat_id,
                    text=caption or "",
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                    disable_web_page_preview=DISABLE_WEB_PREVIEW
                )
                return None, None
    except Exception as e:
        logging.error(f"Ошибка в send_photo_with_download: {e}")
        await bot.send_message(
            chat_id=chat_id,
            text=caption or " ",
            parse_mode="HTML",
            reply_markup=reply_markup,
            disable_web_page_preview=DISABLE_WEB_PREVIEW
        )
        return None, None


def upload_image_to_github(image_path, filename):
    """Загрузка изображения в GitHub"""
    try:
        with open(image_path, "rb") as img_file:
            raw = img_file.read()
        b64 = base64.b64encode(raw).decode("utf-8")
        github_repo.create_file(
            f"{GITHUB_IMAGE_PATH}/{filename}",
            "upload image for post",
            b64,
            branch="main"
        )
        return f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{GITHUB_IMAGE_PATH}/{filename}"
    except Exception as e:
        logging.error(f"Ошибка загрузки файла на GitHub: {e}")
        return None


def delete_image_from_github(filename):
    """Удаление изображения с GitHub"""
    try:
        file_path = f"{GITHUB_IMAGE_PATH}/{filename}"
        contents = github_repo.get_contents(file_path, ref="main")
        github_repo.delete_file(
            contents.path,
            "delete image after posting",
            contents.sha,
            branch="main"
        )
    except Exception as e:
        logging.error(f"Ошибка удаления файла на GitHub: {e}")
# --- ГЕНЕРАЦИЯ ТЕКСТА ---

def _oa_chat_text(prompt: str) -> str:
    """Генерация текста через OpenAI Chat API"""
    try:
        resp = client_oa.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You write concise, inspiring social promos for a crypto+AI project called Ai Coin."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.9,
            max_tokens=220,
            timeout=10
        )
        return (resp.choices[0].message.content or "").strip().strip('"\n` ')
    except Exception as e:
        logging.warning(f"_oa_chat_text error: {e}")
        return "Ai Coin fuses AI with blockchain to turn community ideas into real actions."


# --- РАБОТА С ИСТОРИЕЙ ПОСТОВ ---

async def init_db():
    """Инициализация базы данных для хранения истории постов"""
    async with aiosqlite.connect("post_history.db") as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT,
                timestamp TEXT,
                image_hash TEXT
            )
        """)
        await db.commit()
    logging.info("База данных инициализирована.")


async def is_duplicate_post(text, image_url=None):
    """Проверка, публиковался ли пост ранее"""
    image_hash = None
    if image_url:
        try:
            r = requests.get(image_url, timeout=3)
            r.raise_for_status()
            image_hash = hashlib.sha256(r.content).hexdigest()
        except Exception:
            pass
    async with aiosqlite.connect("post_history.db") as db:
        async with db.execute(
            "SELECT 1 FROM posts WHERE text=? AND (image_hash=? OR ? IS NULL)",
            (text, image_hash, image_hash)
        ) as cursor:
            return await cursor.fetchone() is not None


async def save_post_to_history(text, image_url=None):
    """Сохранение поста в историю"""
    image_hash = None
    if image_url:
        try:
            r = requests.get(image_url, timeout=3)
            r.raise_for_status()
            image_hash = hashlib.sha256(r.content).hexdigest()
        except Exception:
            logging.warning("Не удалось получить хеш изображения для истории.")
    async with aiosqlite.connect("post_history.db") as db:
        await db.execute(
            "INSERT INTO posts (text, timestamp, image_hash) VALUES (?, ?, ?)",
            (text, datetime.now().isoformat(), image_hash)
        )
        await db.commit()
    logging.info("Пост сохранён в историю.")


# --- ПУБЛИКАЦИЯ В TWITTER ---

def publish_post_to_twitter(text, image_url=None):
    """Публикация поста в Twitter"""
    try:
        if image_url:
            media = twitter_client.media_upload(filename=image_url)
            twitter_client.create_tweet(text=text, media_ids=[media.media_id])
        else:
            twitter_client.create_tweet(text=text)
        logging.info("Пост опубликован в Twitter.")
        return True
    except Exception as e:
        logging.error(f"Ошибка публикации в Twitter: {e}")
        return False


# --- ПУБЛИКАЦИЯ В TELEGRAM ---

async def publish_post_to_telegram(text, image_url=None):
    """Публикация поста в Telegram"""
    try:
        if image_url:
            await channel_bot.send_photo(chat_id=TELEGRAM_CHANNEL_USERNAME_ID, photo=image_url, caption=text)
        else:
            await channel_bot.send_message(chat_id=TELEGRAM_CHANNEL_USERNAME_ID, text=text)
        logging.info("Пост опубликован в Telegram.")
        return True
    except Exception as e:
        logging.error(f"Ошибка публикации в Telegram: {e}")
        return False


# --- ОСНОВНОЙ ПОТОК ПУБЛИКАЦИИ ---

async def publish_flow(text, img=None, publish_tg=True, publish_tw=True):
    """Общий поток публикации с проверкой дублей и сохранением истории"""
    tg_status = None
    tw_status = None

    if publish_tg:
        if await is_duplicate_post(text, img):
            await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="⚠️ Дубликат для Telegram.")
            tg_status = False
        else:
            tg_status = await publish_post_to_telegram(text, img)
            if tg_status:
                await save_post_to_history(text, img)

    if publish_tw:
        if await is_duplicate_post(text, img):
            await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="⚠️ Дубликат для Twitter.")
            tw_status = False
        else:
            tw_status = publish_post_to_twitter(text, img)
            if tw_status:
                await save_post_to_history(text, img)

    if publish_tg:
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=("✅ Telegram" if tg_status else "❌ Telegram"))
    if publish_tw:
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=("✅ Twitter" if tw_status else "❌ Twitter"))
# --- КНОПКИ СОГЛАСОВАНИЯ ---

def approval_keyboard():
    """Набор кнопок для согласования поста"""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Опубликовать", callback_data="approve_post"),
            InlineKeyboardButton("♻️ Заново", callback_data="regenerate_post")
        ],
        [
            InlineKeyboardButton("🖼 Картинку", callback_data="regenerate_image"),
            InlineKeyboardButton("💬 Поговорить", callback_data="chat_with_ai")
        ],
        [
            InlineKeyboardButton("🕒 Подумать", callback_data="delay_post"),
            InlineKeyboardButton("🛑 Отменить", callback_data="cancel_post")
        ]
    ])


# --- ОБРАБОТЧИКИ КНОПОК ---

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data

    if action == "approve_post":
        await publish_flow(context.user_data.get("post_text"), context.user_data.get("post_image"))
        await query.edit_message_caption(caption="✅ Пост опубликован!")
    elif action == "regenerate_post":
        new_text = _oa_chat_text("Generate a new crypto+AI promotional tweet.")
        context.user_data["post_text"] = new_text
        await query.edit_message_caption(caption=new_text, reply_markup=approval_keyboard())
    elif action == "regenerate_image":
        await query.edit_message_caption(caption="🖼 Новая картинка (заглушка)", reply_markup=approval_keyboard())
    elif action == "chat_with_ai":
        await query.edit_message_caption(caption="💬 Чат с AI пока в разработке.", reply_markup=approval_keyboard())
    elif action == "delay_post":
        await query.edit_message_caption(caption="🕒 Пост отложен.", reply_markup=approval_keyboard())
    elif action == "cancel_post":
        await query.edit_message_caption(caption="❌ Пост отменён.")


# --- ОТПРАВКА ПОСТА НА СОГЛАСОВАНИЕ ---

async def send_for_approval(text, image_url=None):
    """Отправка поста в Telegram на согласование"""
    await approval_bot.send_message(
        chat_id=TELEGRAM_APPROVAL_CHAT_ID,
        text=text,
        reply_markup=approval_keyboard()
    )


# --- MAIN ---

async def main():
    """Запуск бота"""
    await init_db()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN_APPROVAL).build()
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    # Пример генерации и отправки поста на согласование
    text = _oa_chat_text("Generate a crypto+AI promotional tweet for Ai Coin.")
    await send_for_approval(text)

    await app.run_polling()


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка входящих текстов в чате"""
    text = update.message.text
    await update.message.reply_text(f"Вы написали: {text}", reply_markup=approval_keyboard())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен вручную.")
