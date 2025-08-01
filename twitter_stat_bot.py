import os
import json
import requests
import asyncio
from telegram import Bot
from telegram.error import BadRequest

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN_CHANNEL')
CHANNEL_ID = os.environ.get('TELEGRAM_CHANNEL_USERNAME_ID')
MESSAGE_ID = int(os.environ.get('MESSAGE_ID'))
TWITTER_USERNAME = os.environ.get('TWITTER_USERNAME') or 'AiCoin_ETH'

def get_followers_via_api(username: str) -> str | None:
    headers = {
        "Authorization": "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAgB5dTfR2wYXFY9UL6p4ZQJrkNdo%3D...cut",
        "X-Guest-Token": "1490980653852325888",
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json"
    }

    variables = {
        "screen_name": username,
        "withSafetyModeUserFields": True,
        "withSuperFollowsUserFields": True
    }

    params = {
        "variables": json.dumps(variables)
    }

    url = "https://twitter.com/i/api/graphql/-xfUfZ2o5Wuj5GnE1UuBPA/UserByScreenName"

    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
        data = response.json()
        followers = data["data"]["user"]["result"]["legacy"]["followers_count"]
        print(f"[INFO] Fetched followers: {followers}")
        return str(followers)
    except Exception as e:
        print(f"[ERROR] Twitter API fetch failed: {e}")
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

    followers = get_followers_via_api(TWITTER_USERNAME)
    if not followers:
        followers = "N/A"

    print("Followers parsed:", followers)
    await update_telegram_message(followers)

if __name__ == "__main__":
    asyncio.run(main())