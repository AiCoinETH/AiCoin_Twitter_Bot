import os
import asyncio
import hashlib
import logging
import random
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, time as dt_time

import tweepy
import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, Bot
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters
import aiosqlite
from github import Github

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(funcName)s %(message)s'
)

# --- ENVIRONMENT VARIABLES ---
TELEGRAM_BOT_TOKEN_APPROVAL = os.getenv("TELEGRAM_BOT_TOKEN_APPROVAL")
TELEGRAM_APPROVAL_CHAT_ID_STR = os.getenv("TELEGRAM_APPROVAL_CHAT_ID")
TELEGRAM_BOT_TOKEN_CHANNEL = os.getenv("TELEGRAM_BOT_TOKEN_CHANNEL")
TELEGRAM_CHANNEL_USERNAME_ID = os.getenv("TELEGRAM_CHANNEL_USERNAME_ID")

TWITTER_API_KEY = os.getenv("TWITTER_API_KEY")
TWITTER_API_SECRET = os.getenv("TWITTER_API_SECRET")
TWITTER_ACCESS_TOKEN = os.getenv("TWITTER_ACCESS_TOKEN")
TWITTER_ACCESS_TOKEN_SECRET = os.getenv("TWITTER_ACCESS_TOKEN_SECRET")

GITHUB_TOKEN = os.getenv("ACTION_PAT_GITHUB")
GITHUB_REPO = os.getenv("ACTION_REPO_GITHUB")
GITHUB_IMAGE_PATH = "images_for_posts"

if not all([TELEGRAM_BOT_TOKEN_APPROVAL, TELEGRAM_APPROVAL_CHAT_ID_STR, TELEGRAM_BOT_TOKEN_CHANNEL, TELEGRAM_CHANNEL_USERNAME_ID]):
    logging.error("Не заданы обязательные переменные окружения Telegram!")
    sys.exit(1)
TELEGRAM_APPROVAL_CHAT_ID = int(TELEGRAM_APPROVAL_CHAT_ID_STR)
if not all([TWITTER_API_KEY, TWITTER_API_SECRET, TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_TOKEN_SECRET]):
    logging.error("Не заданы обязательные переменные окружения для Twitter!")
    sys.exit(1)
if not all([GITHUB_TOKEN, GITHUB_REPO]):
    logging.error("Не заданы обязательные переменные окружения GitHub!")
    sys.exit(1)

approval_bot = Bot(token=TELEGRAM_BOT_TOKEN_APPROVAL)
channel_bot = Bot(token=TELEGRAM_BOT_TOKEN_CHANNEL)
approval_lock = asyncio.Lock()
DB_FILE = "post_history.db"
scheduled_posts_per_day = 6
manual_posts_today = 0
TIMER_PUBLISH_DEFAULT = 180
TIMER_PUBLISH_EXTEND = 900

WELCOME_HASHTAGS = "#AiCoin #AI #crypto #тренды #бот #новости"
test_images = [
    "https://upload.wikimedia.org/wikipedia/commons/4/47/PNG_transparency_demonstration_1.png",
    "https://upload.wikimedia.org/wikipedia/commons/3/3f/Fronalpstock_big.jpg",
    "https://upload.wikimedia.org/wikipedia/commons/1/17/Google-flutter-logo.png",
    "https://upload.wikimedia.org/wikipedia/commons/d/d6/Wp-w4-big.jpg"
]
WELCOME_POST_RU = (
    "🚀 Привет! Это бот публикаций.\n\n"
    "ИИ-генерация, новости, идеи, генерация картинок и многое другое."
)
post_data = {
    "text_ru": WELCOME_POST_RU,
    "text_en": WELCOME_POST_RU,
    "image_url": test_images[0],
    "timestamp": None,
    "post_id": 0,
    "is_manual": False
}
prev_data = post_data.copy()
user_self_post = {}
pending_post = {"active": False, "timer": None, "timeout": TIMER_PUBLISH_DEFAULT}
do_not_disturb = {"active": False}
last_action_time = {}

