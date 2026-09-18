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
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
import uvicorn

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sochi_events_bot")

BOT_VERSION = "1.4.0"
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

app = FastAPI()
bot = Bot(BOT_TOKEN)
dp = Dispatcher()
EVENTS = []
LAST_UPDATE = None

# Фильтры хранятся отдельно для каждого пользователя Telegram.
# По умолчанию выбраны все города и оба типа мероприятий.
USER_FILTERS = {}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0 Safari/537.36"
}

# Чем больше официальных площадок, тем шире охват. Ссылки проверены 18.09.2026.
SOURCES = [
    {"name": "Бюро культуры Сочи", "url": "https://sochi.burocultura.ru/events", "area": "Сочи", "official": True},
    {"name": "Официальная афиша Сириуса", "url": "https://www.sirius.gov.ru/afisha/", "area": "Сириус", "official": True},
    {"name": "Концертный центр «Сириус»", "url": "https://concert.sirius.ru/", "area": "Сириус", "official": True},
    {"name": "Художественно-исторический центр «Сириус»", "url": "https://art.sirius.ru/poster/", "area": "Сириус", "official": True},
    {"name": "Сириус Автодром", "url": "https://siriusautodrom.ru/fest2026", "area": "Сириус", "official": True},
    {"name": "Курорт Красная Поляна", "url": "https://krasnayapolyanaresort.ru/events", "area": "Красная Поляна", "official": True},
    {"name": "Сочи Парк", "url": "https://www.sochipark.ru/programma/", "area": "Сочи", "official": True},
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
    m = re.search(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{4}))?(?:[^\d]{0,10})(\d{1,2})[.:](\d{2})", text)
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


def location_from_jsonld(obj, fallback):
    loc = obj.get("location")
    if isinstance(loc, list):
        loc = loc[0] if loc else None
    if isinstance(loc, str):
        value = clean(loc)
        return value or fallback
    if isinstance(loc, dict):
        name = clean(loc.get("name", ""))
        address = loc.get("address")
        if isinstance(address, dict):
            parts = [clean(address.get("streetAddress", "")), clean(address.get("addressLocality", ""))]
            address_text = ", ".join(p for p in parts if p)
            if name and address_text:
                return f"{name}, {address_text}"
            if name:
                return name
            if address_text:
                return address_text
        if name:
            return name
    return fallback


def is_generic_location(value, area):
    v = clean(value).lower()
    a = clean(area).lower()
    generic = {"сочи", "сириус", "красная поляна", "адлер", "эсто-садок", "сочи, россия", "сириус, россия"}
    return v in generic or v == a or v.startswith(a + ",") and len(v) <= len(a) + 10


def extract_location(container, fallback):
    selectors = [
        '[itemprop="location"]', '[itemprop="address"]',
        '[class*="location"]', '[class*="venue"]', '[class*="address"]',
        '[class*="place"]', '[id*="location"]', '[id*="venue"]',
        '[id*="address"]', '[id*="place"]',
    ]
    candidates = []
    for selector in selectors:
        try:
            for tag in container.select(selector):
                text = clean(tag.get_text(" ", strip=True))
                if 3 <= len(text) <= 250:
                    candidates.append(text)
        except Exception:
            pass

    full_text = clean(container.get_text(" ", strip=True))
    # На многих современных афишах подпись склеивается с названием:
    # "МестоКазино...", "Время21:00".
    patterns = [
        r"(?:место|площадка|адрес|зал)\s*[:\-]?\s*([^|]{3,180}?)(?=\s+(?:время|дата|стоимость|купить|подробнее)\b|$)",
        r"(?:место|площадка|адрес|зал)\s*[:\-]?\s*([^•·]{3,180}?)(?=\s*[•·]|$)",
    ]
    for pattern in patterns:
        for m in re.finditer(pattern, full_text, re.IGNORECASE):
            candidates.append(clean(m.group(1)))

    # Частый вариант у JSON/HTML: Venue: ...
    for label in ["venue", "location", "place"]:
        for tag in container.find_all(attrs={"data-" + label: True}):
            value = clean(tag.get("data-" + label, ""))
            if value:
                candidates.append(value)

    bad = {
        "сочи", "сириус", "красная поляна", "подробнее", "афиша", "мероприятия",
        "купить билет", "билеты", "регистрация", "время", "дата"
    }
    for candidate in candidates:
        candidate = clean(candidate.strip(" -|,.:"))
        low = candidate.lower()
        if low in bad or len(candidate) < 3:
            continue
        # Отбрасываем куски, в которых попался только служебный текст.
        if low.startswith(("время", "дата", "стоимость", "купить")):
            continue
        return candidate
    return fallback


