import os
import asyncio
from datetime import datetime
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

BASE_URL = "https://www.dtek-krem.com.ua"
SCHEDULE_URL = f"{BASE_URL}/ua/shutdowns"
AJAX_URL = f"{BASE_URL}/ua/ajax"

CITY_VARIANTS = [
    "с. Білогородка",
    "Білогородка",
]

HOUSE_VARIANTS = [
    "28А",
    "28а",
    "28A",
    "28a",
    "28-А",
    "28-а",
]

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

_resolved_address = None


def status_value(status):
    value = getattr(status, "value", str(status))
    return str(value).lower()


def slot_to_quarters(status):
    value = status_value(status)

    mapping = {
        "yes": ("on", "on"),
        "no": ("off", "off"),
        "maybe": ("maybe", "maybe"),
        "first": ("off", "on"),
        "second": ("on", "off"),
        "mfirst": ("maybe", "on"),
        "msecond": ("on", "maybe"),
    }

    return mapping.get(value, ("unknown", "unknown"))


def slots_to_quarters(slots):
    quarters = []

    for slot_number in range(1, 49):
        status = slots.get(str(slot_number))

        if status is None:
            quarters.extend(("unknown", "unknown"))
        else:
            quarters.extend(slot_to_quarters(status))

    return quarters


def minutes_to_time(total_minutes):
    if total_minutes >= 24 * 60:
        return "24:00"

    hours = total_minutes // 60
    minutes = total_minutes % 60

    return f"{hours:02d}:{minutes:02d}"


def collect_intervals(quarters, wanted_state):
    intervals = []
    start = None

    for index, state in enumerate(quarters + [None]):

        if state == wanted_state and start is None:
            start = index

        if state != wanted_state and start is not None:
            end = index

            intervals.append(
                f"{minutes_to_time(start * 15)}–"
                f"{minutes_to_time(end * 15)}"
            )

            start = None

    return intervals


def format_schedule(slots, title):
    if not slots:
        return (
            f"{title}\n\n"
            "ДТЕК ще не опублікував підтверджений "
            "графік на цей день."
        )

    quarters = slots_to_quarters(slots)

    outage = collect_intervals(
        quarters,
        "off",
    )

    maybe = collect_intervals(
        quarters,
        "maybe",
    )

    lines = [
        title,
        f"📍 {ADDRESS}",
        "",
    ]

    if outage:
        lines.append("🔴 Без світла:")

        lines.extend(
            f"• {item}"
            for item in outage
        )

    else:
        lines.append(
            "🟢 Підтверджених відключень немає."
        )

    if maybe:
        lines.append("")
        lines.append(
            "🟡 Можливе відключення:"
        )

        lines.extend(
            f"• {item}"
            for item in maybe
        )

    return "\n".join(lines)


def format_current_status(slots):
    if not slots:
        return (
            "⚠️ ДТЕК поки не опублікував "
            "підтверджений графік на сьогодні."
        )

    quarters = slots_to_quarters(slots)

    now = datetime.now(KYIV_TZ)

    current_index = (
        now.hour * 4
        + now.minute // 15
    )

    current_state = quarters[
        current_index
    ]

    labels = {
        "on":
            "🟢 За графіком світло має бути.",

        "off":
            "🔴 За графіком зараз має бути "
            "відключення.",

        "maybe":
            "🟡 Зараз можливе відключення.",

        "unknown":
            "⚪ ДТЕК не дав однозначного "
            "статусу на цей час.",
    }

    next_index = None

    for index in range(
        current_index + 1,
        len(quarters),
    ):
        if quarters[index] != current_state:
            next_index = index
            break

    lines = [
        "⚡ Зараз",
        f"📍 {ADDRESS}",
        "",
        labels.get(
            current_state,
            labels["unknown"],
        ),
    ]

    if next_index is not None:

        next_time = minutes_to_time(
            next_index * 15
        )

        next_state = quarters[
            next_index
        ]

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
        f"Оновлено: {now:%H:%M}"
    )

    return "\n".join(lines)


async def get_auth(force=False):

    now = (
        asyncio
        .get_running_loop()
        .time()
    )

    if (
        not force
        and _auth_cache["cookies"]
        is not None
        and
        now
        - _auth_cache["created_at"]
        < 20 * 60
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
            and
            _auth_cache["cookies"]
            is not None
            and
            now
            - _auth_cache["created_at"]
            < 20 * 60
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

        _auth_cache[
            "cookies"
        ] = cookies

        _auth_cache[
            "csrf"
        ] = csrf_token

        _auth_cache[
            "created_at"
        ] = now

        return (
            cookies,
            csrf_token,
        )


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
            "Chrome/120.0.0.0 "
            "Safari/537.36"
        ),

        "Accept":
            "application/json, "
            "text/javascript, "
            "*/*; q=0.01",

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


