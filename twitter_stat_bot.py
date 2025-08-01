
import os
import re
import asyncio
import json
from playwright.async_api import async_playwright
from telegram import Bot

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN_CHANNEL')
CHANNEL_ID = os.environ.get('TELEGRAM_CHANNEL_USERNAME_ID')
MESSAGE_ID = int(os.environ.get('MESSAGE_ID'))
TWITTER_USERNAME = os.environ.get('TWITTER_USERNAME') or 'AiCoin_ETH'

async def get_followers_via_xhr(username: str) -> str | None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        try:
            await page.route("**/UserByScreenName**", lambda route, request: route.continue_())
            xhr_response = {}

            page.on("response", lambda response: xhr_response.update({response.url: response}) if "UserByScreenName" in response.url else None)

            await page.goto(f"https://twitter.com/{username}", timeout=60000)
            await asyncio.sleep(10)

            for url, response in xhr_response.items():
                if response.status == 200:
                    body = await response.text()
                    data = json.loads(body)
                    return str(data["data"]["user"]["result"]["legacy"]["followers_count"])
        except Exception as e:
            print(f"[Playwright XHR ERROR] {e}")
        finally:
            await browser.close()
    return None

async def update_telegram_message(followers):
    bot = Bot(token=TELEGRAM_TOKEN)
    text = (
        f"ðï¸ [Twitter](https://x.com/{TWITTER_USERNAME}): {followers} followers\n"
        f"ð [Website](https://getaicoin.com/)"
    )
    try:
        await bot.edit_message_text(
            chat_id=CHANNEL_ID,
            message_id=MESSAGE_ID,
            text=text,
            parse_mode='Markdown'
        )
        print("[Telegram] Message updated:", text)
    except Exception as e:
        print("[Telegram ERROR]", e)

async def main():
    print("Script started")
    print("TWITTER_USERNAME:", TWITTER_USERNAME)

    followers = await get_followers_via_xhr(TWITTER_USERNAME)
    if not followers:
        followers = "N/A"

    print("Followers parsed:", followers)
    await update_telegram_message(followers)

if __name__ == "__main__":
    asyncio.run(main())