def parse_sitemap_like_events(soup, source):
    """Дополнительный разбор JSON-LD ItemList/Event, который встречается на афишах."""
    found = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text())
        except Exception:
            continue
        objects = data if isinstance(data, list) else [data]
        stack = list(objects)
        while stack:
            obj = stack.pop(0)
            if isinstance(obj, dict):
                if isinstance(obj.get("itemListElement"), list):
                    stack.extend(obj["itemListElement"])
                if obj.get("@type") in ("Event", ["Event"]):
                    title, start = clean(obj.get("name", "")), obj.get("startDate")
                    if title and start:
                        try:
                            dt = datetime.fromisoformat(str(start).replace("Z", "+00:00")).replace(tzinfo=None)
                        except Exception:
                            dt = parse_date_time(str(start))
                        if dt:
                            location = location_from_jsonld(obj, source["area"])
                            found.append({
                                "title": title,
                                "date": dt,
                                "location": location,
                                "area": source["area"],
                                "source": source["name"],
                                "url": urljoin(source["url"], obj.get("url") or source["url"]),
                                "confirmed": source["official"],
                                "event_type": classify_event(title, json.dumps(obj, ensure_ascii=False)),
                            })
    return found


def extract_from_source(source):
    response = requests.get(source["url"], headers=HEADERS, timeout=25)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    found = parse_sitemap_like_events(soup, source)

    for link in soup.find_all("a", href=True):
        href, absolute, text = link.get("href", ""), urljoin(source["url"], link.get("href", "")), clean(link.get_text(" ", strip=True))
        if len(text) < 4 or absolute.rstrip("/") == source["url"].rstrip("/"):
            continue
        allowed = (
            "/events" in absolute or "/event/" in absolute or "/afisha" in absolute
            or "programma" in absolute or source["name"] in ("Официальная афиша Сириуса", "Роза Хутор")
        )
        if not allowed:
            continue
        container = link
        for _ in range(6):
            if container.parent and len(clean(container.parent.get_text(" ", strip=True))) < 3000:
                container = container.parent
            else:
                break
        block = clean(container.get_text(" ", strip=True))
        dt = parse_date_time(block)
        if not dt:
            parent = container.parent
            for _ in range(3):
                if not parent:
                    break
                dt = parse_date_time(parent.get_text(" ", strip=True))
                if dt:
                    container = parent
                    block = clean(container.get_text(" ", strip=True))
                    break
                parent = parent.parent
        if not dt:
            continue
        title = probable_title(container, text)
        if len(title) < 4 or title.lower() in ("войти", "подробнее", "все мероприятия", "культура", "спорт", "кино", "умный туризм", "главная", "афиша"):
            continue
        location = extract_location(container, source["area"])
        found.append({
            "title": title,
            "date": dt,
            "location": location,
            "area": source["area"],
            "source": source["name"],
            "url": absolute,
            "confirmed": source["official"],
            "event_type": classify_event(title, block),
        })
    return found



