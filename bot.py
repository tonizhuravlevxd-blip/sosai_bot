import os
import time
import sqlite3
import base64
import asyncio
import logging
import gc
import aiohttp

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
FAL_KEY = os.getenv("FAL_KEY")

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
    "wide": "1536x1024",
    "phone": "1024x1536"
}

generation_cache = {}
CACHE_TIME = 3600

# защита генераций
active_generations = set()
user_generation_count = {}

MAX_USER_GENERATIONS = 2


# ================= CACHE CLEANER =================

async def cache_cleaner():

    while True:

        await asyncio.sleep(600)

        now = time.time()
        remove_keys = []

        for k, v in generation_cache.items():
            if now - v["time"] > CACHE_TIME:
                remove_keys.append(k)

        for k in remove_keys:
            del generation_cache[k]

        gc.collect()


# ================= DB LOCK =================

db_lock = asyncio.Lock()

RATE_LIMIT_SECONDS = 2
user_last_message = {}

GENERATION_LIMIT = 3
generation_semaphore = asyncio.Semaphore(GENERATION_LIMIT)


def check_rate_limit(user_id):

    now = time.time()
    last = user_last_message.get(user_id, 0)

    if now - last < RATE_LIMIT_SECONDS:
        return False

    user_last_message[user_id] = now
    return True


def get_queue_position():
    return generation_queue.qsize()


# ================= DATABASE =================

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

    cursor.execute(
        "SELECT * FROM users WHERE user_id=?",
        (user_id,)
    )

    return cursor.fetchone()


def reset_week_if_needed(user):

    now = int(time.time())

    if now - user[1] > WEEK_SECONDS:

        # защита от database locked
        async def update():

            async with db_lock:

                cursor.execute(
                    "UPDATE users SET week_start=?, image_count=0 WHERE user_id=?",
                    (now, user[0])
                )

                conn.commit()

        asyncio.create_task(update())

# ================= FAL MODELS CONFIG =================

FAL_MODELS = {

    "banana1": {
        "url": "https://queue.fal.run/fal-ai/nano-banana",
        "edit": True
    },

    "banana2": {
        "url": "https://queue.fal.run/fal-ai/nano-banana-pro",
        "edit": True
    }

}


# ================= FAL VIDEO MODELS =================

FAL_VIDEO_MODELS = {

    "sora2": {
        "url": "https://queue.fal.run/fal-ai/sora-video"
    }

}
# ================= DOWNLOAD FAL IMAGE =================

async def download_fal_image(session, url):

    async with session.get(url) as resp:

        if resp.status != 200:
            raise Exception(f"Failed to download image: {resp.status}")

        return await resp.read()
# ================= UNIVERSAL FAL GENERATOR =================

async def fal_generate(model, prompt, images=None):

    model_cfg = FAL_MODELS[model]

    base_url = model_cfg["url"]
    url = base_url

    if images and model_cfg["edit"]:
        url = f"{base_url}/edit"

    headers = {
        "Authorization": f"Key {FAL_KEY}",
        "Content-Type": "application/json"
    }

    async with aiohttp.ClientSession() as session:

        image_urls = []

        if images:

            for img in images:

                img_base64 = base64.b64encode(img).decode()

                data_uri = f"data:image/jpeg;base64,{img_base64}"

                image_urls.append(data_uri)

        payload = {
            "prompt": prompt,
            "num_images": 1,
            "output_format": "png",
            "safety_tolerance": 5
        }

        if image_urls:
            payload["image_urls"] = image_urls

        async with session.post(url, json=payload, headers=headers) as resp:

            data = await resp.json()

            if "request_id" not in data:
                raise Exception(f"Fal error: {data}")

            request_id = data["request_id"]

        status_url = f"{base_url}/requests/{request_id}/status"
        result_url = f"{base_url}/requests/{request_id}"

        for _ in range(120):

            async with session.get(status_url, headers=headers) as s:

                status_data = await s.json()

                if status_data.get("status") == "COMPLETED":

                    async with session.get(result_url, headers=headers) as r:

                        result = await r.json()

                        images = result.get("images")

                        if not images:
                            raise Exception(f"Fal bad response: {result}")

                        image_url = images[0]["url"]

                        return await download_fal_image(session, image_url)

                if status_data.get("status") == "FAILED":
                    raise Exception(f"Fal generation failed: {status_data}")

            await asyncio.sleep(1)

        raise Exception("Fal generation timeout")

