import tweepy
import requests
import tempfile
import os

# --- ВСТАВЬТЕ СЮДА ВАШИ КЛЮЧИ ---
API_KEY = "50VbJPNB1ONcdQY7Qlqpq3nSr"
API_SECRET = "hFX4qeXNFhP4vYzySYj7tcFjoK2mTmJSAwHrvdqNhwpsh45JgU"
ACCESS_TOKEN = "1937066883548647424-TwgIiyGxGJTlH4czLq2SFyvquBnFLD"
ACCESS_SECRET = "49otHsBIJvWzq4e3dDG6mxUHrAD3w6zDzwKvs5tUH7KyD"

def get_twitter_client():
    auth = tweepy.OAuth1UserHandler(
        API_KEY, API_SECRET,
        ACCESS_TOKEN, ACCESS_SECRET
    )
    return tweepy.API(auth)

def publish_tweet(text, image_url=None):
    api = get_twitter_client()
    if image_url:
        # Скачиваем картинку во временный файл
        response = requests.get(image_url)
        response.raise_for_status()
        with tempfile.NamedTemporaryFile(delete=False, suffix='.png') as tmp:
            tmp.write(response.content)
            tmp_path = tmp.name
        try:
            media = api.media_upload(tmp_path)
            api.update_status(status=text, media_ids=[media.media_id])
        finally:
            os.remove(tmp_path)
    else:
        api.update_status(status=text)