def classify_event(title: str, source_text: str = "") -> str:
    """Определяет, относится ли запись к разовым или постоянным/длительным.

    Важно: само слово «фестиваль» или «турнир» не делает событие длительным.
    Длительным оно считается, если источник указывает период/регулярность,
    либо по названию явно видно, что это постоянная услуга/объект.
    """
    text = clean(f"{title} {source_text}").lower()

    # Явная регулярность: каждый день, по выходным, конкретные дни недели и т.п.
    recurring = [
        "каждый день", "ежедневно", "ежедневный", "ежедневная", "ежедневное",
        "каждую неделю", "каждый понедельник", "каждый вторник",
        "каждую среду", "каждый четверг", "каждую пятницу",
        "каждую субботу", "каждое воскресенье", "по выходным",
        "по будням", "еженедельно", "регулярно",
    ]
    if any(x in text for x in recurring):
        return "Постоянные"

    # Период: «с 18 по 30 сентября», «18 сентября — 5 октября» и т.п.
    has_range = bool(re.search(
        r"\bс\s+\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)"
        r"\s+(?:по|до|—|–|-)+\s+\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)",
        text,
    )) or bool(re.search(r"\b\d{1,2}[./]\d{1,2}\s*[—–-]\s*\d{1,2}[./]\d{1,2}\b", text))
    if has_range:
        return "Постоянные"

    # Постоянные услуги/объекты, которые обычно представлены в афише как
    # доступные в течение длительного периода.
    permanent_keywords = [
        "плавание с дельфинами", "дельфинар", "океанариум", "аквариум",
        "экскурсия", "экскурсии", "музей", "экспозиция", "аттракцион",
        "канатная дорога", "парк развлечений", "катание на лошадях",
        "прокат", "посещение", "шоу-программа",
    ]
    if any(x in text for x in permanent_keywords):
        return "Постоянные"

    # Выставка/ярмарка/проект без явной даты окончания остаются разовыми,
    # если источник не сообщил период. Так мы не будем автоматически
    # превращать любое однодневное мероприятие в «длительное».
    return "Разовые"


def filter_event_type(e):
    return e.get("event_type", "Разовые")

def deduplicate(events):
    unique = {}
    for e in events:
        # Не теряем события разных площадок в одном городе.
        key = (
            re.sub(r"[^a-zа-я0-9]+", "", e["title"].lower()),
            e["date"].strftime("%Y-%m-%d %H:%M"),
            e.get("area", "").lower(),
            e.get("location", "").lower(),
        )
        if key not in unique or len(e["url"]) > len(unique[key]["url"]):
            unique[key] = e
    return sorted(unique.values(), key=lambda x: x["date"])


def enrich_event_location(e):
    """Если карточка дала только город, пробуем страницу самого события."""
    if not is_generic_location(e.get("location", ""), e.get("area", "")):
        return e
    try:
        r = requests.get(e["url"], headers=HEADERS, timeout=10)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or script.get_text())
            except Exception:
                continue
            objects = data if isinstance(data, list) else [data]
            for obj in objects:
                if isinstance(obj, dict) and obj.get("@type") in ("Event", ["Event"]):
                    loc = location_from_jsonld(obj, e["area"])
                    if not is_generic_location(loc, e["area"]):
                        e["location"] = loc
                        return e
        body = clean(soup.get_text(" ", strip=True))
        loc = extract_location(soup, e["area"])
        if not is_generic_location(loc, e["area"]):
            e["location"] = loc
        else:
            # Дополнительный разбор текста страницы по явным подписям.
            m = re.search(r"(?:место|площадка|адрес|зал)\s*[:\-]?\s*([^|•·]{3,180}?)(?=\s+(?:время|дата|стоимость|купить|подробнее)\b|$)", body, re.IGNORECASE)
            if m:
                candidate = clean(m.group(1).strip(" -|,.:"))
                if not is_generic_location(candidate, e["area"]):
                    e["location"] = candidate
    except Exception:
        pass
    return e


async def collect_events():
    global EVENTS, LAST_UPDATE
    all_events = []
    for source in SOURCES:
        try:
            items = await asyncio.to_thread(extract_from_source, source)
            logger.info("%s: найдено %s событий", source["name"], len(items))
            all_events.extend(items)
        except Exception as exc:
            logger.exception("Ошибка источника %s: %s", source["name"], exc)

    if all_events:
        events = deduplicate(all_events)
        # Обогащаем только события, у которых пока нет конкретной площадки.
        generic = [e for e in events if is_generic_location(e.get("location", ""), e.get("area", ""))]
        if generic:
            enriched = await asyncio.gather(*(asyncio.to_thread(enrich_event_location, e) for e in generic))
            generic_by_url = {e["url"]: e for e in enriched}
            events = [generic_by_url.get(e["url"], e) if e["url"] in generic_by_url else e for e in events]
        EVENTS, LAST_UPDATE = deduplicate(events), datetime.now()
        logger.info("Всего после объединения и уточнения площадок: %s", len(EVENTS))
    else:
        logger.warning("Ни одного события не получено; старые данные оставлены без изменений")


