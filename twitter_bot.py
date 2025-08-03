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
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    Bot,
    InputMediaPhoto,
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

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(funcName)s %(message)s'
)

# --- Переменные окружения ---
TELEGRAM_BOT_TOKEN_APPROVAL = os.getenv("TELEGRAM_BOT_TOKEN_APPROVAL")
TELEGRAM_APPROVAL_CHAT_ID = int(os.getenv("TELEGRAM_APPROVAL_CHAT_ID"))
TELEGRAM_BOT_TOKEN_CHANNEL = os.getenv("TELEGRAM_BOT_TOKEN_CHANNEL")
TELEGRAM_CHANNEL_USERNAME_ID = os.getenv("TELEGRAM_CHANNEL_USERNAME_ID")
TWITTER_API_KEY = os.getenv("TWITTER_API_KEY")
TWITTER_API_SECRET = os.getenv("TWITTER_API_SECRET")
TWITTER_ACCESS_TOKEN = os.getenv("TWITTER_ACCESS_TOKEN")
TWITTER_ACCESS_TOKEN_SECRET = os.getenv("TWITTER_ACCESS_TOKEN_SECRET")
GITHUB_TOKEN = os.getenv("ACTION_PAT_GITHUB")
GITHUB_REPO = os.getenv("ACTION_REPO_GITHUB")
GITHUB_IMAGE_PATH = "images_for_posts"

if not all([TELEGRAM_BOT_TOKEN_APPROVAL, TELEGRAM_APPROVAL_CHAT_ID, TELEGRAM_BOT_TOKEN_CHANNEL, TELEGRAM_CHANNEL_USERNAME_ID]):
    logging.error("Не заданы обязательные переменные окружения Telegram!")
    sys.exit(1)
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
WELCOME_HASHTAGS = "#AiCoin #AI #crypto #тренды #бот #новости"

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

user_edit_state = {}
edit_message_id = {}

pending_post = {"active": False, "timer": None, "timeout": TIMER_PUBLISH_DEFAULT}
do_not_disturb = {"active": False}
last_action_time = {}

github_client = Github(GITHUB_TOKEN)
github_repo = github_client.get_repo(GITHUB_REPO)

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

def build_twitter_post(text_ru: str) -> str:
    # Подпись с ссылкой на сайт и Telegram, лимит 280 символов
    signature = (
        "\nLearn more: https://getaicoin.com/ | Telegram: https://t.me/AiCoin_ETH #AiCoin #Ai $Ai #crypto #blockchain #AI #DeFi"
    )
    max_length = 280
    reserve = max_length - len(signature)
    if len(text_ru) > reserve:
        main_part = text_ru[:reserve - 3].rstrip() + "..."
    else:
        main_part = text_ru
    return main_part + signature

def build_telegram_post(text_ru: str) -> str:
    # Подпись с кликабельными ссылками на сайт и Telegram, лимит 1024 символа
    signature = (
        '\n\nLearn more on <a href="https://getaicoin.com/">Website</a> | '
        '<a href="https://t.me/AiCoin_ETH">Telegram</a>'
    )
    max_length = 1024
    reserve = max_length - len(signature)
    if len(text_ru) > reserve:
        main_part = text_ru[:reserve - 3].rstrip() + "..."
    else:
        main_part = text_ru
    return main_part + signature

# --- ДАЛЬШЕ ВСЯ ЛОГИКА КАК БЫЛО! ---

# ... upload_image_to_github, delete_image_from_github, download_image_async, process_telegram_photo,
# send_photo_with_download, publish_post_to_telegram, publish_post_to_twitter,
# is_duplicate_post, save_post_to_history, message_router, button_handler, автопостинг,
# таймеры, send_post_for_approval, startup/shutdown, main()
#
# Просто продолжай свой оригинальный файл дальше, ничего не убирая и не меняя!

# Если тебе нужно чтобы я вставил абсолютно весь код без обрыва (от начала до самого конца, включая все функции, async def, main() и пр.),
# просто дай знать — и я скопирую тебе блоками, чтобы влезло в лимиты чата!

