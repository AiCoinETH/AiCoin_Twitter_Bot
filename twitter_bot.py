import os
import asyncio
import hashlib
import logging
import random
import sys
import tempfile
from datetime import datetime, timedelta, time as dt_time

import tweepy
import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, Bot
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters
import aiosqlite

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

# --- Переменные окружения ---
TELEGRAM_BOT_TOKEN_APPROVAL = os.getenv("TELEGRAM_BOT_TOKEN_APPROVAL")
TELEGRAM_APPROVAL_CHAT_ID = os.getenv("TELEGRAM_APPROVAL_CHAT_ID")
TELEGRAM_BOT_TOKEN_CHANNEL = os.getenv("TELEGRAM_BOT_TOKEN_CHANNEL")
TELEGRAM_CHANNEL_USERNAME_ID = os.getenv("TELEGRAM_CHANNEL_USERNAME_ID")

TWITTER_API_KEY = os.getenv("TWITTER_API_KEY")
TWITTER_API_SECRET = os.getenv("TWITTER_API_SECRET")
TWITTER_ACCESS_TOKEN = os.getenv("TWITTER_ACCESS_TOKEN")
TWITTER_ACCESS_TOKEN_SECRET = os.getenv("TWITTER_ACCESS_TOKEN_SECRET")

TELEGRAM_PHOTO_LIMIT = 10 * 1024 * 1024  # 10 MB
TELEGRAM_CAPTION_LIMIT = 1024

TELEGRAM_LINKS = "Веб сайт: https://getaicoin.com/ | Twitter: https://x.com/AiCoin_ETH"

if not all([TELEGRAM_BOT_TOKEN_APPROVAL, TELEGRAM_APPROVAL_CHAT_ID, TELEGRAM_BOT_TOKEN_CHANNEL, TELEGRAM_CHANNEL_USERNAME_ID]):
    logging.error("Не заданы обязательные переменные окружения Telegram!")
    sys.exit(1)
if not all([TWITTER_API_KEY, TWITTER_API_SECRET, TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_TOKEN_SECRET]):
    logging.error("Не заданы обязательные переменные окружения для Twitter!")
    sys.exit(1)

approval_bot = Bot(token=TELEGRAM_BOT_TOKEN_APPROVAL)
channel_bot = Bot(token=TELEGRAM_BOT_TOKEN_CHANNEL)

approval_lock = asyncio.Lock()
DB_FILE = "post_history.db"
MAX_HISTORY_POSTS = 15
MANUAL_POSTS_PER_DAY = 6
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

pending_post = {"active": False, "timer": None, "timeout": TIMER_PUBLISH_DEFAULT}
do_not_disturb = {"active": False}
last_action_time = {}

# --- Главное меню ---
def main_keyboard(timer: int = None):
    think_label = "🕒 Подумать" if timer is None else f"🕒 Думаем... {timer} сек"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Пост", callback_data="approve")],
        [InlineKeyboardButton("✍️ Сделай сам", callback_data="self_post")],
        [InlineKeyboardButton(think_label, callback_data="think")],
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

# --- Twitter ---
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
    signature = (
        "\nПодробнее в Telegram: t.me/AiCoin_ETH или на сайте: https://getaicoin.com/ "
        "#AiCoin #Ai $Ai #crypto #blockchain #AI #DeFi"
    )
    max_length = 280
    reserve = max_length - len(signature)
    if len(text_ru) > reserve:
        main_part = text_ru[:reserve - 3].rstrip() + "..."
    else:
        main_part = text_ru
    return main_part + signature

def build_telegram_post(text: str) -> str:
    links = "\n\n" + TELEGRAM_LINKS
    reserve = TELEGRAM_CAPTION_LIMIT - len(links)
    if len(text) > reserve:
        text = text[:reserve - 3].rstrip() + "..."
    return text + links

def hash_text(text: str):
    return hashlib.sha256(text.strip().encode('utf-8')).hexdigest()

