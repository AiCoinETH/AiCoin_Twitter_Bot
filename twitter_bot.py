import os
import asyncio
import hashlib
import logging
import random
from datetime import datetime, timedelta

import tweepy
import requests
import tempfile

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, Bot
from telegram.ext import Application, CallbackQueryHandler, ContextTypes
import aiosqlite
import telegram.error

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

# ========= ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ==========
TELEGRAM_BOT_TOKEN_APPROVAL = os.getenv("TELEGRAM_BOT_TOKEN_APPROVAL")
TELEGRAM_APPROVAL_CHAT_ID   = os.getenv("TELEGRAM_APPROVAL_CHAT_ID")
TELEGRAM_BOT_TOKEN_CHANNEL  = os.getenv("TELEGRAM_BOT_TOKEN_CHANNEL")
TELEGRAM_CHANNEL_USERNAME_ID = os.getenv("TELEGRAM_CHANNEL_USERNAME_ID")

# Twitter secrets
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
ACCESS_SECRET = os.getenv("ACCESS_SECRET")
BEARER_TOKEN = os.getenv("BEARER_TOKEN")

if not TELEGRAM_BOT_TOKEN_APPROVAL or not TELEGRAM_APPROVAL_CHAT_ID or not TELEGRAM_BOT_TOKEN_CHANNEL or not TELEGRAM_CHANNEL_USERNAME_ID:
    logging.error("Не заданы обязательные переменные окружения (BOT_TOKEN_APPROVAL, APPROVAL_CHAT_ID, BOT_TOKEN_CHANNEL или CHANNEL_USERNAME_ID)")
    exit(1)

approval_bot = Bot(token=TELEGRAM_BOT_TOKEN_APPROVAL)
channel_bot = Bot(token=TELEGRAM_BOT_TOKEN_CHANNEL)

test_images = [
    "https://upload.wikimedia.org/wikipedia/commons/4/47/PNG_transparency_demonstration_1.png",
    "https://upload.wikimedia.org/wikipedia/commons/3/3f/Fronalpstock_big.jpg",
    "https://upload.wikimedia.org/wikipedia/commons/1/17/Google-flutter-logo.png",
    "https://upload.wikimedia.org/wikipedia/commons/d/d6/Wp-w4-big.jpg"
]

post_data = {
    "text_ru":   "Майнинговые токены снова в фокусе...",
    "image_url": test_images[0],
    "timestamp": None,
    "post_id":   0
}
prev_data = post_data.copy()
post_data["text_en"] = "Mining tokens are back in focus. Example of a full English post for Telegram or short version for Twitter!"

pending_post         = {"active": False, "timer": None}
do_not_disturb       = {"active": False}
last_action_time     = {}
approval_message_ids = {"photo": None}
DB_FILE = "post_history.db"

# ========== КЛАВИАТУРЫ ==========
keyboard = InlineKeyboardMarkup([
    [InlineKeyboardButton("✅ Пост", callback_data="approve")],
    [InlineKeyboardButton("📝 Новый текст", callback_data="regenerate")],
    [InlineKeyboardButton("🖼️ Новая картинка", callback_data="new_image")],
    [InlineKeyboardButton("🆕 Пост целиком", callback_data="new_post")],
    [InlineKeyboardButton("💬 Поговорить", callback_data="chat"), InlineKeyboardButton("🌙 Не беспокоить", callback_data="do_not_disturb")],
    [InlineKeyboardButton("↩️ Вернуть предыдущий пост", callback_data="restore_previous"), InlineKeyboardButton("🔚 Завершить", callback_data="end_day")]
])

def post_choice_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Пост в Twitter", callback_data="post_twitter")],
        [InlineKeyboardButton("Пост в Telegram", callback_data="post_telegram")],
        [InlineKeyboardButton("ПОСТ!", callback_data="post_both")],
        [InlineKeyboardButton("Отмена", callback_data="cancel_to_main")]
    ])

def post_action_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Post EN", callback_data="post_en")],
        [InlineKeyboardButton("Отмена", callback_data="cancel_to_choice")]
    ])

