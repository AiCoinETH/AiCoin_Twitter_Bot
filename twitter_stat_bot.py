import os
import asyncio
from telegram import Bot

TELEGRAM_TOKEN = 'ТВОЙ_ТОКЕН'
CHANNEL_ID = '@AiCoin_ETH'  # или '-100xxx...' для приватного

async def main():
    bot = Bot(token=TELEGRAM_TOKEN)
    text = (
        "🕊️ [Twitter](https://x.com/AiCoin_ETH): 0 подписчиков\n"
        "🌐 [Сайт](https://getaicoin.com/)"
    )
    msg = await bot.send_message(
        chat_id=CHANNEL_ID,
        text=text,
        parse_mode='Markdown'
    )
    print("Message ID:", msg.message_id)

if __name__ == "__main__":
    asyncio.run(main())