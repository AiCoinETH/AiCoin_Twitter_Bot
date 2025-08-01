import os
import asyncio
import hashlib
import logging
import random
import sys
from datetime import datetime, timedelta, time as dt_time
import tweepy
import requests
import tempfile

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, Bot
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters
import aiosqlite
import telegram.error

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

approval_lock = asyncio.Lock()

TELEGRAM_BOT_TOKEN_APPROVAL = os.getenv("TELEGRAM_BOT_TOKEN_APPROVAL")
TELEGRAM_APPROVAL_CHAT_ID   = os.getenv("TELEGRAM_APPROVAL_CHAT_ID")
TELEGRAM_BOT_TOKEN_CHANNEL  = os.getenv("TELEGRAM_BOT_TOKEN_CHANNEL")
TELEGRAM_CHANNEL_USERNAME_ID = os.getenv("TELEGRAM_CHANNEL_USERNAME_ID")

ACTION_PAT_GITHUB = os.getenv("ACTION_PAT_GITHUB") or os.getenv("ACTION_PAT")
ACTION_REPO_GITHUB = os.getenv("ACTION_REPO_GITHUB") or os.getenv("ACTION_REPO")
ACTION_EVENT_GITHUB = os.getenv("ACTION_EVENT_GITHUB") or os.getenv("ACTION_EVENT") or "telegram-bot-restart"

if not TELEGRAM_BOT_TOKEN_APPROVAL or not TELEGRAM_APPROVAL_CHAT_ID or not TELEGRAM_BOT_TOKEN_CHANNEL or not TELEGRAM_CHANNEL_USERNAME_ID:
    logging.error("Не заданы обязательные переменные окружения (BOT_TOKEN_APPROVAL, APPROVAL_CHAT_ID, BOT_TOKEN_CHANNEL или CHANNEL_USERNAME_ID)")
    exit(1)

TWITTER_API_KEY             = os.getenv("TWITTER_API_KEY")
TWITTER_API_SECRET          = os.getenv("TWITTER_API_SECRET")
TWITTER_ACCESS_TOKEN        = os.getenv("TWITTER_ACCESS_TOKEN")
TWITTER_ACCESS_TOKEN_SECRET = os.getenv("TWITTER_ACCESS_TOKEN_SECRET")

if not all([
    TWITTER_API_KEY,
    TWITTER_API_SECRET,
    TWITTER_ACCESS_TOKEN,
    TWITTER_ACCESS_TOKEN_SECRET
]):
    logging.error("Не заданы обязательные переменные окружения для Twitter!")
    exit(1)

def get_twitter_clients():
    client_v2 = tweepy.Client(
        consumer_key=TWITTER_API_KEY,
        consumer_secret=TWITTER_API_SECRET,
        access_token=TWITTER_ACCESS_TOKEN,
        access_token_secret=TWITTER_ACCESS_TOKEN_SECRET
    )
    api_v1 = tweepy.API(
        tweepy.OAuth1UserHandler(
            TWITTER_API_KEY,
            TWITTER_API_SECRET,
            TWITTER_ACCESS_TOKEN,
            TWITTER_ACCESS_TOKEN_SECRET
        )
    )
    return client_v2, api_v1

twitter_client_v2, twitter_api_v1 = get_twitter_clients()

approval_bot = Bot(token=TELEGRAM_BOT_TOKEN_APPROVAL)
channel_bot = Bot(token=TELEGRAM_BOT_TOKEN_CHANNEL)

FSM = {
    "SLEEP": "sleep_today",
    "AUTO": "auto_mode",
    "MANUAL": "manual_mode"
}
fsm_state = {"current": FSM["MANUAL"]}

test_images = [
    "https://upload.wikimedia.org/wikipedia/commons/4/47/PNG_transparency_demonstration_1.png",
    "https://upload.wikimedia.org/wikipedia/commons/3/3f/Fronalpstock_big.jpg",
    "https://upload.wikimedia.org/wikipedia/commons/1/17/Google-flutter-logo.png",
    "https://upload.wikimedia.org/wikipedia/commons/d/d6/Wp-w4-big.jpg"
]

WELCOME_POST_EN = (
    "🚀 Welcome to the publication bot!\n\n"
    "AI content, news, ideas, image generation and more."
)
WELCOME_HASHTAGS = "#AiCoin #AI #crypto #trends #бот #новости"

post_data = {
    "text_en":   WELCOME_POST_EN,
    "image_url": test_images[0],
    "timestamp": None,
    "post_id":   0
}
prev_data = post_data.copy()
user_self_post = {}

