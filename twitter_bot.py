# === Модуль автопостинга и комментирования ===
# Обновлён по заданию: 3 поста в день, 1 из них — обязательный рекламный в прайм-тайм (22:00 по Киеву / UTC+3)

import os
import requests
import tweepy
import telegram
import datetime
import json
import time
import pandas as pd
from github import Github
from io import BytesIO
from openai import OpenAI

# --- Константы и настройки ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TWITTER_API_KEY = os.getenv("API_KEY")
TWITTER_API_SECRET = os.getenv("API_SECRET")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
ACCESS_SECRET = os.getenv("ACCESS_SECRET")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
PINATA_JWT = os.getenv("PINATA_JWT")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
BEARER_TOKEN = os.getenv("BEARER_TOKEN")

# --- Авторизация ---
auth = tweepy.OAuth1UserHandler(TWITTER_API_KEY, TWITTER_API_SECRET, ACCESS_TOKEN, ACCESS_SECRET)
twitter_api = tweepy.API(auth)
telegram_bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
github = Github(GITHUB_TOKEN)
repo = github.get_repo("AiCoinETH/AiCoin_Twitter_Bot")
client = OpenAI(api_key=OPENAI_API_KEY)

# --- Хештеги для анализа Twitter ---
TWITTER_HASHTAGS = ["AiCoin", "AI", "OpenAI", "xAI", "AICrypto", "AIToken"]

# --- Помощник: генерация текста ---
def generate_post_text(trending_topic):
    prompt = f"Напиши короткий пост для Twitter на тему: '{trending_topic}', добавь хештеги #AiCoin, #AI"
    response = client.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=180
    )
    return response.choices[0].message.content.strip()

# --- Генерация изображения через DALL-E ---
def generate_image(prompt):
    response = client.images.generate(
        prompt=prompt,
        n=1,
        size="1024x1024"
    )
    image_url = response.data[0].url
    image_data = requests.get(image_url).content
    return BytesIO(image_data), image_url

# --- Загрузка в Pinata ---
def upload_to_pinata(image_bytes, filename):
    url = "https://api.pinata.cloud/pinning/pinFileToIPFS"
    headers = {"Authorization": f"Bearer {PINATA_JWT}"}
    files = {'file': (filename, image_bytes)}
    response = requests.post(url, files=files, headers=headers)
    return response.json()['IpfsHash']

# --- Публикация в Twitter и Telegram ---
def post_to_socials(text, image_path):
    media = twitter_api.media_upload(image_path)
    tweet = twitter_api.update_status(status=text, media_ids=[media.media_id])
    with open(image_path, 'rb') as img:
        telegram_bot.send_photo(chat_id=TELEGRAM_CHANNEL_ID, photo=img, caption=text)
    return tweet.id_str

# --- Поиск твитов по хештегам ---
def get_twitter_trends_by_hashtag(hashtag):
    headers = {"Authorization": f"Bearer {BEARER_TOKEN}"}
    url = f"https://api.twitter.com/2/tweets/search/recent?query=%23{hashtag}&max_results=5&tweet.fields=text"
    try:
        response = requests.get(url, headers=headers)
        tweets = response.json().get("data", [])
        return [tweet["text"] for tweet in tweets]
    except Exception as e:
        print(f"Ошибка получения твитов по #{hashtag}: {e}")
        return []
# --- Комментарии к постам ---
def handle_comments(tweet_id):
    replies = twitter_api.search_tweets(q=f'to:AiCoinETH', since_id=tweet_id)
    for reply in replies:
        text = reply.text.lower()
        if any(word in text for word in ["partner", "collab", "cooperation"]):
            twitter_api.update_status(status="Thanks! Let's talk at https://getaicoin.com/", in_reply_to_status_id=reply.id)
        elif any(word in text for word in ["ai", "coin", "token"]):
            twitter_api.update_status(status="Great thoughts! Check out #AiCoin — future of decentralized AI. Learn more at https://getaicoin.com/", in_reply_to_status_id=reply.id)
        else:
            twitter_api.update_status(status="Thanks for the comment! Learn more about our project at https://getaicoin.com/", in_reply_to_status_id=reply.id)
# --- Основной логический блок ---
def should_post_now():
    now = datetime.datetime.now()
    return now.hour in [9, 14, 22]

def main():
    now = datetime.datetime.now()
    date = now.strftime("%Y-%m-%d")
    hour = now.hour

    file_log = repo.get_contents("date_time/main/trending_log.csv")
    log_df = pd.read_csv(BytesIO(file_log.decoded_content))
    today_posts = log_df[log_df['date'] == date]
    if len(today_posts) >= 3:
        print("⏰ Лимит постов достигнут")
        return

    is_promo_hour = hour == 22
    all_trends = []
    for tag in TWITTER_HASHTAGS:
        all_trends.extend(get_twitter_trends_by_hashtag(tag))

    twitter_trend = all_trends[0] if all_trends else "#AiCoin"
    trending_topic = f"AI Coin и {twitter_trend} в тренде"
    reason = "Выбор на основе Twitter и Google Trends активности"
    hashtags = "#AiCoin #AI #CryptoNews"
    sources = "https://trends.google.com, https://twitter.com/search?q=%23AiCoin"

    if not is_promo_hour and len(today_posts) >= 2:
        print("⏳ Ждём вечернего рекламного поста")
        return

    post_text = generate_post_text(trending_topic + ". " + reason)
    image_bytes, _ = generate_image(trending_topic)
    image_path = f"promo_{date}_{hour}.png"
    with open(image_path, "wb") as f:
        f.write(image_bytes.getbuffer())

    ipfs_hash = upload_to_pinata(image_bytes, image_path)
    ipfs_url = f"https://gateway.pinata.cloud/ipfs/{ipfs_hash}"
    tweet_id = post_to_socials(post_text, image_path)
    os.remove(image_path)

    new_entry = pd.DataFrame([{
        "date": date,
        "hour": hour,
        "google_trend": "ai coin",
        "twitter_trend": twitter_trend,
        "combined_topic": trending_topic,
        "news_sources": sources,
        "hashtags_used": hashtags,
        "reason": reason,
        "image_ipfs_url": ipfs_url
    }])

    log_df = pd.concat([log_df, new_entry], ignore_index=True)
    updated_log = log_df.to_csv(index=False)
    repo.update_file("date_time/main/trending_log.csv", f"Update log {date} {hour}", updated_log, file_log.sha)

    print("📈 Пост опубликован")
    time.sleep(180)  # ждём 3 минуты
    handle_comments(tweet_id)

if __name__ == "__main__":
    if should_post_now():
        main()
    else:
        print("⏱ Не время для постинга")