# Готов к полной заливке кода!
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
                logging.info(f"download_image_async: попытка {attempt+1} загрузки Telegram file_id={url_or_file_id}")
                file = await bot.get_file(url_or_file_id)
                tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
                await file.download_to_drive(tmp_file.name)
                logging.info(f"download_image_async: Telegram файл скачан во временный файл {tmp_file.name}")
                return tmp_file.name
            except Exception as e:
                logging.warning(f"Попытка {attempt + 1} загрузки Telegram файла не удалась: {e}")
                await asyncio.sleep(1)
        raise Exception("Не удалось скачать файл из Telegram после нескольких попыток")
    else:
        logging.info(f"download_image_async: Скачиваю изображение по URL: {url_or_file_id}")
        headers = {'User-Agent': 'Mozilla/5.0'}
        r = requests.get(url_or_file_id, headers=headers)
        r.raise_for_status()
        tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
        tmp_file.write(r.content)
        tmp_file.close()
        logging.info(f"download_image_async: Изображение сохранено во временный файл: {tmp_file.name}")
        return tmp_file.name

async def save_image_and_get_github_url(image_path):
    filename = f"{uuid.uuid4().hex}.jpg"
    logging.info(f"save_image_and_get_github_url: image_path={image_path}, filename={filename}")
    url = upload_image_to_github(image_path, filename)
    logging.info(f"save_image_and_get_github_url: url={url}")
    return url, filename

async def process_telegram_photo(file_id: str, bot: Bot) -> str:
    logging.info(f"process_telegram_photo: file_id={file_id}")
    file_path = await download_image_async(file_id, is_telegram_file=True, bot=bot)
    url, filename = await save_image_and_get_github_url(file_path)
    os.remove(file_path)
    if not url:
        raise Exception("Не удалось загрузить фото на GitHub")
    logging.info(f"process_telegram_photo: Получена ссылка на GitHub: {url}")
    return url

async def send_photo_with_download(bot, chat_id, url_or_file_id, caption=None, reply_markup=None, use_html=False):
    github_filename = None
    logging.info(f"send_photo_with_download: chat_id={chat_id}, url_or_file_id={url_or_file_id}, caption='{caption}'")
    try:
        if url_or_file_id and not str(url_or_file_id).startswith("http"):
            url = await process_telegram_photo(url_or_file_id, bot)
            github_filename = url.split('/')[-1]
            logging.info(f"send_photo_with_download: отправляю фото по url={url}, caption='{caption}'")
            msg = await bot.send_photo(chat_id=chat_id, photo=url, caption=caption, reply_markup=reply_markup, parse_mode="HTML" if use_html else None)
            return msg, github_filename
        elif url_or_file_id:
            logging.info(f"send_photo_with_download: отправляю фото по url_or_file_id={url_or_file_id}, caption='{caption}'")
            msg = await bot.send_photo(chat_id=chat_id, photo=url_or_file_id, caption=caption, reply_markup=reply_markup, parse_mode="HTML" if use_html else None)
            return msg, None
        else:
            msg = await bot.send_message(chat_id=chat_id, text=caption, reply_markup=reply_markup, parse_mode="HTML" if use_html else None)
            return msg, None
    except Exception as e:
        logging.error(f"Ошибка в send_photo_with_download: {e}")
        raise

async def publish_post_to_telegram(bot, chat_id, text, image_url):
    github_filename = None
    logging.info(f"publish_post_to_telegram: chat_id={chat_id}, text='{text}', image_url={image_url}")
    try:
        msg, github_filename = await send_photo_with_download(bot, chat_id, image_url, caption=text, use_html=True)
        logging.info("Пост успешно опубликован в Telegram!")
        if github_filename:
            delete_image_from_github(github_filename)
        return True
    except Exception as e:
        logging.error(f"Ошибка при публикации в Telegram: {e}")
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"❌ Ошибка при публикации в Telegram: {e}")
        if github_filename:
            delete_image_from_github(github_filename)
        return False

def publish_post_to_twitter(text, image_url=None):
    github_filename = None
    logging.info(f"publish_post_to_twitter: text='{text}', image_url={image_url}")
    try:
        media_ids = None
        file_path = None
        if image_url:
            if not str(image_url).startswith("http"):
                logging.error("Telegram file_id не поддерживается напрямую для Twitter публикации.")
                return False
            headers = {'User-Agent': 'Mozilla/5.0'}
            r = requests.get(image_url, headers=headers)
            r.raise_for_status()
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
            tmp.write(r.content)
            tmp.close()
            file_path = tmp.name
            logging.info(f"publish_post_to_twitter: Скачал картинку во временный файл {file_path}")

        if file_path:
            media = twitter_api_v1.media_upload(file_path)
            media_ids = [media.media_id_string]
            os.remove(file_path)
            logging.info(f"publish_post_to_twitter: media_ids={media_ids}")

        twitter_client_v2.create_tweet(text=text, media_ids=media_ids)
        logging.info("Пост успешно опубликован в Twitter!")

        if image_url and image_url.startswith(f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{GITHUB_IMAGE_PATH}/"):
            github_filename = image_url.split('/')[-1]
            delete_image_from_github(github_filename)
        return True
    except Exception as e:
        pending_post["active"] = False
        logging.error(f"Ошибка публикации в Twitter: {e}")
        asyncio.create_task(approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"❌ Ошибка при публикации в Twitter: {e}\nПроверьте ключи/токены, лимиты публикаций, формат медиа и права доступа."))
        if github_filename:
            delete_image_from_github(github_filename)
        return False

