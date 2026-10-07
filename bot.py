import os
import re
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

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
GROUP = "4.2"

KYIV_TZ = ZoneInfo("Europe/Kyiv")


keyboard = ReplyKeyboardMarkup(
    [
        ["⚡ Зараз"],
        ["📅 Сьогодні", "📆 Завтра"],
    ],
    resize_keyboard=True,
)


def time_to_minutes(value):
    hours, minutes = map(int, value.split(":"))
    return hours * 60 + minutes


def load_schedule(target_date):
    url = (
        "https://alerts.org.ua/"
        f"kyivska-oblast/{target_date.isoformat()}.html"
    )

    response = requests.get(
        url,
        timeout=20,
        headers={
            "User-Agent": (
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/120 Safari/537.36"
            )
        },
    )

    if response.status_code == 404:
        return None

    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    text = soup.get_text("\n", strip=True)

    group_match = re.search(
        rf"Група\s+{re.escape(GROUP)}"
        r"(.*?)"
        r"(?=Група\s+\d+\.\d+|Отримуй|$)",
        text,
        re.DOTALL,
    )

    if not group_match:
        return None

    group_text = group_match.group(1)

    intervals = re.findall(
        r"(\d{2}:\d{2})\s*-\s*"
        r"(\d{2}:\d{2})\s*"
        r"(ON|OFF)",
        group_text,
    )

    update_match = re.search(
        r"Оновлено:\s*([^\n]+)",
        text,
    )

    updated = (
        update_match.group(1).strip()
        if update_match
        else None
    )

    result = []

    for start, end, status in intervals:
        result.append(
            {
                "start": start,
                "end": end,
                "status": status,
            }
        )

    return {
        "intervals": result,
        "updated": updated,
    }


async def get_schedule(target_date):
    return await asyncio.to_thread(
        load_schedule,
        target_date,
    )


def format_day(schedule, title):
    if not schedule or not schedule["intervals"]:
        return (
            f"{title}\n"
            f"📍 {ADDRESS}\n"
            f"🔌 Підчерга {GROUP}\n\n"
            "⚪ Графік ще не опублікований."
        )

    off_periods = [
        item
        for item in schedule["intervals"]
        if item["status"] == "OFF"
    ]

    lines = [
        title,
        f"📍 {ADDRESS}",
        f"🔌 Підчерга {GROUP}",
        "",
    ]

    if not off_periods:
        lines.append(
            "🟢 Планових відключень немає."
        )
    else:
        lines.append("🔴 Без світла:")

        for item in off_periods:
            lines.append(
                f"• {item['start']}–{item['end']}"
            )

    if schedule["updated"]:
        lines.append("")
        lines.append(
            f"Оновлення джерела: "
            f"{schedule['updated']}"
        )

    return "\n".join(lines)


def format_current(schedule):
    if not schedule or not schedule["intervals"]:
        return (
            "⚪ На сьогодні графік "
            "ще не опублікований."
        )

    now = datetime.now(KYIV_TZ)

    current_minutes = (
        now.hour * 60
        + now.minute
    )

    current = None

    for item in schedule["intervals"]:
        start = time_to_minutes(
            item["start"]
        )

        end = time_to_minutes(
            item["end"]
        )

        if (
            start
            <= current_minutes
            < end
        ):
            current = item
            break

    lines = [
        "⚡ Зараз",
        f"📍 {ADDRESS}",
        f"🔌 Підчерга {GROUP}",
        "",
    ]

    if current is None:
        lines.append(
            "⚪ Немає даних "
            "для поточного часу."
        )

    elif current["status"] == "OFF":
        lines.append(
            "🔴 За графіком світла немає."
        )

        lines.append(
            f"💡 Очікуване включення: "
            f"{current['end']}"
        )

    else:
        lines.append(
            "🟢 За графіком світло є."
        )

        next_off = None

        for item in schedule["intervals"]:
            if (
                item["status"] == "OFF"
                and
                time_to_minutes(
                    item["start"]
                ) > current_minutes
            ):
                next_off = item
                break

        if next_off:
            lines.append(
                f"🔌 Наступне відключення: "
                f"{next_off['start']}–"
                f"{next_off['end']}"
            )
        else:
            lines.append(
                "✅ До кінця дня "
                "відключень немає."
            )

    lines.append("")
    lines.append(
        f"Перевірено: {now:%H:%M}"
    )

    if schedule["updated"]:
        lines.append(
            f"Дані оновлено: "
            f"{schedule['updated']}"
        )

    return "\n".join(lines)


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "💡 Бот контролю відключень\n\n"
        f"📍 {ADDRESS}\n"
        f"🔌 Підчерга {GROUP}\n\n"
        "Обери, що показати:",
        reply_markup=keyboard,
    )


async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    text = update.message.text

    now = datetime.now(KYIV_TZ)

    if text == "⚡ Зараз":
        message = await update.message.reply_text(
            "🔎 Перевіряю графік..."
        )

        try:
            schedule = await get_schedule(
                now.date()
            )

            await message.edit_text(
                format_current(schedule)
            )

        except Exception as error:
            print(
                f"STATUS ERROR: {error}",
                flush=True,
            )

            await message.edit_text(
                "❌ Не вдалося отримати "
                "актуальний графік."
            )

    elif text == "📅 Сьогодні":
        message = await update.message.reply_text(
            "🔎 Завантажую графік..."
        )

        try:
            schedule = await get_schedule(
                now.date()
            )

            await message.edit_text(
                format_day(
                    schedule,
                    "📅 Графік на сьогодні",
                )
            )

        except Exception as error:
            print(
                f"TODAY ERROR: {error}",
                flush=True,
            )

            await message.edit_text(
                "❌ Не вдалося отримати "
                "графік."
            )

    elif text == "📆 Завтра":
        message = await update.message.reply_text(
            "🔎 Завантажую графік..."
        )

        try:
            tomorrow = (
                now.date()
                + timedelta(days=1)
            )

            schedule = await get_schedule(
                tomorrow
            )

            await message.edit_text(
                format_day(
                    schedule,
                    "📆 Графік на завтра",
                )
            )

        except Exception as error:
            print(
                f"TOMORROW ERROR: {error}",
                flush=True,
            )

            await message.edit_text(
                "⚪ Графік на завтра "
                "ще не опублікований."
            )


def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "Не задано BOT_TOKEN"
        )

    application = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_message,
        )
    )

    print(
        "Bot started",
        flush=True,
    )

    application.run_polling(
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
