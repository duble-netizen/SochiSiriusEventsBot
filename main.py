import asyncio
import html
import json
import logging
import os
import re
from datetime import datetime, timedelta, date, time
from pathlib import Path
from urllib.parse import urljoin

import requests
from fastapi import FastAPI
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery,
    BufferedInputFile,
)
from zoneinfo import ZoneInfo
import uvicorn

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sochi_events_bot")

BOT_VERSION = "3.0.0-OUTLOOK-ICS"
BOT_TOKEN = os.getenv("BOT_TOKEN")
OUTLOOK_ICS_URL = os.getenv("OUTLOOK_ICS_URL")
CHANNEL_ID = os.getenv("CHANNEL_ID", "@SochiSiriusEvents")
MOSCOW_TZ = ZoneInfo("Europe/Moscow")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not OUTLOOK_ICS_URL:
    raise RuntimeError("OUTLOOK_ICS_URL is not set")

app = FastAPI()
bot = Bot(BOT_TOKEN)
dp = Dispatcher()

EVENTS = []
LAST_UPDATE = None
SUBSCRIBERS_FILE = os.getenv("SUBSCRIBERS_FILE", "subscribers.json")
PUBLICATIONS_FILE = os.getenv("PUBLICATIONS_FILE", "daily_publications.json")
SUBSCRIBERS = set()
HIGHLIGHT_PROPOSALS = {}
HIGHLIGHT_SENDING = False
AUTO_HIGHLIGHT_ENABLED = True
HEADERS = {"User-Agent": "SochiSiriusEventsBot/3.0"}

AREA_ORDER = ["Сочи", "Сириус", "Красная Поляна"]
TYPE_ORDER = ["Разовые", "Постоянные"]

GENERIC_WORDS = {
    "афиша", "программа", "расписание", "мероприятия",
    "все мероприятия", "ближайшие мероприятия",
}

SERVICE_WORDS = (
    "экскурси", "посещение", "прогулка", "прокат", "аренда",
    "трансфер", "услуга", "билет", "тур", "мастер-класс",
)
PERMANENT_WORDS = (
    "каждый день", "ежедневно", "еженедельно", "регулярно",
    "постоянно", "экскурси", "посещение", "программа дня",
)

WINDOW_TZ_MAP = {
    "Russian Standard Time": "Europe/Moscow",
    "Russia Time Zone 3": "Europe/Moscow",
    "W. Europe Standard Time": "Europe/Berlin",
    "Russian Standard Time 2": "Europe/Moscow",
    "Europe/Moscow": "Europe/Moscow",
    "Europe/Berlin": "Europe/Berlin",
}

MONTHS_RU = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}


def clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def ics_unescape(value):
    value = str(value or "")
    value = value.replace("\\n", "\n").replace("\\N", "\n")
    value = value.replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")
    return clean(value)


def unfold_ics(text):
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    result = []
    for line in lines:
        if line.startswith((" ", "\t")) and result:
            result[-1] += line[1:]
        else:
            result.append(line)
    return result


def parse_ics_property(line):
    if ":" not in line:
        return "", {}, ""
    left, value = line.split(":", 1)
    bits = left.split(";")
    name = bits[0].upper()
    params = {}
    for bit in bits[1:]:
        if "=" in bit:
            k, v = bit.split("=", 1)
            params[k.upper()] = v.strip('"')
    return name, params, value


def parse_ics_datetime(value, params=None):
    params = params or {}
    value = value.strip()
    if not value:
        return None

    if len(value) == 8 and value.isdigit():
        try:
            return datetime.strptime(value, "%Y%m%d").replace(tzinfo=MOSCOW_TZ)
        except ValueError:
            return None

    is_utc = value.endswith("Z")
    raw = value[:-1] if is_utc else value
    fmt = "%Y%m%dT%H%M%S" if len(raw) >= 15 else "%Y%m%dT%H%M"
    try:
        dt = datetime.strptime(raw, fmt)
    except ValueError:
        return None

    if is_utc:
        return dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(MOSCOW_TZ)

    tz_name = params.get("TZID", "")
    tz_name = WINDOW_TZ_MAP.get(tz_name, tz_name)
    try:
        tz = ZoneInfo(tz_name) if tz_name else MOSCOW_TZ
    except Exception:
        tz = MOSCOW_TZ
    return dt.replace(tzinfo=tz).astimezone(MOSCOW_TZ)


