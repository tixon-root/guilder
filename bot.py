import os
import asyncio
import logging
import threading
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from flask import Flask, request as flask_request, Response

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

from motor.motor_asyncio import AsyncIOMotorClient
from apscheduler.schedulers.asyncio import AsyncIOScheduler

load_dotenv()

# ══════════════════════════════════════════════════════════════
#  КОНФИГ
# ══════════════════════════════════════════════════════════════

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN   = os.getenv("BOT_TOKEN")
MONGO_URI   = os.getenv("MONGO_URI", "mongodb://localhost:27017")
PORT        = int(os.getenv("PORT", 8080))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")          # https://yourapp.onrender.com  (без / в конце)
OWNER_ID    = int(os.getenv("ADMIN_ID", "6395348885"))
GUILD_URL   = "https://www.rucoyonline.com/guild/Imperia%20Of%20Titans"

CHECK_INTERVAL_MINUTES = 1   # как часто проверять сайт гильдии

# ══════════════════════════════════════════════════════════════
#  ИНИЦИАЛИЗАЦИЯ
# ══════════════════════════════════════════════════════════════

bot       = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp        = Dispatcher()
router    = Router()
scheduler = AsyncIOScheduler()

mongo_client = AsyncIOMotorClient(MONGO_URI)
db           = mongo_client.rucoy_guild
settings_col = db.settings
members_col  = db.members

flask_app = Flask(__name__)

# Один event loop на всё приложение (Flask кидает в него задачи)
main_loop: asyncio.AbstractEventLoop = None


# ══════════════════════════════════════════════════════════════
#  ПАРСИНГ
#
#  Реальная структура таблицы (проверено на сайте):
#    cols[0] = Name + роль/статус внутри текста
#              "Hero Of Titan\nSupporter"
#              "Shop Nomber One\n(Leader)"
#              "NickName\nOnline"   ← когда игрок в сети
#    cols[1] = Level (число)
#    cols[2] = Join date ("Aug 12, 2025")
# ══════════════════════════════════════════════════════════════

def parse_guild_members() -> list[dict]:
    """Синхронный парсинг — запускать через run_in_executor."""
    try:
        resp = requests.get(
            GUILD_URL, timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (compatible; GuildBot/1.0)"}
        )
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.content, "html.parser")

        table = soup.find("table")
        if not table:
            logger.warning("Таблица участников не найдена на странице")
            return []

        members = []
        for row in table.find_all("tr")[1:]:    # пропускаем заголовок
            cols = row.find_all("td")
            if len(cols) < 2:
                continue

            # --- Name + роль ---
            raw   = cols[0].get_text(separator="\n", strip=True)
            lines = [l.strip() for l in raw.splitlines() if l.strip()]
            name      = lines[0] if lines else ""
            role_text = " ".join(lines[1:]).lower() if len(lines) > 1 else ""

            if not name:
                continue

            is_leader = "leader" in role_text
            is_online = "online" in role_text

            # --- Level ---
            level_str = cols[1].get_text(strip=True)
            level     = int(level_str) if level_str.isdigit() else 0

            # --- Join date ---
            join_date = cols[2].get_text(strip=True) if len(cols) >= 3 else ""

            members.append({
                "name":      name,
                "level":     level,
                "join_date": join_date,
                "role":      role_text,
                "is_online": is_online,
                "is_leader": is_leader,
            })

        logger.info(f"Спарсено {len(members)} участников")
        return members

    except Exception as e:
        logger.error(f"parse_guild_members: {e}")
        return []


