import os
import requests
import re
from telegram import Bot

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN_CHANNEL')
CHANNEL_ID = os.environ.get('TELEGRAM_CHANNEL_USERNAME_ID')
MESSAGE_ID = int(os.environ.get('MESSAGE_ID'))
TWITTER_USERNAME = os.environ.get('TWITTER_USERNAME') or "AiCoin_ETH"  # Фоллбэк

def get_twitter_followers(username):
    url = f'https://x.com/{username}'
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36'
    }
    response = requests.get(url, headers=headers, timeout=15)
    if response.status_code != 200:
        print(f"Ошибка запроса X: {response.status_code}")
        return None
    # Регулярка ищет "12,345 Followers" или "1,2 тыс. Followers"
    match = re.search(r'(\d[\d,\. ]*)\s+Followers', response.text)
    if match:
        val = match.group(1).replace(',', '').replace(' ', '').replace('.', '')
        try:
            # Бывают строки типа "1,2 тыс.", но чаще просто число
            return int(val)
        except Exception:
            return val
    return None

def update_telegram_message(followers):
    bot = Bot(token=TELEGRAM_TOKEN)
    # 🕊️ — голубь-эмодзи; можно заменить!
    text = (
        f"🕊️ [Twitter](https://x.com/{TWITTER_USERNAME}): {followers} подписчиков\n"
        "🌐 [Сайт](https://getaicoin.com/)"
    )
    bot.edit_message_text(
        chat_id=CHANNEL_ID,
        message_id=MESSAGE_ID,
        text=text,
        parse_mode='Markdown'
    )

if __name__ == "__main__":
    followers = get_twitter_followers(TWITTER_USERNAME)
    print(f"Текущее количество подписчиков: {followers}")
    if followers is not None:
        update_telegram_message(followers)
    else:
        print("Не удалось получить количество подписчиков!")