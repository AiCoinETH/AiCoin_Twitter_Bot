import os
import requests
import asyncio
from telegram import Bot

# Переменные окружения
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN_CHANNEL')
CHANNEL_ID = os.environ.get('TELEGRAM_CHANNEL_USERNAME_ID')
MESSAGE_ID = int(os.environ.get('MESSAGE_ID'))
TWITTER_USERNAME = os.environ.get('TWITTER_USERNAME') or 'AiCoin_ETH'

# Заголовки из DevTools
HEADERS = {
    "authorization": "Bearer AAAAAAAAAAAAAAAAAAAAANIRLAAAAAAAb9g8OlU9nSTbKMrKm0I%2FQBhN0%3DMvb2Fo5dzsld2Aev5UlixkOUvxnTrw0OtY0U0BdKnM2KJh8D8D",
    "x-guest-token": "1694062146650529793",
    "x-csrf-token": "48a605b7320faeeb5ae2b846a84ecb91",
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.4 Safari/605.1.15",
    "x-twitter-active-user": "yes",
    "x-twitter-client-language": "en",
    "x-twitter-auth-type": "OAuth2Session",
    "accept-language": "en-GB,en;q=0.9",
    "content-type": "application/json"
}

def get_followers_from_graphql(username):
    url = "https://twitter.com/i/api/graphql/HYvLL37gkgw1TgJXAL6Wlw/UserByScreenName"
    params = {
        "variables": f'{{"screen_name":"{username}","withSafetyModeUserFields":true,"withSuperFollowsUserFields":true}}'
    }
    try:
        print(f"[DEBUG] Requesting: {url}")
        r = requests.get(url, headers=HEADERS, params=params, timeout=30)
        print("[DEBUG] Status code:", r.status_code)
        if r.status_code == 200:
            data = r.json()
            followers = data["data"]["user"]["result"]["legacy"]["followers_count"]
            return followers
        else:
            print("[ERROR] Twitter response:", r.text)
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