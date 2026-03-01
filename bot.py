import os
import time
import sqlite3
import base64
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from openai import OpenAI

# ================= ENV =================

TG_TOKEN = os.getenv("TG_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not TG_TOKEN:
    raise ValueError("❌ TG_TOKEN не установлен")

if not OPENAI_API_KEY:
    raise ValueError("❌ OPENAI_API_KEY не установлен")

print("✅ ENV загружены")

client = OpenAI(api_key=OPENAI_API_KEY)

USER_AGREEMENT_URL = "https://disk.yandex.ru/i/IB_pG2pcgtEIGQ"
OFFER_URL = "https://disk.yandex.ru/i/8IXTO8-VSMmbuw"

FREE_LIMIT = 5
REF_BONUS = 3
WEEK_SECONDS = 7 * 24 * 60 * 60

# ================= DATABASE =================

conn = sqlite3.connect("bot.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    week_start INTEGER,
    image_count INTEGER DEFAULT 0,
    accepted_terms INTEGER DEFAULT 0,
    referrals INTEGER DEFAULT 0,
    bonus_images INTEGER DEFAULT 0,
    ref_by INTEGER
)
""")

conn.commit()

# ================= TELEGRAM =================

app = ApplicationBuilder().token(TG_TOKEN).build()

main_keyboard = ReplyKeyboardMarkup(
    [
        [KeyboardButton("🖼 Создать изображение"), KeyboardButton("💬 Чат GPT")],
        [KeyboardButton("👤 Профиль"), KeyboardButton("🎁 Реферальная программа")]
    ],
    resize_keyboard=True
)

terms_keyboard = ReplyKeyboardMarkup(
    [[KeyboardButton("✅ Продолжить")]],
    resize_keyboard=True
)

# ================= HELPERS =================

def get_user(user_id):
    cursor.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    return cursor.fetchone()

def reset_week_if_needed(user):
    now = int(time.time())
    if now - user[1] > WEEK_SECONDS:
        cursor.execute(
            "UPDATE users SET week_start=?, image_count=0 WHERE user_id=?",
            (now, user[0])
        )
        conn.commit()

# ================= HANDLERS =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ref_id = None

    if context.args:
        try:
            ref_id = int(context.args[0])
        except:
            pass

    db_user = get_user(user.id)

    if not db_user:
        cursor.execute(
            "INSERT INTO users (user_id, week_start, accepted_terms, ref_by) VALUES (?, ?, 0, ?)",
            (user.id, int(time.time()), ref_id)
        )
        conn.commit()

        if ref_id and ref_id != user.id:
            cursor.execute(
                "UPDATE users SET referrals=referrals+1, bonus_images=bonus_images+? WHERE user_id=?",
                (REF_BONUS, ref_id)
            )
            conn.commit()

    db_user = get_user(user.id)

    if db_user[3] == 0:
        await update.message.reply_text(
            f"📜 Пользовательское соглашение:\n{USER_AGREEMENT_URL}\n\n"
            f"💰 Оферта:\n{OFFER_URL}\n\n"
            "Нажмите «Продолжить»",
            reply_markup=terms_keyboard
        )
        return

    await update.message.reply_text("🚀 Добро пожаловать!", reply_markup=main_keyboard)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    user = get_user(user_id)

    if user[3] == 0:
        if text == "✅ Продолжить":
            cursor.execute("UPDATE users SET accepted_terms=1 WHERE user_id=?", (user_id,))
            conn.commit()
            await update.message.reply_text("✅ Доступ открыт!", reply_markup=main_keyboard)
        else:
            await update.message.reply_text("❗ Примите условия", reply_markup=terms_keyboard)
        return

    reset_week_if_needed(user)
    user = get_user(user_id)

    if text == "👤 Профиль":
        remaining = FREE_LIMIT + user[6] - user[2]
        await update.message.reply_text(
            f"👤 Ваш профиль\n\n"
            f"🖼 Использовано: {user[2]}\n"
            f"🎁 Бонусы: {user[6]}\n"
            f"📦 Осталось генераций: {remaining}\n"
            f"👥 Приглашено: {user[4]}"
        )
        return

    if text == "🎁 Реферальная программа":
        link = f"https://t.me/{context.bot.username}?start={user_id}"
        await update.message.reply_text(
            f"🎁 Приглашай друзей!\n\n"
            f"За каждого — +{REF_BONUS} генерации\n\n"
            f"🔗 Твоя ссылка:\n{link}"
        )
        return

    if text == "💬 Чат GPT":
        await update.message.reply_text("Напиши сообщение для GPT 👇")
        context.user_data["chat_mode"] = True
        return

    if text == "🖼 Создать изображение":
        await update.message.reply_text("Опиши изображение 👇")
        context.user_data["image_mode"] = True
        return

    # GPT CHAT
    if context.user_data.get("chat_mode"):
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": text}]
        )
        await update.message.reply_text(response.choices[0].message.content)
        return

    # IMAGE GENERATION
    if context.user_data.get("image_mode"):
        remaining = FREE_LIMIT + user[6] - user[2]
        if remaining <= 0:
            await update.message.reply_text("❌ Лимит исчерпан.")
            return

        await update.message.reply_text("🎨 Генерирую изображение...")

        img = client.images.generate(
            model="gpt-image-1",
            prompt=text,
            size="1024x1024"
        )

        image_bytes = base64.b64decode(img.data[0].b64_json)

        await update.message.reply_photo(photo=image_bytes)

        cursor.execute(
            "UPDATE users SET image_count=image_count+1 WHERE user_id=?",
            (user_id,)
        )
        conn.commit()
        return


# ================= REGISTER =================

app.add_handler(CommandHandler("start", start))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

# ================= START =================

if __name__ == "__main__":
    print("🚀 Бот запущен")
    app.run_polling(drop_pending_updates=True)
