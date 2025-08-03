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

TELEGRAM_BOT_TOKEN_APPROVAL = os.getenv("TELEGRAM_BOT_TOKEN_APPROVAL")
TELEGRAM_APPROVAL_CHAT_ID = os.getenv("TELEGRAM_APPROVAL_CHAT_ID")
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
    logging.error("ÐÐµ Ð·Ð°Ð´Ð°Ð½Ñ Ð¾Ð±ÑÐ·Ð°ÑÐµÐ»ÑÐ½ÑÐµ Ð¿ÐµÑÐµÐ¼ÐµÐ½Ð½ÑÐµ Ð¾ÐºÑÑÐ¶ÐµÐ½Ð¸Ñ Telegram!")
    sys.exit(1)
if not all([TWITTER_API_KEY, TWITTER_API_SECRET, TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_TOKEN_SECRET]):
    logging.error("ÐÐµ Ð·Ð°Ð´Ð°Ð½Ñ Ð¾Ð±ÑÐ·Ð°ÑÐµÐ»ÑÐ½ÑÐµ Ð¿ÐµÑÐµÐ¼ÐµÐ½Ð½ÑÐµ Ð¾ÐºÑÑÐ¶ÐµÐ½Ð¸Ñ Ð´Ð»Ñ Twitter!")
    sys.exit(1)
if not all([GITHUB_TOKEN, GITHUB_REPO]):
    logging.error("ÐÐµ Ð·Ð°Ð´Ð°Ð½Ñ Ð¾Ð±ÑÐ·Ð°ÑÐµÐ»ÑÐ½ÑÐµ Ð¿ÐµÑÐµÐ¼ÐµÐ½Ð½ÑÐµ Ð¾ÐºÑÑÐ¶ÐµÐ½Ð¸Ñ GitHub!")
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
    "ð ÐÑÐ¸Ð²ÐµÑ! Ð­ÑÐ¾ Ð±Ð¾Ñ Ð¿ÑÐ±Ð»Ð¸ÐºÐ°ÑÐ¸Ð¹.\n\n"
    "ÐÐ-Ð³ÐµÐ½ÐµÑÐ°ÑÐ¸Ñ, Ð½Ð¾Ð²Ð¾ÑÑÐ¸, Ð¸Ð´ÐµÐ¸, Ð³ÐµÐ½ÐµÑÐ°ÑÐ¸Ñ ÐºÐ°ÑÑÐ¸Ð½Ð¾Ðº Ð¸ Ð¼Ð½Ð¾Ð³Ð¾Ðµ Ð´ÑÑÐ³Ð¾Ðµ."
)
WELCOME_HASHTAGS = "#AiCoin #AI #crypto #ÑÑÐµÐ½Ð´Ñ #Ð±Ð¾Ñ #Ð½Ð¾Ð²Ð¾ÑÑÐ¸"

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

github_client = Github(GITHUB_TOKEN)
github_repo = github_client.get_repo(GITHUB_REPO)

def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("â ÐÐ¾ÑÑ", callback_data="approve")],
        [InlineKeyboardButton("âï¸ Ð¡Ð´ÐµÐ»Ð°Ð¹ ÑÐ°Ð¼", callback_data="self_post")],
        [InlineKeyboardButton("ð ÐÐ¾Ð´ÑÐ¼Ð°ÑÑ", callback_data="think")],
        [InlineKeyboardButton("ð ÐÐ¾Ð²ÑÐ¹ Ð¿Ð¾ÑÑ", callback_data="new_post")],
        [InlineKeyboardButton("âï¸ ÐÐ·Ð¼ÐµÐ½Ð¸ÑÑ", callback_data="edit_post")],
        [InlineKeyboardButton("ð¬ ÐÐ¾Ð³Ð¾Ð²Ð¾ÑÐ¸ÑÑ", callback_data="chat"), InlineKeyboardButton("ð ÐÐµ Ð±ÐµÑÐ¿Ð¾ÐºÐ¾Ð¸ÑÑ", callback_data="do_not_disturb")],
        [InlineKeyboardButton("â©ï¸ ÐÐµÑÐ½ÑÑÑ Ð¿ÑÐµÐ´ÑÐ´ÑÑÐ¸Ð¹ Ð¿Ð¾ÑÑ", callback_data="restore_previous"), InlineKeyboardButton("ð ÐÐ°Ð²ÐµÑÑÐ¸ÑÑ", callback_data="end_day")],
        [InlineKeyboardButton("ð´ ÐÑÐºÐ»ÑÑÐ¸ÑÑ", callback_data="shutdown_bot")],
    ])

def post_choice_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("ÐÐ¾ÑÑ Ð² Twitter", callback_data="post_twitter")],
        [InlineKeyboardButton("ÐÐ¾ÑÑ Ð² Telegram", callback_data="post_telegram")],
        [InlineKeyboardButton("ÐÐÐ¡Ð¢!", callback_data="post_both")],
        [InlineKeyboardButton("âï¸ Ð¡Ð´ÐµÐ»Ð°Ð¹ ÑÐ°Ð¼", callback_data="self_post")],
        [InlineKeyboardButton("â ÐÑÐ¼ÐµÐ½Ð°", callback_data="cancel_to_main")]
    ])

def post_end_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("ð ÐÐ¾Ð²ÑÐ¹ Ð¿Ð¾ÑÑ", callback_data="new_post_manual")],
        [InlineKeyboardButton("âï¸ Ð¡Ð´ÐµÐ»Ð°Ð¹ ÑÐ°Ð¼", callback_data="self_post")],
        [InlineKeyboardButton("ð ÐÐµ Ð±ÐµÑÐ¿Ð¾ÐºÐ¾Ð¸ÑÑ", callback_data="do_not_disturb")],
        [InlineKeyboardButton("ð ÐÐ°Ð²ÐµÑÑÐ¸ÑÑ", callback_data="end_day")],
        [InlineKeyboardButton("ð¬ ÐÐ¾Ð³Ð¾Ð²Ð¾ÑÐ¸ÑÑ", callback_data="chat")]
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
    signature = (
        "\nLearn more: https://getaicoin.com/ | Twitter: https://x.com/AiCoin_ETH #AiCoin #Ai $Ai #crypto #blockchain #AI #DeFi"
    )
    max_length = 280
    reserve = max_length - len(signature)
    if len(text_ru) > reserve:
        main_part = text_ru[:reserve - 3].rstrip() + "..."
    else:
        main_part = text_ru
    return main_part + signature

