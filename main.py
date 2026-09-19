import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
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

BOT_VERSION = "2.0.6-STOP-REPEAT"
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

app = FastAPI()
bot = Bot(BOT_TOKEN)
dp = Dispatcher()
EVENTS = []
LAST_UPDATE = None
CALENDAR_LOG_FILE = os.getenv("CALENDAR_LOG_FILE", "calendar_added_today.json")
CALENDAR_ADDED_LOG = []
SUBSCRIBERS_FILE = os.getenv("SUBSCRIBERS_FILE", "subscribers.json")
SUBSCRIBER_CHAT_IDS = set()
CHANNEL_ID = os.getenv("CHANNEL_ID", "@SochiSiriusEvents")
PUBLICATIONS_FILE = os.getenv("PUBLICATIONS_FILE", "daily_publications.json")
MOSCOW_TZ = ZoneInfo("Europe/Moscow")

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
    {"name": "КЗ «Фестивальный»", "url": "https://www.festivalniy.com/", "area": "Сочи", "official": True},
    {"name": "КЗ «Фестивальный» — афиша ДК Сочи", "url": "https://www.dksochi.ru/playbill/festivalnii", "area": "Сочи", "official": True},
    {"name": "Зимний театр — афиша ДК Сочи", "url": "https://www.dksochi.ru/playbill/zimnii-teatr", "area": "Сочи", "official": True},
    {"name": "Зимний театр — Яндекс Афиша", "url": "https://afisha.yandex.ru/sochi/theatre/places/zimnii-teatr/schedule", "area": "Сочи", "official": False},
    {"name": "Афиша Фестивального — Яндекс Афиша", "url": "https://afisha.yandex.ru/sochi/concert/places/festivalnyi/schedule", "area": "Сочи", "official": False},
    {"name": "Афиша Сочи — локальный агрегатор", "url": "https://afisha-sochi.com/", "area": "Сочи", "official": False},
    {"name": "ИНТЦ «Сириус»", "url": "https://intc.sirius.ru/", "area": "Сириус", "official": True},
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



def extract_image_from_jsonld(obj, page_url=""):
    image = obj.get("image")
    if isinstance(image, list):
        image = image[0] if image else ""
    if isinstance(image, dict):
        image = image.get("url") or image.get("contentUrl") or ""
    return urljoin(page_url, str(image)) if image else ""


def extract_page_image(soup, page_url=""):
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
                    image = extract_image_from_jsonld(obj, page_url)
                    if image:
                        return image
    for prop in ("og:image", "twitter:image"):
        tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        if tag and tag.get("content"):
            return urljoin(page_url, tag["content"])
    return ""


def significance_score(event):
    title = clean(event.get("title", "")).lower()
    score = 0
    if event.get("official"):
        score += 10
    if event.get("category") == "Спорт":
        score += 2
    if event.get("free"):
        score += 1
    for word in [
        "фестиваль", "концерт", "премьера", "финал", "чемпионат",
        "турнир", "симфони", "оркестр", "театр", "балет", "опера",
        "шоу", "марафон", "кубок", "выставка",
    ]:
        if word in title:
            score += 2
    days = (event["date"].date() - datetime.now(MOSCOW_TZ).date()).days
    if days == 0:
        score += 8
    elif days == 1:
        score += 4
    if not is_generic_location(event.get("location", ""), event.get("area", "")):
        score += 3
    if event.get("image_url"):
        score += 3
    return score


def publication_sent(kind, day):
    try:
        data = json.loads(Path(PUBLICATIONS_FILE).read_text(encoding="utf-8"))
    except Exception:
        data = {}
    return data.get(f"{kind}:{day.isoformat()}") is True


def mark_publication(kind, day):
    try:
        try:
            data = json.loads(Path(PUBLICATIONS_FILE).read_text(encoding="utf-8"))
        except Exception:
            data = {}
        data[f"{kind}:{day.isoformat()}"] = True
        Path(PUBLICATIONS_FILE).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        logger.exception("Не удалось сохранить отметку публикации")