def hash_image(img_path: str):
    with open(img_path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()

async def is_duplicate_post(text, image_url, db_file=DB_FILE):
    text_hash = hash_text(text)
    img_hash = None
    try:
        if image_url and str(image_url).startswith("http"):
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
            r = requests.get(image_url, headers={'User-Agent': 'Mozilla/5.0'})
            tmp.write(r.content)
            tmp.close()
            img_hash = hash_image(tmp.name)
            os.remove(tmp.name)
        elif image_url:
            img_hash = image_url
    except Exception:
        img_hash = None

    async with aiosqlite.connect(db_file) as db:
        async with db.execute("SELECT text_hash, image_hash FROM posts ORDER BY id DESC LIMIT ?", (MAX_HISTORY_POSTS,)) as cursor:
            async for row in cursor:
                if text_hash == row[0]:
                    return True
                if img_hash and img_hash == row[1]:
                    return True
    return False

async def save_post_to_db(text, image_url, db_file=DB_FILE):
    text_hash = hash_text(text)
    img_hash = None
    try:
        if image_url and str(image_url).startswith("http"):
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
            r = requests.get(image_url, headers={'User-Agent': 'Mozilla/5.0'})
            tmp.write(r.content)
            tmp.close()
            img_hash = hash_image(tmp.name)
            os.remove(tmp.name)
        elif image_url:
            img_hash = image_url
    except Exception:
        img_hash = None

    async with aiosqlite.connect(db_file) as db:
        await db.execute("INSERT INTO posts (text, timestamp, text_hash, image_hash) VALUES (?, ?, ?, ?)", (
            text, datetime.now().isoformat(), text_hash, img_hash
        ))
        await db.commit()
        await db.execute(f"DELETE FROM posts WHERE id NOT IN (SELECT id FROM posts ORDER BY id DESC LIMIT {MAX_HISTORY_POSTS})")
        await db.commit()

# --- Скачивание картинки ---
def download_image(url_or_file_id, is_telegram_file=False, bot=None):
    if is_telegram_file:
        loop = asyncio.get_event_loop()
        file = loop.run_until_complete(bot.get_file(url_or_file_id))
        file_url = file.file_path if file.file_path.startswith("http") else f"https://api.telegram.org/file/bot{bot.token}/{file.file_path}"
        r = requests.get(file_url)
        r.raise_for_status()
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
        tmp.write(r.content)
        tmp.close()
        if os.path.getsize(tmp.name) > TELEGRAM_PHOTO_LIMIT:
            raise ValueError("❗️Файл слишком большой для Telegram (>10MB)!")
        return tmp.name
    else:
        headers = {'User-Agent': 'Mozilla/5.0'}
        r = requests.get(url_or_file_id, headers=headers)
        r.raise_for_status()
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
        tmp.write(r.content)
        tmp.close()
        if os.path.getsize(tmp.name) > TELEGRAM_PHOTO_LIMIT:
            raise ValueError("❗️Файл слишком большой для Telegram (>10MB)!")
        return tmp.name

async def send_photo_with_download(bot, chat_id, url_or_file_id, caption=None):
    file_path = None
    try:
        is_telegram = not (str(url_or_file_id).startswith("http"))
        file_path = download_image(url_or_file_id, is_telegram, bot if is_telegram else None)
        msg = await bot.send_photo(chat_id=chat_id, photo=open(file_path, "rb"), caption=caption)
        return msg
    except ValueError as ve:
        await bot.send_message(chat_id=chat_id, text=str(ve), disable_web_page_preview=True)
        logging.error(str(ve))
        if caption:
            await bot.send_message(chat_id=chat_id, text=caption, disable_web_page_preview=True)
    except Exception as e:
        await bot.send_message(chat_id=chat_id, text=f"❗️Ошибка: {e}", disable_web_page_preview=True)
        logging.error(str(e))
    finally:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)

