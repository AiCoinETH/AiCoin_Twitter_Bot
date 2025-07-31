import os
import asyncio
import hashlib
import logging
import random
from datetime import datetime, timedelta, time as dt_time
import tweepy
import requests
import tempfile

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, Bot
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters
import aiosqlite
import telegram.error

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

# --- Переменные окружения и настройки ---
TELEGRAM_BOT_TOKEN_APPROVAL = os.getenv("TELEGRAM_BOT_TOKEN_APPROVAL")
TELEGRAM_APPROVAL_CHAT_ID   = os.getenv("TELEGRAM_APPROVAL_CHAT_ID")
TELEGRAM_BOT_TOKEN_CHANNEL  = os.getenv("TELEGRAM_BOT_TOKEN_CHANNEL")
TELEGRAM_CHANNEL_USERNAME_ID = os.getenv("TELEGRAM_CHANNEL_USERNAME_ID")

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

# --- FSM состояния ---
FSM = {
    "SLEEP": "sleep_today",
    "AUTO": "auto_mode",
    "MANUAL": "manual_mode"
}
fsm_state = {"current": FSM["MANUAL"]}

# --- Данные для теста и временные переменные ---
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
    "text_en":   "Mining tokens are back in focus. Example of a full English post for Telegram or short version for Twitter!"
}
prev_data = post_data.copy()
user_self_post = {}

TIMER_PUBLISH_DEFAULT = 180
TIMER_PUBLISH_EXTEND  = 900
pending_post = {"active": False, "timer": None, "timeout": TIMER_PUBLISH_DEFAULT}
do_not_disturb = {"active": False}
last_action_time = {}
approval_message_ids = {"photo": None}
DB_FILE = "post_history.db"
scheduled_posts_per_day = 6
manual_posts_today = 0

def reset_timer(timeout=None):
    pending_post["timer"] = datetime.now()
    if timeout:
        pending_post["timeout"] = timeout

# --- Кнопки для меню ---
def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🆕 Новый пост", callback_data="new_post")],
        [InlineKeyboardButton("✍️ Сделай сам", callback_data="self_post")],
        [InlineKeyboardButton("▶️ Старт", callback_data="start")],
        [InlineKeyboardButton("🌙 Не беспокоить", callback_data="do_not_disturb")],
        [InlineKeyboardButton("🔚 Завершить", callback_data="end_day")],
        [InlineKeyboardButton("▶️ Старт (GitHub Action)", callback_data="run_github_action")]
    ])

def post_choice_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Пост в Twitter", callback_data="post_twitter")],
        [InlineKeyboardButton("Пост в Telegram", callback_data="post_telegram")],
        [InlineKeyboardButton("ПОСТ!", callback_data="post_both")],
        [InlineKeyboardButton("✍️ Сделай сам", callback_data="self_post")],
        [InlineKeyboardButton("▶️ Старт", callback_data="start")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_to_main")]
    ])

def post_end_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🆕 Новый пост", callback_data="new_post_manual")],
        [InlineKeyboardButton("✍️ Сделай сам", callback_data="self_post")],
        [InlineKeyboardButton("▶️ Старт", callback_data="start")],
        [InlineKeyboardButton("🌙 Не беспокоить", callback_data="do_not_disturb")],
        [InlineKeyboardButton("🔚 Завершить", callback_data="end_day")],
        [InlineKeyboardButton("💬 Поговорить", callback_data="chat")]
    ])

def auto_mode_keyboard(next_time=None):
    txt = "🌙 Режим 'Не беспокоить' включён!\nБот будет публиковать посты по расписанию."
    if next_time:
        txt += f"\nБлижайшая публикация сегодня: <b>{next_time.strftime('%H:%M')}</b>"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Отмена режима", callback_data="cancel_auto_mode")]
    ]), txt

def sleep_keyboard(next_time=None):
    rows = [[InlineKeyboardButton("▶️ Старт", callback_data="start")]]
    txt = "Работа бота завершена до следующего дня."
    if next_time:
        txt += f"\nСледующая публикация запланирована на: <b>{next_time.strftime('%Y-%m-%d %H:%M')}</b>"
    return InlineKeyboardMarkup(rows), txt

