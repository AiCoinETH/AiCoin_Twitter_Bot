import os
import asyncio
import hashlib
import logging
import random
import re
from datetime import datetime, timedelta
from pytz import timezone

import requests
import tempfile

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, Bot
from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters, ContextTypes
import aiosqlite
import telegram.error
import tweepy

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

# === TELEGRAM CONFIG ===
TELEGRAM_BOT_TOKEN_APPROVAL = os.getenv("TELEGRAM_BOT_TOKEN_APPROVAL")
TELEGRAM_APPROVAL_CHAT_ID   = os.getenv("TELEGRAM_APPROVAL_CHAT_ID")
TELEGRAM_BOT_TOKEN_CHANNEL  = os.getenv("TELEGRAM_BOT_TOKEN_CHANNEL")
TELEGRAM_CHANNEL_USERNAME_ID = os.getenv("TELEGRAM_CHANNEL_USERNAME_ID")

approval_bot = Bot(token=TELEGRAM_BOT_TOKEN_APPROVAL)
channel_bot = Bot(token=TELEGRAM_BOT_TOKEN_CHANNEL)
KIEV_TZ = timezone('Europe/Kyiv')

# === TWITTER CONFIG ===
TWITTER_API_KEY = os.getenv("API_KEY")
TWITTER_API_SECRET = os.getenv("API_SECRET")
TWITTER_ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
TWITTER_ACCESS_SECRET = os.getenv("ACCESS_SECRET")

def get_twitter_client():
    auth = tweepy.OAuth1UserHandler(
        TWITTER_API_KEY, TWITTER_API_SECRET,
        TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_SECRET
    )
    return tweepy.API(auth)

# === PINATA CONFIG ===
PINATA_JWT = os.getenv("PINATA_JWT")

def download_image(url):
    resp = requests.get(url)
    resp.raise_for_status()
    tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.png')
    tmp_file.write(resp.content)
    tmp_file.close()
    return tmp_file.name

def upload_to_pinata(image_path):
    url = "https://api.pinata.cloud/pinning/pinFileToIPFS"
    headers = {"Authorization": f"Bearer {PINATA_JWT}"}
    with open(image_path, "rb") as file:
        files = {'file': file}
        response = requests.post(url, files=files, headers=headers)
        response.raise_for_status()
        cid = response.json()["IpfsHash"]
        return f"https://gateway.pinata.cloud/ipfs/{cid}"

def publish_tweet_with_pinata(text, image_url):
    img_path = download_image(image_url)
    ipfs_url = upload_to_pinata(img_path)
    api = get_twitter_client()
    media = api.media_upload(img_path)
    tweet_text = text + f"\nIPFS: {ipfs_url}"
    api.update_status(status=tweet_text, media_ids=[media.media_id])
    os.remove(img_path)
    return ipfs_url

# === TEST DATA ===
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
    "post_id":   0,
    "text_en": "Mining tokens are back in focus. Example of a full English post for Telegram or short version for Twitter!"
}
prev_data = post_data.copy()
post_history = []

pending_post         = {"active": False, "timer": None}
do_not_disturb       = {"active": False, "until": None, "reason": None}
last_action_time     = {}
approval_message_ids = {"photo": None}
user_generating      = {}
DB_FILE = "post_history.db"

# ========== UI ==========

def build_keyboard(show_back):
    kb = [
        [InlineKeyboardButton("✅ Пост", callback_data="approve")],
        [InlineKeyboardButton("📝 Новый текст", callback_data="regenerate")],
        [InlineKeyboardButton("🖼️ Новая картинка", callback_data="new_image")],
        [InlineKeyboardButton("🆕 Пост целиком", callback_data="new_post")],
        [InlineKeyboardButton("💬 Поговорить", callback_data="chat"), InlineKeyboardButton("🌙 Не беспокоить", callback_data="do_not_disturb")]
    ]
    if show_back:
        kb.append([InlineKeyboardButton("↩️ Вернуть предыдущий пост", callback_data="restore_previous")])
    kb.append([InlineKeyboardButton("🔚 Завершить", callback_data="end_day")])
    return InlineKeyboardMarkup(kb)