async def send_daily_highlight():
    if not EVENTS:
        logger.warning("Главное событие: список мероприятий пуст")
        return False
    today = datetime.now(MOSCOW_TZ).date()
    candidates = [e for e in EVENTS if e["date"].date() == today]
    if not candidates:
        now_msk = datetime.now(MOSCOW_TZ).replace(tzinfo=None)
        candidates = [e for e in EVENTS if e["date"] >= now_msk]
    if not candidates:
        logger.warning("Главное событие: подходящих мероприятий не найдено")
        return False
    event = max(candidates, key=significance_score)
    status = "✅ ПОДТВЕРЖДЕНО" if event.get("confirmed") else "⚠️ НЕ ПОДТВЕРЖДЕНО"
    price = "Бесплатно" if event.get("free") else "Уточняйте на странице мероприятия"
    time_text = event["date"].strftime("%d.%m.%Y, %H:%M") if event["date"].hour or event["date"].minute else event["date"].strftime("%d.%m.%Y")
    text = (f"<b>⭐ Главное событие дня</b>\n\n"
            f"<b>{escape(event['title'])}</b>\n"
            f"📅 {time_text}\n"
            f"📍 {escape(display_location(event))}\n"
            f"💰 {price}\n"
            f"{status}\n\n"
            f"<a href=\"{escape(event['url'], quote=True)}\">Официальная страница мероприятия</a>")
    sent_any = False
    try:
        if event.get("image_url"):
            await bot.send_photo(CHANNEL_ID, event["image_url"], caption=text, parse_mode="HTML")
        else:
            await bot.send_message(CHANNEL_ID, text, parse_mode="HTML")
        sent_any = True
        logger.info("Главное событие опубликовано в канале %s", CHANNEL_ID)
    except Exception:
        logger.exception("Не удалось опубликовать главное событие в канале %s", CHANNEL_ID)
    for chat_id in list(SUBSCRIBER_CHAT_IDS):
        try:
            if event.get("image_url"):
                await bot.send_photo(chat_id, event["image_url"], caption=text, parse_mode="HTML")
            else:
                await bot.send_message(chat_id, text, parse_mode="HTML")
            sent_any = True
        except Exception:
            logger.exception("Не удалось отправить ежедневный пост chat_id=%s", chat_id)
    return sent_any


# Автопубликация главного события: тест 19.09.2026 был строго ограничен
# окном 13:30–13:35. После теста обычный режим начинается с 20.09.2026.
# Это важно: нельзя использовать условие «после 13:30», иначе после перезапуска
# Render бот сразу публикует пост заново.
HIGHLIGHT_TEST_DATE = "2026-09-19"
HIGHLIGHT_TEST_START = (13, 30)
HIGHLIGHT_TEST_END = (13, 35)

async def daily_highlight_loop():
    while True:
        try:
            now = datetime.now(MOSCOW_TZ)
            day = now.date()
            # 19.09 — только узкое тестовое окно. Если оно уже прошло,
            # автоматическая публикация сегодня больше не запускается.
            if day.isoformat() == HIGHLIGHT_TEST_DATE:
                current = (now.hour, now.minute)
                in_test_window = HIGHLIGHT_TEST_START <= current < HIGHLIGHT_TEST_END
            else:
                # С 20.09 обычное ежедневное окно: 12:30–12:35.
                current = (now.hour, now.minute)
                in_test_window = (12, 30) <= current < (12, 35)

            if in_test_window and not publication_sent("highlight", day):
                if await send_daily_highlight():
                    mark_publication("highlight", day)
                    logger.info("Автоматическая публикация главного события отмечена как выполненная: %s", day)
        except Exception:
            logger.exception("Ошибка ежедневного поста")
        await asyncio.sleep(30)

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
                                "official": source["official"],
                                "event_page": bool(obj.get("url")) and urljoin(source["url"], obj.get("url")).rstrip("/") != source["url"].rstrip("/"),
                                "confirmed": source["official"],
                                "image_url": extract_image_from_jsonld(obj, source["url"]),
                                "event_type": classify_event(title, json.dumps(obj, ensure_ascii=False)),
                                "category": classify_category(title, json.dumps(obj, ensure_ascii=False)),
                                "free": is_free_event(title, json.dumps(obj, ensure_ascii=False)),
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
        title_lower = clean(title).lower()

        generic_title_patterns = [
            r"^афиша$",
            r"^.*:\s*афиша$",
            r"^афиша\s+.*$",
            r"^все\s+мероприятия$",
            r"^ближайшие\s+мероприятия$",
            r"^расписание$",
            r"^программа$",
            r"^программа\s+мероприятий$",
            r"^события$",
        ]
        if (
            len(title) < 4
            or title_lower in ("войти", "подробнее", "все мероприятия", "культура", "спорт", "кино", "умный туризм", "главная")
            or any(re.match(pattern, title_lower) for pattern in generic_title_patterns)
        ):
            continue

        if absolute.rstrip("/") == source["url"].rstrip("/"):
            continue

        location = extract_location(container, source["area"])
        found.append({
            "title": title,
            "date": dt,
            "location": location,
            "area": source["area"],
            "source": source["name"],
            "url": absolute,
            "official": source["official"],
            "event_page": absolute.rstrip("/") != source["url"].rstrip("/"),
            "confirmed": source["official"],
            "image_url": extract_page_image(container if hasattr(container, "find_all") else soup, absolute),
            "event_type": classify_event(title, block),
            "category": classify_category(title, block),
            "free": is_free_event(title, block),
        })
    return found



