import os
import time
import asyncpg
import base64
import asyncio
import logging
import gc
import aiohttp
import json

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

TG_TOKEN = os.getenv("TG_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
FAL_KEY = os.getenv("FAL_KEY")
ADMIN_ID = 5523265642  # ← замени на свой Telegram ID

if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY не установлен")

client = OpenAI(api_key=OPENAI_API_KEY)

FREE_LIMIT = 5
FREE_VIDEO_LIMIT = 3
WEEK_SECONDS = 7 * 24 * 60 * 60
MAX_INPUT_IMAGES = 4
# ===== PREMIUM LIMITS =====

PREMIUM_IMAGE_LIMIT = 200
PREMIUM_VIDEO_LIMIT = 20
PREMIUM_MUSIC_LIMIT = 50

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

    active_generations.add(user_id)


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
    return (
        generation_queue_image.qsize() +
        generation_queue_video.qsize() +
        generation_queue_music.qsize()
    )


# ================= DATABASE =================

db_pool = None

async def init_db():
    global db_pool

    # Подключаемся к БД через DATABASE_URL
    db_pool = await asyncpg.create_pool(DATABASE_URL)

    # Создаем таблицы, если их нет
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
            premium_until BIGINT DEFAULT 0
        )
        """)
                # ✅ ДОБАВЬ ВОТ ЭТО
        await conn.execute("""
        ALTER TABLE users 
        ADD COLUMN IF NOT EXISTS music_count INTEGER DEFAULT 0
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS music_cache (
            prompt TEXT PRIMARY KEY,
            audio_url TEXT,
            created_at BIGINT
        )
        """)



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
    async with db_pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM users WHERE user_id=$1",
            user_id
        )


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
                week_start = $1
            WHERE user_id = $2
            """,
            int(time.time()),
            user_id
        )


# ================= PROMPT SAFETY FILTER =================

def clean_prompt(prompt: str):

    replacements = {

        # оружие
        "стреляет": "испускает свет",
        "стрельба": "энергетический эффект",
        "оружие": "устройство",
        "пистолет": "устройство",
        "бластер": "фантастическое устройство",
        "gun": "device",
        "shoot": "light effect",
        "weapon": "device",
        "blaster": "sci-fi device",

        # насилие
        "убивает": "побеждает",
        "kill": "defeat",
        "killing": "defeating",
        "blood": "red energy",
        "кровь": "красная энергия",

        # бренды
        "simpsons": "yellow cartoon family style",
        "pixar": "3d animated movie style",
        "disney": "fantasy animation style",
        "rick and morty": "crazy sci fi cartoon style",

        # опасные слова для sora
        "laser": "light beam",
        "attack": "action",
        "battle": "scene",
        "fight": "dynamic action",
        "explosion": "bright flash",
    }

    prompt = prompt.lower()

    for bad, good in replacements.items():
        prompt = prompt.replace(bad, good)

    # дополнительная защита
    blocked = [
        "kill",
        "murder",
        "blood",
        "weapon",
        "gun",
        "shoot"
    ]

    for word in blocked:
        prompt = prompt.replace(word, "")

    return prompt
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

