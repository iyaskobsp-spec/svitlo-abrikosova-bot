import os
import asyncio
import math
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from curl_cffi.requests import AsyncSession
from dtek_client import DtekClient
from dtek_client.browser_auth import get_cleared_cookies

from telegram import Update, ReplyKeyboardMarkup
from telegram.error import Forbidden, BadRequest
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

HOUSE = "28А"

DTEK_REFRESH_MINUTES = 15
MONITOR_TICK_SECONDS = 60

DB_PATH = "/data/subscribers.db"

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
    if (
        not force
        and _auth_cache["cookies"] is not None
    ):
        return (
            _auth_cache["cookies"],
            _auth_cache["csrf"],
        )

    async with _auth_lock:
        if (
            not force
            and _auth_cache["cookies"] is not None
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

_monitor_cache = {
    "today_date": None,
    "today_slots": None,
    "tomorrow_date": None,
    "tomorrow_slots": None,
    "house_entry": None,
    "last_refresh": None,
}

def init_db():
    os.makedirs(
        os.path.dirname(DB_PATH),
        exist_ok=True,
    )

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subscribers (
                chat_id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sent_notifications (
                chat_id INTEGER NOT NULL,
                notification_key TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (
                    chat_id,
                    notification_key
                )
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_state (
                state_key TEXT PRIMARY KEY,
                state_value TEXT
            )
            """
        )

        conn.commit()

def add_subscriber(chat_id):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO subscribers (
                chat_id,
                created_at
            )
            VALUES (?, ?)
            """,
            (
                chat_id,
                datetime.now(
                    KYIV_TZ
                ).isoformat(),
            ),
        )

        conn.commit()

def remove_subscriber(chat_id):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            DELETE FROM subscribers
            WHERE chat_id = ?
            """,
            (chat_id,),
        )

        conn.execute(
            """
            DELETE FROM sent_notifications
            WHERE chat_id = ?
            """,
            (chat_id,),
        )

        conn.commit()

def get_subscribers():
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT chat_id
            FROM subscribers
            """
        ).fetchall()

    return [
        row[0]
        for row in rows
    ]


def notification_was_sent(
    chat_id,
    notification_key,
):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM sent_notifications
            WHERE chat_id = ?
              AND notification_key = ?
            """,
            (
                chat_id,
                notification_key,
            ),
        ).fetchone()

    return row is not None


def mark_notification_sent(
    chat_id,
    notification_key,
):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT OR IGNORE
            INTO sent_notifications (
                chat_id,
                notification_key,
                created_at
            )
            VALUES (?, ?, ?)
            """,
            (
                chat_id,
                notification_key,
                datetime.now(
                    KYIV_TZ
                ).isoformat(),
            ),
        )

        conn.commit()


def get_state(state_key):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT state_value
            FROM bot_state
            WHERE state_key = ?
            """,
            (state_key,),
        ).fetchone()

    if row is None:
        return None

    return row[0]


def set_state(
    state_key,
    state_value,
):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT OR REPLACE
            INTO bot_state (
                state_key,
                state_value
            )
            VALUES (?, ?)
            """,
            (
                state_key,
                state_value,
            ),
        )

        conn.commit()


async def send_notification(
    application,
    chat_id,
    text,
):
    try:
        await application.bot.send_message(
            chat_id=chat_id,
            text=text,
        )

        return True

    except (Forbidden, BadRequest):
        remove_subscriber(
            chat_id
        )

        return False

    except Exception as error:
        print(
            "SEND ERROR: "
            f"{chat_id}: "
            f"{type(error).__name__}: "
            f"{error}",
            flush=True,
        )

        return False

async def broadcast(
    application,
    text,
):
    for chat_id in get_subscribers():
        await send_notification(
            application,
            chat_id,
            text,
        )

def normalize_house(value):
    return (
        str(value)
        .strip()
        .upper()
        .replace("A", "А")
        .replace("-", "")
        .replace(" ", "")
    )

def find_house_entry(response):
    wanted = normalize_house(HOUSE)

    for house_number, entry in response.houses.items():
        if normalize_house(house_number) == wanted:
            return entry

    return None

def time_to_datetime(
    target_date,
    value,
):
    if value == "24:00":
        next_date = (
            target_date
            + timedelta(days=1)
        )

        return datetime(
            next_date.year,
            next_date.month,
            next_date.day,
            0,
            0,
            tzinfo=KYIV_TZ,
        )

    hours, minutes = map(
        int,
        value.split(":"),
    )

    return datetime(
        target_date.year,
        target_date.month,
        target_date.day,
        hours,
        minutes,
        tzinfo=KYIV_TZ,
    )

