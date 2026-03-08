import os
import time
import sqlite3
import base64
import asyncio
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BotCommand
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from openai import OpenAI

# ================= ENV =================

TG_TOKEN = os.getenv("TG_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)

FREE_LIMIT = 5
WEEK_SECONDS = 7 * 24 * 60 * 60

USER_AGREEMENT_URL = "https://disk.yandex.ru/i/IB_pG2pcgtEIGQ"
OFFER_URL = "https://disk.yandex.ru/i/8IXTO8-VSMmbuw"

# ================= PRO =================

MAX_WORKERS = 3
generation_queue = asyncio.Queue()
active_generations = {}

# ================= АНТИ-СПАМ =================

RATE_LIMIT_SECONDS = 2
user_last_message = {}

def check_rate_limit(user_id):
    now = time.time()
    last = user_last_message.get(user_id, 0)

    if now - last < RATE_LIMIT_SECONDS:
        return False

    user_last_message[user_id] = now
    return True

# ================= DATABASE =================

conn = sqlite3.connect("bot.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    week_start INTEGER,
    image_count INTEGER DEFAULT 0,
    accepted_terms INTEGER DEFAULT 0,
    referrals INTEGER DEFAULT 0,
    bonus_images INTEGER DEFAULT 0,
    ref_by INTEGER,
    is_active INTEGER DEFAULT 0
)
""")
conn.commit()

# ================= HELPERS =================

def get_user(user_id):
    cursor.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    return cursor.fetchone()

def reset_week_if_needed(user):
    now = int(time.time())
    if now - user[1] > WEEK_SECONDS:
        cursor.execute(
            "UPDATE users SET week_start=?, image_count=0 WHERE user_id=?",
            (now, user[0])
        )
        conn.commit()

def activate_user_if_needed(user):
    if user[7] == 0:
        cursor.execute("UPDATE users SET is_active=1 WHERE user_id=?", (user[0],))
        conn.commit()

        if user[6]:
            cursor.execute(
                "UPDATE users SET bonus_images=bonus_images+1, referrals=referrals+1 WHERE user_id=?",
                (user[6],)
            )
            conn.commit()

# ================= GENERATION WORKER =================

async def generation_worker():

    while True:

        job = await generation_queue.get()

        update = job["update"]
        prompt = job["prompt"]
        size = job["size"]
        user_id = job["user_id"]
        status_message = job["status"]

        try:

            img = client.images.generate(
                model="gpt-image-1",
                prompt=prompt,
                size=size
            )

            image_bytes = base64.b64decode(img.data[0].b64_json)

            await status_message.delete()
            await update.message.reply_photo(photo=image_bytes)

        except Exception as e:

            await update.message.reply_text(
                "⚠ Ошибка генерации. Попробуйте еще раз."
            )

        finally:

            active_generations.pop(user_id, None)
            generation_queue.task_done()

# ================= START =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user
    ref_id = None

    if context.args:
        try:
            ref_id = int(context.args[0])
        except:
            pass

    db_user = get_user(user.id)

    if not db_user:
        cursor.execute(
            "INSERT INTO users (user_id, week_start, accepted_terms, ref_by) VALUES (?, ?, 0, ?)",
            (user.id, int(time.time()), ref_id)
        )
        conn.commit()

    db_user = get_user(user.id)

    if db_user[3] == 0:

        terms_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📄 Пользовательское соглашение", url=USER_AGREEMENT_URL)],
            [InlineKeyboardButton("💰 Публичная оферта", url=OFFER_URL)],
            [InlineKeyboardButton("✅ Продолжить", callback_data="accept_terms")]
        ])

        await update.message.reply_text(
            "📜 Перед началом использования бота необходимо принять условия.",
            reply_markup=terms_keyboard
        )
        return

    await update.message.reply_text(
        "🚀 Sosai bot дает вам БЕСПЛАТНЫЕ генерации и доступ к Nano Banana."
    )

# ================= CALLBACK =================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    if query.data == "accept_terms":

        cursor.execute("UPDATE users SET accepted_terms=1 WHERE user_id=?", (query.from_user.id,))
        conn.commit()

        await query.edit_message_text("✅ Условия приняты.")

    elif query.data.startswith("model_"):

        model_map = {
            "model_flash": "flash",
            "model_banana1": "banana1",
            "model_banana2": "banana2"
        }

        context.user_data["model"] = model_map.get(query.data, "banana2")

        size_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📱 9:16", callback_data="size_9_16")],
            [InlineKeyboardButton("💻 16:9", callback_data="size_16_9")],
            [InlineKeyboardButton("⬜ 1:1", callback_data="size_1_1")]
        ])

        await query.edit_message_text(
            "📐 Выберите формат изображения:",
            reply_markup=size_keyboard
        )

    elif query.data.startswith("size_"):

        size_map = {
            "size_9_16": "1024x1792",
            "size_16_9": "1792x1024",
            "size_1_1": "1024x1024"
        }

        context.user_data["size"] = size_map[query.data]
        context.user_data["image_mode"] = True

        await query.edit_message_text("✏ Отправьте описание изображения.")

# ================= COMMANDS =================

async def photo(update: Update, context: ContextTypes.DEFAULT_TYPE):

    model_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ Flash 3.1", callback_data="model_flash")],
        [InlineKeyboardButton("🍌 Nano Banana 1", callback_data="model_banana1")],
        [InlineKeyboardButton("🍌 Nano Banana 2", callback_data="model_banana2")]
    ])

    await update.message.reply_text(
        "Выберите модель генерации:",
        reply_markup=model_keyboard
    )

# ================= TEXT =================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_user.id

    if not check_rate_limit(user_id):
        await update.message.reply_text("⏳ Подождите 2 секунды.")
        return

    text = update.message.text
    user = get_user(user_id)

    if context.user_data.get("image_mode"):

        remaining = FREE_LIMIT + user[5] - user[2]

        if remaining <= 0:
            await update.message.reply_text("❌ Лимит исчерпан.")
            return

        if user_id in active_generations:
            await update.message.reply_text("⚠ Генерация уже выполняется.")
            return

        activate_user_if_needed(user)

        size = context.user_data.get("size", "1024x1024")

        status_message = await update.message.reply_text(
            "🎨 Генерация изображения..."
        )

        active_generations[user_id] = True

        await generation_queue.put({
            "update": update,
            "prompt": text,
            "size": size,
            "user_id": user_id,
            "status": status_message
        })

        cursor.execute(
            "UPDATE users SET image_count=image_count+1 WHERE user_id=?",
            (user_id,)
        )
        conn.commit()

        context.user_data["image_mode"] = False

# ================= REGISTER =================

app = ApplicationBuilder().token(TG_TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("photo", photo))
app.add_handler(CallbackQueryHandler(button_handler))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

async def set_commands(app):

    await app.bot.set_my_commands([
        BotCommand("start", "Запуск"),
        BotCommand("photo", "Создать изображение"),
    ])

async def post_init(app):

    await set_commands(app)

    for _ in range(MAX_WORKERS):
        asyncio.create_task(generation_worker())

app.post_init = post_init

if __name__ == "__main__":
    print("🚀 Бот запущен")
    app.run_polling(drop_pending_updates=True)
