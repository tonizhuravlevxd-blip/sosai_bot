import os
import asyncio
import time
import base64
from io import BytesIO
from flask import Flask, request
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
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
FREE_IMAGE_LIMIT = 10
WEEK_SECONDS = 7 * 24 * 60 * 60

user_mode = {}
waiting_for_image_prompt = {}
user_image_data = {}
chat_mode_users = {}

# === КНОПОЧНОЕ МЕНЮ ===
main_keyboard = ReplyKeyboardMarkup(
    [
        [KeyboardButton("🖼 Создать изображение"), KeyboardButton("💬 Чат GPT (/uu)")],
        [KeyboardButton("👤 Профиль")]
    ],
    resize_keyboard=True
)

# ================= HELPERS =================

def get_user_image_data(user_id):
    now = time.time()

    if user_id not in user_image_data:
        user_image_data[user_id] = {"count": 0, "week_start": now}

    data = user_image_data[user_id]

    if now - data["week_start"] > WEEK_SECONDS:
        data["count"] = 0
        data["week_start"] = now

    return data

# ================= HANDLERS =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚀 Добро пожаловать!\n\nВыбери действие ниже 👇",
        reply_markup=main_keyboard
    )

async def account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = get_user_image_data(user.id)
    remaining = FREE_IMAGE_LIMIT - data["count"]

    await update.message.reply_text(
        f"👤 Профиль\n\n"
        f"ID: {user.id}\n"
        f"Имя: {user.first_name}\n\n"
        f"🖼 Осталось генераций: {remaining}/{FREE_IMAGE_LIMIT}"
    )

async def photo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    waiting_for_image_prompt[update.effective_user.id] = True
    await update.message.reply_text("Опиши изображение 🎨")

async def chat_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_mode_users[update.effective_user.id] = True
    await update.message.reply_text("💬 Режим чата включён. Пиши сообщение.")

# ================= MESSAGE HANDLER =================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text

    # === НАЖАТИЯ КНОПОК ===
    if text == "🖼 Создать изображение":
        await photo_command(update, context)
        return

    if text == "💬 Чат GPT (/uu)":
        await chat_mode(update, context)
        return

    if text == "👤 Профиль":
        await account(update, context)
        return

    # === ГЕНЕРАЦИЯ ИЗОБРАЖЕНИЯ ===
    if waiting_for_image_prompt.get(user_id):
        waiting_for_image_prompt[user_id] = False

        data = get_user_image_data(user_id)

        if data["count"] >= FREE_IMAGE_LIMIT:
            await update.message.reply_text(
                "❌ Лимит 10 картинок в неделю исчерпан."
            )
            return

        await update.message.reply_text("Создаю изображение... ⏳")

        try:
            img = client.images.generate(
                model="gpt-image-1",
                prompt=text,
                size="512x512"  # дешевле
            )

            image_base64 = img.data[0].b64_json
            image_bytes = base64.b64decode(image_base64)

            data["count"] += 1

            await update.message.reply_photo(photo=BytesIO(image_bytes))

        except Exception as e:
            await update.message.reply_text(f"Ошибка: {str(e)}")

        return

    # === РЕЖИМ ЧАТА ===
    if chat_mode_users.get(user_id):
        try:
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": text}]
            )

            await update.message.reply_text(response.choices[0].message.content)

        except Exception as e:
            await update.message.reply_text(f"Ошибка: {str(e)}")

        return

# === REGISTER HANDLERS ===
telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("account", account))
telegram_app.add_handler(CommandHandler("photo", photo_command))
telegram_app.add_handler(CommandHandler("uu", chat_mode))
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