async def fal_music_generate(prompt, duration=30, max_wait=900):
    """
    Генерация музыки через FAL с прогресс-логированием.
    
    :param prompt: текстовый промпт
    :param duration: длина трека в секундах
    :param max_wait: максимальное время ожидания генерации (в секундах)
    :return: URL с аудио
    """
    prompt = clean_prompt(prompt)  # ✅ очистка перед отправкой

    base_url = "https://queue.fal.run/fal-ai/lyria2"
    headers = {
        "Authorization": f"Key {FAL_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "prompt": prompt,
        "duration": duration
    }

    async with aiohttp.ClientSession() as session:
        # создаём генерацию
        async with session.post(base_url, json=payload, headers=headers) as r:
            text = await r.text()
            try:
                data = json.loads(text)
            except:
                raise Exception(f"Fal bad response: {text}")

        if "request_id" not in data:
            raise Exception(f"Fal music error: {data}")

        request_id = data["request_id"]
        status_url = f"{base_url}/requests/{request_id}/status"
        result_url = f"{base_url}/requests/{request_id}"

        start_time = time.time()
        last_status = None

        while True:
            await asyncio.sleep(2)

            async with session.get(status_url, headers=headers) as r:
                try:
                    status_data = await r.json()
                except Exception:
                    continue

            status = status_data.get("status")

            if status != last_status:
                logging.info(f"🎵 Music generation status: {status} for prompt: {prompt}")
                last_status = status

            # ===== УСПЕШНО =====
            if status == "COMPLETED":
                async with session.get(result_url, headers=headers) as r:
                    try:
                        result = await r.json()
                    except Exception:
                        raise Exception("Failed to parse FAL music result")

                # 🔥 ЛОГ ДЛЯ ДЕБАГА (очень важно)
                logging.info(f"🎵 FAL RAW RESULT: {result}")

                # ===== ВСЕ ВОЗМОЖНЫЕ ВАРИАНТЫ =====
                if "audio" in result and result["audio"]:
                    return result["audio"].get("url")

                if "audios" in result and result["audios"]:
                    return result["audios"][0].get("url")

                if "audio_url" in result:
                    return result["audio_url"]

                if "url" in result:
                    return result["url"]

                if "output" in result:
                    output = result["output"]

                    if isinstance(output, dict):
                        if "audio" in output:
                            return output["audio"].get("url")
                        if "audios" in output and output["audios"]:
                            return output["audios"][0].get("url")

                # ❌ если вообще ничего нет
                raise Exception(f"Fal returned no audio for prompt: {prompt} | result={result}")

            # ===== ОШИБКА =====
            if status == "FAILED":
                raise Exception(f"Fal music generation failed: {status_data}")

            # ===== ТАЙМАУТ =====
            if time.time() - start_time > max_wait:
                raise Exception(f"Music generation timeout (> {max_wait}s) for prompt: {prompt}")





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
generation_semaphore = asyncio.Semaphore(5)  # Ограничение параллельных генераций