def moderation_off_keyboard(reason):
    if reason == "auto":
        return InlineKeyboardMarkup([[InlineKeyboardButton("Включить согласование", callback_data="enable_moderation")]])
    elif reason == "no_publication":
        return InlineKeyboardMarkup([[InlineKeyboardButton("Возобновить публикации", callback_data="enable_moderation")]])

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

# ========== АНТИ-ДУБЛИКАТ ==========
def clean_text(text):
    return re.sub(r'\W+', '', text.lower()).strip()

def text_hash(text):
    cleaned = clean_text(text)
    return hashlib.sha256(cleaned.encode('utf-8')).hexdigest()

async def is_duplicate_text(text):
    hash_text_val = text_hash(text)
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT 1 FROM posts WHERE text_hash = ? LIMIT 1", (hash_text_val,)) as cursor:
            row = await cursor.fetchone()
            return row is not None

async def save_post_to_history(text, image_url=None):
    hash_text_val = text_hash(text)
    image_hash = get_image_hash(image_url) if image_url else None
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO posts (text, text_hash, timestamp, image_hash) VALUES (?, ?, ?, ?)",
            (text, hash_text_val, datetime.now().isoformat(), image_hash)
        )
        await db.commit()

def get_image_hash(url: str) -> str | None:
    try:
        r = requests.get(url, timeout=3)
        r.raise_for_status()
        return hashlib.sha256(r.content).hexdigest()
    except Exception as e:
        logging.warning(f"Не удалось получить хеш изображения: {e}")
        return None

# ========== ГЕНЕРАЦИИ ==========
async def ai_generate_text():
    await asyncio.sleep(0.6)
    return f"✨ [AI] Новый сгенерированный текст поста. #{random.randint(1,9999)}"

async def ai_generate_image():
    await asyncio.sleep(0.4)
    return random.choice(test_images)

async def ai_generate_full():
    return await ai_generate_text(), await ai_generate_image()

async def generate_unique_text(max_attempts=10):
    attempts = 0
    while attempts < max_attempts:
        new_text = await ai_generate_text()
        if not await is_duplicate_text(new_text):
            return new_text
        attempts += 1
    raise Exception("Не удалось сгенерировать уникальный текст за 10 попыток!")

async def generate_unique_full(max_attempts=10):
    attempts = 0
    while attempts < max_attempts:
        new_text, new_image = await ai_generate_full()
        if not await is_duplicate_text(new_text):
            return new_text, new_image
        attempts += 1
    raise Exception("Не удалось сгенерировать уникальный пост за 10 попыток!")

# ========== TWITTER/TG ПОДПИСЬ ==========
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