def display_location(e):
    area = e.get("area", "")
    venue = clean(e.get("location", ""))
    if not venue or is_generic_location(venue, area):
        return area
    if venue.lower().startswith(area.lower() + ","):
        return venue
    return f"{area}, {venue}"


WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def event_text(e, show_date=True):
    time_str = e["date"].strftime("%H:%M") if e["date"].hour or e["date"].minute else "время не указано"
    place = escape(display_location(e))
    if show_date:
        info = f"{e['date'].strftime('%d.%m.%Y')}, {time_str}, {place}"
    else:
        info = f"{time_str}, {place}"
    return f"<b>{escape(e['title'])}</b>\n{info}\n<a href=\"{escape(e['url'], quote=True)}\">Источник</a>"


def grouped_event_blocks(events):
    groups = {}
    for e in events:
        groups.setdefault(e["date"].date(), []).append(e)
    return [(f"<b>{day.strftime('%d.%m.%Y')} — {WEEKDAYS[day.weekday()]}</b>", groups[day]) for day in sorted(groups)]


def get_user_filters(user_id):
    if user_id not in USER_FILTERS:
        USER_FILTERS[user_id] = {
            "areas": {"Сочи", "Сириус", "Красная Поляна"},
            "types": {"Разовые", "Постоянные"},
        }
    return USER_FILTERS[user_id]


def toggle_filter(user_id, kind, value):
    filters = get_user_filters(user_id)
    selected = filters[kind]
    if value in selected:
        # Не оставляем пустой набор: если снять последнюю галочку,
        # возвращаем все значения. Это предотвращает ситуацию «ничего не выбрано».
        if len(selected) > 1:
            selected.remove(value)
        else:
            if kind == "areas":
                selected.update({"Сочи", "Сириус", "Красная Поляна"})
            else:
                selected.update({"Разовые", "Постоянные"})
    else:
        selected.add(value)


def button_label(value, selected):
    return ("✓ " if value in selected else "□ ") + value


def menu(user_id):
    filters = get_user_filters(user_id)
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Сегодня"), KeyboardButton(text="Завтра"), KeyboardButton(text="7 дней")],
        [
            KeyboardButton(text=button_label("Сочи", filters["areas"])),
            KeyboardButton(text=button_label("Сириус", filters["areas"])),
            KeyboardButton(text=button_label("Красная Поляна", filters["areas"])),
        ],
        [
            KeyboardButton(text=button_label("Разовые", filters["types"])),
            KeyboardButton(text=button_label("Постоянные", filters["types"])),
        ],
    ], resize_keyboard=True)


def apply_filters(user_id, events):
    filters = get_user_filters(user_id)
    return [
        e for e in events
        if e.get("area") in filters["areas"] and filter_event_type(e) in filters["types"]
    ]


async def send_events(message: Message, events, title, group_by_date=False, apply_user_filters=True):
    if apply_user_filters:
        events = apply_filters(message.from_user.id, events)
    if not events:
        await message.answer(f"<b>{escape(title)}</b>\n\nМероприятий не найдено.", reply_markup=menu(message.from_user.id), parse_mode="HTML")
        return
    events = events[:50]
    chunks, current_events, current_parts = [], [], [f"<b>{escape(title)}</b>\nНайдено: {len(events)}"]
    if group_by_date:
        for header, day_events in grouped_event_blocks(events):
            day_text = header + "\n\n" + "\n────────────\n\n".join(event_text(ev, False) for ev in day_events)
            if len("\n\n".join(current_parts) + "\n\n" + day_text) > 3500 and current_events:
                chunks.append((current_parts, current_events))
                current_parts, current_events = [], []
            current_parts.append(day_text)
            current_events.extend(day_events)
    else:
        for ev in events:
            block = event_text(ev)
            if len("\n\n".join(current_parts) + "\n────────────\n\n" + block) > 3500 and current_events:
                chunks.append((current_parts, current_events))
                current_parts, current_events = [], []
            current_parts.append(block)
            current_events.append(ev)
    if current_events:
        chunks.append((current_parts, current_events))
    for text_parts, chunk_events in chunks:
        # После ссылки "Источник" больше нет пустого технического сообщения/кнопки.
        await message.answer("\n────────────\n\n".join(text_parts), reply_markup=menu(message.from_user.id), parse_mode="HTML")


