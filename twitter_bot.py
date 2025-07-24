import pandas as pd
import datetime
import openai
import requests
import tweepy
import telegram
import os
import json
import time
import threading
from pytrends.request import TrendReq
from telegram.ext import Updater, MessageHandler, Filters
from github import Github

# === Настройки из GitHub Secrets ===
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TWITTER_API_KEY = os.getenv("API_KEY")
TWITTER_API_SECRET = os.getenv("API_SECRET")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
ACCESS_SECRET = os.getenv("ACCESS_SECRET")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
BEARER_TOKEN = os.getenv("BEARER_TOKEN")
PINATA_JWT = os.getenv("PINATA_JWT")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

# === Подключение к Twitter ===
auth = tweepy.OAuth1UserHandler(TWITTER_API_KEY, TWITTER_API_SECRET, ACCESS_TOKEN, ACCESS_SECRET)
twitter_api = tweepy.API(auth)

# === Подключение к Telegram ===
telegram_bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
updater = Updater(token=TELEGRAM_BOT_TOKEN, use_context=True)
dispatcher = updater.dispatcher

# === Подключение к GitHub ===
g = Github(GITHUB_TOKEN)
repo = g.get_repo("AiCoinETH/AiCoin_Twitter_Bot")

# === Получение CSV расписания ===
schedule_url = "https://raw.githubusercontent.com/AiCoinETH/AiCoin_Twitter_Bot/main/date_time/content_schedule.csv"
try:
    schedule = pd.read_csv(schedule_url)
except Exception as e:
    print("❌ Ошибка загрузки расписания:", e)
    schedule = pd.DataFrame(columns=["date", "time", "category", "topic"])

# === Получение трендов из Google Trends ===
pytrends = TrendReq(hl='en-US', tz=360)
pytrends.build_payload(kw_list=["AI", "Ai Coin"])
top_trending_google = pytrends.related_queries().get("AI", {}).get("top", pd.DataFrame())
google_trend = top_trending_google.iloc[0]['query'] if not top_trending_google.empty else "AI News"

# === Получение трендов из Twitter по хештегам ===
def get_twitter_trends_by_hashtag(hashtag):
    headers = {"Authorization": f"Bearer {BEARER_TOKEN}"}
    url = f"https://api.twitter.com/2/tweets/search/recent?query=%23{hashtag}&max_results=10&tweet.fields=text"
    try:
        response = requests.get(url, headers=headers)
        tweets = response.json().get("data", [])
        if tweets:
            return [tweet["text"] for tweet in tweets]
    except Exception as e:
        print(f"Twitter hashtag error: {e}")
    return []

twitter_keywords = []
for tag in ["AiCoin", "AI", "OpenAI", "xAi", "ArtificialIntelligence", "AICrypto", "AIToken"]:
    twitter_keywords.extend(get_twitter_trends_by_hashtag(tag))

twitter_trending_topic = twitter_keywords[0] if twitter_keywords else "#AiCoin"
trending_topic = f"{google_trend} and {twitter_trending_topic}"
print(f"🔍 Final trending topic: {trending_topic}")

# === Обновление файла schedule с темой ===
def update_schedule_topic():
    now = datetime.datetime.now()
    now_hour = now.hour
    today = now.strftime("%Y-%m-%d")

    if 'topic' not in schedule.columns:
        schedule['topic'] = ""

    schedule.loc[(schedule['date'] == today) & (schedule['time'] == now_hour), 'topic'] = trending_topic
    content = schedule.to_csv(index=False)
    try:
        file = repo.get_contents("date_time/content_schedule.csv")
        repo.update_file("date_time/content_schedule.csv", f"Update topic {today} {now_hour}", content, file.sha)
    except Exception as e:
        print("❌ Ошибка обновления файла расписания:", e)

    # === Логирование тренда в trending_log.csv ===
    log_file_path = "date_time/trending_log.csv"
    try:
        file_log = repo.get_contents(log_file_path)
        log_content = file_log.decoded_content.decode("utf-8")
        log_df = pd.read_csv(pd.compat.StringIO(log_content))
    except Exception as e:
        print("📁 Creating new log file")
        log_df = pd.DataFrame(columns=["date", "hour", "google_trend", "twitter_trend", "combined_topic"])

    already_logged = log_df[(log_df['date'] == today) & (log_df['hour'] == now_hour)]
    if already_logged.empty:
        new_log = {
            "date": today,
            "hour": now_hour,
            "google_trend": google_trend,
            "twitter_trend": twitter_trending_topic,
            "combined_topic": trending_topic
        }
        log_df = pd.concat([log_df, pd.DataFrame([new_log])], ignore_index=True)
        updated_log_csv = log_df.to_csv(index=False)
        try:
            if 'file_log' in locals():
                repo.update_file(log_file_path, "Update trending log", updated_log_csv, file_log.sha)
            else:
                repo.create_file(log_file_path, "Create trending log", updated_log_csv)
            print("📝 Trending topic logged to trending_log.csv")
        except Exception as e:
            print(f"❌ Error updating trending log: {e}")
    else:
        print("⏩ Trending already logged for this hour")

update_schedule_topic()