def is_free_event(title: str, source_text: str = "") -> bool:
    """Определяет бесплатные мероприятия только по явным признакам бесплатного входа."""
    text = clean(f"{title} {source_text}").lower()
    # Не считаем бесплатной парковку/доставку и т.п. — нужен признак именно входа
    free_patterns = [
        r"\bбесплатн(?:ый|ая|ое|ые|о)\s+(?:вход|посещение|участие)\b",
        r"\bвход\s+свободн(?:ый|а|ое)\b",
        r"\bсвободн(?:ый|а|ое)\s+посещение\b",
        r"\bучастие\s+бесплатн(?:ое|о)\b",
        r"\bбесплатно\b",
        r"\bfree\s+(?:entry|admission)\b",
        r"\bвход\s*[:\-]?\s*0\s*(?:₽|руб(?:\.|лей)?)\b",
        r"\bстоимость\s*[:\-]?\s*0\s*(?:₽|руб(?:\.|лей)?)\b",
        r"\bцена\s*[:\-]?\s*0\s*(?:₽|руб(?:\.|лей)?)\b",
    ]
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in free_patterns)


def classify_category(title: str, source_text: str = "") -> str:
    """Определяет тематическую категорию мероприятия. Спорт выделяется отдельно."""
    text = clean(f"{title} {source_text}").lower()
    sport_keywords = [
        "спорт", "спортивн", "футбол", "хоккей", "баскетбол", "волейбол",
        "теннис", "падел", "бадминтон", "регби", "гандбол", "фигурн",
        "катани", "лыж", "сноуборд", "биатлон", "триатлон", "плавани",
        "марафон", "забег", "кросс", "бег", "велогон", "велосипед",
        "велозаезд", "автоспорт", "автогон", "мотогон", "ралли",
        "формула-1", "формула 1", "гонка", "картинг", "бокс", "mma",
        "единоборств", "дзюдо", "самбо", "карате", "тхэквондо",
        "гимнастик", "легкая атлетика", "тяжелая атлетика", "шахмат",
        "киберспорт", "турнир", "чемпионат", "первенство", "кубок",
        "спартакиад", "соревнован", "матч", "старт", "дистанция",
    ]
    return "Спорт" if any(x in text for x in sport_keywords) else "Другое"


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

def event_source_score(event):
    return (
        100 if event.get("official") else 0,
        20 if event.get("event_page") else 0,
        len(event.get("url", "")) / 10000,
    )


def choose_better_event(a, b):
    return b if event_source_score(b) > event_source_score(a) else a


def normalized_event_title(title):
    text = clean(title).lower()
    text = re.sub(r'^кз\s*[«"]?фестивальный[»"]?\s*[:—-]\s*', "", text)
    text = re.sub(r'^фестивальный\s*[:—-]\s*', "", text)
    return re.sub(r"[^a-zа-я0-9]+", "", text)


def deduplicate(events):
    unique = {}
    for e in events:
        key = (
            normalized_event_title(e.get("title", "")),
            e["date"].strftime("%Y-%m-%d %H:%M"),
            e.get("area", "").lower(),
        )
        unique[key] = choose_better_event(unique[key], e) if key in unique else e
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
            "categories": {"Спорт", "Другое"},
            "free_only": False,
        }
    return USER_FILTERS[user_id]


