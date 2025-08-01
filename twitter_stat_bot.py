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
        try:
            await page.goto(f"https://twitter.com/{username}", timeout=60000)
            await page.wait_for_selector('div[data-testid="followers"] span span', timeout=45000)
            count_text = await page.locator('div[data-testid="followers"] span span').first.inner_text()
            return count_text.strip()
        except Exception as e:
            print(f"Playwright error: {e}")
            return None
        finally:
            await browser.close()

async def update_telegram_message(followers):
    bot = Bot(token=TELEGRAM_TOKEN)
    text = (
        f"🕊️ [Twitter](https://x.com/{TWITTER_USERNAME}): {followers} followers\n"
        f"🌐 [Website](https://getaicoin.com/)"
    )
    await bot.edit_message_text(
        chat_id=CHANNEL_ID,
        message_id=MESSAGE_ID,
        text=text,
        parse_mode='Markdown'
    )
    print("Telegram message updated:", text)

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