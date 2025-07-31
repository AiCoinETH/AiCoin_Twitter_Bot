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
from telegram.ext import Application, CallbackQueryHandler, ContextTypes
import aiosqlite
import telegram.error

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

# ========== ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ==========
TELEGRAM_BOT_TOKEN_APPROVAL = os.getenv("TELEGRAM_BOT_TOKEN_APPROVAL")
TELEGRAM_APPROVAL_CHAT_ID   = os.getenv("TELEGRAM_APPROVAL_CHAT_ID")
TELEGRAM_BOT_TOKEN_CHANNEL  = os.getenv("TELEGRAM_BOT_TOKEN_CHANNEL")
TELEGRAM_CHANNEL_USERNAME_ID = os.getenv("TELEGRAM_CHANNEL_USERNAME_ID")

if not TELEGRAM_BOT_TOKEN_APPROVAL or not TELEGRAM_APPROVAL_CHAT_ID or not TELEGRAM_BOT_TOKEN_CHANNEL or not TELEGRAM_CHANNEL_USERNAME_ID:
    logging.error("Не заданы обязательные переменные окружения (BOT_TOKEN_APPROVAL, APPROVAL_CHAT_ID, BOT_TOKEN_CHANNEL или CHANNEL_USERNAME_ID)")
    exit(1)

# ========== TWITTER ==========
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

# ========== ДОБАВЛЕНО ДЛЯ TWITTER FOLLOWERS ==========
def get_twitter_followers_count():
    user = twitter_api_v1.get_user(screen_name="AiCoin_ETH")
    return user.followers_count

async def update_pinned_message_with_followers():
    followers = get_twitter_followers_count()
    text = (
        f"🅧: <b>{followers}</b>\n"
        "Subscribe: https://x.com/AiCoin_ETH\n"
        "🌐: https://getaicoin.com"
    )
    chat = await channel_bot.get_chat(TELEGRAM_CHANNEL_USERNAME_ID)
    pinned = chat.pinned_message
    if pinned:
        await channel_bot.edit_message_text(
            chat_id=TELEGRAM_CHANNEL_USERNAME_ID,
            message_id=pinned.message_id,
            text=text,
            parse_mode='HTML'
        )
    else:
        msg = await channel_bot.send_message(
            chat_id=TELEGRAM_CHANNEL_USERNAME_ID,
            text=text,
            parse_mode='HTML'
        )
        await channel_bot.pin_chat_message(
            chat_id=TELEGRAM_CHANNEL_USERNAME_ID,
            message_id=msg.message_id
        )

# ========== ДАННЫЕ ДЛЯ ТЕСТА ==========
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

# ========== ТАЙМЕРЫ И КОЛ-ВО ПОСТОВ ==========
TIMER_PUBLISH_DEFAULT = 180    # 3 минуты после отправки на модерацию
TIMER_PUBLISH_EXTEND  = 900    # 15 минут после любого нажатия кнопки

pending_post         = {"active": False, "timer": None, "timeout": TIMER_PUBLISH_DEFAULT}
do_not_disturb       = {"active": False}
last_action_time     = {}
approval_message_ids = {"photo": None}
DB_FILE = "post_history.db"

scheduled_posts_per_day = 6
manual_posts_today = 0  # Сколько ручных постов отправлено сегодня

def reset_timer(timeout=None):
    pending_post["timer"] = datetime.now()
    if timeout:
        pending_post["timeout"] = timeout

