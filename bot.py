import os
import logging
import aiohttp
from datetime import datetime
from typing import Optional

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from aiohttp import web
from motor.motor_asyncio import AsyncIOMotorClient
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN   = os.getenv("BOT_TOKEN")
MONGO_URI   = os.getenv("MONGO_URI", "mongodb://localhost:27017")
PORT        = int(os.getenv("PORT", 8080))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
OWNER_ID    = int(os.getenv("ADMIN_ID", "0"))
GUILD_URL   = "https://www.rucoyonline.com/guild/Imperia%20Of%20Titans"

bot       = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp        = Dispatcher()
router    = Router()
scheduler = AsyncIOScheduler()

mongo_client = AsyncIOMotorClient(MONGO_URI)
db           = mongo_client.rucoy_guild
config_col   = db["config"]
members_col  = db["members"]

# ==================== БД ====================

async def get_config():
    doc = await config_col.find_one({"_id": "main"})
    return doc or {}

async def set_config(chat_id: int, topic_id: Optional[int], chat_title: str):
    await config_col.update_one(
        {"_id": "main"},
        {"$set": {"chat_id": chat_id, "topic_id": topic_id, "chat_title": chat_title}},
        upsert=True
    )

async def get_saved_members():
    doc = await members_col.find_one({"_id": "current"})
    return doc.get("members", []) if doc else []

async def save_members(members: list):
    await members_col.update_one(
        {"_id": "current"},
        {"$set": {"members": members, "updated_at": datetime.now()}},
        upsert=True
    )

# ==================== ПАРСИНГ ====================

async def parse_guild_members() -> list:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"}
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(GUILD_URL, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    logger.error(f"Сайт вернул {resp.status}")
                    return []
                html = await resp.text()

        soup  = BeautifulSoup(html, "lxml")
        table = soup.find("table")
        if not table:
            logger.warning("Таблица не найдена")
            return []

        members = []
        for row in table.find_all("tr")[1:]:
            cols = row.find_all("td")
            if len(cols) < 2:
                continue
            try:
                name_raw  = cols[0].get_text(separator=" ").strip()
                is_leader = "Leader" in name_raw
                is_online = "online" in name_raw.lower()

                # Чистим имя
                name = name_raw
                for tag in ["(Leader)", "Leader", "Supporter", "Support",
                            "Member", "online", "Online"]:
                    name = name.replace(tag, "").strip()

                level = 0
                join_date = ""
                try:
                    level = int(cols[1].get_text(strip=True))
                except Exception:
                    pass
                if len(cols) >= 3:
                    join_date = cols[2].get_text(strip=True)

                if name:
                    members.append({
                        "name":      name,
                        "level":     level,
                        "join_date": join_date,
                        "is_leader": is_leader,
                        "is_online": is_online,
                    })
            except Exception as e:
                logger.warning(f"Ошибка строки: {e}")

        return members
    except Exception as e:
        logger.error(f"Парсинг: {e}")
        return []

# ==================== УВЕДОМЛЕНИЯ ====================

async def send_notification(text: str):
    cfg = await get_config()
    chat_id  = cfg.get("chat_id")
    topic_id = cfg.get("topic_id")
    if not chat_id:
        logger.warning("Чат уведомлений не настроен")
        return
    try:
        kwargs = {"chat_id": chat_id, "text": text}
        if topic_id:
            kwargs["message_thread_id"] = topic_id
        await bot.send_message(**kwargs)
    except Exception as e:
        logger.error(f"Ошибка отправки: {e}")

# ==================== ПРОВЕРКА ИЗМЕНЕНИЙ ====================

async def check_guild_changes():
    try:
        current = await parse_guild_members()
        if not current:
            return

        saved        = await get_saved_members()
        current_names = {m["name"] for m in current}
        saved_names   = {m["name"] for m in saved}

        # Новые участники
        for name in current_names - saved_names:
            m    = next(x for x in current if x["name"] == name)
            role = " 👑 Лидер" if m["is_leader"] else ""
            await send_notification(
                f"🎉 <b>Новый участник!</b>{role}\n\n"
                f"⚔️ Ник: <b>{m['name']}</b>\n"
                f"📈 Уровень: <b>{m['level']}</b>\n"
                f"📅 Вступил: {m['join_date']}\n\n"
                f"Добро пожаловать в <b>Imperia Of Titans</b>! 🔥"
            )

        # Ушедшие
        for name in saved_names - current_names:
            m    = next(x for x in saved if x["name"] == name)
            role = " (Лидер)" if m.get("is_leader") else ""
            await send_notification(
                f"👋 <b>Участник покинул гильдию</b>{role}\n\n"
                f"⚔️ Ник: <b>{m['name']}</b>\n"
                f"📈 Уровень: <b>{m['level']}</b>"
            )

        await save_members(current)
        logger.info(f"Проверка: {len(current)} участников, "
                    f"+{len(current_names - saved_names)} -{len(saved_names - current_names)}")

    except Exception as e:
        logger.error(f"check_guild_changes: {e}")

# ==================== КОМАНДЫ ====================

@router.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "👋 Привет! Я бот гильдии <b>Imperia Of Titans</b> 🔥\n\n"
        "Команды (доступны всем):\n"
        "/online — кто сейчас онлайн\n"
        "/lvl — топ-5 по уровню\n"
        "/members — все участники\n\n"
        "Для владельца:\n"
        "/botguild — настроить этот чат для уведомлений\n"
        "/update — обновить список участников"
    )

