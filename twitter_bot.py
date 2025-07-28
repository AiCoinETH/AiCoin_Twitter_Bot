# === Autoposting and Commenting Module via GitHub Actions ===

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
import openai
from pytrends.request import TrendReq
import snscrape.modules.twitter as sntwitter
import random

# --- Constants and Configuration ---
openai.api_key = os.getenv("OPENAI_API_KEY")
TWITTER_API_KEY = os.getenv("API_KEY")
TWITTER_API_SECRET = os.getenv("API_SECRET")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
ACCESS_SECRET = os.getenv("ACCESS_SECRET")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
PINATA_JWT = os.getenv("PINATA_JWT")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

# --- Authentication ---
auth = tweepy.OAuth1UserHandler(TWITTER_API_KEY, TWITTER_API_SECRET, ACCESS_TOKEN, ACCESS_SECRET)
twitter_api = tweepy.API(auth)
telegram_bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
github = Github(GITHUB_TOKEN)
repo = github.get_repo("AiCoinETH/AiCoin_Twitter_Bot")

# --- Hashtags for Analysis ---
TWITTER_HASHTAGS = ["AiCoin", "AI", "OpenAI", "xAI", "AICrypto", "AIToken"]

# --- Google Trends: related query ---
def get_google_related_query():
    pytrends = TrendReq(hl='en-US', tz=360)
    pytrends.build_payload(["Ai coin"], cat=0, timeframe='now 7-d')
    related = pytrends.related_queries().get("Ai coin", {}).get("top")
    if related is not None and not related.empty:
        return related.iloc[0]['query']
    return "AI coin"

# --- Generate dynamic promo topic ---
def get_random_promo_topic():
    promo_topics = [
        "How AiCoin is transforming decentralized finance (DeFi)",
        "The role of AI tokens in Web3 adoption",
        "Why AiCoin stands out in the AI + Blockchain landscape",
        "Future of autonomous smart contracts powered by AiCoin",
        "AiCoin: The bridge between AI innovation and crypto"
    ]
    return random.choice(promo_topics)

# --- Generate short tweet ---
def generate_post_text(topic):
    prompt = f"Write a short tweet (under 280 characters) about the topic '{topic}' with a reference to the crypto project AiCoin. Include hashtags #AiCoin #AI."
    response = openai.ChatCompletion.create(
        model="gpt-4",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=180,
        temperature=0.7
    )
    return response.choices[0].message["content"].strip()[:275]

# --- Generate Telegram message (without hashtags) ---
def generate_telegram_text(topic):
    prompt = f"Write a short Telegram message (no hashtags) about the topic '{topic}' with a reference to the crypto project AiCoin."
    response = openai.ChatCompletion.create(
        model="gpt-4",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=180,
        temperature=0.7
    )
    return response.choices[0].message["content"].strip()

# --- Generate image ---
def generate_image(prompt):
    response = openai.Image.create(
        prompt=prompt,
        n=1,
        size="1024x1024"
    )
    image_url = response['data'][0]['url']
    image_data = requests.get(image_url).content
    return BytesIO(image_data), image_url

# --- Upload to Pinata ---
def upload_to_pinata(image_bytes, filename):
    url = "https://api.pinata.cloud/pinning/pinFileToIPFS"
    headers = {"Authorization": f"Bearer {PINATA_JWT}"}
    files = {'file': (filename, image_bytes)}
    response = requests.post(url, files=files, headers=headers)
    return response.json()['IpfsHash']

# --- Visual Post Block ---
def post_visual_post(topic):
    print("🎨 Generating image for visual post...")
    image_bytes, _ = generate_image(topic)
    image_path = f"visual_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    with open(image_path, "wb") as f:
        f.write(image_bytes.getbuffer())

    print("📦 Uploading image to IPFS...")
    ipfs_hash = upload_to_pinata(image_bytes, image_path)
    ipfs_url = f"https://gateway.pinata.cloud/ipfs/{ipfs_hash}"

    print("📤 Sending visual post to Telegram...")
    telegram_bot.send_photo(chat_id=TELEGRAM_CHANNEL_ID, photo=open(image_path, 'rb'), caption=f"🎨 Visual for topic: {topic}\n🔗 {ipfs_url}")
    os.remove(image_path)

# --- Search tweets by hashtag via snscrape ---
def get_twitter_trends_by_hashtag(hashtag):
    try:
        tweets = []
        for i, tweet in enumerate(sntwitter.TwitterSearchScraper(f"#{hashtag}").get_items()):
            if i >= 5:
                break
            tweets.append(tweet.content)
        return tweets
    except Exception as e:
        print(f"Error scraping tweets for #{hashtag}: {e}")
        return []

# --- Comment handling ---
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

# --- Time check ---
def should_post_now():
    now = datetime.datetime.now()
    return now.hour in [9, 14, 22]

# --- Main automation function ---
def main():
    if not should_post_now():
        print("⏰ Not time to post yet.")
        return

    print("📊 Getting related query from Google Trends...")
    related_query = get_google_related_query()

    print("🧐 Generating promo topic...")
    topic = get_random_promo_topic()

    print("✍️ Generating post text...")
    tweet_text = generate_post_text(topic)
    telegram_text = generate_telegram_text(topic)

    print("📤 Posting to Twitter...")
    try:
        tweet = twitter_api.update_status(tweet_text)
    except Exception as e:
        print(f"Error posting to Twitter: {e}")
        return

    print("📣 Posting to Telegram...")
    try:
        telegram_bot.send_message(chat_id=TELEGRAM_CHANNEL_ID, text=telegram_text)
    except Exception as e:
        print(f"Error posting to Telegram: {e}")

    print("📊 Logging trend...")
    log_data = pd.DataFrame([[datetime.datetime.now(), related_query, topic, tweet_text]],
                             columns=["timestamp", "related_query", "topic", "tweet"])
    log_file = "trending_log.csv"
    if os.path.exists(log_file):
        log_data.to_csv(log_file, mode='a', header=False, index=False)
    else:
        log_data.to_csv(log_file, index=False)

    print("💬 Handling comments...")
    handle_comments(tweet.id)

if __name__ == "__main__":
    main()