def parse_rrule(value):
    result = {}
    for part in str(value or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            result[k.upper()] = v
    return result


def expand_event(base, now):
    """Expand common Outlook RRULE forms for the next 90 days."""
    rule = base.get("rrule")
    if not rule:
        return [base]

    out = []
    start = base["date"]
    until = rule.get("UNTIL")
    until_dt = parse_ics_datetime(until) if until else now + timedelta(days=90)
    until_dt = min(until_dt or now + timedelta(days=90), now + timedelta(days=90))
    count = int(rule.get("COUNT", "1000") or 1000)
    freq = rule.get("FREQ", "").upper()
    interval = max(1, int(rule.get("INTERVAL", "1") or 1))
    byday = [x for x in rule.get("BYDAY", "").split(",") if x]

    cur = start
    generated = 0
    while cur <= until_dt and generated < count and len(out) < 300:
        if cur >= now - timedelta(days=1):
            if freq == "WEEKLY" and byday:
                weekday_map = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
                target_days = {weekday_map.get(x[-2:]) for x in byday}
                if cur.weekday() in target_days:
                    out.append(dict(base, date=cur))
                    generated += 1
            else:
                out.append(dict(base, date=cur))
                generated += 1

        if freq == "DAILY":
            cur += timedelta(days=interval)
        elif freq == "WEEKLY":
            cur += timedelta(days=interval if not byday else 1)
        elif freq == "MONTHLY":
            month = cur.month - 1 + interval
            year = cur.year + month // 12
            month = month % 12 + 1
            day = min(cur.day, [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
            cur = cur.replace(year=year, month=month, day=day)
        else:
            return [base]
    return out or [base]


def extract_urls(text):
    return re.findall(r"https?://[^\s<>\]\)\"']+", str(text or ""))


def infer_area(text):
    low = clean(text).lower()
    if any(x in low for x in ("сириус", "федеральная территория", "университет сириус", "олимпийский парк", "автодром сириус")):
        return "Сириус"
    if any(x in low for x in ("красная поляна", "эсто-садок", "розa хутор", "роза хутор", "газпром", "горки город", "горный кластер")):
        return "Красная Поляна"
    return "Сочи"


def is_cancelled(event):
    return str(event.get("status", "")).upper() == "CANCELLED" or "отмен" in clean(event.get("description", "")).lower()


def is_generic_title(title):
    low = clean(title).lower()
    if low in GENERIC_WORDS:
        return True
    return any(low.startswith(x + ":") or low == x for x in GENERIC_WORDS)


def event_type(title, description=""):
    text = f"{title} {description}".lower()
    if any(x in text for x in PERMANENT_WORDS):
        return "Постоянные"
    return "Разовые"


def category_for(title, description=""):
    text = f"{title} {description}".lower()
    if any(x in text for x in ("спорт", "матч", "турнир", "чемпионат", "марафон", "заплыв", "гонка", "футбол", "хоккей", "теннис", "бокс")):
        return "Спорт"
    if any(x in text for x in ("выставк", "экспозиц")):
        return "Выставка"
    if any(x in text for x in ("лекци", "форум", "конференци", "встреча", "семинар")):
        return "Лекция"
    if any(x in text for x in ("детск", "семейн", "ребен", "аниматор")):
        return "Детское"
    if any(x in text for x in ("театр", "спектакл", "балет", "опера", "постановк")):
        return "Театр"
    if any(x in text for x in ("концерт", "музык", "оркестр", "симфони", "певец", "певиц")):
        return "Концерт"
    if any(x in text for x in ("фестивал", "праздник", "ярмарк")):
        return "Фестиваль"
    return "Другое"


def parse_ics_events(text):
    lines = unfold_ics(text)
    raw_events = []
    current = None
    in_event = False

    for line in lines:
        if line.upper() == "BEGIN:VEVENT":
            current = {}
            in_event = True
            continue
        if line.upper() == "END:VEVENT":
            if current:
                raw_events.append(current)
            current = None
            in_event = False
            continue
        if not in_event:
            continue

        name, params, value = parse_ics_property(line)
        if not name:
            continue
        value = ics_unescape(value)

        if name in {"DTSTART", "DTEND"}:
            current[name] = (value, params)
        elif name == "ATTACH":
            current.setdefault("attachments", []).append((value, params))
        else:
            current[name] = value

    now = datetime.now(MOSCOW_TZ)
    result = []

    for raw in raw_events:
        start = parse_ics_datetime(*(raw.get("DTSTART", ("", {}))))
        if not start:
            continue
        title = clean(raw.get("SUMMARY", "Без названия"))
        if not title or is_generic_title(title):
            continue

        desc = clean(raw.get("DESCRIPTION", ""))
        location = clean(raw.get("LOCATION", ""))
        url = clean(raw.get("URL", ""))

        urls = extract_urls(desc)
        if not url and urls:
            url = urls[0]

        image_url = ""
        for attachment, params in raw.get("attachments", []):
            if "image" in params.get("FMTTYPE", "").lower() or re.search(r"\.(jpg|jpeg|png|webp)(?:\?|$)", attachment, re.I):
                image_url = attachment
                break

        base = {
            "uid": clean(raw.get("UID", "")),
            "title": title,
            "date": start,
            "end": parse_ics_datetime(*(raw.get("DTEND", ("", {})))) if raw.get("DTEND") else None,
            "location": location,
            "description": desc,
            "url": url,
            "image_url": image_url,
            "status": clean(raw.get("STATUS", "")),
            "rrule": parse_rrule(raw.get("RRULE", "")) if raw.get("RRULE") else None,
        }

        if base["rrule"]:
            occurrences = expand_event(base, now)
        else:
            occurrences = [base]

        for event in occurrences:
            combined = f"{event['title']} {event['location']} {event['description']}"
            event["area"] = infer_area(combined)
            event["type"] = event_type(event["title"], event["description"])
            event["category"] = category_for(event["title"], event["description"])
            event["free"] = bool(re.search(r"\bбесплатн\w*|\b0\s*(?:₽|руб)", combined, re.I))
            event["official"] = True
            event["cancelled"] = is_cancelled(event)
            result.append(event)

    # Deduplicate occurrences from duplicate calendar entries.
    unique = {}
    for event in result:
        key = (
            event.get("uid") or "",
            event["date"].astimezone(MOSCOW_TZ).strftime("%Y-%m-%d %H:%M"),
            re.sub(r"\W+", "", event["title"].lower(), flags=re.UNICODE),
        )
        if key in unique:
            # Keep the richer record.
            old = unique[key]
            if len(event.get("description", "")) > len(old.get("description", "")):
                unique[key] = event
        else:
            unique[key] = event

    return sorted(unique.values(), key=lambda x: x["date"])


def load_calendar():
    response = requests.get(OUTLOOK_ICS_URL, headers=HEADERS, timeout=30)
    response.raise_for_status()
    content = response.content.decode("utf-8-sig", errors="replace")
    return parse_ics_events(content)


async def collect_events():
    global EVENTS, LAST_UPDATE
    try:
        events = await asyncio.to_thread(load_calendar)
        EVENTS = events
        LAST_UPDATE = datetime.now(MOSCOW_TZ)
        logger.info("Outlook ICS: найдено %s событий", len(EVENTS))
    except Exception:
        logger.exception("Не удалось загрузить Outlook ICS")


def display_location(event):
    return event.get("location") or event.get("area") or "Место не указано"


def normalized_title(title):
    return re.sub(r"[^a-zа-яё0-9]+", "", clean(title).lower(), flags=re.I)


def event_text(event, include_status=True):
    dt = event["date"].astimezone(MOSCOW_TZ)
    line = f"📅 {dt.strftime('%d.%m.%Y')} {dt.strftime('%H:%M')}\n"
    location = display_location(event)
    text = f"<b>{html.escape(event['title'])}</b>\n{line}📍 {html.escape(location)}"
    if event.get("category"):
        text += f"\n🏷 {html.escape(event['category'])}"
    if include_status and event.get("cancelled"):
        text += "\n❌ ОТМЕНЕНО"
    if event.get("url"):
        text += f'\n🔗 <a href="{html.escape(event["url"], quote=True)}">Источник / страница события</a>'
    return text


def filter_event_list(events, user_id=None):
    if user_id is None:
        return events
    f = get_filters(user_id)
    return [
        e for e in events
        if e["area"] in f["areas"]
        and e["type"] in f["types"]
        and (not f["sports_only"] or e["category"] == "Спорт")
        and (not f["free_only"] or e["free"])
    ]


def get_filters(user_id):
    if user_id not in FILTERS:
        FILTERS[user_id] = {
            "areas": set(AREA_ORDER),
            "types": set(TYPE_ORDER),
            "sports_only": False,
            "free_only": False,
        }
    return FILTERS[user_id]


FILTERS = {}


def menu(user_id):
    f = get_filters(user_id)
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Сегодня"), KeyboardButton(text="Завтра"), KeyboardButton(text="7 дней")],
            [KeyboardButton(text=("✓ " if "Сочи" in f["areas"] else "□ ") + "Сочи"),
             KeyboardButton(text=("✓ " if "Сириус" in f["areas"] else "□ ") + "Сириус"),
             KeyboardButton(text=("✓ " if "Красная Поляна" in f["areas"] else "□ ") + "Красная Поляна")],
            [KeyboardButton(text=("✓ " if "Разовые" in f["types"] else "□ ") + "Разовые"),
             KeyboardButton(text=("✓ " if "Постоянные" in f["types"] else "□ ") + "Постоянные")],
            [KeyboardButton(text=("✓ " if f["sports_only"] else "□ ") + "Спорт"),
             KeyboardButton(text=("✓ " if f["free_only"] else "□ ") + "Бесплатно")],
        ],
        resize_keyboard=True,
    )


def get_events_for_day(day):
    return [e for e in EVENTS if e["date"].astimezone(MOSCOW_TZ).date() == day and not e.get("cancelled")]


async def send_event_list(message, events, title):
    events = filter_event_list(events, message.from_user.id)
    events = sorted(events, key=lambda e: e["date"])[:60]
    if not events:
        await message.answer(f"<b>{html.escape(title)}</b>\n\nМероприятий не найдено.", parse_mode="HTML", reply_markup=menu(message.from_user.id))
        return

    chunks = []
    current = f"<b>{html.escape(title)}</b>\nНайдено: {len(events)}\n\n"
    for event in events:
        block = event_text(event)
        if len(current) + len(block) + 10 > 3800:
            chunks.append(current)
            current = ""
        current += block + "\n\n────────────\n\n"
    if current:
        chunks.append(current)

    for chunk in chunks:
        await message.answer(chunk, parse_mode="HTML", reply_markup=menu(message.from_user.id), disable_web_page_preview=True)


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data):
    try:
        Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        logger.exception("Не удалось сохранить %s", path)


