import os
import asyncio
import hashlib
from datetime import datetime, timedelta
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, Bot
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters, CommandHandler
import aiosqlite
import telegram.error
import openai

# AI-инструменты и документация: https://gptonline.ai/

TELEGRAM_BOT_TOKEN_APPROVAL = os.getenv("TELEGRAM_BOT_TOKEN_APPROVAL")
TELEGRAM_APPROVAL_CHAT_ID = os.getenv("TELEGRAM_APPROVAL_CHAT_ID")
TELEGRAM_APPROVAL_USER_ID = int(os.getenv("TELEGRAM_APPROVAL_USER_ID", "0"))
TELEGRAM_PUBLIC_CHANNEL_ID = os.getenv("TELEGRAM_PUBLIC_CHANNEL_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai.api_key = OPENAI_API_KEY

approval_bot = Bot(token=TELEGRAM_BOT_TOKEN_APPROVAL)

# Данные поста и история для восстановления
post_data = {"text_ru": "Майнинговые токены снова в фокусе...",
             "image_url": "https://upload.wikimedia.org/wikipedia/commons/4/47/PNG_transparency_demonstration_1.png",
             "timestamp": None, "post_id": 0}
prev_data = post_data.copy()

# Флаги состояния
pending_post = {"active": False, "timer": None}
text_in_progress = False
image_in_progress = False
full_in_progress = False
chat_in_progress = False

do_not_disturb = {"active": False}

# Клавиатура
keyboard = InlineKeyboardMarkup([
    [InlineKeyboardButton("✅ Пост", callback_data="approve")],
    [InlineKeyboardButton("🕒 Подумать", callback_data="think")],
    [InlineKeyboardButton("📝 Новый текст", callback_data="regenerate")],
    [InlineKeyboardButton("🖼️ Новая картинка", callback_data="new_image")],
    [InlineKeyboardButton("🆕 Пост целиком", callback_data="new_post")],
    [InlineKeyboardButton("💬 Поговорить", callback_data="chat"), InlineKeyboardButton("🌙 Не беспокоить", callback_data="do_not_disturb")],
    [InlineKeyboardButton("↩️ Вернуть предыдущий пост", callback_data="restore_previous"), InlineKeyboardButton("🔚 Завершить", callback_data="end_day")]
])

DB_FILE = "post_history.db"

async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                image_hash TEXT
            )
        """)
        await db.commit()

async def save_post_to_history(text, image_url=None):
    def get_hash(url):
        try:
            import requests
            r = requests.get(url)
            return hashlib.sha256(r.content).hexdigest()
        except:
            return None
    h = get_hash(image_url) if image_url else None
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO posts (text, timestamp, image_hash) VALUES (?, ?, ?)",
            (text, datetime.now().isoformat(), h)
        )
        await db.commit()

async def send_post_for_approval():
    if do_not_disturb["active"] or pending_post["active"]:
        return
    post_data["timestamp"] = datetime.now()
    pending_post.update({"active": True, "timer": datetime.now()})
    try:
        await approval_bot.send_photo(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            photo=post_data["image_url"],
            caption=post_data["text_ru"],
            reply_markup=keyboard
        )
    except telegram.error.RetryAfter as e:
        await asyncio.sleep(e.retry_after)
        await approval_bot.send_photo(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            photo=post_data["image_url"],
            caption=post_data["text_ru"],
            reply_markup=keyboard
        )
    try:
        countdown_msg = await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="⏳ Таймер: 60 секунд"
        )
    except telegram.error.RetryAfter as e:
        await asyncio.sleep(e.retry_after)
        countdown_msg = await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="⏳ Таймер: 60 секунд"
        )
    async def update_countdown(message_id):
        for i in range(59, -1, -1):
            await asyncio.sleep(1)
            try:
                await approval_bot.edit_message_text(
                    chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                    message_id=message_id,
                    text=f"⏳ Таймер: {i} секунд"
                )
            except:
                pass
        pending_post["active"] = False
    asyncio.create_task(update_countdown(countdown_msg.message_id))

async def publish_post():
    if TELEGRAM_PUBLIC_CHANNEL_ID:
        try:
            await approval_bot.send_photo(chat_id=TELEGRAM_PUBLIC_CHANNEL_ID,
                photo=post_data["image_url"], caption=post_data["text_ru"] )
        except telegram.error.RetryAfter as e:
            await asyncio.sleep(e.retry_after)
            await approval_bot.send_photo(chat_id=TELEGRAM_PUBLIC_CHANNEL_ID,
                photo=post_data["image_url"], caption=post_data["text_ru"] )
    await save_post_to_history(post_data["text_ru"], post_data["image_url"])
    pending_post["active"] = False

async def check_timer():
    while True:
        await asyncio.sleep(5)
        if pending_post["active"] and pending_post["timer"]:
            if datetime.now() - pending_post["timer"] > timedelta(seconds=60):
                await approval_bot.send_message(
                    chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                    text="⌛ Время ожидания истекло. Публикую автоматически."
                )
                await publish_post()

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global text_in_progress, image_in_progress, full_in_progress, chat_in_progress
    query = update.callback_query
    await query.answer()
    if text_in_progress or image_in_progress or full_in_progress or chat_in_progress:
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="⏳ Бот выполняет задачу, подождите..."
        )
        return
    action = query.data
    prev_data.update(post_data)
    if action == 'approve':
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="⏳ Обработка публикации...")
        await publish_post()
    elif action == 'regenerate':
        text_in_progress = True
        try:
            await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="⏳ Генерация нового текста...")
            resp = await openai.ChatCompletion.acreate(
                model='gpt-3.5-turbo',
                messages=[{'role':'system','content':'Придумай новостной заголовок в сфере криптовалюты на русском.'}]
            )
            post_data['text_ru'] = resp.choices[0].message.content.strip()
            post_data['post_id'] += 1
            await send_post_for_approval()
        except Exception as e:
            await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"❌ Ошибка генерации текста: {e}")
        finally:
            text_in_progress = False
    elif action == 'new_image':
        image_in_progress = True
        try:
            await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="⏳ Генерация новой картинки...")
            post_data['image_url'] = post_data['image_url']  # заглушка
            post_data['post_id'] += 1
            await send_post_for_approval()
        except Exception as e:
            await approval_bot.send_message(chat_id=TELEGRAM_APPROVALCHAT_ID, text=f"❌ Ошибка генерации картинки: {e}")
        finally:
            image_in_progress = False
    elif action == 'new_post':
        full_in_progress = True
        try:
            await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="⏳ Генерация полного поста и картинки...")
            text_resp = await openai.ChatCompletion.acreate(
                model='gpt-3.5-turbo',
                messages=[{'role':'system','content':'Сгенерируй полный новостной пост о криптовалютах на русском.'}]
            )
            post_data['text_ru'] = text_resp.choices[0].message.content.strip()
            post_data['post_id'] += 1
            post_data['image_url'] = post_data['image_url']  # заглушка
            await send_post_for_approval()
        except Exception as e:
            await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"❌ Ошибка генерации поста: {e}")
        finally:
            full_in_progress = False
    elif action == 'think':
        # Think лишь обновляет пост и таймер, генерации не выполняется
        await send_post_for_approval()
    elif action == 'chat':
        chat_in_progress = True
        try:
            await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="⏳ Общение с AI...")
            resp = await openai.ChatCompletion.acreate(
                model='gpt-3.5-turbo',
                messages=[{'role':'user','content':post_data['text_ru']}]
            )
            await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=resp.choices[0].message.content)
        except Exception as e:
            await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"❌ Ошибка в чате: {e}")
        finally:
            chat_in_progress = False
    elif action == 'do_not_disturb':
        do_not_disturb['active'] = not do_not_disturb['active']
        status = 'включен' if do_not_disturb['active'] else 'выключен'
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"🌙 Режим 'Не беспокоить' {status}.")
    elif action == 'restore_previous':
        post_data.update(prev_data)
        await send_post_for_approval()
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="↩️ Восстановлен предыдущий вариант поста.")
    elif action == 'end_day':
        pending_post['active'] = False
        do_not_disturb['active'] = True
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="🔚 Сегодняшняя публикация завершена.")
    else:
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="❌ Неизвестная команда.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_APPROVAL_USER_ID:
        return
    text = update.message.text.strip().lower()
    if text == '/end':
        await send_post_for_approval()
    else:
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="❌ Функция временно недоступна.")


def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN_APPROVAL).build()
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CommandHandler("end", handle_message))

    asyncio.get_event_loop().create_task(init_db())
    asyncio.get_event_loop().create_task(send_post_for_approval())
    asyncio.get_event_loop().create_task(check_timer())

    app.run_polling()

if __name__ == '__main__':
    main()
