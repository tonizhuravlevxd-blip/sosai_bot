import os
import asyncio
import time
from flask import Flask, request
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from openai import OpenAI

# === ENV VARIABLES ===
TG_TOKEN = os.getenv("TG_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

client = OpenAI(api_key=OPENAI_API_KEY)

# === GLOBAL EVENT LOOP ===
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

# === APPS ===
flask_app = Flask(__name__)
telegram_app = ApplicationBuilder().token(TG_TOKEN).build()

# === SETTINGS ===
FREE_IMAGE_LIMIT = 25
WEEK_SECONDS = 7 * 24 * 60 * 60

user_mode = {}
waiting_for_image_prompt = {}
user_image_data = {}

# ================= HELPERS =================

def get_user_image_data(user_id):
    now = time.time()

    if user_id not in user_image_data:
        user_image_data[user_id] = {
            "count": 0,
            "week_start": now
        }

    data = user_image_data[user_id]

    # сброс если прошла неделя
    if now - data["week_start"] > WEEK_SECONDS:
        data["count"] = 0
        data["week_start"] = now

    return data

# ================= HANDLERS =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚀 Бот работает!\n\n"
        "Команды:\n"
        "/nano — быстрый режим\n"
        "/pro — мощный режим\n"
        "/photo — создать изображение\n"
        "/account — профиль"
    )

async def account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = get_user_image_data(user.id)

    remaining = FREE_IMAGE_LIMIT - data["count"]

    await update.message.reply_text(
        f"👤 Профиль\n\n"
        f"ID: {user.id}\n"
        f"Имя: {user.first_name}\n"
        f"Режим: {user_mode.get(user.id, 'nano')}\n\n"
        f"🖼 Осталось генераций: {remaining}/{FREE_IMAGE_LIMIT}"
    )

async def set_nano(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_mode[update.effective_user.id] = "gpt-4o-mini"
    await update.message.reply_text("Режим nano включён")

async def set_pro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_mode[update.effective_user.id] = "gpt-4o"
    await update.message.reply_text("Режим pro включён")

async def photo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    waiting_for_image_prompt[update.effective_user.id] = True
    await update.message.reply_text("Опиши изображение, которое хочешь создать 🎨")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text

    # ===== ЕСЛИ ЖДЁМ ОПИСАНИЕ КАРТИНКИ =====
    if waiting_for_image_prompt.get(user_id):
        waiting_for_image_prompt[user_id] = False

        data = get_user_image_data(user_id)

        if data["count"] >= FREE_IMAGE_LIMIT:
            await update.message.reply_text(
                "❌ Лимит 25 бесплатных генераций в неделю исчерпан.\n"
                "Попробуй снова через 7 дней 💎"
            )
            return

        await update.message.reply_text("Создаю изображение... ⏳")

        try:
            img = client.images.generate(
                model="gpt-image-1",
                prompt=text,
                size="1024x1024"
            )

            image_url = img.data[0].url

            data["count"] += 1

            await update.message.reply_photo(image_url)

        except Exception:
            await update.message.reply_text("Ошибка при создании изображения 😢")

        return

    # ===== GPT ТЕКСТ =====
    model = user_mode.get(user_id, "gpt-4o-mini")

    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": text}]
    )

    await update.message.reply_text(response.choices[0].message.content)

# === REGISTER HANDLERS ===
telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("account", account))
telegram_app.add_handler(CommandHandler("nano", set_nano))
telegram_app.add_handler(CommandHandler("pro", set_pro))
telegram_app.add_handler(CommandHandler("photo", photo_command))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

# === FLASK ROUTES ===
@flask_app.route(f"/{TG_TOKEN}", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), telegram_app.bot)
    loop.run_until_complete(telegram_app.process_update(update))
    return "ok"

@flask_app.route("/")
def home():
    return "Bot is running"

# === STARTUP ===
async def setup():
    await telegram_app.initialize()
    await telegram_app.bot.set_webhook(f"{WEBHOOK_URL}/{TG_TOKEN}")

if __name__ == "__main__":
    loop.run_until_complete(setup())
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
