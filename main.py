import asyncio
import html
import json
import logging
import os
import re
from datetime import datetime, timedelta, time
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    BufferedInputFile,
)
from fastapi import FastAPI
import uvicorn

# SochiSiriusEventsBot — clean rewrite
BOT_VERSION = "4.0.0-OUTLOOK-ICS"
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OUTLOOK_ICS_URL = os.getenv("OUTLOOK_ICS_URL", "").strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "@SochiSiriusEvents").strip()
TZ = ZoneInfo("Europe/Moscow")
HEADERS = {"User-Agent": f"SochiSiriusEventsBot/{BOT_VERSION}"}
STATE_FILE = Path(os.getenv("STATE_FILE", "bot_state.json"))
REFRESH_SECONDS = 300
MAX_TELEGRAM_TEXT = 3900

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("sochi_events_bot")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not OUTLOOK_ICS_URL:
    raise RuntimeError("OUTLOOK_ICS_URL is not set")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
app = FastAPI()

EVENTS = []
LAST_UPDATE = None
LAST_ERROR = None
# Private chats that have messaged the bot at least once. No /start command needed.
CHAT_IDS = set()
# At most one pending selection per private chat.
PENDING_SELECTIONS = {}


def now_moscow():
    return datetime.now(TZ)


def clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def unescape_ics(value):
    return clean(str(value or "").replace("\\n", " ").replace("\\N", " ")
                 .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\"))


def unfold_ics(text):
    result = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.startswith((" ", "\t")) and result:
            result[-1] += line[1:]
        else:
            result.append(line)
    return result


def property_parts(line):
    if ":" not in line:
        return "", {}, ""
    left, value = line.split(":", 1)
    bits = left.split(";")
    params = {}
    for bit in bits[1:]:
        if "=" in bit:
            k, v = bit.split("=", 1)
            params[k.upper()] = v.strip('"')
    return bits[0].upper(), params, value


def parse_dt(value, params=None):
    value = str(value or "").strip()
    params = params or {}
    if not value:
        return None
    if len(value) == 8 and value.isdigit():
        try:
            return datetime.strptime(value, "%Y%m%d").replace(tzinfo=TZ)
        except ValueError:
            return None
    utc = value.endswith("Z")
    raw = value[:-1] if utc else value
    fmt = "%Y%m%dT%H%M%S" if len(raw) >= 15 else "%Y%m%dT%H%M"
    try:
        dt = datetime.strptime(raw, fmt)
    except ValueError:
        return None
    if utc:
        return dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(TZ)
    tz_name = params.get("TZID", "Europe/Moscow")
    tz_aliases = {
        "Russian Standard Time": "Europe/Moscow",
        "Russia Time Zone 3": "Europe/Moscow",
        "W. Europe Standard Time": "Europe/Berlin",
    }
    try:
        zone = ZoneInfo(tz_aliases.get(tz_name, tz_name))
    except Exception:
        zone = TZ
    return dt.replace(tzinfo=zone).astimezone(TZ)


def extract_urls(text):
    return re.findall(r"https?://[^\s<>\]\)\"']+", str(text or ""))


def parse_rrule(value):
    out = {}
    for part in str(value or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.upper()] = v
    return out


def add_months(dt, months):
    month0 = dt.month - 1 + months
    year, month = dt.year + month0 // 12, month0 % 12 + 1
    import calendar
    return dt.replace(year=year, month=month, day=min(dt.day, calendar.monthrange(year, month)[1]))


def expand_recurrence(event, now):
    rule = event.get("rrule")
    if not rule:
        return [event]
    freq = rule.get("FREQ", "").upper()
    interval = max(1, int(rule.get("INTERVAL", "1") or 1))
    try:
        count_limit = min(1000, int(rule.get("COUNT", "1000") or 1000))
    except ValueError:
        count_limit = 1000
    until = parse_dt(rule.get("UNTIL", "")) if rule.get("UNTIL") else now + timedelta(days=180)
    until = min(until or now + timedelta(days=180), now + timedelta(days=180))
    byday = [x[-2:] for x in rule.get("BYDAY", "").split(",") if x]
    weekdays = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
    out, cur, count, steps = [], event["date"], 0, 0
    while cur <= until and count < count_limit and len(out) < 400 and steps < 20000:
        steps += 1
        include = cur >= now - timedelta(days=1)
        if freq == "WEEKLY" and byday:
            include = include and cur.weekday() in {weekdays[d] for d in byday if d in weekdays}
        if include:
            out.append({**event, "date": cur})
            count += 1
        if freq == "DAILY":
            cur += timedelta(days=interval)
        elif freq == "WEEKLY":
            cur += timedelta(days=1 if byday else 7 * interval)
        elif freq == "MONTHLY":
            cur = add_months(cur, interval)
        elif freq == "YEARLY":
            try:
                cur = cur.replace(year=cur.year + interval)
            except ValueError:
                cur = cur.replace(year=cur.year + interval, day=28)
        else:
            return [event]
    return out or [event]


def infer_area(text):
    low = clean(text).lower()
    if any(x in low for x in ("сириус", "федеральная территория", "университет сириус", "олимпийский парк", "автодром сириус")):
        return "Сириус"
    if any(x in low for x in ("красная поляна", "эсто-садок", "роза хутор", "газпром", "горки город", "горный кластер")):
        return "Красная Поляна"
    return "Сочи"


def parse_calendar(text):
    raw_events, current, in_event = [], None, False
    for line in unfold_ics(text):
        upper = line.upper()
        if upper == "BEGIN:VEVENT":
            current, in_event = {}, True
            continue
        if upper == "END:VEVENT":
            if current is not None:
                raw_events.append(current)
            current, in_event = None, False
            continue
        if not in_event:
            continue
        name, params, value = property_parts(line)
        if not name:
            continue
        if name in {"DTSTART", "DTEND"}:
            current[name] = (value, params)
        elif name == "ATTACH":
            current.setdefault("attachments", []).append((value, params))
        else:
            current[name] = unescape_ics(value)

    now = now_moscow()
    parsed = []
    for raw in raw_events:
        start_pair = raw.get("DTSTART", ("", {}))
        start = parse_dt(*start_pair)
        if not start:
            continue
        title = clean(raw.get("SUMMARY", "Без названия"))
        if not title:
            continue
        description = clean(raw.get("DESCRIPTION", ""))
        location = clean(raw.get("LOCATION", ""))
        url = clean(raw.get("URL", ""))
        urls = extract_urls(description)
        if not url and urls:
            url = urls[0].rstrip(".,")
        image_url = ""
        for attachment, params in raw.get("attachments", []):
            if "image" in params.get("FMTTYPE", "").lower() or re.search(r"\.(jpg|jpeg|png|webp)(?:\?|$)", attachment, re.I):
                image_url = attachment
                break
        base = {
            "uid": clean(raw.get("UID", "")), "title": title, "date": start,
            "end": parse_dt(*raw["DTEND"]) if raw.get("DTEND") else None,
            "location": location, "description": description, "url": url,
            "image_url": image_url, "status": clean(raw.get("STATUS", "")),
            "rrule": parse_rrule(raw.get("RRULE", "")) if raw.get("RRULE") else None,
        }
        for event in expand_recurrence(base, now):
            combined = f"{title} {location} {description}"
            event["area"] = infer_area(combined)
            event["cancelled"] = event["status"].upper() == "CANCELLED" or bool(re.search(r"\bотмен[её]н\w*", description, re.I))
            parsed.append(event)

    unique = {}
    for event in parsed:
        key = (event.get("uid") or re.sub(r"\W+", "", event["title"].lower()), event["date"].strftime("%Y-%m-%d %H:%M"))
        old = unique.get(key)
        if old is None or len(event.get("description", "")) > len(old.get("description", "")):
            unique[key] = event
    return sorted(unique.values(), key=lambda e: e["date"])


def load_calendar_sync():
    response = requests.get(OUTLOOK_ICS_URL, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return parse_calendar(response.content.decode("utf-8-sig", errors="replace"))


async def refresh_calendar():
    global EVENTS, LAST_UPDATE, LAST_ERROR
    try:
        loaded = await asyncio.to_thread(load_calendar_sync)
        EVENTS = loaded
        LAST_UPDATE = now_moscow()
        LAST_ERROR = None
        log.info("Outlook ICS refresh OK: %d events", len(EVENTS))
        return True
    except Exception as exc:
        LAST_ERROR = f"{type(exc).__name__}: {exc}"
        log.exception("Outlook ICS refresh failed; keeping last successful snapshot")
        return False


def day_events(day=None):
    day = day or now_moscow().date()
    return [e for e in EVENTS if e["date"].astimezone(TZ).date() == day and not e["cancelled"]]


def event_location(event):
    return event.get("location") or event.get("area") or "Место не указано"


def event_html(event, include_category=False):
    dt = event["date"].astimezone(TZ)
    title = html.escape(event["title"])
    location = html.escape(event_location(event))
    lines = [f"<b>{title}</b>", f"{dt.strftime('%d.%m.%Y %H:%M')} — {location}"]
    if include_category:
        lines.append(f"Район: {html.escape(event.get('area', 'Сочи'))}")
    if event.get("url"):
        lines.append(f'<a href="{html.escape(event["url"], quote=True)}">Страница события</a>')
    return "\n".join(lines)


def main_number(event):
    # Marker can be in Outlook description or location; exact label is TELEGRAM_MAIN: 1/2/3.
    text = f"{event.get('description', '')}\n{event.get('location', '')}"
    match = re.search(r"(?i)(?:^|\s)TELEGRAM_MAIN\s*:\s*([123])\b", text)
    return int(match.group(1)) if match else None


def main_candidates():
    marked = [(main_number(e), e) for e in day_events()]
    marked = [(n, e) for n, e in marked if n is not None]
    marked.sort(key=lambda item: item[0])
    # Do not silently offer duplicates or more than one event for the same marker.
    result, seen = [], set()
    for number, event in marked:
        if number in seen:
            log.warning("Duplicate TELEGRAM_MAIN:%s marker found; ignoring later event", number)
            continue
        seen.add(number)
        result.append((number, event))
    return result[:3]


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"chats": [], "sent": {}}


def save_state(state):
    tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def state_chats():
    state = load_state()
    return {int(x) for x in state.get("chats", []) if str(x).lstrip("-").isdigit()}


def remember_chat(chat_id):
    state = load_state()
    chats = {int(x) for x in state.get("chats", []) if str(x).lstrip("-").isdigit()}
    chats.add(int(chat_id))
    state["chats"] = sorted(chats)
    save_state(state)


def was_sent(key, day):
    return load_state().get("sent", {}).get(f"{key}:{day.isoformat()}") is True


def mark_sent(key, day):
    state = load_state()
    state.setdefault("sent", {})[f"{key}:{day.isoformat()}"] = True
    save_state(state)


def daily_summary():
    day = now_moscow().date()
    events = day_events(day)
    lines = [f"<b>Афиша на сегодня — {day.strftime('%d.%m.%Y')}</b>", ""]
    if not events:
        lines.append("На сегодня в Outlook Calendar мероприятий не найдено.")
    else:
        for event in events:
            lines.append(event_html(event))
            lines.append("")
            lines.append("────────────")
            lines.append("")
    text = "\n".join(lines).strip()
    # Telegram message limit; keep whole event blocks when possible.
    if len(text) <= MAX_TELEGRAM_TEXT:
        return text
    compact = [f"<b>Афиша на сегодня — {day.strftime('%d.%m.%Y')}</b>", ""]
    for event in events:
        block = event_html(event)
        if len("\n\n".join(compact + [block])) > MAX_TELEGRAM_TEXT:
            compact.append("Остальные события доступны в Outlook Calendar.")
            break
        compact.extend([block, ""])
    return "\n\n".join(compact)


async def publish_daily():
    day = now_moscow().date()
    if was_sent("daily", day):
        return
    if LAST_UPDATE is None:
        log.error("Daily publication skipped: calendar has never loaded successfully")
        return
    # Mark only after Telegram confirms successful send.
    await bot.send_message(CHANNEL_ID, daily_summary(), parse_mode="HTML", disable_web_page_preview=True)
    mark_sent("daily", day)
    log.info("Daily summary delivered to channel for %s", day.isoformat())


async def fetch_image(event):
    url = event.get("image_url", "")
    if not url and event.get("url"):
        try:
            from bs4 import BeautifulSoup
            response = await asyncio.to_thread(requests.get, event["url"], headers=HEADERS, timeout=15)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            tag = soup.find("meta", attrs={"property": "og:image"}) or soup.find("meta", attrs={"name": "twitter:image"})
            if tag and tag.get("content"):
                url = urljoin(event["url"], tag["content"])
        except Exception:
            log.exception("Could not discover event image")
    if not url:
        return None
    try:
        response = await asyncio.to_thread(requests.get, url, headers=HEADERS, timeout=20)
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").lower()
        if not content_type.startswith("image/") or len(response.content) > 9 * 1024 * 1024:
            return None
        ext = ".png" if "png" in content_type else ".webp" if "webp" in content_type else ".jpg"
        return BufferedInputFile(response.content, filename="event" + ext)
    except Exception:
        log.exception("Could not fetch event image")
        return None


async def publish_selected(chat_id, event):
    text = event_html(event)
    image = await fetch_image(event)
    if image:
        await bot.send_photo(CHANNEL_ID, image, caption=text, parse_mode="HTML")
    else:
        await bot.send_message(CHANNEL_ID, text, parse_mode="HTML", disable_web_page_preview=False)
    PENDING_SELECTIONS.pop(chat_id, None)
    log.info("Confirmed main event published: %s", event["title"])


async def offer_main_candidates(day):
    if was_sent("proposals", day):
        return
    if LAST_UPDATE is None:
        log.error("Main-event proposals skipped: calendar has never loaded successfully")
        return
    candidates = main_candidates()
    if not candidates:
        log.warning("No TELEGRAM_MAIN: 1/2/3 candidates for %s", day.isoformat())
        # Record a successful check so we don't spam the user every 30 seconds.
        mark_sent("proposals", day)
        return
    chats = state_chats()
    if not chats:
        log.error("Cannot send main-event choices: no private chat has contacted the bot")
        return
    for chat_id in chats:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"Выбрать вариант {i}", callback_data=f"choose:{i}")]
            for i, _ in candidates
        ])
        text = "<b>Выбери одно главное мероприятие для публикации в канал</b>\n\n"
        text += "\n\n────────────\n\n".join(event_html(event, include_category=True) for _, event in candidates)
        PENDING_SELECTIONS[chat_id] = {"day": day.isoformat(), "candidates": {i: event for i, event in candidates}}
        try:
            await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=keyboard, disable_web_page_preview=True)
        except Exception:
            log.exception("Failed sending main-event options to private chat %s", chat_id)
            continue
    # Only mark after at least one private chat accepted the proposal.
    mark_sent("proposals", day)
    log.info("Main-event choices sent for %s", day.isoformat())


