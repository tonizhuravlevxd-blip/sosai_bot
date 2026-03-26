import asyncio
import json
import logging
import os
import redis.asyncio as redis

# ===== ВАЖНО: импортируешь свою функцию =====
from main import handle_generation_job, init_db

logging.basicConfig(level=logging.INFO)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

QUEUE_IMAGE = "queue:image"
QUEUE_VIDEO = "queue:video"
QUEUE_MUSIC = "queue:music"

redis_client = None


async def init_redis():
    global redis_client
    redis_client = redis.from_url(
        REDIS_URL,
        decode_responses=True
    )
    logging.info("✅ Redis подключен")


# ===== ОСНОВНОЙ WORKER =====
async def worker_loop(queue_name):

    logging.info(f"🚀 Worker запущен: {queue_name}")

    while True:
        try:
            # BRPOP = блокирующее ожидание задачи
            _, job_data = await redis_client.brpop(queue_name)

            job = json.loads(job_data)

            logging.info(f"🔥 JOB: {job.get('mode')} | user={job.get('user_id')}")

            await process_job(job)

        except Exception as e:
            logging.error(f"❌ Worker loop error: {e}")
            await asyncio.sleep(1)


# ===== ОБРАБОТКА ЗАДАЧИ =====
async def process_job(job):

    try:
        # ⚠️ ВАЖНО: handle_generation_job ожидает update/context
        # поэтому мы прокидываем "фейковые" значения
        job["update"] = None
        job["context"] = None

        await handle_generation_job(job)

    except asyncio.CancelledError:
        logging.warning("⛔ Job cancelled")

    except Exception as e:
        logging.error(f"❌ Job error: {e}")


# ===== ПАРАЛЛЕЛЬНЫЕ WORKERS =====
async def start_workers():

    await asyncio.gather(
        # изображения (основная нагрузка)
        worker_loop(QUEUE_IMAGE),
        worker_loop(QUEUE_IMAGE),
        worker_loop(QUEUE_IMAGE),
        worker_loop(QUEUE_IMAGE),

        # видео (дорогие задачи)
        worker_loop(QUEUE_VIDEO),
        worker_loop(QUEUE_VIDEO),

        # музыка
        worker_loop(QUEUE_MUSIC),
    )


# ===== MAIN =====
async def main():
    await init_redis()
    await init_db()  # используем ту же БД
    await start_workers()


if __name__ == "__main__":
    print("🚀 Worker запущен")
    asyncio.run(main())