# --- Функции для генерации расписания ---
def generate_random_schedule(
    posts_per_day=6,
    day_start_hour=6,
    day_end_hour=24,
    min_offset=-20,
    max_offset=20
):
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

def publish_post_to_twitter(text, image_url=None):
    try:
        media_ids = None
        if image_url:
            if image_url.startswith('http'):
                response = requests.get(image_url)
                response.raise_for_status()
                with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as tmp:
                    tmp.write(response.content)
                    tmp_path = tmp.name
                try:
                    media = twitter_api_v1.media_upload(tmp_path)
                    media_ids = [media.media_id_string]
                finally:
                    os.remove(tmp_path)
        twitter_client_v2.create_tweet(text=text, media_ids=media_ids)
        logging.info("Пост успешно опубликован в Twitter!")
        return True
    except Exception as e:
        pending_post["active"] = False
        logging.error(f"Ошибка публикации в Twitter: {e}")
        asyncio.create_task(approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text=f"❌ Ошибка при публикации в Twitter: {e}\n"
                 "Проверьте:\n"
                 "- Действительность ключей/токенов\n"
                 "- Лимиты публикации (Twitter API)\n"
                 "- Формат медиа\n"
                 "- Права доступа\n"
                 "Пост не будет опубликован повторно автоматически."
        ))
        return False

# --- База данных (по желанию) ---
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

def get_image_hash(url: str) -> str | None:
    try:
        r = requests.get(url, timeout=3)
        r.raise_for_status()
        return hashlib.sha256(r.content).hexdigest()
    except Exception as e:
        logging.warning(f"Не удалось получить хеш изображения: {e}")
        return None

async def save_post_to_history(text, image_url=None):
    image_hash = get_image_hash(image_url) if image_url else None
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO posts (text, timestamp, image_hash) VALUES (?, ?, ?)",
            (text, datetime.now().isoformat(), image_hash)
        )
        await db.commit()
    logging.info("Пост сохранён в историю.")

# --- "Сделай сам" — обработчик пользовательского текста и фото ---
async def self_post_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in user_self_post and user_self_post[user_id]['state'] == 'wait_post':
        text = update.message.text or ""
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

