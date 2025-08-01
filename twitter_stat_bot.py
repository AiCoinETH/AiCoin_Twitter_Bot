import os
import requests
import asyncio
from telegram import Bot

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN_CHANNEL')
CHANNEL_ID = os.environ.get('TELEGRAM_CHANNEL_USERNAME_ID')
MESSAGE_ID = int(os.environ.get('MESSAGE_ID'))
TWITTER_USERNAME = os.environ.get('TWITTER_USERNAME') or 'AiCoin_ETH'

AUTHORIZATION = "Bearer AAAAAAAAAAAAAAAAAAAAANIRLAAAAAAAb9g8OlU9nSTbKMrKm0I%2FQBhN0%3DMvb2Fo5dzsld2Aev5UlixkOUvxnTrw0OtY0U0BdKnM2KJh8D8D"

def get_guest_token():
    r = requests.post(
        "https://api.twitter.com/1.1/guest/activate.json",
        headers={"authorization": AUTHORIZATION},
        timeout=10
    )
    if r.status_code == 200:
        return r.json()["guest_token"]
    else:
        print("[ERROR] Failed to get guest token:", r.status_code)
        return None

def get_followers_from_graphql(username):
    guest_token = get_guest_token()
    if not guest_token:
        return None

    headers = {
        "authorization": AUTHORIZATION,
        "x-guest-token": guest_token,
        "user-agent": "Mozilla/5.0"
    }
    url = "https://twitter.com/i/api/graphql/HYvLL37gkgw1TgJXAL6Wlw/UserByScreenName"
    params = {
        "variables": f'{{"screen_name":"{username}","withSafetyModeUserFields":true,"withSuperFollowsUserFields":true}}'
    }
    try:
        r = requests.get(url, headers=headers, params=params, timeout=30)
        if r.status_code == 200:
            data = r.json()
            return data["data"]["user"]["result"]["legacy"]["followers_count"]
        else:
            print("[ERROR] Twitter response:", r.status_code, r.text)
    except Exception as e:
        print("[ERROR]", e)
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
        print("[Telegram] Message updated")
    except Exception as e:
        print("[Telegram ERROR]", e)

async def main():
    print("Script started")
    print("TWITTER_USERNAME:", TWITTER_USERNAME)

    followers = get_followers_from_graphql(TWITTER_USERNAME)
    if not followers:
        followers = "N/A"

    print("Followers parsed:", followers)
    await update_telegram_message(followers)

if __name__ == "__main__":
    asyncio.run(main())