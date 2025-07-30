import os
import asyncio
from telegram import Bot

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")         # Токен вашего бота
CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")   # ID или username вашего канала (например, '@AiCoin_ETH' или '-1002868465126')

async def main():
    bot = Bot(token=TOKEN)
    try:
        msg = await bot.send_message(chat_id=CHANNEL_ID, text="🚀 Это тестовое сообщение от бота!")
        print("Успешно отправлено! message_id:", msg.message_id)
    except Exception as e:
        print("Ошибка при отправке:", e)

if __name__ == "__main__":
    asyncio.run(main())