@router.message(Command("botguild"))
async def cmd_botguild(message: Message):
    if message.from_user.id != OWNER_ID:
        return

    chat_id    = message.chat.id
    topic_id   = message.message_thread_id
    chat_title = message.chat.title or "Личные сообщения"
    topic_info = f" → тема <code>{topic_id}</code>" if topic_id else ""

    await set_config(chat_id, topic_id, chat_title)
    await message.answer(
        f"✅ <b>Настроено!</b>\n\n"
        f"📍 Чат: <b>{chat_title}</b>{topic_info}\n"
        f"ID: <code>{chat_id}</code>\n\n"
        "Сюда будут приходить уведомления о новых и ушедших участниках."
    )

@router.message(Command("update"))
async def cmd_update(message: Message):
    if message.from_user.id != OWNER_ID:
        return
    await message.answer("🔄 Загружаю данные с сайта гильдии...")
    members = await parse_guild_members()
    if not members:
        await message.answer("❌ Не удалось получить данные. Проверь сайт гильдии.")
        return
    await save_members(members)
    await message.answer(f"✅ Готово! Загружено участников: <b>{len(members)}</b>")

@router.message(Command("online"))
async def cmd_online(message: Message):
    saved   = await get_saved_members()
    online  = [m for m in saved if m.get("is_online")]
    if not online:
        # Пробуем получить свежие данные
        fresh  = await parse_guild_members()
        online = [m for m in fresh if m.get("is_online")]

    if not online:
        await message.answer("🔴 Сейчас никого нет онлайн.")
        return

    text = "🟢 <b>Онлайн участники:</b>\n\n"
    for m in online:
        role  = "👑 " if m.get("is_leader") else "⚔️ "
        text += f"{role}<b>{m['name']}</b> — ур. {m['level']}\n"
    await message.answer(text)

@router.message(Command("lvl"))
async def cmd_lvl(message: Message):
    saved = await get_saved_members()
    if not saved:
        await message.answer("❌ Данные не загружены. Напиши /update")
        return
    top5   = sorted(saved, key=lambda x: x.get("level", 0), reverse=True)[:5]
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    text   = "🏆 <b>Топ-5 игроков гильдии:</b>\n\n"
    for i, m in enumerate(top5):
        role  = "👑 " if m.get("is_leader") else ""
        text += f"{medals[i]} {role}<b>{m['name']}</b> — ур. {m['level']}\n"
    await message.answer(text)

@router.message(Command("members"))
async def cmd_members(message: Message):
    saved = await get_saved_members()
    if not saved:
        await message.answer("❌ Данные не загружены. Напиши /update")
        return
    sorted_m = sorted(saved, key=lambda x: x.get("level", 0), reverse=True)
    text = f"👥 <b>Imperia Of Titans</b> ({len(sorted_m)} уч.):\n\n"
    for m in sorted_m:
        role   = "👑 " if m.get("is_leader") else ""
        online = "🟢" if m.get("is_online") else "⚪"
        text  += f"{online} {role}<b>{m['name']}</b> — ур. {m['level']}\n"
    if len(text) > 4000:
        text = text[:3900] + "\n\n...и другие"
    await message.answer(text)

# ==================== ЗАПУСК ====================

async def health_check(request):
    return web.Response(text="OK")

async def on_startup(app):
    logger.info("Запуск бота...")
    if WEBHOOK_URL:
        url = f"{WEBHOOK_URL}/{BOT_TOKEN}"
        await bot.set_webhook(url)
        logger.info(f"Webhook: {url}")
    else:
        logger.warning("WEBHOOK_URL не задан!")

    if not scheduler.running:
        scheduler.add_job(check_guild_changes, "interval", minutes=5)
        scheduler.start()
        logger.info("Планировщик запущен — проверка каждые 5 мин")

async def on_shutdown(app):
    await bot.session.close()
    if scheduler.running:
        scheduler.shutdown()

def main():
    dp.include_router(router)

    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)

    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=f"/{BOT_TOKEN}")
    setup_application(app, dp, bot=bot)

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    logger.info(f"Сервер на порту {PORT}")
    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