# ================= FAL VIDEO GENERATOR =================

async def fal_video_generate(prompt, images=None):

    base_url = FAL_VIDEO_MODELS["sora2"]["url"]

    headers = {
        "Authorization": f"Key {FAL_KEY}",
        "Content-Type": "application/json"
    }

    async with aiohttp.ClientSession() as session:

        image_urls = []

        if images:

            for img in images:

                img_base64 = base64.b64encode(img).decode()

                data_uri = f"data:image/jpeg;base64,{img_base64}"

                image_urls.append(data_uri)

        payload = {
            "prompt": prompt,
            "duration": 5,
            "aspect_ratio": "16:9"
        }

        if image_urls:
            payload["image_urls"] = image_urls

        async with session.post(base_url, json=payload, headers=headers) as resp:

            data = await resp.json()

            if "request_id" not in data:
                raise Exception(f"Fal video error: {data}")

            request_id = data["request_id"]

        status_url = f"{base_url}/requests/{request_id}/status"
        result_url = f"{base_url}/requests/{request_id}"

        for _ in range(180):

            async with session.get(status_url, headers=headers) as s:

                status = await s.json()

                if status.get("status") == "COMPLETED":

                    async with session.get(result_url, headers=headers) as r:

                        result = await r.json()

                        video_url = None

                        if "video" in result:
                            video_url = result["video"]["url"]

                        elif "videos" in result:
                            video_url = result["videos"][0]["url"]

                        if not video_url:
                            raise Exception(f"Fal video bad response: {result}")

                        async with session.get(video_url) as v:
                            return await v.read()

                if status.get("status") == "FAILED":
                    raise Exception("Sora video generation failed")

            await asyncio.sleep(2)

        raise Exception("Sora video timeout")
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

        mode = job.get("mode", "image")

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

                if cached and time.time() - cached["time"] < CACHE_TIME and mode != "video":

                    try:
                        await status.delete()
                    except:
                        pass

                    await update.message.reply_photo(
                        photo=cached["image"]
                    )

                    generation_queue.task_done()
                    continue

                images = images[:MAX_INPUT_IMAGES]

                # ================= VIDEO MODE (SORA2) =================

                if mode == "video":

                    video_bytes = await fal_video_generate(prompt, images)

                    try:
                        await status.delete()
                    except:
                        pass

                    await asyncio.wait_for(
                        update.message.reply_video(
                            video=video_bytes
                        ),
                        timeout=60
                    )

                    context.user_data["input_images"] = []
                    context.user_data["last_images"] = []

                    generation_queue.task_done()
                    continue

                # ================= FAL IMAGE MODELS =================

                if model in FAL_MODELS:

                    image_bytes = await fal_generate(model, prompt, images)

                # ================= OPENAI MODELS =================

                else:

                    if images:

                        upload_images = []

                        for img in images:
                            upload_images.append(("image.png", img))

                        result = client.images.edit(
                            model="gpt-image-1",
                            image=upload_images,
                            prompt=prompt,
                            size=size,
                        )

                    else:

                        result = client.images.generate(
                            model="gpt-image-1",
                            prompt=prompt,
                            size=size,
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

                await asyncio.wait_for(
                    update.message.reply_photo(
                        photo=image_bytes,
                        reply_markup=keyboard
                    ),
                    timeout=30
                )

                async with db_lock:

                    cursor.execute(
                        "UPDATE users SET image_count=image_count+1 WHERE user_id=?",
                        (user_id,)
                    )

                    conn.commit()

                context.user_data["input_images"] = []
                context.user_data["last_images"] = []

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

                if user_id in active_generations:
                    active_generations.remove(user_id)

                if user_id in user_generation_count:

                    user_generation_count[user_id] -= 1

                    if user_generation_count[user_id] <= 0:
                        del user_generation_count[user_id]

# ================= START =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    ref_by = None

    if context.args:
        try:
            ref_by = int(context.args[0])
        except:
            pass

    db_user = get_user(user.id)

    if not db_user:

        cursor.execute(
            "INSERT INTO users (user_id, week_start, accepted_terms, ref_by) VALUES (?, ?, 0, ?)",
            (user.id, int(time.time()), ref_by)
        )

        conn.commit()

        if ref_by and ref_by != user.id:

            cursor.execute(
                "UPDATE users SET referrals=referrals+1, bonus_images=bonus_images+1 WHERE user_id=?",
                (ref_by,)
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


# ================= FINISH =================

async def finish(update: Update, context: ContextTypes.DEFAULT_TYPE):

    context.user_data.clear()

    await update.message.reply_text(
        "✅ Генерация завершена. Используйте /photo чтобы начать снова."
    )


# ================= RESTART =================

async def restart(update: Update, context: ContextTypes.DEFAULT_TYPE):

    context.user_data["input_images"] = []
    context.user_data["last_images"] = []

    await update.message.reply_text(
        "🔄 Сессия перезапущена. Выберите модель через /photo"
    )


# ================= CALLBACK =================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    data = query.data

    if data == "finish":

        context.user_data.clear()
        await query.message.reply_text("✅ Генерация завершена.")

    elif data == "restart":

        context.user_data["input_images"] = []
        context.user_data["last_images"] = []

        await query.message.reply_text("🔄 Начните заново. Используйте /photo")

    elif data == "accept_terms":

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
            "✏ Напишите текст или отправьте 1-4 фото"
        )

    elif data == "model_banana1":

        context.user_data["model"] = "banana1"

        await query.message.reply_text(
            "✅ Выбрана модель:\n🍌 Nano Banana 1\n\n"
            "✏ Напишите текст или отправьте 1-4 фото"
        )

    elif data == "model_banana2":

        context.user_data["model"] = "banana2"

        await query.message.reply_text(
            "✅ Выбрана модель:\n🍌 Nano Banana 2\n\n"
            "✏ Напишите текст или отправьте 1-4 фото"
        )

    elif data == "size_square":

        context.user_data["size"] = SIZE_CONFIG["square"]
        await query.message.reply_text("⬜ Разрешение 1:1 выбрано")

    elif data == "size_wide":

        context.user_data["size"] = SIZE_CONFIG["wide"]
        await query.message.reply_text("🖥 Разрешение 16:9 выбрано")

    elif data == "size_phone":

        context.user_data["size"] = SIZE_CONFIG["phone"]
        await query.message.reply_text("📱 Вертикальное разрешение выбрано")

    elif data == "repeat":

        prompt = context.user_data.get("last_prompt")
        images = context.user_data.get("last_images", [])

        position = get_queue_position() + 1

        status = await query.message.reply_text(
            f"⏳ Вы в очереди: {position}\n🎨 Подготовка генерации..."
        )

        await generation_queue.put({
            "update": update,
            "context": context,
            "prompt": prompt,
            "size": context.user_data.get("size","1024x1024"),
            "model": context.user_data.get("model","banana2"),
            "images": images,
            "user_id": query.from_user.id,
            "status": status
        })


# ================= PHOTO / TEXT HANDLERS =================
# (оставлены полностью без изменений)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_user.id

    if "model" not in context.user_data:

        await update.message.reply_text(
            "⚠ Сначала выберите модель\nВведите /photo"
        )

        return

    if "input_images" not in context.user_data:
        context.user_data["input_images"] = []

    if len(context.user_data["input_images"]) >= MAX_INPUT_IMAGES:
        return

    photo = update.message.photo[-1]

    file = await photo.get_file()

    image_bytes = bytes(await file.download_as_bytearray())

    context.user_data["input_images"].append(image_bytes)

    caption = update.message.caption

    if caption:

        context.user_data["last_prompt"] = caption
        context.user_data["last_images"] = context.user_data["input_images"]

        position = get_queue_position() + 1

        status = await update.message.reply_text(
            f"⏳ Вы в очереди: {position}\n🎨 Подготовка генерации..."
        )

        await generation_queue.put({
            "update": update,
            "context": context,
            "prompt": caption,
            "size": context.user_data.get("size","1024x1024"),
            "model": context.user_data.get("model","banana2"),
            "images": context.user_data["input_images"],
            "user_id": user_id,
            "status": status
        })


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_user.id
    text = update.message.text

    # ================= GLOBAL ANTI SPAM =================

    if not check_rate_limit(user_id):
        await update.message.reply_text("⏳ Не так быстро. Подождите 2 секунды.")
        return

    if len(text) > 800:
        await update.message.reply_text("⚠ Слишком длинный запрос.")
        return

    # ================= CHATGPT MODE =================

    if context.user_data.get("chat_mode"):

        try:

            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "user", "content": text}
                ]
            )

            answer = response.choices[0].message.content

            await update.message.reply_text(answer)

        except Exception as e:

            logging.error(f"ChatGPT error: {e}")

            await update.message.reply_text(
                "⚠ Ошибка ChatGPT. Попробуйте позже."
            )

        return

    # ================= ПРОВЕРКА ВЫБРАНА ЛИ МОДЕЛЬ =================

    if "model" not in context.user_data:

        await update.message.reply_text(
            "⚠ Сначала выберите модель.\n\nВведите /photo"
        )

        return

    # ================= GENERATION MODE =================

    # защита от двойной генерации
    if user_id in active_generations:
        await update.message.reply_text("⏳ Ваша генерация уже выполняется")
        return

    count = user_generation_count.get(user_id, 0)

    if count >= MAX_USER_GENERATIONS:
        await update.message.reply_text("⚠️ Подождите завершения текущих генераций")
        return

    user = get_user(user_id)

    reset_week_if_needed(user)

    used = user[2]
    bonus = user[5]

    remaining = FREE_LIMIT + bonus - used

    if remaining <= 0:

        await update.message.reply_text(
            "❌ Бесплатные генерации закончились.\n"
            "Пригласите друзей через /ref"
        )

        return

    if generation_queue.full():

        await update.message.reply_text(
            "⚠️ Сервер перегружен. Попробуйте через несколько секунд."
        )

        return

    # блокируем генерацию
    user_generation_count[user_id] = count + 1
    active_generations.add(user_id)

    context.user_data["last_prompt"] = text
    context.user_data["last_images"] = context.user_data.get("input_images", [])

    position = get_queue_position() + 1

    status = await update.message.reply_text(
        f"⏳ Вы в очереди: {position}\n🎨 Подготовка генерации..."
    )

    await generation_queue.put({
        "update": update,
        "context": context,
        "prompt": text,
        "size": context.user_data.get("size", "1024x1024"),
        "model": context.user_data.get("model", "banana2"),
        "images": context.user_data.get("input_images", []),
        "user_id": user_id,
        "status": status
    })