# ================== UNIVERSAL HANDLER (FIXED) ==================
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

    msg = getattr(update, "message", None)
    if not msg and getattr(update, "callback_query", None):
        msg = update.callback_query.message

    if mode in ["cartoon", "video"] and not prompt and not images:
        await msg.reply_text("📸 Пожалуйста, отправьте текст или фото для генерации мультфильма/видео.")
        return

    async with generation_semaphore:
        try:
            user = await get_user(user_id)
            if not user:
                return
            await reset_week_if_needed(user)

            # ===== лимиты видео/мультфильма =====
            if mode in ["video", "cartoon"]:
                video_used = user.get("video_count", 0)
                if video_used >= FREE_VIDEO_LIMIT:
                    try:
                        if status:
                            await status.delete()
                    except:
                        pass
                    await msg.reply_text("🎬 Лимит видео/мультфильма на неделю исчерпан.")
                    return

            # ===== стили =====
            style = ""
            if model == "banana1":
                style = "cinematic lighting ultra realistic 8k"
            elif model == "banana2":
                style = "hyper detailed masterpiece artstation quality"

            cartoon_style = context.user_data.get("cartoon_style")

            # ==== Формирование prompt ====
            if prompt:
                if mode == "image" and style:
                    prompt = f"{style} {prompt}"
                elif mode in ["cartoon", "video"] and cartoon_style:
                    prompt = f"{cartoon_style}, {prompt}"

            if mode != "music" and prompt:
                prompt = clean_prompt(prompt)

            # ===== кеширование изображений =====
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

            images = images[:MAX_INPUT_IMAGES]

            # ================= MUSIC MODE =================
            if mode == "music" and prompt:
                chat = getattr(update, "effective_chat", None)
                if not chat:
                    active_generations.discard(user_id)
                    user_generation_count[user_id] = max(0, user_generation_count.get(user_id, 1) - 1)
                    return

                chat_id = chat.id

                cached_audio = await get_cached_music(prompt)
                if cached_audio:
                    try:
                        if status:
                            await status.delete()
                    except:
                        pass
                    await context.bot.send_audio(chat_id=chat_id, audio=cached_audio, title="Generated Song")
                    active_generations.discard(user_id)
                    return

                if status is None:
                    status = await msg.reply_text("🎵 Музыка генерируется… 0%")

                async def music_progress(msg, interval=1):
                    pct = 0
                    last_text = ""
                    try:
                        while True:
                            await asyncio.sleep(interval)
                            pct = min(pct + 10, 100)
                            new_text = f"🎵 Музыка генерируется… {pct}%"
                            if new_text != last_text:
                                try:
                                    await msg.edit_text(new_text)
                                    last_text = new_text
                                except:
                                    pass
                    except asyncio.CancelledError:
                        pass

                progress_task = asyncio.create_task(music_progress(status))
                audio_url = await fal_music_generate(prompt)
                progress_task.cancel()
                try:
                    await progress_task
                except asyncio.CancelledError:
                    pass

                try:
                    if status:
                        await status.delete()
                except:
                    pass

                await save_music_cache(prompt, audio_url)
                await context.bot.send_audio(chat_id=chat_id, audio=audio_url, title="Generated Song")

                async with db_pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE users SET music_count = COALESCE(music_count,0) + 1 WHERE user_id=$1",
                        user_id
                    )

                context.user_data["mode"] = None
                active_generations.discard(user_id)
                return

            # ================= VIDEO / CARTOON MODE =================
            if mode in ["video", "cartoon"]:
                # Сброс стиля мультфильма для обычного видео
                if mode == "video":
                    context.user_data["cartoon_style"] = None
                # ✅ FIX: защита от пустого prompt
                if not prompt and not images:
                    await msg.reply_text("⚠️ Пустой запрос")
                    return

                if status is None:
                    status = await msg.reply_text("🎬 Видео генерируется… 0%")

                async def video_progress(msg, interval=1):
                    pct = 0
                    last_text = ""
                    try:
                        while True:
                            await asyncio.sleep(interval)
                            pct = min(pct + 10, 100)
                            new_text = f"🎬 Видео генерируется… {pct}%"
                            if new_text != last_text:
                                try:
                                    await msg.edit_text(new_text)
                                    last_text = new_text
                                except:
                                    pass
                    except asyncio.CancelledError:
                        pass

                progress_task = asyncio.create_task(video_progress(status))

                video_bytes = None
                for attempt in range(3):
                    try:
                        video_bytes = await fal_video_generate(prompt, images)
                        break
                    except Exception as e:
                        logging.warning(f"Fal video generation failed, attempt {attempt+1}: {e}")
                        await asyncio.sleep(5)

                if video_bytes is None:
                    progress_task.cancel()
                    try:
                        await progress_task
                    except asyncio.CancelledError:
                        pass
                    await msg.reply_text("⚠️ Сервис генерации видео временно недоступен, попробуйте позже.")
                    return

                progress_task.cancel()
                try:
                    await progress_task
                except asyncio.CancelledError:
                    pass

                try:
                    if status:
                        await status.delete()
                except:
                    pass

                await msg.reply_video(video=video_bytes)
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE users SET video_count = video_count + 1 WHERE user_id=$1",
                        user_id
                    )

                context.user_data["input_images"] = []
                context.user_data["last_images"] = []
                return

            # ================= IMAGE MODE =================
            if prompt:
                # Сброс стиля мультфильма для обычного изображения
                if mode == "image":
                    context.user_data["cartoon_style"] = None
                chat = getattr(update, "effective_chat", None)
                if not chat:
                    return

                chat_id = chat.id

                upload_task = asyncio.create_task(fake_photo_upload(context.bot, chat_id))
                try:
                    image_bytes = await fal_generate(model, prompt, images)
                finally:
                    upload_task.cancel()
                    try:
                        await upload_task
                    except asyncio.CancelledError:
                        pass

                if cache_key:
                    generation_cache[cache_key] = {"image": image_bytes, "time": time.time()}

                try:
                    if status:
                        await status.delete()
                except:
                    pass

                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔁 Повторить", callback_data="repeat"),
                     InlineKeyboardButton("🆕 Начать заново", callback_data="restart")],
                    [InlineKeyboardButton("❌ Закончить", callback_data="finish")]
                ])

                await msg.reply_photo(photo=image_bytes, reply_markup=keyboard)
                context.user_data["mode"] = "image"

                async with db_pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE users SET image_count = image_count + 1 WHERE user_id = $1",
                        user_id
                    )

                context.user_data["input_images"] = []
                context.user_data["last_images"] = []

        except Exception as e:
            logging.error(f"Generation error: {e}")
            try:
                await msg.reply_text("⚠ Ошибка генерации. Попробуйте позже.")
            except:
                pass

        finally:
            active_generations.discard(user_id)
            user_generation_count[user_id] = max(0, user_generation_count.get(user_id, 1) - 1)
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

        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO users (user_id, week_start, accepted_terms, ref_by)
                VALUES ($1, $2, 0, $3)
                """,
                user.id, int(time.time()), ref_by
            )

        if ref_by and ref_by != user.id:

            async with db_pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE users 
                    SET referrals = referrals + 1,
                        bonus_images = bonus_images + 1
                    WHERE user_id = $1
                    """,
                    ref_by
                )

        db_user = await get_user(user.id)

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
        "🎨 200 генераций изображений\n"
        "🎬 20 видео / мультфильмов\n"
        "🎵 50 генераций музыки\n\n"
        "500 рублей через СПБ\n\n"
        "⏳ действует 30 дней\n\n"
        "Выберите способ оплаты:",
        reply_markup=keyboard
    )


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

    await update.message.reply_text(
        "🍩 Поздравляем!\n\n"
        "Вы получили Пончик-статус Premium на 30 дней!"
    )