def load_subscribers():
    global SUBSCRIBERS
    SUBSCRIBERS = {int(x) for x in load_json(SUBSCRIBERS_FILE, []) if str(x).lstrip("-").isdigit()}


def save_subscribers():
    save_json(SUBSCRIBERS_FILE, sorted(SUBSCRIBERS))


def publication_sent(kind, day):
    data = load_json(PUBLICATIONS_FILE, {})
    return data.get(f"{kind}:{day.isoformat()}") is True


def mark_publication(kind, day):
    data = load_json(PUBLICATIONS_FILE, {})
    data[f"{kind}:{day.isoformat()}"] = True
    save_json(PUBLICATIONS_FILE, data)


def significance_score(event):
    title = clean(event["title"]).lower()
    score = 0
    if event["cancelled"]:
        return -10000
    if event["type"] == "Постоянные":
        score -= 30
    if any(w in title for w in SERVICE_WORDS):
        score -= 20
    for word, points in {
        "фестиваль": 18, "концерт": 16, "премьера": 18, "финал": 18,
        "чемпионат": 17, "турнир": 14, "спектакль": 15, "театр": 13,
        "балет": 14, "опера": 14, "шоу": 14, "марафон": 14,
        "кубок": 13, "выставка": 11, "открытие": 13, "праздник": 12,
        "оркестр": 12, "симфони": 12,
    }.items():
        if word in title:
            score += points
    if event["category"] == "Спорт":
        score += 5
    if event["area"] == "Сириус":
        score += 2
    if event["free"]:
        score += 1
    if event.get("image_url"):
        score += 2
    today = datetime.now(MOSCOW_TZ).date()
    delta = (event["date"].astimezone(MOSCOW_TZ).date() - today).days
    if delta == 0:
        score += 15
        if event["date"].astimezone(MOSCOW_TZ) < datetime.now(MOSCOW_TZ):
            score -= 20
    elif delta == 1:
        score += 6
    return score


