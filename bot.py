import os
import time
import asyncpg
import base64
import asyncio
import logging
import gc
import aiohttp
import json
import io

from telegram.ext import PreCheckoutQueryHandler

from yookassa import Configuration, Payment

Configuration.account_id = os.getenv("YOOKASSA_SHOP_ID")
Configuration.secret_key = os.getenv("YOOKASSA_SECRET_KEY")


import uuid

async def create_payment(user_id: int, payment_type="premium", price=100):

    price = float(price)  

    description_map = {
        "premium": "Премиум на месяц",
        "video": "Покупка 1 видео",
        "music": "Покупка 1 трека"
    }

    description = description_map.get(payment_type, "Покупка")

    payment = Payment.create({
        "amount": {
            "value": f"{price:.2f}",
            "currency": "RUB"
        },
        "confirmation": {
            "type": "redirect",
            "return_url": "https://t.me/Sosai_uu_bot"
        },
        "capture": True,
        "description": f"{description} для user {user_id}",
        "metadata": {
            "user_id": str(user_id),
            "type": payment_type
        },
        "receipt": {
            "customer": {
                "email": f"user{user_id}@example.com"
            },
            "items": [
                {
                    "description": description,
                    "quantity": "1.00",
                    "amount": {
                        "value": f"{price:.2f}",
                        "currency": "RUB"
                    },
                    "vat_code": 1,
                    "payment_mode": "full_payment",
                    "payment_subject": "service"
                }
            ]
        }
    })

    return payment.confirmation.confirmation_url


DATABASE_URL = os.getenv("DATABASE_URL")
db_pool = None

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

import logging
from telegram import Update
from telegram.ext import ContextTypes

TG_TOKEN = os.getenv("TG_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
FAL_KEY = os.getenv("FAL_KEY")
ADMIN_IDS = [5523265642,7924313002] 

if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY не установлен")

client = OpenAI(api_key=OPENAI_API_KEY)

FREE_LIMIT = 5
FREE_VIDEO_LIMIT = 1
WEEK_SECONDS = 7 * 24 * 60 * 60
MAX_INPUT_IMAGES = 4
# ===== PREMIUM LIMITS =====
# ================= PRICES =================
PRICE_VIDEO = "28.00"
PRICE_MUSIC = "50.00"
PRICE_CARTOON = "29.00"

PREMIUM_IMAGE_LIMIT = 20
PREMIUM_VIDEO_LIMIT = 5
PREMIUM_MUSIC_LIMIT = 3

REQUIRED_CHANNEL = "@sosai_ai"

USER_AGREEMENT_URL = "https://disk.yandex.ru/i/IB_pG2pcgtEIGQ"
OFFER_URL = "https://disk.yandex.ru/i/8IXTO8-VSMmbuw"

MAX_WORKERS = 8

generation_queue = None



SIZE_CONFIG = {
    "square": "1024x1024",
    "wide": "1536x1024",
    "phone": "1024x1536"
}

generation_cache = {}
CACHE_TIME = 3600
USER_CACHE = {}
USER_CACHE_TTL = 60  # секунд

# защита генераций
active_generations = set()
user_generation_count = {}

MAX_USER_GENERATIONS = 2
def check_user_generation_limit(user_id):

    count = user_generation_count.get(user_id, 0)

    if count >= MAX_USER_GENERATIONS:
        return False, "⚠️ Подождите завершения текущих генераций"

    return True, None


def lock_user_generation(user_id):
    count = user_generation_count.get(user_id, 0)
    user_generation_count[user_id] = count + 1
    
def unlock_user_generation(user_id):
    count = user_generation_count.get(user_id, 0)

    if count <= 1:
        user_generation_count.pop(user_id, None)
    else:
        user_generation_count[user_id] = count - 1    
    


# ================= CACHE CLEANER =================
MAX_CACHE_SIZE = 500

async def user_cache_cleaner():
    while True:
        await asyncio.sleep(120)

        now = time.time()
        to_delete = []

        for k, v in USER_CACHE.items():
            if now - v["time"] > USER_CACHE_TTL:
                to_delete.append(k)

        for k in to_delete:
            USER_CACHE.pop(k, None)

async def cache_cleaner():

    while True:

        await asyncio.sleep(600)

        now = time.time()
        remove_keys = []

        # Удаляем устаревшие элементы
        for k, v in generation_cache.items():
            if now - v["time"] > CACHE_TIME:
                remove_keys.append(k)

        for k in remove_keys:
            del generation_cache[k]

        # ===== Ограничение максимального размера кэша =====
        while len(generation_cache) > MAX_CACHE_SIZE:
            # удаляем самый старый элемент
            generation_cache.pop(next(iter(generation_cache)))

        gc.collect()

# ================= DB LOCK =================

db_lock = asyncio.Lock()

RATE_LIMIT_SECONDS = 1.5
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





# ================= DATABASE =================

async def init_db():
    global db_pool

    # 🔥 ОПТИМИЗИРОВАННЫЙ ПУЛ
    db_pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=5,
        max_size=20,
        command_timeout=60
    )

    async with db_pool.acquire() as conn:

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            week_start BIGINT,
            image_count INTEGER DEFAULT 0,
            video_count INTEGER DEFAULT 0,
            accepted_terms INTEGER DEFAULT 0,
            referrals INTEGER DEFAULT 0,
            bonus_images INTEGER DEFAULT 0,
            ref_by BIGINT,
            is_active INTEGER DEFAULT 0,
            premium INTEGER DEFAULT 0,
            premium_until BIGINT DEFAULT 0,
            last_payment_id TEXT,
            music_count INTEGER DEFAULT 0,

            paid_video INTEGER DEFAULT 0,
            paid_music INTEGER DEFAULT 0,

            -- 🔥 НОВОЕ
            created_at BIGINT DEFAULT 0,
            last_active BIGINT DEFAULT 0,
            ref_rewarded INTEGER DEFAULT 0
        )
        """)

        # ===== SAFE ALTER =====
        await conn.execute("""
        ALTER TABLE users ADD COLUMN IF NOT EXISTS last_payment_id TEXT
        """)

        await conn.execute("""
        ALTER TABLE users ADD COLUMN IF NOT EXISTS music_count INTEGER DEFAULT 0
        """)

        await conn.execute("""
        ALTER TABLE users ADD COLUMN IF NOT EXISTS paid_video INTEGER DEFAULT 0
        """)

        await conn.execute("""
        ALTER TABLE users ADD COLUMN IF NOT EXISTS paid_music INTEGER DEFAULT 0
        """)

        await conn.execute("""
        ALTER TABLE users ADD COLUMN IF NOT EXISTS ref_rewarded INTEGER DEFAULT 0
        """)

        await conn.execute("""
        ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at BIGINT DEFAULT 0
        """)

        await conn.execute("""
        ALTER TABLE users ADD COLUMN IF NOT EXISTS last_active BIGINT DEFAULT 0
        """)

        # 🔥 ИНДЕКСЫ (очень важно для нагрузки)
        await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_users_created_at ON users(created_at)
        """)

        await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_users_last_active ON users(last_active)
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS music_cache (
            prompt TEXT PRIMARY KEY,
            audio_url TEXT,
            created_at BIGINT
        )
        """)


async def stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Нет доступа")
        return

    now = int(time.time())
    day_ago = now - 86400

    async with db_pool.acquire() as conn:

        total_users = await conn.fetchval("SELECT COUNT(*) FROM users")

        new_users_24h = await conn.fetchval("""
            SELECT COUNT(*) FROM users WHERE created_at > $1
        """, day_ago)

        active_24h = await conn.fetchval("""
            SELECT COUNT(*) FROM users WHERE last_active > $1
        """, day_ago)

        total_images = await conn.fetchval("SELECT SUM(image_count) FROM users")
        total_videos = await conn.fetchval("SELECT SUM(video_count) FROM users")
        total_music = await conn.fetchval("SELECT SUM(music_count) FROM users")

        paid_video = await conn.fetchval("SELECT SUM(paid_video) FROM users")
        paid_music = await conn.fetchval("SELECT SUM(paid_music) FROM users")

        # ✅ НОВОЕ: premium пользователи
        premium_users = await conn.fetchval("""
            SELECT COUNT(*) FROM users WHERE premium = 1
        """)

        # ✅ НОВОЕ: все генерации за всё время
        total_generations_all = await conn.fetchval("""
            SELECT 
                COALESCE(SUM(image_count),0) +
                COALESCE(SUM(video_count),0) +
                COALESCE(SUM(music_count),0)
            FROM users
        """)

    total_images = total_images or 0
    total_videos = total_videos or 0
    total_music = total_music or 0

    total_generations = total_images + total_videos + total_music

    # 🔥 ОНЛАЙН ИЗ ПАМЯТИ
    online = sum(
        1 for t in ONLINE_USERS.values()
        if time.time() - t < ONLINE_TTL
    )

    text = f"""