def upload_image_to_github(image_path, filename):
    logging.info(f"upload_image_to_github: image_path={image_path}, filename={filename}")
    with open(image_path, "rb") as img_file:
        content = img_file.read()
    try:
        github_repo.create_file(f"{GITHUB_IMAGE_PATH}/{filename}", "upload image for post", content, branch="main")
        url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{GITHUB_IMAGE_PATH}/{filename}"
        logging.info(f"upload_image_to_github: ÐÐ°Ð³ÑÑÐ¶ÐµÐ½Ð¾ Ð½Ð° GitHub: {url}")
        return url
    except Exception as e:
        logging.error(f"ÐÑÐ¸Ð±ÐºÐ° Ð·Ð°Ð³ÑÑÐ·ÐºÐ¸ ÑÐ°Ð¹Ð»Ð° Ð½Ð° GitHub: {e}")
        return None

def delete_image_from_github(filename):
    try:
        file_path = f"{GITHUB_IMAGE_PATH}/{filename}"
        contents = github_repo.get_contents(file_path, ref="main")
        github_repo.delete_file(contents.path, "delete image after posting", contents.sha, branch="main")
        logging.info(f"delete_image_from_github: Ð£Ð´Ð°Ð»ÑÐ½ ÑÐ°Ð¹Ð» Ñ GitHub: {filename}")
    except Exception as e:
        logging.error(f"ÐÑÐ¸Ð±ÐºÐ° ÑÐ´Ð°Ð»ÐµÐ½Ð¸Ñ ÑÐ°Ð¹Ð»Ð° Ñ GitHub: {e}")

async def download_image_async(url_or_file_id, is_telegram_file=False, bot=None, retries=3):
    if is_telegram_file:
        for attempt in range(retries):
            try:
                logging.info(f"download_image_async: Ð¿Ð¾Ð¿ÑÑÐºÐ° {attempt+1} Ð·Ð°Ð³ÑÑÐ·ÐºÐ¸ Telegram file_id={url_or_file_id}")
                file = await bot.get_file(url_or_file_id)
                tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
                await file.download_to_drive(tmp_file.name)
                logging.info(f"download_image_async: Telegram ÑÐ°Ð¹Ð» ÑÐºÐ°ÑÐ°Ð½ Ð²Ð¾ Ð²ÑÐµÐ¼ÐµÐ½Ð½ÑÐ¹ ÑÐ°Ð¹Ð» {tmp_file.name}")
                return tmp_file.name
            except Exception as e:
                logging.warning(f"ÐÐ¾Ð¿ÑÑÐºÐ° {attempt + 1} Ð·Ð°Ð³ÑÑÐ·ÐºÐ¸ Telegram ÑÐ°Ð¹Ð»Ð° Ð½Ðµ ÑÐ´Ð°Ð»Ð°ÑÑ: {e}")
                await asyncio.sleep(1)
        raise Exception("ÐÐµ ÑÐ´Ð°Ð»Ð¾ÑÑ ÑÐºÐ°ÑÐ°ÑÑ ÑÐ°Ð¹Ð» Ð¸Ð· Telegram Ð¿Ð¾ÑÐ»Ðµ Ð½ÐµÑÐºÐ¾Ð»ÑÐºÐ¸Ñ Ð¿Ð¾Ð¿ÑÑÐ¾Ðº")
    else:
        logging.info(f"download_image_async: Ð¡ÐºÐ°ÑÐ¸Ð²Ð°Ñ Ð¸Ð·Ð¾Ð±ÑÐ°Ð¶ÐµÐ½Ð¸Ðµ Ð¿Ð¾ URL: {url_or_file_id}")
        headers = {'User-Agent': 'Mozilla/5.0'}
        r = requests.get(url_or_file_id, headers=headers)
        r.raise_for_status()
        tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
        tmp_file.write(r.content)
        tmp_file.close()
        logging.info(f"download_image_async: ÐÐ·Ð¾Ð±ÑÐ°Ð¶ÐµÐ½Ð¸Ðµ ÑÐ¾ÑÑÐ°Ð½ÐµÐ½Ð¾ Ð²Ð¾ Ð²ÑÐµÐ¼ÐµÐ½Ð½ÑÐ¹ ÑÐ°Ð¹Ð»: {tmp_file.name}")
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
        raise Exception("ÐÐµ ÑÐ´Ð°Ð»Ð¾ÑÑ Ð·Ð°Ð³ÑÑÐ·Ð¸ÑÑ ÑÐ¾ÑÐ¾ Ð½Ð° GitHub")
    logging.info(f"process_telegram_photo: ÐÐ¾Ð»ÑÑÐµÐ½Ð° ÑÑÑÐ»ÐºÐ° Ð½Ð° GitHub: {url}")
    return url

