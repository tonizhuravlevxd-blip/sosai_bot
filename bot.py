import os
import asyncio
from flask import Flask, request
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from openai import OpenAI

# === ENV VARIABLES ===
TG_TOKEN = os.getenv("TG_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

client = OpenAI(api_key=OPENAI_API_KEY)

# === APPS ===
flask_app = Flask(__name__)
telegram_app = ApplicationBuilder().token(TG_TOKEN).build()

user_mode = {}

# === TELEGRAM HANDLERS ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Бот работает 🚀 Используй /nano или /pro")

async def set_nano(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_mode[update.effective_user.id] = "gpt-4o-mini"
    await update.message.reply_text("Режим nano включён")

async def set_pro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_mode[update.effective_user.id] = "gpt-4o"
    await update.message.reply_text("Режим pro включён")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    model = user_mode.get(user_id, "gpt-4o-mini")

    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": update.message.text}]
    )

    await update.message.reply_text(response.choices[0].message.content)

telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("nano", set_nano))
telegram_app.add_handler(CommandHandler("pro", set_pro))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

# === FLASK ROUTES ===
@flask_app.route(f"/{TG_TOKEN}", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), telegram_app.bot)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(telegram_app.process_update(update))
    loop.close()

    return "ok"

@flask_app.route("/")
def home():
    return "Bot is running"

# === STARTUP ===
async def setup():
    await telegram_app.initialize()
    await telegram_app.bot.set_webhook(f"{WEBHOOK_URL}/{TG_TOKEN}")

if __name__ == "__main__":
    asyncio.run(setup())
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