📊 <b>СТАТИСТИКА БОТА</b>

👤 Всего пользователей: {total_users}
🆕 Новые за 24ч: {new_users_24h}
🔥 Активные за 24ч: {active_24h}
👀 Онлайн сейчас: {online}

🎨 Генерации:
🖼 Фото: {total_images}
🎬 Видео: {total_videos}
🎵 Музыка: {total_music}
📦 Всего: {total_generations}

💳 Куплено:
🎬 Видео: {paid_video or 0}
🎵 Музыка: {paid_music or 0}
💰 Premium: {premium_users}

📦 Всего генераций за всё время: {total_generations_all}

⚙️ Очередь:
🖼 Image: {generation_queue_image.qsize()}
🎬 Video: {generation_queue_video.qsize()}
🎵 Music: {generation_queue_music.qsize()}
"""

    await update.message.reply_text(text, parse_mode="HTML")

# ================= MUSIC CACHE FUNCTIONS =================

async def get_cached_music(prompt):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT audio_url FROM music_cache WHERE prompt=$1",
            prompt
        )

        if row:
            return row["audio_url"]

        return None


async def save_music_cache(prompt, audio_url):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO music_cache (prompt, audio_url, created_at)
            VALUES ($1, $2, $3)
            ON CONFLICT (prompt) DO UPDATE
            SET audio_url = EXCLUDED.audio_url,
                created_at = EXCLUDED.created_at
            """,
            prompt, audio_url, int(time.time())
        )


# ================= USER FUNCTIONS =================

async def get_user(user_id):

    now = time.time()

    cached = USER_CACHE.get(user_id)

    if cached and now - cached["time"] < USER_CACHE_TTL:
        return cached["data"]

    async with db_pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT * FROM users WHERE user_id=$1",
            user_id
        )

    if user:
        USER_CACHE[user_id] = {
            "data": user,
            "time": now
        }

    return user


async def reset_week_if_needed(user):

    now = int(time.time())

    if not user["week_start"] or now - user["week_start"] > WEEK_SECONDS:

        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE users 
                SET week_start=$1, video_count=0, image_count=0 
                WHERE user_id=$2
                """,
                now, user["user_id"]
            )
            USER_CACHE.pop(user["user_id"], None)


def is_premium(user):

    if not user:
        return False

    premium = user["premium"]
    premium_until = user["premium_until"]

    if premium == 1 and premium_until > int(time.time()):
        return True

    return False        

async def reset_user_limits(user_id):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE users
            SET image_count = 0,
                video_count = 0,
                music_count = 0,
                premium = 0,
                premium_until = 0,
                week_start = $1
            WHERE user_id = $2
            """,
            int(time.time()),
            user_id
        )
        USER_CACHE.pop(user_id, None)

async def is_user_subscribed(bot, user_id):
    try:
        member = await bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        return member.status in ["member", "administrator", "creator"]
    except:
        return False

def get_subscribe_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Подписаться", url=f"https://t.me/{REQUIRED_CHANNEL.replace('@','')}")],
        [InlineKeyboardButton("✅ Проверить подписку", callback_data="check_sub")]
    ])

# ================= ULTRA PROMPT ENGINE =================

def clean_prompt(prompt: str, mode: str = "image"):

    if not prompt:
        return prompt

    # ===== SAFE REPLACEMENTS (БЕЗ ЛОМАНИЯ СМЫСЛА) =====
    replacements = {

        # оружие → нейтрально
        "стреляет": "испускает свет",
        "стрельба": "энергетический эффект",
        "оружие": "устройство",
        "пистолет": "устройство",
        "бластер": "фантастическое устройство",

        "gun": "futuristic device",
        "weapon": "tool",
        "shoot": "emit light",
        "shooting": "light effect",

        # насилие → cinematic
        "убивает": "побеждает",
        "кровь": "красная энергия",

        "kill": "defeat",
        "killing": "defeating",
        "blood": "red energy",
        "murder": "dramatic action",

        # бренды → стили
        "simpsons": "yellow cartoon sitcom style",
        "pixar": "3d animated cinematic style",
        "disney": "fantasy animation style",
        "rick and morty": "crazy sci-fi cartoon style",

        # sora sensitive
        "laser": "light beam",
        "attack": "fast action movement",
        "battle": "epic cinematic scene",
        "fight": "dynamic action sequence",
        "explosion": "bright cinematic flash",
    }

    cleaned = prompt

    # НЕ делаем lower() ❗
    for bad, good in replacements.items():
        cleaned = cleaned.replace(bad, good)
        cleaned = cleaned.replace(bad.capitalize(), good)

    # ===== MODE SWITCH (БЕЗ БУСТЕРОВ) =====
    mode = (mode or "").lower()

    return cleaned
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

# ================= CARTOON STYLES =================

CARTOON_STYLES = {

    "pixar": "3D animated movie style, expressive eyes, cinematic lighting",

    "disney": "magical fantasy animation style, colorful cinematic lighting",

    "anime": "japanese anime movie style, vibrant colors, detailed animation",

    "dreamworks": "cinematic animated character style, expressive faces",

    "ghibli": "soft watercolor anime style, dreamy lighting, nature atmosphere",

    "simpsons": "yellow skin cartoon family style, bold outlines, sitcom animation",

    "rickmorty": "crazy sci fi cartoon style, exaggerated expressions, bold lines"
}

# ================= FAL VIDEO MODELS =================

FAL_VIDEO_MODELS = {

    "text": {
        "url": "https://queue.fal.run/fal-ai/sora-2/text-to-video"
    },

    "image": {
        "url": "https://queue.fal.run/fal-ai/sora-2/image-to-video"
    }

}
# ================= DOWNLOAD FAL IMAGE =================

async def download_fal_image(session, url):

    async with session.get(url) as resp:

        if resp.status != 200:
            raise Exception(f"Failed to download image: {resp.status}")

        return await resp.read()
# ================= UNIVERSAL FAL GENERATOR =================

async def retry(func, *args, retries=3):

    for i in range(retries):
        try:
            return await func(*args)
        except Exception as e:
            if i == retries - 1:
                raise
            await asyncio.sleep(2)

async def fal_generate(model, prompt, images=None):
    prompt = clean_prompt(prompt)  # ✅ очистка перед отправкой

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

            status_url = data["status_url"]
            result_url = data["response_url"]

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

