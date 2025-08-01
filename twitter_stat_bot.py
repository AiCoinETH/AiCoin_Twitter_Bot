import re

def get_twitter_followers(username):
    import requests
    url = f'https://x.com/{username}'
    headers = {'User-Agent': 'Mozilla/5.0'}
    r = requests.get(url, headers=headers, timeout=10)
    # Проверяем оба варианта: Followers и читателей
    match = re.search(r'(\d[\d,\.]*)\s+(Followers|читателей)', r.text)
    if match:
        return match.group(1).replace(',', '').replace('.', '')
    else:
        return None