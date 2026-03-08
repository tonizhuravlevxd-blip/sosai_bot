import os
import time
import sqlite3
import base64
import asyncio
import logging

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


logging.basicConfig(level=logging.INFO)

TG_TOKEN = os.getenv("TG_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY не установлен")

client = OpenAI(api_key=OPENAI_API_KEY)

FREE_LIMIT = 5
WEEK_SECONDS = 7 * 24 * 60 * 60
MAX_INPUT_IMAGES = 4

USER_AGREEMENT_URL = "https://disk.yandex.ru/i/IB_pG2pcgtEIGQ"
OFFER_URL = "https://disk.yandex.ru/i/8IXTO8-VSMmbuw"


# ================= PERFORMANCE =================

MAX_WORKERS = 4
generation_queue = asyncio.Queue()
active_generations = {}
db_lock = asyncio.Lock()


# ================= RATE LIMIT =================

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

conn = sqlite3.connect(
    "bot.db",
    check_same_thread=False,
    timeout=30
)

conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA synchronous=NORMAL")

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


async def activate_user_if_needed(user):

    if user[7] == 0:

        async with db_lock:

            cursor.execute(
                "UPDATE users SET is_active=1 WHERE user_id=?",
                (user[0],)
            )

            conn.commit()

            if user[6]:

                cursor.execute(
                    "UPDATE users SET bonus_images=bonus_images+1, referrals=referrals+1 WHERE user_id=?",
                    (user[6],)
                )

                conn.commit()


# ================= WORKER =================

async def generation_worker():

    while True:

        job = await generation_queue.get()

        update = job["update"]
        prompt = job["prompt"]
        size = job["size"]
        model = job["model"]
        images = job["images"]
        user_id = job["user_id"]
        status = job["status"]

        try:

            style = ""

            if model == "banana1":
                style = "cinematic lighting ultra realistic 8k"

            elif model == "banana2":
                style = "hyper detailed masterpiece artstation quality"

            elif model == "flash":
                style = "fast simple render"

            prompt = f"{style} {prompt}"

            images = images[:MAX_INPUT_IMAGES]

            if images:

                upload_images = []
                for i, img in enumerate(images):
                    upload_images.append((f"image{i}.png", img))

                result = client.images.edit(
                    model="gpt-image-1",
                    image=upload_images,
                    prompt=prompt,
                    size=size
                )

            else:

                result = client.images.generate(
                    model="gpt-image-1",
                    prompt=prompt,
                    size=size,
                    n=1
                )

            image_base64 = result.data[0].b64_json
            image_bytes = base64.b64decode(image_base64)

            try:
                await status.delete()
            except:
                pass

            await update.message.reply_photo(photo=image_bytes)

        except Exception as e:

            logging.error(f"Generation error: {e}")

            try:
                await update.message.reply_text(
                    "⚠ Ошибка генерации."
                )
            except:
                pass

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

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📄 Пользовательское соглашение", url=USER_AGREEMENT_URL)],
            [InlineKeyboardButton("💰 Публичная оферта", url=OFFER_URL)],
            [InlineKeyboardButton("✅ Продолжить", callback_data="accept_terms")]
        ])

        await update.message.reply_text(
            "📜 Перед началом использования бота необходимо принять условия.",
            reply_markup=keyboard
        )

        return

    await update.message.reply_text("🚀 Sosai bot готов к генерации.")


# ================= CALLBACK =================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    if query.data == "accept_terms":

        cursor.execute(
            "UPDATE users SET accepted_terms=1 WHERE user_id=?",
            (query.from_user.id,)
        )

        conn.commit()

        await query.edit_message_text("✅ Условия приняты.")

    elif query.data.startswith("model_"):

        model_map = {
            "model_flash": "flash",
            "model_banana1": "banana1",
            "model_banana2": "banana2"
        }

        context.user_data["model"] = model_map.get(query.data)

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📱 9:16", callback_data="size_9_16")],
            [InlineKeyboardButton("💻 16:9", callback_data="size_16_9")],
            [InlineKeyboardButton("⬜ 1:1", callback_data="size_1_1")]
        ])

        await query.edit_message_text(
            "📐 Выберите формат изображения:",
            reply_markup=keyboard
        )

    elif query.data.startswith("size_"):

        size_map = {
            "size_9_16": "1024x1792",
            "size_16_9": "1792x1024",
            "size_1_1": "1024x1024"
        }

        context.user_data["size"] = size_map[query.data]
        context.user_data["image_mode"] = True

        await query.edit_message_text(
            "✏ Отправьте описание или изображения."
        )


# ================= PHOTO INPUT =================

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if "input_images" not in context.user_data:
        context.user_data["input_images"] = []

    if len(context.user_data["input_images"]) >= MAX_INPUT_IMAGES:

        await update.message.reply_text(
            "❌ Можно загрузить максимум 4 изображения."
        )
        return

    photo = update.message.photo[-1]
    file = await photo.get_file()

    image_bytes = bytes(await file.download_as_bytearray())

    context.user_data["input_images"].append(image_bytes)

    await update.message.reply_text(
        f"📷 Загружено изображений: {len(context.user_data['input_images'])}\n"
        f"Теперь отправьте описание."
    )
