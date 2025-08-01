import os
import requests
import re
import asyncio
from telegram import Bot

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN_CHANNEL')
CHANNEL_ID = os.environ.get('TELEGRAM_CHANNEL_USERNAME_ID')
MESSAGE_ID = int(os.environ.get('MESSAGE_ID'))
TWITTER_USERNAME = os.environ.get('TWITTER_USERNAME')

NITTER_INSTANCES = [
    "nitter.net",
    "nitter.poast.org",
    "nitter.privacydev.net",
    "nitter.moomoo.me"
    # Добавь сюда другие свежие инстансы, если найдёшь!
]

def get_twitter_followers_nitter(username):
    headers = {'User-Agent': 'Mozilla/5.0'}
    for instance in NITTER_INSTANCES:
        try:
            url = f"https://{instance}/{username}"
            r = requests.get(url, headers=headers, timeout=10)
            match = re.search(r'profile-stat-num">([\d,\.]+)</span>\s*Followers', r.text)
            if match:
                return match.group(1).replace(',', '').replace('.', '')
        except Exception as e:
            print(f"Nitter error at {instance}: {e}")
    return "N/A"

async def update_telegram_message(followers):
    bot = Bot(token=TELEGRAM_TOKEN)
    text = (
        "🕊️ [Twitter](https://x.com/AiCoin_ETH): {} followers\n"
        "🌐 [Website](https://getaicoin.com/)"
    ).format(followers)
    await bot.edit_message_text(
        chat_id=CHANNEL_ID,
        message_id=MESSAGE_ID,
        text=text,
        parse_mode='Markdown'
    )

if __name__ == "__main__":
    print("Script started")
    print("TWITTER_USERNAME:", TWITTER_USERNAME)
    print("Parsing Nitter for user:", TWITTER_USERNAME)
    followers = get_twitter_followers_nitter(TWITTER_USERNAME)
    print("Followers parsed:", followers)
    asyncio.run(update_telegram_message(followers))