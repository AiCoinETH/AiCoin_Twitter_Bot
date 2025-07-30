import os
import asyncio
import hashlib
import logging
import random
import re
from datetime import datetime, timedelta
from pytz import timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, Bot
from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters, ContextTypes
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

approval_bot = Bot(token=TELEGRAM_BOT_TOKEN_APPROVAL)
channel_bot = Bot(token=TELEGRAM_BOT_TOKEN_CHANNEL)

KIEV_TZ = timezone('Europe/Kyiv')

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

# ========== КЛАВИАТУРЫ ==========
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

# ========== АНТИ-ДУБЛИКАТ (только текст) ==========
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

# ========== ИИ-ЗАГЛУШКИ ==========
async def ai_generate_text():
    await asyncio.sleep(0.6)  # имитируем работу AI
    return f"✨ [AI] Новый сгенерированный текст поста. #{random.randint(1,9999)}"

async def ai_generate_image():
    await asyncio.sleep(0.4)
    return random.choice(test_images)

async def ai_generate_full():
    return await ai_generate_text(), await ai_generate_image()

# ========== АВТО-ГЕНЕРАЦИЯ УНИКАЛЬНОГО ТЕКСТА ==========
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
    logging.info("База данных инициализирована.")

def get_image_hash(url: str) -> str | None:
    try:
        import requests
        r = requests.get(url, timeout=3)
        r.raise_for_status()
        return hashlib.sha256(r.content).hexdigest()
    except Exception as e:
        logging.warning(f"Не удалось получить хеш изображения: {e}")
        return None

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

    if show_back is None:
        show_back = bool(post_history)
    post_data["timestamp"] = datetime.now()
    pending_post.update({"active": True, "timer": datetime.now()})
    try:
        photo_msg = await approval_bot.send_photo(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            photo=post_data["image_url"],
            caption=post_data["text_ru"],
            reply_markup=build_keyboard(show_back)
        )
        approval_message_ids["photo"] = photo_msg.message_id
        logging.info("Пост отправлен на согласование.")
    except Exception as e:
        logging.error(f"Ошибка при отправке на согласование: {e}")

# ========== ПУБЛИКАЦИЯ В КАНАЛ И TWITTER (заглушка) ==========
async def auto_publish_everywhere(post_data):
    await channel_bot.send_photo(
        chat_id=TELEGRAM_CHANNEL_USERNAME_ID,
        photo=post_data["image_url"],
        caption=post_data["text_en"] + "\n\n🌐 https://getaicoin.com/"
    )
    twitter_text = build_twitter_post(post_data["text_en"])
    logging.info(f"[TWITTER] Опубликовано: {twitter_text}")

# ========== ТАЙМЕР МОДЕРАЦИИ И АВТОВЫКЛЮЧЕНИЕ ==========
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
        if pending_post["active"] and pending_post.get("timer") and (datetime.now() - pending_post["timer"]) > timedelta(seconds=60):
            try:
                await approval_bot.send_message(
                    chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                    text="⌛ Время ожидания истекло. Публикую автоматически."
                )
            except Exception:
                pass
            await auto_publish_everywhere(post_data)
            pending_post["active"] = False

# ========== ОБРАБОТЧИК КНОПОК ==========
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_action_time, prev_data
    await update.callback_query.answer()
    user_id = update.effective_user.id

    # --- Проверка: идёт ли генерация? ---
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

    # ====== РЕЖИМЫ ======
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

    # --- "✅ Пост" ---
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
            await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="✅ Успешно отправлено в Twitter!")
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
            await channel_bot.send_photo(
                chat_id=TELEGRAM_CHANNEL_USERNAME_ID,
                photo=post_data["image_url"],
                caption=post_data["text_en"] + "\n\n🌐 https://getaicoin.com/"
            )
            await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="✅ Успешно отправлено в Telegram!")
            await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="✅ Успешно отправлено в Twitter!")
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

    # --- Генерации через ИИ ---
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

    # --- Поговорить (чат режим) ---
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

    # --- Режим "Не беспокоить" ---
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

    # --- Завершить день ---
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

    # --- Снова включить модерацию/публикации ---
    if action == "enable_moderation":
        do_not_disturb.update({"active": False, "until": None, "reason": None})
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="Согласование и публикации снова включены.",
            reply_markup=build_keyboard(show_back=bool(post_history))
        )
        return

# ========== ЧАТ-МОД РЕАЛИЗАЦИЯ ==========
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("chat_mode"):
        user_text = update.message.text
        answer = f"🤖 [AI] Ответ на: {user_text}\n(Тут будет генерация от ИИ)"
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=answer)
        if user_text.strip().lower() in ['завершить', 'end', 'стоп', 'готово']:
            context.user_data["chat_mode"] = False
            post_history.append(post_data.copy())
            post_data["text_ru"] = f"📝 [AI Chat] Итоговый пост: {user_text}"  # Можно подставить результат диалога
            await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="✅ Беседа завершена. Новый пост создан!")
            await send_post_for_approval(show_back=True)

# ========== ЗАПУСК ==========
async def delayed_start(app: Application):
    await init_db()
    await send_post_for_approval(show_back=False)
    asyncio.create_task(check_timer())
    logging.info("Бот запущен и готов к работе.")

def main():
    logging.info("Старт Telegram бота модерации и публикации…")
    app = Application.builder()\
        .token(TELEGRAM_BOT_TOKEN_APPROVAL)\
        .post_init(delayed_start)\
        .build()
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), message_handler))
    app.run_polling(poll_interval=0.12, timeout=1)

if __name__ == "__main__":
    main()