TIMER_PUBLISH_DEFAULT = 180
TIMER_PUBLISH_EXTEND  = 900

pending_post         = {"active": False, "timer": None, "timeout": TIMER_PUBLISH_DEFAULT}
do_not_disturb       = {"active": False}
last_action_time     = {}
approval_message_ids = {"photo": None}
DB_FILE = "post_history.db"

scheduled_posts_per_day = 6
manual_posts_today = 0

def reset_timer(timeout=None):
    pending_post["timer"] = datetime.now()
    if timeout:
        pending_post["timeout"] = timeout

def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Пост", callback_data="approve")],
        [InlineKeyboardButton("✍️ Сделай сам", callback_data="self_post")],
        [InlineKeyboardButton("🕒 Подумать", callback_data="think")],
        [InlineKeyboardButton("🆕 Новый пост", callback_data="new_post")],
        [InlineKeyboardButton("💬 Поговорить", callback_data="chat"), InlineKeyboardButton("🌙 Не беспокоить", callback_data="do_not_disturb")],
        [InlineKeyboardButton("↩️ Вернуть предыдущий пост", callback_data="restore_previous"), InlineKeyboardButton("🔚 Завершить", callback_data="end_day")],
        [InlineKeyboardButton("🔴 Выключить", callback_data="shutdown_bot")],
    ])

def post_choice_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Пост в Twitter", callback_data="post_twitter")],
        [InlineKeyboardButton("Пост в Telegram", callback_data="post_telegram")],
        [InlineKeyboardButton("ПОСТ!", callback_data="post_both")],
        [InlineKeyboardButton("✍️ Сделай сам", callback_data="self_post")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_to_main")]
    ])

def post_end_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🆕 Новый пост", callback_data="new_post_manual")],
        [InlineKeyboardButton("✍️ Сделай сам", callback_data="self_post")],
        [InlineKeyboardButton("▶️ Старт (GitHub Action)", callback_data="run_github_action")],
        [InlineKeyboardButton("🌙 Не беспокоить", callback_data="do_not_disturb")],
        [InlineKeyboardButton("🔚 Завершить", callback_data="end_day")],
        [InlineKeyboardButton("💬 Поговорить", callback_data="chat")]
    ])

def start_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Опубликовать приветствие", callback_data="start_publish")],
        [InlineKeyboardButton("🆕 Новый пост", callback_data="new_post")],
        [InlineKeyboardButton("✍️ Сделай сам", callback_data="self_post")]
    ])

def generate_random_schedule(
    posts_per_day=6,
    day_start_hour=6,
    day_end_hour=23,
    min_offset=-20,
    max_offset=20
):
    if day_end_hour > 23: day_end_hour = 23
    now = datetime.now()
    today = now.date()
    start = datetime.combine(today, dt_time(hour=day_start_hour, minute=0, second=0))
    if now > start:
        start = now + timedelta(seconds=1)
    end = datetime.combine(today, dt_time(hour=day_end_hour, minute=0, second=0))
    total_seconds = int((end - start).total_seconds())
    if posts_per_day < 1:
        return []
    base_step = total_seconds // posts_per_day
    schedule = []
    for i in range(posts_per_day):
        base_sec = i * base_step
        offset_sec = random.randint(min_offset * 60, max_offset * 60) + random.randint(-59, 59)
        post_time = start + timedelta(seconds=base_sec + offset_sec)
        if post_time < start:
            post_time = start
        if post_time > end:
            post_time = end
        schedule.append(post_time)
    schedule.sort()
    return schedule

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

def download_image_to_temp(image_url):
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; AiBot/1.0; +https://gptonline.ai/)"
    }
    response = requests.get(image_url, headers=headers)
    response.raise_for_status()
    with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as tmp:
        tmp.write(response.content)
        tmp_path = tmp.name
    return tmp_path

def publish_post_to_twitter(text, image_url=None):
    try:
        media_ids = None
        tmp_path = None
        if image_url:
            # Всегда скачиваем файл!
            if image_url.startswith('http'):
                tmp_path = download_image_to_temp(image_url)
            else:
                # Telegram file_id — надо сначала скачать file!
                return False
            media = twitter_api_v1.media_upload(tmp_path)
            media_ids = [media.media_id_string]
        twitter_client_v2.create_tweet(text=text, media_ids=media_ids)
        logging.info("Пост успешно опубликован в Twitter!")
        if tmp_path:
            os.remove(tmp_path)
        return True
    except Exception as e:
        logging.error(f"Ошибка публикации в Twitter: {e}")
        return False

