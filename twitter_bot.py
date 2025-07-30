import os
import openai
import asyncio
import json
import hashlib
from datetime import datetime, timedelta
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, Bot
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters, CommandHandler
import aiosqlite

TELEGRAM_BOT_TOKEN_APPROVAL = os.getenv("TELEGRAM_BOT_TOKEN_APPROVAL")
TELEGRAM_APPROVAL_CHAT_ID = os.getenv("TELEGRAM_APPROVAL_CHAT_ID")
TELEGRAM_APPROVAL_USER_ID = int(os.getenv("TELEGRAM_APPROVAL_USER_ID", "0"))
TELEGRAM_PUBLIC_CHANNEL_ID = os.getenv("TELEGRAM_PUBLIC_CHANNEL_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai.api_key = OPENAI_API_KEY

approval_bot = Bot(token=TELEGRAM_BOT_TOKEN_APPROVAL)
post_data = {
    "text_ru": "Майнинговые токены снова в фокусе...",
    "text_en": "Mining tokens are gaining attention again...",
    "image_url": "https://upload.wikimedia.org/wikipedia/commons/4/47/PNG_transparency_demonstration_1.png",
    "timestamp": None
}
pending_post = {"active": False, "timer": None}
in_dialog = {"active": False}
do_not_disturb = {"active": False}
ru_variants = [
    "Майнинговые токены снова в фокусе...",
    "Инвесторы проявляют повышенный интерес к майнинговым токенам...",
    "Новые AI-алгоритмы меняют подход к добыче криптовалют..."
]
image_variants = [
    "https://upload.wikimedia.org/wikipedia/commons/thumb/a/a3/June_odd-eyed-cat.jpg/440px-June_odd-eyed-cat.jpg",
    "https://upload.wikimedia.org/wikipedia/commons/thumb/0/0a/Cat_03.jpg/480px-Cat_03.jpg",
    "https://upload.wikimedia.org/wikipedia/commons/thumb/5/5e/Sleeping_cat_on_her_back.jpg/480px-Sleeping_cat_on_her_back.jpg"
]
variant_index = 0
image_index = 0
DB_FILE = "post_history.db"
keyboard = InlineKeyboardMarkup([
    [InlineKeyboardButton("✅ Пост", callback_data="approve")],
    [InlineKeyboardButton("🕒 Подумать", callback_data="think")],
    [InlineKeyboardButton("📝 Новый текст", callback_data="regenerate")],
    [InlineKeyboardButton("🖼️ Новая картинка", callback_data="new_image")],
    [InlineKeyboardButton("🆕 Пост целиком", callback_data="new_post")],
    [InlineKeyboardButton("💬 Поговорить", callback_data="chat"), InlineKeyboardButton("🌙 Не беспокоить", callback_data="do_not_disturb")],
    [InlineKeyboardButton("🛑 Отменить", callback_data="cancel")]
])

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
    image_hash = get_image_hash(image_url) if image_url else None
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO posts (text, timestamp, image_hash) VALUES (?, ?, ?)",
            (text, datetime.now().isoformat(), image_hash)
        )
        await db.commit()

def get_image_hash(image_url):
    try:
        import requests
        response = requests.get(image_url)
        return hashlib.sha256(response.content).hexdigest()
    except Exception:
        return None

async def is_duplicate(text, image_url=None):
    image_hash = get_image_hash(image_url) if image_url else None
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT text, image_hash FROM posts WHERE timestamp > ?", ((datetime.now() - timedelta(days=30)).isoformat(),)) as cursor:
            async for row in cursor:
                if row[0] == text or (image_hash and row[1] == image_hash):
                    return True
    return False

