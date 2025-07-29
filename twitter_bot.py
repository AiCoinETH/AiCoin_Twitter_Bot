
import os
import openai
import asyncio
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, Bot
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
)

TELEGRAM_BOT_TOKEN_APPROVAL = os.getenv("TELEGRAM_BOT_TOKEN_APPROVAL")
TELEGRAM_APPROVAL_CHAT_ID = os.getenv("TELEGRAM_APPROVAL_CHAT_ID")
TELEGRAM_APPROVAL_USER_ID = int(os.getenv("TELEGRAM_APPROVAL_USER_ID", "0"))

approval_bot = Bot(token=TELEGRAM_BOT_TOKEN_APPROVAL)

# Временная заглушка для send_post_for_approval
async def send_post_for_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await approval_bot.send_message(
        chat_id=TELEGRAM_APPROVAL_CHAT_ID,
        text="Пост для согласования: [заглушка]",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Опубликовать", callback_data="approve")],
            [InlineKeyboardButton("♻️ Заново", callback_data="regenerate")],
            [InlineKeyboardButton("🖼️ Картинку", callback_data="new_image")],
            [InlineKeyboardButton("💬 Поговорить", callback_data="chat")]
        ])
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"Вы нажали: {query.data}")

async def delayed_start(app: Application):
    await asyncio.sleep(2)
    await send_post_for_approval(None, None)

def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN_APPROVAL).post_init(delayed_start).build()
    app.add_handler(CallbackQueryHandler(button_handler))
    app.run_polling()

if __name__ == "__main__":
    main()
