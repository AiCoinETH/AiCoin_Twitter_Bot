import os
import requests
import re
from telegram import Bot

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN_CHANNEL')
CHANNEL_ID = os.environ.get('TELEGRAM_CHANNEL_USERNAME_ID')
MESSAGE_ID = int(os.environ.get('MESSAGE_ID'))
TWITTER_USERNAME = os.environ.get('TWITTER_USERNAME') or "AiCoin_ETH"

def format_number(num):
    try:
        num = int(num)
        return f"{num:,}"
    except Exception:
        # Например, 1.2K, 3.5M и т.п.
        return str(num)

def get_twitter_followers(username):
    url = f'https://x.com/{username}'
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36'
    }
    response = requests.get(url, headers=headers, timeout=15)
    if response.status_code != 200:
        print(f"X (Twitter) error: {response.status_code}")
        return None
    # Ищем варианты 123,456 Followers, 1,2K Followers, 3.4M Followers и т.д.
    match = re.search(r'(\d[\d,\.]*[KM]?)\s+Followers', response.text)
    if match:
        val = match.group(1).replace(',', '')
        # Переводим 1.2K, 3.4M → 1200, 3400000
        if val.lower().endswith('k'):
            return int(float(val[:-1].replace(',', '.')) * 1000)
        if val.lower().endswith('m'):
            return int(float(val[:-1].replace(',', '.')) * 1000000)
        try:
            return int(val)
        except Exception:
            return val
    return None

def update_telegram_message(followers):
    bot = Bot(token=TELEGRAM_TOKEN)
    followers_str = format_number(followers)
    text = (
        f"🕊️ [Twitter](https://x.com/{TWITTER_USERNAME}): {followers_str} followers\n"
        "🌐 [Website](https://getaicoin.com/)"
    )
    bot.edit_message_text(
        chat_id=CHANNEL_ID,
        message_id=MESSAGE_ID,
        text=text,
        parse_mode='Markdown'
    )

if __name__ == "__main__":
    followers = get_twitter_followers(TWITTER_USERNAME)
    print(f"Current Twitter followers: {followers}")
    if followers is not None:
        update_telegram_message(followers)
    else:
        print("Could not get follower count!")