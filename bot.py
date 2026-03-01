import os
import asyncio
import time
import sqlite3
from flask import Flask, request
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from openai import OpenAI

# === ENV VARIABLES ===
TG_TOKEN = os.getenv("TG_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

if not TG_TOKEN:
    raise ValueError("❌ TG_TOKEN не установлен")

if not OPENAI_API_KEY:
    raise ValueError("❌ OPENAI_API_KEY не установлен")

if not WEBHOOK_URL:
    raise ValueError("❌ WEBHOOK_URL не установлен")

print("✅ ENV переменные загружены")

# === ДОКУМЕНТЫ ===
USER_AGREEMENT_URL = "https://disk.yandex.ru/i/IB_pG2pcgtEIGQ"
OFFER_URL = "https://disk.yandex.ru/i/8IXTO8-VSMmbuw"

client = OpenAI(api_key=OPENAI_API_KEY)

# === DATABASE ===
conn = sqlite3.connect("/var/data/bot.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    week_start INTEGER,
    image_count INTEGER DEFAULT 0,
    accepted_terms INTEGER DEFAULT 0
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS referrals (
    invited_id INTEGER PRIMARY KEY,
    referrer_id INTEGER,
    rewarded INTEGER DEFAULT 0
)
""")

conn.commit()

# === APPS ===
flask_app = Flask(__name__)
telegram_app = ApplicationBuilder().token(TG_TOKEN).build()

# === КЛАВИАТУРЫ ===
main_keyboard = ReplyKeyboardMarkup(
    [
        [KeyboardButton("🖼 Создать изображение"), KeyboardButton("💬 Чат GPT (/uu)")],
        [KeyboardButton("👤 Профиль"), KeyboardButton("🎁 Реферальная программа")]
    ],
    resize_keyboard=True
)

terms_keyboard = ReplyKeyboardMarkup(
    [[KeyboardButton("✅ Продолжить")]],
    resize_keyboard=True
)

# ================= HANDLERS =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    cursor.execute("SELECT accepted_terms FROM users WHERE user_id=?", (user.id,))
    row = cursor.fetchone()

    if not row:
        cursor.execute(
            "INSERT INTO users (user_id, week_start, image_count, accepted_terms) VALUES (?, ?, 0, 0)",
            (user.id, int(time.time()))
        )
        conn.commit()
        accepted = 0
    else:
        accepted = row[0]

    if accepted == 0:
        await update.message.reply_text(
            f"📜 Перед началом использования бота:\n\n"
            f"📄 Пользовательское соглашение:\n{USER_AGREEMENT_URL}\n\n"
            f"💰 Публичная оферта:\n{OFFER_URL}\n\n"
            f"Нажимая «Продолжить», вы соглашаетесь с условиями.",
            reply_markup=terms_keyboard,
            disable_web_page_preview=True
        )
        return

    await update.message.reply_text(
        "🚀 Добро пожаловать!\n\nВыберите действие 👇",
        reply_markup=main_keyboard
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text

    cursor.execute("SELECT accepted_terms FROM users WHERE user_id=?", (user_id,))
    row = cursor.fetchone()

    if not row or row[0] == 0:
        if text == "✅ Продолжить":
            cursor.execute("UPDATE users SET accepted_terms=1 WHERE user_id=?", (user_id,))
            conn.commit()

            await update.message.reply_text(
                "✅ Спасибо! Теперь бот доступен 🚀",
                reply_markup=main_keyboard
            )
            return

        await update.message.reply_text(
            "❗ Сначала необходимо принять условия.",
            reply_markup=terms_keyboard
        )
        return

# ================= REGISTRATION =================

telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

# ================= WEBHOOK =================

event_loop = None  # глобальный loop

@flask_app.route(f"/{TG_TOKEN}", methods=["POST"])
def webhook():
    global event_loop

    update = Update.de_json(request.get_json(force=True), telegram_app.bot)

    # используем созданный loop
    event_loop.create_task(
        telegram_app.process_update(update)
    )

    return "ok"

# ================= START =================

if __name__ == "__main__":

    async def startup():
        await telegram_app.initialize()
        await telegram_app.start()
        await telegram_app.bot.set_webhook(f"{WEBHOOK_URL}/{TG_TOKEN}")
        print("🚀 Webhook установлен")

    # СОЗДАЁМ LOOP ВРУЧНУЮ (для Python 3.14)
    event_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(event_loop)

    event_loop.run_until_complete(startup())

    flask_app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000))
    )
