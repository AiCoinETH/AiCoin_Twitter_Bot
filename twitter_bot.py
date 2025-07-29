
import os
import logging
import asyncio
import openai
import random
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.constants import ParseMode

# === Константы окружения ===
TELEGRAM_BOT_TOKEN_APPROVAL = os.getenv("TELEGRAM_BOT_TOKEN_APPROVAL")
TELEGRAM_APPROVAL_CHAT_ID = os.getenv("TELEGRAM_APPROVAL_CHAT_ID")
TELEGRAM_APPROVAL_USER_ID = int(os.getenv("TELEGRAM_APPROVAL_USER_ID", "0"))

# === Настройка логирования ===
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# === Заглушка для генерации текста и картинки ===
async def generate_post():
    return "Пример текста поста", "https://placekitten.com/800/400"

# === Функция отправки поста на одобрение ===
async def send_post_for_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, image_url = await generate_post()
    keyboard = [
        [
            InlineKeyboardButton("✅ Одобрить", callback_data="approve"),
            InlineKeyboardButton("🔁 Переделать", callback_data="regenerate"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await context.bot.send_photo(
        chat_id=TELEGRAM_APPROVAL_CHAT_ID,
        photo=image_url,
        caption=f"*Новая новость (русский вариант)*\n\n{text}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup,
    )

# === Обработка кнопок ===
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "approve":
        await query.edit_message_caption(caption="✅ Пост одобрен.")
    elif query.data == "regenerate":
        await send_post_for_approval(update, context)

# === Автозапуск при старте ===
async def post_init_hook(app):
    await app.bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="🤖 Бот запущен.")
    await send_post_for_approval(None, app)

# === Основной запуск ===
def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN_APPROVAL).post_init(post_init_hook).build()

    app.add_handler(CallbackQueryHandler(button_handler))
    app.run_polling()

if __name__ == "__main__":
    main()