# ... далее будет: init_db, is_duplicate_post, save_post_to_history, message_router, button_handler, автопостинг, таймеры, send_post_for_approval, startup/shutdown, main()
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
    logging.info("База данных инициализирована.")

async def is_duplicate_post(text, image_url=None):
    image_hash = None
    try:
        if image_url:
            if not str(image_url).startswith("http"):
                file_path = await download_image_async(image_url, True, approval_bot)
                with open(file_path, "rb") as f:
                    image_hash = hashlib.sha256(f.read()).hexdigest()
                os.remove(file_path)
            else:
                r = requests.get(image_url, timeout=3)
                r.raise_for_status()
                image_hash = hashlib.sha256(r.content).hexdigest()
    except Exception as e:
        logging.warning(f"Не удалось получить хеш изображения для антидубля: {e}")
        image_hash = None

    async with aiosqlite.connect(DB_FILE) as db:
        if image_hash:
            query = "SELECT COUNT(*) FROM posts WHERE text=? OR image_hash=?"
            args = (text, image_hash)
        else:
            query = "SELECT COUNT(*) FROM posts WHERE text=?"
            args = (text,)
        async with db.execute(query, args) as cursor:
            row = await cursor.fetchone()
            return row[0] > 0

async def save_post_to_history(text, image_url=None):
    image_hash = None
    logging.info(f"save_post_to_history: text='{text}', image_url={image_url}")
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
        except Exception as e:
            logging.warning(f"Не удалось получить хеш изображения: {e}")
            image_hash = None
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("INSERT INTO posts (text, timestamp, image_hash) VALUES (?, ?, ?)", (text, datetime.now().isoformat(), image_hash))
        await db.commit()
    logging.info("Пост сохранён в историю.")