async def fal_music_generate(prompt, duration=30, max_wait=180):
    """
    Генерация музыки через FAL с прогресс-логированием.

    :param prompt: текстовый промпт
    :param duration: длина трека в секундах
    :param max_wait: максимальное время ожидания генерации (в секундах)
    :return: URL с аудио
    """
    prompt = clean_prompt(prompt)

    base_url = "https://queue.fal.run/sonauto/v2/text-to-music"
    headers = {
        "Authorization": f"Key {FAL_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "prompt": prompt,
        "duration": duration
    }

    logging.info(f"🎵 FAL REQUEST: {payload}")

    async with aiohttp.ClientSession() as session:

        # ===== СОЗДАНИЕ ЗАДАЧИ =====
        async with session.post(base_url, json=payload, headers=headers) as r:
            text = await r.text()
            logging.info(f"🎵 FAL CREATE RESPONSE: {text}")

            try:
                data = json.loads(text)
            except Exception:
                raise Exception(f"Fal bad response: {text}")

        if "request_id" not in data:
            raise Exception(f"Fal music error: {data}")

        request_id = data["request_id"]
        logging.info(f"🎵 FAL REQUEST_ID: {request_id}")

        status_url = data["status_url"]
        result_url = data["response_url"]

        start_time = time.time()
        last_status = None

        # ===== ОЖИДАНИЕ =====
        while True:
            await asyncio.sleep(2)

            async with session.get(status_url, headers=headers) as r:
                try:
                    status_data = await r.json()
                except Exception as e:
                    logging.error(f"Status parse error: {e}")
                    continue

            status = status_data.get("status")
            logging.info(f"🎵 STATUS RAW: {status_data}")

            if status != last_status:
                logging.info(f"🎵 Music generation status: {status} | prompt: {prompt}")
                last_status = status

            # ===== УСПЕХ =====
            if status == "COMPLETED":
                async with session.get(result_url, headers=headers) as r:
                    try:
                        result = await r.json()
                    except Exception:
                        raise Exception("Failed to parse FAL music result")

                logging.info(f"🎵 FAL RAW RESULT: {result}")

                audio_url = None

                if "audio" in result:
                    if isinstance(result["audio"], dict):
                        audio_url = result["audio"].get("url")
                    elif isinstance(result["audio"], list) and result["audio"]:
                        audio_url = result["audio"][0].get("url")

                if not audio_url and "audios" in result:
                    audio_url = result["audios"][0].get("url")

                if not audio_url and "audio_url" in result:
                    audio_url = result["audio_url"]

                if not audio_url and "url" in result:
                    audio_url = result["url"]

                if not audio_url and "output" in result:
                    output = result["output"]

                    if isinstance(output, dict):
                        if "audio" in output:
                            if isinstance(output["audio"], dict):
                                audio_url = output["audio"].get("url")
                            elif isinstance(output["audio"], list) and output["audio"]:
                                audio_url = output["audio"][0].get("url")

                        elif "audios" in output:
                            audio_url = output["audios"][0].get("url")

                if not audio_url:
                    logging.error(f"❌ NO AUDIO URL: {result}")
                    raise Exception(f"Fal returned no audio | result={result}")

                logging.info(f"🎧 FAL Audio URL: {audio_url}")
                return audio_url

            # ===== ОШИБКА =====
            if status == "FAILED":
                logging.error(f"❌ FAL FAILED: {status_data}")
                raise Exception(f"Fal music generation failed: {status_data}")

            # ===== ТАЙМАУТЫ =====
            if time.time() - start_time > max_wait:
                logging.error(f"❌ TIMEOUT: {status_data}")
                raise Exception(f"Music generation timeout (> {max_wait}s)")





# ================= FAL VIDEO GENERATOR =================

async def fal_video_generate(prompt, images=None):
    prompt = clean_prompt(prompt)  # ✅ очистка перед отправкой

    if images:
        base_url = FAL_VIDEO_MODELS["image"]["url"]
    else:
        base_url = FAL_VIDEO_MODELS["text"]["url"]

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
            "duration": 4,
            "resolution": "720p"
        }
        logging.info(f"🎬 Video generation started for prompt: {prompt}")

        # если есть картинка — используем как стартовый кадр
        if images and image_urls:
            payload["image_url"] = image_urls[0]

        async with session.post(base_url, json=payload, headers=headers) as resp:

            data = await resp.json()

            if "request_id" not in data:
                raise Exception(f"Fal video error: {data}")

            request_id = data["request_id"]

        status_url = f"https://queue.fal.run/fal-ai/sora-2/requests/{request_id}/status"
        result_url = f"https://queue.fal.run/fal-ai/sora-2/requests/{request_id}"

        # sora-2 может генерировать долго
        for _ in range(300):

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

# ================= FAKE PHOTO UPLOAD ACTION =================
async def fake_photo_upload(bot, chat_id):
    try:
        while True:
            await bot.send_chat_action(
                chat_id=chat_id,
                action="upload_photo"
            )
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass



# ================= QUEUES AND SEMAPHORES =================
generation_queue_image = asyncio.Queue(maxsize=5000)
generation_queue_video = asyncio.Queue(maxsize=2000)
generation_queue_music = asyncio.Queue(maxsize=2000)
user_locks = {}

async def can_generate_video(conn, user_id, premium, free_limit):

    user = await conn.fetchrow(
        "SELECT video_count, paid_video FROM users WHERE user_id=$1",
        user_id
    )

    if premium:
        return user["video_count"] < PREMIUM_VIDEO_LIMIT

    if user["paid_video"] > 0:
        return True

    return user["video_count"] < free_limit

async def consume_video(conn, user_id, premium, free_limit):

    # ===== PREMIUM =====
    if premium:
        result = await conn.fetchrow("""
            UPDATE users
            SET video_count = video_count + 1
            WHERE user_id=$1 AND video_count < $2
            RETURNING video_count
        """, user_id, PREMIUM_VIDEO_LIMIT)
        USER_CACHE.pop(user_id, None)

        return bool(result)

    # ===== ✅ СНАЧАЛА ПЛАТНЫЕ (БЕЗ УВЕЛИЧЕНИЯ video_count) =====
    result = await conn.fetchrow("""
        UPDATE users
        SET paid_video = paid_video - 1
        WHERE user_id=$1 AND paid_video > 0
        RETURNING paid_video
    """, user_id)
    USER_CACHE.pop(user_id, None)

    if result:
        return True

    # ===== FREE =====
    result = await conn.fetchrow("""
        UPDATE users
        SET video_count = video_count + 1
        WHERE user_id=$1 AND video_count < $2
        RETURNING video_count
    """, user_id, free_limit)
    USER_CACHE.pop(user_id, None)

    return bool(result)


# ================== UNIVERSAL HANDLER (FIXED FINAL) ==================
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
ADMIN_REPLY_STATE = {}
SUPPORT_REPLY_MAP = {}
ONLINE_USERS = {}
ONLINE_TTL = 300
active_generations = set()
GLOBAL_RATE_LIMIT = asyncio.Semaphore(100)
GLOBAL_SEMAPHORE = asyncio.Semaphore(50)

semaphore_image = asyncio.Semaphore(30)
semaphore_video = asyncio.Semaphore(10)
semaphore_music = asyncio.Semaphore(5)