async def send_post_for_approval(update: Update = None, context: ContextTypes.DEFAULT_TYPE = None):
    if do_not_disturb["active"]:
        return
    post_data["timestamp"] = datetime.now()
    pending_post["active"] = True
    pending_post["timer"] = datetime.now()

    await approval_bot.send_photo(
        chat_id=TELEGRAM_APPROVAL_CHAT_ID,
        photo=post_data["image_url"],
        caption=post_data["text_ru"],
        reply_markup=keyboard
    )

    countdown_msg = await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="⏳ Таймер: 60 секунд")

    async def update_countdown(message_id):
        for i in range(59, -1, -1):
            await asyncio.sleep(1)
            try:
                await approval_bot.edit_message_text(chat_id=TELEGRAM_APPROVAL_CHAT_ID, message_id=message_id, text=f"⏳ Таймер: {i} секунд")
            except:
                pass

    asyncio.create_task(update_countdown(countdown_msg.message_id))

async def publish_post():
    full_text = post_data["text_en"]
    footer = "... Продолжение на сайте https://getaicoin.com/ или телеграм канале t.me/AiCoin_ETH #AiCoin $Ai"
    max_length = 280 - len(footer)
    short_text = full_text[:max_length].rstrip() + " " + footer

    await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="🇬🇧 Английская версия: " + short_text)

    if TELEGRAM_PUBLIC_CHANNEL_ID:
        await approval_bot.send_photo(
            chat_id=TELEGRAM_PUBLIC_CHANNEL_ID,
            photo=post_data["image_url"],
            caption=post_data["text_en"] + "\n\n📎 Читайте нас также на сайте: https://getaicoin.com/"
        )

    await save_post_to_history(post_data["text_ru"], post_data["image_url"])
    await approval_bot.send_photo(
        chat_id=TELEGRAM_APPROVAL_CHAT_ID,
        photo=post_data["image_url"],
        caption=post_data["text_ru"] + "\n\nПолный текст: " + post_data["text_en"]
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global variant_index, image_index
    query = update.callback_query
    await query.answer()
    action = query.data

    if action == "approve":
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="✅ Пост опубликован.")
        pending_post["active"] = False
        await publish_post()
    elif action == "regenerate":
        variant_index = (variant_index + 1) % len(ru_variants)
        post_data["text_ru"] = ru_variants[variant_index]
        await send_post_for_approval()
    elif action == "new_image":
        image_index = (image_index + 1) % len(image_variants)
        post_data["image_url"] = image_variants[image_index]
        await send_post_for_approval()
    elif action == "new_post":
        # Заглушка для новой генерации
        post_data["text_ru"] = "[Заглушка] Новый пост."
        post_data["text_en"] = "[Placeholder] New post."
        post_data["image_url"] = image_variants[image_index]
        await send_post_for_approval()
    elif action == "chat":
        in_dialog["active"] = True
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="💬 [Заглушка] Начало чата с OpenAI\n" + post_data["text_ru"]
        )
    elif action == "do_not_disturb":
        do_not_disturb["active"] = True
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="🌙 Режим 'Не беспокоить' включен.")
    elif action == "cancel":
        pending_post["active"] = False
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="🛑 Публикация отменена.")
    elif action == "think":
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="🕒 Подумайте. Я жду решения.")
        await send_post_for_approval()

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not in_dialog["active"] or update.effective_user.id != TELEGRAM_APPROVAL_USER_ID:
        return
    if update.message.text.lower() == "/end":
        in_dialog["active"] = False
        await send_post_for_approval()
    else:
        await update.message.reply_text("🔁 Обсуждаем... Введите /end для завершения.")

async def check_timer():
    while True:
        await asyncio.sleep(5)
        if pending_post["active"] and pending_post["timer"] and not do_not_disturb["active"]:
            if datetime.now() - pending_post["timer"] > timedelta(seconds=60):
                await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="⌛ Время ожидания истекло. Публикую автоматически.")
                await publish_post()
                pending_post["active"] = False

async def delayed_start(app: Application):
    await asyncio.sleep(2)
    await init_db()
    await send_post_for_approval()
    asyncio.create_task(check_timer())

def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN_APPROVAL).post_init(delayed_start).build()
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CommandHandler("end", handle_message))
    app.run_polling()

if __name__ == "__main__":
    main()