# --- Основной обработчик кнопок с логикой всех сценариев ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_action_time, prev_data, manual_posts_today
    await update.callback_query.answer()
    user_id = update.effective_user.id
    now = datetime.now()
    action = update.callback_query.data
    prev_data.update(post_data)

    # Главное меню (старт, отмена)
    if action in ["start", "cancel_to_main"]:
        fsm_state["current"] = FSM["MANUAL"]
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="Главное меню:",
            reply_markup=main_keyboard()
        )
        return

    # Новый пост
    if action == "new_post":
        fsm_state["current"] = FSM["MANUAL"]
        post_data["text_ru"] = f"Новый тестовый пост #{post_data['post_id'] + 1}"
        post_data["image_url"] = random.choice(test_images)
        post_data["post_id"] += 1
        await approval_bot.send_photo(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            photo=post_data["image_url"],
            caption=post_data["text_ru"],
            reply_markup=post_choice_keyboard()
        )
        return

    # Сделай сам
    if action == "self_post":
        user_self_post[user_id] = {'text': '', 'image': None, 'state': 'wait_post'}
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="✍️ Напиши свой текст поста и (опционально) приложи фото — всё одним сообщением. После этого появится предпросмотр с кнопками."
        )
        return

    if action == "finish_self_post":
        data = user_self_post.get(user_id)
        if not data or not data.get('text'):
            user_self_post.pop(user_id, None)
            await approval_bot.send_message(
                chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                text="Главное меню:",
                reply_markup=main_keyboard()
            )
            return
        if data.get('image'):
            await approval_bot.send_photo(
                chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                photo=data['image'],
                caption=data['text'],
                reply_markup=post_choice_keyboard()
            )
        else:
            await approval_bot.send_message(
                chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                text=data['text'],
                reply_markup=post_choice_keyboard()
            )
        post_data["text_ru"] = data['text']
        post_data["text_en"] = data['text']
        post_data["image_url"] = data.get('image')
        post_data["is_manual"] = True
        user_self_post.pop(user_id, None)
        return

    # Не беспокоить
    if action == "do_not_disturb":
        fsm_state["current"] = FSM["AUTO"]
        schedule = generate_random_schedule(posts_per_day=scheduled_posts_per_day)
        next_time = schedule[0] if schedule else None
        kb, txt = auto_mode_keyboard(next_time)
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text=txt,
            parse_mode="HTML",
            reply_markup=kb
        )
        return

    if action == "cancel_auto_mode":
        fsm_state["current"] = FSM["MANUAL"]
        schedule = generate_random_schedule(posts_per_day=scheduled_posts_per_day)
        next_time = schedule[0] if schedule else None
        txt = "✅ Режим 'Не беспокоить' отключён.\n"
        if next_time:
            txt += f"Следующая публикация по расписанию: <b>{next_time.strftime('%H:%M')}</b>"
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text=txt,
            parse_mode="HTML",
            reply_markup=main_keyboard()
        )
        return

    # Завершить на сегодня
    if action == "end_day":
        fsm_state["current"] = FSM["SLEEP"]
        tomorrow = datetime.combine(datetime.now().date() + timedelta(days=1), dt_time(hour=9))
        kb, txt = sleep_keyboard(next_time=tomorrow)
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text=f"🔚 Работа завершена на сегодня.\n{txt}",
            parse_mode="HTML",
            reply_markup=kb
        )
        return

    # Запуск GitHub Actions
    if action == "run_github_action":
        GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
        GITHUB_REPO = os.getenv("GITHUB_REPO")  # формат "user/repo"
        GITHUB_WORKFLOW = os.getenv("GITHUB_WORKFLOW")  # например "workflow.yml"

        if not all([GITHUB_TOKEN, GITHUB_REPO, GITHUB_WORKFLOW]):
            await approval_bot.send_message(
                chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                text="❌ Не заданы переменные окружения для GitHub Action."
            )
            return

        url = f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/{GITHUB_WORKFLOW}/dispatches"
        headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json"
        }
        data = {"ref": "main"}  # или ветка, с которой запускается workflow

        response = requests.post(url, headers=headers, json=data)

        if response.status_code == 204:
            await approval_bot.send_message(
                chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                text="✅ GitHub Action успешно запущен!"
            )
        else:
            await approval_bot.send_message(
                chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                text=f"❌ Ошибка запуска GitHub Action: {response.status_code}"
            )
        return

    # Публикация
    if action in ["post_twitter", "post_telegram", "post_both"]:
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="✅ Пост опубликован!\nБот переходит в режим ожидания по расписанию.",
            reply_markup=main_keyboard()
        )
        fsm_state["current"] = FSM["MANUAL"]
        return

# --- Фоновый автопостинг ---
async def schedule_daily_posts():
    while True:
        if fsm_state["current"] == FSM["SLEEP"]:
            await asyncio.sleep(60)
            continue
        if fsm_state["current"] == FSM["AUTO"]:
            schedule = generate_random_schedule(posts_per_day=scheduled_posts_per_day)
            for t in schedule:
                now = datetime.now()
                delay = (t - now).total_seconds()
                if delay > 0:
                    await asyncio.sleep(delay)
                await approval_bot.send_message(
                    chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                    text=f"Авто-публикация по расписанию: {t.strftime('%H:%M')}\n(тут будет публикация в канал/X)",
                )
            fsm_state["current"] = FSM["SLEEP"]
        else:
            await asyncio.sleep(10)

async def delayed_start(app: Application):
    await init_db()
    asyncio.create_task(schedule_daily_posts())
    await approval_bot.send_message(
        chat_id=TELEGRAM_APPROVAL_CHAT_ID,
        text="Бот запущен. Главное меню:",
        reply_markup=main_keyboard()
    )
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

if __name__ == "__main__":
    main()