def highlight_candidates():
    today = datetime.now(MOSCOW_TZ).date()
    pool = [
        e for e in EVENTS
        if e["date"].astimezone(MOSCOW_TZ).date() == today
        and not e["cancelled"]
        and e["date"] > datetime.now(MOSCOW_TZ)
        and e["type"] == "Разовые"
        and not any(w in clean(e["title"]).lower() for w in SERVICE_WORDS)
    ]
    pool.sort(key=significance_score, reverse=True)

    result = []
    seen = set()
    for e in pool:
        key = normalized_title(e["title"])
        if key in seen:
            continue
        result.append(e)
        seen.add(key)
        if len(result) == 3:
            break
    return result


def proposal_text(event, number):
    dt = event["date"].astimezone(MOSCOW_TZ)
    text = (
        f"<b>⭐ Вариант {number}</b>\n\n"
        f"<b>{html.escape(event['title'])}</b>\n"
        f"📅 {dt.strftime('%d.%m.%Y, %H:%M')}\n"
        f"📍 {html.escape(display_location(event))}\n"
    )
    if event.get("url"):
        text += f'\n🔗 <a href="{html.escape(event["url"], quote=True)}">Официальная страница / источник</a>'
    return text


def fetch_image_sync(event):
    if event.get("image_url"):
        url = event["image_url"]
    elif event.get("url"):
        try:
            from bs4 import BeautifulSoup
            r = requests.get(event["url"], headers=HEADERS, timeout=15)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            tag = soup.find("meta", attrs={"property": "og:image"}) or soup.find("meta", attrs={"name": "twitter:image"})
            url = urljoin(event["url"], tag.get("content")) if tag and tag.get("content") else ""
        except Exception:
            url = ""
    else:
        url = ""
    if not url:
        return None
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        content_type = r.headers.get("Content-Type", "")
        if not content_type.startswith("image/") or len(r.content) > 9 * 1024 * 1024:
            return None
        ext = ".jpg"
        if "png" in content_type:
            ext = ".png"
        elif "webp" in content_type:
            ext = ".webp"
        return BufferedInputFile(r.content, filename="poster" + ext)
    except Exception:
        return None


