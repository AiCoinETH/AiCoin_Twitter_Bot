import os
import asyncio
from telegram import Bot
from playwright.async_api import async_playwright

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN_CHANNEL"]
CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_USERNAME_ID"]
MESSAGE_ID = int(os.environ["MESSAGE_ID"])
TWITTER_USERNAME = os.environ.get("TWITTER_USERNAME", "AiCoin_ETH")

async def get_followers_from_twitter(username):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        try:
            await page.goto(f"https://twitter.com/{username}", timeout=60000)
            await page.wait_for_selector('a[href$="/followers"] > div > div > span > span', timeout=60000)
            text = await page.locator('a[href$="/followers"] > div > div > span > span').inner_text()
            return text.strip().replace(',', '')
        except Exception as e:
            print(f"[Playwright ERROR] {e}")
            return None
        finally:
            await browser.close()

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
    followers = await get_followers_from_twitter(TWITTER_USERNAME)
    if not followers:
        followers = "N/A"
    print("Followers parsed:", followers)
    await update_telegram_message(followers)

if __name__ == "__main__":
    asyncio.run(main())