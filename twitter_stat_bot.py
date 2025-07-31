import os
import requests
import re
from telegram import Bot

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN_CHANNEL')
CHANNEL_ID = os.environ.get('TELEGRAM_CHANNEL_USERNAME_ID')
MESSAGE_ID = int(os.environ.get('MESSAGE_ID'))
TWITTER_USERNAME = os.environ.get('TWITTER_USERNAME')

def get_twitter_followers(username):
    url = f'https://twitter.com/{username}'
    headers = {'User-Agent': 'Mozilla/5.0'}
    r = requests.get(url, headers=headers)
    match = re.search(r'(\d[\d,]*) Followers', r.text)
    if match:
        return match.group(1).replace(',', '')
    else:
        return None

def update_telegram_message(followers):
    bot = Bot(token=TELEGRAM_TOKEN)
    text = f"В Twitter сейчас {followers} подписчиков!"
    bot.edit_message_text(chat_id=CHANNEL_ID, message_id=MESSAGE_ID, text=text)

if __name__ == "__main__":
    followers = get_twitter_followers(TWITTER_USERNAME)
    if followers:
        update_telegram_message(followers)
    else:
        print("Не удалось получить количество подписчиков")
