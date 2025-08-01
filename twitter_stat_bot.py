import os
from telegram import Bot

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN_CHANNEL')
CHANNEL_ID = os.environ.get('TELEGRAM_CHANNEL_USERNAME_ID')

bot = Bot(token=TELEGRAM_TOKEN)

text = (
    "🕊️ [Twitter](https://x.com/AiCoin_ETH): 0 подписчиков\n"
    "🌐 [Сайт](https://getaicoin.com/)"
)
msg = bot.send_message(
    chat_id=CHANNEL_ID,
    text=text,
    parse_mode='Markdown'
)

print("Message ID:", msg.message_id)