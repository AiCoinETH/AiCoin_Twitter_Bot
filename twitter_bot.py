import os
import sys
import asyncio
import hashlib
import logging
import random
import tempfile
from datetime import datetime, timedelta, time as dt_time

import tweepy
import requests

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, Update, Bot, InputMediaPhoto
)
from telegram.ext import (
    Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters
)
import aiosqlite
import telegram.error

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(funcName)s %(message)s'
)

# --- Переменные окружения и настройки ---
TELEGRAM_BOT_TOKEN_APPROVAL = os.getenv("TELEGRAM_BOT_TOKEN_APPROVAL")
TELEGRAM_APPROVAL_CHAT_ID   = int(os.getenv("TELEGRAM_APPROVAL_CHAT_ID"))
TELEGRAM_BOT_TOKEN_CHANNEL  = os.getenv("TELEGRAM_BOT_TOKEN_CHANNEL")
TELEGRAM_CHANNEL_USERNAME_ID = os.getenv("TELEGRAM_CHANNEL_USERNAME_ID")

TWITTER_API_KEY = os.getenv("TWITTER_API_KEY")
TWITTER_API_SECRET = os.getenv("TWITTER_API_SECRET")
TWITTER_ACCESS_TOKEN = os.getenv("TWITTER_ACCESS_TOKEN")
TWITTER_ACCESS_SECRET = os.getenv("TWITTER_ACCESS_SECRET")
TWITTER_BEARER_TOKEN = os.getenv("TWITTER_BEARER_TOKEN")

# Дополнительные параметры для публикаций и истории
ACTION_PAT_GITHUB = os.getenv("ACTION_PAT_GITHUB") or os.getenv("ACTION_PAT")
ACTION_REPO_GITHUB = os.getenv("ACTION_REPO_GITHUB") or os.getenv("ACTION_REPO")
ACTION_EVENT_GITHUB = os.getenv("ACTION_EVENT_GITHUB") or "manual"

POST_APPROVE_TIMEOUT_SEC = 300
POST_IMAGE_DIR = "./img"
TRENDING_LOG = "trending_log.csv"

# ================ СОСТОЯНИЯ =================
STATE_DEFAULT = "default"
STATE_CUSTOM = "custom"  # пользователь пишет свой пост (Сделай сам)
STATE_WAITING = "waiting"

user_states = {}      # user_id: state (default, custom и т.д.)
custom_drafts = {}    # user_id: {'text': ..., 'photo': ...}

approval_lock = asyncio.Lock()

# ================== КНОПКИ ==================
def build_main_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Опубликовать", callback_data="approve"),
            InlineKeyboardButton("♻️ Заново", callback_data="redo"),
            InlineKeyboardButton("🖼 Картинку", callback_data="picture"),
        ],
        [
            InlineKeyboardButton("💬 Поговорить", callback_data="talk"),
            InlineKeyboardButton("🛑 Отменить", callback_data="cancel"),
            InlineKeyboardButton("🕒 Подумать", callback_data="think"),
        ],
        [
            InlineKeyboardButton("✍️ Сделай сам", callback_data="custom"),
        ],
    ])

def build_custom_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Завершить генерацию", callback_data="custom_done")]
    ])
    # --- Вспомогательные функции для логов ---
def log_post(text, img_url=None):
    dt = datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(TRENDING_LOG, "a", encoding="utf-8") as f:
        f.write(f"{dt};{repr(text)};{img_url or ''}\n")

def clear_old_trending_log():
    try:
        if not os.path.exists(TRENDING_LOG):
            return
        lines = []
        threshold = datetime.now() - timedelta(days=15)
        with open(TRENDING_LOG, "r", encoding="utf-8") as f:
            for line in f:
                parts = line.split(";")
                if not parts:
                    continue
                try:
                    dt = datetime.strptime(parts[0], "%Y-%m-%d %H:%M")
                    if dt >= threshold:
                        lines.append(line)
                except Exception:
                    continue
        with open(TRENDING_LOG, "w", encoding="utf-8") as f:
            f.writelines(lines)
    except Exception as e:
        logging.error(f"Ошибка очистки trending_log: {e}")