async def send_photo_by_file(bot: Bot, chat_id, image_url, caption):
    """
    Отправка фото в Telegram через скачивание, всегда file!
    """
    tmp_path = None
    try:
        if image_url and image_url.startswith('http'):
            tmp_path = download_image_to_temp(image_url)
            with open(tmp_path, "rb") as f:
                msg = await bot.send_photo(chat_id=chat_id, photo=f, caption=caption)
            os.remove(tmp_path)
            return msg
        elif image_url and not image_url.startswith('http'):
            # file_id от Telegram — можно отправлять напрямую
            msg = await bot.send_photo(chat_id=chat_id, photo=image_url, caption=caption)
            return msg
        else:
            # Нет фото
            msg = await bot.send_message(chat_id=chat_id, text=caption)
            return msg
    except Exception as e:
        logging.error(f"Не удалось отправить фото по ссылке: {e}")
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)
        await bot.send_message(chat_id=chat_id, text=f"Ошибка отправки фото: {e}")
        return None

async def send_post_for_approval():
    async with approval_lock:
        if do_not_disturb["active"] or pending_post["active"]:
            return
        post_data["timestamp"] = datetime.now()
        pending_post.update({
            "active": True,
            "timer": datetime.now(),
            "timeout": TIMER_PUBLISH_DEFAULT
        })
        try:
            msg = await send_photo_by_file(
                approval_bot,
                TELEGRAM_APPROVAL_CHAT_ID,
                post_data["image_url"],
                post_data["text_en"] + "\n\n" + WELCOME_HASHTAGS,
            )
            approval_message_ids["photo"] = getattr(msg, "message_id", None)
            logging.info("Пост отправлен на согласование.")
        except Exception as e:
            logging.error(f"Ошибка при отправке на согласование: {e}")

async def check_timer():
    while True:
        await asyncio.sleep(0.5)
        if pending_post["active"] and pending_post.get("timer"):
            passed = (datetime.now() - pending_post["timer"]).total_seconds()
            if passed > pending_post.get("timeout", TIMER_PUBLISH_DEFAULT):
                try:
                    base_text = post_data["text_en"].strip()
                    telegram_text = f"{base_text}\n\nRead more: https://getaicoin.com/"
                    twitter_text = build_twitter_post(base_text)
                    await approval_bot.send_message(
                        chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                        text="⌛ Время ожидания истекло. Публикую автоматически."
                    )
                    await send_photo_by_file(
                        channel_bot,
                        TELEGRAM_CHANNEL_USERNAME_ID,
                        post_data["image_url"],
                        telegram_text,
                    )
                    publish_post_to_twitter(twitter_text, post_data["image_url"])
                    logging.info("Автоматическая публикация произведена.")
                    await approval_bot.send_message(
                        chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                        text="✅ Посты автоматически опубликованы в Telegram и Twitter."
                    )
                    await approval_bot.send_message(
                        chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                        text="Выберите действие:",
                        reply_markup=post_end_keyboard()
                    )
                    shutdown_bot_and_exit()
                except Exception as e:
                    pending_post["active"] = False
                    await approval_bot.send_message(
                        chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                        text=f"❌ Ошибка при автопубликации: {e}\nВозможные действия: проверьте ключи, лимиты, права бота, лимиты Twitter/Telegram."
                    )
                    await approval_bot.send_message(
                        chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                        text="Выберите действие:",
                        reply_markup=post_end_keyboard()
                    )
                    logging.error(f"Ошибка при автопубликации: {e}")
                pending_post["active"] = False