async def publish_post_to_telegram(bot, chat_id, text, image_url):
    try:
        if image_url:
            await send_photo_with_download(bot, chat_id, image_url, caption=text)
        else:
            await bot.send_message(chat_id=chat_id, text=text, disable_web_page_preview=True)
        logging.info("Пост успешно опубликован в Telegram!")
        return True
    except Exception as e:
        logging.error(f"Ошибка при публикации в Telegram: {e}")
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text=f"❌ Ошибка при публикации в Telegram: {e}",
            disable_web_page_preview=True
        )
        return False

async def publish_message_with_no_preview(bot, chat_id, text):
    await bot.send_message(chat_id=chat_id, text=text, disable_web_page_preview=True)

def publish_post_to_twitter(text, image_url=None):
    try:
        media_ids = None
        if image_url:
            is_telegram = not (str(image_url).startswith("http"))
            file_path = download_image(image_url, is_telegram, approval_bot if is_telegram else None)
            try:
                media = twitter_api_v1.media_upload(file_path)
                media_ids = [media.media_id_string]
            finally:
                if file_path and os.path.exists(file_path):
                    os.remove(file_path)
        twitter_client_v2.create_tweet(text=text, media_ids=media_ids)
        logging.info("Пост успешно опубликован в Twitter!")
        return True
    except Exception as e:
        pending_post["active"] = False
        logging.error(f"Ошибка публикации в Twitter: {e}")
        asyncio.create_task(approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text=f"❌ Ошибка при публикации в Twitter: {e}\nПроверьте ключи/токены, лимиты публикаций, формат медиа и права доступа.",
            disable_web_page_preview=True
        ))
        return False

def shutdown_bot_and_exit():
    logging.info("Завершение работы бота через shutdown_bot_and_exit() и exit")
    try:
        asyncio.create_task(
            approval_bot.send_message(
                chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                text="🔴 Бот полностью выключен. GitHub Actions больше не тратит минуты!",
                disable_web_page_preview=True
            )
        )
    except Exception:
        pass
    import time; time.sleep(2)
    os._exit(0)

