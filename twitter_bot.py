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

# --- Здесь храним, кто в режиме редактирования текста ---
user_editing_text = set()

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

# --- (далее все функции upload_image_to_github, download_image_async и прочие оставляем без изменений) ---

# ========= Роутер сообщений =========
async def message_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # --- Если пользователь в режиме редактирования текста ---
    if user_id in user_editing_text:
        text = update.message.text or ""
        image_url = post_data["image_url"]
        if update.message.photo:
            image_url = await process_telegram_photo(update.message.photo[-1].file_id, approval_bot)
        post_data["text_ru"] = text
        post_data["image_url"] = image_url
        user_editing_text.remove(user_id)

        # Обновляем пост в чате одобрения
        try:
            await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="✏️ Пост успешно изменён:")
            await send_photo_with_download(
                approval_bot,
                TELEGRAM_APPROVAL_CHAT_ID,
                post_data["image_url"],
                caption=post_data["text_ru"],
                reply_markup=post_choice_keyboard()
            )
        except Exception as e:
            logging.error(f"Ошибка при отправке обновлённого поста: {e}")
        return

    # --- "Сделай сам" (ручной режим) ---
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
                        caption=text
                    )
                elif text:
                    await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=text)
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
                    caption=user_self_post[user_id]['text']
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
        # Включаем пользователя в режим редактирования
        user_editing_text.add(user_id)
        try:
            await approval_bot.send_message(
                chat_id=user_id,
                text="✏️ Редактируй текст поста здесь (можно добавить фото) и отправь мне обратно. Текущий текст:\n\n" + post_data["text_ru"]
            )
            await approval_bot.send_message(
                chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                text="Пользователь начал редактирование поста в личных сообщениях.",
                reply_markup=main_keyboard()
            )
        except Exception as e:
            logging.error(f"Ошибка отправки личного сообщения пользователю для редактирования: {e}")
        return

    # --- Здесь остальные действия кнопок без изменений ---

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
                    await send_photo_with_download(approval_bot, TELEGRAM_APPROVAL_CHAT_ID, image_url, caption=twitter_text, reply_markup=post_choice_keyboard())
                else:
                    await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=twitter_text, reply_markup=post_choice_keyboard())
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
        await send_photo_with_download(approval_bot, TELEGRAM_APPROVAL_CHAT_ID, post_data["image_url"], caption=twitter_text)
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="Выберите площадку:", reply_markup=post_choice_keyboard())
        return

    if action in ["post_twitter", "post_telegram", "post_both"]:
        base_text = post_data["text_ru"].strip()
        telegram_text = f"{base_text}\n\nLearn more: https://getaicoin.com/"
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
        if user_id in user_editing_text:
            user_editing_text.remove(user_id)
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
        post_data["text_ru"] = f"Ручной новый пост #{post_data['post_id'] + 1}"
        post_data["image_url"] = random.choice(test_images)
        post_data["post_id"] += 1
        post_data["is_manual"] = True
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

# (Остальные функции, автопостинг, таймеры, startup/shutdown — оставляем без изменений)

async def delayed_start(app: Application):
    await init_db()
    asyncio.create_task(schedule_daily_posts())
    asyncio.create_task(check_timer())
    await send_photo_with_download(
        approval_bot,
        TELEGRAM_APPROVAL_CHAT_ID,
        post_data["image_url"],
        caption=post_data["text_ru"] + "\n\n" + WELCOME_HASHTAGS,
        reply_markup=main_keyboard()
    )

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