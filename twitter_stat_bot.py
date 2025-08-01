import os
import requests
import re
from telegram import Bot

NITTER_INSTANCES = [
    "nitter.poast.org",
    "nitter.kavin.rocks",
    "nitter.projectsegfau.lt",
    "nitter.unixfox.eu",
    "nitter.adminforge.de",
    "nitter.hu",
    "nitter.catalyst.sx",
    "nitter.privacydev.net",
    "nitter.1d4.us",
]

def get_twitter_followers(username):
    headers = {'User-Agent': 'Mozilla/5.0'}
    for host in NITTER_INSTANCES:
        url = f"https://{host}/{username}"
        print(f"Trying {url} ...")
        try:
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code != 200:
                print(f"Status {r.status_code} at {host}")
                continue
            # English + fallback
            match = re.search(r'(\d[\d,\.]*)\s+(Followers|читателей)', r.text)
            if match:
                return match.group(1).replace(',', '').replace('.', '')
        except Exception as e:
            print(f"Nitter error at {host}: {e}")
    return "N/A"

def update_telegram_message(followers):
    TELEGRAM_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN_CHANNEL')
    CHANNEL_ID = os.environ.get('TELEGRAM_CHANNEL_USERNAME_ID')
    MESSAGE_ID = int(os.environ.get('MESSAGE_ID'))
    bot = Bot(token=TELEGRAM_TOKEN)
    text = (
        f"🕊️ [Twitter](https://x.com/AiCoin_ETH): {followers} followers\n"
        f"🌐 [Website](https://getaicoin.com/)"
    )
    bot.edit_message_text(
        chat_id=CHANNEL_ID,
        message_id=MESSAGE_ID,
        text=text,
        parse_mode='Markdown'
    )
    print("Telegram message updated:", text)

if __name__ == "__main__":
    print("Script started")
    TWITTER_USERNAME = os.environ.get('TWITTER_USERNAME') or "AiCoin_ETH"
    print("TWITTER_USERNAME:", TWITTER_USERNAME)
    print(f"Parsing Nitter for user: {TWITTER_USERNAME}")
    followers = get_twitter_followers(TWITTER_USERNAME)
    print("Followers parsed:", followers)
    update_telegram_message(followers)