async def handle_generation_job(job):

    update = job["update"]
    context = job["context"]
    prompt = job.get("prompt")
    size = job.get("size", "1024x1024")
    model = job.get("model", "banana2")
    images = job.get("images", [])
    user_id = job["user_id"]
    status = job.get("status")
    mode = job.get("mode", "image")
    video_allowed = False

    msg = getattr(update, "message", None)
    if not msg and getattr(update, "callback_query", None):
        msg = update.callback_query.message

    if not prompt and not images:
        logging.warning(f"⚠ ПУСТАЯ ЗАДАЧА user={user_id} mode={mode}")
        return

    # ===== 🔥 НОВЫЙ LOCK ВМЕСТО active_generations =====
    lock = user_locks.setdefault(user_id, asyncio.Lock())

    if lock.locked():
        if msg:
            await msg.reply_text("⏳ Генерация уже выполняется.")
        return

    async with lock:
        try:
            # ===== СЕМАФОРЫ =====
            sem = semaphore_image
            if mode in ["video", "cartoon"]:
                sem = semaphore_video
            elif mode == "music":
                sem = semaphore_music

            async with GLOBAL_RATE_LIMIT:
                async with GLOBAL_SEMAPHORE:
                    async with sem:

                        # ===== 🔥 АТОМАРНЫЕ ЛИМИТЫ =====
                        async with db_pool.acquire() as conn:

                            user = await conn.fetchrow(
                                "SELECT * FROM users WHERE user_id=$1",
                                user_id
                            )

                            if not user:
                                return

                            logging.info(f"USER DATA: {dict(user)}")

                            await reset_week_if_needed(user)
                            premium = is_premium(user)

                            # ===== IMAGE =====
                            if mode == "image":

                                if not premium:
                                    free_limit = 2

                                    if user["image_count"] >= free_limit:

                                        # ✅ сначала проверяем, нажал ли пользователь кнопку
                                        if not context.user_data.get("sub_checked"):

                                            subscribed = await is_user_subscribed(context.bot, user_id)

                                            if not subscribed:
                                                await msg.reply_text(
                                                    "📢 Бесплатный лимит (2 фото) исчерпан.\n\n"
                                                    "Подпишитесь на канал и нажмите проверить 👇",
                                                    reply_markup=get_subscribe_keyboard()
                                                )
                                                return

                                            # ✅ если реально подписан — сохраняем
                                            context.user_data["sub_checked"] = True

                                        # ✅ даём расширенный лимит
                                        limit = FREE_LIMIT + user.get("bonus_images", 0)
                                    else:
                                        limit = free_limit
                                else:
                                    limit = PREMIUM_IMAGE_LIMIT

                                result = await conn.fetchrow("""
                                    UPDATE users
                                    SET image_count = image_count + 1
                                    WHERE user_id=$1 AND image_count < $2
                                    RETURNING image_count
                                """, user_id, limit)
                                USER_CACHE.pop(user_id, None)

                                if not result:
                                    await msg.reply_text("⚠️ Лимит изображений исчерпан")
                                    return

                            # ================= VIDEO / CARTOON =================
                            elif mode in ["video", "cartoon"]:

                                if not premium:
                                    subscribed = await is_user_subscribed(context.bot, user_id)

                                    # ===== ЖЁСТКАЯ БЛОКИРОВКА ДО ПРОВЕРКИ =====
                                    if not context.user_data.get("sub_checked"):

                                        context.user_data["pending_video"] = True

                                        await msg.reply_text(
                                            "📢 Перед бесплатной генерацией нужно подтвердить подписку 👇\n\n"
                                            "Даже если вы подписаны — нажмите кнопку для проверки",
                                            reply_markup=get_subscribe_keyboard()
                                        )
                                        return

                                logging.info(f"🎬 START VIDEO FLOW user={user_id}")

                                user = await conn.fetchrow(
                                    "SELECT * FROM users WHERE user_id=$1",
                                    user_id
                                )

                                premium = is_premium(user)

                                logging.info(f"USER BEFORE CHECK: {dict(user)}")

                                paid_video = user.get("paid_video") or 0
                                video_count = user.get("video_count") or 0

                                logging.info(
                                    f"🎯 DECISION user={user_id} "
                                    f"paid={paid_video} video_count={video_count} premium={premium}"
                                )

                                if paid_video > 0:
                                    logging.info(f"💰 USING PAID VIDEO user={user_id}")

                                    result = await conn.fetchrow("""
                                        UPDATE users
                                        SET paid_video = paid_video - 1
                                        WHERE user_id=$1 AND paid_video > 0
                                        RETURNING paid_video
                                    """, user_id)
                                    USER_CACHE.pop(user_id, None)

                                    if not result:
                                        logging.error(f"❌ FAILED TO USE PAID VIDEO user={user_id}")
                                        await msg.reply_text("⚠️ Ошибка списания платного видео")
                                        return

                                elif premium:
                                    logging.info(f"🍩 USING PREMIUM LIMIT user={user_id}")

                                    result = await conn.fetchrow("""
                                        UPDATE users
                                        SET video_count = video_count + 1
                                        WHERE user_id=$1 AND video_count < $2
                                        RETURNING video_count
                                    """, user_id, PREMIUM_VIDEO_LIMIT)
                                    USER_CACHE.pop(user_id, None)

                                    if not result:
                                        await msg.reply_text("⚠️ Лимит видео исчерпан (Premium)")
                                        return

                                else:
                                    logging.info(f"🆓 USING FREE LIMIT user={user_id}")

                                    result = await conn.fetchrow("""
                                        UPDATE users
                                        SET video_count = video_count + 1
                                        WHERE user_id=$1 AND video_count < $2
                                        RETURNING video_count
                                    """, user_id, FREE_VIDEO_LIMIT)
                                    USER_CACHE.pop(user_id, None)

                                    if not result:
                                        keyboard = InlineKeyboardMarkup([
                                            [InlineKeyboardButton("💳 Купить 1 видео", callback_data="buy_video")],
                                            [InlineKeyboardButton("🍩 Premium", callback_data="buy_spb")]
                                        ])

                                        await msg.reply_text(
                                            "🎬 Лимит видео исчерпан",
                                            reply_markup=keyboard
                                        )
                                        return
                                        

            model_name = "NanoBanana 1" if model == "banana1" else "NanoBanana 2"

            text_map = {
                "image": f"<pre>🎨 Шедевр создает {model_name}</pre>",
                "video": "<pre>🎬 Генерация видео... 0%</pre>",
                "cartoon": "<pre>🎬 Генерация мультфильма... 0%</pre>",
                "music": "<pre>🎵 Генерация музыки... 0%</pre>"
            }

            if status:
                try:
                    await status.edit_text(
                        text_map.get(mode, "⏳ Генерация..."),
                        reply_markup=cancel_button,
                        parse_mode="HTML"
                    )
                except:
                    pass
            else:
                status = await msg.reply_text(
                    text_map.get(mode, "⏳ Генерация..."),
                    reply_markup=cancel_button,
                    parse_mode="HTML"
                )

            images_local = images[:MAX_INPUT_IMAGES]

            style = ""
            if model == "banana1":
                style = "cinematic lighting ultra realistic 8k"
            elif model == "banana2":
                style = "hyper detailed masterpiece artstation quality"

            cartoon_style = context.user_data.get("cartoon_style")

            if prompt:
                if mode == "image" and style:
                    prompt = f"{style} {prompt}"
                elif mode in ["cartoon", "video"] and cartoon_style:
                    prompt = f"{cartoon_style}, {prompt}"

            if prompt:
                prompt = clean_prompt(prompt)

            cache_key = f"{prompt}_{model}_{size}" if prompt else None
            cached = generation_cache.get(cache_key) if cache_key else None

            if cached and time.time() - cached["time"] < CACHE_TIME and mode not in ["video", "music"]:
                try:
                    if status:
                        await status.delete()
                except:
                    pass
                await msg.reply_photo(photo=cached["image"])
                return

            # ================= IMAGE =================
            if mode == "image":

                async def dots_animation():
                    dots_list = ["", ".", "..", "..."]
                    i = 0

                    try:
                        while True:
                            dots = dots_list[i % len(dots_list)]
                            text = f"<pre>🦕 Пожалуйста ожидайте,шедевр создает {model_name}{dots}</pre>"
                            try:
                                await status.edit_text(text, parse_mode="HTML")
                            except:
                                pass
                            i += 1
                            await asyncio.sleep(0.6)
                    except asyncio.CancelledError:
                        pass

                animation_task = asyncio.create_task(dots_animation())

                upload_task = asyncio.create_task(
                    fake_photo_upload(context.bot, update.effective_chat.id)
                )

                try:
                    for attempt in range(2):
                        try:
                            result = await asyncio.wait_for(
                                fal_generate(model, prompt, images_local),
                                timeout=300
                            )
                            break
                        except Exception as e:
                            if attempt == 1:
                                raise e
                            await asyncio.sleep(1)
                finally:
                    upload_task.cancel()
                    animation_task.cancel()

                try:
                    await status.delete()
                except:
                    pass

                keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("🔁 Повторить", callback_data="repeat"),
                        InlineKeyboardButton("🆕 Начать заново", callback_data="restart")
                    ],
                    [
                        InlineKeyboardButton("❌ Закончить", callback_data="finish")
                    ]
                ])

                await msg.reply_photo(photo=result, reply_markup=keyboard)
                                
                async with db_pool.acquire() as conn:

                    ref_data = await conn.fetchrow(
                        "SELECT ref_by, ref_rewarded FROM users WHERE user_id=$1",
                        user_id
                    )

                    if ref_data and ref_data["ref_by"] and ref_data["ref_rewarded"] == 0:

                        await conn.execute(
                            """
                            UPDATE users
                            SET ref_rewarded = 1
                            WHERE user_id=$1
                            """,
                            user_id
                        )

                        await conn.execute(
                            """
                            UPDATE users
                            SET bonus_images = bonus_images + 1
                            WHERE user_id=$1
                            """,
                            ref_data["ref_by"]
                        )

                context.user_data["last_prompt"] = prompt
                context.user_data["last_images"] = images_local

            # ================= VIDEO / CARTOON =================
            elif mode in ["video", "cartoon"]:

                import random

                async def progress_updater():
                    steps = [
                        "🎬 Анализ промпта...",
                        "֎🇦🇮 Подготовка модели...",
                        "🎥 Генерация сцен...",
                        "🎞 Рендеринг кадров...",
                        "🧙 Просим волшебника помочь...",
                        "🦄 Происходит магия...",
                        "✩°｡⋆⸜(˙꒳​˙ )...",
                        "🍕 Перерыв на обед...",
                        "👨🏻‍💻 Рендеринг кадров...",
                        "🐇 Кролик попал в кадр...",
                        "🕵🏼 Ищем кролика...",
                        "🧹 Убираем лишнее...",
                        "✨ Постобработка...",
                        "🦕 Шедевр почти готов...",
                        "😱 Осталось совсем немного...",
                        "📦 Финальная сборка..."
                    ]

                    idx = 0
                    last_text = ""

                    try:
                        while idx < len(steps):
                            new_text = steps[idx]

                            if new_text != last_text:
                                try:
                                    await status.edit_text(new_text)
                                    last_text = new_text
                                except:
                                    pass

                            await asyncio.sleep(random.randint(5, 10))
                            idx += 1

                        # финальное сообщение
                        try:
                            await status.edit_text("🚀 Завершение обработки...")
                        except:
                            pass

                    except asyncio.CancelledError:
                        pass

                progress_task = asyncio.create_task(progress_updater())

                try:
                    result_bytes = await asyncio.wait_for(
                        fal_video_generate(prompt, images_local),
                        timeout=600
                    )
                finally:
                    progress_task.cancel()

                try:
                    await status.delete()
                except:
                    pass

                result_file = io.BytesIO(result_bytes)
                result_file.name = "video.mp4"
                result_file.seek(0)

                try:
                    await context.bot.send_video(chat_id=update.effective_chat.id, video=result_file)
                except:
                    result_file.seek(0)
                    await context.bot.send_document(chat_id=update.effective_chat.id, document=result_file)
                
            # ================= MUSIC =================
            elif mode == "music":
                premium = is_premium(user)

                if not premium:
                    paid_music = user.get("paid_music", 0)
                    if paid_music <= 0:
                        keyboard = InlineKeyboardMarkup([
                            [InlineKeyboardButton("💳 Купить трек (100₽)", callback_data="buy_music")],
                            [InlineKeyboardButton("🍩 Premium", callback_data="buy_spb")]
                        ])
                        await msg.reply_text(
                            "🎵 Нужна оплата для генерации музыки",
                            reply_markup=keyboard
                        )
                        return

                    async with db_pool.acquire() as conn:
                        await conn.execute("""
                            UPDATE users SET paid_music = paid_music - 1 WHERE user_id=$1
                        """, user_id)
                        USER_CACHE.pop(user_id, None)

                cached_audio_url = await get_cached_music(prompt)
                chat_id = update.effective_chat.id

                if cached_audio_url:
                    try:
                        if status:
                            await status.delete()
                    except:
                        pass

                    async with aiohttp.ClientSession() as session:
                        async with session.get(cached_audio_url) as resp:
                            audio_bytes = await resp.read()

                    audio_file = io.BytesIO(audio_bytes)
                    ext = cached_audio_url.split(".")[-1]
                    audio_file.name = f"song.{ext}"
                    audio_file.seek(0)

                    try:
                        await context.bot.send_audio(chat_id=chat_id, audio=audio_file)
                    except:
                        audio_file.seek(0)
                        await context.bot.send_document(chat_id=chat_id, document=audio_file)

                else:

                    async def progress_updater():
                        pct = 0
                        last_text = ""
                        try:
                            while True:
                                await asyncio.sleep(3)
                                pct = min(pct + 2, 100)
                                new_text = f"🎵 Генерация музыки... {pct}%"
                                if new_text != last_text:
                                    try:
                                        await status.edit_text(new_text)
                                        last_text = new_text
                                    except:
                                        pass
                        except asyncio.CancelledError:
                            pass

                    progress_task = asyncio.create_task(progress_updater())

                    try:
                        result = await asyncio.wait_for(
                            fal_music_generate(prompt),
                            timeout=360
                        )
                    finally:
                        progress_task.cancel()

                    try:
                        if status:
                            await status.edit_text("✅ Готово 100%")
                            await status.delete()
                    except:
                        pass

                    async with aiohttp.ClientSession() as session:
                        async with session.get(result) as resp:
                            audio_bytes = await resp.read()

                    await save_music_cache(prompt, result)

                    audio_file = io.BytesIO(audio_bytes)
                    ext = result.split(".")[-1]
                    audio_file.name = f"song.{ext}"
                    audio_file.seek(0)

                    try:
                        await context.bot.send_audio(chat_id=chat_id, audio=audio_file)
                    except:
                        audio_file.seek(0)
                        await context.bot.send_document(chat_id=chat_id, document=audio_file)

        except Exception as e:
            logging.error(f"❌ HANDLE ERROR: {e}")

        # ===== 🔥 УНИВЕРСАЛЬНЫЙ ОТВЕТ ПОЛЬЗОВАТЕЛЮ =====
            if msg:
                try:
                    await msg.reply_text(
                        "⚠️ Не удалось сгенерировать результат.\n\n"
                        "💡 Возможные причины:\n"
                        "• промпт заблокирован системой безопасности\n"
                        "• слишком сложное или чувствительное описание\n"
                        "• модель не смогла обработать запрос\n\n"
                        "✨ Попробуйте изменить промпт:\n"
                        "• упростите описание\n"
                        "• уберите чувствительные слова\n"
                        "• используйте более общий стиль\n\n"
                        "📌 Пример:\n"
                        "`Пусть ест пончик и скажет : Всем привет`",
                        parse_mode="Markdown"
                    )
                except:
                    pass

        finally:
            if user_id in active_generations:
                active_generations.discard(user_id)

            unlock_user_generation(user_id)
                
            if user_id in user_locks and not user_locks[user_id].locked():
                user_locks.pop(user_id, None)
            logging.info(f"🧹 CLEANUP user {user_id}")
