import os
import openai
import asyncio
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, Bot
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
)
from datetime import datetime

TELEGRAM_BOT_TOKEN_APPROVAL = os.getenv("TELEGRAM_BOT_TOKEN_APPROVAL")
TELEGRAM_APPROVAL_CHAT_ID = os.getenv("TELEGRAM_APPROVAL_CHAT_ID")
TELEGRAM_APPROVAL_USER_ID = int(os.getenv("TELEGRAM_APPROVAL_USER_ID", "0"))

approval_bot = Bot(token=TELEGRAM_BOT_TOKEN_APPROVAL)
post_data = {"text": "Пост для согласования: [заглушка]", "image": "URL"}

pending_post = {"active": True, "timer": None}

keyboard = InlineKeyboardMarkup([
    [InlineKeyboardButton("✅ Опубликовать", callback_data="approve")],
    [InlineKeyboardButton("♻️ Заново", callback_data="regenerate")],
    [InlineKeyboardButton("🖼️ Картинку", callback_data="new_image")],
    [InlineKeyboardButton("💬 Поговорить", callback_data="chat")],
    [InlineKeyboardButton("🛑 Отменить", callback_data="cancel")],
    [InlineKeyboardButton("🕒 Подумать", callback_data="think")]
])

async def send_post_for_approval(update: Update = None, context: ContextTypes.DEFAULT_TYPE = None):
    try:
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text=post_data["text"],
            reply_markup=keyboard
        )
    except Exception as e:
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"❌ Ошибка при отправке поста: {e}")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data

    if action == "approve":
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="✅ Пост опубликован.")
        # Здесь будет логика публикации
    elif action == "regenerate":
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="♻️ Генерирую новый пост...")
        await send_post_for_approval()
    elif action == "new_image":
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="🖼️ Генерирую новую картинку...")
    elif action == "chat":
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="💬 Переход в режим диалога. Чем могу помочь?")
    elif action == "cancel":
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="🛑 Публикация отменена.")
    elif action == "think":
        pending_post["timer"] = datetime.now()
        pending_post["active"] = True
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="🕒 Подумайте. Я жду решения.")
    else:
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"❓ Неизвестная команда: {action}")

async def delayed_start(app: Application):
    await asyncio.sleep(2)
    await send_post_for_approval()

def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN_APPROVAL).post_init(delayed_start).build()
    app.add_handler(CallbackQueryHandler(button_handler))
    app.run_polling()

if __name__ == "__main__":
    main()
