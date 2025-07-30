import tweepy
import requests
import os
import tempfile

# Берём ключи из переменных окружения для секьюрности (лучше для GitHub Actions!)
API_KEY = os.getenv("API_KEY", "50VbJPNB1ONcdQY7Qlqpq3nSr")
API_SECRET = os.getenv("API_SECRET", "hFX4qeXNFhP4vYzySYj7tcFjoK2mTmJSAwHrvdqNhwpsh45JgU")
BEARER_TOKEN = os.getenv("BEARER_TOKEN", "AAAAAAAAAAAAAAAAAAAAANkk2wEAAAAA8XR%2BqHierzWkhUVc%2FZHMShO4S5U%3Du4cH7dn5LVk9lwhj0A8eIsDpyT9xCctROuCAnUTeEqlHjRXBuF")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN", "1937066883548647424-TwgIiyGxGJTlH4czLq2SFyvquBnFLD")
ACCESS_SECRET = os.getenv("ACCESS_SECRET", "49otHsBIJvWzq4e3dDG6mxUHrAD3w6zDzwKvs5tUH7KyD")

def get_twitter_v2_client():
    return tweepy.Client(
        bearer_token=BEARER_TOKEN,
        consumer_key=API_KEY,
        consumer_secret=API_SECRET,
        access_token=ACCESS_TOKEN,
        access_token_secret=ACCESS_SECRET
    )

def publish_tweet_v2(text, image_url=None):
    client = get_twitter_v2_client()
    media_id = None
    if image_url:
        # Скачиваем картинку во временный файл
        response = requests.get(image_url)
        response.raise_for_status()
        with tempfile.NamedTemporaryFile(delete=False, suffix='.png') as tmp:
            tmp.write(response.content)
            tmp_path = tmp.name
        try:
            # API v2 не поддерживает upload напрямую, но Tweepy клиент реализует это через v1.1
            media = client.media_upload(tmp_path)
            media_id = media.media_id
        finally:
            os.remove(tmp_path)
    # Публикуем твит с media_id
    client.create_tweet(text=text, media_ids=[media_id] if media_id else None)
    print("Пост отправлен в Twitter!")

if __name__ == "__main__":
    tweet_text = "Тестовый твит через Tweepy API v2 (Free)! 🚀"
    image_url = "https://upload.wikimedia.org/wikipedia/commons/4/47/PNG_transparency_demonstration_1.png"
    publish_tweet_v2(tweet_text, image_url)
