import pandas as pd
import datetime
import openai
import requests
import tweepy
import telegram
import os

# === Настройки из GitHub Secrets ===
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TWITTER_API_KEY = os.getenv("API_KEY")
TWITTER_API_SECRET = os.getenv("API_SECRET")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
ACCESS_SECRET = os.getenv("ACCESS_SECRET")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")

# === Подключение к Twitter ===
auth = tweepy.OAuth1UserHandler(TWITTER_API_KEY, TWITTER_API_SECRET, ACCESS_TOKEN, ACCESS_SECRET)
twitter_api = tweepy.API(auth)

# === Подключение к Telegram ===
telegram_bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)

# === Загрузка CSV из GitHub (можно заменить на локальный путь) ===
schedule_url = "https://raw.githubusercontent.com/YOUR_USERNAME/YOUR_REPO/main/content_schedule.csv"
schedule = pd.read_csv(schedule_url)

# === Текущая дата и слот ===
today = datetime.date.today().strftime("%Y-%m-%d")
now_hour = datetime.datetime.now().hour

if now_hour < 12:
    slot = "Morning News"
elif now_hour < 18:
    slot = "Afternoon Engagement"
else:
    slot = "Evening Promo"

row = schedule[(schedule['Date'] == today) & (schedule['Slot'] == slot)]

if row.empty:
    print("Нет темы для текущего слота.")
    exit()

# === Генерация текста через GPT ===
openai.api_key = OPENAI_API_KEY

topic = row.iloc[0]['Final Post Theme'] or row.iloc[0]['Google Trend Topic'] or "Ai Coin and AI revolution"
prompt = f"Write a short Twitter post about: {topic}"

response = openai.ChatCompletion.create(
    model="gpt-4o",
    messages=[
        {"role": "system", "content": "You're an AI social media strategist."},
        {"role": "user", "content": prompt}
    ]
)

tweet_text = response['choices'][0]['message']['content'].strip()

# === Загрузка картинки с IPFS (если CID указан) ===
image_url = None
cid = row.iloc[0]['Image CID (Pinata)']
if cid:
    image_url = f"https://gateway.pinata.cloud/ipfs/{cid}"
    img_data = requests.get(image_url).content
    with open("temp_img.jpg", "wb") as f:
        f.write(img_data)
    twitter_api.update_status_with_media(status=tweet_text, filename="temp_img.jpg")
else:
    twitter_api.update_status(status=tweet_text)

# === Публикация в Telegram ===
telegram_bot.send_message(chat_id=TELEGRAM_CHANNEL_ID, text=tweet_text)
if cid:
    telegram_bot.send_photo(chat_id=TELEGRAM_CHANNEL_ID, photo=open(image_url, 'rb'))

print(f"Пост опубликован: {slot} — {topic}")
