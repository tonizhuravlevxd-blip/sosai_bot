import os
import time
import sqlite3
import base64
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BotCommand
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
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

USER_AGREEMENT_URL = "https://disk.yandex.ru/i/IB_pG2pcgtEIGQ"
OFFER_URL = "https://disk.yandex.ru/i/8IXTO8-VSMmbuw"

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
    if user[7] == 0:
        cursor.execute("UPDATE users SET is_active=1 WHERE user_id=?", (user[0],))
        conn.commit()

        if user[6]:
            cursor.execute(
                "UPDATE users SET bonus_images=bonus_images+1, referrals=referrals+1 WHERE user_id=?",
                (user[6],)
            )
            conn.commit()

# ================= START =================

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
        terms_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📄 Пользовательское соглашение", url=USER_AGREEMENT_URL)],
            [InlineKeyboardButton("💰 Публичная оферта", url=OFFER_URL)],
            [InlineKeyboardButton("✅ Продолжить", callback_data="accept_terms")]
        ])

        await update.message.reply_text(
            "📜 Перед началом использования бота необходимо принять условия.",
            reply_markup=terms_keyboard,
        )
        return

    await update.message.reply_text("🚀 Добро пожаловать!")

# ================= CALLBACK =================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == "accept_terms":
        cursor.execute("UPDATE users SET accepted_terms=1 WHERE user_id=?", (user_id,))
        conn.commit()
        await query.edit_message_text("✅ Условия приняты.\nТеперь используйте меню слева.")

# ================= ACCOUNT =================

async def account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    user = get_user(tg_user.id)

    reset_week_if_needed(user)
    user = get_user(tg_user.id)

    used = user[2]
    bonus = user[5]
    remaining = FREE_LIMIT + bonus - used

    await update.message.reply_text(
        f"👤 Ваш профиль\n\n"
        f"🆔 ID: {tg_user.id}\n"
        f"👤 Username: @{tg_user.username}\n\n"
        f"🆓 Бесплатно: {FREE_LIMIT}\n"
        f"🎁 Бонусы: {bonus}\n"
        f"📦 Доступно: {remaining}\n"
        f"👥 Рефералов: {user[4]}"
    )

# ================= REF =================

async def ref(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    link = f"https://t.me/{context.bot.username}?start={user_id}"

    await update.message.reply_text(
        "🎁 Реферальная программа\n\n"
        "За каждого активного пользователя вы получаете +1 генерацию.\n\n"
        f"Ваша ссылка:\n{link}"
    )

# ================= GPT =================

async def uu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("💬 Напишите сообщение для ChatGPT")
    context.user_data["chat_mode"] = True

# ================= IMAGE =================

async def photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🖼 Отправьте описание изображения")
    context.user_data["image_mode"] = True

# ================= TEXT =================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user = get_user(update.effective_user.id)

    if context.user_data.get("chat_mode"):
        activate_user_if_needed(user)

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": text}]
        )

        await update.message.reply_text(response.choices[0].message.content)
        return

    if context.user_data.get("image_mode"):
        remaining = FREE_LIMIT + user[5] - user[2]
        if remaining <= 0:
            await update.message.reply_text("❌ Лимит исчерпан.")
            return

        activate_user_if_needed(user)

        await update.message.reply_text(
            "Nano Banana 2 создает шедевр, пожалуйста подождите..."
        )

        img = client.images.generate(
            model="gpt-image-1",
            prompt=text,
            size="1024x1024"
        )

        image_bytes = base64.b64decode(img.data[0].b64_json)
        await update.message.reply_photo(photo=image_bytes)

        cursor.execute(
            "UPDATE users SET image_count=image_count+1 WHERE user_id=?",
            (update.effective_user.id,)
        )
        conn.commit()
        return

# ================= REGISTER =================

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("account", account))
app.add_handler(CommandHandler("ref", ref))
app.add_handler(CommandHandler("uu", uu))
app.add_handler(CommandHandler("photo", photo))

app.add_handler(CallbackQueryHandler(button_handler))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

# ================= SET MENU =================

async def set_commands(app):
    await app.bot.set_my_commands([
        BotCommand("start", "Запуск бота"),
        BotCommand("account", "Мой профиль"),
        BotCommand("ref", "Реферальная программа"),
        BotCommand("uu", "Чат GPT"),
        BotCommand("photo", "Создать изображение"),
    ])

app.post_init = set_commands

# ================= START =================

if __name__ == "__main__":
    print("🚀 Бот запущен")
    app.run_polling(drop_pending_updates=True)
