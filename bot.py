import os
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from curl_cffi.requests import AsyncSession
from dtek_client import DtekClient
from dtek_client.browser_auth import get_cleared_cookies

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

CITY = "с. Білогородка"
STREET = "вул. Абрикосова"

GROUP_ID = "GPV4.2"
GROUP_NAME = "4.2"

BASE_URL = "https://www.dtek-krem.com.ua"
SCHEDULE_URL = f"{BASE_URL}/ua/shutdowns"
AJAX_URL = f"{BASE_URL}/ua/ajax"

KYIV_TZ = ZoneInfo("Europe/Kyiv")


keyboard = ReplyKeyboardMarkup(
    [
        ["⚡ Зараз"],
        ["📅 Сьогодні", "📆 Завтра"],
    ],
    resize_keyboard=True,
)


_auth_lock = asyncio.Lock()

_auth_cache = {
    "cookies": None,
    "csrf": None,
    "created_at": 0.0,
}


def format_date_ua(value):
    return f"{value.day}.{value.month}.{value.year}"


def status_value(status):
    value = getattr(status, "value", str(status))
    return str(value).lower()


def slots_to_segments(slots):
    numeric_keys = sorted(
        int(key)
        for key in slots.keys()
        if str(key).isdigit()
    )

    if not numeric_keys:
        return [], 30

    slot_count = max(numeric_keys)

    # У ДТЕК для цього графіка 24 часові комірки:
    # 00–01, 01–02 ... 23–24.
    #
    # FIRST / SECOND означають відповідно
    # перші або другі 30 хвилин години.
    #
    # Залишаємо також підтримку формату 48 слотів,
    # якщо ДТЕК колись його поверне.
    if slot_count <= 24:
        half_minutes = 30
    else:
        half_minutes = 15

    mapping = {
        "yes": ("on", "on"),
        "no": ("off", "off"),
        "maybe": ("maybe", "maybe"),
        "first": ("off", "on"),
        "second": ("on", "off"),
        "mfirst": ("maybe", "on"),
        "msecond": ("on", "maybe"),
    }

    segments = []

    for slot_number in range(1, slot_count + 1):
        status = slots.get(str(slot_number))

        if status is None:
            segments.extend(
                ("unknown", "unknown")
            )
            continue

        segments.extend(
            mapping.get(
                status_value(status),
                ("unknown", "unknown"),
            )
        )

    return segments, half_minutes


def minutes_to_time(total_minutes):
    if total_minutes >= 24 * 60:
        return "24:00"

    hours = total_minutes // 60
    minutes = total_minutes % 60

    return f"{hours:02d}:{minutes:02d}"


def collect_intervals(
    segments,
    wanted_state,
    segment_minutes,
):
    intervals = []
    start = None

    for index, state in enumerate(
        segments + [None]
    ):
        if (
            state == wanted_state
            and start is None
        ):
            start = index

        if (
            state != wanted_state
            and start is not None
        ):
            end = index

            intervals.append(
                (
                    minutes_to_time(
                        start * segment_minutes
                    ),
                    minutes_to_time(
                        end * segment_minutes
                    ),
                )
            )

            start = None

    return intervals


async def get_auth(force=False):
    now = (
        asyncio
        .get_running_loop()
        .time()
    )

    if (
        not force
        and _auth_cache["cookies"] is not None
        and now - _auth_cache["created_at"] < 20 * 60
    ):
        return (
            _auth_cache["cookies"],
            _auth_cache["csrf"],
        )

    async with _auth_lock:
        now = (
            asyncio
            .get_running_loop()
            .time()
        )

        if (
            not force
            and _auth_cache["cookies"] is not None
            and now - _auth_cache["created_at"] < 20 * 60
        ):
            return (
                _auth_cache["cookies"],
                _auth_cache["csrf"],
            )

        cookies, csrf_token = (
            await get_cleared_cookies(
                SCHEDULE_URL
            )
        )

        _auth_cache["cookies"] = cookies
        _auth_cache["csrf"] = csrf_token
        _auth_cache["created_at"] = now

        return cookies, csrf_token


