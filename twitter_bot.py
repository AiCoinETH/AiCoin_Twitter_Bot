import asyncio
import logging
from telegram import Bot

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

TOKEN = "ВАШ_ТОКЕН_БОТА"  # Вставьте сюда токен вашего бота
CHANNEL_ID = "-1002868465126"  # Или "@AiCoin_ETH"

async def main():
    bot = Bot(token=TOKEN)
    logging.info(f"Пробуем отправить сообщение в канал: {CHANNEL_ID}")
    try:
        message = await bot.send_message(chat_id=CHANNEL_ID, text="Тест публикации из асинхронного скрипта")
        logging.info(f"Сообщение успешно отправлено, ID: {message.message_id}")
    except Exception as e:
        logging.error(f"Ошибка при отправке сообщения: {e}")

if __name__ == "__main__":
    asyncio.run(main())
