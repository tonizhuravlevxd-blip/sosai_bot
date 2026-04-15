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
import traceback

from translations import TEXTS

from telegram.ext import PreCheckoutQueryHandler

from yookassa import Configuration, Payment

Configuration.account_id = os.getenv("YOOKASSA_SHOP_ID")
Configuration.secret_key = os.getenv("YOOKASSA_SECRET_KEY")

async def t(user_id, key, **kwargs):
    user = await get_user(user_id)
    lang = user.get("language", "ru")

    text = TEXTS.get(key, {}).get(lang, key)

    return text.format(**kwargs)


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

FREE_CHAT_LIMIT = 4
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
no_mode_cooldown = {}
NO_MODE_COOLDOWN_TIME = 10

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

def check_global_spam(user_id):

    now = time.time()

    # ===== ЕСЛИ ЗАБЛОКАН =====
    blocked_until = user_blocked_until.get(user_id, 0)
    if now < blocked_until:
        return False

    # ===== ЛОГ СООБЩЕНИЙ =====
    log = user_message_log.get(user_id, [])

    # очищаем старые
    log = [t for t in log if now - t < SPAM_WINDOW]

    log.append(now)
    user_message_log[user_id] = log

    # ===== ЕСЛИ СПАМ =====
    if len(log) > SPAM_LIMIT:
        user_blocked_until[user_id] = now + SPAM_BLOCK_TIME
        user_message_log[user_id] = []
        return False

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
            chat_count INTEGER DEFAULT 0,

            paid_video INTEGER DEFAULT 0,
            paid_music INTEGER DEFAULT 0,

            -- 🔥 НОВОЕ
            premium_images INTEGER DEFAULT 0,
            premium_videos INTEGER DEFAULT 0,
            premium_music INTEGER DEFAULT 0,

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

        await conn.execute("""
        ALTER TABLE users ADD COLUMN IF NOT EXISTS chat_count INTEGER DEFAULT 0
        """)

        await conn.execute("""
        ALTER TABLE users ADD COLUMN IF NOT EXISTS language TEXT DEFAULT 'ru'
        """)

        # 🔥 ДОБАВЛЯЕМ PREMIUM ЛИМИТЫ (SAFE)
        await conn.execute("""
        ALTER TABLE users ADD COLUMN IF NOT EXISTS premium_images INTEGER DEFAULT 0
        """)

        await conn.execute("""
        ALTER TABLE users ADD COLUMN IF NOT EXISTS premium_videos INTEGER DEFAULT 0
        """)

        await conn.execute("""
        ALTER TABLE users ADD COLUMN IF NOT EXISTS premium_music INTEGER DEFAULT 0
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


import asyncio
import aiohttp
import time
import json
import logging


async def fal_music_generate(prompt, duration=30, max_wait=300):
    """
    Priority mode:
    1. Sonauto (main, waits, monitors queue)
    2. Ace-Step (fallback if stuck/slow)
    """

    prompt = clean_prompt(prompt)

    headers = {
        "Authorization": f"Key {FAL_KEY}",
        "Content-Type": "application/json"
    }

    sonauto = {
        "name": "sonauto",
        "url": "https://queue.fal.run/sonauto/v2/text-to-music",
        "payload": {
            "prompt": prompt,
            "duration": duration,
            "output_format": "mp3"
        }
    }

    ace = {
        "name": "ace-step",
        "url": "https://queue.fal.run/fal-ai/minimax-music/v2.6",
        "payload": {
            "prompt": prompt,
            "duration": duration,
            "output_format": "mp3"
        }
    }

    async def run_model(model, detect_stuck=False):
        try:
            async with aiohttp.ClientSession() as session:

                # ===== CREATE JOB =====
                async with session.post(model["url"], json=model["payload"], headers=headers) as r:
                    text = await r.text()

                    if r.status not in (200, 202):
                        raise Exception(f"{model['name']} create failed: {r.status} {text}")

                    data = json.loads(text)

                status_url = data.get("status_url")
                result_url = data.get("response_url")

                if not status_url:
                    raise Exception(f"{model['name']} no status_url")

                start_time = time.time()

                # для детекта "залипания"
                last_position = None
                last_change_time = time.time()

                while True:
                    await asyncio.sleep(2)

                    if time.time() - start_time > max_wait:
                        raise Exception(f"{model['name']} timeout")

                    # ===== STATUS =====
                    async with session.get(status_url, headers=headers) as r:
                        if r.status != 200:
                            continue
                        status_data = json.loads(await r.text())

                    status = status_data.get("status")
                    queue_pos = status_data.get("queue_position")

                    logging.info(f"🎵 {model['name']} STATUS={status} POS={queue_pos}")

                    # ===== DETECT STUCK =====
                    if detect_stuck:
                        if queue_pos is not None:
                            if queue_pos == last_position:
                                if time.time() - last_change_time > 20:
                                    raise Exception("QUEUE_STUCK")
                            else:
                                last_position = queue_pos
                                last_change_time = time.time()

                    # ===== RESULT =====
                    result = None
                    if result_url:
                        async with session.get(result_url, headers=headers) as r:
                            if r.status == 200:
                                try:
                                    result = json.loads(await r.text())
                                except:
                                    pass

                    if result:
                        status = "COMPLETED"

                    if status == "COMPLETED" and result:

                        audio = result.get("audio")

                        if isinstance(audio, dict):
                            audio_url = audio.get("url")
                        elif isinstance(audio, list) and audio:
                            audio_url = audio[0].get("url")
                        else:
                            audio_url = (
                                result.get("audio_url")
                                or result.get("url")
                            )

                        if audio_url:
                            return {
                                "model": model["name"],
                                "url": audio_url
                            }

                        raise Exception(f"{model['name']} no audio url")

                    if status == "FAILED":
                        raise Exception(f"{model['name']} failed")

        except Exception as e:
            return {
                "model": model["name"],
                "error": str(e)
            }

    # ================= PRIORITY FLOW =================

    # 🚀 1. запускаем Sona
    sona_task = asyncio.create_task(run_model(sonauto, detect_stuck=True))

    try:
        # ⏳ даем ей шанс
        done, pending = await asyncio.wait(
            [sona_task],
            timeout=25
        )

        if done:
            result = list(done)[0].result()
            if "url" in result:
                logging.info(f"🎧 SONA WIN: {result['url']}")
                return result["url"]

    except Exception as e:
        if "QUEUE_STUCK" not in str(e):
            logging.warning(f"Sona issue: {e}")

    # 🔄 2. fallback
    logging.info("⚡ Switching to ACE fallback")

    ace_task = asyncio.create_task(run_model(ace))

    done, pending = await asyncio.wait(
        [sona_task, ace_task],
        return_when=asyncio.FIRST_COMPLETED
    )

    # ❌ отменяем оставшиеся
    for task in pending:
        task.cancel()

    for task in done:
        result = task.result()
        if "url" in result:
            logging.info(f"🎧 WINNER: {result['model']} -> {result['url']}")
            return result["url"]

    raise Exception("All models failed")


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

# ================= FAL VIDEO REMIX =================
async def fal_video_remix(video_bytes, prompt, images=None):

    import base64

    prompt = clean_prompt(prompt)

    headers = {
        "Authorization": f"Key {FAL_KEY}"
    }

    timeout = aiohttp.ClientTimeout(total=600)

    async with aiohttp.ClientSession(timeout=timeout) as session:

        # 🔥 1. FIX: NO UPLOAD API (убираем источник 502)
        video_b64 = base64.b64encode(video_bytes).decode("utf-8")
        video_url = f"data:video/mp4;base64,{video_b64}"

        # 🔥 2. REMIX REQUEST
        payload = {
            "prompt": prompt,
            "video_url": video_url,
            "image_urls": images[:4] if images else []
        }

        async with session.post(
            "https://queue.fal.run/fal-ai/kling-video/o1/standard/video-to-video/edit",
            json=payload,
            headers={**headers, "Content-Type": "application/json"}
        ) as resp:

            text = await resp.text()

            try:
                data = await resp.json()
            except:
                raise Exception(f"Kling response not JSON: {text}")

            request_id = data.get("request_id")

            if not request_id:
                raise Exception(f"No request_id: {data}")

        # 🔥 3. STATUS CHECK
        status_url = f"https://queue.fal.run/fal-ai/kling-video/requests/{request_id}/status"
        result_url = f"https://queue.fal.run/fal-ai/kling-video/requests/{request_id}"

        for _ in range(300):

            async with session.get(status_url, headers=headers) as s:

                if s.status != 200:
                    await asyncio.sleep(2)
                    continue

                status = await s.json()
                state = status.get("status")

                if state == "COMPLETED":

                    async with session.get(result_url, headers=headers) as r:

                        result = await r.json()

                        video_url = result.get("video", {}).get("url")

                        if not video_url:
                            raise Exception(f"Bad result: {result}")

                        async with session.get(video_url) as v:
                            return await v.read()

                if state == "FAILED":
                    raise Exception(f"Kling failed: {status}")

            await asyncio.sleep(2)

        raise Exception("Remix timeout")
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

# ================= HANDLE IMAGE (REMIX) =================
async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_user.id

    if context.user_data.get("mode") != "remix":
        return

    if not context.user_data.get("input_video_ready"):
        await update.message.reply_text(
            "⚠️ Сначала отправьте видео"
        )
        return

    photo = update.message.photo[-1]

    file = await context.bot.get_file(photo.file_id)
    image_bytes = await file.download_as_bytearray()

    if "input_images" not in context.user_data:
        context.user_data["input_images"] = []

    context.user_data["input_images"].append(image_bytes)

    await update.message.reply_text(
        "🖼 Фото добавлено как референс\n"
        "Теперь отправьте текст ✏"
    )

# ================= HANDLE VIDEO =================
async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):

    
    import logging
    import tempfile
    import subprocess
    import os

    user_id = update.effective_user.id
    ONLINE_USERS[user_id] = time.time()

    logging.info(f"🎬 HANDLE VIDEO START user={user_id}")

    if not check_global_spam(user_id):
        logging.warning(f"🚫 SPAM BLOCK user={user_id}")
        return

    if user_id in active_generations:
        logging.warning(f"⏳ ALREADY GENERATING user={user_id}")
        await update.message.reply_text("⏳ Дождитесь завершения текущей генерации")
        return

    mode = context.user_data.get("mode")
    logging.info(f"📌 MODE={mode} user={user_id}")

    if mode != "remix":
        logging.info(f"❌ WRONG MODE user={user_id}")
        return

    video = update.message.video

    if not video:
        logging.warning(f"❌ NO VIDEO user={user_id}")
        return

    # ================= VALIDATION =================

    # ===== ПРОВЕРКА РАЗМЕРА =====
    if not video.width or not video.height:
        await update.message.reply_text(
            "⚠️ Не удалось определить размер видео\n"
            "Пожалуйста отправьте видео"
        )
        return

    original_w = video.width
    original_h = video.height

    logging.info(f"📐 ORIGINAL SIZE {original_w}x{original_h}")

    # ===== ПРОВЕРКА ФОРМАТА =====
    if video.mime_type not in ["video/mp4", "video/quicktime"]:
        await update.message.reply_text(
            "⚠️ Поддерживается только формат MP4\n\n"
            "📌 Пожалуйста отправьте .mp4 видео"
        )
        return

    # ===== ПРОВЕРКА РАЗМЕРА ФАЙЛА =====
    if video.file_size and video.file_size > 200_000_000:
        logging.warning(f"⚠️ VIDEO TOO BIG user={user_id} size={video.file_size}")
        await update.message.reply_text("⚠️ Видео слишком большое (макс 200MB)")
        return

    try:
        logging.info(f"⬇️ DOWNLOADING VIDEO user={user_id}")

        file = await context.bot.get_file(video.file_id)
        video_bytes = await file.download_as_bytearray()

        if not video_bytes:
            logging.error(f"❌ EMPTY VIDEO BYTES user={user_id}")
            await update.message.reply_text("⚠️ Не удалось загрузить видео")
            return

        video_bytes = bytes(video_bytes)

        logging.info(f"✅ VIDEO DOWNLOADED user={user_id} size={len(video_bytes)}")

        # ================= АВТО РЕСАЙЗ ДО 720x720 =================
        try:

            # 🔥 если уже 720x720 — не трогаем
            if original_w == 720 and original_h == 720:
                logging.info(f"⚡ SKIP RESIZE (already 720x720) user={user_id}")
                processed_bytes = video_bytes

            else:
                logging.info(f"🔄 RESIZE START user={user_id}")

                with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as input_tmp:
                    input_tmp.write(video_bytes)
                    input_path = input_tmp.name

                output_path = input_path.replace(".mp4", "_720.mp4")

                command = [
                    "ffmpeg",
                    "-i", input_path,
                    "-vf",
                    "scale=720:720:force_original_aspect_ratio=increase,crop=720:720",
                    "-c:v", "libx264",
                    "-preset", "fast",
                    "-crf", "23",
                    "-c:a", "aac",
                    "-b:a", "128k",
                    "-y",
                    output_path
                ]

                subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                with open(output_path, "rb") as f:
                    processed_bytes = f.read()

                os.remove(input_path)
                os.remove(output_path)

                logging.info(f"✅ RESIZED TO 720x720 user={user_id}")

        except Exception as e:
            logging.error(f"❌ RESIZE ERROR user={user_id}: {e}")
            await update.message.reply_text("⚠️ Ошибка обработки видео")
            return

        # ================= СОХРАНЯЕМ =================
        context.user_data["input_video"] = processed_bytes
        context.user_data["input_video_bytes"] = processed_bytes
        context.user_data["input_video_ready"] = True

        context.user_data["input_video_url"] = None
        context.user_data["last_video_error"] = None

        if "input_images" not in context.user_data:
            context.user_data["input_images"] = []

        logging.info(f"🧠 CONTEXT SAVED user={user_id}")

        await update.message.reply_text(
            "✅ Видео загружено\n\n"
            "📌 Что дальше:\n"
            "1. Отправьте ✏ текст (что сделать с видео)\n"
            "2. Можете добавить 🖼 фото (референс)\n\n"
            "Пример:\n"
            "👉 Замени авто на видео на авто из фото\n"
            "👉 Сделай как TikTok тренд\n\n"
            "🚀 Kling ждет ваш запрос"
        )

    except Exception as e:

        error_trace = traceback.format_exc()

        logging.error(f"❌ HANDLE VIDEO ERROR user={user_id}: {e}")
        logging.error(error_trace)

        context.user_data["last_video_error"] = str(e)

        await update.message.reply_text(
            f"⚠️ Ошибка загрузки видео:\n{e}"
        )
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

async def safe_edit(message, text, **kwargs):
    try:
        if getattr(message, "text", None) == text:
            return
        await message.edit_text(text, **kwargs)
    except Exception as e:
        if "message is not modified" in str(e):
            return
        logging.warning(f"EDIT ERROR: {e}")


# ================== UNIVERSAL HANDLER (FIXED FINAL) ==================
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
# ===== GLOBAL ANTISPAM =====
user_message_log = {}
user_blocked_until = {}

SPAM_WINDOW = 10        # секунд
SPAM_LIMIT = 6         # сообщений за окно
SPAM_BLOCK_TIME = 30   # бан (сек)
ADMIN_REPLY_STATE = {}
SUPPORT_REPLY_MAP = {}
ONLINE_USERS = {}
ONLINE_TTL = 300
active_generations = set()
GLOBAL_RATE_LIMIT = asyncio.Semaphore(300)
GLOBAL_SEMAPHORE = asyncio.Semaphore(300)

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
            if mode in ["video", "cartoon", "remix"]:
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

                                # ✅ ТОЛЬКО ПРОВЕРКА (без списания)
                                if user["image_count"] >= limit:
                                    await msg.reply_text("⚠️ Лимит изображений исчерпан")
                                    return

                            # ================= VIDEO / CARTOON =================
                            elif mode in ["video", "cartoon", "remix"]:

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

                                # ===== ТОЛЬКО ПРОВЕРКА (БЕЗ СПИСАНИЯ) =====

                                if paid_video > 0:
                                    logging.info(f"💰 PAID VIDEO AVAILABLE user={user_id}")

                                elif premium:
                                    logging.info(f"🍩 USING PREMIUM LIMIT user={user_id}")

                                    if video_count >= PREMIUM_VIDEO_LIMIT:
                                        await msg.reply_text("⚠️ Лимит видео исчерпан (Premium)")
                                        return

                                else:
                                    logging.info(f"🆓 USING FREE LIMIT user={user_id}")

                                    if video_count >= FREE_VIDEO_LIMIT:
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
                "remix": "<pre>🔥 Создание тренда (Remix)... 0%</pre>",
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
                elif mode in ["cartoon", "video", "remix"] and cartoon_style:
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
                                await safe_edit(status, text, parse_mode="HTML")
                            except:
                                pass
                            i += 1
                            await asyncio.sleep(1.5)
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

                # ✅ СПИСАНИЕ ТОЛЬКО ПОСЛЕ УСПЕШНОЙ ГЕНЕРАЦИИ
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        """
                        UPDATE users
                        SET image_count = image_count + 1
                        WHERE user_id=$1
                        """,
                        user_id
                    )

                USER_CACHE.pop(user_id, None)
                                
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
                        "֎ Подготовка модели...",
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
                                    await safe_edit(status, new_text)
                                    last_text = new_text
                                except:
                                    pass

                            await asyncio.sleep(random.randint(5, 10))
                            idx += 1

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

                # ✅ СПИСАНИЕ ТОЛЬКО ПОСЛЕ УСПЕШНОЙ ОТПРАВКИ
                async with db_pool.acquire() as conn:

                    user = await conn.fetchrow(
                        "SELECT paid_video, video_count FROM users WHERE user_id=$1",
                        user_id
                    )

                    paid_video = user.get("paid_video") or 0
                    video_count = user.get("video_count") or 0

                    if paid_video > 0:
                        await conn.execute(
                            """
                            UPDATE users
                            SET paid_video = paid_video - 1
                            WHERE user_id=$1
                            """,
                            user_id
                        )
                    else:
                        await conn.execute(
                            """
                            UPDATE users
                            SET video_count = video_count + 1
                            WHERE user_id=$1
                            """,
                            user_id
                        )

                USER_CACHE.pop(user_id, None)


            # ================= REMIX =================
            if mode == "remix":

                import random
                import tempfile
                import subprocess
                                             
                async def progress_updater():
                    steps = [
                        "👨🏻‍🏫 Анализ видео...",
                        "🌐 Ищем материал в интернете...",
                        "🎬 Подготовка KLING модели...",
                        "✂️ Настраиваем видео по размеру...",
                        "🎥 Обработка видео...",
                        "👽 В кадр попал пришелец...",
                        "🚀 Убираем лишнее из кадра...",
                        "🎥 Обработка видео...",
                        "✨ Применение эффектов...",
                        "🧠 AI думает как TikTok...",
                        "🦄 Добавляем магию...",
                        "🌠 Падающая звезда...",
                        "⭐ Загадайте желание...",
                        "🎞 Рендеринг кадров...",
                        "🦕 Почти готово...",
                        "🔌 Кто то выключил свет...",
                        "🐘 Слоник задел шнур питания...",
                        "💾 Проверка сохранения видео...",
                        "🏁 Видео почти готово...",
                        "🤩 Я посмотрел, это шедевр...",
                        "🥳 Осталось немного...",
                        "🦥 О смотри кого нашел...",
                        "(∩｀-´)⊃━☆ﾟ.*･｡ﾟ Ускоряю процесс...",
                        "🍿 Думаю уже финал...",
                        "📦 Финальная сборка..."
                    ]

                    idx = 0
                    last_text = ""

                    try:
                        while True:
                            new_text = steps[idx % len(steps)]

                            if new_text != last_text:
                                try:
                                    await safe_edit(status, new_text)
                                    last_text = new_text
                                except:
                                    pass

                            await asyncio.sleep(random.randint(4, 8))
                            idx += 1

                    except asyncio.CancelledError:
                        pass


                video_bytes = job.get("video")
                images = job.get("images", [])

                # 🔥 HARD FALLBACK
                if not video_bytes:
                    video_bytes = (
                        context.user_data.get("input_video")
                        or context.user_data.get("input_video_bytes")
                    )

                if not images:
                    images = context.user_data.get("input_images", [])

                if not video_bytes:
                    if msg:
                        await msg.reply_text("⚠️ Сначала отправьте видео")
                    return

                # ================= AUTO RESIZE 720x720 =================
                try:
                    with tempfile.NamedTemporaryFile(suffix=".mp4") as inp, \
                         tempfile.NamedTemporaryFile(suffix=".mp4") as out:

                        inp.write(video_bytes)
                        inp.flush()

                        cmd = [
                            "ffmpeg",
                            "-y",
                            "-i", inp.name,
                            "-vf", "scale=720:720:force_original_aspect_ratio=decrease,pad=720:720:(ow-iw)/2:(oh-ih)/2",
                            "-c:v", "libx264",
                            "-preset", "veryfast",
                            "-crf", "23",
                            "-pix_fmt", "yuv420p",
                            "-movflags", "+faststart",
                            "-c:a", "aac",
                            "-b:a", "128k",
                            out.name
                        ]

                        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                        with open(out.name, "rb") as f:
                            video_bytes = f.read()

                except Exception as e:
                    print("⚠️ RESIZE ERROR:", e)

                # 🔥 Kling limit
                if len(images) > 4:
                    images = images[:4]

                # 🔥 prompt fix
                if images and "@Image" not in prompt:
                    prompt = prompt + " Use @Image1 for style reference"

                # 🔥 FIX: convert images bytes -> base64 urls
                image_urls = []

                if images:
                    for img in images:
                        try:
                            img_b64 = base64.b64encode(img).decode("utf-8")
                            image_urls.append(f"data:image/jpeg;base64,{img_b64}")
                        except Exception as e:
                            print("⚠️ IMAGE BASE64 ERROR:", e)

                progress_task = asyncio.create_task(progress_updater())

                result_bytes = None
                video_url = None

                try:

                    # ================= REQUEST =================
                    video_b64 = base64.b64encode(video_bytes).decode("utf-8")
                    video_url = f"data:video/mp4;base64,{video_b64}"

                    async with aiohttp.ClientSession() as session:

                        async with session.post(
                            "https://queue.fal.run/fal-ai/kling-video/o1/standard/video-to-video/edit",
                            json={
                                "prompt": prompt,
                                "video_url": video_url,
                                "image_urls": image_urls
                            },
                            headers={
                                "Authorization": f"Key {FAL_KEY}",
                                "Content-Type": "application/json"
                            }
                        ) as resp:

                            text = await resp.text()

                            try:
                                data = await resp.json()
                            except:
                                raise Exception(f"Kling not JSON: {text}")

                            request_id = data.get("request_id")

                            if not request_id:
                                raise Exception(f"No request_id: {data}")


                    # ================= POLL =================
                    status_url = f"https://queue.fal.run/fal-ai/kling-video/requests/{request_id}/status"
                    result_url = f"https://queue.fal.run/fal-ai/kling-video/requests/{request_id}"

                    async with aiohttp.ClientSession() as session:

                        for _ in range(300):

                            async with session.get(status_url) as s:

                                status_json = await s.json()
                                state = status_json.get("status")

                                if state == "COMPLETED":

                                    async with session.get(result_url) as r:
                                        result = await r.json()

                                        video_file_url = result.get("video", {}).get("url")

                                        if not video_file_url:
                                            raise Exception(f"Bad result: {result}")

                                        async with session.get(video_file_url) as v:
                                            result_bytes = await v.read()

                                    break

                                if state == "FAILED":
                                    raise Exception(f"FAL failed: {status_json}")

                            await asyncio.sleep(2)

                except Exception as e:

                    err = traceback.format_exc()

                    try:
                        await safe_edit(status, f"⚠️ Ошибка remix:\n{e}")
                    except:
                        pass

                    print("❌ REMIX ERROR:", err)
                    return

                finally:
                    progress_task.cancel()
                    try:
                        await progress_task
                    except:
                        pass


                try:
                    if status:
                        await status.delete()
                except:
                    pass


                if not result_bytes:
                    if msg:
                        await msg.reply_text("⚠️ FAL не вернул видео")
                    return


                result_file = io.BytesIO(result_bytes)
                result_file.name = "remix.mp4"
                result_file.seek(0)

                try:
                    await context.bot.send_video(
                        chat_id=update.effective_chat.id,
                        video=result_file,
                        supports_streaming=True,
                        filename="video.mp4"
                    )
                except:
                    result_file.seek(0)
                    await context.bot.send_document(
                        chat_id=update.effective_chat.id,
                        document=result_file
                    )

                           # ✅ СПИСАНИЕ ПОСЛЕ УСПЕХА
                async with db_pool.acquire() as conn:

                    if paid_video > 0:
                        await conn.execute(
                            "UPDATE users SET paid_video = paid_video - 1 WHERE user_id=$1",
                            user_id
                        )
                    else:
                        await conn.execute(
                            "UPDATE users SET video_count = video_count + 1 WHERE user_id=$1",
                            user_id
                        )

                    USER_CACHE.pop(user_id, None)
                
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

                        # ✅ СПИСАНИЕ ПОСЛЕ УСПЕХА
                        if not premium:
                            async with db_pool.acquire() as conn:
                                await conn.execute(
                                    "UPDATE users SET paid_music = paid_music - 1 WHERE user_id=$1",
                                    user_id
                                )
                                USER_CACHE.pop(user_id, None)

                    except:
                        audio_file.seek(0)
                        await context.bot.send_document(chat_id=chat_id, document=audio_file)

                        # ✅ СПИСАНИЕ ПОСЛЕ УСПЕХА (fallback)
                        if not premium:
                            async with db_pool.acquire() as conn:
                                await conn.execute(
                                    "UPDATE users SET paid_music = paid_music - 1 WHERE user_id=$1",
                                    user_id
                                )
                                USER_CACHE.pop(user_id, None)

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
                                        await safe_edit(status, new_text)
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

                        # ✅ СПИСАНИЕ ПОСЛЕ УСПЕХА
                        if not premium:
                            async with db_pool.acquire() as conn:
                                await conn.execute(
                                    "UPDATE users SET paid_music = paid_music - 1 WHERE user_id=$1",
                                    user_id
                                )
                                USER_CACHE.pop(user_id, None)

                    except:
                        audio_file.seek(0)
                        await context.bot.send_document(chat_id=chat_id, document=audio_file)

                        # ✅ СПИСАНИЕ ПОСЛЕ УСПЕХА (fallback)
                        if not premium:
                            async with db_pool.acquire() as conn:
                                await conn.execute(
                                    "UPDATE users SET paid_music = paid_music - 1 WHERE user_id=$1",
                                    user_id
                                )
                                USER_CACHE.pop(user_id, None)

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
            try:
                # ================= 🔥 ACTIVE GENERATIONS =================
                if user_id in active_generations:
                    active_generations.discard(user_id)

                # ================= 🔓 UNLOCK =================
                try:
                    unlock_user_generation(user_id)
                except Exception as e:
                    logging.error(f"UNLOCK ERROR: {e}")

                # ================= 🧹 USER DATA CLEAN =================
                try:
                    if context and hasattr(context, "user_data"):

                        # 🔥 удаляем ТЯЖЕЛЫЕ объекты (самое важное)
                        context.user_data.pop("input_video", None)
                        context.user_data.pop("input_video_bytes", None)
                        context.user_data.pop("input_images", None)

                        # 🔥 чистим кэш генерации
                        context.user_data.pop("last_images", None)
                        context.user_data.pop("last_prompt", None)

                        # 🔥 чистим временные флаги
                        context.user_data.pop("pending_video", None)
                        context.user_data.pop("input_video_ready", None)

                except Exception as e:
                    logging.error(f"USER_DATA CLEAN ERROR: {e}")

                # ================= 🔐 LOCK CLEAN (АНТИ УТЕЧКА) =================
                try:
                    # ❗ ВАЖНО: только удаляем ссылку, unlock уже выше
                    if user_id in user_locks:
                        user_locks.pop(user_id, None)

                except Exception as e:
                    logging.error(f"LOCK CLEAN ERROR: {e}")

                # ================= 🧠 GC HINT (для больших видео) =================
                try:
                    import gc
                    gc.collect()
                except:
                    pass

                logging.info(f"🧹 CLEANUP user {user_id}")

            except Exception as e:
                logging.error(f"FINAL CLEANUP ERROR: {e}")
# ================== WORKERS ==================
async def image_worker():
    while True:
        try:
            job = await generation_queue_image.get()

            try:
                await handle_generation_job(job)
            except Exception as e:
                logging.error(f"❌ IMAGE WORKER ERROR: {e}")

            finally:
                generation_queue_image.task_done()

        except Exception as e:
            logging.critical(f"💀 IMAGE WORKER CRASH: {e}")
            await asyncio.sleep(1)


async def video_worker():
    while True:
        try:
            job = await generation_queue_video.get()

            try:
                await handle_generation_job(job)
            except Exception as e:
                logging.error(f"❌ VIDEO WORKER ERROR: {e}")

            finally:
                generation_queue_video.task_done()

        except Exception as e:
            logging.critical(f"💀 VIDEO WORKER CRASH: {e}")
            await asyncio.sleep(1)


async def music_worker():
    while True:
        try:
            job = await generation_queue_music.get()

            try:
                # 🔥 защита от зависания генерации
                await asyncio.wait_for(
                    handle_generation_job(job),
                    timeout=420
                )

            except asyncio.TimeoutError:
                logging.error("⏰ MUSIC TIMEOUT (job завис)")

            except Exception as e:
                logging.error(f"❌ MUSIC WORKER ERROR: {e}")

            finally:
                generation_queue_music.task_done()

        except Exception as e:
            logging.critical(f"💀 MUSIC WORKER CRASH: {e}")
            await asyncio.sleep(1)

async def worker_watchdog():

    while True:
        await asyncio.sleep(10)

        if generation_queue_image.qsize() > 0:
            logging.warning("⚠️ Проверка image workers")

        if generation_queue_video.qsize() > 0:
            logging.warning("⚠️ Проверка video workers")

        if generation_queue_music.qsize() > 0:
            logging.warning("⚠️ Проверка music workers")


# ================= START =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ================= 🧹 RESET CONTEXT =================
    try:
        context.user_data.clear()
    except:
        pass
        
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
        await t(update.effective_user.id, "start")
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
        "🐢 20 генераций изображений\n"
        "🎬 5 видео / мультфильмов\n"
        "🎵 3 генераций музыки\n\n"
        "499 рублей через СПБ\n\n"
        "⏳ действует 30 дней\n\n"
        "Выберите способ оплаты:",
        reply_markup=keyboard
    )



# ================= PAYMENT SUCCESS =================
async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_user.id
    payment = update.message.successful_payment

    if not payment:
        return

    payload = payment.invoice_payload
    currency = payment.currency  # ⭐ XTR = Stars, RUB = СПБ / YooKassa

    try:

        # ================= ⭐ TELEGRAM STARS =================
        if currency == "XTR":

            if payload == "premium_stars":

                now = int(time.time())
                add_time = 30 * 24 * 60 * 60

                async with db_pool.acquire() as conn:

                    user = await conn.fetchrow(
                        "SELECT premium_until FROM users WHERE user_id=$1",
                        user_id
                    )

                    if user and user["premium_until"] and user["premium_until"] > now:
                        premium_until = user["premium_until"] + add_time
                    else:
                        premium_until = now + add_time

                    await conn.execute(
                        """
                        UPDATE users 
                        SET premium = 1,
                            premium_until = $1,
                            premium_images = premium_images + 20,
                            premium_videos = premium_videos + 5,
                            premium_music = premium_music + 3
                        WHERE user_id = $2
                        """,
                        premium_until, user_id
                    )

                USER_CACHE.pop(user_id, None)

                await update.message.reply_text(
                    "⭐ Оплата через Stars прошла успешно!\n\n"
                    "🍩 Premium активирован на 30 дней 🚀"
                )
                return

        # ================= 💳 СПБ / ЮKASSA =================
        else:

            if payload == "premium_donut":

                now = int(time.time())
                add_time = 30 * 24 * 60 * 60

                async with db_pool.acquire() as conn:

                    user = await conn.fetchrow(
                        "SELECT premium_until FROM users WHERE user_id=$1",
                        user_id
                    )

                    if user and user["premium_until"] and user["premium_until"] > now:
                        premium_until = user["premium_until"] + add_time
                    else:
                        premium_until = now + add_time

                    await conn.execute(
                        """
                        UPDATE users 
                        SET premium = 1,
                            premium_until = $1,
                            premium_images = premium_images + 20,
                            premium_videos = premium_videos + 5,
                            premium_music = premium_music + 3
                        WHERE user_id = $2
                        """,
                        premium_until, user_id
                    )

                USER_CACHE.pop(user_id, None)

                await update.message.reply_text(
                    "💳 Оплата прошла успешно!\n\n"
                    "🍩 Premium активирован на 30 дней 🚀"
                )
                return

        # ================= ❌ НЕИЗВЕСТНЫЙ PAYLOAD =================
        logging.warning(f"UNKNOWN PAYMENT: {payload} | {currency}")

        await update.message.reply_text(
            "⚠️ Платёж получен, но не распознан. Напишите в поддержку."
        )

    except Exception as e:

        logging.error(f"PAYMENT ERROR: {e}")

        try:
            await update.message.reply_text(
                "⚠️ Ошибка при активации. Напишите в поддержку."
            )
        except:
            pass

async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query

    try:
        await query.answer(ok=True)
    except Exception as e:
        logging.error(f"❌ PRECHECKOUT ERROR: {e}")

# ================= IMPORTS =================
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice
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

        # ================= LANGUAGE =================
    if data.startswith("lang_"):

        lang = data.split("_")[1]

        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET language=$1 WHERE user_id=$2",
                lang, user_id
            )

        USER_CACHE.pop(user_id, None)

        if lang == "ru":
            text = "✅ Язык переключен на русский"
        else:
            text = "✅ Language switched to English"

        await query.message.reply_text(text)
        return

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

    # ================= ADMIN POST =================
    if data == "admin_post":
        if user_id not in ADMIN_IDS:
            await query.message.reply_text("❌ Нет доступа")
            return

        context.user_data["admin_post_mode"] = True

        await query.message.reply_text(
            "📢 Режим поста включен\n\n"
            "✍️ Напишите сообщение — оно отправится ВСЕМ пользователям"
        )
        return
       
    elif data == "check_sub":
        subscribed = await is_user_subscribed(context.bot, user_id)

        if subscribed:
            context.user_data["sub_checked"] = True  # 🔥 ВАЖНО

            await query.message.reply_text("✅ Подписка подтверждена!")

            # ================= REMIX MODE =================
            if context.user_data.get("mode") == "remix" or context.user_data.get("pending_video"):

                context.user_data.pop("pending_video", None)

                context.user_data["mode"] = "remix"
                context.user_data["input_images"] = []
                context.user_data["input_video"] = None
                context.user_data["input_video_ready"] = False

                await query.message.reply_text(
                    "🧝🦸 Режим Kling не активирован!\n\n"
                    "☝ Нажмите кнопку ✳️ Сделать замену (KLING)\n"
                    
                )
                return

            # ================= ОСТАЛЬНЫЕ РЕЖИМЫ =================
            if context.user_data.get("pending_video"):
                context.user_data.pop("pending_video", None)

                await query.message.reply_text(
                    "🎬 Теперь отправьте промпт или фото — генерация доступна"
                )

        else:
            await query.message.reply_text(await t(user_id, "not_subscribed"))
        return

    # ================= Обработка кнопок =================
    if data == "buy_stars":
        await query.message.reply_invoice(
            title="🍩 Пончик Premium",
            description="30 дней Premium доступа",
            payload="premium_stars",
            provider_token="",  # ⭐ ОБЯЗАТЕЛЬНО ПУСТОЙ ДЛЯ TELEGRAM STARS
            currency="XTR",  # ⭐ ВАЛЮТА TELEGRAM STARS
            prices=[
                LabeledPrice(label="Premium", amount=500)  # ⭐ 500 Stars
            ],
            need_name=False,
            need_phone_number=False,
            need_email=False,
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

    elif data == "video_remix":

        subscribed = await is_user_subscribed(context.bot, user_id)

        if not subscribed:
            context.user_data["pending_video"] = True

            await query.message.reply_text(
                "📢 Перед использованием Kling нужно подписаться 👇",
                reply_markup=get_subscribe_keyboard()
            )
            return

        # ===== ЕСЛИ ПОДПИСАН =====
        context.user_data.clear()

        context.user_data["sub_checked"] = True
        context.user_data["mode"] = "remix"
        context.user_data["input_images"] = []
        context.user_data["input_video"] = None
        context.user_data["input_video_ready"] = False

        await query.message.reply_text(
            "🧝🦸 Режим Kling Remix\n\n"
            "Отправьте:\n"
            "1. 🎥 Только Видео сначало\n"
            
            "Видео автоматически подстроится под Kling,поэтому надо подождать 👇\n"
        )
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

    elif data == "psychologist_mode":

        context.user_data.clear()

        # Включаем режим психолога
        context.user_data["chat_mode"] = True
        context.user_data["system_prompt"] = (
            "Ты профессиональный психолог. Отвечай спокойно, поддерживающе, "
            "помогай человеку разобраться в эмоциях, не осуждай, задавай мягкие вопросы."
        )

        # Сбрасываем режим генерации
        context.user_data["mode"] = None
        context.user_data["last_prompt"] = None
        context.user_data["last_images"] = []
        context.user_data["input_images"] = []

        await query.message.reply_text(
            "🧠 Режим психолога активирован\n\n"
            "Можете написать, что вас беспокоит. Я постараюсь помочь вам эмоционально 💙"
        )
        return

    # ================= REPEAT (ИСПРАВЛЕН) =================
    elif data == "repeat":
        prompt = context.user_data.get("last_prompt")
        images = context.user_data.get("last_images", [])
        mode = context.user_data.get("mode", "image")

        if user_id in active_generations:
            await query.message.reply_text("⏳ Ваша генерация уже в очереди или выполняется")
            return

        # 🔥 ДОБАВЛЕНО: проверка лимита
        allowed, msg = check_user_generation_limit(user_id)
        if not allowed:
            await query.message.reply_text(msg)
            return

        # 🔥 ДОБАВЛЕНО: защита от перегрузки
        if get_queue_position() > 1000:
            await query.message.reply_text("🚫 Сервер перегружен, попробуйте позже")
            return

        # 🔥 ДОБАВЛЕНО: блокировка
        lock_user_generation(user_id)

        position = get_queue_position() + 1
        status = await query.message.reply_text(
            f"⏳ Вы в очереди: {position}\n🦕 Шедевр создается, немного надо подождать..."
        )

        queue_map = {
            "image": generation_queue_image,
            "video": generation_queue_video,
            "cartoon": generation_queue_video,
            "remix": generation_queue_video,
            "music": generation_queue_music
        }

        try:
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
        except:
            unlock_user_generation(user_id)
            raise

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


LAST_ACTIVE_CACHE = {}

async def update_last_active(user_id):
    now = time.time()

    last = LAST_ACTIVE_CACHE.get(user_id, 0)

    if now - last < 60:  # обновляем раз в минуту
        return

    LAST_ACTIVE_CACHE[user_id] = now

    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET last_active=$1 WHERE user_id=$2",
            int(now), user_id
        )


async def language(update: Update, context: ContextTypes.DEFAULT_TYPE):

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru"),
            InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")
        ]
    ])

    user_id = update.effective_user.id

    await update.message.reply_text(
        await t(user_id, "choose_language"),
        reply_markup=keyboard
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
        await t(update.effective_user.id, "sos")
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    
    user_id = update.effective_user.id
    ONLINE_USERS[user_id] = time.time()
   
    if not check_global_spam(user_id):
        return
        
    mode = context.user_data.get("mode")

    if mode not in ["video", "cartoon", "image", "remix"]:

        # 🔥 анти-флуд именно для этого сообщения
        now = time.time()
        last_warn = context.user_data.get("last_mode_warn", 0)

        if now - last_warn < 5:
            return  # ❌ молча игнорим флуд

        context.user_data["last_mode_warn"] = now

        await update.message.reply_text(
            "⚠ Сначала выберите режим генерации: /photo, /video, /cartoon, /remix или /suno"
        )
        return

    if mode in ["image", "cartoon"] and "model" not in context.user_data:
        context.user_data["model"] = "banana2"  # ✅ Автоустановка модели для мультфильмов

    if "input_images" not in context.user_data:
        context.user_data["input_images"] = []

    if len(context.user_data["input_images"]) >= MAX_INPUT_IMAGES:
        await update.message.reply_text(f"⚠ Можно загрузить максимум {MAX_INPUT_IMAGES} фото")
        return

    photo = update.message.photo[-1]

    if photo.file_size and photo.file_size > 5_000_000:
        await update.message.reply_text("⚠️ Фото слишком большое (макс 5MB)")
        return

    file = await photo.get_file()

    try:
        image_bytes = bytes(await file.download_as_bytearray())
    except:
        await update.message.reply_text("❌ Ошибка загрузки фото, попробуйте снова")
        return

    # 🔥 СОХРАНЯЕМ КАРТИНКИ (ВАЖНО ДЛЯ KLING)
    context.user_data.setdefault("input_images", []).append(image_bytes)

    caption = update.message.caption

    if caption:
        context.user_data["last_prompt"] = caption
        context.user_data["last_images"] = context.user_data["input_images"]

        if user_id in active_generations:
            await update.message.reply_text("⏳ Ваша генерация уже в очереди или выполняется")
            return

        if get_queue_position() > 1000:
            await update.message.reply_text("🚫 Сервер перегружен, попробуйте позже")
            return

        # 🔥 FIX: для remix проверяем видео
        if mode == "remix" and not context.user_data.get("input_video_ready"):
            await update.message.reply_text("⚠️ Для Remix сначала отправьте видео")
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
            "remix": generation_queue_video,
            "music": generation_queue_music
        }

        await queue_map.get(mode, generation_queue_image).put({
            "update": update,
            "context": context,
            "prompt": caption,
            "size": context.user_data.get("size", "1024x1024"),
            "model": context.user_data.get("model", "banana2"),
            "images": context.user_data.get("input_images", []),
            "video": context.user_data.get("input_video"),
            "video_ready": context.user_data.get("input_video_ready"),
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

        # ===== SUPPORT =====
    if context.user_data.get("support_mode"):

        if not message:
            return

        user = update.effective_user
        text = message.text

        # 🔥 защита от пустого сообщения
        if not text or not text.strip():
            await message.reply_text("⚠️ Напишите сообщение для поддержки")
            return

        # 🔥 анти-спам (чтобы не отправляли 10 раз подряд)
        now = time.time()
        last = context.user_data.get("last_support_msg", 0)

        if now - last < 5:
            return

        context.user_data["last_support_msg"] = now

        msg = f"""
🆘 <b>Новое обращение</b>

👤 ID: <code>{user.id}</code>
📛 @{user.username or "нет"}
👀 {user.first_name}

💬 {text}
"""

        sent = 0

        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=msg,
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("💬 Ответить", callback_data=f"reply_{user.id}")]
                    ])
                )
                sent += 1

            except Exception as e:
                logging.error(f"❌ SUPPORT SEND ERROR to {admin_id}: {e}")

        # 🔥 если никому не отправилось
        if sent == 0:
            await message.reply_text("⚠️ Ошибка отправки в поддержку. Попробуйте позже.")
            return

        await message.reply_text("✅ Сообщение отправлено в поддержку")

        # 🔥 безопасно выключаем режим
        context.user_data.pop("support_mode", None)

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

        # ===== ✅ ADMIN POST =====
    if user_id in ADMIN_IDS and context.user_data.get("admin_post_mode"):

        text = message.text

        await message.reply_text("🚀 Начинаю рассылку...")

        sent = 0
        failed = 0

        async with db_pool.acquire() as conn:
            users = await conn.fetch("SELECT user_id FROM users")

        for u in users:
            try:
                await context.bot.send_message(u["user_id"], text)
                sent += 1
                await asyncio.sleep(0.03)  # анти-флуд Telegram
            except:
                failed += 1

        await message.reply_text(
            f"✅ Рассылка завершена\n\n"
            f"📤 Отправлено: {sent}\n"
            f"❌ Ошибок: {failed}"
        )

        context.user_data["admin_post_mode"] = False
        return

        # ===== ✅ ГЛОБАЛЬНЫЙ АНТИ-СПАМ =====
    if not check_global_spam(user_id):
        return

    prompt = message.text if message.text else None
    images = context.user_data.get("input_images", [])
    mode = context.user_data.get("mode")

    if not mode and not context.user_data.get("chat_mode"):

        # 🔥 анти-флуд для "выберите режим"
        now = time.time()
        last_warn = context.user_data.get("last_mode_warn", 0)

        if now - last_warn < 5:
            return  # ❌ игнорим спам

        context.user_data["last_mode_warn"] = now

        await message.reply_text(await t(user_id, "choose_mode"))
        return


    if user_id in active_generations:
        await message.reply_text(await t(user_id, "already_generating"))
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

    # ===== FIX: получаем пользователя заранее =====
    user = await get_user(user_id)

    if not user:
        await message.reply_text("⚠ Ошибка пользователя. Напишите /start")
        return

    premium_active = (
        user["premium"] == 1 and user["premium_until"] > int(time.time())
    )

    if context.user_data.get("chat_mode"):

        chat_count = user.get("chat_count", 0)

        if not premium_active and chat_count >= FREE_CHAT_LIMIT:
            await message.reply_text(
                "⚠️ Бесплатный лимит ChatGPT (4 запроса) исчерпан.\n\nКупите Premium 👇",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🍩 Купить Premium", callback_data="buy_spb")]
                ])
            )
            return

        try:
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "system",
                        "content": context.user_data.get("system_prompt", "")
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ]
            )

            answer = response.choices[0].message.content

            async with db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE users SET chat_count = chat_count + 1 WHERE user_id=$1",
                    user_id
                )
                USER_CACHE.pop(user_id, None)

            await message.reply_text(answer)

        except Exception as e:
            logging.error(f"ChatGPT error: {e}")
            await message.reply_text("⚠ Ошибка ChatGPT. Попробуйте позже.")

        return

    if mode in ["video", "cartoon", "remix"] and not prompt:
        await message.reply_text("⚠ Пожалуйста, отправьте текст или фото для генерации видео/мультфильма")
        return

    # 🔥 FIX: проверка видео для remix
    if mode == "remix" and not context.user_data.get("input_video"):
        await message.reply_text("⚠️ Сначала отправьте видео для Remix")
        return
    
    await reset_week_if_needed(user)

    queue_map = {
        "image": generation_queue_image,
        "video": generation_queue_video,
        "cartoon": generation_queue_video,
        "remix": generation_queue_video,
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
        "images": context.user_data.get("input_images", images),
        "video": context.user_data.get("input_video"),
        "video_ready": context.user_data.get("input_video_ready"),
        "user_id": user_id,
        "mode": mode,
        "status": status
    })
    

# ================= COMMANDS =================

async def video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["mode"] = "video"
    context.user_data["cartoon_style"] = None  # ✅ сброс старого стиля мультфильма

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✳️ Сделать замену (KLING)", callback_data="video_remix")]
    ])

    await update.message.reply_text(
        "🎬 Режим видео (Sora2) включен\n\n"
        "Обычная генерация:\n"
        "• текст\n"
        "• фото + текст\n\n"
        "🎎 Или сделайте тренд замену:",
        reply_markup=keyboard
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

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🧠 Психолог", callback_data="psychologist_mode")]
    ])

    context.user_data["chat_mode"] = True
    context.user_data["mode"] = None
    context.user_data["system_prompt"] = ""

    await update.message.reply_text(
        "🤖 Режим ChatGPT включен\n\n"
        "Выберите режим:",
        reply_markup=keyboard
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
        premium_images = user.get("premium_images", 0)
        premium_videos = user.get("premium_videos", 0)
        premium_music = user.get("premium_music", 0)

        remaining_images = premium_images - used_images
        remaining_videos = premium_videos - used_videos
        remaining_music = premium_music - used_music
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
            [InlineKeyboardButton("♻️ Обнулить лимиты", callback_data="reset_limits")],
            [InlineKeyboardButton("📢 Сделать пост", callback_data="admin_post")]
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
        f"<b>Кончились генерации?</b>\n"
        f"<b>За каждого активного пользователя вы получаете +1 генерацию.</b>\n\n"
        f"<b>Просто скопируй ссылку и отправь другу 👇</b>\n\n"
        f"{link}",
        parse_mode="HTML"
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
app.add_handler(CommandHandler("language", language))
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
app.add_handler(MessageHandler(filters.VIDEO, handle_video))
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
    IMAGE_WORKERS = 20
    VIDEO_WORKERS = 6
    MUSIC_WORKERS = 4

    for _ in range(IMAGE_WORKERS):
        asyncio.create_task(image_worker())

    for _ in range(VIDEO_WORKERS):
        asyncio.create_task(video_worker())

    for _ in range(MUSIC_WORKERS):
        asyncio.create_task(music_worker())

    # ================= ФОНОВЫЕ ЗАДАЧИ =================
    asyncio.create_task(user_cache_cleaner())
    asyncio.create_task(cache_cleaner())
    asyncio.create_task(worker_watchdog())

    # ================= КОМАНДЫ =================
    await set_commands(app)

    logging.info("✅ PostgreSQL подключен и бот готов")

    if not db_pool:
        raise Exception("❌ DB не инициализирована")


app.post_init = post_init




if __name__ == "__main__":
    print("🚀 Бот запущен")
    app.run_polling()
    
    
