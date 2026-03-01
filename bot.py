import os
import asyncio
import time
import base64
import sqlite3
from io import BytesIO
from flask import Flask, request
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from openai import OpenAI

# === ENV VARIABLES ===
TG_TOKEN = os.getenv("TG_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
# === ПРОВЕРКА ENV (чтобы Render не падал молча) ===
if not TG_TOKEN:
    raise ValueError("❌ TG_TOKEN не установлен в Render (Environment Variables)")

if not OPENAI_API_KEY:
    raise ValueError("❌ OPENAI_API_KEY не установлен в Render (Environment Variables)")

if not WEBHOOK_URL:
    raise ValueError("❌ WEBHOOK_URL не установлен в Render (Environment Variables)")

print("✅ TG_TOKEN найден")
print("✅ OPENAI_API_KEY найден")
print("✅ WEBHOOK_URL найден")

# === ДОКУМЕНТЫ (вставь публичные ссылки!) ===
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

# === ВАЖНО: ОБНОВЛЕНИЕ СТРУКТУРЫ ЕСЛИ БАЗА СТАРАЯ ===
try:
    cursor.execute("ALTER TABLE users ADD COLUMN accepted_terms INTEGER DEFAULT 0")
    conn.commit()
except sqlite3.OperationalError:
    pass

# === GLOBAL EVENT LOOP ===
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

# === APPS ===
flask_app = Flask(__name__)
telegram_app = ApplicationBuilder().token(TG_TOKEN).build()

# === SETTINGS ===
FREE_IMAGE_LIMIT = 10
WEEK_SECONDS = 7 * 24 * 60 * 60

waiting_for_image_prompt = {}
chat_mode_users = {}
selected_image_model = {}

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

# ================= HELPERS =================

def get_user_image_data(user_id):
    now = int(time.time())
    cursor.execute("SELECT week_start, image_count FROM users WHERE user_id=?", (user_id,))
    row = cursor.fetchone()

    if not row:
        cursor.execute(
            "INSERT INTO users (user_id, week_start, image_count, accepted_terms) VALUES (?, ?, 0, 0)",
            (user_id, now)
        )
        conn.commit()
        return {"week_start": now, "count": 0}

    week_start, image_count = row

    if now - week_start > WEEK_SECONDS:
        cursor.execute(
            "UPDATE users SET week_start=?, image_count=0 WHERE user_id=?",
            (now, user_id)
        )
        conn.commit()
        return {"week_start": now, "count": 0}

    return {"week_start": week_start, "count": image_count}

def get_referrals_count(user_id):
    cursor.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (user_id,))
    return cursor.fetchone()[0]

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
            f"📜 Перед началом использования бота необходимо ознакомиться с документами:\n\n"
            f"📄 Пользовательское соглашение:\n{USER_AGREEMENT_URL}\n\n"
            f"💰 Публичная оферта:\n{OFFER_URL}\n\n"
            f"Нажимая «Продолжить», вы подтверждаете согласие с условиями.",
            reply_markup=terms_keyboard,
            disable_web_page_preview=True
        )
        return

    await update.message.reply_text(
        "🚀 Добро пожаловать!\n\nВыбери действие ниже 👇",
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
                "✅ Спасибо! Теперь вы можете пользоваться ботом 🚀",
                reply_markup=main_keyboard
            )
            return

        await update.message.reply_text(
            f"📜 Для использования бота необходимо принять условия:\n\n"
            f"{USER_AGREEMENT_URL}\n\n"
            f"{OFFER_URL}",
            reply_markup=terms_keyboard,
            disable_web_page_preview=True
        )
        return

    # остальная логика остаётся без изменений