async def send_highlight_proposals(chat_id):
    global HIGHLIGHT_SENDING
    if HIGHLIGHT_SENDING:
        return
    candidates = highlight_candidates()
    if not candidates:
        await bot.send_message(chat_id, "На сегодня не нашёл подходящих кандидатов на главное событие.")
        return
    HIGHLIGHT_SENDING = True
    try:
        HIGHLIGHT_PROPOSALS[chat_id] = candidates
        await bot.send_message(
            chat_id,
            "<b>⭐ Главное событие дня</b>\n\nВыбери один из 3 вариантов. Публикация произойдёт только после твоего выбора.",
            parse_mode="HTML",
        )
        for i, event in enumerate(candidates):
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=f"Выбрать вариант {i+1}", callback_data=f"highlight:{i}")
            ]])
            text = proposal_text(event, i + 1)
            image = await asyncio.to_thread(fetch_image_sync, event)
            if image:
                await bot.send_photo(chat_id, image, caption=text, parse_mode="HTML", reply_markup=kb)
            else:
                await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb)
    finally:
        HIGHLIGHT_SENDING = False


async def publish_highlight(event):
    dt = event["date"].astimezone(MOSCOW_TZ)
    text = (
        f"<b>⭐ Главное событие сегодня</b>\n\n"
        f"<b>{html.escape(event['title'])}</b>\n"
        f"📅 {dt.strftime('%d.%m.%Y, %H:%M')}\n"
        f"📍 {html.escape(display_location(event))}\n"
    )
    if event.get("url"):
        text += f'\n🔗 <a href="{html.escape(event["url"], quote=True)}">Страница события</a>'

    image = await asyncio.to_thread(fetch_image_sync, event)
    try:
        if image:
            await bot.send_photo(CHANNEL_ID, image, caption=text, parse_mode="HTML")
        else:
            await bot.send_message(CHANNEL_ID, text, parse_mode="HTML", disable_web_page_preview=False)
        return True
    except Exception:
        logger.exception("Ошибка публикации главного события")
        return False