@dp.message(Command("version"))
async def version_command(message: Message):
    if message.chat.type == "private":
        remember_chat(message.chat.id)
    await message.answer(f"Версия бота: <b>{BOT_VERSION}</b>", parse_mode="HTML")


@dp.message()
async def register_private_chat(message: Message):
    # All commands except /version are deliberately unsupported.
    if message.chat.type != "private":
        return
    if message.text and message.text.startswith("/"):
        return
    remember_chat(message.chat.id)
    await message.answer("Бот работает автоматически. Единственная команда: /version")


@dp.callback_query(F.data.startswith("choose:"))
async def choose_main(callback: CallbackQuery):
    chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    pending = PENDING_SELECTIONS.get(chat_id)
    if not pending or pending.get("day") != now_moscow().date().isoformat():
        await callback.answer("Предложения устарели. Новые появятся завтра.", show_alert=True)
        return
    try:
        number = int(callback.data.split(":", 1)[1])
        event = pending["candidates"][number]
    except (ValueError, KeyError, TypeError):
        await callback.answer("Этот вариант недоступен.", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Да, опубликовать", callback_data=f"confirm:{number}"),
         InlineKeyboardButton(text="Отмена", callback_data="cancel")]
    ])
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        f"Подтверди публикацию этого события в канал:\n\n{event_html(event)}",
        parse_mode="HTML", reply_markup=keyboard, disable_web_page_preview=True,
    )
    await callback.answer("Осталось подтвердить публикацию")