# ================== WORKERS ==================
async def image_worker():
    while True:
        job = await generation_queue_image.get()
        try:
            await handle_generation_job(job)
        finally:
            generation_queue_image.task_done()


async def video_worker():
    while True:
        job = await generation_queue_video.get()
        try:
            await handle_generation_job(job)
        finally:
            generation_queue_video.task_done()


async def music_worker():
    while True:
        job = await generation_queue_music.get()
        try:
            await handle_generation_job(job)
        finally:
            generation_queue_music.task_done()


# ================= START =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    ref_by = None

    if context.args:
        try:
            ref_by = int(context.args[0])
        except:
            pass

    db_user = await get_user(user.id)

    if not db_user:

        now = int(time.time())

        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO users (
                    user_id,
                    week_start,
                    accepted_terms,
                    ref_by,
                    created_at,
                    last_active
                )
                VALUES ($1, $2, 0, $3, $4, $5)
                """,
                user.id, now, ref_by, now, now
            )

        if ref_by and ref_by != user.id:

            async with db_pool.acquire() as conn:

                # ❗ Проверяем, не был ли уже реферал привязан
                existing = await conn.fetchval(
                    "SELECT ref_by FROM users WHERE user_id=$1",
                    user.id
                )

                if existing is None:

                    # ❗ Проверка: существует ли реферер
                    ref_exists = await conn.fetchval(
                        "SELECT 1 FROM users WHERE user_id=$1",
                        ref_by
                    )

                    if ref_exists:

                        # 🔥 анти-абуз: не даем бонус сразу, только фиксируем реферала
                        await conn.execute(
                            """
                            UPDATE users 
                            SET referrals = referrals + 1
                            WHERE user_id = $1
                            """,
                            ref_by
                        )

                        # 🔥 помечаем, что пользователь еще не дал награду
                        await conn.execute(
                            """
                            UPDATE users
                            SET ref_rewarded = 0
                            WHERE user_id = $1
                            """,
                            user.id
                        )

                        USER_CACHE.pop(user.id, None)

        db_user = await get_user(user.id)

    # 🔥 ОБНОВЛЯЕМ АКТИВНОСТЬ (ВАЖНО ДЛЯ /stats)
    await update_last_active(user.id)

    if db_user["accepted_terms"] == 0:

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

    await update.message.reply_text(
        "Наш бот дает вам возможность создать бесплатно свой мультфильм🦕\n"
        "с помощью Sora2, генерации с помощью NanoBanana2🍌, свою музыку и другие крутые функции\n"
        "╾━╤デ╦︻(•_- )Используйте МЕНЮ слева\n"
        "🐧 Sosai bot готов к генерации."
    )

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


# ================= PREMIUM COMMAND =================
async def premium(update: Update, context: ContextTypes.DEFAULT_TYPE):

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⭐ Купить за Stars", callback_data="buy_stars")
        ],
        [
            InlineKeyboardButton("💳 Оплатить через СПБ", callback_data="buy_spb")
        ]
    ])

    await update.message.reply_text(
        "🍩 Пончик-статус Premium\n\n"
        "Что входит:\n"
        "🐳 50 генераций изображений\n"
        "🎬 5 видео / мультфильмов\n"
        "🎵 3 генераций музыки\n\n"
        "499 рублей через СПБ\n\n"
        "⏳ действует 30 дней\n\n"
        "Выберите способ оплаты:",
        reply_markup=keyboard
    )


async def ensure_premium_sync(user_id):
    async with db_pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT premium, premium_until FROM users WHERE user_id=$1",
            user_id
        )

        if not user:
            return False

        if user["premium"] == 1 and user["premium_until"] > int(time.time()):
            return True

        return False

# ================= PAYMENT SUCCESS =================
async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_user.id

    premium_until = int(time.time()) + (30 * 24 * 60 * 60)

    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE users 
            SET premium = 1,
                premium_until = $1
            WHERE user_id = $2
            """,
            premium_until, user_id
        )
        USER_CACHE.pop(user_id, None)

    await update.message.reply_text(
        "🍩 Оплата прошла успешно!\n\nPremium активирован на 30 дней 🚀"
    )

