import os
import requests
import re
import asyncio
from telegram import Bot
from playwright.async_api import async_playwright

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN_CHANNEL')
CHANNEL_ID = os.environ.get('TELEGRAM_CHANNEL_USERNAME_ID')
MESSAGE_ID = int(os.environ.get('MESSAGE_ID'))
TWITTER_USERNAME = os.environ.get('TWITTER_USERNAME') or 'AiCoin_ETH'

def get_followers_from_nitter(username):
    nitter_instances = [
        "https://nitter.poast.org", "https://nitter.projectsegfau.lt", "https://nitter.hu",
        "https://nitter.unixfox.eu", "https://nitter.adminforge.de", "https://nitter.privacydev.net",
        "https://nitter.moomoo.me", "https://nitter.catalyst.sx", "https://nitter.1d4.us",
        "https://nitter.42l.fr", "https://nitter.pufe.org"
    ]
    for base in nitter_instances:
        url = f"{base}/{username}"
        print(f"Trying {url} ...")
        try:
            r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
            if r.status_code != 200:
                print(f"Status {r.status_code} at {base}")
                continue
            match = re.search(r'([\d,\.]+)\s+Followers', r.text)
            if match:
                return match.group(1).replace(',', '').replace('.', '')
        except Exception as e:
            print(f"Nitter error at {base}: {e}")
    return None

def get_followers_from_socialblade(username):
    url = f"https://socialblade.com/twitter/user/{username.lower()}"
    print(f"Trying Socialblade: {url}")
    try:
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
        if r.status_code != 200:
            print(f"Socialblade returned {r.status_code}")
            return None
        match = re.search(r'Twitter Followers[\s\S]{0,100}?class="(?:[^"]*?)(?:Counter|YouTubeUserTopLight).*?>([\d,\.]+)<', r.text)
        if match:
            return match.group(1).replace(',', '').replace('.', '')
        match = re.search(r'<span class="BadgeValue">([\d,\.]+)</span>', r.text)
        if match:
            return match.group(1).replace(',', '').replace('.', '')
    except Exception as e:
        print(f"Socialblade error: {e}")
    return None

async def get_followers_from_twitter(username: str) -> str | None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
        page = await context.new_page()
        try:
            await page.goto(f"https://twitter.com/{username}", timeout=60000)
            await page.wait_for_selector('a[href$="/followers"] span span', timeout=30000)
            count_text = await page.locator('a[href$="/followers"] span span').first.inner_text()
            return count_text.strip()
        except Exception as e:
            print(f"Playwright error: {e}")
            return None
        finally:
            await browser.close()

def update_telegram_message(followers):
    bot = Bot(token=TELEGRAM_TOKEN)
    text = (
        f"🕊️ [Twitter](https://x.com/{TWITTER_USERNAME}): {followers} followers\n"
        f"🌐 [Website](https://getaicoin.com/)"
    )
    bot.edit_message_text(
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
    update_telegram_message(followers)

if __name__ == "__main__":
    asyncio.run(main())