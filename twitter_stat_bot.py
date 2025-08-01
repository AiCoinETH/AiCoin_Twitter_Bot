import os
import requests
import re
import asyncio
from telegram import Bot
from telegram import __version__ as tg_ver
from playwright.async_api import async_playwright

print("python-telegram-bot version:", tg_ver)

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN_CHANNEL')
CHANNEL_ID = os.environ.get('TELEGRAM_CHANNEL_USERNAME_ID')
MESSAGE_ID = int(os.environ.get('MESSAGE_ID'))
TWITTER_USERNAME = os.environ.get('TWITTER_USERNAME') or 'AiCoin_ETH'

def get_followers_from_nitter(username):
    # ... без изменений ...
    return None

def get_followers_from_socialblade(username):
    # ... без изменений ...
    return None

async def get_followers_from_twitter(username: str) -> str | None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
        page = await context.new_page()

        followers_count = None

        def handle_response(response):
            if "UserByScreenName" in response.url and response.status == 200:
                asyncio.create_task(parse_json(response))

        async def parse_json(response):
            nonlocal followers_count
            try:
                json_data = await response.json()
                followers_count = (
                    json_data.get("data", {})
                    .get("user", {})
                    .get("result", {})
                    .get("legacy", {})
                    .get("followers_count")
                )
            except Exception as e:
                print(f"JSON parse error: {e}")

        page.on("response", handle_response)

        try:
            await page.goto(f"https://twitter.com/{username}", timeout=60000)
            await page.wait_for_selector('[data-testid="primaryColumn"]', timeout=60000)
            await asyncio.sleep(5)  # дождись XHR-запросов
        except Exception as e:
            print(f"Playwright error: {e}")

        await browser.close()
        return str(followers_count) if followers_count else None

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
        print("Telegram message updated:", text)
    except Exception as e:
        if "Message is not modified" in str(e):
            print("Telegram: message unchanged — skipping.")
        else:
            raise

async def main():
    print("Script started")
    print("TWITTER_USERNAME:", TWITTER_USERNAME)

    followers = get_followers_from_nitter(TWITTER_USERNAME)
    if not followers:
        print("Nitter failed, trying Socialblade")
        followers = get_followers_from_socialblade(TWITTER_USERNAME)
    if not followers:
        print("Socialblade failed, trying Twitter directly")
        followers = await get_followers_from_twitter(TWITTER_USERNAME)
    if not followers:
        followers = "N/A"

    print("Followers parsed:", followers)
    await update_telegram_message(followers)

if __name__ == "__main__":
    asyncio.run(main())