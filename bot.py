import os

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from dtek_client import DtekClient


BOT_TOKEN = os.getenv("BOT_TOKEN")

ADDRESS = "Білогородка, вул. Абрикосова, 28А"

DTEK_AJAX_URL = "https://www.dtek-krem.com.ua/ua/ajax"

CITY_VARIANTS = [
    "с. Білогородка",
    "Білогородка",
]

HOUSE_VARIANTS = [
    "28А",
    "28а",
    "28A",
    "28a",
]


keyboard = ReplyKeyboardMarkup(
    [
        ["⚡ Зараз"],
        ["📅 Сьогодні", "📆 Завтра"],
    ],
    resize_keyboard=True,
)


async def find_dtek_address():
    last_error = None

    async with DtekClient(
        "krem",
        ajax_url=DTEK_AJAX_URL,
    ) as client:

        for city in CITY_VARIANTS:
            try:
                streets = await client.get_streets(city)
            except Exception as error:
                last_error = error
                continue

            street = next(
                (
                    item.name
                    for item in streets
                    if "абрикос" in item.name.lower()
                ),
                None,
            )

            if not street:
                continue

            for house in HOUSE_VARIANTS:
                try:
                    result = await client.get_group_by_address(
                        city=city,
                        street=street,
                        house_number=house,
                    )

                    return {
                        "result": result,
                        "city": city,
                        "street": street,
                        "house": house,
                    }

                except Exception as error:
                    last_error = error

    if last_error:
        raise last_error

    raise RuntimeError("Адресу не знайдено в базі ДТЕК")


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
        wait_message = await update.message.reply_text(
            "🔎 Перевіряю адресу в ДТЕК..."
        )

        try:
            data = await find_dtek_address()

            result = data["result"]

            group_name = getattr(
                result,
                "group_display_name",
                None,
            )

            group_id = getattr(
                result,
                "group_id",
                None,
            )

            await wait_message.edit_text(
                "✅ Адресу знайдено в ДТЕК\n\n"
                f"📍 {data['city']}, {data['street']}, {data['house']}\n"
                f"🔌 Група: {group_name or group_id or 'визначена'}\n\n"
                "Зв'язок із ДТЕК працює."
            )

        except Exception as error:
            print(
                f"DTEK ERROR: "
                f"{type(error).__name__}: {error}"
            )

            await wait_message.edit_text(
                "❌ ДТЕК поки не віддав дані.\n\n"
                f"Помилка: {type(error).__name__}\n"
                f"{error}"
            )

    elif text == "📅 Сьогодні":
        await update.message.reply_text(
            "📅 Спочатку перевіряємо з'єднання з ДТЕК через кнопку «⚡ Зараз»."
        )

    elif text == "📆 Завтра":
        await update.message.reply_text(
            "📆 Спочатку перевіряємо з'єднання з ДТЕК через кнопку «⚡ Зараз»."
        )


def main():
    if not BOT_TOKEN:
        raise RuntimeError("Не задано змінну BOT_TOKEN")

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message,
        )
    )

    print("Bot started")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
