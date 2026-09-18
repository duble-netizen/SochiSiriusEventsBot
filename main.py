import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
import uvicorn

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sochi_events_bot")

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

app = FastAPI()
bot = Bot(BOT_TOKEN)
dp = Dispatcher()

EVENTS = []
LAST_UPDATE = None

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0 Safari/537.36"
    )
}

SOURCES = [
    {
        "name": "Бюро культуры Сочи",
        "url": "https://sochi.burocultura.ru/events",
        "location": "Сочи",
        "official": True,
    },
    {
        "name": "Официальная афиша Сириуса",
        "url": "https://www.sirius.gov.ru/afisha/",
        "location": "Сириус",
        "official": True,
    },
    {
        "name": "Концертный центр «Сириус»",
        "url": "https://concert.sirius.ru/",
        "location": "Сириус",
        "official": True,
    },
]


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}


def parse_date_time(text: str):
    """Find Russian date/time patterns in a block of text."""
    text = clean(text).lower()
    year = datetime.now().year

    # 18 сентября, 19:30
    m = re.search(
        r"\b(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|"
        r"августа|сентября|октября|ноября|декабря)(?:\s+(\d{4}))?"
        r"(?:[^\d]{0,15})(\d{1,2}):(\d{2})",
        text,
    )
    if m:
        day = int(m.group(1))
        month = MONTHS[m.group(2)]
        year = int(m.group(3)) if m.group(3) else year
        return datetime(year, month, day, int(m.group(4)), int(m.group(5)))

    # 18 сентября без времени
    m = re.search(
        r"\b(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|"
        r"августа|сентября|октября|ноября|декабря)(?:\s+(\d{4}))?",
        text,
    )
    if m:
        day = int(m.group(1))
        month = MONTHS[m.group(2)]
        year = int(m.group(3)) if m.group(3) else year
        return datetime(year, month, day)

    # 18.09.2026 19:30 / 18.09 19:30
    m = re.search(
        r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{4}))?"
        r"(?:[^\d]{0,10})(\d{1,2}):(\d{2})",
        text,
    )
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else year
        return datetime(year, month, day, int(m.group(4)), int(m.group(5)))

    # 18.09.2026 без времени
    m = re.search(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b", text)
    if m:
        return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))

    return None


def probable_title(container, link_text: str) -> str:
    # Prefer headings inside the same card/container.
    for tag in container.find_all(["h1", "h2", "h3", "h4", "h5", "strong"], limit=8):
        t = clean(tag.get_text(" ", strip=True))
        if len(t) >= 4 and not re.match(r"^\d", t):
            return t

    t = clean(link_text)
    # Remove common metadata accidentally included in link text.
    t = re.sub(r"^\d+\+?\s*", "", t)
    return t[:180]