def load_calendar_added_log():
    """Загружает журнал событий, которые бот фактически добавил в календарь."""
    global CALENDAR_ADDED_LOG
    try:
        if os.path.exists(CALENDAR_LOG_FILE):
            with open(CALENDAR_LOG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                CALENDAR_ADDED_LOG = data if isinstance(data, list) else []
        else:
            CALENDAR_ADDED_LOG = []
    except Exception:
        logger.exception("Не удалось загрузить журнал добавлений в календарь")
        CALENDAR_ADDED_LOG = []


def record_calendar_added(event):
    """Записывает факт реального добавления события в календарь.

    Эту функцию вызывает модуль синхронизации с Outlook после успешного создания
    события. Само обнаружение мероприятия в интернете сюда не записывается.
    """
    global CALENDAR_ADDED_LOG
    item = {
        "added_at": datetime.now().isoformat(),
        "title": event.get("title", ""),
        "date": event.get("date").isoformat() if event.get("date") else "",
        "area": event.get("area", ""),
        "location": event.get("location", ""),
        "status": event.get("status", "⚠️ НЕ ПОДТВЕРЖДЕНО"),
        "source": event.get("source", ""),
    }
    CALENDAR_ADDED_LOG.append(item)
    # Храним журнал за последние 31 день, чтобы файл не рос бесконечно.
    cutoff = datetime.now() - timedelta(days=31)
    CALENDAR_ADDED_LOG = [
        x for x in CALENDAR_ADDED_LOG
        if x.get("added_at") and datetime.fromisoformat(x["added_at"]) >= cutoff
    ]
    try:
        with open(CALENDAR_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(CALENDAR_ADDED_LOG, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.exception("Не удалось сохранить журнал добавлений в календарь")


def calendar_added_today():
    today = datetime.now().date()
    result = []
    for item in CALENDAR_ADDED_LOG:
        try:
            if datetime.fromisoformat(item["added_at"]).date() == today:
                result.append(item)
        except Exception:
            continue
    return sorted(result, key=lambda x: x.get("added_at", ""), reverse=True)


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
        [KeyboardButton(text=button_label("Спорт", filters["categories"]))],
        [KeyboardButton(text=button_label("Бесплатно", {"Бесплатно"} if filters.get("free_only") else set()))],
    ], resize_keyboard=True)


def apply_filters(user_id, events):
    filters = get_user_filters(user_id)
    return [
        e for e in events
        if e.get("area") in filters["areas"]
        and filter_event_type(e) in filters["types"]
        and (set(filters.get("categories", {"Спорт", "Другое"})) == {"Спорт", "Другое"}
             or e.get("category", "Другое") in filters.get("categories", {"Спорт", "Другое"}))
        and (not filters.get("free_only", False) or e.get("free", False))
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


def load_subscribers():
    global SUBSCRIBER_CHAT_IDS
    try:
        if os.path.exists(SUBSCRIBERS_FILE):
            with open(SUBSCRIBERS_FILE, "r", encoding="utf-8") as f:
                SUBSCRIBER_CHAT_IDS = {int(x) for x in json.load(f)}
    except Exception:
        logger.exception("Не удалось загрузить список подписчиков")


def save_subscribers():
    try:
        with open(SUBSCRIBERS_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(SUBSCRIBER_CHAT_IDS), f)
    except Exception:
        logger.exception("Не удалось сохранить список подписчиков")


@dp.message(Command("start"))
async def start(message: Message):
    SUBSCRIBER_CHAT_IDS.add(message.chat.id)
    save_subscribers()
    await message.answer(f"<b>Афиша Сочи и Сириуса</b>\n\nМероприятия из реальных источников.\nИспользуйте кнопки ниже.\n\nВерсия бота: <b>{BOT_VERSION}</b>", reply_markup=menu(message.from_user.id), parse_mode="HTML")


@dp.message(Command("version"))
async def version_cmd(message: Message):
    await message.answer(f"Версия бота: <b>{BOT_VERSION}</b>", parse_mode="HTML", reply_markup=menu(message.from_user.id))


@dp.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer("Команды:\n/today — сегодня\n/tomorrow — завтра\n/week — ближайшие 7 дней\n/new — что добавлено в календарь сегодня\n/источник — сайты, которые ежедневно мониторит бот\n\nАфиша автоматически обновляется при каждом запуске бота и затем каждые 30 минут.", reply_markup=menu(message.from_user.id))


def sources_message() -> str:
    lines = ["<b>Источники ежедневного мониторинга</b>", ""]
    for i, source in enumerate(SOURCES, 1):
        status = "официальный" if source.get("official") else "агрегатор"
        lines.append(
            f"{i}. <b>{escape(source['name'])}</b> — {status}\n"
            f"   <a href=\"{escape(source['url'], quote=True)}\">{escape(source['url'])}</a>"
        )
    lines.append("")
    lines.append(f"Всего источников: <b>{len(SOURCES)}</b>")
    return "\n".join(lines)


# Telegram Bot API обычно ожидает латинские команды, поэтому дополнительно
# поддерживаем /source. При этом пользовательская команда /источник тоже работает.
@dp.message(Command("source"))
@dp.message(lambda m: (m.text or "").split()[0].lower() == "/источник")
async def sources_cmd(message: Message):
    await message.answer(
        sources_message(),
        reply_markup=menu(message.from_user.id),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


@dp.message(Command("sport"))
async def sport_command(message: Message):
    get_user_filters(message.from_user.id)["categories"] = {"Спорт"}
    await send_events(message, [e for e in EVENTS if e.get("category") == "Спорт"], "Спорт", apply_user_filters=False)


@dp.message(Command("free"))
async def free_command(message: Message):
    get_user_filters(message.from_user.id)["free_only"] = True
    await send_events(message, [e for e in EVENTS if e.get("free")], "Бесплатно", apply_user_filters=False)


@dp.message(lambda m: m.text in {"Спорт", "✓ Спорт", "□ Спорт"})
async def sport_filter(message: Message):
    filters = get_user_filters(message.from_user.id)
    selected = filters["categories"]
    if selected == {"Спорт", "Другое"}:
        filters["categories"] = {"Спорт"}
    else:
        filters["categories"] = {"Спорт", "Другое"}
    category_text = "только Спорт" if filters["categories"] == {"Спорт"} else "все категории"
    await message.answer(f"Категория: {category_text}", reply_markup=menu(message.from_user.id))


@dp.message(lambda m: m.text in {"Бесплатно", "✓ Бесплатно", "□ Бесплатно"})
async def free_filter(message: Message):
    filters = get_user_filters(message.from_user.id)
    filters["free_only"] = not filters.get("free_only", False)
    status = "только бесплатные мероприятия" if filters["free_only"] else "все мероприятия"
    await message.answer(f"Фильтр: {status}", reply_markup=menu(message.from_user.id))


@dp.message(Command("new"))
@dp.message(lambda m: m.text == "Новое")
async def new_events(message: Message):
    items = calendar_added_today()
    if not items:
        await message.answer(
            "<b>Новое</b>\n\nСегодня бот пока ничего не добавил в календарь.",
            reply_markup=menu(message.from_user.id),
            parse_mode="HTML",
        )
        return

    parts = [f"<b>Новое за сегодня</b>\nДобавлено в календарь: <b>{len(items)}</b>\n"]
    for item in items:
        dt = item.get("date", "")
        try:
            event_dt = datetime.fromisoformat(dt)
            date_text = event_dt.strftime("%d.%m.%Y")
            time_text = event_dt.strftime("%H:%M") if event_dt.hour or event_dt.minute else "время не указано"
        except Exception:
            date_text, time_text = dt, ""
        location = item.get("location") or item.get("area") or "Место не указано"
        status = item.get("status") or "⚠️ НЕ ПОДТВЕРЖДЕНО"
        parts.append(
            f"<b>{escape(item.get('title', 'Без названия'))}</b>\n"
            f"📅 {date_text} {time_text}\n"
            f"📍 {escape(location)}\n"
            f"{escape(status)}"
        )
    await message.answer("\n\n────────────\n\n".join(parts), reply_markup=menu(message.from_user.id), parse_mode="HTML")


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


@dp.message(Command("главное"))
async def manual_highlight(message: Message):
    if message.chat.type == "private":
        SUBSCRIBER_CHAT_IDS.add(message.chat.id)
        save_subscribers()
    ok = await send_daily_highlight()
    await message.answer("⭐ Главное событие опубликовано в канале и отправлено подписчикам." if ok else "Не удалось найти или отправить главное событие. Проверьте логи Render.")


@app.get("/")
async def root():
    return {"status": "ok", "bot": "SochiSiriusEventsBot", "version": BOT_VERSION, "events": len(EVENTS), "last_update": LAST_UPDATE.isoformat() if LAST_UPDATE else None}


@app.api_route("/health", methods=["GET", "HEAD"])
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
    load_calendar_added_log()
    load_subscribers()
    await collect_events()
    collector_task = asyncio.create_task(collector_loop())
    bot_task = asyncio.create_task(bot_loop())
    daily_highlight_task = asyncio.create_task(daily_highlight_loop())
    config = uvicorn.Config(app, host="0.0.0.0", port=int(os.getenv("PORT", "10000")), log_level="info")
    server = uvicorn.Server(config)
    web_task = asyncio.create_task(server.serve())
    try:
        await asyncio.gather(collector_task, bot_task, web_task)
    finally:
        collector_task.cancel()
        bot_task.cancel()
        daily_highlight_task.cancel()
        web_task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
