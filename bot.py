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

# === ДОКУМЕНТЫ (вставь свои ссылки) ===
USER_AGREEMENT_URL = "https://disk.yandex.ru/edit/disk/disk%2F%D0%9F%D0%BE%D0%BB%D1%8C%D0%B7%D0%BE%D0%B2%D0%B0%D1%82%D0%B5%D0%BB%D1%8C%D1%81%D0%BA%D0%BE%D0%B5%20%D1%81%D0%BE%D0%B3%D0%BB%D0%B0%D1%88%D0%B5%D0%BD%D0%B8%D0%B5.docx"
OFFER_URL = "https://disk.yandex.ru/edit/disk/disk%2F%D0%9E%D1%84%D0%B5%D1%80%D1%82%D0%B0.docx"

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

async def account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = get_user_image_data(user.id)
    remaining = FREE_IMAGE_LIMIT - data["count"]
    invited = get_referrals_count(user.id)

    await update.message.reply_text(
        f"👤 Профиль\n\n"
        f"ID: {user.id}\n"
        f"Имя: {user.first_name}\n\n"
        f"🖼 Осталось генераций: {remaining}/{FREE_IMAGE_LIMIT}\n"
        f"🎁 Засчитано рефералов: {invited}"
    )

async def referral_program(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    bot_username = (await context.bot.get_me()).username
    invited = get_referrals_count(user_id)
    link = f"https://t.me/{bot_username}?start={user_id}"

    await update.message.reply_text(
        f"🎁 Реферальная программа\n\n"
        f"Засчитано рефералов: {invited}\n"
        f"За каждого активного пользователя — +1 генерация 🖼\n\n"
        f"Твоя ссылка:\n{link}"
    )

async def photo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = ReplyKeyboardMarkup(
        [
            [KeyboardButton("⚡ Nano")],
            [KeyboardButton("🍌 Nano Banano 2")],
            [KeyboardButton("💎 Pro")]
        ],
        resize_keyboard=True
    )
    await update.message.reply_text(
        "Выбери модель генерации изображения 👇",
        reply_markup=keyboard
    )

async def chat_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_mode_users[update.effective_user.id] = True
    await update.message.reply_text("💬 Режим чата включён. Пиши сообщение.")

# ================= MESSAGE HANDLER =================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text

    # === ПРОВЕРКА ПРИНЯТИЯ УСЛОВИЙ ===
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

    # === МЕНЮ ===
    if text == "🖼 Создать изображение":
        await photo_command(update, context)
        return

    if text == "💬 Чат GPT (/uu)":
        await chat_mode(update, context)
        return

    if text == "👤 Профиль":
        await account(update, context)
        return

    if text == "🎁 Реферальная программа":
        await referral_program(update, context)
        return

    # === ВЫБОР МОДЕЛИ ===
    if text in ["⚡ Nano", "🍌 Nano Banano 2", "💎 Pro"]:
        selected_image_model[user_id] = "gpt-image-1"
        waiting_for_image_prompt[user_id] = True
        await update.message.reply_text("Опиши изображение 🎨", reply_markup=main_keyboard)
        return

    # === ГЕНЕРАЦИЯ ===
    if waiting_for_image_prompt.get(user_id):
        waiting_for_image_prompt[user_id] = False
        data = get_user_image_data(user_id)

        if data["count"] >= FREE_IMAGE_LIMIT:
            await update.message.reply_text("❌ Лимит 10 картинок в неделю исчерпан.")
            return

        await update.message.reply_text("Создаю изображение... ⏳")

        try:
            img = client.images.generate(
                model="gpt-image-1",
                prompt=text,
                size="512x512"
            )

            image_base64 = img.data[0].b64_json
            image_bytes = base64.b64decode(image_base64)

            cursor.execute(
                "UPDATE users SET image_count = image_count + 1 WHERE user_id=?",
                (user_id,)
            )
            conn.commit()

            await update.message.reply_photo(photo=BytesIO(image_bytes))

        except Exception as e:
            await update.message.reply_text(f"Ошибка: {str(e)}")

        return

    # === ЧАТ ===
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

# === REGISTER ===
telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("account", account))
telegram_app.add_handler(CommandHandler("photo", photo_command))
telegram_app.add_handler(CommandHandler("uu", chat_mode))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

# === FLASK ===
@flask_app.route(f"/{TG_TOKEN}", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), telegram_app.bot)
    loop.run_until_complete(telegram_app.process_update(update))
    return "ok"

@flask_app.route("/")
def home():
    return "Bot is running"

async def setup():
    await telegram_app.initialize()
    await telegram_app.bot.set_webhook(f"{WEBHOOK_URL}/{TG_TOKEN}")

if __name__ == "__main__":
    loop.run_until_complete(setup())
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