@dp.callback_query(F.data.startswith("confirm:"))
async def confirm_main(callback: CallbackQuery):
    chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    pending = PENDING_SELECTIONS.get(chat_id)
    if not pending or pending.get("day") != now_moscow().date().isoformat():
        await callback.answer("Предложение устарело.", show_alert=True)
        return
    try:
        number = int(callback.data.split(":", 1)[1])
        event = pending["candidates"][number]
        # Refresh the calendar and ensure this marker still points to today's event.
        await refresh_calendar()
        current = {n: e for n, e in main_candidates()}
        fresh = current.get(number)
        if not fresh or fresh.get("uid") != event.get("uid") or fresh["date"] != event["date"]:
            await callback.answer("Событие в календаре изменилось. Выбор сброшен.", show_alert=True)
            PENDING_SELECTIONS.pop(chat_id, None)
            return
        event = fresh
        await publish_selected(chat_id, event)
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(f"Опубликовано в канал: <b>{html.escape(event['title'])}</b>", parse_mode="HTML")
        await callback.answer("Опубликовано")
    except Exception:
        log.exception("Failed publishing confirmed main event")
        await callback.answer("Не удалось опубликовать. Попробуй позже.", show_alert=True)


@dp.callback_query(F.data == "cancel")
async def cancel_main(callback: CallbackQuery):
    chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Публикация отменена")