def extract_from_source(source):
    response = requests.get(source["url"], headers=HEADERS, timeout=25)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    found = []

    # 1. JSON-LD Event objects, when a site provides them.
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text())
        except Exception:
            continue

        objects = data if isinstance(data, list) else [data]
        for obj in objects:
            if not isinstance(obj, dict):
                continue
            if obj.get("@type") not in ("Event", ["Event"]):
                continue
            title = clean(obj.get("name", ""))
            start = obj.get("startDate")
            if not title or not start:
                continue
            try:
                dt = datetime.fromisoformat(start.replace("Z", "+00:00")).replace(tzinfo=None)
            except Exception:
                dt = parse_date_time(str(start))
            if dt:
                location = source["location"]
                loc_obj = obj.get("location")
                if isinstance(loc_obj, dict):
                    location = clean(loc_obj.get("name") or location)
                url = obj.get("url") or source["url"]
                found.append({
                    "title": title,
                    "date": dt,
                    "location": location,
                    "source": source["name"],
                    "url": urljoin(source["url"], url),
                    "confirmed": source["official"],
                })

    # 2. Link/card heuristic. This is the fallback for the current pages.
    for link in soup.find_all("a", href=True):
        href = link.get("href", "")
        absolute = urljoin(source["url"], href)
        text = clean(link.get_text(" ", strip=True))

        if len(text) < 4:
            continue
        if absolute.rstrip("/") == source["url"].rstrip("/"):
            continue

        # Candidate event links.
        is_candidate = (
            "/events/" in absolute
            or "/event/" in absolute
            or "afisha" in absolute
            or source["name"] == "Официальная афиша Сириуса"
        )
        if not is_candidate:
            continue

        container = link
        for _ in range(5):
            if container.parent and len(clean(container.parent.get_text(" ", strip=True))) < 2500:
                container = container.parent
            else:
                break

        block = clean(container.get_text(" ", strip=True))
        dt = parse_date_time(block)

        # A date can be outside the immediate link, so also inspect the next
        # few parents if necessary.
        if not dt:
            parent = container.parent
            for _ in range(3):
                if not parent:
                    break
                dt = parse_date_time(parent.get_text(" ", strip=True))
                if dt:
                    container = parent
                    block = clean(parent.get_text(" ", strip=True))
                    break
                parent = parent.parent

        if not dt:
            continue

        title = probable_title(container, text)
        if len(title) < 4:
            continue

        # Skip navigation/filter links and obvious non-events.
        bad = (
            "войти", "подробнее", "все мероприятия", "культура",
            "спорт", "кино", "умный туризм", "главная", "афиша",
        )
        if title.lower() in bad:
            continue

        found.append({
            "title": title,
            "date": dt,
            "location": source["location"],
            "source": source["name"],
            "url": absolute,
            "confirmed": source["official"],
        })

    return found


def deduplicate(events):
    unique = {}
    for e in events:
        key = (
            re.sub(r"[^a-zа-я0-9]+", "", e["title"].lower()),
            e["date"].strftime("%Y-%m-%d %H:%M"),
            e["location"].lower(),
        )
        if key not in unique:
            unique[key] = e
        else:
            # Prefer the more specific event URL.
            if len(e["url"]) > len(unique[key]["url"]):
                unique[key] = e

    return sorted(unique.values(), key=lambda x: x["date"])


async def collect_events():
    global EVENTS, LAST_UPDATE

    all_events = []
    errors = []

    for source in SOURCES:
        try:
            items = await asyncio.to_thread(extract_from_source, source)
            logger.info("%s: найдено %s событий", source["name"], len(items))
            all_events.extend(items)
        except Exception as exc:
            logger.exception("Ошибка источника %s: %s", source["name"], exc)
            errors.append(f"{source['name']}: {exc}")

    if all_events:
        EVENTS = deduplicate(all_events)
        LAST_UPDATE = datetime.now()
        logger.info("Всего после объединения: %s", len(EVENTS))
    else:
        logger.warning("Ни одного события не получено; старые данные оставлены без изменений")

    return errors


def event_text(e):
    status = "✅ ПОДТВЕРЖДЕНО" if e["confirmed"] else "⚠️ НЕ ПОДТВЕРЖДЕНО"
    date_str = e["date"].strftime("%d.%m.%Y")
    time_str = e["date"].strftime("%H:%M") if e["date"].hour or e["date"].minute else "время не указано"
    return (
        f"🎭 <b>{e['title']}</b>\n"
        f"📅 {date_str} {time_str}\n"
        f"📍 {e['location']}\n"
        f"{status}\n"
        f"🔗 <a href=\"{e['url']}\">Источник</a>"
    )


def menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📅 Сегодня"), KeyboardButton(text="📅 Завтра")],
            [KeyboardButton(text="📆 7 дней")],
            [KeyboardButton(text="📍 Сочи"), KeyboardButton(text="📍 Сириус")],
            [KeyboardButton(text="⚠️ Неподтверждённые")],
            [KeyboardButton(text="🔄 Обновить афишу")],
        ],
        resize_keyboard=True,
    )