def daily_summary_text():
    day = datetime.now(MOSCOW_TZ).date()
    events = get_events_for_day(day)
    events.sort(key=lambda e: e["date"])
    lines = [f"<b>📅 Афиша на сегодня — {day.strftime('%d.%m.%Y')}</b>", ""]
    if not events:
        return "\n".join(lines + ["Мероприятий в календаре не найдено."])
    for e in events[:70]:
        dt = e["date"].astimezone(MOSCOW_TZ)
        lines.append(
            f"• <b>{dt.strftime('%H:%M')}</b> — {html.escape(e['title'])} "
            f"({html.escape(display_location(e))})"
        )
        if e.get("url"):
            lines.append(f'  🔗 <a href="{html.escape(e["url"], quote=True)}">Источник</a>')
    return "\n".join(lines)


async def publish_daily_summary():
    day = datetime.now(MOSCOW_TZ).date()
    if publication_sent("daily", day):
        return
    try:
        await bot.send_message(CHANNEL_ID, daily_summary_text(), parse_mode="HTML", disable_web_page_preview=True)
        for chat_id in list(SUBSCRIBERS):
            try:
                await bot.send_message(chat_id, daily_summary_text(), parse_mode="HTML", disable_web_page_preview=True)
            except Exception:
                logger.exception("Не удалось отправить дневную программу %s", chat_id)
        mark_publication("daily", day)
        logger.info("Ежедневная программа опубликована")
    except Exception:
        logger.exception("Не удалось опубликовать ежедневную программу")


async def daily_schedule_loop():
    sent_day = None
    while True:
        now = datetime.now(MOSCOW_TZ)
        # Окно 11:00–11:30, фактическая отправка в 11:05.
        if now.hour == 11 and 5 <= now.minute <= 30 and sent_day != now.date():
            await publish_daily_summary()
            sent_day = now.date()
        await asyncio.sleep(30)


async def daily_highlight_loop():
    sent_day = None
    while True:
        now = datetime.now(MOSCOW_TZ)
        # Окно 12:00–12:15, предложение трёх кандидатов.
        if AUTO_HIGHLIGHT_ENABLED and now.hour == 12 and 0 <= now.minute <= 15 and sent_day != now.date():
            candidates = highlight_candidates()
            if candidates:
                # Предложение получает владелец/первый подписчик.
                targets = list(SUBSCRIBERS)
                if targets:
                    await send_highlight_proposals(targets[0])
                sent_day = now.date()
        await asyncio.sleep(30)


@dp.message(Command("start"))
async def start(message: Message):
    SUBSCRIBERS.add(message.chat.id)
    save_subscribers()
    await message.answer(
        f"<b>Афиша Сочи | Сириус | Красная Поляна</b>\n\n"
        f"Источник данных: ваш Outlook Calendar.\n"
        f"Событий загружено: {len(EVENTS)}\n\n"
        f"Версия: {BOT_VERSION}",
        parse_mode="HTML",
        reply_markup=menu(message.from_user.id),
    )


