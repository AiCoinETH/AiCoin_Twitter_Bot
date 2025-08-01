import os
import requests
import re
from telegram import Bot

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN_CHANNEL')
CHANNEL_ID = os.environ.get('TELEGRAM_CHANNEL_USERNAME_ID')
MESSAGE_ID = int(os.environ.get('MESSAGE_ID'))
TWITTER_USERNAME = os.environ.get('TWITTER_USERNAME')  # например, 'AiCoin_ETH'

def get_twitter_followers_nitter(username):
    # Используем nitter.net, если недоступен — меняй на другой инстанс
    url = f'https://nitter.1d4.us/{username}'
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code != 200:
            return None
        html = r.text
        # Парсим строчку вида <span class="profile-stat-num">11</span> Followers
        match = re.search(r'profile-stat-num">([\d,\.]+)<.*?Followers', html, re.DOTALL)
        if not match:
            # fallback для русскоязычного инстанса или других версий
            match = re.search(r'profile-stat-num">([\d,\.]+)<', html)
        if match:
            return match.group(1).replace(',', '').replace('.', '')
    except Exception as e:
        print("Nitter error:", e)
    return None

def update_telegram_message(followers):
    bot = Bot(token=TELEGRAM_TOKEN)
    text = (
        "🕊️ [Twitter](https://x.com/AiCoin_ETH): {} followers\n"
        "🌐 [Website](https://getaicoin.com/)"
    ).format(followers if followers is not None else '0')
    bot.edit_message_text(
        chat_id=CHANNEL_ID,
        message_id=MESSAGE_ID,
        text=text,
        parse_mode='Markdown'
    )

if __name__ == "__main__":
    print("Script started")
    print("TWITTER_USERNAME:", TWITTER_USERNAME)
    print("Парсим Nitter для пользователя:", TWITTER_USERNAME)
    followers = get_twitter_followers_nitter(TWITTER_USERNAME)
    print("Followers parsed:", followers)
    if followers is not None:
        update_telegram_message(followers)
        print("Telegram message updated!")
    else:
        print("Failed to get followers count!")