async def make_session(
    force_auth=False
):
    cookies, csrf_token = (
        await get_auth(
            force=force_auth
        )
    )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "application/json, "
            "text/javascript, "
            "*/*; q=0.01"
        ),
        "Accept-Language":
            "uk,en;q=0.9",
        "Origin":
            BASE_URL,
        "Referer":
            SCHEDULE_URL,
        "X-Requested-With":
            "XMLHttpRequest",
        "Content-Type":
            "application/"
            "x-www-form-urlencoded; "
            "charset=UTF-8",
    }

    if csrf_token:
        headers[
            "X-CSRF-Token"
        ] = csrf_token

    return AsyncSession(
        timeout=20.0,
        headers=headers,
        cookies=cookies,
        impersonate="chrome120",
    )


async def fetch_dtek_day(
    target_date
):
    last_error = None

    for attempt in range(2):
        session = await make_session(
            force_auth=(
                attempt == 1
            )
        )

        try:
            client = DtekClient(
                "krem",
                ajax_url=AJAX_URL,
                session=session,
                timeout=20,
            )

            # Адресу та групу вже знаємо.
            # Нічого не шукаємо по списках міст,
            # вулиць чи будинків.
            response = (
                await client.get_home_num(
                    CITY,
                    STREET,
                )
            )

            if response.fact is None:
                return None, None

            today_ts = (
                response.fact.today_ts
            )

            today_date = (
                datetime.fromtimestamp(
                    today_ts,
                    tz=KYIV_TZ,
                )
                .date()
            )

            delta_days = (
                target_date
                - today_date
            ).days

            target_ts = (
                today_ts
                + delta_days * 86400
            )

            slots = (
                response.fact
                .get_group_day(
                    target_ts,
                    GROUP_ID,
                )
            )

            updated = (
                response.fact.update
                or
                response.update_timestamp
            )

            return slots, updated

        except Exception as error:
            last_error = error

            print(
                f"DTEK attempt "
                f"{attempt + 1}: "
                f"{type(error).__name__}: "
                f"{error}",
                flush=True,
            )

            _auth_cache[
                "cookies"
            ] = None

            _auth_cache[
                "csrf"
            ] = None

            _auth_cache[
                "created_at"
            ] = 0.0

        finally:
            await session.close()

    raise last_error


def format_day(
    slots,
    target_date,
    label,
    updated,
):
    title = (
        f"{label} "
        f"{format_date_ua(target_date)}"
    )

    if not slots:
        return (
            f"{title}\n"
            f"📍 {ADDRESS}\n"
            f"🔌 Підчерга {GROUP_NAME}\n\n"
            "⚪ ДТЕК ще не опублікував "
            "графік на цю дату."
        )

    segments, segment_minutes = (
        slots_to_segments(
            slots
        )
    )

    outages = collect_intervals(
        segments,
        "off",
        segment_minutes,
    )

    maybe = collect_intervals(
        segments,
        "maybe",
        segment_minutes,
    )

    lines = [
        title,
        f"📍 {ADDRESS}",
        f"🔌 Підчерга {GROUP_NAME}",
        "",
    ]

    if outages:
        lines.append(
            "🔴 Без світла:"
        )

        for start, end in outages:
            lines.append(
                f"• {start}–{end}"
            )

    else:
        lines.append(
            "🟢 Підтверджених "
            "відключень немає."
        )

    if maybe:
        lines.append("")
        lines.append(
            "🟡 Можливе відключення:"
        )

        for start, end in maybe:
            lines.append(
                f"• {start}–{end}"
            )

    if updated:
        lines.append("")
        lines.append(
            f"ДТЕК оновлено: "
            f"{updated}"
        )

    return "\n".join(lines)