def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Пост", callback_data="approve")],
        [InlineKeyboardButton("✍️ Сделай сам", callback_data="self_post")],
        [InlineKeyboardButton("🕒 Подумать", callback_data="think")],
        [InlineKeyboardButton("🆕 Новый пост", callback_data="new_post")],
        [InlineKeyboardButton("✏️ Изменить", callback_data="edit_post")],
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
        [InlineKeyboardButton("🌙 Не беспокоить", callback_data="do_not_disturb")],
        [InlineKeyboardButton("🔚 Завершить", callback_data="end_day")],
        [InlineKeyboardButton("💬 Поговорить", callback_data="chat")]
    ])

# --- Twitter и GitHub
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
github_client = Github(GITHUB_TOKEN)
github_repo = github_client.get_repo(GITHUB_REPO)

def build_twitter_post(text_ru: str) -> str:
    signature = "\nLearn more: https://getaicoin.com/ | Twitter: https://x.com/AiCoin_ETH #AiCoin #Ai $Ai #crypto #blockchain #AI #DeFi"
    max_length = 280
    reserve = max_length - len(signature)
    if len(text_ru) > reserve:
        main_part = text_ru[:reserve - 3].rstrip() + "..."
    else:
        main_part = text_ru
    return main_part + signature

def build_telegram_post(text_ru: str) -> str:
    signature = '\n\n<a href="https://getaicoin.com/">Website</a> | <a href="https://x.com/AiCoin_ETH">Twitter</a>'
    return text_ru.strip() + signature
def upload_image_to_github(image_path, filename):
    logging.info(f"upload_image_to_github: image_path={image_path}, filename={filename}")
    with open(image_path, "rb") as img_file:
        content = img_file.read()
    try:
        github_repo.create_file(f"{GITHUB_IMAGE_PATH}/{filename}", "upload image for post", content, branch="main")
        url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{GITHUB_IMAGE_PATH}/{filename}"
        logging.info(f"upload_image_to_github: Загружено на GitHub: {url}")
        return url
    except Exception as e:
        logging.error(f"Ошибка загрузки файла на GitHub: {e}")
        return None

def delete_image_from_github(filename):
    try:
        file_path = f"{GITHUB_IMAGE_PATH}/{filename}"
        contents = github_repo.get_contents(file_path, ref="main")
        github_repo.delete_file(contents.path, "delete image after posting", contents.sha, branch="main")
        logging.info(f"delete_image_from_github: Удалён файл с GitHub: {filename}")
    except Exception as e:
        logging.error(f"Ошибка удаления файла с GitHub: {e}")

async def download_image_async(url_or_file_id, is_telegram_file=False, bot=None, retries=3):
    if is_telegram_file:
        for attempt in range(retries):
            try:
                file = await bot.get_file(url_or_file_id)
                tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
                await file.download_to_drive(tmp_file.name)
                return tmp_file.name
            except Exception as e:
                await asyncio.sleep(1)
        raise Exception("Не удалось скачать файл из Telegram после нескольких попыток")
    else:
        headers = {'User-Agent': 'Mozilla/5.0'}
        r = requests.get(url_or_file_id, headers=headers)
        r.raise_for_status()
        tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
        tmp_file.write(r.content)
        tmp_file.close()
        return tmp_file.name

async def save_image_and_get_github_url(image_path):
    filename = f"{uuid.uuid4().hex}.jpg"
    url = upload_image_to_github(image_path, filename)
    return url, filename

async def process_telegram_photo(file_id: str, bot: Bot) -> str:
    file_path = await download_image_async(file_id, is_telegram_file=True, bot=bot)
    url, filename = await save_image_and_get_github_url(file_path)
    os.remove(file_path)
    if not url:
        raise Exception("Не удалось загрузить фото на GitHub")
    return url

