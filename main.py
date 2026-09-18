import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta
from html import escape
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
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

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"}

SOURCES = [
    {"name": "Бюро культуры Сочи", "url": "https://sochi.burocultura.ru/events", "location": "Сочи", "official": True},
    {"name": "Официальная афиша Сириуса", "url": "https://www.sirius.gov.ru/afisha/", "location": "Сириус", "official": True},
    {"name": "Концертный центр «Сириус»", "url": "https://concert.sirius.ru/", "location": "Сириус", "official": True},
]


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6, "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12}


def parse_date_time(text: str):
    text = clean(text).lower()
    year = datetime.now().year
    m = re.search(r"\b(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)(?:\s+(\d{4}))?(?:[^\d]{0,15})(\d{1,2}):(\d{2})", text)
    if m:
        return datetime(int(m.group(3)) if m.group(3) else year, MONTHS[m.group(2)], int(m.group(1)), int(m.group(4)), int(m.group(5)))
    m = re.search(r"\b(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)(?:\s+(\d{4}))?", text)
    if m:
        return datetime(int(m.group(3)) if m.group(3) else year, MONTHS[m.group(2)], int(m.group(1)))
    m = re.search(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{4}))?(?:[^\d]{0,10})(\d{1,2}):(\d{2})", text)
    if m:
        return datetime(int(m.group(3)) if m.group(3) else year, int(m.group(2)), int(m.group(1)), int(m.group(4)), int(m.group(5)))
    m = re.search(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b", text)
    if m:
        return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    return None


def probable_title(container, link_text: str) -> str:
    for tag in container.find_all(["h1", "h2", "h3", "h4", "h5", "strong"], limit=8):
        t = clean(tag.get_text(" ", strip=True))
        if len(t) >= 4 and not re.match(r"^\d", t):
            return t
    return re.sub(r"^\d+\+?\s*", "", clean(link_text))[:180]


def extract_from_source(source):
    response = requests.get(source["url"], headers=HEADERS, timeout=25)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    found = []

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text())
        except Exception:
            continue
        objects = data if isinstance(data, list) else [data]
        for obj in objects:
            if not isinstance(obj, dict) or obj.get("@type") not in ("Event", ["Event"]):
                continue
            title, start = clean(obj.get("name", "")), obj.get("startDate")
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
                found.append({"title": title, "date": dt, "location": location, "source": source["name"], "url": urljoin(source["url"], obj.get("url") or source["url"]), "confirmed": source["official"]})

    for link in soup.find_all("a", href=True):
        href, absolute, text = link.get("href", ""), urljoin(source["url"], link.get("href", "")), clean(link.get_text(" ", strip=True))
        if len(text) < 4 or absolute.rstrip("/") == source["url"].rstrip("/"):
            continue
        if not ("/events/" in absolute or "/event/" in absolute or "afisha" in absolute or source["name"] == "Официальная афиша Сириуса"):
            continue
        container = link
        for _ in range(5):
            if container.parent and len(clean(container.parent.get_text(" ", strip=True))) < 2500:
                container = container.parent
            else:
                break
        block, dt = clean(container.get_text(" ", strip=True)), parse_date_time(clean(container.get_text(" ", strip=True)))
        if not dt:
            parent = container.parent
            for _ in range(3):
                if not parent:
                    break
                dt = parse_date_time(parent.get_text(" ", strip=True))
                if dt:
                    container = parent
                    break
                parent = parent.parent
        if not dt:
            continue
        title = probable_title(container, text)
        if len(title) < 4 or title.lower() in ("войти", "подробнее", "все мероприятия", "культура", "спорт", "кино", "умный туризм", "главная", "афиша"):
            continue
        found.append({"title": title, "date": dt, "location": source["location"], "source": source["name"], "url": absolute, "confirmed": source["official"]})
    return found


def deduplicate(events):
    unique = {}
    for e in events:
        key = (re.sub(r"[^a-zа-я0-9]+", "", e["title"].lower()), e["date"].strftime("%Y-%m-%d %H:%M"), e["location"].lower())
        if key not in unique or len(e["url"]) > len(unique[key]["url"]):
            unique[key] = e
    return sorted(unique.values(), key=lambda x: x["date"])


async def collect_events():
    global EVENTS, LAST_UPDATE
    all_events, errors = [], []
    for source in SOURCES:
        try:
            items = await asyncio.to_thread(extract_from_source, source)
            logger.info("%s: найдено %s событий", source["name"], len(items))
            all_events.extend(items)
        except Exception as exc:
            logger.exception("Ошибка источника %s: %s", source["name"], exc)
            errors.append(f"{source['name']}: {exc}")
    if all_events:
        EVENTS, LAST_UPDATE = deduplicate(all_events), datetime.now()
        logger.info("Всего после объединения: %s", len(EVENTS))
    else:
        logger.warning("Ни одного события не получено; старые данные оставлены без изменений")
    return errors


WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def event_text(e, show_date=True):
    time_str = e["date"].strftime("%H:%M") if e["date"].hour or e["date"].minute else "время не указано"
    if show_date:
        info = f"{e['date'].strftime('%d.%m.%Y')}, {time_str}, {escape(e['location'])}"
    else:
        info = f"{time_str}, {escape(e['location'])}"
    return f"<b>{escape(e['title'])}</b>\n{info}"


