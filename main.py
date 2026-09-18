import os
import asyncio
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from fastapi import FastAPI
import uvicorn

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

PORT = int(os.getenv("PORT", "10000"))

dp = Dispatcher()
app = FastAPI()

# Временные тестовые данные. Позже подключим автоматический сбор афиши.
EVENTS = [
    {
        "date": "18.09.2026", "time": "20:00",
        "title": "Лесоповал", "place": "Фестивальный, Сочи",
        "status": "✅ ПОДТВЕРЖДЕНО"
    },
    {
        "date": "18.09.2026", "time": "21:00",
        "title": "Лабуград. Ленинград трибьют-шоу", "place": "Треугольник, Сочи",
        "status": "✅ ПОДТВЕРЖДЕНО"
    },
    {
        "date": "19.09.2026", "time": "20:00",
        "title": "Лолита", "place": "Фестивальный, Сочи",
        "status": "✅ ПОДТВЕРЖДЕНО"
    },
]


def menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📅 Сегодня"), KeyboardButton(text="📅 Завтра")],
            [KeyboardButton(text="📆 7 дней"), KeyboardButton(text="📍 Сочи")],
            [KeyboardButton(text="📍 Сириус"), KeyboardButton(text="⚠️ Неподтверждённые")],
        ],
        resize_keyboard=True
    )


def format_events(events):
    if not events:
        return "Пока мероприятий не найдено."
    out = []
    for e in events:
        out.append(
            f"{e['status']}\n"
            f"📅 {e['date']}  🕐 {e['time']}\n"
            f"🎭 {e['title']}\n"
            f"📍 {e['place']}"
        )
    return "\n\n".join(out)


@app.get("/")
async def root():
    return {"status": "ok", "service": "SochiSiriusEventsBot"}


@app.get("/health")
async def health():
    return {"status": "healthy"}


@dp.message(Command("start"))
async def start(message: Message):
    await message.answer(
        "👋 Привет! Это афиша Сочи и Сириуса.\n\n"
        "Сейчас это тестовая версия. Следующим этапом подключим автоматический сбор мероприятий "
        "из источников и Outlook.",
        reply_markup=menu()
    )


@dp.message(Command("today"))
@dp.message(F.text == "📅 Сегодня")
async def today(message: Message):
    await message.answer("📅 МЕРОПРИЯТИЯ СЕГОДНЯ\n\n" + format_events(EVENTS))


@dp.message(Command("tomorrow"))
@dp.message(F.text == "📅 Завтра")
async def tomorrow(message: Message):
    await message.answer("📅 МЕРОПРИЯТИЯ ЗАВТРА\n\nПока тестовых данных нет.")


@dp.message(Command("week"))
@dp.message(F.text == "📆 7 дней")
async def week(message: Message):
    await message.answer("📆 БЛИЖАЙШИЕ 7 ДНЕЙ\n\n" + format_events(EVENTS))


@dp.message(F.text == "📍 Сочи")
async def sochi(message: Message):
    events = [e for e in EVENTS if "Сочи" in e["place"]]
    await message.answer("📍 СОЧИ\n\n" + format_events(events))


@dp.message(F.text == "📍 Сириус")
async def sirius(message: Message):
    events = [e for e in EVENTS if "Сириус" in e["place"]]
    await message.answer("📍 СИРИУС\n\n" + format_events(events))


@dp.message(F.text == "⚠️ Неподтверждённые")
async def unconfirmed(message: Message):
    events = [e for e in EVENTS if "НЕ ПОДТВЕРЖДЕНО" in e["status"]]
    await message.answer("⚠️ НЕ ПОДТВЕРЖДЁННЫЕ\n\n" + format_events(events))


@dp.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer(
        "/start — главное меню\n"
        "/today — сегодня\n"
        "/tomorrow — завтра\n"
        "/week — ближайшие 7 дней"
    )


async def main():
    bot = Bot(TOKEN)
    server = uvicorn.Server(
        uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="info")
    )
    try:
        await asyncio.gather(
            dp.start_polling(bot),
            server.serve(),
        )
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
