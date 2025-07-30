import logging
from telegram import Bot

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

# Замените на ваш токен и ID/username канала
TOKEN = "ВАШ_ТОКЕН_БОТА"
CHANNEL_ID = "-1002868465126"  # Или "@AiCoin_ETH"

def main():
    bot = Bot(token=TOKEN)
    try:
        message = bot.send_message(chat_id=CHANNEL_ID, text="Тест публикации из telegram_channel_test.py")
        logging.info(f"Сообщение успешно отправлено, ID: {message.message_id}")
    except Exception as e:
        logging.error(f"Ошибка при отправке сообщения: {e}")

if __name__ == "__main__":
    main()