async def send_photo_with_download(bot, chat_id, url_or_file_id, caption=None, reply_markup=None):
    github_filename = None
    logging.info(f"send_photo_with_download: chat_id={chat_id}, url_or_file_id={url_or_file_id}, caption='{caption}'")
    try:
        if not str(url_or_file_id).startswith("http"):
            url = await process_telegram_photo(url_or_file_id, bot)
            github_filename = url.split('/')[-1]
            logging.info(f"send_photo_with_download: Ð¾ÑÐ¿ÑÐ°Ð²Ð»ÑÑ ÑÐ¾ÑÐ¾ Ð¿Ð¾ url={url}, caption='{caption}'")
            msg = await bot.send_photo(chat_id=chat_id, photo=url, caption=caption, reply_markup=reply_markup)
            return msg, github_filename
        else:
            logging.info(f"send_photo_with_download: Ð¾ÑÐ¿ÑÐ°Ð²Ð»ÑÑ ÑÐ¾ÑÐ¾ Ð¿Ð¾ url_or_file_id={url_or_file_id}, caption='{caption}'")
            msg = await bot.send_photo(chat_id=chat_id, photo=url_or_file_id, caption=caption, reply_markup=reply_markup)
            return msg, None
    except Exception as e:
        logging.error(f"ÐÑÐ¸Ð±ÐºÐ° Ð² send_photo_with_download: {e}")
        raise

async def publish_post_to_telegram(bot, chat_id, text, image_url):
    github_filename = None
    logging.info(f"publish_post_to_telegram: chat_id={chat_id}, text='{text}', image_url={image_url}")
    try:
        msg, github_filename = await send_photo_with_download(bot, chat_id, image_url, caption=text)
        logging.info("ÐÐ¾ÑÑ ÑÑÐ¿ÐµÑÐ½Ð¾ Ð¾Ð¿ÑÐ±Ð»Ð¸ÐºÐ¾Ð²Ð°Ð½ Ð² Telegram!")
        if github_filename:
            delete_image_from_github(github_filename)
        return True
    except Exception as e:
        logging.error(f"ÐÑÐ¸Ð±ÐºÐ° Ð¿ÑÐ¸ Ð¿ÑÐ±Ð»Ð¸ÐºÐ°ÑÐ¸Ð¸ Ð² Telegram: {e}")
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"â ÐÑÐ¸Ð±ÐºÐ° Ð¿ÑÐ¸ Ð¿ÑÐ±Ð»Ð¸ÐºÐ°ÑÐ¸Ð¸ Ð² Telegram: {e}")
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
                logging.error("Telegram file_id Ð½Ðµ Ð¿Ð¾Ð´Ð´ÐµÑÐ¶Ð¸Ð²Ð°ÐµÑÑÑ Ð½Ð°Ð¿ÑÑÐ¼ÑÑ Ð´Ð»Ñ Twitter Ð¿ÑÐ±Ð»Ð¸ÐºÐ°ÑÐ¸Ð¸.")
                return False
            headers = {'User-Agent': 'Mozilla/5.0'}
            r = requests.get(image_url, headers=headers)
            r.raise_for_status()
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
            tmp.write(r.content)
            tmp.close()
            file_path = tmp.name
            logging.info(f"publish_post_to_twitter: Ð¡ÐºÐ°ÑÐ°Ð» ÐºÐ°ÑÑÐ¸Ð½ÐºÑ Ð²Ð¾ Ð²ÑÐµÐ¼ÐµÐ½Ð½ÑÐ¹ ÑÐ°Ð¹Ð» {file_path}")

        if file_path:
            media = twitter_api_v1.media_upload(file_path)
            media_ids = [media.media_id_string]
            os.remove(file_path)
            logging.info(f"publish_post_to_twitter: media_ids={media_ids}")

        twitter_client_v2.create_tweet(text=text, media_ids=media_ids)
        logging.info("ÐÐ¾ÑÑ ÑÑÐ¿ÐµÑÐ½Ð¾ Ð¾Ð¿ÑÐ±Ð»Ð¸ÐºÐ¾Ð²Ð°Ð½ Ð² Twitter!")

        if image_url and image_url.startswith(f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{GITHUB_IMAGE_PATH}/"):
            github_filename = image_url.split('/')[-1]
            delete_image_from_github(github_filename)
        return True
    except Exception as e:
        pending_post["active"] = False
        logging.error(f"ÐÑÐ¸Ð±ÐºÐ° Ð¿ÑÐ±Ð»Ð¸ÐºÐ°ÑÐ¸Ð¸ Ð² Twitter: {e}")
        asyncio.create_task(approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"â ÐÑÐ¸Ð±ÐºÐ° Ð¿ÑÐ¸ Ð¿ÑÐ±Ð»Ð¸ÐºÐ°ÑÐ¸Ð¸ Ð² Twitter: {e}\nÐÑÐ¾Ð²ÐµÑÑÑÐµ ÐºÐ»ÑÑÐ¸/ÑÐ¾ÐºÐµÐ½Ñ, Ð»Ð¸Ð¼Ð¸ÑÑ Ð¿ÑÐ±Ð»Ð¸ÐºÐ°ÑÐ¸Ð¹, ÑÐ¾ÑÐ¼Ð°Ñ Ð¼ÐµÐ´Ð¸Ð° Ð¸ Ð¿ÑÐ°Ð²Ð° Ð´Ð¾ÑÑÑÐ¿Ð°."))
        if github_filename:
            delete_image_from_github(github_filename)
        return False

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
    logging.info("ÐÐ°Ð·Ð° Ð´Ð°Ð½Ð½ÑÑ Ð¸Ð½Ð¸ÑÐ¸Ð°Ð»Ð¸Ð·Ð¸ÑÐ¾Ð²Ð°Ð½Ð°.")

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
            logging.warning(f"ÐÐµ ÑÐ´Ð°Ð»Ð¾ÑÑ Ð¿Ð¾Ð»ÑÑÐ¸ÑÑ ÑÐµÑ Ð¸Ð·Ð¾Ð±ÑÐ°Ð¶ÐµÐ½Ð¸Ñ: {e}")
            image_hash = None
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("INSERT INTO posts (text, timestamp, image_hash) VALUES (?, ?, ?)", (text, datetime.now().isoformat(), image_hash))
        await db.commit()
    logging.info("ÐÐ¾ÑÑ ÑÐ¾ÑÑÐ°Ð½ÑÐ½ Ð² Ð¸ÑÑÐ¾ÑÐ¸Ñ.")

