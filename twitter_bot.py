import os
import asyncio
import logging
from telegram import Bot

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")  # будет '@AiCoin_ETH'

if not TOKEN or not CHANNEL_ID:
    logging.error("Переменные окружения для токена или канала не заданы")
    exit(1)

async def main():
    bot = Bot(token=TOKEN)
    logging.info(f"Отправляю сообщение в канал {CHANNEL_ID}")
    try:
        msg = await bot.send_message(chat_id=CHANNEL_ID, text="Тест публикации через username")
        logging.info(f"Сообщение успешно отправлено, ID: {msg.message_id}")
    except Exception as e:
        logging.error(f"Ошибка отправки: {e}")

if __name__ == "__main__":
    asyncio.run(main())

