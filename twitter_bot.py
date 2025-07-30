import os
import asyncio
import logging
from telegram import Bot

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")  # Здесь будет '@AiCoin_ETH'

async def main():
    if not TOKEN or not CHANNEL_ID:
        logging.error("Переменные окружения для токена или канала не заданы")
        return

    bot = Bot(token=TOKEN)
    logging.info(f"Отправляем сообщение в канал: {CHANNEL_ID}")

    try:
        message = await bot.send_message(chat_id=CHANNEL_ID, text="Тест публикации в канал через username")
        logging.info(f"Сообщение успешно отправлено, id: {message.message_id}")
    except Exception as e:
        logging.error(f"Ошибка при отправке сообщения: {e}")

if __name__ == "__main__":
    asyncio.run(main())