async def scheduler_loop():
    while True:
        now = now_moscow()
        day = now.date()
        # Exactly once per local date, as soon as the process is running at/after 09:30.
        # If the service restarts after the window, it catches up rather than losing the task.
        if now.time() >= time(9, 30) and not was_sent("daily", day):
            try:
                await publish_daily()
            except Exception:
                log.exception("Daily channel publication failed; will retry")
        # Offer choices from 10:30 onwards, after the daily post.
        if now.time() >= time(10, 30) and not was_sent("proposals", day):
            try:
                await offer_main_candidates(day)
            except Exception:
                log.exception("Main-event proposal step failed; will retry")
        await asyncio.sleep(20)


async def refresh_loop():
    while True:
        await asyncio.sleep(REFRESH_SECONDS)
        await refresh_calendar()


@app.get("/")
async def root():
    return {"status": "ok", "bot": "SochiSiriusEventsBot", "version": BOT_VERSION,
            "source": "Outlook ICS", "events": len(EVENTS),
            "last_update": LAST_UPDATE.isoformat() if LAST_UPDATE else None,
            "last_error": LAST_ERROR}


@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    ok = LAST_UPDATE is not None
    return {"status": "ok" if ok else "degraded", "version": BOT_VERSION,
            "calendar_loaded": ok, "events": len(EVENTS), "last_error": LAST_ERROR}


async def bot_loop():
    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot)


async def main():
    await refresh_calendar()
    tasks = [asyncio.create_task(refresh_loop()), asyncio.create_task(bot_loop()),
             asyncio.create_task(scheduler_loop())]
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=int(os.getenv("PORT", "10000")), log_level="info"))
    tasks.append(asyncio.create_task(server.serve()))
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