async def fetch_members() -> list[dict]:
    """Асинхронная обёртка над парсером."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, parse_guild_members)


# ══════════════════════════════════════════════════════════════
#  НАСТРОЙКИ УВЕДОМЛЕНИЙ
# ══════════════════════════════════════════════════════════════

async def get_notify_chat() -> tuple:
    doc = await settings_col.find_one({"_id": "notify"})
    if not doc:
        return None, None
    return doc.get("chat_id"), doc.get("topic_id")


async def save_notify_chat(chat_id: int, topic_id, chat_title: str):
    await settings_col.update_one(
        {"_id": "notify"},
        {"$set": {
            "chat_id":    chat_id,
            "topic_id":   topic_id,
            "chat_title": chat_title,
        }},
        upsert=True
    )


async def send_notify(text: str):
    chat_id, topic_id = await get_notify_chat()
    if not chat_id:
        logger.warning("Чат уведомлений не настроен — используй /botguild")
        return
    try:
        kwargs = {"chat_id": chat_id, "text": text}
        if topic_id:
            kwargs["message_thread_id"] = topic_id
        await bot.send_message(**kwargs)
    except Exception as e:
        logger.error(f"send_notify: {e}")


# ══════════════════════════════════════════════════════════════
#  АВТОПРОВЕРКА ИЗМЕНЕНИЙ
# ══════════════════════════════════════════════════════════════

async def check_guild_changes():
    """Сравнивает текущий список участников с сохранённым в БД.
    Шлёт уведомления при изменениях, обновляет снимок."""
    try:
        current = await fetch_members()
        if not current:
            return

        current_map = {m["name"]: m for m in current}
        old_docs    = await members_col.find({}).to_list(length=None)
        old_map     = {d["name"]: d for d in old_docs}

        # Новые участники
        for name, m in current_map.items():
            if name not in old_map:
                role_tag = " 👑 <b>Лидер</b>" if m["is_leader"] else ""
                await send_notify(
                    f"🎉 <b>Новый участник!</b>{role_tag}\n\n"
                    f"⚔️ Ник: <b>{m['name']}</b>\n"
                    f"📈 Уровень: <b>{m['level']}</b>\n"
                    f"📅 Вступил: {m['join_date']}\n\n"
                    "Добро пожаловать в <b>Imperia Of Titans</b>! 🔥"
                )
                logger.info(f"Новый участник: {name}")

        # Ушедшие участники
        for name, d in old_map.items():
            if name not in current_map:
                role_tag = " (Лидер)" if d.get("is_leader") else ""
                await send_notify(
                    f"👋 <b>Участник покинул гильдию</b>{role_tag}\n\n"
                    f"⚔️ Ник: <b>{d['name']}</b>\n"
                    f"📈 Уровень: <b>{d['level']}</b>"
                )
                logger.info(f"Ушёл: {name}")

        # Обновляем снимок в БД
        await members_col.delete_many({})
        await members_col.insert_many(current)

    except Exception as e:
        logger.error(f"check_guild_changes: {e}")


# ══════════════════════════════════════════════════════════════
#  КОМАНДЫ БОТА
# ══════════════════════════════════════════════════════════════

@router.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "👋 Привет! Я бот гильдии <b>Imperia Of Titans</b> 🔥\n\n"
        "<b>Команды для всех:</b>\n"
        "/online — кто сейчас онлайн\n"
        "/lvl — топ-5 по уровню\n\n"
        "<b>Для владельца:</b>\n"
        "/botguild — настроить эту тему/чат для уведомлений\n\n"
        f"⏱ Данные обновляются автоматически каждые {CHECK_INTERVAL_MINUTES} мин."
    )


@router.message(Command("botguild"))
async def cmd_botguild(message: Message):
    if message.from_user.id != OWNER_ID:
        await message.answer("❌ Эта команда только для владельца бота.")
        return

    chat_id    = message.chat.id
    topic_id   = message.message_thread_id      # None если без тем
    chat_title = message.chat.title or "Личные сообщения"
    topic_info = f"\n🗂 Тема ID: <code>{topic_id}</code>" if topic_id else ""

    await save_notify_chat(chat_id, topic_id, chat_title)
    await message.answer(
        f"✅ <b>Готово!</b>\n\n"
        f"📍 Чат: <b>{chat_title}</b>{topic_info}\n"
        f"🆔 Chat ID: <code>{chat_id}</code>\n\n"
        f"Уведомления о входе/выходе участников будут приходить сюда.\n"
        f"⏱ Проверка каждые {CHECK_INTERVAL_MINUTES} мин."
    )


@router.message(Command("online"))
async def cmd_online(message: Message):
    # Всегда свежие данные прямо с сайта
    members = await fetch_members()
    online  = [m for m in members if m["is_online"]]

    if not online:
        await message.answer("⚪ Сейчас никого нет онлайн.")
        return

    text = f"🟢 <b>Онлайн ({len(online)}):</b>\n\n"
    for m in online:
        icon  = "👑" if m["is_leader"] else "⚔️"
        text += f"{icon} <b>{m['name']}</b> — ур. {m['level']}\n"
    await message.answer(text)


@router.message(Command("lvl"))
async def cmd_lvl(message: Message):
    # Берём из БД (обновляется автоматически)
    all_m = await members_col.find({}).to_list(length=None)
    if not all_m:
        await message.answer("⏳ Данные ещё загружаются, подожди минуту.")
        return

    top5   = sorted(all_m, key=lambda x: x.get("level", 0), reverse=True)[:5]
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    text   = "🏆 <b>Топ-5 игроков гильдии:</b>\n\n"
    for i, m in enumerate(top5):
        icon  = "👑 " if m.get("is_leader") else ""
        text += f"{medals[i]} {icon}<b>{m['name']}</b> — ур. {m['level']}\n"
    await message.answer(text)


# ══════════════════════════════════════════════════════════════
#  FLASK — вебхук + health check
# ══════════════════════════════════════════════════════════════

@flask_app.get("/")
@flask_app.get("/health")
def health():
    return Response("OK", status=200)


@flask_app.post(f"/{BOT_TOKEN}")
def webhook():
    """Принимает апдейты Telegram и передаёт их в aiogram."""
    from aiogram.types import Update

    data = flask_request.get_json(force=True, silent=True)
    if not data:
        return Response("Bad Request", status=400)

    async def _process():
        update = Update.model_validate(data)
        await dp.feed_update(bot, update)

    asyncio.run_coroutine_threadsafe(_process(), main_loop)
    return Response("OK", status=200)


# ══════════════════════════════════════════════════════════════
#  ЗАПУСК
# ══════════════════════════════════════════════════════════════

async def bot_main():
    global main_loop
    main_loop = asyncio.get_running_loop()

    dp.include_router(router)

    # Устанавливаем webhook
    if not WEBHOOK_URL:
        logger.error("WEBHOOK_URL не задан! Бот не будет получать сообщения от Telegram.")
    else:
        full_url = f"{WEBHOOK_URL.rstrip('/')}/{BOT_TOKEN}"
        await bot.set_webhook(full_url)
        logger.info(f"Webhook установлен: {full_url}")

    # Первоначальная загрузка участников если БД пустая
    count = await members_col.count_documents({})
    if count == 0:
        logger.info("БД пустая — загружаем первоначальный список...")
        members = await fetch_members()
        if members:
            await members_col.insert_many(members)
            logger.info(f"Загружено {len(members)} участников")

    # Запускаем планировщик
    scheduler.add_job(
        check_guild_changes,
        "interval",
        minutes=CHECK_INTERVAL_MINUTES,
        id="guild_check",
        max_instances=1,    # не запускать параллельно
    )
    scheduler.start()
    logger.info(f"Планировщик запущен — проверка каждые {CHECK_INTERVAL_MINUTES} мин.")

    # Держим loop живым
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await bot.session.close()
        scheduler.shutdown(wait=False)


def run_bot():
    """Запускает asyncio event loop с ботом в отдельном потоке."""
    asyncio.run(bot_main())


def main():
    # Бот в фоновом потоке
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    # Небольшая пауза чтобы loop успел стартануть до Flask
    import time
    time.sleep(2)

    logger.info(f"Flask стартует на порту {PORT}")
    flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
