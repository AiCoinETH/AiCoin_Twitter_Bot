import os
import asyncio
from telegram import Bot

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN_APPROVAL")  # Токен берём из secrets
CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")     # Id канала берём из secrets

async def main():
    bot = Bot(token=TOKEN)
    try:
        msg = await bot.send_message(chat_id=CHANNEL_ID, text="✅ Тестовое сообщение от Telegram-бота!")
        print("Текст успешно отправлен! Message id:", msg.message_id)

        photo_msg = await bot.send_photo(
            chat_id=CHANNEL_ID,
            photo="https://upload.wikimedia.org/wikipedia/commons/4/47/PNG_transparency_demonstration_1.png",
            caption="🖼️ Тестовое изображение"
        )
        print("Фото успешно отправлено! Message id:", photo_msg.message_id)

    except Exception as e:
        print("Ошибка при публикации:", e)

if __name__ == "__main__":
    asyncio.run(main())