# ================= IMPORTS =================
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, MessageHandler, filters
import logging

# ================= CALLBACK =================
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    # ================= ADMIN (РАННИЙ ВЫХОД) =================
    if data == "reset_limits":
        if user_id != ADMIN_ID:
            await query.message.reply_text("❌ Нет доступа")
            return
        await reset_user_limits(user_id)
        await query.message.reply_text("♻️ Лимиты обнулены")
        return

    # ================= Обработка кнопок =================
    if data == "buy_stars":
        await query.message.reply_invoice(
            title="🍩 Пончик Premium",
            description="30 дней Premium доступа",
            payload="premium_donut",
            provider_token="",
            currency="XTR",
            prices=[{"label": "Premium", "amount": 500}]
        )

    elif data == "buy_spb":
        spb_link = (
            "https://yoomoney.ru/quickpay/shop-widget?"
            "writer=SELLER_ID&"
            "targets=Premium+Donut&"
            "default-sum=500&"
            "button-text=11&"
            "payment-type-choice=on&"
            "quickpay-form=shop&"
            f"metadata[user_id]={user_id}&"
            f"successURL=https://t.me/{context.bot.username}"
        )
        await query.message.reply_text(
            f"💳 Оплата через СПБ (ЮKassa)\n\n"
            f"Нажмите на ссылку и оплатите:\n{spb_link}\n\n"
            f"После успешной оплаты ваш статус Premium активируется автоматически."
        )

    elif data == "finish":
        context.user_data.clear()
        await query.message.reply_text("✅ Генерация завершена.")

    elif data == "restart":
        context.user_data["input_images"] = []
        context.user_data["last_images"] = []
        await query.message.reply_text("🔄 Начните заново. Используйте /photo")

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
        await query.edit_message_text("✅ Условия приняты.")

    # ================= MODE / MODEL =================
    elif data == "model_banana1":
        context.user_data["model"] = "banana1"
        context.user_data["mode"] = "image"
        context.user_data["cartoon_style"] = None
        context.user_data["last_prompt"] = None
        context.user_data["last_images"] = []
        await query.message.reply_text(
            "✅ Выбрана модель:\n🍌 Nano Banana 1\n\n"
            "✏ Напишите текст или отправьте 1-4 фото"
        )

    elif data == "model_banana2":
        context.user_data["model"] = "banana2"
        context.user_data["mode"] = "image"
        context.user_data["cartoon_style"] = None
        context.user_data["last_prompt"] = None
        context.user_data["last_images"] = []
        await query.message.reply_text(
            "✅ Выбрана модель:\n🍌 Nano Banana 2\n\n"
            "✏ Напишите текст или отправьте 1-4 фото"
        )

    # ================= SIZE =================
    elif data == "size_square":
        context.user_data["size"] = SIZE_CONFIG["square"]
        await query.message.reply_text("⬜ Разрешение 1:1 выбрано")

    elif data == "size_wide":
        context.user_data["size"] = SIZE_CONFIG["wide"]
        await query.message.reply_text("🖥 Разрешение 16:9 выбрано")

    elif data == "size_phone":
        context.user_data["size"] = SIZE_CONFIG["phone"]
        await query.message.reply_text("📱 Вертикальное разрешение выбрано")

    # ================= MUSIC =================
    elif data == "suno_hit":
        context.user_data["mode"] = "music"
        context.user_data["cartoon_style"] = None
        context.user_data["last_prompt"] = None
        context.user_data["last_images"] = []
        await query.message.reply_text(
            "🎵 Напишите тему песни\n\n"
            "Пример:\n"
            "emotional pop song about lost love"
        )

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

    # ================= REPEAT =================
    elif data == "repeat":
        prompt = context.user_data.get("last_prompt")
        images = context.user_data.get("last_images", [])
        mode = context.user_data.get("mode", "image")

        if user_id in active_generations:
            await query.message.reply_text("⏳ Ваша генерация уже в очереди или выполняется")
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

        # ✅ Применяем clean_prompt для видео и мультфильмов
        if mode in ["video", "cartoon"] and prompt:
            prompt = clean_prompt(prompt)

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

    # ================= CLEAR OLD STYLES FOR NON-CARTOON =================
    else:
        # Если любая другая кнопка/действие и не мультфильм — сброс стиля
        if context.user_data.get("mode") not in ["cartoon"]:
            context.user_data["cartoon_style"] = None

    # ================= Проверка очереди и лимитов =================
    mode = context.user_data.get("mode", "image")
    queue_map = {
        "image": generation_queue_image,
        "video": generation_queue_video,
        "cartoon": generation_queue_video,
        "music": generation_queue_music
    }

    logging.info(f"📥 Enqueue job for user {user_id}, mode {mode}, queue size before: {queue_map.get(mode).qsize()}")

    if user_id in active_generations:
        logging.info(f"⚠ User {user_id} already has an active job, skipping enqueue")
    else:
        last_prompt = context.user_data.get("last_prompt")
        last_images = context.user_data.get("last_images", [])

        if mode in ["video", "cartoon"] and not last_prompt and not last_images:
            await query.message.reply_text("⚠️ Пустой запрос для видео/мультфильма")
            return

        allowed, msg = check_user_generation_limit(user_id)
        if not allowed:
            await query.message.reply_text(msg)
            return

        # ✅ Применяем clean_prompt перед отправкой в очередь
        safe_prompt = last_prompt
        if mode in ["video", "cartoon"] and last_prompt:
            safe_prompt = clean_prompt(last_prompt)

        job = {
            "update": update,
            "context": context,
            "prompt": safe_prompt,
            "size": context.user_data.get("size", "1024x1024"),
            "model": context.user_data.get("model", "banana2"),
            "images": last_images,
            "user_id": user_id,
            "mode": mode
        }

        lock_user_generation(user_id)
        await queue_map.get(mode, generation_queue_image).put(job)
        logging.info(f"✅ Job enqueued for user {user_id}, mode {mode}, queue size now: {queue_map.get(mode).qsize()}")
        active_generations.add(user_id)