# ========== КЛАВИАТУРЫ ==========
keyboard = InlineKeyboardMarkup([
    [InlineKeyboardButton("✅ Пост", callback_data="approve")],
    [InlineKeyboardButton("🕒 Подумать", callback_data="think")],
    [InlineKeyboardButton("🆕 Новый пост", callback_data="new_post")],
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

post_end_keyboard = InlineKeyboardMarkup([
    [InlineKeyboardButton("🆕 Новый пост", callback_data="new_post_manual")],
    [InlineKeyboardButton("🌙 Не беспокоить", callback_data="do_not_disturb")],
    [InlineKeyboardButton("🔚 Завершить", callback_data="end_day")],
    [InlineKeyboardButton("💬 Поговорить", callback_data="chat")]
])

# ========== ГЕНЕРАЦИЯ РАСПИСАНИЯ ==========
def generate_random_schedule(
    posts_per_day=6,
    day_start_hour=6,
    day_end_hour=24,
    min_offset=-20,
    max_offset=20
):
    now = datetime.now()
    today = now.date()
    # Не начинать публикацию в прошлом, если бот запущен днем
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
        # Не выходить за границы
        if post_time < start:
            post_time = start
        if post_time > end:
            post_time = end
        schedule.append(post_time)
    schedule.sort()
    return schedule

# ========== ФУНКЦИИ ДЛЯ ПОСТРОЕНИЯ ТЕКСТА ==========
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

# ========== TWITTER POST С ОБХОДОМ ==========
def publish_post_to_twitter(text, image_url=None):
    try:
        media_ids = None
        if image_url:
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

# ========== ИНИЦИАЛИЗАЦИЯ БД ==========
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

# ========== ОТПРАВКА НА МОДЕРАЦИЮ ==========
async def send_post_for_approval():
    if do_not_disturb["active"] or pending_post["active"]:
        return

    post_data["timestamp"] = datetime.now()
    pending_post.update({
        "active": True,
        "timer": datetime.now(),
        "timeout": TIMER_PUBLISH_DEFAULT
    })
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

# ========== ПУБЛИКАЦИЯ В КАНАЛ ==========
async def publish_post_to_channel():
    try:
        msg = await channel_bot.send_photo(
            chat_id=TELEGRAM_CHANNEL_USERNAME_ID,
            photo=post_data["image_url"],
            caption=post_data["text_ru"]
        )
        logging.info(f"Пост опубликован в канал {TELEGRAM_CHANNEL_USERNAME_ID}, message_id={msg.message_id}")
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text=f"✅ Пост опубликован в канал {TELEGRAM_CHANNEL_USERNAME_ID}!\n\nСсылка: https://t.me/{TELEGRAM_CHANNEL_USERNAME_ID.lstrip('@')}/{msg.message_id}"
        )
    except telegram.error.Forbidden as e:
        pending_post["active"] = False
        logging.error(f"Forbidden: Бот не админ или не может писать в канал {TELEGRAM_CHANNEL_USERNAME_ID}: {e}")
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="❌ Не удалось опубликовать пост: у бота нет прав или он не в канале!"
        )
    except telegram.error.BadRequest as e:
        pending_post["active"] = False
        logging.error(f"BadRequest: Проверьте username канала {TELEGRAM_CHANNEL_USERNAME_ID}: {e}")
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text=f"❌ Ошибка: проверьте username канала {TELEGRAM_CHANNEL_USERNAME_ID}!"
        )
    except Exception as e:
        pending_post["active"] = False
        logging.error(f"Ошибка публикации в канал {TELEGRAM_CHANNEL_USERNAME_ID}: {e}")
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="❌ Ошибка публикации в канал!"
        )

    asyncio.create_task(save_post_to_history(post_data["text_ru"], post_data["image_url"]))
    pending_post["active"] = False

# ========== ТАЙМЕР МОДЕРАЦИИ ==========
async def check_timer():
    while True:
        await asyncio.sleep(0.5)
        if pending_post["active"] and pending_post.get("timer"):
            passed = (datetime.now() - pending_post["timer"]).total_seconds()
            if passed > pending_post.get("timeout", TIMER_PUBLISH_DEFAULT):
                try:
                    await approval_bot.send_message(
                        chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                        text="⌛ Время ожидания истекло. Публикую автоматически."
                    )
                    await publish_post_to_channel()
                    twitter_text = build_twitter_post(post_data["text_en"])
                    publish_post_to_twitter(twitter_text, post_data["image_url"])
                    logging.info("Автоматическая публикация произведена.")
                    await approval_bot.send_message(
                        chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                        text="✅ Посты автоматически опубликованы в Telegram и Twitter."
                    )
                    await approval_bot.send_message(
                        chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                        text="Выберите действие:",
                        reply_markup=post_end_keyboard
                    )
                except Exception as e:
                    pending_post["active"] = False
                    await approval_bot.send_message(
                        chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                        text=f"❌ Ошибка при автопубликации: {e}\nВозможные действия: проверьте ключи, лимиты, права бота, лимиты Twitter/Telegram."
                    )
                    await approval_bot.send_message(
                        chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                        text="Выберите действие:",
                        reply_markup=post_end_keyboard
                    )
                    logging.error(f"Ошибка при автопубликации: {e}")
                pending_post["active"] = False  # Остановить все таймеры этого поста

# ========== АСИНХРОННОЕ РАСПИСАНИЕ ==========
async def schedule_daily_posts():
    global manual_posts_today
    while True:
        manual_posts_today = 0  # сбрасываем счетчик ручных постов каждый новый день
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
                # Пересчёт в реальном времени — если ручной пост был опубликован, уменьшается оставшееся число
                if posts_left() <= 0:
                    break
                now = datetime.now()
                delay = (post_time - now).total_seconds()
                if delay > 0:
                    logging.info(f"Жду {int(delay)} сек до {post_time.strftime('%H:%M:%S')} для публикации авто-поста")
                    await asyncio.sleep(delay)
                pending_post["active"] = False
                post_data["text_ru"] = f"Новый пост ({post_time.strftime('%H:%M:%S')})"
                post_data["image_url"] = random.choice(test_images)
                post_data["post_id"] += 1
                post_data["is_manual"] = False
                await send_post_for_approval()
                # Ждём публикации поста или автотаймаута (автоматическая публикация по таймеру)
                while pending_post["active"]:
                    await asyncio.sleep(1)
        # Ждём до следующего дня
        tomorrow = datetime.combine(datetime.now().date() + timedelta(days=1), dt_time(hour=0))
        to_next_day = (tomorrow - datetime.now()).total_seconds()
        await asyncio.sleep(to_next_day)
        manual_posts_today = 0  # сбрасываем в начале нового дня