async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    await query.answer(ok=True)

# ================= IMPORTS =================
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, MessageHandler, filters
import logging

# ================= CALLBACK =================
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    try:
        await query.answer()
    except:
        pass

    data = query.data
    user_id = query.from_user.id

    # ================= SUPPORT REPLY =================
    if data.startswith("reply_"):

        target_user_id = int(data.split("_")[1])

        ADMIN_REPLY_STATE[user_id] = target_user_id

        await query.message.reply_text(
            f"✍️ Напишите ответ пользователю {target_user_id}"
        )
        return

    # ================= ADMIN (РАННИЙ ВЫХОД) =================
    if data == "reset_limits":
        if user_id not in ADMIN_IDS:
            await query.message.reply_text("❌ Нет доступа")
            return
        await reset_user_limits(user_id)
        await query.message.reply_text("♻️ Лимиты обнулены")
        return

       
    elif data == "check_sub":
        subscribed = await is_user_subscribed(context.bot, user_id)

        if subscribed:
            context.user_data["sub_checked"] = True  # 🔥 ВАЖНО

            await query.message.reply_text("✅ Подписка подтверждена!")

            if context.user_data.get("pending_video"):
                context.user_data.pop("pending_video")

                await query.message.reply_text(
                    "🎬 Теперь отправьте промпт или фото — генерация доступна"
                )
        else:
            await query.message.reply_text("❌ Вы не подписаны на канал")
        return

    # ================= Обработка кнопок =================
    if data == "buy_stars":
        YOOKASSA_PROVIDER_TOKEN = os.environ.get("YOOKASSA_PROVIDER_TOKEN")
        await query.message.reply_invoice(
            title="🍩 Пончик Premium",
            description="30 дней Premium доступа",
            payload="premium_donut",
            provider_token=YOOKASSA_PROVIDER_TOKEN,
            currency="RUB",
            prices=[{"label": "Premium", "amount": 50000}],
            need_name=True,
            need_phone_number=True,
            need_email=True,
            need_shipping_address=False,
            is_flexible=False
        )
        return

    elif data == "buy_spb":
        pay_url = await create_payment(user_id)

        await query.message.reply_text(
            f"💳 Оплата через ЮKassa\n\n"
            f"Перейдите и оплатите:\n{pay_url}"
        )
        return

    elif data == "finish":
        context.user_data.clear()
        await query.message.reply_text("✅ Генерация завершена.")
        return

    elif data == "restart":
        context.user_data["input_images"] = []
        context.user_data["last_images"] = []
        await query.message.reply_text("🔄 Начните заново. Используйте /photo")
        return

    elif data == "accept_terms":
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE users 
                SET accepted_terms = 1 
                WHERE user_id = $1
                """,
                user_id
            )
            USER_CACHE.pop(user_id, None)
        await query.edit_message_text("✅ Условия приняты.")
        return

    # ================= MODE / MODEL =================
    elif data in ["model_banana1", "model_banana2"]:
        context.user_data["model"] = "banana1" if data == "model_banana1" else "banana2"
        context.user_data["mode"] = "image"
        context.user_data["cartoon_style"] = None
        context.user_data["last_prompt"] = None
        context.user_data["last_images"] = []

        await query.message.reply_text(
            f"✅ Выбрана модель: {'🍌 Nano Banana 1' if data=='model_banana1' else '🍌 Nano Banana 2'}\n\n"
            "✏ Сначала напишите текст или отправьте 1-4 фото"
        )
        return

    # ================= SIZE =================
    elif data == "size_square":
        context.user_data["size"] = SIZE_CONFIG["square"]
        await query.message.reply_text("⬜ Разрешение 1:1 выбрано")
        return

    elif data == "size_wide":
        context.user_data["size"] = SIZE_CONFIG["wide"]
        await query.message.reply_text("🖥 Разрешение 16:9 выбрано")
        return

    elif data == "size_phone":
        context.user_data["size"] = SIZE_CONFIG["phone"]
        await query.message.reply_text("📱 Вертикальное разрешение выбрано")
        return

    # ================= MUSIC =================
    elif data == "suno_hit":
        context.user_data["mode"] = "music"
        context.user_data["cartoon_style"] = None
        context.user_data["last_prompt"] = None
        context.user_data["last_images"] = []

        await query.message.reply_text(
            "🎵 Напишите тему песни и выберите жанр\n\n"
            "Пример:\n"
            "Сделай веселую песню в жанре Рэп про мою сестру Вику"
        )
        return

    elif data == "buy_video":
        url = await create_payment(user_id, "video", PRICE_VIDEO)
        await query.message.reply_text(f"💳 Оплата видео:\n{url}")
        return

    elif data == "buy_music":
        url = await create_payment(user_id, "music", PRICE_MUSIC)
        await query.message.reply_text(f"💳 Оплата музыки:\n{url}")
        return

    # ================= CARTOON STYLES =================
    elif data.startswith("cartoon_"):
        style_key = data.replace("cartoon_", "")
        if style_key not in CARTOON_STYLES:
            return

        context.user_data["cartoon_style"] = CARTOON_STYLES[style_key]
        context.user_data["mode"] = "cartoon"

        if "model" not in context.user_data:
            context.user_data["model"] = "banana2"

        context.user_data["last_prompt"] = None
        context.user_data["last_images"] = []
        context.user_data["input_images"] = []

        await query.message.reply_text(
            f"🎬 Стиль выбран: {style_key.upper()}\n\n"
            "✏ Теперь отправьте:\n"
            "• текст\n"
            "или\n"
            "• фото + текст\n\n"
            "После этого бот создаст мультфильм 🎥"
        )
        return

    # ================= REPEAT (ОСТАВЛЯЕМ) =================
    elif data == "repeat":
        prompt = context.user_data.get("last_prompt")
        images = context.user_data.get("last_images", [])
        mode = context.user_data.get("mode", "image")

        if user_id in active_generations:
            await query.message.reply_text("⏳ Ваша генерация уже в очереди или выполняется")
            return

        if get_queue_position() > 1000:
            await query.reply_text("🚫 Сервер перегружен, попробуйте позже")
            return

        position = get_queue_position() + 1
        status = await query.message.reply_text(
            f"⏳ Вы в очереди: {position}\n🦕 Шедевр создается, немного надо подождать..."
        )

        queue_map = {
            "image": generation_queue_image,
            "video": generation_queue_video,
            "cartoon": generation_queue_video,
            "music": generation_queue_music
        }

        await queue_map.get(mode, generation_queue_image).put({
            "update": update,
            "context": context,
            "prompt": prompt,
            "size": context.user_data.get("size", "1024x1024"),
            "model": context.user_data.get("model", "banana2"),
            "images": images,
            "user_id": user_id,
            "mode": mode,
            "status": status
        })
        return

    # ================= CLEAR OLD STYLES =================
    if context.user_data.get("mode") not in ["cartoon"]:
        context.user_data["cartoon_style"] = None
        

# ================= PHOTO / TEXT HANDLERS =================
def get_queue_position():
    video_cartoon_queue = generation_queue_video.qsize()
    return generation_queue_image.qsize() + video_cartoon_queue + generation_queue_music.qsize()

async def use_paid_video(user_id):
    async with db_pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT paid_video FROM users WHERE user_id=$1",
            user_id
        )

        if user and user["paid_video"] > 0:
            await conn.execute(
                "UPDATE users SET paid_video = paid_video - 1 WHERE user_id=$1",
                user_id
            )
            USER_CACHE.pop(user_id, None)
            return True

        return False


async def update_last_active(user_id):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET last_active=$1 WHERE user_id=$2",
            int(time.time()), user_id
        )


async def support_reply_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data

    if not data.startswith("reply_"):
        return

    target_user_id = int(data.split("_")[1])

    context.user_data["reply_to_user"] = target_user_id

    await query.message.reply_text(
        f"✍️ Напишите ответ пользователю {target_user_id}"
    )

async def sos_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["support_mode"] = True

    await update.message.reply_text(
        "🆘 Напишите ваше сообщение, и я передам его в поддержку."
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    
    user_id = update.effective_user.id
    ONLINE_USERS[user_id] = time.time()
    mode = context.user_data.get("mode")

    if mode not in ["video", "cartoon", "image"]:
        await update.message.reply_text(
            "⚠ Сначала выберите режим генерации: /photo, /video, /cartoon или /suno"
        )
        return

    if mode in ["image", "cartoon"] and "model" not in context.user_data:
        context.user_data["model"] = "banana2"  # ✅ Автоустановка модели для мультфильмов
        # await update.message.reply_text("⚠ Сначала выберите модель\nВведите /photo")
        # return

    if "input_images" not in context.user_data:
        context.user_data["input_images"] = []

    if len(context.user_data["input_images"]) >= MAX_INPUT_IMAGES:
        context.user_data["input_images"] = []

    photo = update.message.photo[-1]

    if photo.file_size and photo.file_size > 5_000_000:
        await update.message.reply_text("⚠️ Фото слишком большое (макс 5MB)")
        return

    file = await photo.get_file()
    image_bytes = bytes(await file.download_as_bytearray())
    context.user_data["input_images"].append(image_bytes)

    caption = update.message.caption

    if caption:
        context.user_data["last_prompt"] = caption
        context.user_data["last_images"] = context.user_data["input_images"]

        if user_id in active_generations:
            await update.message.reply_text("⏳ Ваша генерация уже в очереди или выполняется")
            return

        if get_queue_position() > 1000:
            await update.reply_text("🚫 Сервер перегружен, попробуйте позже")
            return

        position = get_queue_position() + 1
        status = await update.message.reply_text(
            f"⏳ Вы в очереди: {position}\n🦕 Генерация создается, немного надо подождать..."
        )

        allowed, msg = check_user_generation_limit(user_id)
        if not allowed:
            await update.message.reply_text(msg or "⚠️ Лимит генераций достигнут")
            return

        lock_user_generation(user_id)

        queue_map = {
            "image": generation_queue_image,
            "video": generation_queue_video,
            "cartoon": generation_queue_video,
            "music": generation_queue_music
        }

        

        await queue_map.get(mode, generation_queue_image).put({
            "update": update,
            "context": context,
            "prompt": caption,
            "size": context.user_data.get("size", "1024x1024"),
            "model": context.user_data.get("model", "banana2"),
            "images": context.user_data["input_images"],
            "user_id": user_id,
            "mode": mode,
            "status": status
        })
        

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    
    user_id = update.effective_user.id
    ONLINE_USERS[user_id] = time.time()
    message = update.message

    if not message:
        return

    # ===== ADMIN REPLY SUPPORT =====
    if user_id in ADMIN_IDS:
        if message.reply_to_message:

            original_msg_id = message.reply_to_message.message_id
            target_user_id = SUPPORT_REPLY_MAP.get(original_msg_id)

            if target_user_id:
                try:
                    await context.bot.send_message(
                        target_user_id,
                        f"💬 Ответ поддержки:\n\n{message.text}"
                    )

                    await message.reply_text("✅ Ответ отправлен")
                except:
                    await message.reply_text("❌ Ошибка отправки")

                return

    # ===== ADMIN BUTTON REPLY =====
    if user_id in ADMIN_IDS and ADMIN_REPLY_STATE.get(user_id):

        target_user_id = ADMIN_REPLY_STATE.get(user_id)

        try:
            await context.bot.send_message(
                target_user_id,
                f"💬 Ответ поддержки:\n\n{message.text}"
            )

            await message.reply_text("✅ Ответ отправлен")

        except:
            await message.reply_text("❌ Ошибка отправки")

        ADMIN_REPLY_STATE.pop(user_id, None)
        return

    # ===== SUPPORT =====
    if context.user_data.get("support_mode"):

        user = update.effective_user
        text = message.text

        msg = f"""
