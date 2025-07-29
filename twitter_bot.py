
import os
import openai
import asyncio
import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, InputMediaPhoto
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
)
import time
from datetime import datetime

# Настройки логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

# Инициализация переменных среды
TELEGRAM_BOT_TOKEN_APPROVAL = os.getenv("TELEGRAM_BOT_TOKEN_APPROVAL")
TELEGRAM_APPROVAL_CHAT_ID = os.getenv("TELEGRAM_APPROVAL_CHAT_ID")
TELEGRAM_APPROVAL_USER_ID = int(os.getenv("TELEGRAM_APPROVAL_USER_ID", "0"))
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PINATA_JWT = os.getenv("PINATA_JWT")

openai.api_key = OPENAI_API_KEY

# === Хранилище состояния ===
approval_messages = {}

# === Кнопки под постами ===
def build_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Пост", callback_data="approve")],
        [InlineKeyboardButton("⏳ Продлить", callback_data="delay")],
        [InlineKeyboardButton("♻️ Ещё раз", callback_data="regenerate")],
        [InlineKeyboardButton("🖼️ Картинка", callback_data="image")],
        [InlineKeyboardButton("💬 Поговорить", callback_data="chat")],
        [InlineKeyboardButton("🧠 Своя тема", callback_data="custom")]
    ])

# === Примерная функция генерации текста и изображения ===
async def generate_post():
    return {
        "text_ru": "Это пример поста на русском языке для согласования.",
        "text_en": "This is a sample post in English for publishing.",
        "image_url": "https://via.placeholder.com/800x400.png?text=AI+Coin"
    }

# === Отправка поста на согласование ===
async def send_post_for_approval(context: ContextTypes.DEFAULT_TYPE):
    post = await generate_post()

    msg = await context.bot.send_photo(
        chat_id=TELEGRAM_APPROVAL_CHAT_ID,
        photo=post["image_url"],
        caption=post["text_ru"],
        reply_markup=build_keyboard(),
    )

    approval_messages[msg.message_id] = post

# === Обработка кнопок ===
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()

    if user_id != TELEGRAM_APPROVAL_USER_ID:
        await query.edit_message_caption(
            caption="⛔ Только администратор может управлять этим ботом."
        )
        return

    post = approval_messages.get(query.message.message_id)
    if not post:
        await query.edit_message_caption(caption="⚠️ Пост не найден.")
        return

    if query.data == "approve":
        await context.bot.send_photo(
            chat_id=TELEGRAM_CHANNEL_ID,
            photo=post["image_url"],
            caption=post["text_en"]
        )
        await query.edit_message_caption(caption="✅ Пост опубликован.")

    elif query.data == "delay":
        await query.edit_message_caption(caption="⏳ Отложено.")

    elif query.data == "regenerate":
        await query.edit_message_caption(caption="🔄 Генерация нового поста...")
        await send_post_for_approval(context)

    elif query.data == "image":
        await query.edit_message_caption(caption="🖼️ Генерация другой картинки...")
        await send_post_for_approval(context)

    elif query.data == "chat":
        await query.edit_message_caption(caption="💬 Переход в диалог...")
        await context.bot.send_message(chat_id=user_id, text="Готов поговорить. О чём пост?")

    elif query.data == "custom":
        await query.edit_message_caption(caption="🧠 Своя тема. Жду идею.")
        await context.bot.send_message(chat_id=user_id, text="Напиши тему вручную.")

# === Основной запуск ===
def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN_APPROVAL).build()

    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: None))
    app.add_handler(CommandHandler("start", lambda u, c: None))

    # ⏱ Тест: Запуск через 2 секунды
    async def delayed_start():
        await asyncio.sleep(2)
        await send_post_for_approval(app.bot)

    app.create_task(delayed_start())
    app.run_polling()

if __name__ == "__main__":
    main()