async def resolve_address(client):

    global _resolved_address

    if _resolved_address is not None:
        return _resolved_address

    last_error = None

    for city in CITY_VARIANTS:

        try:
            streets = (
                await client
                .get_streets(city)
            )

        except Exception as error:
            last_error = error
            continue

        matching_streets = [
            street.name
            for street in streets
            if "абрикос"
            in street.name.lower()
        ]

        for street in matching_streets:

            for house in HOUSE_VARIANTS:

                try:
                    result = (
                        await client
                        .get_group_by_address(
                            city=city,
                            street=street,
                            house_number=house,
                        )
                    )

                    _resolved_address = {
                        "city":
                            city,

                        "street":
                            street,

                        "house":
                            house,

                        "group":
                            (
                                result
                                .group_display_name

                                or

                                result
                                .group_id
                            ),
                    }

                    return (
                        _resolved_address
                    )

                except Exception as error:
                    last_error = error

    if last_error:
        raise last_error

    raise RuntimeError(
        "ДТЕК не знайшов адресу "
        "Білогородка, "
        "Абрикосова, 28А"
    )


async def fetch_dtek(mode):

    last_error = None

    for attempt in range(2):

        session = (
            await make_session(
                force_auth=(
                    attempt == 1
                )
            )
        )

        try:
            client = DtekClient(
                "krem",
                ajax_url=AJAX_URL,
                session=session,
                timeout=20,
            )

            address = (
                await resolve_address(
                    client
                )
            )

            if mode == "group":
                return address

            if mode == "today":

                return (
                    await client
                    .get_today_schedule(
                        address["city"],
                        address["street"],
                        address["house"],
                    )
                )

            if mode == "tomorrow":

                return (
                    await client
                    .get_tomorrow_schedule(
                        address["city"],
                        address["street"],
                        address["house"],
                    )
                )

            raise ValueError(
                f"Невідомий режим: "
                f"{mode}"
            )

        except Exception as error:

            last_error = error

            print(
                f"DTEK attempt "
                f"{attempt + 1} error: "
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


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        "💡 Бот контролю відключень\n\n"
        f"📍 {ADDRESS}\n\n"
        "Обери, що показати:",
        reply_markup=keyboard,
    )


async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    text = update.message.text

    if text == "⚡ Зараз":

        message = (
            await update.message
            .reply_text(
                "🔎 Перевіряю ДТЕК..."
            )
        )

        try:
            slots = await fetch_dtek(
                "today"
            )

            await message.edit_text(
                format_current_status(
                    slots
                )
            )

        except Exception as error:

            print(
                "STATUS ERROR: "
                f"{type(error).__name__}: "
                f"{error}",
                flush=True,
            )

            await message.edit_text(
                "❌ Не вдалося отримати "
                "дані ДТЕК.\n"
                "Спробуй ще раз "
                "через хвилину."
            )

    elif text == "📅 Сьогодні":

        message = (
            await update.message
            .reply_text(
                "🔎 Завантажую графік..."
            )
        )

        try:
            slots = await fetch_dtek(
                "today"
            )

            await message.edit_text(
                format_schedule(
                    slots,
                    "📅 Графік на сьогодні",
                )
            )

        except Exception as error:

            print(
                "TODAY ERROR: "
                f"{type(error).__name__}: "
                f"{error}",
                flush=True,
            )

            await message.edit_text(
                "❌ Не вдалося отримати "
                "графік ДТЕК.\n"
                "Спробуй ще раз "
                "через хвилину."
            )

    elif text == "📆 Завтра":

        message = (
            await update.message
            .reply_text(
                "🔎 Завантажую графік..."
            )
        )

        try:
            slots = await fetch_dtek(
                "tomorrow"
            )

            await message.edit_text(
                format_schedule(
                    slots,
                    "📆 Графік на завтра",
                )
            )

        except Exception as error:

            print(
                "TOMORROW ERROR: "
                f"{type(error).__name__}: "
                f"{error}",
                flush=True,
            )

            await message.edit_text(
                "❌ Не вдалося отримати "
                "графік ДТЕК.\n"
                "Спробуй ще раз "
                "через хвилину."
            )


def main():

    if not BOT_TOKEN:
        raise RuntimeError(
            "Не задано змінну "
            "BOT_TOKEN"
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
        allowed_updates=
            Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
