import asyncio
import logging
from telegram import Bot

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

TOKEN = "8097657551:AAFEpfksrlBc2-2PZ-ieAJg0_T3mheUv7jk"
CHANNEL_ID = "@AiCoin_ETH"  # Используем username канала

async def main():
    bot = Bot(token=TOKEN)
    logging.info(f"Пытаемся отправить сообщение в канал {CHANNEL_ID}")
    try:
        message = await bot.send_message(chat_id=CHANNEL_ID, text="Тест публикации из скрипта")
        logging.info(f"Сообщение отправлено успешно, id: {message.message_id}")
    except Exception as e:
        logging.error(f"Ошибка при отправке сообщения: {e}")

if __name__ == "__main__":
    asyncio.run(main())