def get_outage_intervals(
    slots,
    target_date,
):
    if not slots:
        return []

    segments, segment_minutes = (
        slots_to_segments(slots)
    )

    raw_intervals = collect_intervals(
        segments,
        "off",
        segment_minutes,
    )

    result = []

    for start, end in raw_intervals:
        result.append(
            (
                time_to_datetime(
                    target_date,
                    start,
                ),
                time_to_datetime(
                    target_date,
                    end,
                ),
                start,
                end,
            )
        )

    return result

async def fetch_monitor_snapshot():
    last_error = None

    for attempt in range(2):
        session = await make_session(
            force_auth=(attempt == 1)
        )

        try:
            client = DtekClient(
                "krem",
                ajax_url=AJAX_URL,
                session=session,
                timeout=20,
            )

            response = (
                await client.get_home_num(
                    CITY,
                    STREET,
                )
            )

            now = datetime.now(KYIV_TZ)
            today = now.date()

            today_slots = None
            tomorrow_slots = None

            if response.fact is not None:
                today_ts = (
                    response.fact.today_ts
                )

                today_slots = (
                    response.fact
                    .get_group_day(
                        today_ts,
                        GROUP_ID,
                    )
                )

                tomorrow_slots = (
                    response.fact
                    .get_group_day(
                        today_ts + 86400,
                        GROUP_ID,
                    )
                )

            return {
                "today_date":
                    today,

                "today_slots":
                    today_slots,

                "tomorrow_date":
                    today + timedelta(days=1),

                "tomorrow_slots":
                    tomorrow_slots,

                "house_entry":
                    find_house_entry(
                        response
                    ),
            }

        except Exception as error:
            last_error = error

            print(
                "MONITOR DTEK ERROR: "
                f"{type(error).__name__}: "
                f"{error}",
                flush=True,
            )

            _auth_cache["cookies"] = None
            _auth_cache["csrf"] = None

        finally:
            await session.close()

    raise last_error

async def refresh_monitor_cache():
    snapshot = (
        await fetch_monitor_snapshot()
    )

    _monitor_cache.update(snapshot)

    _monitor_cache[
        "last_refresh"
    ] = datetime.now(KYIV_TZ)

async def maybe_send_emergency_alert(
    application,
):
    house_entry = (
        _monitor_cache[
            "house_entry"
        ]
    )

    if house_entry is None:
        return

    current_state = bool(
        house_entry
        .has_current_outage
    )

    current_value = (
        "1"
        if current_state
        else "0"
    )

    previous_value = get_state(
        "emergency_state"
    )

    if previous_value is None:
        set_state(
            "emergency_state",
            current_value,
        )

        if not current_state:
            return

    elif (
        previous_value
        == current_value
    ):
        return

    else:
        set_state(
            "emergency_state",
            current_value,
        )

    if current_state:
        lines = [
            "⚠️ Позапланове відключення",
            f"📍 {ADDRESS}",
            "",
            (
                "ДТЕК зараз показує "
                "поточне позапланове "
                "відключення за цією адресою."
            ),
        ]

        if house_entry.start_date:
            lines.append(
                f"Початок: "
                f"{house_entry.start_date}"
            )

        if house_entry.end_date:
            lines.append(
                f"Очікуване завершення: "
                f"{house_entry.end_date}"
            )

        await broadcast(
            application,
            "\n".join(lines),
        )

    else:
        await broadcast(
            application,
            (
                "✅ Позапланове "
                "відключення знято\n"
                f"📍 {ADDRESS}\n\n"
                "ДТЕК більше не показує "
                "поточне позапланове "
                "відключення за цією адресою."
            ),
        )

