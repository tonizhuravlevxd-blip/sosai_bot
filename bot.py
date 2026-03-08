import asyncio
import logging
import os
from io import BytesIO

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto
)

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)

from openai import AsyncOpenAI

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

client = AsyncOpenAI(api_key=OPENAI_API_KEY)

MAX_INPUT_IMAGES = 4

SIZE_CONFIG = {
    "square": "1024x1024",
    "wide": "1792x1024",
    "phone": "1024x1792"
}

GEN_QUEUE = asyncio.Queue()

# увеличено для скорости
GEN_WORKERS = 4


async def generation_worker(app: Application):

    while True:

        job = await GEN_QUEUE.get()

        try:

            user_id = job["user_id"]
            chat_id = job["chat_id"]
            prompt = job["prompt"]
            model = job["model"]

            size_key = job["size"]
            size = SIZE_CONFIG.get(size_key, "1024x1024")

            images = job["images"]

            inputs = []

            if prompt:
                inputs.append(prompt)

            for img in images:
                inputs.append(img)

            result = await client.images.generate(
                model=model,
                prompt=prompt,
                size=size
            )

            image_url = result.data[0].url

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔁 Повторить",
                        callback_data=f"repeat_{job['id']}"
                    )
                ]
            ])

            await app.bot.send_photo(
                chat_id=chat_id,
                photo=image_url,
                reply_markup=keyboard
            )

        except Exception as e:

            logging.error(f"Generation error: {e}")

            await app.bot.send_message(
                chat_id=chat_id,
                text="❌ Ошибка генерации. Попробуйте другой текст."
            )

        GEN_QUEUE.task_done()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "👋 Отправьте фото (до 4) и текст\n\n"
        "или напишите текст для генерации."
    )


async def photo(update: Update, context: ContextTypes.DEFAULT_TYPE):

    keyboard = InlineKeyboardMarkup([

        [
            InlineKeyboardButton("⚡ Flash", callback_data="model_flash"),
            InlineKeyboardButton("🍌 Nano Banana 1", callback_data="model_banana1"),
            InlineKeyboardButton("🍌 Nano Banana 2", callback_data="model_banana2")
        ],

        [
            InlineKeyboardButton("⬜ 1:1", callback_data="size_square"),
            InlineKeyboardButton("🖥 16:9", callback_data="size_wide"),
            InlineKeyboardButton("📱 Phone", callback_data="size_phone")
        ]

    ])

    await update.message.reply_text(
        "🎨 Выберите модель и разрешение:",
        reply_markup=keyboard
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    data = query.data

    if data == "model_flash":

        context.user_data["model"] = "gpt-image-1"

        await query.message.reply_text(
            "⚡ Выбрана модель Flash\n\n"
            "Отправьте фото или напишите текст"
        )

    elif data == "model_banana1":

        context.user_data["model"] = "gpt-image-1"

        await query.message.reply_text(
            "🍌 Выбрана модель Nano Banana 1\n\n"
            "Отправьте фото или напишите текст"
        )

    elif data == "model_banana2":

        context.user_data["model"] = "gpt-image-1"

        await query.message.reply_text(
            "🍌 Выбрана модель Nano Banana 2\n\n"
            "Отправьте фото или напишите текст"
        )

    elif data == "size_square":

        context.user_data["size"] = "square"

        await query.message.reply_text(
            "⬜ Выбрано разрешение 1:1"
        )

    elif data == "size_wide":

        context.user_data["size"] = "wide"

        await query.message.reply_text(
            "🖥 Выбрано разрешение 16:9"
        )

    elif data == "size_phone":

        context.user_data["size"] = "phone"

        await query.message.reply_text(
            "📱 Выбрано разрешение для телефона"
        )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):

    photos = update.message.photo

    file = await photos[-1].get_file()

    img_bytes = await file.download_as_bytearray()

    images = context.user_data.get("images", [])

    if len(images) >= MAX_INPUT_IMAGES:

        await update.message.reply_text(
            "⚠️ Можно максимум 4 изображения"
        )
        return

    images.append(BytesIO(img_bytes))

    context.user_data["images"] = images

    await update.message.reply_text(
        f"📸 Загружено изображений: {len(images)}"
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):

    prompt = update.message.text

    images = context.user_data.get("images", [])

    model = context.user_data.get("model", "gpt-image-1")

    size = context.user_data.get("size", "square")

    job = {
        "id": str(update.message.message_id),
        "user_id": update.effective_user.id,
        "chat_id": update.effective_chat.id,
        "prompt": prompt,
        "images": images,
        "model": model,
        "size": size
    }

    await GEN_QUEUE.put(job)

    context.user_data["images"] = []

    await update.message.reply_text(
        "⏳ Генерация..."
    )


async def main():

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("photo", photo))

    app.add_handler(CallbackQueryHandler(button_handler))

    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    for _ in range(GEN_WORKERS):
        asyncio.create_task(generation_worker(app))

    await app.run_polling()


if __name__ == "__main__":
    asyncio.run(main())