async def check_timer():
    while True:
        await asyncio.sleep(0.5)
        if pending_post["active"] and pending_post.get("timer"):
            passed = (datetime.now() - pending_post["timer"]).total_seconds()
            if passed > pending_post.get("timeout", TIMER_PUBLISH_DEFAULT):
                try:
                    base_text = post_data["text_ru"].strip()
                    telegram_text = f"{base_text}\n\nLearn more: https://getaicoin.com/"
                    twitter_text = build_twitter_post(base_text)
                    logging.info("check_timer: ÐÑÐµÐ¼Ñ Ð¾Ð¶Ð¸Ð´Ð°Ð½Ð¸Ñ Ð¸ÑÑÐµÐºÐ»Ð¾, Ð½Ð°ÑÐ¸Ð½Ð°Ñ Ð°Ð²ÑÐ¾Ð¿ÑÐ±Ð»Ð¸ÐºÐ°ÑÐ¸Ñ.")
                    await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="â ÐÑÐµÐ¼Ñ Ð¾Ð¶Ð¸Ð´Ð°Ð½Ð¸Ñ Ð¸ÑÑÐµÐºÐ»Ð¾. ÐÑÐ±Ð»Ð¸ÐºÑÑ Ð°Ð²ÑÐ¾Ð¼Ð°ÑÐ¸ÑÐµÑÐºÐ¸.")
                    await publish_post_to_telegram(channel_bot, TELEGRAM_CHANNEL_USERNAME_ID, telegram_text, post_data["image_url"])
                    publish_post_to_twitter(twitter_text, post_data["image_url"])
                    logging.info("ÐÐ²ÑÐ¾Ð¼Ð°ÑÐ¸ÑÐµÑÐºÐ°Ñ Ð¿ÑÐ±Ð»Ð¸ÐºÐ°ÑÐ¸Ñ Ð¿ÑÐ¾Ð¸Ð·Ð²ÐµÐ´ÐµÐ½Ð°.")
                    await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="â ÐÐ¾ÑÑÑ Ð°Ð²ÑÐ¾Ð¼Ð°ÑÐ¸ÑÐµÑÐºÐ¸ Ð¾Ð¿ÑÐ±Ð»Ð¸ÐºÐ¾Ð²Ð°Ð½Ñ Ð² Telegram Ð¸ Twitter.")
                    await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="ÐÑÐ±ÐµÑÐ¸ÑÐµ Ð´ÐµÐ¹ÑÑÐ²Ð¸Ðµ:", reply_markup=post_end_keyboard())
                    shutdown_bot_and_exit()
                except Exception as e:
                    pending_post["active"] = False
                    await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"â ÐÑÐ¸Ð±ÐºÐ° Ð¿ÑÐ¸ Ð°Ð²ÑÐ¾Ð¿ÑÐ±Ð»Ð¸ÐºÐ°ÑÐ¸Ð¸: {e}\nÐÑÐ¾Ð²ÐµÑÑÑÐµ ÐºÐ»ÑÑÐ¸, Ð»Ð¸Ð¼Ð¸ÑÑ, Ð¿ÑÐ°Ð²Ð° Ð±Ð¾ÑÐ°, Ð»Ð¸Ð¼Ð¸ÑÑ Twitter/Telegram.")
                    await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="ÐÑÐ±ÐµÑÐ¸ÑÐµ Ð´ÐµÐ¹ÑÑÐ²Ð¸Ðµ:", reply_markup=post_end_keyboard())
                    logging.error(f"ÐÑÐ¸Ð±ÐºÐ° Ð¿ÑÐ¸ Ð°Ð²ÑÐ¾Ð¿ÑÐ±Ð»Ð¸ÐºÐ°ÑÐ¸Ð¸: {e}")
                pending_post["active"] = False

def reset_timer(timeout=None):
    pending_post["timer"] = datetime.now()
    if timeout:
        pending_post["timeout"] = timeout

async def send_post_for_approval():
    async with approval_lock:
        if do_not_disturb["active"] or pending_post["active"]:
            logging.info("send_post_for_approval: ÐÐµ Ð¾ÑÐ¿ÑÐ°Ð²Ð»ÑÑ Ð¿Ð¾ÑÑ - DND Ð¸Ð»Ð¸ ÑÐ¶Ðµ Ð°ÐºÑÐ¸Ð²ÐµÐ½.")
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
            logging.info(f"send_post_for_approval: Ð¾ÑÐ¿ÑÐ°Ð²ÐºÐ° Ð½Ð° ÑÐ¾Ð³Ð»Ð°ÑÐ¾Ð²Ð°Ð½Ð¸Ðµ image_url={post_data['image_url']}, text_ru='{post_data['text_ru']}'")
            await send_photo_with_download(
                approval_bot,
                TELEGRAM_APPROVAL_CHAT_ID,
                post_data["image_url"],
                caption=post_data["text_ru"] + "\n\n" + WELCOME_HASHTAGS,
                reply_markup=main_keyboard()
            )
            logging.info("ÐÐ¾ÑÑ Ð¾ÑÐ¿ÑÐ°Ð²Ð»ÐµÐ½ Ð½Ð° ÑÐ¾Ð³Ð»Ð°ÑÐ¾Ð²Ð°Ð½Ð¸Ðµ.")
        except Exception as e:
            logging.error(f"ÐÑÐ¸Ð±ÐºÐ° Ð¿ÑÐ¸ Ð¾ÑÐ¿ÑÐ°Ð²ÐºÐµ Ð½Ð° ÑÐ¾Ð³Ð»Ð°ÑÐ¾Ð²Ð°Ð½Ð¸Ðµ: {e}")

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
            logging.info(f"ÐÐ´Ñ Ð´Ð¾ 06:00... {int(to_sleep)} ÑÐµÐº")
            await asyncio.sleep(to_sleep)

        posts_left = lambda: scheduled_posts_per_day - manual_posts_today
        while posts_left() > 0:
            schedule = generate_random_schedule(posts_per_day=posts_left())
            logging.info(f"Ð Ð°ÑÐ¿Ð¸ÑÐ°Ð½Ð¸Ðµ Ð°Ð²ÑÐ¾-Ð¿Ð¾ÑÑÐ¾Ð² Ð½Ð° ÑÐµÐ³Ð¾Ð´Ð½Ñ: {[t.strftime('%H:%M:%S') for t in schedule]}")
            for post_time in schedule:
                if posts_left() <= 0:
                    break
                now = datetime.now()
                delay = (post_time - now).total_seconds()
                if delay > 0:
                    logging.info(f"ÐÐ´Ñ {int(delay)} ÑÐµÐº Ð´Ð¾ {post_time.strftime('%H:%M:%S')} Ð´Ð»Ñ Ð¿ÑÐ±Ð»Ð¸ÐºÐ°ÑÐ¸Ð¸ Ð°Ð²ÑÐ¾-Ð¿Ð¾ÑÑÐ°")
                    await asyncio.sleep(delay)
                post_data["text_ru"] = f"ÐÐ¾Ð²ÑÐ¹ Ð¿Ð¾ÑÑ ({post_time.strftime('%H:%M:%S')})"
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

