import asyncio
from telegram import Bot

TOKEN = "ВАШ_ТОКЕН_ОТ_BOTFATHER"  # например, "8097657551:AAFEpfksrlBc2-2PZ-ieAJg0_T3mheUv7jk"
CHANNEL_ID = "-1002868465126"      # id вашего канала (именно с минусом!) или публичный username (@AiCoin_ETH)

async def main():
    bot = Bot(token=TOKEN)
    try:
        # Тест: отправляем текст
        msg = await bot.send_message(chat_id=CHANNEL_ID, text="✅ Тестовое сообщение от Telegram-бота!")
        print("Текст успешно отправлен! Message id:", msg.message_id)

        # Тест: отправляем картинку с подписью
        photo_msg = await bot.send_photo(
            chat_id=CHANNEL_ID,
            photo="https://upload.wikimedia.org/wikipedia/commons/4/47/PNG_transparency_demonstration_1.png",
            caption="🖼️ Тестовое изображение"
        )
        print("Фото успешно отправлено! Message id:", photo_msg.message_id)

    except Exception as e:
        print("Ошибка при публикации:", e)

if __name__ == "__main__":
    asyncio.run(main())
