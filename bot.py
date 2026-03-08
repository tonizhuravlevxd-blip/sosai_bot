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

MAX_WORKERS = 4
generation_queue = asyncio.Queue(maxsize=200)
active_generations = {}
db_lock = asyncio.Lock()

RATE_LIMIT_SECONDS = 2
user_last_message = {}

def check_rate_limit(user_id):
    now = time.time()
    last = user_last_message.get(user_id, 0)
    if now - last < RATE_LIMIT_SECONDS:
        return False
    user_last_message[user_id] = now
    return True

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

async def generation_worker():

    while True:

        job = await generation_queue.get()

        update = job["update"]
        context = job["context"]
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

                for img in images:
                    upload_images.append(("image.png", img))

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
                    size=size
                )

            image_base64 = result.data[0].b64_json
            image_bytes = base64.b64decode(image_base64)

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🔁 Повторить", callback_data="repeat"),
                    InlineKeyboardButton("🆕 Начать заново", callback_data="restart")
                ],
                [
                    InlineKeyboardButton("❌ Закончить", callback_data="finish")
                ]
            ])

            try:
                await status.delete()
            except:
                pass

            await update.message.reply_photo(
                photo=image_bytes,
                reply_markup=keyboard
            )

        except Exception as e:

            logging.error(f"Generation error: {e}")

            await update.message.reply_text("⚠ Ошибка генерации.")

        finally:

            generation_queue.task_done()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    db_user = get_user(user.id)

    if not db_user:

        cursor.execute(
            "INSERT INTO users (user_id, week_start, accepted_terms) VALUES (?, ?, 0)",
            (user.id, int(time.time()))
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

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    data = query.data

    if data == "accept_terms":

        cursor.execute(
            "UPDATE users SET accepted_terms=1 WHERE user_id=?",
            (query.from_user.id,)
        )

        conn.commit()

        await query.edit_message_text("✅ Условия приняты.")

    elif data == "model_flash":

        context.user_data["model"] = "flash"

        await query.message.reply_text(
            "✅ Выбрана модель:\n⚡ Flash\n\n"
            "✏ Напишите текст для генерации\n"
            "или отправьте 1-4 фото"
        )

    elif data == "model_banana1":

        context.user_data["model"] = "banana1"

        await query.message.reply_text(
            "✅ Выбрана модель:\n🍌 Nano Banana 1\n\n"
            "✏ Напишите текст для генерации\n"
            "или отправьте 1-4 фото"
        )

    elif data == "model_banana2":

        context.user_data["model"] = "banana2"

        await query.message.reply_text(
            "✅ Выбрана модель:\n🍌 Nano Banana 2\n\n"
            "✏ Напишите текст для генерации\n"
            "или отправьте 1-4 фото"
        )

    elif data == "repeat":

        prompt = context.user_data.get("last_prompt")
        images = context.user_data.get("last_images", [])

        status = await query.message.reply_text("🎨 Генерация...")

        await generation_queue.put({
            "update": update,
            "context": context,
            "prompt": prompt,
            "size": "1024x1024",
            "model": context.user_data.get("model","banana2"),
            "images": images,
            "user_id": query.from_user.id,
            "status": status
        })

    elif data == "restart":

        context.user_data.clear()

        await query.message.reply_text("🔄 Сначала выберите модель через /photo")

    elif data == "finish":

        context.user_data.clear()

        await query.message.reply_text("✅ Сессия завершена")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_user.id

    if "model" not in context.user_data:

        await update.message.reply_text(
            "⚠ Сначала выберите модель\nВведите /photo"
        )

        return

    if "input_images" not in context.user_data:
        context.user_data["input_images"] = []

    photo = update.message.photo[-1]

    file = await photo.get_file()

    image_bytes = bytes(await file.download_as_bytearray())

    context.user_data["input_images"].append(image_bytes)

    caption = update.message.caption

    if caption:

        status = await update.message.reply_text("🎨 Генерация...")

        await generation_queue.put({
            "update": update,
            "context": context,
            "prompt": caption,
            "size": "1024x1024",
            "model": context.user_data.get("model","banana2"),
            "images": context.user_data["input_images"],
            "user_id": user_id,
            "status": status
        })

        context.user_data["input_images"] = []

    else:

        await update.message.reply_text(
            f"📷 Загружено изображений: {len(context.user_data['input_images'])}\n"
            "Теперь отправьте текст."
        )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_user.id

    if "model" not in context.user_data:

        await update.message.reply_text(
            "⚠ Сначала выберите модель генерации\nВведите /photo"
        )

        return

    if not check_rate_limit(user_id):

        await update.message.reply_text("⏳ Подождите 2 секунды")

        return

    text = update.message.text

    status = await update.message.reply_text("🎨 Генерация...")

    await generation_queue.put({
        "update": update,
        "context": context,
        "prompt": text,
        "size": "1024x1024",
        "model": context.user_data.get("model","banana2"),
        "images": context.user_data.get("input_images",[]),
        "user_id": user_id,
        "status": status
    })

async def account(update: Update, context: ContextTypes.DEFAULT_TYPE):

    tg_user = update.effective_user
    user = get_user(tg_user.id)

    used = user[2]
    bonus = user[5]

    remaining = FREE_LIMIT + bonus - used

    await update.message.reply_text(
        f"👤 Профиль\n\n"
        f"🆔 ID: {tg_user.id}\n"
        f"👤 Username: @{tg_user.username}\n\n"
        f"🎁 Бонусы: {bonus}\n"
        f"📦 Доступно: {remaining}\n"
        f"👥 Рефералов: {user[4]}"
    )

async def ref(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_user.id
    link = f"https://t.me/{context.bot.username}?start={user_id}"

    await update.message.reply_text(
        f"🎁 Реферальная программа\n\n"
        f"За активного пользователя вы получаете +1 генерацию.\n\n{link}"
    )

async def photo(update: Update, context: ContextTypes.DEFAULT_TYPE):

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ Flash", callback_data="model_flash")],
        [InlineKeyboardButton("🍌 Nano Banana 1", callback_data="model_banana1")],
        [InlineKeyboardButton("🍌 Nano Banana 2", callback_data="model_banana2")]
    ])

    await update.message.reply_text(
        "🎨 Выберите модель генерации:",
        reply_markup=keyboard
    )

app = ApplicationBuilder().token(TG_TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("account", account))
app.add_handler(CommandHandler("ref", ref))
app.add_handler(CommandHandler("photo", photo))

app.add_handler(CallbackQueryHandler(button_handler))
app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

async def set_commands(app):

    await app.bot.set_my_commands([
        BotCommand("start", "Запуск"),
        BotCommand("account", "Профиль"),
        BotCommand("ref", "Реферальная программа"),
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