# --- ÐÐ¾Ð³Ð¸ÐºÐ° "Ð¡Ð´ÐµÐ»Ð°Ð¹ ÑÐ°Ð¼" ---
async def self_post_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logging.info(f"self_post_message_handler: Ð¿Ð¾Ð»ÑÑÐµÐ½Ð¾ ÑÐ¾Ð¾Ð±ÑÐµÐ½Ð¸Ðµ Ð¾Ñ user_id={user_id}")
    if user_id in user_self_post and user_self_post[user_id]['state'] == 'wait_post':
        text = update.message.text or update.message.caption or ""
        image_url = None
        if update.message.photo:
            image_url = await process_telegram_photo(update.message.photo[-1].file_id, approval_bot)
        logging.info(f"self_post_message_handler: ÑÐ¾ÑÑÐ°Ð½ÐµÐ½Ð¸Ðµ text='{text}', image_url={image_url}")
        user_self_post[user_id]['text'] = text
        user_self_post[user_id]['image'] = image_url
        user_self_post[user_id]['state'] = 'wait_confirm'

        try:
            if image_url:
                await send_photo_with_download(
                    approval_bot,
                    TELEGRAM_APPROVAL_CHAT_ID,
                    image_url,
                    caption=text
                )
            elif text:
                await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=text)
            await approval_bot.send_message(
                chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                text="ÐÑÐ¾Ð²ÐµÑÑ Ð¿Ð¾ÑÑ. ÐÑÐ»Ð¸ Ð²ÑÑ Ð¾Ðº â Ð½Ð°Ð¶Ð¼Ð¸ ð¤ ÐÐ°Ð²ÐµÑÑÐ¸ÑÑ Ð³ÐµÐ½ÐµÑÐ°ÑÐ¸Ñ.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("ð¤ ÐÐ°Ð²ÐµÑÑÐ¸ÑÑ Ð³ÐµÐ½ÐµÑÐ°ÑÐ¸Ñ Ð¿Ð¾ÑÑÐ°", callback_data="finish_self_post")],
                    [InlineKeyboardButton("â ÐÑÐ¼ÐµÐ½Ð°", callback_data="cancel_to_main")]
                ])
            )
        except Exception as e:
            logging.error(f"ÐÑÐ¸Ð±ÐºÐ° Ð¾ÑÐ¿ÑÐ°Ð²ÐºÐ¸ Ð¿ÑÐµÐ´Ð¿ÑÐ¾ÑÐ¼Ð¾ÑÑÐ° 'Ð¡Ð´ÐµÐ»Ð°Ð¹ ÑÐ°Ð¼': {e}")
        return

# --- ÐÐ¾Ð³Ð¸ÐºÐ° "ÐÐ·Ð¼ÐµÐ½Ð¸ÑÑ Ð¿Ð¾ÑÑ" ---
async def edit_post_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in user_self_post and user_self_post[user_id]['state'] == 'wait_edit':
        text = update.message.text or update.message.caption or None
        image_url = None
        if update.message.photo:
            image_url = await process_telegram_photo(update.message.photo[-1].file_id, approval_bot)
        if text:
            post_data["text_ru"] = text
        if image_url:
            post_data["image_url"] = image_url
        user_self_post.pop(user_id, None)
        try:
            await send_photo_with_download(
                approval_bot,
                TELEGRAM_APPROVAL_CHAT_ID,
                post_data["image_url"],
                caption=post_data["text_ru"],
                reply_markup=post_choice_keyboard()
            )
        except Exception as e:
            logging.error(f"ÐÑÐ¸Ð±ÐºÐ° Ð¿ÑÐµÐ´Ð¿ÑÐ¾ÑÐ¼Ð¾ÑÑÐ° Ð¿Ð¾ÑÐ»Ðµ Ð¸Ð·Ð¼ÐµÐ½ÐµÐ½Ð¸Ñ: {e}")
        return

# --- Routing ÑÐ¾Ð¾Ð±ÑÐµÐ½Ð¸Ð¹ ---
async def message_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in user_self_post and user_self_post[user_id].get('state') == 'wait_edit':
        await edit_post_message_handler(update, context)
        return
    await self_post_message_handler(update, context)