def format_current(
    slots,
    updated,
):
    if not slots:
        return (
            "⚪ ДТЕК ще не опублікував "
            "графік на сьогодні."
        )

    segments, segment_minutes = (
        slots_to_segments(
            slots
        )
    )

    now = datetime.now(
        KYIV_TZ
    )

    current_minutes = (
        now.hour * 60
        + now.minute
    )

    current_index = (
        current_minutes
        // segment_minutes
    )

    if (
        current_index
        >= len(segments)
    ):
        current_index = (
            len(segments) - 1
        )

    current_state = (
        segments[
            current_index
        ]
    )

    labels = {
        "on":
            "🟢 За графіком світло є.",

        "off":
            "🔴 За графіком світла немає.",

        "maybe":
            "🟡 Зараз можливе відключення.",

        "unknown":
            "⚪ ДТЕК не дав "
            "однозначного статусу.",
    }

    lines = [
        "⚡ Зараз",
        f"📍 {ADDRESS}",
        f"🔌 Підчерга {GROUP_NAME}",
        "",
        labels.get(
            current_state,
            labels["unknown"],
        ),
    ]

    next_index = None

    for index in range(
        current_index + 1,
        len(segments),
    ):
        if (
            segments[index]
            != current_state
        ):
            next_index = index
            break

    if next_index is not None:
        next_time = (
            minutes_to_time(
                next_index
                * segment_minutes
            )
        )

        next_state = (
            segments[
                next_index
            ]
        )

        next_labels = {
            "on":
                "світло має бути",

            "off":
                "очікується відключення",

            "maybe":
                "можливе відключення",

            "unknown":
                "статус невизначений",
        }

        lines.append(
            f"Наступна зміна о "
            f"{next_time}: "
            f"{next_labels.get(next_state)}."
        )

    lines.append("")
    lines.append(
        f"Перевірено: "
        f"{now:%H:%M}"
    )

    if updated:
        lines.append(
            f"ДТЕК оновлено: "
            f"{updated}"
        )

    return "\n".join(lines)


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "💡 Бот контролю відключень\n\n"
        f"📍 {ADDRESS}\n"
        f"🔌 Підчерга {GROUP_NAME}\n\n"
        "Обери, що показати:",
        reply_markup=keyboard,
    )


async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    text = update.message.text

    now = datetime.now(
        KYIV_TZ
    )

    if text == "⚡ Зараз":
        message = (
            await update.message
            .reply_text(
                "🔎 Перевіряю ДТЕК..."
            )
        )

        try:
            slots, updated = (
                await fetch_dtek_day(
                    now.date()
                )
            )

            await message.edit_text(
                format_current(
                    slots,
                    updated,
                )
            )

        except Exception as error:
            print(
                f"STATUS ERROR: "
                f"{type(error).__name__}: "
                f"{error}",
                flush=True,
            )

            await message.edit_text(
                "❌ Не вдалося отримати "
                "актуальні дані ДТЕК."
            )

    elif text == "📅 Сьогодні":
        message = (
            await update.message
            .reply_text(
                "🔎 Завантажую "
                "графік ДТЕК..."
            )
        )

        try:
            slots, updated = (
                await fetch_dtek_day(
                    now.date()
                )
            )

            await message.edit_text(
                format_day(
                    slots,
                    now.date(),
                    "📅 Графік на сьогодні",
                    updated,
                )
            )

        except Exception as error:
            print(
                f"TODAY ERROR: "
                f"{type(error).__name__}: "
                f"{error}",
                flush=True,
            )

            await message.edit_text(
                "❌ Не вдалося отримати "
                "графік ДТЕК."
            )

    elif text == "📆 Завтра":
        message = (
            await update.message
            .reply_text(
                "🔎 Завантажую "
                "графік ДТЕК..."
            )
        )

        tomorrow = (
            now.date()
            + timedelta(days=1)
        )

        try:
            slots, updated = (
                await fetch_dtek_day(
                    tomorrow
                )
            )

            await message.edit_text(
                format_day(
                    slots,
                    tomorrow,
                    "📆 Графік на завтра",
                    updated,
                )
            )

        except Exception as error:
            print(
                f"TOMORROW ERROR: "
                f"{type(error).__name__}: "
                f"{error}",
                flush=True,
            )

            await message.edit_text(
                "❌ Не вдалося отримати "
                "графік ДТЕК."
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
