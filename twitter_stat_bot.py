import os
import re
import requests
import asyncio
from telegram import Bot
from telegram.error import BadRequest

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN_CHANNEL')
CHANNEL_ID = os.environ.get('TELEGRAM_CHANNEL_USERNAME_ID')
MESSAGE_ID = int(os.environ.get('MESSAGE_ID'))
TWITTER_USERNAME = os.environ.get('TWITTER_USERNAME') or 'AiCoin_ETH'

def get_followers_from_html(username):
    try:
        url = f"https://twitter.com/{username}"
        headers = {
            "User-Agent": "Mozilla/5.0"
        }
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code != 200:
            print(f"[ERROR] Twitter page returned status {r.status_code}")
            return None

        match = re.search(r'"followers_count":(\d+)', r.text)
        if match:
            followers = match.group(1)
            print(f"[INFO] Found followers: {followers}")
            return followers
        else:
            print("[WARN] Regex match not found in HTML")
            return None
    except Exception as e:
        print(f"[ERROR] HTML fetch failed: {e}")
        return None

async def update_telegram_message(followers):
    bot = Bot(token=TELEGRAM_TOKEN)
    text = (
        f"🕊️ [Twitter](https://x.com/{TWITTER_USERNAME}): {followers} followers\n"
        f"🌐 [Website](https://getaicoin.com/)"
    )
    try:
        await bot.edit_message_text(
            chat_id=CHANNEL_ID,
            message_id=MESSAGE_ID,
            text=text,
            parse_mode='Markdown'
        )
        print("[Telegram] Message updated.")
    except BadRequest as e:
        if "Message is not modified" in str(e):
            print("[Telegram] Message unchanged — skipping.")
        else:
            raise

async def main():
    print("Script started")
    print("TWITTER_USERNAME:", TWITTER_USERNAME)

    followers = get_followers_from_html(TWITTER_USERNAME)
    if not followers:
        followers = "N/A"

    print("Followers parsed:", followers)
    await update_telegram_message(followers)

if __name__ == "__main__":
    asyncio.run(main())