async def maybe_send_schedule_notifications(
    application,
):
    subscribers = (
        get_subscribers()
    )

    if not subscribers:
        return

    now = datetime.now(
        KYIV_TZ
    )

    schedules = [
        (
            _monitor_cache[
                "today_date"
            ],
            _monitor_cache[
                "today_slots"
            ],
        ),
        (
            _monitor_cache[
                "tomorrow_date"
            ],
            _monitor_cache[
                "tomorrow_slots"
            ],
        ),
    ]

    for target_date, slots in schedules:
        if (
            target_date is None
            or not slots
        ):
            continue

        intervals = (
            get_outage_intervals(
                slots,
                target_date,
            )
        )

        for (
            start_dt,
            end_dt,
            start_text,
            end_text,
        ) in intervals:

            until_start = (
                start_dt - now
            ).total_seconds()

            off_key = (
                f"{target_date}:"
                f"off:"
                f"{start_text}:"
                f"{end_text}"
            )

            if (
                0
                < until_start
                <= 30 * 60
            ):
                minutes_left = max(
                    1,
                    math.ceil(
                        until_start / 60
                    ),
                )

                text = (
                    "🔌 Увага\n"
                    f"Через {minutes_left} хв "
                    "за графіком буде "
                    "відключення.\n\n"
                    f"⏰ "
                    f"{start_text}–{end_text}\n"
                    f"📍 {ADDRESS}"
                )

                for chat_id in subscribers:
                    if notification_was_sent(
                        chat_id,
                        off_key,
                    ):
                        continue

                    sent = await send_notification(
                        application,
                        chat_id,
                        text,
                    )

                    if sent:
                        mark_notification_sent(
                            chat_id,
                            off_key,
                        )

            until_end = (
                end_dt - now
            ).total_seconds()

            on_key = (
                f"{target_date}:"
                f"on:"
                f"{start_text}:"
                f"{end_text}"
            )

            if (
                start_dt <= now < end_dt
                and 0
                < until_end
                <= 30 * 60
            ):
                minutes_left = max(
                    1,
                    math.ceil(
                        until_end / 60
                    ),
                )

                text = (
                    "💡 Увага\n"
                    f"Приблизно через "
                    f"{minutes_left} хв "
                    "за графіком має "
                    "з'явитися світло.\n\n"
                    f"⏰ До {end_text}\n"
                    f"📍 {ADDRESS}"
                )

                for chat_id in subscribers:
                    if notification_was_sent(
                        chat_id,
                        on_key,
                    ):
                        continue

                    sent = await send_notification(
                        application,
                        chat_id,
                        text,
                    )

                    if sent:
                        mark_notification_sent(
                            chat_id,
                            on_key,
                        )

async def monitor_loop(
    application,
):
    await asyncio.sleep(5)

    while True:
        try:
            now = datetime.now(KYIV_TZ)

            last_refresh = (
                _monitor_cache[
                    "last_refresh"
                ]
            )

            need_refresh = (
                last_refresh is None
                or
                now - last_refresh
                >= timedelta(
                    minutes=
                        DTEK_REFRESH_MINUTES
                )
            )

            if need_refresh:
                await refresh_monitor_cache()

                await maybe_send_emergency_alert(
                    application
                )

            await maybe_send_schedule_notifications(
                application
            )

        except Exception as error:
            print(
                "MONITOR ERROR: "
                f"{type(error).__name__}: "
                f"{error}",
                flush=True,
            )

        await asyncio.sleep(
            MONITOR_TICK_SECONDS
        )

async def post_init(
    application,
):
    application.bot_data[
        "monitor_task"
    ] = asyncio.create_task(
        monitor_loop(
            application
        )
    )

async def post_shutdown(
    application,
):
    task = application.bot_data.get(
        "monitor_task"
    )

    if task is None:
        return

    task.cancel()

    try:
        await task
    except asyncio.CancelledError:
        pass

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    chat_id = (
        update.effective_chat.id
    )

    add_subscriber(
        chat_id
    )

    await update.message.reply_text(
        "💡 Бот контролю відключень\n\n"
        f"📍 {ADDRESS}\n"
        f"🔌 Підчерга {GROUP_NAME}\n\n"
        "🔔 Автоматичні попередження "
        "увімкнені.\n"
        "Відписатися: /stop\n\n"
        "Обери, що показати:",
        reply_markup=keyboard,
    )

    house_entry = (
        _monitor_cache[
            "house_entry"
        ]
    )

    if (
        house_entry is not None
        and bool(
            house_entry
            .has_current_outage
        )
    ):
        await update.message.reply_text(
            "⚠️ ДТЕК зараз показує "
            "позапланове відключення "
            "за цією адресою."
        )

async def stop(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    remove_subscriber(
        update.effective_chat.id
    )

    await update.message.reply_text(
        "🔕 Автоматичні попередження "
        "вимкнені.\n\n"
        "Кнопки «Зараз / Сьогодні / "
        "Завтра» працюють як і раніше.\n"
        "Щоб знову підписатися: /start"
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

    init_db()    

    application = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    application.add_handler(
        CommandHandler(
            "stop",
            stop,
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
