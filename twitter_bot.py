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

test_images = [
    "https://upload.wikimedia.org/wikipedia/commons/4/47/PNG_transparency_demonstration_1.png",
    "https://upload.wikimedia.org/wikipedia/commons/3/3f/Fronalpstock_big.jpg",
    "https://upload.wikimedia.org/wikipedia/commons/1/17/Google-flutter-logo.png",
    "https://upload.wikimedia.org/wikipedia/commons/d/d6/Wp-w4-big.jpg"
]

WELCOME_POST_RU = (
    "🚀 Добро пожаловать в бота публикаций!\n\n"
    "AI контент, новости, идеи, генерация изображений и многое другое."
)
WELCOME_HASHTAGS = "#AiCoin #AI #crypto #тренды #бот #новости"

post_data = {
    "text_ru":   WELCOME_POST_RU,
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

async def prepare_photo_for_send(image_url, bot):
    tmp_path = None
    if image_url and str(image_url).startswith('http'):
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; AiBot/1.0; +https://gptonline.ai/)"
        }
        response = requests.get(image_url, headers=headers)
        response.raise_for_status()
        with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as tmp:
            tmp.write(response.content)
            tmp_path = tmp.name
    elif image_url:
        file_obj = await bot.get_file(image_url)
        file_bytes = await file_obj.download_as_bytearray()
        with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name
    return tmp_path

def build_twitter_post_ru(text_ru: str) -> str:
    signature = (
        "\nЧитайте больше в Telegram: t.me/AiCoin_ETH или на сайте: https://getaicoin.com/ "
        "#AiCoin #Ai $Ai #crypto #blockchain #AI #DeFi"
    )
    max_length = 280
    reserve = max_length - len(signature)
    if len(text_ru) > reserve:
        main_part = text_ru[:reserve - 3].rstrip() + "..."
    else:
        main_part = text_ru
    return main_part + signature

def publish_post_to_twitter(text, image_url=None):
    try:
        media_ids = None
        if image_url:
            if str(image_url).startswith('http'):
                headers = {
                    "User-Agent": "Mozilla/5.0 (compatible; AiBot/1.0; +https://gptonline.ai/)"
                }
                response = requests.get(image_url, headers=headers)
                response.raise_for_status()
                with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as tmp:
                    tmp.write(response.content)
                    tmp_path = tmp.name
                try:
                    media = twitter_api_v1.media_upload(tmp_path)
                    media_ids = [media.media_id_string]
                finally:
                    os.remove(tmp_path)
            else:
                loop = asyncio.get_event_loop()
                async def get_photo():
                    file_obj = await approval_bot.get_file(image_url)
                    file_bytes = await file_obj.download_as_bytearray()
                    with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as tmp:
                        tmp.write(file_bytes)
                        return tmp.name
                tmp_path = loop.run_until_complete(get_photo())
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
            tmp_path = await prepare_photo_for_send(post_data["image_url"], approval_bot)
            with open(tmp_path, "rb") as f:
                photo_msg = await approval_bot.send_photo(
                    chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                    photo=f,
                    caption=post_data["text_ru"] + "\n\n" + WELCOME_HASHTAGS,
                    reply_markup=main_keyboard()
                )
            os.remove(tmp_path)
            approval_message_ids["photo"] = photo_msg.message_id
            logging.info("Пост отправлен на согласование.")
        except Exception as e:
            logging.error(f"Ошибка при отправке на согласование: {e}")

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
            tmp_path = await prepare_photo_for_send(image, approval_bot)
            with open(tmp_path, "rb") as f:
                await approval_bot.send_photo(
                    chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                    photo=f,
                    caption=text if text else "(пустой пост)",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("📤 Завершить генерацию поста", callback_data="finish_self_post")],
                        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_to_main")]
                    ])
                )
            os.remove(tmp_path)
        else:
            await approval_bot.send_message(
                chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                text=text if text else "(пустой пост)",
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
            post_data["text_ru"] = text
            if image:
                post_data["image_url"] = image
            else:
                post_data["image_url"] = random.choice(test_images)
            post_data["post_id"] += 1
            post_data["is_manual"] = True
            user_self_post.pop(user_id, None)
            if post_data["image_url"]:
                tmp_path = await prepare_photo_for_send(post_data["image_url"], approval_bot)
                with open(tmp_path, "rb") as f:
                    await approval_bot.send_photo(
                        chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                        photo=f,
                        caption=post_data["text_ru"],
                        reply_markup=post_choice_keyboard()
                    )
                os.remove(tmp_path)
            else:
                await approval_bot.send_message(
                    chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                    text=post_data["text_ru"],
                    reply_markup=post_choice_keyboard()
                )
        return

    if action == "approve":
        twitter_text = build_twitter_post_ru(post_data["text_ru"])
        tmp_path = await prepare_photo_for_send(post_data["image_url"], approval_bot)
        with open(tmp_path, "rb") as f:
            await approval_bot.send_photo(
                chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                photo=f,
                caption=twitter_text,
                reply_markup=post_choice_keyboard()
            )
        os.remove(tmp_path)
        return

    if action in ["post_twitter", "post_telegram", "post_both"]:
        base_text = post_data["text_ru"].strip()
        telegram_text = f"{base_text}\n\nЧитать больше: https://getaicoin.com/"
        twitter_text = build_twitter_post_ru(base_text)

        telegram_success = False
        twitter_success = False

        if action in ["post_telegram", "post_both"]:
            try:
                tmp_path = await prepare_photo_for_send(post_data["image_url"], channel_bot)
                with open(tmp_path, "rb") as f:
                    await channel_bot.send_photo(
                        chat_id=TELEGRAM_CHANNEL_USERNAME_ID,
                        photo=f,
                        caption=telegram_text
                    )
                os.remove(tmp_path)
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
            text="💬 Начинаем чат:\n" + post_data["text_ru"],
            reply_markup=main_keyboard()
        )
        return

    if action == "do_not_disturb":
        do_not_disturb["active"] = not do_not_disturb["active"]
        status = "включён" if do_not_disturb["active"] else "выключен"
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text=f"🌙 Режим «Не беспокоить» {status}.",
            reply_markup=main_keyboard()
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

async def delayed_start(app: Application):
    asyncio.create_task(send_post_for_approval())
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