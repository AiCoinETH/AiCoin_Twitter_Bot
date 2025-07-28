import os
import time
import openai
import random
import requests
from io import BytesIO
from datetime import datetime
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, MessageHandler, Filters
import tweepy

# === Константы и переменные окружения ===
TELEGRAM_BOT_TOKEN_APPROVAL = os.getenv("TELEGRAM_BOT_TOKEN_APPROVAL")
TELEGRAM_APPROVAL_CHAT_ID = os.getenv("TELEGRAM_APPROVAL_CHAT_ID")
TELEGRAM_APPROVAL_USER_ID = int(os.getenv("TELEGRAM_APPROVAL_USER_ID", "0"))
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PINATA_JWT = os.getenv("PINATA_JWT")
TWITTER_API_KEY = os.getenv("API_KEY")
TWITTER_API_SECRET = os.getenv("API_SECRET")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
ACCESS_SECRET = os.getenv("ACCESS_SECRET")

openai.api_key = OPENAI_API_KEY
approval_bot = Bot(token=TELEGRAM_BOT_TOKEN_APPROVAL)

auth = tweepy.OAuth1UserHandler(TWITTER_API_KEY, TWITTER_API_SECRET, ACCESS_TOKEN, ACCESS_SECRET)
twitter_api = tweepy.API(auth)

state = {"mode": None, "generated": {}, "custom_prompt": None}

def generate_ai_post():
    topic = "AI coin and decentralized intelligence in Web3"
    img_prompt = f"futuristic ai crypto coin, glowing neural circuits, cyberpunk style, concept of {topic}"
    text_prompt = f"Напиши новость на русском языке о популярности AI токенов, включая $Ai Coin, в 2025 году. Сделай её информативной, но краткой, как новость или обзор."

    image = openai.Image.create(prompt=img_prompt, n=1, size="1024x1024")["data"][0]["url"]
    text = openai.ChatCompletion.create(
        model="gpt-4",
        messages=[{"role": "user", "content": text_prompt}],
        max_tokens=300
    )["choices"][0]["message"]["content"].strip()
    return topic, text, image

def build_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ OK", callback_data="approve"), InlineKeyboardButton("❌ Отказ", callback_data="reject")],
        [InlineKeyboardButton("🕐 Подумать", callback_data="wait"), InlineKeyboardButton("🔁 Заново", callback_data="regen")],
        [InlineKeyboardButton("📝 Задать тему", callback_data="custom"), InlineKeyboardButton("🤖 Поговорить", callback_data="chat")],
        [InlineKeyboardButton("🖼 Новая картинка", callback_data="regen_image")]
    ])

def send_post_for_approval(context):
    topic, text, image_url = generate_ai_post()
    state["generated"] = {"topic": topic, "text": text, "image": image_url}
    approval_bot.send_photo(
        chat_id=TELEGRAM_APPROVAL_CHAT_ID,
        photo=image_url,
        caption=f"""🧠 *Новая новость (русский вариант)*

{text}""",
        parse_mode="Markdown",
        reply_markup=build_keyboard()
    )
    context.job_queue.run_once(timeout_autopost, 180)

def timeout_autopost(context):
    post_final(context, auto=True)

def post_final(context, auto=False):
    data = state["generated"]
    if not data:
        return
    full_text = data["text"]
    image_url = data["image"]
    translated = openai.ChatCompletion.create(
        model="gpt-4",
        messages=[{"role": "user", "content": f"Переведи новость на английский язык:

{full_text}"}],
        max_tokens=300
    )["choices"][0]["message"]["content"].strip()

    short_text = translated[:240] + "\n\nMore: t.me/AiCoin_ETH\n#AiCoin #AI"
    img_data = requests.get(image_url).content
    img_bytes = BytesIO(img_data)

    try:
        twitter_api.update_status_with_media(filename="post.png", file=img_bytes, status=short_text)
    except Exception as e:
        approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"❌ Ошибка при публикации в Twitter: {e}")
    try:
        approval_bot.send_photo(chat_id=TELEGRAM_CHANNEL_ID, photo=img_bytes, caption=translated)
    except Exception as e:
        approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"❌ Ошибка Telegram-публикации: {e}")
    approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="✅ Пост опубликован.")

def handle_callback(update: Update, context):
    query = update.callback_query
    user_id = query.from_user.id
    if user_id != TELEGRAM_APPROVAL_USER_ID:
        query.answer("⛔ Только администратор может подтверждать посты.", show_alert=True)
        return

    data = query.data
    if data == "approve":
        post_final(context)
    elif data == "reject":
        query.message.reply_text("❌ Пост отклонён. Обсуждаем дальше...")
    elif data == "wait":
        query.message.reply_text("🕐 Ожидаю дальше. У тебя есть ещё 3 минуты.")
    elif data == "regen":
        send_post_for_approval(context)
    elif data == "custom":
        state["mode"] = "custom"
        query.message.reply_text("📝 Введи тему, по которой сгенерировать новость:")
    elif data == "chat":
        state["mode"] = "chat"
        query.message.reply_text("🤖 Готов обсудить. Напиши что-нибудь.")
    elif data == "regen_image":
        topic = state["generated"].get("topic", "AI and crypto")
        image = openai.Image.create(prompt=topic, n=1, size="1024x1024")["data"][0]["url"]
        state["generated"]["image"] = image
        approval_bot.send_photo(chat_id=TELEGRAM_APPROVAL_CHAT_ID, photo=image, caption="🖼 Новое изображение для текущего текста")

def handle_message(update: Update, context):
    if update.effective_user.id != TELEGRAM_APPROVAL_USER_ID:
        return
    if state["mode"] == "custom":
        topic = update.message.text
        text_prompt = f"Напиши новость на русском языке на тему: {topic}"
        img_prompt = topic
        image = openai.Image.create(prompt=img_prompt, n=1, size="1024x1024")["data"][0]["url"]
        text = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[{"role": "user", "content": text_prompt}],
            max_tokens=300
        )["choices"][0]["message"]["content"].strip()
        state["generated"] = {"topic": topic, "text": text, "image": image}
        approval_bot.send_photo(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            photo=image,
            caption=f"""📝 Сгенерировано по твоей теме:

{text}""",
            parse_mode="Markdown",
            reply_markup=build_keyboard()
        )
        state["mode"] = None
    elif state["mode"] == "chat":
        prompt = update.message.text
        reply = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300
        )["choices"][0]["message"]["content"].strip()
        update.message.reply_text(reply)

def main():
    updater = Updater(token=TELEGRAM_BOT_TOKEN_APPROVAL, use_context=True)
    dp = updater.dispatcher
    dp.add_handler(CommandHandler("start", send_post_for_approval))
    dp.add_handler(CallbackQueryHandler(handle_callback))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))
    updater.start_polling()
    updater.idle()