# ========== ОБРАБОТЧИК КНОПОК ==========
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_action_time, prev_data, manual_posts_today
    await update.callback_query.answer()
    if pending_post["active"]:
        reset_timer(TIMER_PUBLISH_EXTEND)

    user_id = update.effective_user.id
    now = datetime.now()
    if user_id in last_action_time and (now - last_action_time[user_id]).seconds < 3:
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="⏳ Подождите немного...", reply_markup=keyboard)
        return
    last_action_time[user_id] = now
    action = update.callback_query.data
    prev_data.update(post_data)

    # --- Новая ветка: после "✅ Пост" ---
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
        twitter_text = build_twitter_post(post_data["text_en"])
        twitter_success = False
        telegram_success = False
        is_manual = post_data.get("is_manual", False)
        if mode == "twitter":
            twitter_success = publish_post_to_twitter(twitter_text, post_data["image_url"])
            pending_post["active"] = False
            await approval_bot.send_message(
                chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                text="✅ Успешно отправлено в Twitter!" if twitter_success else "❌ Не удалось отправить в Twitter.",
                reply_markup=None
            )
            await approval_bot.send_message(
                chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                text="Выберите действие:",
                reply_markup=post_end_keyboard
            )
        elif mode == "telegram":
            try:
                await channel_bot.send_photo(
                    chat_id=TELEGRAM_CHANNEL_USERNAME_ID,
                    photo=post_data["image_url"],
                    caption=post_data["text_en"] + "\n\n🌐 https://getaicoin.com/"
                )
                telegram_success = True
            except Exception as e:
                logging.error(f"Ошибка при публикации в Telegram: {e}")
                await approval_bot.send_message(
                    chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                    text=f"❌ Не удалось отправить в Telegram: {e}",
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
                text="Выберите действие:",
                reply_markup=post_end_keyboard
            )
        elif mode == "both":
            try:
                await channel_bot.send_photo(
                    chat_id=TELEGRAM_CHANNEL_USERNAME_ID,
                    photo=post_data["image_url"],
                    caption=post_data["text_en"] + "\n\n🌐 https://getaicoin.com/"
                )
                telegram_success = True
            except Exception as e:
                logging.error(f"Ошибка при публикации в Telegram: {e}")
                await approval_bot.send_message(
                    chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                    text=f"❌ Не удалось отправить в Telegram: {e}",
                    reply_markup=None
                )
            twitter_success = publish_post_to_twitter(twitter_text, post_data["image_url"])
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
                text="Выберите действие:",
                reply_markup=post_end_keyboard
            )
        # После публикации ручного поста - уменьшаем число авто-постов
        if is_manual:
            manual_posts_today += 1
            post_data["is_manual"] = False
        return
    if action == "cancel_to_main":
        if pending_post["active"]:
            await send_post_for_approval()
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

    if action == "new_post":
        pending_post["active"] = False
        post_data["text_ru"] = f"Новый тестовый пост #{post_data['post_id'] + 1}"
        post_data["image_url"] = random.choice(test_images)
        post_data["post_id"] += 1
        post_data["is_manual"] = False
        await send_post_for_approval()
        return
    if action == "new_post_manual":
        pending_post["active"] = False
        post_data["text_ru"] = f"Ручной новый пост #{post_data['post_id'] + 1}"
        post_data["image_url"] = random.choice(test_images)
        post_data["post_id"] += 1
        post_data["is_manual"] = True
        await send_post_for_approval()
        return
    elif action == 'think':
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="🧐 Думаем дальше…", reply_markup=keyboard)
    elif action == "chat":
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="💬 Начинаем чат:\n" + post_data["text_ru"],
            reply_markup=post_end_keyboard
        )
    elif action == "do_not_disturb":
        do_not_disturb["active"] = not do_not_disturb["active"]
        status = "включён" if do_not_disturb["active"] else "выключен"
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text=f"🌙 Режим «Не беспокоить» {status}.",
            reply_markup=post_end_keyboard
        )
    elif action == "restore_previous":
        post_data.update(prev_data)
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="↩️ Восстановлен предыдущий вариант.", reply_markup=keyboard)
        if pending_post["active"]:
            await send_post_for_approval()
    elif action == "end_day":
        pending_post["active"] = False
        do_not_disturb["active"] = True
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="🔚 Завершили публикации на сегодня.", reply_markup=post_end_keyboard)

# ========== ЗАПУСК ==========
async def delayed_start(app: Application):
    await init_db()
    asyncio.create_task(schedule_daily_posts())
    await send_post_for_approval()
    asyncio.create_task(check_timer())
    await update_pinned_message_with_followers()  # <<< ДОБАВЛЕНО!
    logging.info("Бот запущен и готов к работе.")

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