async def send_photo_with_download(bot, chat_id, url_or_file_id, caption=None, reply_markup=None):
    github_filename = None
    def is_valid_image_url(url):
        try:
            resp = requests.head(url, timeout=5)
            return resp.headers.get('Content-Type', '').startswith('image/')
        except Exception:
            return False
    try:
        if isinstance(url_or_file_id, str) and url_or_file_id.startswith("images_for_posts/") and os.path.exists(url_or_file_id):
            with open(url_or_file_id, "rb") as img:
                msg = await bot.send_photo(chat_id=chat_id, photo=img, caption=caption, parse_mode="HTML", reply_markup=reply_markup)
            return msg, None
        elif not str(url_or_file_id).startswith("http"):
            url = await process_telegram_photo(url_or_file_id, bot)
            github_filename = url.split('/')[-1]
            msg = await bot.send_photo(chat_id=chat_id, photo=url, caption=caption, parse_mode="HTML", reply_markup=reply_markup)
            return msg, github_filename
        else:
            if not is_valid_image_url(url_or_file_id):
                await bot.send_message(chat_id=chat_id, text=caption, parse_mode="HTML", reply_markup=reply_markup)
                return None, None
            try:
                response = requests.get(url_or_file_id, timeout=10)
                response.raise_for_status()
                tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
                tmp_file.write(response.content)
                tmp_file.close()
                with open(tmp_file.name, "rb") as img:
                    msg = await bot.send_photo(chat_id=chat_id, photo=img, caption=caption, parse_mode="HTML", reply_markup=reply_markup)
                os.remove(tmp_file.name)
                return msg, None
            except Exception:
                await bot.send_message(chat_id=chat_id, text=caption, parse_mode="HTML", reply_markup=reply_markup)
                return None, None
    except Exception as e:
        logging.error(f"Ошибка в send_photo_with_download: {e}")
        await bot.send_message(chat_id=chat_id, text=caption, parse_mode="HTML", reply_markup=reply_markup)
        return None, None

async def safe_preview_post(bot, chat_id, text, image_url=None, reply_markup=None):
    try:
        if image_url:
            try:
                await send_photo_with_download(bot, chat_id, image_url, caption=text, reply_markup=reply_markup)
            except Exception as e:
                await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
        else:
            await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
    except Exception as e:
        await bot.send_message(chat_id=chat_id, text="Ошибка предпросмотра. Вот текст поста:\n\n" + text, reply_markup=reply_markup)

async def preview_dual(bot, chat_id, text, image_url=None, reply_markup=None):
    preview = (
        f"<b>Telegram:</b>\n{build_telegram_post(text)}\n\n"
        f"<b>Twitter:</b>\n{build_twitter_post(text)}"
    )
    await safe_preview_post(bot, chat_id, preview, image_url=image_url, reply_markup=reply_markup)