# --- ÐÐ±ÑÐ°Ð±Ð¾ÑÐºÐ° ÐºÐ½Ð¾Ð¿Ð¾Ðº ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_action_time, prev_data, manual_posts_today
    try:
        await update.callback_query.answer()
    except Exception as e:
        logging.warning(f"ÐÐµ ÑÐ´Ð°Ð»Ð¾ÑÑ Ð¾ÑÐ²ÐµÑÐ¸ÑÑ Ð½Ð° callback_query: {e}")
    if pending_post["active"]:
        reset_timer(TIMER_PUBLISH_EXTEND)
    user_id = update.effective_user.id
    now = datetime.now()
    if user_id in last_action_time and (now - last_action_time[user_id]).seconds < 3:
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="â³ ÐÐ¾Ð´Ð¾Ð¶Ð´Ð¸ÑÐµ Ð½ÐµÐ¼Ð½Ð¾Ð³Ð¾...", reply_markup=main_keyboard())
        return
    last_action_time[user_id] = now
    action = update.callback_query.data
    logging.info(f"button_handler: user_id={user_id}, action={action}")
    prev_data.update(post_data)

    if action == "edit_post":
        try:
            await update.callback_query.message.delete()
        except Exception:
            pass
        user_self_post[user_id] = {'state': 'wait_edit'}
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="âï¸ ÐÑÐ¸ÑÐ»Ð¸ Ð½Ð¾Ð²ÑÐ¹ ÑÐµÐºÑÑ Ð¸/Ð¸Ð»Ð¸ ÑÐ¾ÑÐ¾ Ð´Ð»Ñ ÑÐµÐ´Ð°ÐºÑÐ¸ÑÐ¾Ð²Ð°Ð½Ð¸Ñ Ð¿Ð¾ÑÑÐ° (Ð² Ð¾Ð´Ð½Ð¾Ð¼ ÑÐ¾Ð¾Ð±ÑÐµÐ½Ð¸Ð¸).",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("â ÐÑÐ¼ÐµÐ½Ð°", callback_data="cancel_to_main")]])
        )
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
            try:
                if image_url:
                    logging.info(f"button_handler: Ð¿ÑÐµÐ´Ð¿ÑÐ¾ÑÐ¼Ð¾ÑÑ finish_self_post image_url={image_url}, caption='{twitter_text}'")
                    await send_photo_with_download(approval_bot, TELEGRAM_APPROVAL_CHAT_ID, image_url, caption=twitter_text, reply_markup=post_choice_keyboard())
                else:
                    logging.info(f"button_handler: Ð¿ÑÐµÐ´Ð¿ÑÐ¾ÑÐ¼Ð¾ÑÑ finish_self_post text='{twitter_text}'")
                    await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=twitter_text, reply_markup=post_choice_keyboard())
            except Exception as e:
                logging.error(f"ÐÑÐ¸Ð±ÐºÐ° Ð¿ÑÐµÐ´Ð¿ÑÐ¾ÑÐ¼Ð¾ÑÑÐ° Ð¿Ð¾ÑÐ»Ðµ Ð·Ð°Ð²ÐµÑÑÐµÐ½Ð¸Ñ 'Ð¡Ð´ÐµÐ»Ð°Ð¹ ÑÐ°Ð¼': {e}")
        return

    if action == "shutdown_bot":
        logging.info("ÐÑÑÐ°Ð½Ð°Ð²Ð»Ð¸Ð²Ð°Ñ Ð±Ð¾ÑÐ° Ð¿Ð¾ ÐºÐ½Ð¾Ð¿ÐºÐµ!")
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="ð´ ÐÐ¾Ñ Ð¿Ð¾Ð»Ð½Ð¾ÑÑÑÑ Ð²ÑÐºÐ»ÑÑÐµÐ½. GitHub Actions Ð±Ð¾Ð»ÑÑÐµ Ð½Ðµ ÑÑÐ°ÑÐ¸Ñ Ð¼Ð¸Ð½ÑÑÑ!")
        await asyncio.sleep(2)
        shutdown_bot_and_exit()
        return

    if action == "approve":
        twitter_text = build_twitter_post(post_data["text_ru"])
        logging.info(f"button_handler: approve, send_photo_with_download image_url={post_data['image_url']}, caption='{twitter_text}'")
        await send_photo_with_download(approval_bot, TELEGRAM_APPROVAL_CHAT_ID, post_data["image_url"], caption=twitter_text)
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="ÐÑÐ±ÐµÑÐ¸ÑÐµ Ð¿Ð»Ð¾ÑÐ°Ð´ÐºÑ:", reply_markup=post_choice_keyboard())
        return

    if action in ["post_twitter", "post_telegram", "post_both"]:
        base_text = post_data["text_ru"].strip()
        telegram_text = f"{base_text}\n\nLearn more: https://getaicoin.com/"
        twitter_text = build_twitter_post(base_text)

        telegram_success = False
        twitter_success = False

        if action in ["post_telegram", "post_both"]:
            try:
                logging.info(f"button_handler: Ð¿ÑÐ±Ð»Ð¸ÐºÐ°ÑÐ¸Ñ Telegram, text='{telegram_text}', image_url={post_data['image_url']}")
                telegram_success = await publish_post_to_telegram(channel_bot, TELEGRAM_CHANNEL_USERNAME_ID, telegram_text, post_data["image_url"])
            except Exception as e:
                logging.error(f"ÐÑÐ¸Ð±ÐºÐ° Ð¿ÑÐ¸ Ð¿ÑÐ±Ð»Ð¸ÐºÐ°ÑÐ¸Ð¸ Ð² Telegram: {e}")
                await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"â ÐÐµ ÑÐ´Ð°Ð»Ð¾ÑÑ Ð¾ÑÐ¿ÑÐ°Ð²Ð¸ÑÑ Ð² Telegram: {e}")

        if action in ["post_twitter", "post_both"]:
            try:
                logging.info(f"button_handler: Ð¿ÑÐ±Ð»Ð¸ÐºÐ°ÑÐ¸Ñ Twitter, text='{twitter_text}', image_url={post_data['image_url']}")
                twitter_success = publish_post_to_twitter(twitter_text, post_data["image_url"])
            except Exception as e:
                logging.error(f"ÐÑÐ¸Ð±ÐºÐ° Ð¿ÑÐ¸ Ð¿ÑÐ±Ð»Ð¸ÐºÐ°ÑÐ¸Ð¸ Ð² Twitter: {e}")
                await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"â ÐÐµ ÑÐ´Ð°Ð»Ð¾ÑÑ Ð¾ÑÐ¿ÑÐ°Ð²Ð¸ÑÑ Ð² Twitter: {e}")

        pending_post["active"] = False
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="â Ð£ÑÐ¿ÐµÑÐ½Ð¾ Ð¾ÑÐ¿ÑÐ°Ð²Ð»ÐµÐ½Ð¾ Ð² Telegram!" if telegram_success else "â ÐÐµ ÑÐ´Ð°Ð»Ð¾ÑÑ Ð¾ÑÐ¿ÑÐ°Ð²Ð¸ÑÑ Ð² Telegram.")
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="â Ð£ÑÐ¿ÐµÑÐ½Ð¾ Ð¾ÑÐ¿ÑÐ°Ð²Ð»ÐµÐ½Ð¾ Ð² Twitter!" if twitter_success else "â ÐÐµ ÑÐ´Ð°Ð»Ð¾ÑÑ Ð¾ÑÐ¿ÑÐ°Ð²Ð¸ÑÑ Ð² Twitter.")
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="Ð Ð°Ð±Ð¾ÑÐ° Ð·Ð°Ð²ÐµÑÑÐµÐ½Ð°.", reply_markup=post_end_keyboard())
        shutdown_bot_and_exit()
        return

    if action == "self_post":
        try:
            await update.callback_query.message.delete()
        except Exception:
            pass
        user_self_post[user_id] = {'text': '', 'image': None, 'state': 'wait_post'}
        logging.info(f"button_handler: self_post, user_id={user_id} Ð¿ÐµÑÐµÑÐµÐ» Ð² ÑÐµÐ¶Ð¸Ð¼ Ð²Ð²Ð¾Ð´Ð° ÑÐµÐºÑÑÐ°/ÑÐ¾ÑÐ¾")
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="âï¸ ÐÐ°Ð¿Ð¸ÑÐ¸ ÑÐ²Ð¾Ð¹ ÑÐµÐºÑÑ Ð¿Ð¾ÑÑÐ° Ð¸ (Ð¾Ð¿ÑÐ¸Ð¾Ð½Ð°Ð»ÑÐ½Ð¾) Ð¿ÑÐ¸Ð»Ð¾Ð¶Ð¸ ÑÐ¾ÑÐ¾ â Ð²ÑÑ Ð¾Ð´Ð½Ð¸Ð¼ ÑÐ¾Ð¾Ð±ÑÐµÐ½Ð¸ÐµÐ¼. ÐÐ¾ÑÐ»Ðµ ÑÑÐ¾Ð³Ð¾ Ð¿Ð¾ÑÐ²Ð¸ÑÑÑ Ð¿ÑÐµÐ´Ð¿ÑÐ¾ÑÐ¼Ð¾ÑÑ Ñ ÐºÐ½Ð¾Ð¿ÐºÐ°Ð¼Ð¸.")
        return

    if action == "cancel_to_main":
        try:
            await update.callback_query.message.delete()
        except Exception:
            pass
        user_self_post.pop(user_id, None)
        logging.info(f"button_handler: cancel_to_main, user_id={user_id} Ð²Ð¾Ð·Ð²ÑÐ°ÑÑÐ½ Ð² Ð³Ð»Ð°Ð²Ð½Ð¾Ðµ Ð¼ÐµÐ½Ñ")
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="ÐÐ»Ð°Ð²Ð½Ð¾Ðµ Ð¼ÐµÐ½Ñ:", reply_markup=main_keyboard())
        return

    if action == "restore_previous":
        post_data.update(prev_data)
        logging.info("button_handler: restore_previous, Ð²Ð¾ÑÑÑÐ°Ð½Ð¾Ð²Ð»ÐµÐ½ Ð¿ÑÐµÐ´ÑÐ´ÑÑÐ¸Ð¹ Ð²Ð°ÑÐ¸Ð°Ð½Ñ Ð¿Ð¾ÑÑÐ°")
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="â©ï¸ ÐÐ¾ÑÑÑÐ°Ð½Ð¾Ð²Ð»ÐµÐ½ Ð¿ÑÐµÐ´ÑÐ´ÑÑÐ¸Ð¹ Ð²Ð°ÑÐ¸Ð°Ð½Ñ.", reply_markup=main_keyboard())
        if pending_post["active"]:
            await send_post_for_approval()
        return

    if action == "end_day":
        pending_post["active"] = False
        do_not_disturb["active"] = True
        tomorrow = datetime.combine(datetime.now().date() + timedelta(days=1), dt_time(hour=9))
        kb = main_keyboard()
        logging.info("button_handler: end_day, Ð±Ð¾Ñ Ð·Ð°Ð²ÐµÑÑÐ°ÐµÑ ÑÐ°Ð±Ð¾ÑÑ Ð´Ð¾ Ð·Ð°Ð²ÑÑÐ°")
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"ð Ð Ð°Ð±Ð¾ÑÐ° Ð·Ð°Ð²ÐµÑÑÐµÐ½Ð° Ð½Ð° ÑÐµÐ³Ð¾Ð´Ð½Ñ.\nÐ¡Ð»ÐµÐ´ÑÑÑÐ°Ñ Ð¿ÑÐ±Ð»Ð¸ÐºÐ°ÑÐ¸Ñ: {tomorrow.strftime('%Y-%m-%d %H:%M')}", parse_mode="HTML", reply_markup=kb)
        return

    if action == "think":
        logging.info("button_handler: think, Ð¿Ð¾Ð»ÑÐ·Ð¾Ð²Ð°ÑÐµÐ»Ñ Ð´ÑÐ¼Ð°ÐµÑ Ð´Ð°Ð»ÑÑÐµ")
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="ð§ ÐÑÐ¼Ð°ÐµÐ¼ Ð´Ð°Ð»ÑÑÐµâ¦", reply_markup=main_keyboard())
        return

    if action == "chat":
        logging.info("button_handler: chat, ÑÐµÐ¶Ð¸Ð¼ ÑÐ°ÑÐ°")
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="ð¬ ÐÐ°ÑÐ¸Ð½Ð°ÐµÐ¼ ÑÐ°Ñ:\n" + post_data["text_ru"], reply_markup=post_end_keyboard())
        return

    if action == "do_not_disturb":
        do_not_disturb["active"] = not do_not_disturb["active"]
        status = "Ð²ÐºÐ»ÑÑÑÐ½" if do_not_disturb["active"] else "Ð²ÑÐºÐ»ÑÑÐµÐ½"
        logging.info(f"button_handler: do_not_disturb, ÑÐµÐ¶Ð¸Ð¼ {status}")
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"ð Ð ÐµÐ¶Ð¸Ð¼ Â«ÐÐµ Ð±ÐµÑÐ¿Ð¾ÐºÐ¾Ð¸ÑÑÂ» {status}.", reply_markup=post_end_keyboard())
        return

    if action == "new_post":
        pending_post["active"] = False
        post_data["text_ru"] = f"Ð¢ÐµÑÑÐ¾Ð²ÑÐ¹ Ð½Ð¾Ð²ÑÐ¹ Ð¿Ð¾ÑÑ #{post_data['post_id'] + 1}"
        post_data["image_url"] = random.choice(test_images)
        post_data["post_id"] += 1
        post_data["is_manual"] = False
        logging.info("button_handler: new_post, Ð°Ð²ÑÐ¾Ð³ÐµÐ½ÐµÑÐ°ÑÐ¸Ñ Ð½Ð¾Ð²Ð¾Ð³Ð¾ Ð¿Ð¾ÑÑÐ°")
        await send_photo_with_download(
            approval_bot,
            TELEGRAM_APPROVAL_CHAT_ID,
            post_data["image_url"],
            caption=post_data["text_ru"] + "\n\n" + WELCOME_HASHTAGS,
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
        post_data["text_ru"] = f"Ð ÑÑÐ½Ð¾Ð¹ Ð½Ð¾Ð²ÑÐ¹ Ð¿Ð¾ÑÑ #{post_data['post_id'] + 1}"
        post_data["image_url"] = random.choice(test_images)
        post_data["post_id"] += 1
        post_data["is_manual"] = True
        logging.info("button_handler: new_post_manual, ÑÑÑÐ½Ð°Ñ Ð³ÐµÐ½ÐµÑÐ°ÑÐ¸Ñ Ð½Ð¾Ð²Ð¾Ð³Ð¾ Ð¿Ð¾ÑÑÐ°")
        await send_photo_with_download(
            approval_bot,
            TELEGRAM_APPROVAL_CHAT_ID,
            post_data["image_url"],
            caption=post_data["text_ru"] + "\n\n" + WELCOME_HASHTAGS,
            reply_markup=main_keyboard()
        )
        pending_post.update({
            "active": True,
            "timer": datetime.now(),
            "timeout": TIMER_PUBLISH_DEFAULT
        })
        return

async def delayed_start(app: Application):
    logging.info("delayed_start: Ð¸Ð½Ð¸ÑÐ¸Ð°Ð»Ð¸Ð·Ð°ÑÐ¸Ñ Ð±Ð°Ð·Ñ Ð¸ Ð·Ð°Ð¿ÑÑÐº Ð·Ð°Ð´Ð°Ñ")
    await init_db()
    asyncio.create_task(schedule_daily_posts())
    asyncio.create_task(check_timer())
    # ÐÑÐ¸Ð²ÐµÑÑÑÐ²Ð¸Ðµ: ÑÑÐ°Ð·Ñ ÐºÐ°ÑÑÐ¸Ð½ÐºÐ° + ÑÐµÐºÑÑ + ÐºÐ½Ð¾Ð¿ÐºÐ¸
    await send_photo_with_download(
        approval_bot,
        TELEGRAM_APPROVAL_CHAT_ID,
        post_data["image_url"],
        caption=post_data["text_ru"] + "\n\n" + WELCOME_HASHTAGS,
        reply_markup=main_keyboard()
    )
    logging.info("ÐÐ¾Ñ Ð·Ð°Ð¿ÑÑÐµÐ½ Ð¸ Ð³Ð¾ÑÐ¾Ð² Ðº ÑÐ°Ð±Ð¾ÑÐµ.")

def shutdown_bot_and_exit():
    logging.info("ÐÐ°Ð²ÐµÑÑÐµÐ½Ð¸Ðµ ÑÐ°Ð±Ð¾ÑÑ Ð±Ð¾ÑÐ° ÑÐµÑÐµÐ· shutdown_bot_and_exit()")
    try:
        asyncio.create_task(approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="ð´ ÐÐ¾Ñ Ð¿Ð¾Ð»Ð½Ð¾ÑÑÑÑ Ð²ÑÐºÐ»ÑÑÐµÐ½. GitHub Actions Ð±Ð¾Ð»ÑÑÐµ Ð½Ðµ ÑÑÐ°ÑÐ¸Ñ Ð¼Ð¸Ð½ÑÑÑ!"))
    except Exception:
        pass
    import time; time.sleep(2)
    os._exit(0)

def main():
    logging.info("main: Ð¡ÑÐ°ÑÑ Telegram Ð±Ð¾ÑÐ° Ð¼Ð¾Ð´ÐµÑÐ°ÑÐ¸Ð¸ Ð¸ Ð¿ÑÐ±Ð»Ð¸ÐºÐ°ÑÐ¸Ð¸â¦")
    app = Application.builder()\
        .token(TELEGRAM_BOT_TOKEN_APPROVAL)\
        .post_init(delayed_start)\
        .build()
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, message_router))
    app.run_polling(poll_interval=0.12, timeout=1)

if __name__ == "__main__":
    main()