# ========== TWITTER ==========
def get_api_v1():
    auth = tweepy.OAuth1UserHandler(API_KEY, API_SECRET, ACCESS_TOKEN, ACCESS_SECRET)
    return tweepy.API(auth)

def get_client_v2():
    return tweepy.Client(
        bearer_token=BEARER_TOKEN,
        consumer_key=API_KEY,
        consumer_secret=API_SECRET,
        access_token=ACCESS_TOKEN,
        access_token_secret=ACCESS_SECRET
    )

def publish_tweet_v2(text, image_url=None):
    client = get_client_v2()
    media_ids = None
    if image_url:
        api_v1 = get_api_v1()
        response = requests.get(image_url)
        response.raise_for_status()
        with tempfile.NamedTemporaryFile(delete=False, suffix='.png') as tmp:
            tmp.write(response.content)
            tmp_path = tmp.name
        try:
            media = api_v1.media_upload(tmp_path)
            media_ids = [media.media_id_string]
        finally:
            os.remove(tmp_path)
    client.create_tweet(text=text, media_ids=media_ids)
    logging.info("[TWITTER] Твит успешно опубликован!")

# ========== ТЕКСТ ==========
def build_twitter_post(text_en: str) -> str:
    signature = (
        "\nRead more on Telegram: t.me/AiCoin_ETH or on the website: https://getaicoin.com/ "
        "#AiCoin #Ai $Ai #crypto #blockchain #AI #DeFi"
    )
    max_length = 280
    reserve = max_length - len(signature)
    if len(text_en) > reserve:
        main_part = text_en[:reserve - 3].rstrip() + "..."
    else:
        main_part = text_en
    return main_part + signature

# ========== ОТПРАВКА НА МОДЕРАЦИЮ ==========
async def send_post_for_approval():
    post_data["timestamp"] = datetime.now()
    pending_post.update({"active": True, "timer": datetime.now()})
    try:
        photo_msg = await approval_bot.send_photo(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            photo=post_data["image_url"],
            caption=post_data["text_ru"],
            reply_markup=keyboard
        )
        approval_message_ids["photo"] = photo_msg.message_id
        logging.info("Пост отправлен на согласование.")
    except Exception as e:
        logging.error(f"Ошибка при отправке на согласование: {e}")

# ========== ОБРАБОТЧИК КНОПОК ==========
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    action = update.callback_query.data

    if action == "approve":
        twitter_text = build_twitter_post(post_data["text_en"])
        await approval_bot.send_photo(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            photo=post_data["image_url"],
            caption=twitter_text,
            reply_markup=post_choice_keyboard()
        )
        return

    if action == "post_twitter":
        twitter_text = build_twitter_post(post_data["text_en"])
        context.user_data["publish_mode"] = "twitter"
        await approval_bot.send_photo(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            photo=post_data["image_url"],
            caption=twitter_text,
            reply_markup=post_action_keyboard()
        )
        return

    if action == "post_en":
        mode = context.user_data.get("publish_mode", "twitter")
        if mode == "twitter":
            twitter_text = build_twitter_post(post_data["text_en"])
            try:
                publish_tweet_v2(twitter_text, post_data["image_url"])
                await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="✅ Успешно отправлено в Twitter!", reply_markup=keyboard)
            except Exception as e:
                await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"❌ Ошибка отправки в Twitter: {e}", reply_markup=keyboard)
        await send_post_for_approval()
        return

    # ... (сюда вставь остальной обработчик — генерация, Telegram и т.д.)

# ========== ЗАПУСК ==========
async def delayed_start(app: Application):
    await send_post_for_approval()

def main():
    logging.info("Старт Telegram бота модерации и публикации…")
    app = Application.builder()\
        .token(TELEGRAM_BOT_TOKEN_APPROVAL)\
        .post_init(delayed_start)\
        .build()
    app.add_handler(CallbackQueryHandler(button_handler))
    app.run_polling(poll_interval=0.12, timeout=1)

if __name__ == "__main__":
    main()
