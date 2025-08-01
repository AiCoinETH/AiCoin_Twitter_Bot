import os
import re
import requests
from telegram import Bot

# --- SETTINGS ---
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN_CHANNEL')
CHANNEL_ID     = os.environ.get('TELEGRAM_CHANNEL_USERNAME_ID')
MESSAGE_ID     = int(os.environ.get('MESSAGE_ID'))
TWITTER_USERNAME = os.environ.get('TWITTER_USERNAME') or 'AiCoin_ETH'

# --- ACTUAL NITTER INSTANCES (можешь расширить) ---
NITTER_INSTANCES = [
    "nitter.poast.org",
    "nitter.in.projectsegfau.lt",
    "nitter.hu",
    "nitter.pufe.org",
    "nitter.privacydev.net",
    "nitter.moomoo.me",
    "nitter.42l.fr",
    "nitter.1d4.us",
]

def get_twitter_followers_nitter(username):
    headers = {'User-Agent': 'Mozilla/5.0'}
    for instance in NITTER_INSTANCES:
        try:
            url = f"https://{instance}/{username}"
            print(f"Trying {url} ...")
            r = requests.get(url, headers=headers, timeout=10)
            # Ищем статистику подписчиков (Followers) в ответе
            match = re.search(r'profile-stat-num">([\d,\.]+)</span>\s*Followers', r.text)
            if match:
                followers = match.group(1).replace(',', '').replace('.', '')
                print(f"Followers found: {followers} at {instance}")
                return followers
        except Exception as e:
            print(f"Nitter error at {instance}: {e}")
    return "N/A"

def update_telegram_message(followers):
    bot = Bot(token=TELEGRAM_TOKEN)
    text = (
        "🕊️ [Twitter](https://x.com/{username}): {followers} followers\n"
        "🌐 [Website](https://getaicoin.com/)"
    ).format(username=TWITTER_USERNAME, followers=followers)
    # Отправляем отредактированное сообщение в канал
    bot.edit_message_text(
        chat_id=CHANNEL_ID,
        message_id=MESSAGE_ID,
        text=text,
        parse_mode='Markdown'
    )
    print(f"Telegram message updated: {text}")

if __name__ == "__main__":
    print("Script started")
    print("TWITTER_USERNAME:", TWITTER_USERNAME)
    print(f"Parsing Nitter for user: {TWITTER_USERNAME}")
    followers = get_twitter_followers_nitter(TWITTER_USERNAME)
    print(f"Followers parsed: {followers}")
    update_telegram_message(followers)