async def send_events(message: Message, events, title):
    if not events:
        await message.answer(f"<b>{title}</b>\n\nМероприятий не найдено.", reply_markup=menu())
        return

    await message.answer(f"<b>{title}</b>\nНайдено: {len(events)}", reply_markup=menu())
    for e in events[:50]:
        await message.answer(event_text(e))


@dp.message(Command("start"))
async def start(message: Message):
    await message.answer(
        "👋 <b>Афиша Сочи и Сириуса</b>\n\n"
        "Теперь бот получает мероприятия из реальных источников.\n"
        "Используйте кнопки ниже.",
        reply_markup=menu(),
    )


@dp.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer(
        "Команды:\n"
        "/today — сегодня\n"
        "/tomorrow — завтра\n"
        "/week — ближайшие 7 дней\n\n"
        "Кнопка «🔄 Обновить афишу» запускает сбор данных прямо сейчас.",
        reply_markup=menu(),
    )


@dp.message(Command("today"))
@dp.message(lambda m: m.text == "📅 Сегодня")
async def today(message: Message):
    d = datetime.now().date()
    await send_events(
        message,
        [e for e in EVENTS if e["date"].date() == d],
        "📅 Сегодня",
    )


@dp.message(Command("tomorrow"))
@dp.message(lambda m: m.text == "📅 Завтра")
async def tomorrow(message: Message):
    d = (datetime.now() + timedelta(days=1)).date()
    await send_events(
        message,
        [e for e in EVENTS if e["date"].date() == d],
        "📅 Завтра",
    )


@dp.message(Command("week"))
@dp.message(lambda m: m.text == "📆 7 дней")
async def week(message: Message):
    now = datetime.now()
    end = now + timedelta(days=7)
    await send_events(
        message,
        [e for e in EVENTS if now <= e["date"] <= end],
        "📆 Ближайшие 7 дней",
    )


@dp.message(lambda m: m.text == "📍 Сочи")
async def sochi(message: Message):
    await send_events(
        message,
        [e for e in EVENTS if e["location"].lower() == "сочи"],
        "📍 Сочи",
    )


@dp.message(lambda m: m.text == "📍 Сириус")
async def sirius(message: Message):
    await send_events(
        message,
        [e for e in EVENTS if e["location"].lower() == "сириус"],
        "📍 Сириус",
    )


@dp.message(lambda m: m.text == "⚠️ Неподтверждённые")
async def unconfirmed(message: Message):
    await send_events(
        message,
        [e for e in EVENTS if not e["confirmed"]],
        "⚠️ Неподтверждённые",
    )


@dp.message(lambda m: m.text == "🔄 Обновить афишу")
async def refresh(message: Message):
    await message.answer("🔄 Обновляю афишу...")
    errors = await collect_events()
    extra = ""
    if errors:
        extra = "\n\n⚠️ Один или несколько источников временно недоступны."
    await message.answer(
        f"Готово. Найдено мероприятий: <b>{len(EVENTS)}</b>{extra}",
        reply_markup=menu(),
    )


@app.get("/")
async def root():
    return {
        "status": "ok",
        "bot": "SochiSiriusEventsBot",
        "events": len(EVENTS),
        "last_update": LAST_UPDATE.isoformat() if LAST_UPDATE else None,
    }


@app.get("/health")
async def health():
    return {"status": "ok", "events": len(EVENTS)}


async def collector_loop():
    while True:
        try:
            await collect_events()
        except Exception:
            logger.exception("Ошибка фонового сборщика")
        await asyncio.sleep(1800)  # 30 минут


async def bot_loop():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


async def main():
    await collect_events()

    collector_task = asyncio.create_task(collector_loop())
    bot_task = asyncio.create_task(bot_loop())

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
        log_level="info",
    )
    server = uvicorn.Server(config)
    web_task = asyncio.create_task(server.serve())

    try:
        await asyncio.gather(collector_task, bot_task, web_task)
    finally:
        collector_task.cancel()
        bot_task.cancel()
        web_task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