# ========= Роутер сообщений =========
async def message_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # Обработка режима редактирования
    if user_edit_state.get(user_id):
        text = update.message.text or update.message.caption or post_data["text_ru"]
        image_url = post_data["image_url"]
        if update.message.photo:
            image_url = await process_telegram_photo(update.message.photo[-1].file_id, approval_bot)
        post_data["text_ru"] = text
        post_data["image_url"] = image_url
        user_edit_state[user_id] = False

        # Редактируем исходное сообщение бота по message_id
        msg_id = edit_message_id.get(user_id)
        try:
            if image_url and str(image_url).startswith("http"):
                await approval_bot.edit_message_media(
                    chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                    message_id=msg_id,
                    media=InputMediaPhoto(media=image_url, caption=build_telegram_post(text)),
                    reply_markup=post_choice_keyboard()
                )
            else:
                await approval_bot.edit_message_text(
                    chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                    message_id=msg_id,
                    text=build_telegram_post(text),
                    reply_markup=post_choice_keyboard(),
                    parse_mode="HTML"
                )
            logging.info(f"Сообщение {msg_id} отредактировано успешно")
        except Exception as e:
            logging.error(f"Ошибка редактирования сообщения: {e}")
            # Если редактирование не удалось, отправим новое сообщение с кнопками и запомним message_id
            msg = await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=build_telegram_post(text), reply_markup=post_choice_keyboard(), parse_mode="HTML")
            edit_message_id[user_id] = msg.message_id
        return

    # Логика «Сделай сам» (ручной режим)
    if user_id in user_self_post:
        state = user_self_post[user_id].get('state')
        if state == 'wait_post':
            text = update.message.text or update.message.caption or ""
            image_url = None
            if update.message.photo:
                image_url = await process_telegram_photo(update.message.photo[-1].file_id, approval_bot)
            user_self_post[user_id]['text'] = text
            user_self_post[user_id]['image'] = image_url
            user_self_post[user_id]['state'] = 'wait_confirm'

            if await is_duplicate_post(text, image_url):
                await approval_bot.send_message(
                    chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                    text="⛔️ Такой пост уже был опубликован (дубль по тексту или фото)!",
                    reply_markup=main_keyboard()
                )
                user_self_post.pop(user_id, None)
                return

            try:
                if image_url:
                    await send_photo_with_download(
                        approval_bot,
                        TELEGRAM_APPROVAL_CHAT_ID,
                        image_url,
                        caption=build_telegram_post(text),
                        use_html=True
                    )
                elif text:
                    await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=build_telegram_post(text), parse_mode="HTML")
                await approval_bot.send_message(
                    chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                    text="Проверь пост. Если всё ок — нажми 📤 Завершить генерацию.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("📤 Завершить генерацию поста", callback_data="finish_self_post")],
                        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_to_main")]
                    ])
                )
            except Exception as e:
                logging.error(f"Ошибка предпросмотра 'Сделай сам': {e}")
            return
        elif state == 'wait_edit':
            text = update.message.text or update.message.caption or ""
            image_url = None
            if update.message.photo:
                image_url = await process_telegram_photo(update.message.photo[-1].file_id, approval_bot)
            if text:
                user_self_post[user_id]['text'] = text
            if image_url:
                user_self_post[user_id]['image'] = image_url
            user_self_post[user_id]['state'] = 'wait_confirm'
            try:
                await send_photo_with_download(
                    approval_bot,
                    TELEGRAM_APPROVAL_CHAT_ID,
                    user_self_post[user_id]['image'],
                    caption=build_telegram_post(user_self_post[user_id]['text']),
                    use_html=True
                )
                await approval_bot.send_message(
                    chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                    text="Проверь отредактированный пост. Если всё ок — нажми 📤 Завершить генерацию.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("📤 Завершить генерацию поста", callback_data="finish_self_post")],
                        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_to_main")]
                    ])
                )
            except Exception as e:
                logging.error(f"Ошибка предпросмотра после ручного редактирования: {e}")
            return

    return
    # ========== Callback/Кнопки ==========
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_action_time, prev_data, manual_posts_today
    try:
        await update.callback_query.answer()
    except Exception as e:
        logging.warning(f"Не удалось ответить на callback_query: {e}")

    if pending_post["active"]:
        reset_timer(TIMER_PUBLISH_EXTEND)
    user_id = update.effective_user.id
    now = datetime.now()
    if user_id in last_action_time and (now - last_action_time[user_id]).seconds < 3:
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="⏳ Подождите немного...", reply_markup=main_keyboard())
        return
    last_action_time[user_id] = now
    action = update.callback_query.data
    logging.info(f"button_handler: user_id={user_id}, action={action}")
    prev_data.update(post_data)

    if action == "edit_post":
        user_edit_state[user_id] = True
        sent_msg = await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text=build_telegram_post(post_data["text_ru"]),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cancel_to_main")]]),
            parse_mode="HTML"
        )
        edit_message_id[user_id] = sent_msg.message_id
        return

    if action == "finish_self_post":
        info = user_self_post.get(user_id)
        logging.info(f"button_handler: finish_self_post info={info}")
        if info and info["state"] == "wait_confirm":
            text = info.get("text", "")
            image_url = info.get("image", None)
            twitter_text = build_twitter_post(text)
            post_data["text_ru"] = text
            if image_url:
                post_data["image_url"] = image_url
            else:
                post_data["image_url"] = random.choice(test_images)
            post_data["post_id"] += 1
            post_data["is_manual"] = True
            user_self_post.pop(user_id, None)

            if await is_duplicate_post(text, image_url):
                await approval_bot.send_message(
                    chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                    text="⛔️ Такой пост уже был опубликован (дубль по тексту или фото)!",
                    reply_markup=main_keyboard()
                )
                return

            try:
                if image_url:
                    await send_photo_with_download(approval_bot, TELEGRAM_APPROVAL_CHAT_ID, image_url, caption=build_telegram_post(text), reply_markup=post_choice_keyboard(), use_html=True)
                else:
                    await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=build_telegram_post(text), reply_markup=post_choice_keyboard(), parse_mode="HTML")
            except Exception as e:
                logging.error(f"Ошибка предпросмотра после завершения 'Сделай сам': {e}")
        return

    if action == "shutdown_bot":
        logging.info("Останавливаю бота по кнопке!")
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="🔴 Бот полностью выключен. GitHub Actions больше не тратит минуты!")
        await asyncio.sleep(2)
        shutdown_bot_and_exit()
        return

    if action == "approve":
        twitter_text = build_twitter_post(post_data["text_ru"])
        msg, _ = await send_photo_with_download(approval_bot, TELEGRAM_APPROVAL_CHAT_ID, post_data["image_url"], caption=build_telegram_post(post_data["text_ru"]), use_html=True)
        edit_message_id[user_id] = msg.message_id
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="Выберите площадку:", reply_markup=post_choice_keyboard())
        return

    if action in ["post_twitter", "post_telegram", "post_both"]:
        base_text = post_data["text_ru"].strip()
        telegram_text = build_telegram_post(base_text)
        twitter_text = build_twitter_post(base_text)

        telegram_success = False
        twitter_success = False

        if action in ["post_telegram", "post_both"]:
            try:
                telegram_success = await publish_post_to_telegram(channel_bot, TELEGRAM_CHANNEL_USERNAME_ID, telegram_text, post_data["image_url"])
            except Exception as e:
                await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"❌ Не удалось отправить в Telegram: {e}")

        if action in ["post_twitter", "post_both"]:
            try:
                twitter_success = publish_post_to_twitter(twitter_text, post_data["image_url"])
            except Exception as e:
                await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"❌ Не удалось отправить в Twitter: {e}")

        await save_post_to_history(base_text, post_data["image_url"])

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
        user_self_post[user_id] = {'text': '', 'image': None, 'state': 'wait_post'}
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="✍️ Напиши свой текст поста и (опционально) приложи фото — всё одним сообщением. После этого появится предпросмотр с кнопками.")
        return

    if action == "cancel_to_main":
        try:
            await update.callback_query.message.delete()
        except Exception:
            pass
        user_self_post.pop(user_id, None)
        user_edit_state[user_id] = False
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
        msg, _ = await send_photo_with_download(
            approval_bot,
            TELEGRAM_APPROVAL_CHAT_ID,
            post_data["image_url"],
            caption=build_telegram_post(post_data["text_ru"] + "\n\n" + WELCOME_HASHTAGS),
            reply_markup=main_keyboard(),
            use_html=True
        )
        edit_message_id[user_id] = msg.message_id
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
        msg, _ = await send_photo_with_download(
            approval_bot,
            TELEGRAM_APPROVAL_CHAT_ID,
            post_data["image_url"],
            caption=build_telegram_post(post_data["text_ru"] + "\n\n" + WELCOME_HASHTAGS),
            reply_markup=main_keyboard(),
            use_html=True
        )
        edit_message_id[user_id] = msg.message_id
        pending_post.update({
            "active": True,
            "timer": datetime.now(),
            "timeout": TIMER_PUBLISH_DEFAULT
        })
        return

