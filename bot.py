import os
import asyncio
import logging
import aiohttp
from datetime import datetime
from typing import Optional, Dict, List

from aiogram import Bot, Dispatcher, F, Router
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
settings_col = db.settings
members_col  = db.members


# ==================== НАСТРОЙКИ ЧАТА ====================

async def get_notify_chat():
    doc = await settings_col.find_one({"_id": "notify"})
    if not doc:
        return None, None
    return doc.get("chat_id"), doc.get("topic_id")

async def save_notify_chat(chat_id: int, topic_id, chat_title: str):
    await settings_col.update_one(
        {"_id": "notify"},
        {"$set": {"chat_id": chat_id, "topic_id": topic_id, "chat_title": chat_title}},
        upsert=True
    )

async def send_notify(text: str):
    chat_id, topic_id = await get_notify_chat()
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

# ==================== ПАРСИНГ ГИЛЬДИИ ====================

async def parse_guild():
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"}
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(GUILD_URL, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    logger.error(f"Сайт вернул {resp.status}")
                    return None
                html = await resp.text()

        soup = BeautifulSoup(html, "lxml")
        members = []
        table = soup.find("table")
        if not table:
            logger.warning("Таблица не найдена")
            return None

        for row in table.find_all("tr")[1:]:
            cols = row.find_all("td")
            if len(cols) < 2:
                continue
            try:
                name_text = cols[0].get_text(separator=" ").strip()
                is_leader = "Leader" in name_text
                is_online = "online" in name_text.lower()

                nick = name_text
                for tag in ["(Leader)", "Leader", "Supporter", "Support",
                            "Member", "online", "Online"]:
                    nick = nick.replace(tag, "").strip()

                level = 0
                join_date = ""
                try:
                    level = int(cols[1].get_text(strip=True))
                except Exception:
                    pass
                if len(cols) >= 3:
                    join_date = cols[2].get_text(strip=True)

                if nick:
                    members.append({
                        "nick":      nick,
                        "level":     level,
                        "join_date": join_date,
                        "is_leader": is_leader,
                        "is_online": is_online,
                    })
            except Exception as e:
                logger.warning(f"Строка: {e}")
                continue

        return members
    except Exception as e:
        logger.error(f"Парсинг: {e}")
        return None

# ==================== ПРОВЕРКА ИЗМЕНЕНИЙ ====================

async def check_guild_changes():
    try:
        new_members = await parse_guild()
        if new_members is None:
            return

        new_nicks = {m["nick"]: m for m in new_members}
        old_docs  = await members_col.find({}).to_list(length=None)
        old_nicks = {d["nick"]: d for d in old_docs}

        joined = [m for nick, m in new_nicks.items() if nick not in old_nicks]
        left   = [d for nick, d in old_nicks.items() if nick not in new_nicks]

        for m in joined:
            role = " 👑 <b>Лидер</b>" if m["is_leader"] else ""
            text = (
                f"🎉 <b>Новый участник!</b>{role}\n\n"
                f"⚔️ Ник: <b>{m['nick']}</b>\n"
                f"📈 Уровень: <b>{m['level']}</b>\n"
                f"📅 Вступил: {m['join_date']}\n\n"
                f"Добро пожаловать в <b>Imperia Of Titans</b>! 🔥"
            )
            await send_notify(text)

        for d in left:
            role = " (Лидер)" if d.get("is_leader") else ""
            text = (
                f"👋 <b>Участник покинул гильдию</b>{role}\n\n"
                f"⚔️ Ник: <b>{d['nick']}</b>\n"
                f"📈 Уровень: <b>{d['level']}</b>"
            )
            await send_notify(text)

        if joined or left:
            await members_col.delete_many({})
            if new_members:
                await members_col.insert_many(new_members)
        elif not old_docs and new_members:
            await members_col.insert_many(new_members)
            logger.info(f"Первичная загрузка: {len(new_members)} участников")

    except Exception as e:
        logger.error(f"check_guild_changes: {e}")


# ==================== КОМАНДЫ ====================

@router.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "👋 Привет! Я бот гильдии <b>Imperia Of Titans</b> 🔥\n\n"
        "Команды:\n"
        "/online — онлайн участники\n"
        "/lvl — топ-5 по уровню\n"
        "/members — все участники\n\n"
        "Для владельца:\n"
        "/botguild — настроить этот чат для уведомлений\n"
        "/update — обновить данные гильдии"
    )


@router.message(Command("botguild"))
async def cmd_botguild(message: Message):
    if message.from_user.id != OWNER_ID:
        return

    chat_id    = message.chat.id
    topic_id   = message.message_thread_id
    chat_title = message.chat.title or "Личные сообщения"
    topic_info = f" → тема <code>{topic_id}</code>" if topic_id else ""

    await save_notify_chat(chat_id, topic_id, chat_title)

    await message.answer(
        f"✅ <b>Готово! Уведомления настроены.</b>\n\n"
        f"📍 Чат: <b>{chat_title}</b>{topic_info}\n"
        f"ID: <code>{chat_id}</code>\n\n"
        "Сюда будут приходить уведомления о новых и ушедших участниках."
    )


@router.message(Command("update"))
async def cmd_update(message: Message):
    if message.from_user.id != OWNER_ID:
        return

    await message.answer("🔄 Обновляю данные гильдии...")
    members = await parse_guild()

    if not members:
        await message.answer("❌ Не удалось получить данные с сайта.")
        return

    await members_col.delete_many({})
    await members_col.insert_many(members)
    await message.answer(f"✅ Обновлено! Участников: <b>{len(members)}</b>")


@router.message(Command("online"))
async def cmd_online(message: Message):
    members = await members_col.find({"is_online": True}).to_list(length=None)
    if not members:
        await message.answer("🔴 Сейчас никого нет онлайн.")
        return
    text = "🟢 <b>Онлайн участники:</b>\n\n"
    for m in members:
        role  = "👑 " if m.get("is_leader") else ""
        text += f"{role}⚔️ <b>{m['nick']}</b> — ур. {m['level']}\n"
    await message.answer(text)


@router.message(Command("lvl"))
async def cmd_lvl(message: Message):
    all_m = await members_col.find({}).to_list(length=None)
    if not all_m:
        await message.answer("❌ Данные ещё не загружены. Напиши /update")
        return
    top5   = sorted(all_m, key=lambda x: x.get("level", 0), reverse=True)[:5]
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    text   = "🏆 <b>Топ-5 игроков гильдии:</b>\n\n"
    for i, m in enumerate(top5):
        role  = "👑 " if m.get("is_leader") else ""
        text += f"{medals[i]} {role}<b>{m['nick']}</b> — ур. {m['level']}\n"
    await message.answer(text)


@router.message(Command("members"))
async def cmd_members(message: Message):
    all_m = await members_col.find({}).to_list(length=None)
    if not all_m:
        await message.answer("❌ Данные ещё не загружены. Напиши /update")
        return
    sorted_m = sorted(all_m, key=lambda x: x.get("level", 0), reverse=True)
    text = f"👥 <b>Imperia Of Titans</b> ({len(sorted_m)} участников):\n\n"
    for m in sorted_m:
        role   = "👑 " if m.get("is_leader") else ""
        online = "🟢" if m.get("is_online") else "⚪"
        text  += f"{online} {role}<b>{m['nick']}</b> — ур. {m['level']}\n"
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
        logger.info("Планировщик: проверка каждые 5 мин")

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
