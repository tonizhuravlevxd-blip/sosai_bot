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

client = OpenAI(api_key=OPENAI_API_KEY)

FREE_LIMIT = 5
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
    ref_by INTEGER,
    is_active INTEGER DEFAULT 0
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

def activate_user_if_needed(user):
    # если пользователь впервые стал активным
    if user[7] == 0:
        cursor.execute(
            "UPDATE users SET is_active=1 WHERE user_id=?",
            (user[0],)
        )
        conn.commit()

        # начисляем бонус пригласившему
        if user[6]:
            cursor.execute(
                "UPDATE users SET bonus_images=bonus_images+1, referrals=referrals+1 WHERE user_id=?",
                (user[6],)
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

    db_user = get_user(user.id)

    if db_user[3] == 0:
        await update.message.reply_text(
            "📜 Примите условия для продолжения",
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

    # ================= ПРОФИЛЬ =================

    if text == "👤 Профиль":
        used = user[2]
        bonus = user[5]
        remaining = FREE_LIMIT + bonus - used

        await update.message.reply_text(
            f"👤 Ваш профиль\n\n"
            f"🆓 Бесплатно в неделю: {FREE_LIMIT}\n"
            f"🖼 Использовано: {used}\n"
            f"🎁 Бонусные генерации: {bonus}\n"
            f"📦 Доступно сейчас: {remaining}\n"
            f"👥 Активных рефералов: {user[4]}"
        )
        return

    # ================= РЕФЕРАЛКА =================

    if text == "🎁 Реферальная программа":
        link = f"https://t.me/{context.bot.username}?start={user_id}"
        await update.message.reply_text(
            "🎁 Реферальная программа\n\n"
            "Вы получаете +1 генерацию\n"
            "за каждого приглашённого пользователя,\n"
            "который реально что-то написал или создал.\n\n"
            f"🔗 Ваша ссылка:\n{link}"
        )
        return

    # ================= GPT =================

    if text == "💬 Чат GPT":
        await update.message.reply_text("Напишите сообщение 👇")
        context.user_data["chat_mode"] = True
        return

    if context.user_data.get("chat_mode"):
        activate_user_if_needed(user)

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": text}]
        )

        await update.message.reply_text(response.choices[0].message.content)
        return

    # ================= IMAGE =================

    if text == "🖼 Создать изображение":
        await update.message.reply_text("Опишите изображение 👇")
        context.user_data["image_mode"] = True
        return

    if context.user_data.get("image_mode"):
        remaining = FREE_LIMIT + user[5] - user[2]

        if remaining <= 0:
            await update.message.reply_text("❌ Лимит исчерпан.")
            return

        activate_user_if_needed(user)

        await update.message.reply_text("🎨 Генерирую...")

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


# ================= START =================

app.add_handler(CommandHandler("start", start))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

if __name__ == "__main__":
    print("🚀 Бот запущен")
    app.run_polling(drop_pending_updates=True)