# ========== Автопостинг ===========
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
    logging.info(f"generate_random_schedule: {[(t.strftime('%H:%M:%S')) for t in schedule]}")
    return schedule

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

# ========== Таймеры/Автопубликация ===========
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
                    await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="⌛ Время ожидания истекло. Публикую автоматически.")
                    await publish_post_to_telegram(channel_bot, TELEGRAM_CHANNEL_USERNAME_ID, telegram_text, post_data["image_url"])
                    publish_post_to_twitter(twitter_text, post_data["image_url"])
                    await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="✅ Посты автоматически опубликованы в Telegram и Twitter.")
                    await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="Выберите действие:", reply_markup=post_end_keyboard())
                    shutdown_bot_and_exit()
                except Exception as e:
                    pending_post["active"] = False
                    await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"❌ Ошибка при автопубликации: {e}\nПроверьте ключи, лимиты, права бота, лимиты Twitter/Telegram.")
                    await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="Выберите действие:", reply_markup=post_end_keyboard())
                pending_post["active"] = False

def reset_timer(timeout=None):
    pending_post["timer"] = datetime.now()
    if timeout:
        pending_post["timeout"] = timeout

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
            msg, _ = await send_photo_with_download(
                approval_bot,
                TELEGRAM_APPROVAL_CHAT_ID,
                post_data["image_url"],
                caption=build_telegram_post(post_data["text_ru"] + "\n\n" + WELCOME_HASHTAGS),
                reply_markup=main_keyboard(),
                use_html=True
            )
            edit_message_id[0] = msg.message_id
        except Exception as e:
            logging.error(f"Ошибка при отправке на согласование: {e}")

# ============= Startup/shutdown =====================
async def delayed_start(app: Application):
    await init_db()
    asyncio.create_task(schedule_daily_posts())
    asyncio.create_task(check_timer())
    msg, _ = await send_photo_with_download(
        approval_bot,
        TELEGRAM_APPROVAL_CHAT_ID,
        post_data["image_url"],
        caption=build_telegram_post(post_data["text_ru"] + "\n\n" + WELCOME_HASHTAGS),
        reply_markup=main_keyboard(),
        use_html=True
    )
    edit_message_id[0] = msg.message_id

def shutdown_bot_and_exit():
    try:
        asyncio.create_task(approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="🔴 Бот полностью выключен. GitHub Actions больше не тратит минуты!"))
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
    