def event_keyboard(events):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Источник", url=e["url"])] for e in events])


def grouped_event_blocks(events):
    groups = {}
    for e in events:
        groups.setdefault(e["date"].date(), []).append(e)
    return [(f"<b>{day.strftime('%d.%m.%Y')} — {WEEKDAYS[day.weekday()]}</b>", groups[day]) for day in sorted(groups)]


def menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Сегодня"), KeyboardButton(text="Завтра")],
        [KeyboardButton(text="7 дней")],
        [KeyboardButton(text="Сочи"), KeyboardButton(text="Сириус")],
        [KeyboardButton(text="Неподтверждённые")],
        [KeyboardButton(text="Обновить афишу")],
    ], resize_keyboard=True)


async def send_events(message: Message, events, title, group_by_date=False):
    if not events:
        await message.answer(f"<b>{escape(title)}</b>\n\nМероприятий не найдено.", reply_markup=menu(), parse_mode="HTML")
        return
    events = events[:50]
    chunks, current_events, current_parts = [], [], [f"<b>{escape(title)}</b>\nНайдено: {len(events)}"]
    if group_by_date:
        for header, day_events in grouped_event_blocks(events):
            day_text = header + "\n\n" + "\n\n────────────\n\n".join(event_text(ev, False) for ev in day_events)
            if len("\n\n".join(current_parts) + "\n\n" + day_text) > 3500 and current_events:
                chunks.append((current_parts, current_events))
                current_parts, current_events = [], []
            current_parts.append(day_text)
            current_events.extend(day_events)
    else:
        for ev in events:
            block = event_text(ev)
            if len("\n\n".join(current_parts) + "\n\n────────────\n\n" + block) > 3500 and current_events:
                chunks.append((current_parts, current_events))
                current_parts, current_events = [], []
            current_parts.append(block)
            current_events.append(ev)
    if current_events:
        chunks.append((current_parts, current_events))
    for text_parts, chunk_events in chunks:
        await message.answer("\n\n────────────\n\n".join(text_parts), reply_markup=event_keyboard(chunk_events), parse_mode="HTML")
    await message.answer("Выберите следующий раздел", reply_markup=menu())


@dp.message(Command("start"))
async def start(message: Message):
    await message.answer("<b>Афиша Сочи и Сириуса</b>\n\nМероприятия из реальных источников.\nИспользуйте кнопки ниже.", reply_markup=menu(), parse_mode="HTML")


@dp.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer("Команды:\n/today — сегодня\n/tomorrow — завтра\n/week — ближайшие 7 дней\n\nКнопка «Обновить афишу» запускает сбор данных прямо сейчас.", reply_markup=menu())


@dp.message(Command("today"))
@dp.message(lambda m: m.text == "Сегодня")
async def today(message: Message):
    d = datetime.now().date()
    await send_events(message, [e for e in EVENTS if e["date"].date() == d], "Сегодня")


@dp.message(Command("tomorrow"))
@dp.message(lambda m: m.text == "Завтра")
async def tomorrow(message: Message):
    d = (datetime.now() + timedelta(days=1)).date()
    await send_events(message, [e for e in EVENTS if e["date"].date() == d], "Завтра")


@dp.message(Command("week"))
@dp.message(lambda m: m.text == "7 дней")
async def week(message: Message):
    now, end = datetime.now(), datetime.now() + timedelta(days=7)
    await send_events(message, [e for e in EVENTS if now <= e["date"] <= end], "Ближайшие 7 дней", group_by_date=True)


@dp.message(lambda m: m.text == "Сочи")
async def sochi(message: Message):
    await send_events(message, [e for e in EVENTS if e["location"].lower() == "сочи"], "Сочи")


@dp.message(lambda m: m.text == "Сириус")
async def sirius(message: Message):
    await send_events(message, [e for e in EVENTS if e["location"].lower() == "сириус"], "Сириус")


@dp.message(lambda m: m.text == "Неподтверждённые")
async def unconfirmed(message: Message):
    await send_events(message, [e for e in EVENTS if not e["confirmed"]], "Неподтверждённые")


@dp.message(lambda m: m.text == "Обновить афишу")
async def refresh(message: Message):
    await message.answer("<b>Обновляю афишу...</b>", parse_mode="HTML")
    errors = await collect_events()
    extra = "\n\nНекоторые источники временно недоступны." if errors else ""
    await message.answer(f"<b>Готово.</b> Найдено мероприятий: <b>{len(EVENTS)}</b>{extra}", reply_markup=menu(), parse_mode="HTML")


@app.get("/")
async def root():
    return {"status": "ok", "bot": "SochiSiriusEventsBot", "events": len(EVENTS), "last_update": LAST_UPDATE.isoformat() if LAST_UPDATE else None}


@app.get("/health")
async def health():
    return {"status": "ok", "events": len(EVENTS)}


async def collector_loop():
    while True:
        try:
            await collect_events()
        except Exception:
            logger.exception("Ошибка фонового сборщика")
        await asyncio.sleep(1800)


async def bot_loop():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


async def main():
    await collect_events()
    collector_task = asyncio.create_task(collector_loop())
    bot_task = asyncio.create_task(bot_loop())
    config = uvicorn.Config(app, host="0.0.0.0", port=int(os.getenv("PORT", "10000")), log_level="info")
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