# --- DB, SCHEDULER & TIMER ---
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                image_hash TEXT
            )
        """)
        await db.commit()

async def save_post_to_history(text, image_url=None):
    image_hash = None
    if image_url:
        try:
            is_telegram = not (str(image_url).startswith("http"))
            if is_telegram:
                file_path = await download_image_async(image_url, True, approval_bot)
                with open(file_path, "rb") as f:
                    image_hash = hashlib.sha256(f.read()).hexdigest()
                os.remove(file_path)
            else:
                r = requests.get(image_url, timeout=3)
                r.raise_for_status()
                image_hash = hashlib.sha256(r.content).hexdigest()
        except Exception:
            image_hash = None
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO posts (text, timestamp, image_hash) VALUES (?, ?, ?)",
            (text, datetime.now().isoformat(), image_hash)
        )
        await db.commit()
def reset_timer(timeout=None):
    pending_post["timer"] = datetime.now()
    if timeout:
        pending_post["timeout"] = timeout

async def check_timer():
    while True:
        await asyncio.sleep(0.5)
        if pending_post["active"] and pending_post.get("timer"):
            passed = (datetime.now() - pending_post["timer"]).total_seconds()
            if passed > pending_post.get("timeout", TIMER_PUBLISH_DEFAULT):
                try:
                    base_text = post_data["text_ru"].strip()
                    telegram_text = build_telegram_post(base_text)
                    twitter_text = build_twitter_post(base_text)
                    await approval_bot.send_message(
                        chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                        text="⌛ Время ожидания истекло. Публикую автоматически."
                    )
                    await publish_post_to_telegram(
                        channel_bot, TELEGRAM_CHANNEL_USERNAME_ID, telegram_text, post_data["image_url"]
                    )
                    publish_post_to_twitter(twitter_text, post_data["image_url"])
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
                        text=f"❌ Ошибка при автопубликации: {e}"
                    )
                    await approval_bot.send_message(
                        chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                        text="Выберите действие:",
                        reply_markup=post_end_keyboard()
                    )
                pending_post["active"] = False

def generate_random_schedule(posts_per_day=6, day_start_hour=6, day_end_hour=23, min_offset=-20, max_offset=20):
    if day_end_hour > 23:
        day_end_hour = 23
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

async def schedule_daily_posts():
    global manual_posts_today
    while True:
        manual_posts_today = 0
        now = datetime.now()
        if now.hour < 6:
            to_sleep = (datetime.combine(now.date(), dt_time(hour=6)) - now).total_seconds()
            await asyncio.sleep(to_sleep)
        posts_left = lambda: scheduled_posts_per_day - manual_posts_today
        while posts_left() > 0:
            schedule = generate_random_schedule(posts_per_day=posts_left())
            for post_time in schedule:
                if posts_left() <= 0:
                    break
                now = datetime.now()
                delay = (post_time - now).total_seconds()
                if delay > 0:
                    await asyncio.sleep(delay)
                post_data["text_ru"] = f"Новый пост ({post_time.strftime('%H:%M:%S')})"
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
            if not str(post_data["image_url"]).startswith("http"):
                url = await process_telegram_photo(post_data["image_url"], approval_bot)
                post_data["image_url"] = url
            await safe_preview_post(
                approval_bot,
                TELEGRAM_APPROVAL_CHAT_ID,
                post_data["text_ru"] + "\n\n" + WELCOME_HASHTAGS,
                image_url=post_data["image_url"],
                reply_markup=main_keyboard()
            )
        except Exception as e:
            logging.error(f"Ошибка при отправке на согласование: {e}")

# --- Self post / edit / router ---
SESSION_KEY = "self_approval"

async def self_post_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = SESSION_KEY
    state = user_self_post.get(key, {}).get('state')
    if state not in ['wait_post', 'wait_confirm']:
        await approval_bot.send_message(
            chat_id=update.effective_chat.id,
            text="✍️ Чтобы отправить свой пост, сначала нажми кнопку 'Сделай сам'!"
        )
        return

    text = update.message.text or update.message.caption or ""
    image_url = None
    if update.message.photo:
        try:
            image_url = await process_telegram_photo(update.message.photo[-1].file_id, approval_bot)
        except Exception as e:
            await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="❌ Не удалось обработать фото. Попробуйте ещё раз.")
            return

    if not text and not image_url:
        await approval_bot.send_message(chat_id=update.effective_chat.id, text="❗️Пришлите хотя бы текст или фотографию для поста.")
        return

    user_self_post[key]['text'] = text
    user_self_post[key]['image'] = image_url
    user_self_post[key]['state'] = 'wait_confirm'

    try:
        await preview_dual(
            approval_bot,
            TELEGRAM_APPROVAL_CHAT_ID,
            text,
            image_url=image_url,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📤 Завершить генерацию поста", callback_data="finish_self_post")],
                [InlineKeyboardButton("❌ Отмена", callback_data="cancel_to_main")]
            ])
        )
    except Exception:
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="❌ Не удалось показать предпросмотр поста. Попробуйте снова.")

async def edit_post_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = SESSION_KEY
    if update.message.reply_to_message and update.message.reply_to_message.from_user.is_bot:
        text = update.message.text or update.message.caption or None
        image_url = None
        if update.message.photo:
            image_url = await process_telegram_photo(update.message.photo[-1].file_id, approval_bot)
        if text:
            post_data["text_ru"] = text
        if image_url:
            post_data["image_url"] = image_url
        try:
            await preview_dual(
                approval_bot,
                TELEGRAM_APPROVAL_CHAT_ID,
                post_data["text_ru"],
                image_url=post_data["image_url"],
                reply_markup=post_choice_keyboard()
            )
        except Exception:
            pass
        return

    if key in user_self_post and user_self_post[key]['state'] == 'wait_edit':
        text = update.message.text or update.message.caption or None
        image_url = None
        if update.message.photo:
            image_url = await process_telegram_photo(update.message.photo[-1].file_id, approval_bot)
        if text:
            post_data["text_ru"] = text
        if image_url:
            post_data["image_url"] = image_url
        user_self_post.pop(key, None)
        try:
            await preview_dual(
                approval_bot,
                TELEGRAM_APPROVAL_CHAT_ID,
                post_data["text_ru"],
                image_url=post_data["image_url"],
                reply_markup=post_choice_keyboard()
            )
        except Exception:
            pass

async def message_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = SESSION_KEY
    if update.message.reply_to_message and update.message.reply_to_message.from_user.is_bot:
        await edit_post_message_handler(update, context)
        return

    if not user_self_post.get(key):
        user_self_post[key] = {'text': '', 'image': None, 'state': 'wait_post'}

    state = user_self_post[key]['state']
    if state == 'wait_edit':
        await edit_post_message_handler(update, context)
        return
    if state in ['wait_post', 'wait_confirm']:
        await self_post_message_handler(update, context)
        return
    await approval_bot.send_message(
        chat_id=update.effective_chat.id,
        text="✍️ Чтобы отправить свой пост, сначала нажми кнопку 'Сделай сам'!"
    )
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_action_time, prev_data, manual_posts_today
    key = SESSION_KEY
    try:
        await update.callback_query.answer()
    except Exception as e:
        logging.warning(f"Не удалось ответить на callback_query: {e}")
    if pending_post["active"]:
        reset_timer(TIMER_PUBLISH_EXTEND)
    user_id = update.effective_user.id
    now = datetime.now()
    if user_id in last_action_time and (now - last_action_time[user_id]).seconds < 3:
        logging.info(f"User {user_id} слишком часто нажимает кнопки")
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="⏳ Подождите немного...", reply_markup=main_keyboard())
        return
    last_action_time[user_id] = now
    action = update.callback_query.data
    prev_data.update(post_data)
    logging.info(f"[button_handler] action={action} user_id={user_id}")

    if action == "edit_post":
        try:
            await update.callback_query.message.delete()
        except Exception:
            pass
        user_self_post[key] = {'state': 'wait_edit'}
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="✏️ Пришли новый текст и/или фото для редактирования поста (в одном сообщении), либо просто отправь новое сообщение в reply на текущий предпросмотр.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cancel_to_main")]])
        )
        return

    if action == "finish_self_post":
        info = user_self_post.get(key)
        if not (info and info["state"] == "wait_confirm"):
            logging.warning(f"[button_handler] Некорректный вызов finish_self_post")
            return
        text = info.get("text", "")
        image_url = info.get("image", None)
        post_data["text_ru"] = text
        if image_url:
            post_data["image_url"] = image_url
        else:
            post_data["image_url"] = random.choice(test_images)
        post_data["post_id"] += 1
        post_data["is_manual"] = True
        user_self_post.pop(key, None)
        try:
            await update.callback_query.message.delete()
        except Exception:
            pass

        logging.info(f"[button_handler] finish_self_post: предпросмотр self-поста: text='{post_data['text_ru'][:60]}...', image_url={post_data['image_url']}")

        try:
            await preview_dual(
                approval_bot,
                TELEGRAM_APPROVAL_CHAT_ID,
                post_data["text_ru"],
                image_url=post_data["image_url"],
                reply_markup=post_choice_keyboard()
            )
        except Exception as e:
            logging.error(f"[button_handler] Ошибка предпросмотра после finish_self_post: {e}")

        pending_post.update({
            "active": True,
            "timer": datetime.now(),
            "timeout": TIMER_PUBLISH_DEFAULT
        })
        return

    if action == "shutdown_bot":
        logging.info("Останавливаю бота по кнопке!")
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="🔴 Бот полностью выключен. GitHub Actions больше не тратит минуты!")
        await asyncio.sleep(2)
        shutdown_bot_and_exit()
        return

    if action == "approve":
        await preview_dual(
            approval_bot,
            TELEGRAM_APPROVAL_CHAT_ID,
            post_data["text_ru"],
            image_url=post_data["image_url"],
            reply_markup=post_choice_keyboard()
        )
        logging.info("approve: предпросмотр успешно отправлен")
        pending_post.update({
            "active": True,
            "timer": datetime.now(),
            "timeout": TIMER_PUBLISH_DEFAULT
        })
        return

    if action in ["post_twitter", "post_telegram", "post_both"]:
        pending_post.update({
            "active": True,
            "timer": datetime.now(),
            "timeout": TIMER_PUBLISH_DEFAULT
        })

        base_text = post_data["text_ru"].strip()
        telegram_text = build_telegram_post(base_text)
        twitter_text = build_twitter_post(base_text)

        telegram_success = False
        twitter_success = False

        if action in ["post_telegram", "post_both"]:
            try:
                telegram_success = await publish_post_to_telegram(channel_bot, TELEGRAM_CHANNEL_USERNAME_ID, telegram_text, post_data["image_url"])
            except Exception as e:
                logging.error(f"Ошибка при публикации в Telegram: {e}")
                await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"❌ Не удалось отправить в Telegram: {e}")

        if action in ["post_twitter", "post_both"]:
            try:
                twitter_success = publish_post_to_twitter(twitter_text, post_data["image_url"])
            except Exception as e:
                logging.error(f"Ошибка при публикации в Twitter: {e}")
                await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"❌ Не удалось отправить в Twitter: {e}")

        pending_post["active"] = False
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="✅ Успешно отправлено в Telegram!" if telegram_success else "❌ Не удалось отправить в Telegram.")
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="✅ Успешно отправлено в Twitter!" if twitter_success else "❌ Не удалось отправить в Twitter.")
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="Работа завершена.", reply_markup=post_end_keyboard())
        shutdown_bot_and_exit()
        return

    if action == "self_post":
        try:
            await update.callback_query.message.delete()
        except Exception:
            pass
        user_self_post[key] = {'text': '', 'image': None, 'state': 'wait_post'}
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="✍️ Напиши свой текст поста и (опционально) приложи фото — всё одним сообщением. После этого появится предпросмотр с кнопками.")
        return

    if action == "cancel_to_main":
        try:
            await update.callback_query.message.delete()
        except Exception:
            pass
        user_self_post.pop(key, None)
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="Главное меню:", reply_markup=main_keyboard())
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
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="💬 Начинаем чат:\n" + post_data["text_ru"], reply_markup=post_end_keyboard())
        return

    if action == "do_not_disturb":
        do_not_disturb["active"] = not do_not_disturb["active"]
        status = "включён" if do_not_disturb["active"] else "выключен"
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"🌙 Режим «Не беспокоить» {status}.", reply_markup=post_end_keyboard())
        return

    if action == "new_post":
        pending_post["active"] = False
        post_data["text_ru"] = f"Тестовый новый пост #{post_data['post_id'] + 1}"
        post_data["image_url"] = random.choice(test_images)
        post_data["post_id"] += 1
        post_data["is_manual"] = False
        await safe_preview_post(
            approval_bot,
            TELEGRAM_APPROVAL_CHAT_ID,
            post_data["text_ru"] + "\n\n" + WELCOME_HASHTAGS,
            image_url=post_data["image_url"],
            reply_markup=main_keyboard()
        )
        pending_post.update({
            "active": True,
            "timer": datetime.now(),
            "timeout": TIMER_PUBLISH_DEFAULT
        })
        return

    if action == "new_post_manual":
        pending_post["active"] = False
        post_data["text_ru"] = f"Ручной новый пост #{post_data['post_id'] + 1}"
        post_data["image_url"] = random.choice(test_images)
        post_data["post_id"] += 1
        post_data["is_manual"] = True
        await safe_preview_post(
            approval_bot,
            TELEGRAM_APPROVAL_CHAT_ID,
            post_data["text_ru"] + "\n\n" + WELCOME_HASHTAGS,
            image_url=post_data["image_url"],
            reply_markup=main_keyboard()
        )
        pending_post.update({
            "active": True,
            "timer": datetime.now(),
            "timeout": TIMER_PUBLISH_DEFAULT
        })
        return

def publish_post_to_twitter(text, image_url=None):
    github_filename = None
    try:
        media_ids = None
        file_path = None
        if image_url:
            if not str(image_url).startswith("http"):
                logging.error("Telegram file_id не поддерживается напрямую для Twitter публикации.")
                return False
            r = requests.get(image_url, headers={'User-Agent': 'Mozilla/5.0'})
            r.raise_for_status()
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
            tmp.write(r.content)
            tmp.close()
            file_path = tmp.name

        if file_path:
            media = twitter_api_v1.media_upload(file_path)
            media_ids = [media.media_id_string]
            os.remove(file_path)

        twitter_client_v2.create_tweet(text=text, media_ids=media_ids)
        if image_url and image_url.startswith(f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{GITHUB_IMAGE_PATH}/"):
            github_filename = image_url.split('/')[-1]
            delete_image_from_github(github_filename)
        return True
    except Exception as e:
        pending_post["active"] = False
        logging.error(f"Ошибка публикации в Twitter: {e}")
        asyncio.create_task(approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text=f"❌ Ошибка при публикации в Twitter: {e}"
        ))
        if github_filename:
            delete_image_from_github(github_filename)
        return False

async def publish_post_to_telegram(bot, chat_id, text, image_url):
    github_filename = None
    try:
        msg, github_filename = await send_photo_with_download(bot, chat_id, image_url, caption=text)
        if github_filename:
            delete_image_from_github(github_filename)
        return True
    except Exception as e:
        logging.error(f"Ошибка при публикации в Telegram: {e}")
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"❌ Ошибка при публикации в Telegram: {e}")
        if github_filename:
            delete_image_from_github(github_filename)
        return False

async def delayed_start(app: Application):
    await init_db()
    asyncio.create_task(schedule_daily_posts())
    asyncio.create_task(check_timer())
    await safe_preview_post(
        approval_bot,
        TELEGRAM_APPROVAL_CHAT_ID,
        post_data["text_ru"] + "\n\n" + WELCOME_HASHTAGS,
        image_url=post_data["image_url"],
        reply_markup=main_keyboard()
    )
    logging.info("Бот успешно запущен и готов принимать сообщения")

def shutdown_bot_and_exit():
    try:
        asyncio.create_task(approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="🔴 Бот полностью выключен. GitHub Actions больше не тратит минуты!"
        ))
    except Exception:
        pass
    import time; time.sleep(2)
    os._exit(0)

def main():
    app = Application.builder()\
        .token(TELEGRAM_BOT_TOKEN_APPROVAL)\
        .post_init(delayed_start)\
        .build()
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, message_router))
    app.run_polling(poll_interval=0.12, timeout=1)

if __name__ == "__main__":
    main()