@dp.message(Command("start"))
async def start(message: Message):
    await message.answer(f"<b>Афиша Сочи и Сириуса</b>\n\nМероприятия из реальных источников.\nИспользуйте кнопки ниже.\n\nВерсия бота: <b>{BOT_VERSION}</b>", reply_markup=menu(message.from_user.id), parse_mode="HTML")


@dp.message(Command("version"))
async def version_cmd(message: Message):
    await message.answer(f"Версия бота: <b>{BOT_VERSION}</b>", parse_mode="HTML", reply_markup=menu(message.from_user.id))


@dp.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer("Команды:\n/today — сегодня\n/tomorrow — завтра\n/week — ближайшие 7 дней\n\nАфиша автоматически обновляется при каждом запуске бота и затем каждые 30 минут.", reply_markup=menu(message.from_user.id))


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


@dp.message(lambda m: m.text in {"Сочи", "✓ Сочи", "□ Сочи"})
async def sochi(message: Message):
    toggle_filter(message.from_user.id, "areas", "Сочи")
    filters = get_user_filters(message.from_user.id)
    areas = ", ".join(sorted(filters["areas"], key=["Сочи", "Сириус", "Красная Поляна"].index))
    types = ", ".join(sorted(filters["types"], key=["Разовые", "Постоянные"].index))
    await message.answer(f"Выбраны города: {areas}\nВыбраны типы: {types}", reply_markup=menu(message.from_user.id))


@dp.message(lambda m: m.text in {"Сириус", "✓ Сириус", "□ Сириус"})
async def sirius(message: Message):
    toggle_filter(message.from_user.id, "areas", "Сириус")
    filters = get_user_filters(message.from_user.id)
    areas = ", ".join(sorted(filters["areas"], key=["Сочи", "Сириус", "Красная Поляна"].index))
    types = ", ".join(sorted(filters["types"], key=["Разовые", "Постоянные"].index))
    await message.answer(f"Выбраны города: {areas}\nВыбраны типы: {types}", reply_markup=menu(message.from_user.id))


@dp.message(lambda m: m.text in {"Красная Поляна", "✓ Красная Поляна", "□ Красная Поляна"})
async def krasnaya_polyana(message: Message):
    toggle_filter(message.from_user.id, "areas", "Красная Поляна")
    filters = get_user_filters(message.from_user.id)
    areas = ", ".join(sorted(filters["areas"], key=["Сочи", "Сириус", "Красная Поляна"].index))
    types = ", ".join(sorted(filters["types"], key=["Разовые", "Постоянные"].index))
    await message.answer(f"Выбраны города: {areas}\nВыбраны типы: {types}", reply_markup=menu(message.from_user.id))


@dp.message(lambda m: m.text in {"Разовые", "✓ Разовые", "□ Разовые"})
async def one_time(message: Message):
    toggle_filter(message.from_user.id, "types", "Разовые")
    filters = get_user_filters(message.from_user.id)
    types = ", ".join(sorted(filters["types"], key=["Разовые", "Постоянные"].index))
    areas = ", ".join(sorted(filters["areas"], key=["Сочи", "Сириус", "Красная Поляна"].index))
    await message.answer(f"Выбраны типы: {types}\nВыбраны города: {areas}", reply_markup=menu(message.from_user.id))


@dp.message(lambda m: m.text in {"Постоянные", "✓ Постоянные", "□ Постоянные"})
async def long_term(message: Message):
    toggle_filter(message.from_user.id, "types", "Постоянные")
    filters = get_user_filters(message.from_user.id)
    types = ", ".join(sorted(filters["types"], key=["Разовые", "Постоянные"].index))
    areas = ", ".join(sorted(filters["areas"], key=["Сочи", "Сириус", "Красная Поляна"].index))
    await message.answer(f"Выбраны типы: {types}\nВыбраны города: {areas}", reply_markup=menu(message.from_user.id))


@app.get("/")
async def root():
    return {"status": "ok", "bot": "SochiSiriusEventsBot", "version": BOT_VERSION, "events": len(EVENTS), "last_update": LAST_UPDATE.isoformat() if LAST_UPDATE else None}


@app.get("/health")
async def health():
    return {"status": "ok", "version": BOT_VERSION, "events": len(EVENTS)}


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