async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                text_hash TEXT,
                image_hash TEXT
            )
            """
        )
        await db.commit()
    logging.info("База данных инициализирована.")

# --- Self-пост ---
async def self_post_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in user_self_post or user_self_post[user_id].get('state') != 'wait_post':
        return

    text = update.message.text or ""
    image = None
    if update.message.photo:
        image = update.message.photo[-1].file_id

    links = "\n\n" + TELEGRAM_LINKS
    max_caption = TELEGRAM_CAPTION_LIMIT
    reserve = max_caption - len(links)
    if len(text) > reserve:
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text=f"❗️Длина поста превышает лимит для Telegram ({max_caption} символов с учетом ссылок). Ваш текст: {len(text)} символов, доступно: {reserve}.\nУкоротите сообщение!",
            disable_web_page_preview=True
        )
        return

    # Проверка на дубликат!
    if await is_duplicate_post(text, image):
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="❗️Этот пост или картинка уже были опубликованы за последние 15 постов. Измени текст или прикрепи другую картинку.",
            disable_web_page_preview=True
        )
        return

    user_self_post[user_id]['text'] = text
    user_self_post[user_id]['image'] = image
    user_self_post[user_id]['state'] = 'wait_confirm'

    preview = build_telegram_post(text)
    if image:
        await send_photo_with_download(
            approval_bot,
            TELEGRAM_APPROVAL_CHAT_ID,
            image,
            caption=preview
        )
    elif text:
        await publish_message_with_no_preview(
            approval_bot,
            TELEGRAM_APPROVAL_CHAT_ID,
            preview
        )
    else:
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="(пустое сообщение)",
            disable_web_page_preview=True
        )

    await approval_bot.send_message(
        chat_id=TELEGRAM_APPROVAL_CHAT_ID,
        text="Проверь пост. Если всё ок — нажми 📤 Завершить генерацию.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 Завершить генерацию поста", callback_data="finish_self_post")],
            [InlineKeyboardButton("❌ Отмена", callback_data="cancel_to_main")]
        ]),
        disable_web_page_preview=True
    )

# --- Обработка кнопок ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_action_time, prev_data, manual_posts_today
    await update.callback_query.answer()
    if pending_post["active"]:
        pending_post["timer"] = datetime.now()
    user_id = update.effective_user.id
    now = datetime.now()
    if user_id in last_action_time and (now - last_action_time[user_id]).seconds < 3:
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="⏳ Подождите немного...", reply_markup=main_keyboard(), disable_web_page_preview=True)
        return
    last_action_time[user_id] = now
    action = update.callback_query.data
    prev_data.update(post_data)

    if action == "finish_self_post":
        info = user_self_post.get(user_id)
        if info and info["state"] == "wait_confirm":
            text = info.get("text", "")
            image = info.get("image", None)
            post_data["text_ru"] = text
            post_data["image_url"] = image if image else None  # только если приложено!
            post_data["post_id"] += 1
            post_data["is_manual"] = True
            user_self_post.pop(user_id, None)

            twitter_preview = build_twitter_post(text)

            try:
                await approval_bot.send_message(
                    chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                    text="Финальный пост для Twitter:\n\n" + twitter_preview,
                    reply_markup=post_choice_keyboard(),
                    disable_web_page_preview=True
                )
                logging.info("Показан финальный Twitter-пост с выбором площадки.")
            except Exception as e:
                logging.error(f"Ошибка отправки меню выбора площадки: {e}")
                await approval_bot.send_message(
                    chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                    text=twitter_preview + "\n\n(Не удалось показать меню выбора площадки)",
                    disable_web_page_preview=True
                )
        else:
            await update.callback_query.answer("Ошибка: состояние не позволяет завершить генерацию.", show_alert=True)
        return

    if action == "shutdown_bot":
        logging.info("Останавливаю бота по кнопке!")
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="🔴 Бот полностью выключен. GitHub Actions больше не тратит минуты!",
            disable_web_page_preview=True
        )
        await asyncio.sleep(2)
        shutdown_bot_and_exit()
        return

    if action == "approve":
        twitter_text = build_twitter_post(post_data["text_ru"])
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="Финальный пост для Twitter:\n\n" + twitter_text,
            reply_markup=post_choice_keyboard(),
            disable_web_page_preview=True
        )
        return

    if action in ["post_twitter", "post_telegram", "post_both"]:
        base_text = post_data["text_ru"].strip()
        telegram_text = build_telegram_post(base_text)
        twitter_text = build_twitter_post(base_text)

        if await is_duplicate_post(base_text, post_data["image_url"]):
            await approval_bot.send_message(
                chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                text="❗️Этот пост или картинка уже были опубликованы за последние 15 постов. Измени текст или прикрепи другую картинку.",
                disable_web_page_preview=True
            )
            return

        telegram_success = False
        twitter_success = False

        if action in ["post_telegram", "post_both"]:
            try:
                telegram_success = await publish_post_to_telegram(channel_bot, TELEGRAM_CHANNEL_USERNAME_ID, telegram_text, post_data["image_url"])
            except Exception as e:
                logging.error(f"Ошибка при публикации в Telegram: {e}")
                await approval_bot.send_message(
                    chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                    text=f"❌ Не удалось отправить в Telegram: {e}",
                    reply_markup=None,
                    disable_web_page_preview=True
                )

        if action in ["post_twitter", "post_both"]:
            try:
                twitter_success = publish_post_to_twitter(twitter_text, post_data["image_url"])
            except Exception as e:
                logging.error(f"Ошибка при публикации в Twitter: {e}")
                await approval_bot.send_message(
                    chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                    text=f"❌ Не удалось отправить в Twitter: {e}",
                    reply_markup=None,
                    disable_web_page_preview=True
                )

        pending_post["active"] = False

        if telegram_success or twitter_success:
            await save_post_to_db(base_text, post_data["image_url"])

        if telegram_success:
            await approval_bot.send_message(
                chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                text="✅ Успешно отправлено в Telegram!",
                reply_markup=None,
                disable_web_page_preview=True
            )
        else:
            await approval_bot.send_message(
                chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                text="❌ Не удалось отправить в Telegram.",
                reply_markup=None,
                disable_web_page_preview=True
            )

        if twitter_success:
            await approval_bot.send_message(
                chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                text="✅ Успешно отправлено в Twitter!",
                reply_markup=None,
                disable_web_page_preview=True
            )
        else:
            await approval_bot.send_message(
                chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                text="❌ Не удалось отправить в Twitter.",
                reply_markup=None,
                disable_web_page_preview=True
            )

        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="Работа завершена.",
            reply_markup=post_end_keyboard(),
            disable_web_page_preview=True
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
            text="✍️ Напиши свой текст поста и (опционально) приложи фото — всё одним сообщением. После этого появится предпросмотр с кнопками.",
            disable_web_page_preview=True
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
            reply_markup=main_keyboard(),
            disable_web_page_preview=True
        )
        return

    if action == "restore_previous":
        post_data.update(prev_data)
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="↩️ Восстановлен предыдущий вариант.", reply_markup=main_keyboard(), disable_web_page_preview=True)
        if pending_post["active"]:
            await send_post_for_approval()
        return

    if action == "end_day":
        pending_post["active"] = False
        do_not_disturb["active"] = True
        tomorrow = datetime.combine(datetime.now().date() + timedelta(days=1), dt_time(hour=9))
        kb = main_keyboard()
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"🔚 Работа завершена на сегодня.\nСледующая публикация: {tomorrow.strftime('%Y-%m-%d %H:%M')}", parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
        return

    if action == "think":
        if pending_post.get("active") and pending_post.get("timer"):
            seconds_left = pending_post["timeout"] - int((datetime.now() - pending_post["timer"]).total_seconds())
            seconds_left = max(seconds_left, 0)
        else:
            seconds_left = TIMER_PUBLISH_DEFAULT
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"🧐 Думаем дальше… До автопубликации {seconds_left} сек", reply_markup=main_keyboard(timer=seconds_left), disable_web_page_preview=True)
        return

    if action == "chat":
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="💬 Начинаем чат:\n" + post_data["text_ru"],
            reply_markup=post_end_keyboard(),
            disable_web_page_preview=True
        )
        return

    if action == "do_not_disturb":
        do_not_disturb["active"] = not do_not_disturb["active"]
        status = "включён" if do_not_disturb["active"] else "выключен"
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text=f"🌙 Режим «Не беспокоить» {status}.",
            reply_markup=post_end_keyboard(),
            disable_web_page_preview=True
        )
        return

    if action == "new_post":
        pending_post["active"] = False
        post_data["text_ru"] = f"Тестовый новый пост #{post_data['post_id'] + 1}"
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
            preview = build_telegram_post(post_data["text_ru"])
            await send_photo_with_download(
                approval_bot,
                TELEGRAM_APPROVAL_CHAT_ID,
                post_data["image_url"],
                caption=preview
            )
            logging.info("Пост отправлен на согласование.")
        except Exception as e:
            logging.error(f"Ошибка при отправке на согласование: {e}")

async def delayed_start(app: Application):
    await init_db()
    await send_photo_with_download(
        approval_bot,
        TELEGRAM_APPROVAL_CHAT_ID,
        post_data["image_url"],
        caption=build_telegram_post(post_data["text_ru"])
    )
    await approval_bot.send_message(
        chat_id=TELEGRAM_APPROVAL_CHAT_ID,
        text="Добро пожаловать! Выберите действие:",
        reply_markup=main_keyboard(),
        disable_web_page_preview=True
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