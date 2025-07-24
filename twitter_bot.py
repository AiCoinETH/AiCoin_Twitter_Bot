# === Модуль автопостинга и комментирования ===
# Создаётся по техническому заданию от пользователя

import os
import openai
import requests
import tweepy
import telegram
import datetime
import json
import time
import pandas as pd
from github import Github
from io import BytesIO

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

# --- Авторизация ---
auth = tweepy.OAuth1UserHandler(TWITTER_API_KEY, TWITTER_API_SECRET, ACCESS_TOKEN, ACCESS_SECRET)
twitter_api = tweepy.API(auth)
telegram_bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
github = Github(GITHUB_TOKEN)
repo = github.get_repo("AiCoinETH/AiCoin_Twitter_Bot")

# --- Помощник: генерация текста ---
def generate_post_text(trending_topic):
    openai.api_key = OPENAI_API_KEY
    prompt = f"Напиши короткий пост для Twitter на тему: '{trending_topic}', добавь хештеги #AiCoin, #AI"
    response = openai.ChatCompletion.create(
        model="gpt-4",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=180
    )
    return response.choices[0].message['content'].strip()

# --- Генерация изображения через DALL-E ---
def generate_image(prompt):
    response = openai.Image.create(
        prompt=prompt,
        n=1,
        size="1024x1024"
    )
    image_url = response['data'][0]['url']
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
    # Twitter
    media = twitter_api.media_upload(image_path)
    tweet = twitter_api.update_status(status=text, media_ids=[media.media_id])

    # Telegram
    with open(image_path, 'rb') as img:
        telegram_bot.send_photo(chat_id=TELEGRAM_CHANNEL_ID, photo=img, caption=text)

    return tweet.id_str

# --- Основной обработчик ---
def main():
    now = datetime.datetime.now()
    hour = now.hour
    date = now.strftime("%Y-%m-%d")

    trending_topic = "AI Coin в тренде по США, Пакистану и Нигерии"
    hashtags = "#AiCoin #AI #CryptoNews"
    reason = "Повышенный интерес к теме AI Coin в Google Trends и Twitter"
    sources = "https://trends.google.com, https://twitter.com/search?q=%23AiCoin"

    post_text = generate_post_text(trending_topic + ". " + reason)
    image_bytes, image_preview_url = generate_image(trending_topic)
    ipfs_hash = upload_to_pinata(image_bytes, f"promo_{date}_{hour}.png")
    image_path = f"promo_{date}_{hour}.png"

    # Сохраняем изображение временно
    with open(image_path, "wb") as f:
        f.write(image_bytes.getbuffer())

    tweet_id = post_to_socials(post_text, image_path)
    os.remove(image_path)

    # --- Обновление trending_log.csv ---
    file_log = repo.get_contents("date_time/main/trending_log.csv")
    log_df = pd.read_csv(BytesIO(file_log.decoded_content))

    log_df = pd.concat([log_df, pd.DataFrame([{
        "date": date,
        "hour": hour,
        "google_trend": "ai coin",
        "twitter_trend": "#AiCoin",
        "combined_topic": trending_topic,
        "news_sources": sources,
        "hashtags_used": hashtags,
        "reason": reason,
        "image_ipfs_url": f"https://gateway.pinata.cloud/ipfs/{ipfs_hash}"
    }])], ignore_index=True)

    updated_log = log_df.to_csv(index=False)
    repo.update_file("date_time/main/trending_log.csv", f"Update trending log {date} {hour}", updated_log, file_log.sha)

    print("\U0001F4AC Пост опубликован и залогирован.")

# --- Комментарии: обработка откликов ---
def handle_comments(tweet_id):
    replies = twitter_api.search_tweets(q=f'to:AiCoinETH', since_id=tweet_id)
    for reply in replies:
        text = reply.text.lower()
        if any(word in text for word in ["partner", "collab", "cooperation"]):
            twitter_api.update_status(status="Thank you! Let's discuss on our site: https://AiCoinETH.com", in_reply_to_status_id=reply.id)
        elif any(word in text for word in ["ai", "coin", "token"]):
            twitter_api.update_status(status="Glad you're interested! #AiCoin is the future of decentralized AI.", in_reply_to_status_id=reply.id)
        else:
            twitter_api.update_status(status="Thanks for your comment! Discover more about our project at https://AiCoinETH.com", in_reply_to_status_id=reply.id)

# === Запуск ===
if __name__ == "__main__":
    main()
    time.sleep(180)  # Ждём 3 минуты перед обработкой комментариев
    # handle_comments(tweet_id)  # tweet_id должен быть сохранён отдельно при запуске