# ========== ИНИЦИАЛИЗАЦИЯ БД ==========
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                text_hash TEXT,
                timestamp TEXT NOT NULL,
                image_hash TEXT
            )
            """
        )
        await db.commit()

# ========== РЕЖИМЫ ==========
def is_do_not_disturb_active():
    now = datetime.now(KIEV_TZ)
    if do_not_disturb["active"] and do_not_disturb["until"] and now < do_not_disturb["until"]:
        return True
    if do_not_disturb["active"]:
        do_not_disturb.update({"active": False, "until": None, "reason": None})  # Автоотключение
    return False

# ========== ОТПРАВКА НА МОДЕРАЦИЮ ==========
async def send_post_for_approval(show_back=None):
    post_data["timestamp"] = datetime.now()
    pending_post.update({"active": True, "timer": datetime.now()})
    if is_do_not_disturb_active():
        if do_not_disturb["reason"] == "auto":
            await auto_publish_everywhere(post_data)
            await approval_bot.send_message(
                chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                text="✅ Опубликовано автоматически (режим 'Не беспокоить')"
            )
        elif do_not_disturb["reason"] == "no_publication":
            await approval_bot.send_message(
                chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                text="🚫 Сегодня публикаций не будет (режим 'Завершить')."
            )
        pending_post["active"] = False
        pending_post["timer"] = None
        return

    if show_back is None:
        show_back = bool(post_history)
    try:
        photo_msg = await approval_bot.send_photo(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            photo=post_data["image_url"],
            caption=post_data["text_ru"],
            reply_markup=build_keyboard(show_back)
        )
        approval_message_ids["photo"] = photo_msg.message_id
    except Exception as e:
        logging.error(f"Ошибка при отправке на согласование: {e}")

# ========== ПУБЛИКАЦИЯ В TG, TWITTER, PINATA ==========
async def auto_publish_everywhere(post_data):
    # Telegram
    await channel_bot.send_photo(
        chat_id=TELEGRAM_CHANNEL_USERNAME_ID,
        photo=post_data["image_url"],
        caption=post_data["text_en"] + "\n\n🌐 https://getaicoin.com/"
    )
    # Twitter + Pinata
    tweet_text = build_twitter_post(post_data["text_en"])
    ipfs_url = publish_tweet_with_pinata(tweet_text, post_data["image_url"])
    logging.info(f"[TWITTER] Опубликовано: {tweet_text}\nIPFS: {ipfs_url}")

# ========== ТАЙМЕР ==========

async def check_timer():
    while True:
        await asyncio.sleep(5)
        if do_not_disturb["active"] and do_not_disturb["until"]:
            now = datetime.now(KIEV_TZ)
            if now > do_not_disturb["until"]:
                do_not_disturb.update({"active": False, "until": None, "reason": None})
                await approval_bot.send_message(
                    chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                    text="Режим дня завершён. Согласование снова включено."
                )
        if pending_post["active"] and pending_post.get("timer") and (datetime.now() - pending_post["timer"]) > timedelta(minutes=15):
            try:
                await approval_bot.send_message(
                    chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                    text="⌛ Время ожидания истекло (15 минут). Публикую автоматически."
                )
            except Exception:
                pass
            await auto_publish_everywhere(post_data)
            pending_post["active"] = False
            pending_post["timer"] = None

# ========== ОБРАБОТЧИК КНОПОК ==========
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_action_time, prev_data
    await update.callback_query.answer()
    user_id = update.effective_user.id

    if user_generating.get(user_id, False):
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="⏳ Идёт генерация. Пожалуйста, дождитесь завершения предыдущей операции."
        )
        return

    now = datetime.now()
    if user_id in last_action_time and (now - last_action_time[user_id]).seconds < 1:
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="⏳ Не нажимайте слишком часто!"
        )
        return

    last_action_time[user_id] = now
    action = update.callback_query.data
    prev_data.update(post_data)
    pending_post["active"] = False
    pending_post["timer"] = None

    if is_do_not_disturb_active():
        if do_not_disturb["reason"] == "auto":
            await auto_publish_everywhere(post_data)
            await approval_bot.send_message(
                chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                text="✅ Опубликовано автоматически (режим 'Не беспокоить')"
            )
        elif do_not_disturb["reason"] == "no_publication":
            await approval_bot.send_message(
                chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                text="🚫 Сегодня публикаций не будет (режим 'Завершить')."
            )
        return

    if action == 'approve':
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
    if action == "post_telegram":
        context.user_data["publish_mode"] = "telegram"
        await approval_bot.send_photo(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            photo=post_data["image_url"],
            caption=post_data["text_en"] + "\n\n🌐 https://getaicoin.com/",
            reply_markup=post_action_keyboard()
        )
        return
    if action == "post_both":
        twitter_text = build_twitter_post(post_data["text_en"])
        context.user_data["publish_mode"] = "both"
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
            ipfs_url = publish_tweet_with_pinata(post_data["text_en"], post_data["image_url"])
            await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"✅ Успешно отправлено в Twitter!\nIPFS: {ipfs_url}")
            await asyncio.sleep(1.5)
            await send_post_for_approval(show_back=bool(post_history))
        elif mode == "telegram":
            await channel_bot.send_photo(
                chat_id=TELEGRAM_CHANNEL_USERNAME_ID,
                photo=post_data["image_url"],
                caption=post_data["text_en"] + "\n\n🌐 https://getaicoin.com/"
            )
            await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="✅ Успешно отправлено в Telegram!")
            await asyncio.sleep(1.5)
            await send_post_for_approval(show_back=bool(post_history))
        elif mode == "both":
            ipfs_url = publish_tweet_with_pinata(post_data["text_en"], post_data["image_url"])
            await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"✅ Успешно отправлено в Twitter!\nIPFS: {ipfs_url}")
            await channel_bot.send_photo(
                chat_id=TELEGRAM_CHANNEL_USERNAME_ID,
                photo=post_data["image_url"],
                caption=post_data["text_en"] + "\n\n🌐 https://getaicoin.com/"
            )
            await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="✅ Успешно отправлено в Telegram!")
            await asyncio.sleep(2)
            await send_post_for_approval(show_back=bool(post_history))
        return
    if action == "cancel_to_main":
        await send_post_for_approval(show_back=bool(post_history))
        return
    if action == "cancel_to_choice":
        twitter_text = build_twitter_post(post_data["text_en"])
        await approval_bot.send_photo(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            photo=post_data["image_url"],
            caption=twitter_text,
            reply_markup=post_choice_keyboard()
        )
        return

    if action == 'regenerate':
        user_generating[user_id] = True
        post_history.append(post_data.copy())
        try:
            post_data["text_ru"] = await generate_unique_text()
            await send_post_for_approval(show_back=True)
        except Exception as e:
            await approval_bot.send_message(
                chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                text=f"⚠️ Не удалось сгенерировать уникальный текст (возможно проблема с генерацией или все варианты уже были). Попробуйте позже или перезапустите бота.\nОшибка: {e}"
            )
        user_generating[user_id] = False
        return

    if action == 'new_post':
        user_generating[user_id] = True
        post_history.append(post_data.copy())
        try:
            post_data["text_ru"], post_data["image_url"] = await generate_unique_full()
            await send_post_for_approval(show_back=True)
        except Exception as e:
            await approval_bot.send_message(
                chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                text=f"⚠️ Не удалось сгенерировать уникальный пост (возможно проблема с генерацией или все варианты уже были). Попробуйте позже или перезапустите бота.\nОшибка: {e}"
            )
        user_generating[user_id] = False
        return

    if action == 'new_image':
        user_generating[user_id] = True
        post_history.append(post_data.copy())
        try:
            post_data["image_url"] = await ai_generate_image()
            await send_post_for_approval(show_back=True)
        except Exception as e:
            await approval_bot.send_message(
                chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                text=f"⚠️ Не удалось сгенерировать картинку: {e}\nПопробуйте позже или перезапустите бота."
            )
        user_generating[user_id] = False
        return

    if action == "restore_previous" and post_history:
        post_data.update(post_history.pop())
        await send_post_for_approval(show_back=bool(post_history))
        return

    if action == "chat":
        context.user_data["chat_mode"] = True
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="💬 Обсудим публикацию! Напишите свой вопрос или предложение."
        )
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text=post_data["text_ru"]
        )
        return

    if action == "do_not_disturb":
        now = datetime.now(KIEV_TZ)
        end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=0)
        do_not_disturb.update({"active": True, "until": end_of_day, "reason": "auto"})
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="🌙 Сегодня не беспокоить. Всё публикуется автоматически.",
            reply_markup=moderation_off_keyboard("auto")
        )
        return

    if action == "end_day":
        now = datetime.now(KIEV_TZ)
        end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=0)
        do_not_disturb.update({"active": True, "until": end_of_day, "reason": "no_publication"})
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="🔚 Сегодня публикаций не будет.",
            reply_markup=moderation_off_keyboard("no_publication")
        )
        return

    if action == "enable_moderation":
        do_not_disturb.update({"active": False, "until": None, "reason": None})
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="Согласование и публикации снова включены.",
            reply_markup=build_keyboard(show_back=bool(post_history))
        )
        return

# ========== ЧАТ-МОД ==========
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("chat_mode"):
        user_text = update.message.text
        answer = f"🤖 [AI] Ответ на: {user_text}\n(Тут будет генерация от ИИ)"
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=answer)
        if user_text.strip().lower() in ['завершить', 'end', 'стоп', 'готово']:
            context.user_data["chat_mode"] = False
            post_history.append(post_data.copy())
            post_data["text_ru"] = f"📝 [AI Chat] Итоговый пост: {user_text}"
            await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="✅ Беседа завершена. Новый пост создан!")
            await send_post_for_approval(show_back=True)

# ========== ЗАПУСК ==========
async def delayed_start(app: Application):
    await init_db()
    await send_post_for_approval(show_back=False)
    asyncio.create_task(check_timer())

def main():
    app = Application.builder()\
        .token(TELEGRAM_BOT_TOKEN_APPROVAL)\
        .post_init(delayed_start)\
        .build()
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), message_handler))
    app.run_polling(poll_interval=0.12, timeout=1)

if __name__ == "__main__":
    main()