# --- Основные хендлеры ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_states[user_id] = STATE_DEFAULT
    await update.message.reply_text(
        "👋 Привет! Готов генерировать посты. Выбери действие:",
        reply_markup=build_main_menu()
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = user_states.get(user_id, STATE_DEFAULT)
    if state == STATE_CUSTOM:
        text = update.message.text or ""
        photo_file_id = None
        if update.message.photo:
            photo_file_id = update.message.photo[-1].file_id
        if user_id not in custom_drafts:
            custom_drafts[user_id] = {"text": "", "photo": None}
        custom_drafts[user_id]["text"] = text.strip()
        custom_drafts[user_id]["photo"] = photo_file_id
        await update.message.reply_text(
            "Черновик сохранён! Для завершения нажми кнопку ниже.",
            reply_markup=build_custom_menu()
        )
        return
    # другие состояния (можно добавить свои обработки)

# --- Обработка колбеков (кнопки) ---
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    state = user_states.get(user_id, STATE_DEFAULT)
    data = query.data

    # --- "Сделай сам" ---
    if data == "custom":
        user_states[user_id] = STATE_CUSTOM
        custom_drafts[user_id] = {"text": "", "photo": None}
        await query.message.reply_text(
            "✍️ Введите ваш пост. Можно отправить одним сообщением текст и картинку (по желанию). Когда закончите — нажмите «Завершить генерацию».",
            reply_markup=build_custom_menu()
        )
        await query.answer()
        return

    # --- Завершение генерации своего поста ---
    if data == "custom_done":
        draft = custom_drafts.get(user_id, {"text": "", "photo": None})
        if not draft["text"]:
            await query.message.reply_text(
                "❗️Вы ничего не написали. Отправьте текст поста, затем нажмите «Завершить генерацию».",
                reply_markup=build_custom_menu()
            )
            await query.answer()
            return
        # Отправляем на согласование как обычный пост
        await send_for_approval(
            context=context,
            chat_id=user_id,
            text=draft["text"],
            photo_id=draft["photo"],
            is_custom=True
        )
        user_states[user_id] = STATE_WAITING
        await query.answer("Пост отправлен на согласование!")
        return

    # --- Подтверждение публикации ---
    if data == "approve":
        await approve_post_callback(update, context)
        await query.answer("Пост опубликован!")
        return

    # --- Заново ---
    if data == "redo":
        await query.message.reply_text(
            "♻️ Давайте заново. Выберите действие:",
            reply_markup=build_main_menu()
        )
        user_states[user_id] = STATE_DEFAULT
        await query.answer()
        return

    # --- Отмена ---
    if data == "cancel":
        await query.message.reply_text(
            "❌ Операция отменена.",
            reply_markup=build_main_menu()
        )
        user_states[user_id] = STATE_DEFAULT
        await query.answer()
        return

    # --- Подумать ---
    if data == "think":
        await query.message.reply_text(
            "🕒 Я подожду, когда вы будете готовы. Нажмите любую кнопку, чтобы продолжить.",
            reply_markup=build_custom_menu() if user_states.get(user_id) == STATE_CUSTOM else build_main_menu()
        )
        await query.answer()
        return

    # --- Картинку, Поговорить и др. (можно добавить свою обработку) ---

# --- Отправка поста на согласование ---
async def send_for_approval(context, chat_id, text, photo_id=None, is_custom=False):
    kb = [
        [InlineKeyboardButton("✅ Опубликовать", callback_data="approve"),
         InlineKeyboardButton("♻️ Заново", callback_data="redo")],
        [InlineKeyboardButton("🖼 Картинку", callback_data="picture"),
         InlineKeyboardButton("💬 Поговорить", callback_data="talk")],
        [InlineKeyboardButton("🕒 Подумать", callback_data="think"),
         InlineKeyboardButton("🛑 Отменить", callback_data="cancel")]
    ]
    reply_markup = InlineKeyboardMarkup(kb)
    caption = text[:1024]  # Telegram ограничение
    if photo_id:
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=photo_id,
            caption=caption,
            reply_markup=reply_markup
        )
    else:
        await context.bot.send_message(
            chat_id=chat_id,
            text=caption,
            reply_markup=reply_markup
        )
    # Запускаем автотаймер публикации (если нужно)
    context.job_queue.run_once(
        lambda ctx: asyncio.create_task(auto_approve_post(ctx, chat_id, text, photo_id)),
        POST_APPROVE_TIMEOUT_SEC,
        name=f"autopost_{chat_id}"
    )

async def auto_approve_post(context, chat_id, text, photo_id):
    await publish_post(context, chat_id, text, photo_id)
    await context.bot.send_message(chat_id, "⏱ Время вышло — пост опубликован автоматически.")
    await context.bot.send_message(chat_id, "🔙 Главное меню:", reply_markup=build_main_menu())
    user_states[chat_id] = STATE_DEFAULT

# --- Подтверждение публикации (Approve) ---
async def approve_post_callback(update, context):
    query = update.callback_query
    user_id = query.from_user.id
    message = query.message
    text = message.text or message.caption or ""
    photo_id = message.photo[-1].file_id if message.photo else None
    await publish_post(context, user_id, text, photo_id)
    await context.bot.send_message(user_id, "✅ Пост опубликован!", reply_markup=build_main_menu())
    user_states[user_id] = STATE_DEFAULT

# --- Публикация поста в канал/в Twitter ---
async def publish_post(context, user_id, text, photo_id=None):
    try:
        if photo_id:
            await context.bot.send_photo(
                chat_id=TELEGRAM_CHANNEL_USERNAME_ID,
                photo=photo_id,
                caption=text[:1024]
            )
        else:
            await context.bot.send_message(
                chat_id=TELEGRAM_CHANNEL_USERNAME_ID,
                text=text
            )
        log_post(text)
        # ТУТ ДОБАВЬ СВОЮ ПУБЛИКАЦИЮ В TWITTER (если нужно)
    except Exception as e:
        logging.error(f"Ошибка публикации: {e}")
        # --- КНОПКИ И МЕНЮ ---

def build_main_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Опубликовать", callback_data="approve"),
            InlineKeyboardButton("♻️ Заново", callback_data="redo"),
            InlineKeyboardButton("🖼 Картинку", callback_data="picture"),
        ],
        [
            InlineKeyboardButton("💬 Поговорить", callback_data="talk"),
            InlineKeyboardButton("🛑 Отменить", callback_data="cancel"),
            InlineKeyboardButton("🕒 Подумать", callback_data="think"),
        ],
        [
            InlineKeyboardButton("✍️ Сделай сам", callback_data="custom"),
        ],
    ])

def build_custom_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Завершить генерацию", callback_data="custom_done")],
        [InlineKeyboardButton("🛑 Отменить", callback_data="cancel")]
    ])

# --- СОСТОЯНИЯ и ХРАНИЛИЩЕ ---

STATE_DEFAULT = "default"
STATE_CUSTOM = "custom"
STATE_WAITING = "waiting"

user_states = {}      # user_id: state (default, custom и т.д.)
custom_drafts = {}    # user_id: {'text': ..., 'photo': ...}

# --- ЗАПУСК ПРИЛОЖЕНИЯ ---
def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN_APPROVAL).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.COMMAND, start))  # /start
    app.run_polling()

if __name__ == "__main__":
    main()