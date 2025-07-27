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

# --- Post to Twitter and Telegram ---
def post_to_socials(text, image_path=None, telegram_text=None):
    if image_path:
        media = twitter_api.media_upload(image_path)
        tweet = twitter_api.update_status(status=text, media_ids=[media.media_id])
        with open(image_path, 'rb') as img:
            telegram_bot.send_photo(chat_id=TELEGRAM_CHANNEL_ID, photo=img, caption=telegram_text or text)
        return tweet.id_str
    else:
        tweet = twitter_api.update_status(status=text)
        telegram_bot.send_message(chat_id=TELEGRAM_CHANNEL_ID, text=telegram_text or text)
        return tweet.id_str

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

# --- Check post time ---
def should_post_now():
    now = datetime.datetime.now()
    return now.hour in [9, 14, 22]

# --- Main logic ---
def main():
    now = datetime.datetime.now()
    print(f"🔍 MAIN STARTED at {now}")
    date = now.strftime("%Y-%m-%d")
    hour = now.hour
    print(f"🕒 Current hour: {hour}")

    try:
        file_log = repo.get_contents("date_time/main/trending_log.csv")
        log_df = pd.read_csv(BytesIO(file_log.decoded_content))
    except Exception as e:
        print(f"❌ Failed to load trending_log.csv: {e}")
        return

    today_posts = log_df[log_df['date'] == date]

    try:
        if hour == 9:
            print("🧭 Block: Google Trends")
            topic = get_google_related_query()
            print(f"📈 Google trend topic: {topic}")

            if topic in log_df['google_trend'].values:
                print("🟡 Already posted: Google Trends")
                return

            text = generate_post_text(topic)
            tg_text = generate_telegram_text(topic)
            tweet_id = post_to_socials(text, telegram_text=tg_text)

        elif hour == 14:
            print("🧭 Block: Twitter Trends")
            all_trends = []
            for tag in TWITTER_HASHTAGS:
                print(f"🔍 Scraping: #{tag}")
                all_trends.extend(get_twitter_trends_by_hashtag(tag))

            twitter_trend = all_trends[0] if all_trends else "#AiCoin"
            print(f"🐦 Twitter trend topic: {twitter_trend}")

            if twitter_trend in log_df['twitter_trend'].values:
                print("🟡 Already posted: Twitter")
                return

            text = generate_post_text(twitter_trend)
            tg_text = generate_telegram_text(twitter_trend)
            tweet_id = post_to_socials(text, telegram_text=tg_text)

        elif hour == 22:
            print("🧭 Block: Promo Post with image")
            promo_topic = "AiCoin and the future of AI in Web3"
            text = generate_post_text(promo_topic)
            tg_text = generate_telegram_text(promo_topic)

            print("🖼 Generating image...")
            image_bytes, _ = generate_image(promo_topic)
            image_path = f"promo_{date}_{hour}.png"
            with open(image_path, "wb") as f:
                f.write(image_bytes.getbuffer())

            print("📦 Uploading to Pinata...")
            ipfs_hash = upload_to_pinata(image_bytes, image_path)
            ipfs_url = f"https://gateway.pinata.cloud/ipfs/{ipfs_hash}"

            print("🚀 Posting to socials...")
            tweet_id = post_to_socials(text, image_path=image_path, telegram_text=tg_text)
            os.remove(image_path)
        else:
            print("⏱ Not posting time")
            return

    except Exception as e:
        print(f"❌ Error during posting block: {e}")
        return

    try:
        print("💾 Updating trending_log.csv...")
        new_entry = pd.DataFrame([{
            "date": date,
            "hour": hour,
            "google_trend": topic if hour == 9 else "",
            "twitter_trend": twitter_trend if hour == 14 else "",
            "combined_topic": promo_topic if hour == 22 else "",
            "news_sources": "https://trends.google.com" if hour == 9 else "https://twitter.com" if hour == 14 else "GPT promo",
            "hashtags_used": "#AiCoin #AI",
            "reason": "Automated posting by trend",
            "image_ipfs_url": ipfs_url if hour == 22 else "",
            "tweet_id": tweet_id
        }])

        log_df = pd.concat([log_df, new_entry], ignore_index=True)
        updated_log = log_df.to_csv(index=False)
        repo.update_file("date_time/main/trending_log.csv", f"Update log {date} {hour}", updated_log, file_log.sha)
    except Exception as e:
        print(f"❌ Failed to update CSV log: {e}")
        return

    print("📢 Post published successfully")

    try:
        time.sleep(180)
        print("💬 Checking replies...")
        handle_comments(tweet_id)
    except Exception as e:
        print(f"❌ Error in handle_comments: {e}")

if __name__ == "__main__":
    if should_post_now():
        main()
    else:
        print("⏱ Not posting time")
