import asyncio
import json
import logging
import os

import redis.asyncio as redis
from telegram import Bot

# ===== ИМПОРТ ТВОЕЙ ЛОГИКИ =====
from main import handle_generation_job, init_db

logging.basicConfig(level=logging.INFO)

# ===== ENV =====
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
TG_TOKEN = os.getenv("TG_TOKEN")

# ===== REDIS QUEUES =====
QUEUE_IMAGE = "queue:image"
QUEUE_VIDEO = "queue:video"
QUEUE_MUSIC = "queue:music"

# ===== GLOBALS =====
redis_client = None
bot = None

# ограничение одновременных задач (очень важно)
GLOBAL_SEMAPHORE = asyncio.Semaphore(10)


# ================= INIT =================
async def init_redis():
    global redis_client
    redis_client = redis.from_url(
        REDIS_URL,
        decode_responses=True
    )
    logging.info("✅ Redis подключен")


async def init_bot():
    global bot
    bot = Bot(token=TG_TOKEN)
    logging.info("✅ Telegram Bot инициализирован")


# ================= WORKER LOOP =================
async def worker_loop(queue_name):

    logging.info(f"🚀 Worker запущен: {queue_name}")

    while True:
        try:
            # ждём задачу
            _, job_data = await redis_client.brpop(queue_name)

            job = json.loads(job_data)

            logging.info(
                f"🔥 JOB | mode={job.get('mode')} | user={job.get('user_id')}"
            )

            # запускаем обработку
            asyncio.create_task(process_job(job))

        except Exception as e:
            logging.error(f"❌ Worker loop error: {e}")
            await asyncio.sleep(1)


# ================= JOB PROCESS =================
async def process_job(job):

    async with GLOBAL_SEMAPHORE:

        try:
            user_id = job.get("user_id")
            chat_id = job.get("chat_id")

            if not chat_id:
                logging.error("❌ Нет chat_id в job")
                return

            # ===== прокидываем bot внутрь =====
            job["bot"] = bot

            # ⚠️ убираем update/context
            job["update"] = None
            job["context"] = None

            # ===== ВАЖНО: лог старта =====
            await safe_send(chat_id, "⏳ Генерация началась...")

            await handle_generation_job(job)

        except asyncio.CancelledError:
            logging.warning("⛔ Job cancelled")

        except Exception as e:
            logging.error(f"❌ Job error: {e}")

            try:
                await safe_send(chat_id, "❌ Ошибка генерации. Попробуйте позже.")
            except:
                pass


# ================= SAFE SEND =================
async def safe_send(chat_id, text):
    try:
        await asyncio.wait_for(
            bot.send_message(chat_id=chat_id, text=text),
            timeout=10
        )
    except Exception as e:
        logging.error(f"❌ Telegram send error: {e}")


# ================= WORKERS =================
async def start_workers():

    await asyncio.gather(
        # IMAGE (основная нагрузка)
        worker_loop(QUEUE_IMAGE),
        worker_loop(QUEUE_IMAGE),
        worker_loop(QUEUE_IMAGE),
        worker_loop(QUEUE_IMAGE),
        worker_loop(QUEUE_IMAGE),

        # VIDEO
        worker_loop(QUEUE_VIDEO),
        worker_loop(QUEUE_VIDEO),

        # MUSIC
        worker_loop(QUEUE_MUSIC),
    )


# ================= MAIN =================
async def main():
    await init_redis()
    await init_bot()
    await init_db()

    logging.info("🚀 Worker готов к работе")

    await start_workers()


if __name__ == "__main__":
    asyncio.run(main())
