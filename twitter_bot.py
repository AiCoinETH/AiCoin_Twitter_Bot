import os
import openai
import asyncio
import json
from datetime import datetime, timedelta
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, Bot, InputMediaPhoto
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters, CommandHandler

TELEGRAM_BOT_TOKEN_APPROVAL = os.getenv("TELEGRAM_BOT_TOKEN_APPROVAL")
TELEGRAM_APPROVAL_CHAT_ID = os.getenv("TELEGRAM_APPROVAL_CHAT_ID")
TELEGRAM_APPROVAL_USER_ID = int(os.getenv("TELEGRAM_APPROVAL_USER_ID", "0"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
POST_HISTORY_FILE = "post_history.json"
openai.api_key = OPENAI_API_KEY

approval_bot = Bot(token=TELEGRAM_BOT_TOKEN_APPROVAL)
post_data = {
    "text_ru": "Майнинговые токены снова в фокусе: интерес инвесторов растет на фоне появления новых AI-алгоритмов оптимизации добычи криптовалют. Это может изменить правила игры на рынке.",
    "text_en": "Mining tokens are gaining attention again as investors react to emerging AI algorithms optimizing crypto extraction. This could reshape the market.",
    "image_url": "https://upload.wikimedia.org/wikipedia/commons/4/47/PNG_transparency_demonstration_1.png",
    "timestamp": None
}

pending_post = {"active": False, "timer": None}
in_dialog = {"active": False}
do_not_disturb = {"active": False}

keyboard = InlineKeyboardMarkup([
    [InlineKeyboardButton("✅ Пост", callback_data="approve")],
    [InlineKeyboardButton("🕒 Подумать", callback_data="think")],
    [InlineKeyboardButton("♻️ Еще один", callback_data="regenerate")],
    [InlineKeyboardButton("🖼️ Картинку", callback_data="new_image")],
    [InlineKeyboardButton("💬 Поговорить", callback_data="chat"), InlineKeyboardButton("🌙 Не беспокоить", callback_data="do_not_disturb")],
    [InlineKeyboardButton("🛑 Отменить", callback_data="cancel"), InlineKeyboardButton("✅ Завершить диалог", callback_data="end_dialog")]
])

ru_variants = [
    "Майнинговые токены снова в фокусе...",
    "Инвесторы проявляют повышенный интерес к майнинговым токенам...",
    "Новые AI-алгоритмы меняют подход к добыче криптовалют..."
]
variant_index = 0

def load_post_history():
    if not os.path.exists(POST_HISTORY_FILE):
        return []
    with open(POST_HISTORY_FILE, "r") as file:
        history = json.load(file)
    threshold = datetime.now() - timedelta(days=30)
    history = [entry for entry in history if datetime.fromisoformat(entry["timestamp"]) > threshold]
    with open(POST_HISTORY_FILE, "w") as file:
        json.dump(history, file)
    return history

import hashlib
import requests

def get_image_hash(image_url):
    try:
        response = requests.get(image_url)
        return hashlib.sha256(response.content).hexdigest()
    except Exception:
        return None

def save_post_to_history(text, image_url=None):
    history = load_post_history()
    image_hash = get_image_hash(image_url) if image_url else None
    history.append({"text": text, "timestamp": datetime.now().isoformat(), "image_hash": image_hash})
    with open(POST_HISTORY_FILE, "w") as file:
        json.dump(history, file)

def is_duplicate(text, image_url=None):
    history = load_post_history()
    image_hash = get_image_hash(image_url) if image_url else None
    for entry in history:
        if entry["text"] == text:
            return True
        if image_hash and entry.get("image_hash") == image_hash:
            return True
    return False

async def send_post_for_approval(update: Update = None, context: ContextTypes.DEFAULT_TYPE = None):
    if do_not_disturb["active"]:
        return
    post_data["timestamp"] = datetime.now()
    pending_post["active"] = True
    pending_post["timer"] = datetime.now()
    msg = await approval_bot.send_photo(
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
    if do_not_disturb["active"]:
        return
    post_data["timestamp"] = datetime.now()
    pending_post["active"] = True
    await approval_bot.send_photo(
        chat_id=TELEGRAM_APPROVAL_CHAT_ID,
        photo=post_data["image_url"],
        caption=post_data["text_ru"],
        reply_markup=keyboard
    )
    pending_post["timer"] = datetime.now()
    await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="⏳ Таймер: 60 секунд до автопубликации")
    if do_not_disturb["active"]:
        return
    post_data["timestamp"] = datetime.now()
    pending_post["active"] = True
    await approval_bot.send_photo(
        chat_id=TELEGRAM_APPROVAL_CHAT_ID,
        photo=post_data["image_url"],
        caption=post_data["text_ru"],
        reply_markup=keyboard
    )

async def publish_post():
    save_post_to_history(post_data["text_ru"])
    await approval_bot.send_photo(
        chat_id=TELEGRAM_APPROVAL_CHAT_ID,
        photo=post_data["image_url"],
        caption=post_data["text_ru"] + "\n\nПолный текст: " + post_data["text_en"]
    )
    twitter_text = post_data["text_en"][:220] + "... Продолжение в Telegram: t.me/AiCoin_ETH или на https://getaicoin.com/ #AiCoin $Ai"
    print("Twitter пост:", twitter_text)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global variant_index
    query = update.callback_query
    await query.answer()
    action = query.data

    if action == "end_dialog":
        in_dialog["active"] = False
        await send_post_for_approval()
        return
    global variant_index
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
        await approval_bot.send_photo(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            photo=post_data["image_url"],
            caption="♻️ Новый пост: " + post_data["text_ru"],
            reply_markup=keyboard
        )
    elif action == "new_image":
        post_data["image_url"] = "https://upload.wikimedia.org/wikipedia/commons/4/47/PNG_transparency_demonstration_1.png"
        await approval_bot.send_photo(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            photo=post_data["image_url"],
            caption="🖼️ Новый пост: Заглушка",
            reply_markup=keyboard
        )
    elif action == "chat":
        in_dialog["active"] = True
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="💬 Заглушка: генерация пока отключена. Введите любое сообщение.")
    elif action == "do_not_disturb":
        do_not_disturb["active"] = True
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="🌙 Режим 'Не беспокоить' включен. Бот не будет отправлять уведомления.")
    elif action == "cancel":
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="🛑 Публикация отменена.")
        pending_post["active"] = False
    elif action == "think":
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="🕒 Подумайте. Я жду решения. ⏳ Таймер: 60 секунд")
        pending_post["timer"] = datetime.now()
        pending_post["active"] = True

async def check_timer():
    while True:
        await asyncio.sleep(5)
        if pending_post["active"] and pending_post["timer"] and not do_not_disturb["active"]:
            elapsed = datetime.now() - pending_post["timer"]
            if elapsed > timedelta(seconds=60):
                await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="⌛ Время ожидания истекло. Публикую автоматически.")
                await publish_post()
                pending_post["active"] = False

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text.lower() == "/end":
        in_dialog["active"] = False
        await send_post_for_approval()
        return
    if not in_dialog["active"] or update.effective_user.id != TELEGRAM_APPROVAL_USER_ID:
        return
    await update.message.reply_text("🔁 Заглушка: генерация отключена. Введите /end для завершения.")

async def delayed_start(app: Application):
    await asyncio.sleep(2)
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