@dp.message(Command("version"))
async def version_cmd(message: Message):
    await message.answer(f"Версия: <b>{BOT_VERSION}</b>\nИсточник: Outlook Calendar (ICS)", parse_mode="HTML", reply_markup=menu(message.from_user.id))


@dp.message(Command("today"))
@dp.message(lambda m: m.text == "Сегодня")
async def today_cmd(message: Message):
    await send_event_list(message, get_events_for_day(datetime.now(MOSCOW_TZ).date()), "Сегодня")


@dp.message(Command("tomorrow"))
@dp.message(lambda m: m.text == "Завтра")
async def tomorrow_cmd(message: Message):
    day = datetime.now(MOSCOW_TZ).date() + timedelta(days=1)
    await send_event_list(message, get_events_for_day(day), "Завтра")


@dp.message(Command("week"))
@dp.message(lambda m: m.text == "7 дней")
async def week_cmd(message: Message):
    now = datetime.now(MOSCOW_TZ)
    end = now + timedelta(days=7)
    events = [e for e in EVENTS if now <= e["date"] <= end and not e["cancelled"]]
    await send_event_list(message, events, "Ближайшие 7 дней")


@dp.message(Command("главное"))
async def main_cmd(message: Message):
    SUBSCRIBERS.add(message.chat.id)
    save_subscribers()
    await send_highlight_proposals(message.chat.id)


@dp.message(lambda m: (m.text or "").lower() in {"/стоп", "/stop"})
async def stop_cmd(message: Message):
    global AUTO_HIGHLIGHT_ENABLED
    AUTO_HIGHLIGHT_ENABLED = False
    await message.answer("🛑 Автоматическое предложение главного события остановлено. Остальные функции продолжают работать.", reply_markup=menu(message.from_user.id))


@dp.message(Command("source"))
@dp.message(lambda m: (m.text or "").split()[0].lower() == "/источник")
async def source_cmd(message: Message):
    await message.answer(
        "<b>Источник событий</b>\n\n"
        "Бот получает мероприятия только из вашего Outlook Calendar через опубликованный ICS.\n"
        "Внешние сайты больше не используются для сбора списка событий.",
        parse_mode="HTML",
        reply_markup=menu(message.from_user.id),
    )


@dp.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer(
        "<b>Команды</b>\n"
        "/today — сегодня\n"
        "/tomorrow — завтра\n"
        "/week — 7 дней\n"
        "/главное — предложить 3 кандидата\n"
        "/стоп — остановить автоматическое предложение главного\n"
        "/source — источник данных\n"
        "/version — версия",
        parse_mode="HTML",
        reply_markup=menu(message.from_user.id),
    )


@dp.message(lambda m: m.text in {"✓ Сочи", "□ Сочи", "Сочи"})
async def area_sochi(message: Message):
    f = get_filters(message.from_user.id)
    if "Сочи" in f["areas"] and len(f["areas"]) == 1:
        f["areas"] = set(AREA_ORDER)
    elif "Сочи" in f["areas"]:
        f["areas"].remove("Сочи")
    else:
        f["areas"].add("Сочи")
    await message.answer("Фильтр обновлён.", reply_markup=menu(message.from_user.id))


@dp.message(lambda m: m.text in {"✓ Сириус", "□ Сириус", "Сириус"})
async def area_sirius(message: Message):
    f = get_filters(message.from_user.id)
    if "Сириус" in f["areas"] and len(f["areas"]) == 1:
        f["areas"] = set(AREA_ORDER)
    elif "Сириус" in f["areas"]:
        f["areas"].remove("Сириус")
    else:
        f["areas"].add("Сириус")
    await message.answer("Фильтр обновлён.", reply_markup=menu(message.from_user.id))