# ================= PHOTO / TEXT HANDLERS =================
def get_queue_position():
    video_cartoon_queue = generation_queue_video.qsize()
    return generation_queue_image.qsize() + video_cartoon_queue + generation_queue_music.qsize()


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
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
        active_generations.add(user_id)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    message = update.message

    if not message:
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
    if not user:
        await message.reply_text("⚠ Ошибка пользователя. Напишите /start")
        return

    await reset_week_if_needed(user)

    used_images = user["image_count"]
    used_videos = user["video_count"]
    bonus = user["bonus_images"]

    if is_premium(user):
        remaining_images = PREMIUM_IMAGE_LIMIT - used_images
        video_limit = PREMIUM_VIDEO_LIMIT
    else:
        remaining_images = FREE_LIMIT + bonus - used_images
        video_limit = FREE_VIDEO_LIMIT

    if mode in ["video", "cartoon"] and used_videos >= video_limit:
        await message.reply_text(
            "🎬 Бесплатный лимит видео/мультфильма исчерпан.\nНовый будет доступен через неделю."
        )
        return

    queue_map = {
        "image": generation_queue_image,
        "video": generation_queue_video,
        "cartoon": generation_queue_video,
        "music": generation_queue_music
    }

    context.user_data["last_prompt"] = prompt
    context.user_data["last_images"] = images

    position = get_queue_position() + 1
    status = await message.reply_text(
        f"⏳ Вы в очереди: {position}\n🦕 Генерация создается, немного надо подождать..."
    )

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
    active_generations.add(user_id)