# ================= COMMANDS =================
async def video(update: Update, context: ContextTypes.DEFAULT_TYPE):

    context.user_data["mode"] = "video"

    await update.message.reply_text(
        "🎬 Режим видео включён (Sora2)\n\n"
        "Отправьте промпт или фото + текст."
    )

async def uu(update: Update, context: ContextTypes.DEFAULT_TYPE):

    context.user_data["chat_mode"] = True

    await update.message.reply_text(
        "🤖 Режим ChatGPT включен\n\n"
        "Напишите чем вам помочь."
    )


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
app.add_handler(CommandHandler("account", account))
app.add_handler(CommandHandler("ref", ref))
app.add_handler(CommandHandler("photo", photo))
app.add_handler(CommandHandler("video", video))
app.add_handler(CommandHandler("uu", uu))
app.add_handler(CommandHandler("finish", finish))
app.add_handler(CommandHandler("restart", restart))

app.add_handler(CallbackQueryHandler(button_handler))

app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))


async def set_commands(app):

    await app.bot.set_my_commands([
        BotCommand("start", "Запуск"),
        BotCommand("account", "Профиль"),
        BotCommand("ref", "Реферальная программа"),
        BotCommand("photo", "Создать изображение"),
        BotCommand("video", "Создать видео"),
        BotCommand("uu", "Лимит генераций"),
        BotCommand("finish", "Закончить генерацию"),
        BotCommand("restart", "Перезапустить")
    ])

async def start_worker():

    while True:

        try:

            await generation_worker()

        except Exception as e:

            logging.error(f"Worker crashed: {e}")

            await asyncio.sleep(2)

async def post_init(app):

    await set_commands(app)

    for _ in range(MAX_WORKERS):
        asyncio.create_task(start_worker())

    asyncio.create_task(cache_cleaner())


app.post_init = post_init


if __name__ == "__main__":
    print("🚀 Бот запущен")
    app.run_polling(drop_pending_updates=True)

