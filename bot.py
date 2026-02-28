import os
import asyncio
from flask import Flask, request
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    BotCommand
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes
)
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

user_mode = {}
waiting_for_image_prompt = {}

# === MAIN KEYBOARD ===
main_keyboard = ReplyKeyboardMarkup(
    [
        [KeyboardButton("ℹ️ Что умеет бот"), KeyboardButton("👤 Мой профиль")],
        [KeyboardButton("🖼 Создать изображение")]
    ],
    resize_keyboard=True
)

# ================= HANDLERS =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🚀 Добро пожаловать!\n\n"
        "Я могу:\n"
        "• Отвечать на вопросы\n"
        "• Генерировать текст\n"
        "• Создавать изображения\n\n"
        "Выбери действие ниже 👇"
    )

    await update.message.reply_text(text, reply_markup=main_keyboard)


async def account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (
        f"👤 Профиль\n\n"
        f"ID: {user.id}\n"
        f"Имя: {user.first_name}\n"
        f"Режим: {user_mode.get(user.id, 'nano')}"
    )

    await update.message.reply_text(text)


async def set_nano(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_mode[update.effective_user.id] = "gpt-4o-mini"
    await update.message.reply_text("Режим nano включён")


async def set_pro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_mode[update.effective_user.id] = "gpt-4o"
    await update.message.reply_text("Режим pro включён")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text

    # ===== КНОПКИ =====
    if text == "ℹ️ Что умеет бот":
        await start(update, context)
        return

    if text == "👤 Мой профиль":
        await account(update, context)
        return

    if text == "🖼 Создать изображение":
        waiting_for_image_prompt[user_id] = True
        await update.message.reply_text("Опиши изображение, которое хочешь создать 🎨")
        return

    # ===== РЕЖИМ ОЖИДАНИЯ ИЗОБРАЖЕНИЯ =====
    if waiting_for_image_prompt.get(user_id):
        waiting_for_image_prompt[user_id] = False

        await update.message.reply_text("Создаю изображение... ⏳")

        try:
            img = client.images.generate(
                model="gpt-image-1",
                prompt=text,
                size="1024x1024"
            )

            image_url = img.data[0].url

            await update.message.reply_photo(image_url)

        except Exception as e:
            await update.message.reply_text("Ошибка при создании изображения 😢")

        return

    # ===== GPT ОТВЕТ =====
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
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))


# ================= FLASK =================

@flask_app.route(f"/{TG_TOKEN}", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), telegram_app.bot)
    loop.run_until_complete(telegram_app.process_update(update))
    return "ok"


@flask_app.route("/")
def home():
    return "Bot is running"


# ================= STARTUP =================

async def setup():
    await telegram_app.initialize()
    await telegram_app.bot.set_webhook(f"{WEBHOOK_URL}/{TG_TOKEN}")

    await telegram_app.bot.set_my_commands([
        BotCommand("start", "Что умеет бот"),
        BotCommand("account", "Мой профиль"),
        BotCommand("photo", "Создать изображение")
    ])


if __name__ == "__main__":
    loop.run_until_complete(setup())
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
