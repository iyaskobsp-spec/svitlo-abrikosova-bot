import os

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


BOT_TOKEN = os.getenv("BOT_TOKEN")

ADDRESS = "Білогородка, вул. Абрикосова, 28А"


keyboard = ReplyKeyboardMarkup(
    [
        ["⚡ Зараз"],
        ["📅 Сьогодні", "📆 Завтра"],
    ],
    resize_keyboard=True,
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"💡 Бот контролю відключень\n\n"
        f"📍 {ADDRESS}\n\n"
        f"Бот запущений і працює.",
        reply_markup=keyboard,
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == "⚡ Зараз":
        await update.message.reply_text(
            "⚡ Перевірка поточного стану буде підключена на наступному кроці."
        )

    elif text == "📅 Сьогодні":
        await update.message.reply_text(
            "📅 Графік на сьогодні буде підключений на наступному кроці."
        )

    elif text == "📆 Завтра":
        await update.message.reply_text(
            "📆 Графік на завтра буде підключений на наступному кроці."
        )


def main():
    if not BOT_TOKEN:
        raise RuntimeError("Не задано змінну BOT_TOKEN")

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    print("Bot started")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
