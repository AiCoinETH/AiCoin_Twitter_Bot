import tweepy
import requests
import tempfile
import os

# Ключи (лучше через переменные окружения!)
API_KEY = os.getenv("API_KEY", "50VbJPNB1ONcdQY7Qlqpq3nSr")
API_SECRET = os.getenv("API_SECRET", "hFX4qeXNFhP4vYzySYj7tcFjoK2mTmJSAwHrvdqNhwpsh45JgU")
BEARER_TOKEN = os.getenv("BEARER_TOKEN", "AAAAAAAAAAAAAAAAAAAAANkk2wEAAAAA8XR%2BqHierzWkhUVc%2FZHMShO4S5U%3Du4cH7dn5LVk9lwhj0A8eIsDpyT9xCctROuCAnUTeEqlHjRXBuF")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN", "1937066883548647424-TwgIiyGxGJTlH4czLq2SFyvquBnFLD")
ACCESS_SECRET = os.getenv("ACCESS_SECRET", "49otHsBIJvWzq4e3dDG6mxUHrAD3w6zDzwKvs5tUH7KyD")

def get_api_v1():
    auth = tweepy.OAuth1UserHandler(API_KEY, API_SECRET, ACCESS_TOKEN, ACCESS_SECRET)
    return tweepy.API(auth)

def get_client_v2():
    return tweepy.Client(
        bearer_token=BEARER_TOKEN,
        consumer_key=API_KEY,
        consumer_secret=API_SECRET,
        access_token=ACCESS_TOKEN,
        access_token_secret=ACCESS_SECRET
    )

def publish_tweet_v2(text, image_url=None):
    client = get_client_v2()
    media_ids = None
    if image_url:
        # Скачиваем картинку во временный файл и грузим через API v1.1
        api_v1 = get_api_v1()
        response = requests.get(image_url)
        response.raise_for_status()
        with tempfile.NamedTemporaryFile(delete=False, suffix='.png') as tmp:
            tmp.write(response.content)
            tmp_path = tmp.name
        try:
            media = api_v1.media_upload(tmp_path)
            media_ids = [media.media_id_string]
        finally:
            os.remove(tmp_path)
    # Публикуем твит через v2
    client.create_tweet(text=text, media_ids=media_ids)
    print("Пост отправлен в Twitter!")

if __name__ == "__main__":
    tweet_text = "Тестовый твит через Tweepy (media_id через v1.1 + твит через v2)! 🚀"
    image_url = "https://upload.wikimedia.org/wikipedia/commons/4/47/PNG_transparency_demonstration_1.png"
    publish_tweet_v2(tweet_text, image_url)
