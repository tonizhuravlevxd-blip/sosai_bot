import os
import time
import sqlite3
import base64
import asyncio
import logging
import gc

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

SIZE_CONFIG = {
    "square": "1024x1024",
    "wide": "1792x1024",
    "phone": "1024x1792"
}

generation_cache = {}
CACHE_TIME = 3600

db_lock = asyncio.Lock()

RATE_LIMIT_SECONDS = 2
user_last_message = {}

GENERATION_LIMIT = 3
generation_semaphore = asyncio.Semaphore(GENERATION_LIMIT)

# ================= CACHE CLEANER =================

async def cache_cleaner():

    while True:

        await asyncio.sleep(600)

        now = time.time()

        remove_keys = []

        for k,v in generation_cache.items():
            if now - v["time"] > CACHE_TIME:
                remove_keys.append(k)

        for k in remove_keys:
            generation_cache.pop(k, None)

# ================= RATE LIMIT =================

def check_rate_limit(user_id):
    now = time.time()
    last = user_last_message.get(user_id, 0)

    if now - last < RATE_LIMIT_SECONDS:
        return False

    user_last_message[user_id] = now
    return True


def get_queue_position():
    return generation_queue.qsize()


conn = sqlite3.connect("bot.db", check_same_thread=False, timeout=30)
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

# ================= WORKER =================

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

        async with generation_semaphore:

            try:

                style = ""

                if model == "banana1":
                    style = "cinematic lighting ultra realistic 8k"

                elif model == "banana2":
                    style = "hyper detailed masterpiece artstation quality"

                elif model == "flash":
                    style = "fast simple render"

                prompt = f"{style} {prompt}"

                cache_key = f"{prompt}_{model}_{size}"

                cached = generation_cache.get(cache_key)

                if cached and time.time() - cached["time"] < CACHE_TIME:

                    try:
                        await status.delete()
                    except:
                        pass

                    await update.message.reply_photo(photo=cached["image"])

                    generation_queue.task_done()
                    continue

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

                generation_cache[cache_key] = {
                    "image": image_bytes,
                    "time": time.time()
                }

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

                cursor.execute(
                    "UPDATE users SET image_count=image_count+1 WHERE user_id=?",
                    (user_id,)
                )
                conn.commit()

                context.user_data["input_images"] = []

            except Exception as e:

                logging.error(f"Generation error: {e}")

                error_text = str(e)

                if "moderation" in error_text or "safety" in error_text:

                    await update.message.reply_text(
                        "🚫 Запрос отклонён системой безопасности.\n"
                        "Попробуйте изменить текст или изображение."
                    )

                else:

                    await update.message.reply_text(
                        "⚠ Ошибка генерации. Попробуйте позже."
                    )

            finally:

                generation_queue.task_done()

                images.clear()

                gc.collect()

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

        if ref_id:
            cursor.execute(
                "UPDATE users SET referrals = referrals + 1, bonus_images = bonus_images + 1 WHERE user_id=?",
                (ref_id,)
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

# ================= COMMANDS =================

async def finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("✅ Сессия завершена")

async def restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("🔄 Сессия сброшена. Выберите модель через /photo")

async def uu(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_user.id

    user = get_user(user_id)

    reset_week_if_needed(user)

    used = user[2]
    bonus = user[5]

    remaining = FREE_LIMIT + bonus - used

    await update.message.reply_text(
        f"📊 Лимит генераций\n\n"
        f"Использовано: {used}\n"
        f"Бонус: {bonus}\n"
        f"Доступно: {remaining}"
    )

# ================= PHOTO =================

async def photo(update: Update, context: ContextTypes.DEFAULT_TYPE):

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ Flash", callback_data="model_flash")],
        [InlineKeyboardButton("🍌 Nano Banana 1", callback_data="model_banana1")],
        [InlineKeyboardButton("🍌 Nano Banana 2", callback_data="model_banana2")],
        [
            InlineKeyboardButton("⬜ 1:1", callback_data="size_square"),
            InlineKeyboardButton("🖥 16:9", callback_data="size_wide"),
            InlineKeyboardButton("📱 Phone", callback_data="size_phone")
        ]
    ])

    await update.message.reply_text(
        "🎨 Выберите модель и размер изображения:",
        reply_markup=keyboard
    )

# ================= REGISTER =================

app = ApplicationBuilder().token(TG_TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("photo", photo))
app.add_handler(CommandHandler("finish", finish))
app.add_handler(CommandHandler("restart", restart))
app.add_handler(CommandHandler("uu", uu))

app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

async def set_commands(app):

    await app.bot.set_my_commands([
        BotCommand("start","Запуск"),
        BotCommand("photo","Создать изображение"),
        BotCommand("finish","Закончить"),
        BotCommand("restart","Сбросить"),
        BotCommand("uu","Лимит генераций")
    ])

async def post_init(app):

    await set_commands(app)

    for _ in range(MAX_WORKERS):
        asyncio.create_task(generation_worker())

    asyncio.create_task(cache_cleaner())

app.post_init = post_init

if __name__ == "__main__":
    print("🚀 Бот запущен")
    app.run_polling(drop_pending_updates=True)