# ================= COMMANDS =================

async def video(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
            InlineKeyboardButton("🇯🇵 Anime", callback_data="cartoon_anime"),
            InlineKeyboardButton("🎥 DreamWorks", callback_data="cartoon_dreamworks")
        ],
        [
            InlineKeyboardButton("🌿 Ghibli", callback_data="cartoon_ghibli"),
            InlineKeyboardButton("🟡 Simpsons", callback_data="cartoon_simpsons")
        ],
        [
            InlineKeyboardButton("🧪 RickMorty", callback_data="cartoon_rickmorty")
        ]
    ])

    context.user_data["mode"] = "cartoon"
    await update.message.reply_text(
        "🎨 Выберите стиль мультфильма:",
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

    used = user["image_count"]
    bonus = user["bonus_images"]

    remaining = FREE_LIMIT + bonus - used

    keyboard = None

    # ✅ Кнопка только для админа
    if tg_user.id == ADMIN_ID:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("♻️ Обнулить лимиты", callback_data="reset_limits")]
        ])

    await update.message.reply_text(
        f"👤 Профиль\n\n"
        f"🆔 ID: {tg_user.id}\n"
        f"👤 Username: @{tg_user.username}\n\n"
        f"🎁 Бонусы: {bonus}\n"
        f"📦 Доступно: {remaining}\n"
        f"👥 Рефералов: {user['referrals']}",
        reply_markup=keyboard  # ✅ ВОТ ЭТО ТЫ ЗАБЫЛ
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
        "🎨 Выберите модель и размер изображения:",
        reply_markup=keyboard
    )

async def suno(update: Update, context: ContextTypes.DEFAULT_TYPE):

    # очищаем старые режимы
    context.user_data.clear()

    # устанавливаем режим музыки
    context.user_data["mode"] = "music"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎵 Hit song", callback_data="suno_hit")]
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

app.add_handler(CallbackQueryHandler(button_handler))

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
# Убираем повторное создание очередей в post_init
# Вместо этого используем глобальные очереди

async def post_init(app):
    global generation_queue_image, generation_queue_video, generation_queue_music

    await init_db()

    # Уже объявленные глобальные очереди, не создаем новые
    # generation_queue_image = asyncio.Queue(maxsize=5000)
    # generation_queue_video = asyncio.Queue(maxsize=2000)
    # generation_queue_music = asyncio.Queue(maxsize=2000)
    
    # Общая очередь для статистики / повторов
    global generation_queue
    generation_queue = asyncio.Queue(maxsize=10000)

    for _ in range(5):
        asyncio.create_task(image_worker())
    for _ in range(2):
        asyncio.create_task(video_worker())
    for _ in range(1):
        asyncio.create_task(music_worker())

    asyncio.create_task(cache_cleaner())
    await set_commands(app)
    logging.info("✅ PostgreSQL подключен и бот готов")
    if not db_pool:
        raise Exception("❌ DB не инициализирована")


app.post_init = post_init


if __name__ == "__main__":
    print("🚀 Бот запущен")
    app.run_polling(drop_pending_updates=True)