async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                image_hash TEXT
            )
            """
        )
        await db.commit()
    logging.info("База данных инициализирована.")

async def save_post_to_history(text, image_url=None):
    image_hash = None
    if image_url:
        try:
            r = requests.get(image_url, timeout=3)
            r.raise_for_status()
            image_hash = hashlib.sha256(r.content).hexdigest()
        except Exception as e:
            logging.warning(f"Не удалось получить хеш изображения: {e}")
            image_hash = None
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO posts (text, timestamp, image_hash) VALUES (?, ?, ?)",
            (text, datetime.now().isoformat(), image_hash)
        )
        await db.commit()
    logging.info("Пост сохранён в историю.")

async def schedule_daily_posts():
    global manual_posts_today
    while True:
        manual_posts_today = 0
        now = datetime.now()
        if now.hour < 6:
            to_sleep = (datetime.combine(now.date(), dt_time(hour=6)) - now).total_seconds()
            logging.info(f"Жду до 06:00... {int(to_sleep)} сек")
            await asyncio.sleep(to_sleep)

        posts_left = lambda: scheduled_posts_per_day - manual_posts_today
        while posts_left() > 0:
            schedule = generate_random_schedule(posts_per_day=posts_left())
            logging.info(f"Расписание авто-постов на сегодня: {[t.strftime('%H:%M:%S') for t in schedule]}")
            for post_time in schedule:
                if posts_left() <= 0:
                    break
                now = datetime.now()
                delay = (post_time - now).total_seconds()
                if delay > 0:
                    logging.info(f"Жду {int(delay)} сек до {post_time.strftime('%H:%M:%S')} для публикации авто-поста")
                    await asyncio.sleep(delay)
                post_data["text_en"] = f"New post ({post_time.strftime('%H:%M:%S')})"
                post_data["image_url"] = random.choice(test_images)
                post_data["post_id"] += 1
                post_data["is_manual"] = False
                await send_post_for_approval()
                while pending_post["active"]:
                    await asyncio.sleep(1)
        tomorrow = datetime.combine(datetime.now().date() + timedelta(days=1), dt_time(hour=0))
        to_next_day = (tomorrow - datetime.now()).total_seconds()
        await asyncio.sleep(to_next_day)
        manual_posts_today = 0

async def self_post_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in user_self_post and user_self_post[user_id]['state'] == 'wait_post':
        text = update.message.text if update.message.text is not None else ""
        if not text:
            text = "🖼️ Фото без текста"
        image = None
        if update.message.photo:
            image = update.message.photo[-1].file_id
        user_self_post[user_id]['text'] = text
        user_self_post[user_id]['image'] = image
        user_self_post[user_id]['state'] = 'wait_confirm'
        if image:
            await approval_bot.send_photo(
                chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                photo=image,
                caption=text,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📤 Завершить генерацию поста", callback_data="finish_self_post")],
                    [InlineKeyboardButton("❌ Отмена", callback_data="cancel_to_main")]
                ])
            )
        else:
            await approval_bot.send_message(
                chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                text=text,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📤 Завершить генерацию поста", callback_data="finish_self_post")],
                    [InlineKeyboardButton("❌ Отмена", callback_data="cancel_to_main")]
                ])
            )
        return

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_action_time, prev_data, manual_posts_today
    await update.callback_query.answer()
    if pending_post["active"]:
        reset_timer(TIMER_PUBLISH_EXTEND)
    user_id = update.effective_user.id
    now = datetime.now()
    if user_id in last_action_time and (now - last_action_time[user_id]).seconds < 3:
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="⏳ Подождите немного...", reply_markup=main_keyboard())
        return
    last_action_time[user_id] = now
    action = update.callback_query.data
    prev_data.update(post_data)

    if action == "finish_self_post":
        info = user_self_post.get(user_id)
        if info and info["state"] == "wait_confirm":
            text = info.get("text", "")
            image = info.get("image", None)
            post_data["text_en"] = text
            if image:
                post_data["image_url"] = image
            else:
                post_data["image_url"] = random.choice(test_images)
            post_data["post_id"] += 1
            post_data["is_manual"] = True
            user_self_post.pop(user_id, None)
            if post_data["image_url"]:
                await approval_bot.send_photo(
                    chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                    photo=post_data["image_url"],
                    caption=post_data["text_en"],
                    reply_markup=post_choice_keyboard()
                )
            else:
                await approval_bot.send_message(
                    chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                    text=post_data["text_en"],
                    reply_markup=post_choice_keyboard()
                )
        return

    if action == "shutdown_bot":
        logging.info("Останавливаю бота по кнопке!")
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="🔴 Бот полностью выключен. GitHub Actions больше не тратит минуты!"
        )
        await asyncio.sleep(2)
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="Чтобы возобновить работу, нажмите ▶️ Старт.\nИли опубликуйте приветственный пост:",
            reply_markup=start_keyboard()
        )
        shutdown_bot_and_exit()
        return

    if action == "approve":
        twitter_text = build_twitter_post(post_data["text_en"])
        await approval_bot.send_photo(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            photo=post_data["image_url"],
            caption=twitter_text,
            reply_markup=post_choice_keyboard()
        )
        return

    if action in ["post_twitter", "post_telegram", "post_both"]:
        base_text = post_data["text_en"].strip()
        telegram_text = f"{base_text}\n\nRead more: https://getaicoin.com/"
        twitter_text = build_twitter_post(base_text)

        telegram_success = False
        twitter_success = False

        if action in ["post_telegram", "post_both"]:
            try:
                await send_photo_by_file(
                    channel_bot,
                    TELEGRAM_CHANNEL_USERNAME_ID,
                    post_data["image_url"],
                    telegram_text,
                )
                telegram_success = True
            except Exception as e:
                logging.error(f"Ошибка при публикации в Telegram: {e}")
                await approval_bot.send_message(
                    chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                    text=f"❌ Не удалось отправить в Telegram: {e}",
                    reply_markup=None
                )

        if action in ["post_twitter", "post_both"]:
            try:
                twitter_success = publish_post_to_twitter(twitter_text, post_data["image_url"])
            except Exception as e:
                logging.error(f"Ошибка при публикации в Twitter: {e}")
                await approval_bot.send_message(
                    chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                    text=f"❌ Не удалось отправить в Twitter: {e}",
                    reply_markup=None
                )

        pending_post["active"] = False
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="✅ Успешно отправлено в Telegram!" if telegram_success else "❌ Не удалось отправить в Telegram.",
            reply_markup=None
        )
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="✅ Успешно отправлено в Twitter!" if twitter_success else "❌ Не удалось отправить в Twitter.",
            reply_markup=None
        )
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="Работа завершена.",
            reply_markup=None
        )
        shutdown_bot_and_exit()
        return

    if action == "self_post":
        try:
            await update.callback_query.message.delete()
        except Exception:
            pass
        user_self_post[user_id] = {'text': '', 'image': None, 'state': 'wait_post'}
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="✍️ Напиши свой текст поста и (опционально) приложи фото — всё одним сообщением. После этого появится предпросмотр с кнопками."
        )
        return

    if action == "cancel_to_main":
        try:
            await update.callback_query.message.delete()
        except Exception:
            pass
        user_self_post.pop(user_id, None)
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="Главное меню:",
            reply_markup=main_keyboard()
        )
        return

    if action == "restore_previous":
        post_data.update(prev_data)
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="↩️ Восстановлен предыдущий вариант.", reply_markup=main_keyboard())
        if pending_post["active"]:
            await send_post_for_approval()
        return

    if action == "end_day":
        pending_post["active"] = False
        do_not_disturb["active"] = True
        tomorrow = datetime.combine(datetime.now().date() + timedelta(days=1), dt_time(hour=9))
        kb = main_keyboard()
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"🔚 Работа завершена на сегодня.\nСледующая публикация: {tomorrow.strftime('%Y-%m-%d %H:%M')}", parse_mode="HTML", reply_markup=kb)
        return

    if action == "think":
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="🧐 Думаем дальше…", reply_markup=main_keyboard())
        return

    if action == "chat":
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="💬 Начинаем чат:\n" + post_data["text_en"],
            reply_markup=post_end_keyboard()
        )
        return

    if action == "do_not_disturb":
        do_not_disturb["active"] = not do_not_disturb["active"]
        status = "включён" if do_not_disturb["active"] else "выключен"
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text=f"🌙 Режим «Не беспокоить» {status}.",
            reply_markup=post_end_keyboard()
        )
        return

    if action == "new_post":
        pending_post["active"] = False
        post_data["text_en"] = f"New test post #{post_data['post_id'] + 1}"
        post_data["image_url"] = random.choice(test_images)
        post_data["post_id"] += 1
        post_data["is_manual"] = False
        await send_post_for_approval()
        return

    if action == "new_post_manual":
        pending_post["active"] = False
        post_data["text_en"] = f"Manual new post #{post_data['post_id'] + 1}"
        post_data["image_url"] = random.choice(test_images)
        post_data["post_id"] += 1
        post_data["is_manual"] = True
        await send_post_for_approval()
        return

async def delayed_start(app: Application):
    await init_db()
    asyncio.create_task(schedule_daily_posts())
    asyncio.create_task(check_timer())
    await send_post_for_approval()
    logging.info("Бот запущен и готов к работе.")

def main():
    logging.info("Старт Telegram бота модерации и публикации…")
    app = Application.builder()\
        .token(TELEGRAM_BOT_TOKEN_APPROVAL)\
        .post_init(delayed_start)\
        .build()
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, self_post_message_handler))
    app.run_polling(poll_interval=0.12, timeout=1)

def shutdown_bot_and_exit():
    logging.info("Завершение работы бота через shutdown_bot_and_exit()")
    try:
        asyncio.create_task(
            approval_bot.send_message(
                chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                text="🔴 Бот полностью выключен. GitHub Actions больше не тратит минуты!"
            )
        )
    except Exception:
        pass
    import time; time.sleep(2)
    os._exit(0)

if __name__ == "__main__":
    main()