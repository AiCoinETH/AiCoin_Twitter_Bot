import logging
from telegram import Bot

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

TOKEN = "ВАШ_ТОКЕН_БОТА"  # Ваш токен
CHANNEL_ID = "-1002868465126"  # Или "@AiCoin_ETH"

def main():
    bot = Bot(token=TOKEN)
    logging.info(f"Пробуем отправить сообщение в канал: {CHANNEL_ID}")
    try:
        message = bot.send_message(chat_id=CHANNEL_ID, text="Тест публикации из скрипта")
        logging.info(f"Сообщение отправлено успешно, ID: {message.message_id}")
    except Exception as e:
        logging.error(f"Ошибка при отправке сообщения: {e}")

if __name__ == "__main__":
    main()