🆘 <b>Новое обращение</b>

👤 ID: <code>{user.id}</code>
📛 @{user.username or "нет"}
👀 {user.first_name}

💬 {text}
"""

        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    admin_id,
                    msg,
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("💬 Ответить", callback_data=f"reply_{user.id}")]
                    ])
                )

            except:
                pass

        await message.reply_text("✅ Сообщение отправлено в поддержку")
        context.user_data["support_mode"] = False
        return

    prompt = message.text if message.text else None
    images = context.user_data.get("input_images", [])
    mode = context.user_data.get("mode")

    if user_id in active_generations:
        await message.reply_text("⏳ Ваша генерация уже выполняется")
        return

    count = user_generation_count.get(user_id, 0)
    if count >= MAX_USER_GENERATIONS:
        await message.reply_text("⚠️ Подождите завершения текущих генераций")
        return

    if not check_rate_limit(user_id):
        await message.reply_text("⏳ Не так быстро. Подождите 2 секунды.")
        return

    if prompt and len(prompt) > 800:
        await message.reply_text("⚠ Слишком длинный запрос.")
        return

    if context.user_data.get("chat_mode"):
        try:
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}]
            )
            answer = response.choices[0].message.content
            await message.reply_text(answer)
        except Exception as e:
            logging.error(f"ChatGPT error: {e}")
            await message.reply_text("⚠ Ошибка ChatGPT. Попробуйте позже.")
        return

    if mode in ["video", "cartoon"] and not prompt and not images:
        await message.reply_text("⚠ Пожалуйста, отправьте текст или фото для генерации видео/мультфильма")
        return

    user = await get_user(user_id)
    premium_active = await ensure_premium_sync(user_id)
    
    if not user:
        await message.reply_text("⚠ Ошибка пользователя. Напишите /start")
        return

    await reset_week_if_needed(user)

    queue_map = {
        "image": generation_queue_image,
        "video": generation_queue_video,
        "cartoon": generation_queue_video,
        "music": generation_queue_music
    }

    context.user_data["last_prompt"] = prompt
    context.user_data["last_images"] = images
    
    if get_queue_position() > 1000:
        await message.reply_text("🚫 Сервер перегружен, попробуйте позже")
        return

    position = get_queue_position() + 1
    status = await message.reply_text(
        f"⏳ Вы в очереди: {position}\n🦕 Генерация создается, немного надо подождать..."
    )
    lock_user_generation(user_id)

    await queue_map.get(mode, generation_queue_image).put({
        "update": update,
        "context": context,
        "prompt": prompt,
        "size": context.user_data.get("size", "1024x1024"),
        "model": context.user_data.get("model", "banana2"),
        "images": images,
        "user_id": user_id,
        "mode": mode,
        "status": status
    })
    

# ================= COMMANDS =================

async def video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["mode"] = "video"
    context.user_data["cartoon_style"] = None  # ✅ сброс старого стиля мультфильма
    await update.message.reply_text(
        "🎬 Режим видео включён (Sora2)\n\n"
        "Отправьте промпт или фото + текст."
    )


async def cartoon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎬 Pixar", callback_data="cartoon_pixar"),
            InlineKeyboardButton("🏰 Disney", callback_data="cartoon_disney")
        ],
        [
            InlineKeyboardButton("🇯🇵 АНИМЕ", callback_data="cartoon_anime"),
            InlineKeyboardButton("🎥 DreamWorks", callback_data="cartoon_dreamworks")
        ],
        [
            InlineKeyboardButton("🌿 ГИБЛИ", callback_data="cartoon_ghibli"),
            InlineKeyboardButton("🟡 СИМПСОНЫ", callback_data="cartoon_simpsons")
        ],
        [
            InlineKeyboardButton("🧪 РИКиМОРТИ", callback_data="cartoon_rickmorty")
        ]
    ])
    
    context.user_data.clear()
    context.user_data["mode"] = "cartoon"
    await update.message.reply_text(
        "🐉 Выберите стиль мультфильма:",
        reply_markup=keyboard
    )


async def uu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["chat_mode"] = True
    await update.message.reply_text(
        "🤖 Режим ChatGPT включен\n\n"
        "Напишите чем вам помочь."
    )

async def account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user

    user = await get_user(tg_user.id)

    if not user:
        await update.message.reply_text("⚠ Ошибка пользователя. Напишите /start")
        return

    # ===== ТЕКУЩИЕ СЧЁТЧИКИ =====
    used_images = user["image_count"]
    used_videos = user["video_count"]
    used_music = user.get("music_count", 0)

    bonus = user["bonus_images"]

    paid_video = user.get("paid_video", 0)
    paid_music = user.get("paid_music", 0)

    # ===== ПРЕМИУМ =====
    premium_active = (
        user.get("premium", 0) == 1
        and user.get("premium_until", 0) > int(time.time())
    )

    premium_status = "🍩 Пончик-Премиум ЕСТЬ" if premium_active else "❌ Премиум нет"

    # ===== РАСЧЁТ ЛИМИТОВ =====
    if premium_active:
        remaining_images = PREMIUM_IMAGE_LIMIT - used_images
        remaining_videos = PREMIUM_VIDEO_LIMIT - used_videos
        remaining_music = PREMIUM_MUSIC_LIMIT - used_music
    else:
        remaining_images = FREE_LIMIT + bonus - used_images
        remaining_videos = FREE_VIDEO_LIMIT - used_videos
        remaining_music = 0  # бесплатно музыки нет

    # защита от минусов
    remaining_images = max(0, remaining_images)
    remaining_videos = max(0, remaining_videos)
    remaining_music = max(0, remaining_music)

    keyboard = None

    # ===== КНОПКА АДМИНА =====
    if tg_user.id in ADMIN_IDS:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("♻️ Обнулить лимиты", callback_data="reset_limits")]
        ])

    # ===== ТЕКСТ ПРОФИЛЯ =====
    profile_text = (
        f"👤 Профиль\n\n"
        f"🆔 ID: {tg_user.id}\n"
        f"👤 Username: @{tg_user.username}\n\n"

        f"📸 Изображения осталось: {remaining_images}\n"
        f"🎬 Видео осталось: {remaining_videos}\n"
        f"🎵 Музыка осталось: {remaining_music}\n\n"

        f"💳 Куплено видео: {paid_video}\n"
        f"💳 Куплено музыки: {paid_music}\n\n"

        f"🎁 Бонусы: {bonus}\n"
        f"👥 Рефералов: {user['referrals']}\n\n"

        f"🍩 Статус: {premium_status}"
    )

    await update.message.reply_text(
        profile_text,
        reply_markup=keyboard,
        parse_mode=None
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
        
        [InlineKeyboardButton("🍌 Nano Banana 1", callback_data="model_banana1")],
        [InlineKeyboardButton("🍌 Nano Banana 2", callback_data="model_banana2")],
        [
            InlineKeyboardButton("⬜ 1:1", callback_data="size_square"),
            InlineKeyboardButton("🖥 16:9", callback_data="size_wide"),
            InlineKeyboardButton("📱 Phone", callback_data="size_phone")
        ]
    ])

    await update.message.reply_text(
        "👾 Выберите модель и размер изображения:",
        reply_markup=keyboard
    )

async def suno(update: Update, context: ContextTypes.DEFAULT_TYPE):

    # очищаем старые режимы
    context.user_data.clear()

    # устанавливаем режим музыки
    context.user_data["mode"] = "music"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎵 Создать хит", callback_data="suno_hit")]
    ])

    await update.message.reply_text(
        "🎶 Suno AI генератор песен\n\n"
        "Нажмите кнопку ниже чтобы создать хит",
        reply_markup=keyboard
    )



# ================= REGISTER =================


app = ApplicationBuilder().token(TG_TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("account", account))
app.add_handler(CommandHandler("premium", premium))    
app.add_handler(CommandHandler("ref", ref))
app.add_handler(CommandHandler("photo", photo))
app.add_handler(CommandHandler("video", video))
app.add_handler(CommandHandler("cartoon", cartoon))
app.add_handler(CommandHandler("suno", suno))
app.add_handler(CommandHandler("uu", uu))
app.add_handler(CommandHandler("finish", finish))
app.add_handler(CommandHandler("restart", restart))
app.add_handler(CommandHandler("stats", stats_handler))
app.add_handler(CommandHandler("sos", sos_handler))


app.add_handler(CallbackQueryHandler(button_handler))
app.add_handler(PreCheckoutQueryHandler(pre_checkout))
app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))



async def set_commands(app):

    await app.bot.set_my_commands([
        BotCommand("start", "Запуск"),
        BotCommand("account", "Профиль"),
        BotCommand("premium", "🍩 Купить Premium"),
        BotCommand("ref", "Реферальная программа"),
        BotCommand("photo", "Создать изображение"),
        BotCommand("video", "Создать видео"),
        BotCommand("cartoon", "Сделать мультфильм"),
        BotCommand("suno", "Создать песню"),
        BotCommand("uu", "Лимит генераций"),
        BotCommand("finish", "Закончить генерацию"),
        BotCommand("restart", "Перезапустить")
    ])





# ================= POST INIT =================

async def post_init(app):
    global generation_queue_image, generation_queue_video, generation_queue_music

    await init_db()

    global generation_queue
    generation_queue = asyncio.Queue(maxsize=10000)

    # ================= ОПТИМАЛЬНЫЕ ВОРКЕРЫ =================
    IMAGE_WORKERS = 8
    VIDEO_WORKERS = 3
    MUSIC_WORKERS = 2

    for _ in range(IMAGE_WORKERS):
        asyncio.create_task(image_worker())

    for _ in range(VIDEO_WORKERS):
        asyncio.create_task(video_worker())

    for _ in range(MUSIC_WORKERS):
        asyncio.create_task(music_worker())

    # ================= ФОНОВЫЕ ЗАДАЧИ =================
    asyncio.create_task(user_cache_cleaner())
    asyncio.create_task(cache_cleaner())

    # ================= КОМАНДЫ =================
    await set_commands(app)

    logging.info("✅ PostgreSQL подключен и бот готов")

    if not db_pool:
        raise Exception("❌ DB не инициализирована")


app.post_init = post_init




if __name__ == "__main__":
    print("🚀 Бот запущен")
    app.run_polling()
    
    
