import os
import tweepy

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
ACCESS_SECRET = os.getenv("ACCESS_SECRET")

auth = tweepy.OAuth1UserHandler(API_KEY, API_SECRET, ACCESS_TOKEN, ACCESS_SECRET)
api = tweepy.API(auth)

tweet = "🚀 The AI revolution is here. Follow @AiCoin_ETH for the future of crypto. #AiCoin #Web3"
api.update_status(tweet)

print("✅ Твит отправлен!")
