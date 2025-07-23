import os
import tweepy
import requests

# Twitter Auth
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
ACCESS_SECRET = os.getenv("ACCESS_SECRET")

auth = tweepy.OAuth1UserHandler(API_KEY, API_SECRET, ACCESS_TOKEN, ACCESS_SECRET)
twitter_api = tweepy.API(auth)

# Telegram Auth
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")

def get_news():
    # Заменить на реальную генерацию или API позже
    return "🧠 AI revolution continues! Follow @AiCoin_ETH 🚀 #AiCoin #Web3 #Crypto"

def post_to_twitter(text):
    twitter_api.update_status(text)
    print("✅ Опубликовано в Twitter")

def post_to_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHANNEL_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    response = requests.post(url, data=payload)
    if response.status_code == 200:
        print("✅ Опубликовано в Telegram")
    else:
        print("⚠️ Ошибка Telegram:", response.text)

if __name__ == "__main__":
    news = get_news()
    post_to_twitter(news)
    post_to_telegram(news)