@dp.message(lambda m: m.text in {"✓ Красная Поляна", "□ Красная Поляна", "Красная Поляна"})
async def area_kp(message: Message):
    f = get_filters(message.from_user.id)
    if "Красная Поляна" in f["areas"] and len(f["areas"]) == 1:
        f["areas"] = set(AREA_ORDER)
    elif "Красная Поляна" in f["areas"]:
        f["areas"].remove("Красная Поляна")
    else:
        f["areas"].add("Красная Поляна")
    await message.answer("Фильтр обновлён.", reply_markup=menu(message.from_user.id))


@dp.message(lambda m: m.text in {"✓ Разовые", "□ Разовые", "Разовые"})
async def type_one(message: Message):
    f = get_filters(message.from_user.id)
    if "Разовые" in f["types"] and len(f["types"]) == 1:
        f["types"] = set(TYPE_ORDER)
    elif "Разовые" in f["types"]:
        f["types"].remove("Разовые")
    else:
        f["types"].add("Разовые")
    await message.answer("Фильтр обновлён.", reply_markup=menu(message.from_user.id))


@dp.message(lambda m: m.text in {"✓ Постоянные", "□ Постоянные", "Постоянные"})
async def type_regular(message: Message):
    f = get_filters(message.from_user.id)
    if "Постоянные" in f["types"] and len(f["types"]) == 1:
        f["types"] = set(TYPE_ORDER)
    elif "Постоянные" in f["types"]:
        f["types"].remove("Постоянные")
    else:
        f["types"].add("Постоянные")
    await message.answer("Фильтр обновлён.", reply_markup=menu(message.from_user.id))


@dp.message(lambda m: m.text in {"✓ Спорт", "□ Спорт", "Спорт"})
async def sport_filter(message: Message):
    f = get_filters(message.from_user.id)
    f["sports_only"] = not f["sports_only"]
    await message.answer("Фильтр спорта обновлён.", reply_markup=menu(message.from_user.id))


@dp.message(lambda m: m.text in {"✓ Бесплатно", "□ Бесплатно", "Бесплатно"})
async def free_filter(message: Message):
    f = get_filters(message.from_user.id)
    f["free_only"] = not f["free_only"]
    await message.answer("Фильтр бесплатных обновлён.", reply_markup=menu(message.from_user.id))


@dp.callback_query(lambda c: c.data and c.data.startswith("highlight:"))
async def highlight_callback(callback: CallbackQuery):
    chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    candidates = HIGHLIGHT_PROPOSALS.get(chat_id, [])
    try:
        index = int(callback.data.split(":", 1)[1])
    except Exception:
        index = -1
    if index < 0 or index >= len(candidates):
        await callback.answer("Варианты уже устарели.", show_alert=True)
        return

    event = candidates[index]
    ok = await publish_highlight(event)
    await callback.answer("Опубликовано" if ok else "Ошибка публикации")
    if ok:
        HIGHLIGHT_PROPOSALS.pop(chat_id, None)
        await callback.message.answer(f"✅ Опубликовано: <b>{html.escape(event['title'])}</b>", parse_mode="HTML")


@app.get("/")
async def root():
    return {
        "status": "ok",
        "bot": "SochiSiriusEventsBot",
        "version": BOT_VERSION,
        "source": "Outlook ICS",
        "events": len(EVENTS),
        "last_update": LAST_UPDATE.isoformat() if LAST_UPDATE else None,
    }


@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    return {"status": "ok", "version": BOT_VERSION, "events": len(EVENTS)}


async def refresh_loop():
    while True:
        await asyncio.sleep(300)
        try:
            await collect_events()
        except Exception:
            logger.exception("Ошибка обновления Outlook ICS")


async def bot_loop():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


async def main():
    load_subscribers()
    await collect_events()
    tasks = [
        asyncio.create_task(refresh_loop()),
        asyncio.create_task(bot_loop()),
        asyncio.create_task(daily_schedule_loop()),
        asyncio.create_task(daily_highlight_loop()),
    ]
    server = uvicorn.Server(
        uvicorn.Config(app, host="0.0.0.0", port=int(os.getenv("PORT", "10000")), log_level="info")
    )
    web_task = asyncio.create_task(server.serve())
    tasks.append(web_task)
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
