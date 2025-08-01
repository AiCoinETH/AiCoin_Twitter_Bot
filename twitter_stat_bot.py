import os
import re
import requests
from telegram import Bot

# --- Получаем данные из переменных окружения ---
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN_CHANNEL')
CHANNEL_ID = os.environ.get('TELEGRAM_CHANNEL_USERNAME_ID')
MESSAGE_ID = int(os.environ.get('MESSAGE_ID'))
TWITTER_USERNAME = os.environ.get('TWITTER_USERNAME')

def get_twitter_followers(username):
    print(f"Парсим X (Twitter) для пользователя: {username}")
    url = f'https://x.com/{username}'
    headers = {'User-Agent': 'Mozilla/5.0'}
    r = requests.get(url, headers=headers, timeout=10)
    # Проверяем оба варианта: Followers (EN) и читателей (RU)
    match = re.search(r'(\d[\d,\.]*)\s+(Followers|читателей)', r.text)
    if match:
        print("Нашли совпадение:", match.group(0))
        return match.group(1).replace(',', '').replace('.', '')
    else:
        print("Не удалось найти число подписчиков на странице!")
        return None

def update_telegram_message(followers):
    bot = Bot(token=TELEGRAM_TOKEN)
    # Английский текст для канала, все ссылки кликабельные (Markdown)
    text = (
        "🕊️ [Twitter](https://x.com/AiCoin_ETH): {} followers\n"
        "🌐 [Website](https://getaicoin.com/)"
    ).format(followers)
    bot.edit_message_text(
        chat_id=CHANNEL_ID,
        message_id=MESSAGE_ID,
        text=text,
        parse_mode='Markdown'
    )
    print("Telegram message updated!")

if __name__ == "__main__":
    print("Script started")
    print("TWITTER_USERNAME:", TWITTER_USERNAME)
    followers = get_twitter_followers(TWITTER_USERNAME)
    print("Followers parsed:", followers)
    if followers:
        update_telegram_message(followers)
    else:
        print("Failed to get followers count!")