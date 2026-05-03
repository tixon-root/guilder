import os
import asyncio
import logging
import aiohttp
from datetime import datetime, timedelta
from typing import Optional, Dict, List

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from aiohttp import web
from motor.motor_asyncio import AsyncIOMotorClient
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

# ==================== ЛОГИРОВАНИЕ ====================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== КОНФИГУРАЦИЯ ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
PORT = int(os.getenv("PORT", 8080))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # https://твое-приложение.onrender.com
OWNER_ID = int(os.getenv("ADMIN_ID", "0"))

ACHIEVEMENTS = {
    "farm_10":    {"name": "🌱 Росток",          "desc": "10 сеансов фарма",    "reward": 10},
    "farm_50":    {"name": "🌿 Садовод",          "desc": "50 сеансов фарма",    "reward": 50},
    "farm_200":   {"name": "⚒ Шахтер",           "desc": "200 сеансов фарма",   "reward": 250},
    "farm_1000":  {"name": "💎 Алмазная рука",    "desc": "1000 сеансов фарма",  "reward": 1500},
    "farm_3000":  {"name": "🌌 Повелитель льда",  "desc": "3000 сеансов фарма",  "reward": 5000},
    "wins_10":    {"name": "⚔️ Дуэлянт",         "desc": "10 побед в кубах",    "reward": 50},
    "wins_50":    {"name": "🛡 Гладиатор",        "desc": "50 побед в кубах",    "reward": 500},
    "wins_100":   {"name": "🌋 Непобедимый",      "desc": "100 побед в кубах",   "reward": 2000},
    "ref_1":      {"name": "🤝 Друг",             "desc": "Пригласил 1 игрока",  "reward": 25},
    "ref_10":     {"name": "📢 Лидер",            "desc": "Пригласил 10 игроков","reward": 300},
    "ref_50":     {"name": "👑 Магнат трафика",   "desc": "Пригласил 50 игроков","reward": 2000},
    "rich_1000":  {"name": "💵 Зажиточный",       "desc": "Собрал 1000 ICE",     "reward": 100},
    "rich_10000": {"name": "💰 Миллионер",        "desc": "Собрал 10 000 ICE",   "reward": 1500},
    "rich_100000":{"name": "🏛 Форбс",            "desc": "Собрал 100 000 ICE",  "reward": 10000},
    "lvl_10":     {"name": "📈 Растущий",         "desc": "Достиг 10 уровня",    "reward": 100},
    "lvl_50":     {"name": "🔥 Мастер",           "desc": "Достиг 50 уровня",    "reward": 1500},
    "lvl_100":    {"name": "⚡️ Бог фарма",       "desc": "Достиг 100 уровня",   "reward": 10000},
}

# ==================== ИНИЦИАЛИЗАЦИЯ ====================
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
scheduler = AsyncIOScheduler()

mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client.rucoy_guild
guild_col       = db.guild
users_col       = db.users
applications_col= db.applications
logs_col        = db.logs
settings_col    = db.settings   # <-- хранит ADMIN_CHAT_ID, GUILD_CHAT_ID и тему

# ==================== FSM СОСТОЯНИЯ ====================
class ApplicationForm(StatesGroup):
    screenshot  = State()
    game_nick   = State()
    timezone    = State()
    friends     = State()
    prev_guild  = State()
    goals       = State()
    why_guild   = State()
    ready_lead  = State()
    play_time   = State()
    confirm     = State()

class BotSetState(StatesGroup):
    waiting_chat    = State()   # ждём пересланное сообщение из чата
    waiting_type    = State()   # какой чат настраиваем: admin / guild
    waiting_topic   = State()   # ждём выбор темы (если форум)

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================

async def get_settings() -> Dict:
    """Получить текущие настройки бота из БД"""
    doc = await settings_col.find_one({"_id": "main"})
    return doc or {}

async def save_settings(data: Dict):
    """Сохранить настройки"""
    await settings_col.update_one({"_id": "main"}, {"$set": data}, upsert=True)

async def get_admin_chat() -> tuple[int, Optional[int]]:
    """Вернуть (chat_id, topic_id) для чата заявок"""
    s = await get_settings()
    return s.get("admin_chat_id", 0), s.get("admin_topic_id")

async def get_guild_chat() -> tuple[int, Optional[int]]:
    """Вернуть (chat_id, topic_id) для чата гильдии"""
    s = await get_settings()
    return s.get("guild_chat_id", 0), s.get("guild_topic_id")

async def get_user_role(user_id: int) -> str:
    user = await users_col.find_one({"tg_id": user_id})
    return user.get("role", "member") if user else "member"

async def is_admin(user_id: int) -> bool:
    role = await get_user_role(user_id)
    return role in ["owner", "admin"]

async def log_action(action: str, by_admin: int,
                     target_user: Optional[int] = None,
                     details: Optional[Dict] = None):
    await logs_col.insert_one({
        "action": action,
        "by_admin": by_admin,
        "target_user": target_user,
        "details": details or {},
        "date": datetime.now()
    })

# ==================== КЛАВИАТУРЫ ====================

def get_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔰 Вступить в гильдию",    callback_data="apply")],
        [InlineKeyboardButton(text="🏰 Информация о гильдии",  callback_data="guild_info")],
        [InlineKeyboardButton(text="👥 Список участников",     callback_data="guild_members")],
        [InlineKeyboardButton(text="📊 Статистика",            callback_data="stats")],
    ])

def get_admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Заявки",              callback_data="admin_applications")],
        [InlineKeyboardButton(text="👑 Лидеры",              callback_data="admin_leaders")],
        [InlineKeyboardButton(text="⚙️ Настройки гильдии",  callback_data="admin_settings")],
        [InlineKeyboardButton(text="🔧 Настройка бота",      callback_data="botset_menu")],
        [InlineKeyboardButton(text="🔙 Главное меню",        callback_data="main_menu")],
    ])

def get_botset_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📬 Настроить чат заявок",    callback_data="botset_admin_chat")],
        [InlineKeyboardButton(text="📢 Настроить чат гильдии",   callback_data="botset_guild_chat")],
        [InlineKeyboardButton(text="ℹ️ Текущие настройки",       callback_data="botset_info")],
        [InlineKeyboardButton(text="🔙 Назад",                   callback_data="admin_panel")],
    ])

# ==================== ПАРСИНГ ГИЛЬДИИ ====================

async def parse_guild_page(url: str) -> Optional[Dict]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    logger.error(f"RucoyStats HTTP {resp.status}")
                    return None
                html = await resp.text()

        soup = BeautifulSoup(html, 'lxml')

        guild_header = soup.find('h1') or soup.find('h2')
        guild_name = guild_header.text.strip() if guild_header else "Unknown Guild"

        members = []
        table = soup.find('table')
        if table:
            for row in table.find_all('tr')[1:]:
                cols = row.find_all('td')
                if len(cols) >= 3:
                    try:
                        name       = cols[1].text.strip()
                        level      = int(cols[2].text.strip())
                        last_online= cols[3].text.strip() if len(cols) > 3 else ""
                        members.append({
                            "nick": name,
                            "level": level,
                            "last_seen_str": last_online,
                            "last_seen": datetime.now()
                        })
                    except Exception:
                        continue

        avg_lvl = sum(m['level'] for m in members) // len(members) if members else 0
        leader  = members[0]['nick'] if members else "Unknown"

        return {
            "name": guild_name,
            "url": url,
            "leader": leader,
            "members": members,
            "member_count": len(members),
            "avg_lvl": avg_lvl,
            "last_update": datetime.now()
        }
    except Exception as e:
        logger.error(f"Ошибка парсинга: {e}")
        return None

async def update_guild_data():
    try:
        guild_data = await guild_col.find_one()
        if not guild_data or "url" not in guild_data:
            return
        new_data = await parse_guild_page(guild_data["url"])
        if new_data:
            await guild_col.update_one({}, {"$set": new_data})
            logger.info(f"Гильдия {new_data['name']} обновлена")
    except Exception as e:
        logger.error(f"update_guild_data: {e}")

async def check_inactive_members():
    """Уведомить в чат гильдии о неактивных участниках"""
    try:
        guild_data = await guild_col.find_one()
        if not guild_data:
            return
        members = guild_data.get("members", [])
        threshold = datetime.now() - timedelta(days=7)
        inactive  = [m for m in members if m.get("last_seen", datetime.now()) < threshold]
        if not inactive:
            return
        guild_chat_id, guild_topic_id = await get_guild_chat()
        if not guild_chat_id:
            return
        text = f"⚠️ <b>Неактивные участники (7+ дней):</b>\n\n"
        for m in inactive[:20]:
            text += f"🟡 {m['nick']} — ур. {m['level']}\n"
        kwargs = {"chat_id": guild_chat_id, "text": text}
        if guild_topic_id:
            kwargs["message_thread_id"] = guild_topic_id
        await bot.send_message(**kwargs)
    except Exception as e:
        logger.error(f"check_inactive_members: {e}")

# ==================== ОТПРАВКА В ЧАТЫ ====================

async def send_to_admin_chat(text: str, **kwargs):
    """Отправить сообщение в чат заявок (с учётом темы)"""
    chat_id, topic_id = await get_admin_chat()
    if not chat_id:
        logger.warning("admin_chat_id не настроен")
        return None
    if topic_id:
        kwargs["message_thread_id"] = topic_id
    return await bot.send_message(chat_id=chat_id, text=text, **kwargs)

async def send_photo_to_admin_chat(photo, caption: str, **kwargs):
    """Отправить фото в чат заявок"""
    chat_id, topic_id = await get_admin_chat()
    if not chat_id:
        return None
    if topic_id:
        kwargs["message_thread_id"] = topic_id
    return await bot.send_photo(chat_id=chat_id, photo=photo, caption=caption, **kwargs)

async def send_to_guild_chat(text: str, **kwargs):
    """Отправить сообщение в чат гильдии"""
    chat_id, topic_id = await get_guild_chat()
    if not chat_id:
        return None
    if topic_id:
        kwargs["message_thread_id"] = topic_id
    return await bot.send_message(chat_id=chat_id, text=text, **kwargs)

# ==================== КОМАНДЫ ====================

@router.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    user    = await users_col.find_one({"tg_id": user_id})

    if user and user.get("role") == "banned":
        await message.answer("⛔ Вы заблокированы и не можете использовать бота.")
        return

    if not user:
        await users_col.insert_one({
            "tg_id":     user_id,
            "username":  message.from_user.username or "unknown",
            "role":      "owner" if user_id == OWNER_ID else "member",
            "joined_at": datetime.now()
        })

    text = (
        f"👋 Привет, <b>{message.from_user.first_name}</b>!\n\n"
        "Добро пожаловать в бот управления гильдией Rucoy Online!\n\n"
        "Используй меню ниже для навигации:"
    )
    await message.answer(text, reply_markup=get_main_keyboard())

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not await is_admin(message.from_user.id):
        await message.answer("❌ У вас нет прав для доступа к админ-панели")
        return
    await message.answer("⚙️ <b>АДМИН-ПАНЕЛЬ</b>\n\nУправление гильдией",
                         reply_markup=get_admin_keyboard())

# ==================== /botset КОМАНДА ====================

@router.message(Command("botset"))
async def cmd_botset(message: Message, state: FSMContext):
    """Настройка чатов бота — только для владельца"""
    if message.from_user.id != OWNER_ID:
        await message.answer("❌ Только владелец может настраивать бота")
        return

    await message.answer(
        "🔧 <b>Настройка бота</b>\n\n"
        "Выберите что настроить:",
        reply_markup=get_botset_keyboard()
    )

@router.callback_query(F.data == "botset_menu")
async def botset_menu(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("❌ Только владелец", show_alert=True)
        return
    await callback.message.edit_text(
        "🔧 <b>Настройка бота</b>\n\nВыберите что настроить:",
        reply_markup=get_botset_keyboard()
    )
    await callback.answer()

@router.callback_query(F.data == "botset_info")
async def botset_info(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("❌ Только владелец", show_alert=True)
        return

    s = await get_settings()
    admin_chat_id  = s.get("admin_chat_id", "не настроен")
    admin_topic_id = s.get("admin_topic_id", "нет")
    guild_chat_id  = s.get("guild_chat_id", "не настроен")
    guild_topic_id = s.get("guild_topic_id", "нет")

    text = (
        "ℹ️ <b>Текущие настройки бота</b>\n\n"
        f"📬 Чат заявок:\n"
        f"  ID: <code>{admin_chat_id}</code>\n"
        f"  Тема: <code>{admin_topic_id}</code>\n\n"
        f"📢 Чат гильдии:\n"
        f"  ID: <code>{guild_chat_id}</code>\n"
        f"  Тема: <code>{guild_topic_id}</code>"
    )

    await callback.message.edit_text(text, reply_markup=get_botset_keyboard())
    await callback.answer()

@router.callback_query(F.data.in_({"botset_admin_chat", "botset_guild_chat"}))
async def botset_start_setup(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("❌ Только владелец", show_alert=True)
        return

    chat_type = "admin" if callback.data == "botset_admin_chat" else "guild"
    chat_label = "заявок" if chat_type == "admin" else "гильдии"

    await state.update_data(botset_type=chat_type)
    await state.set_state(BotSetState.waiting_chat)

    await callback.message.edit_text(
        f"📨 <b>Настройка чата {chat_label}</b>\n\n"
        "Перешлите любое сообщение из нужного чата — "
        "бот автоматически определит его ID.\n\n"
        "Или введите ID чата вручную (например: <code>-1001234567890</code>)\n\n"
        "Отправьте /cancel для отмены"
    )
    await callback.answer()

@router.message(BotSetState.waiting_chat)
async def botset_receive_chat(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return

    if message.text and message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Настройка отменена", reply_markup=get_botset_keyboard())
        return

    chat_id = None

    # Если переслано из чата
    if message.forward_from_chat:
        chat_id = message.forward_from_chat.id
        chat_title = message.forward_from_chat.title
    # Если введён ID вручную
    elif message.text:
        try:
            chat_id = int(message.text.strip())
            chat_title = f"ID {chat_id}"
        except ValueError:
            await message.answer("❌ Неверный формат. Введите числовой ID чата или перешлите сообщение из нужного чата")
            return

    if not chat_id:
        await message.answer("❌ Не удалось определить ID чата. Перешлите сообщение из нужного чата")
        return

    await state.update_data(botset_chat_id=chat_id, botset_chat_title=chat_title)

    # Проверяем — есть ли в чате темы (forum supergroup)
    try:
        chat_info = await bot.get_chat(chat_id)
        is_forum  = getattr(chat_info, 'is_forum', False)
    except Exception:
        is_forum = False

    if is_forum:
        # Предлагаем выбрать тему
        await state.set_state(BotSetState.waiting_topic)
        await message.answer(
            f"✅ Чат найден: <b>{chat_title}</b>\n\n"
            "Этот чат является форумом с темами.\n"
            "Введите <b>ID темы</b> (message_thread_id) куда отправлять сообщения, "
            "или напишите <b>0</b> — чтобы писать в общий чат без темы.\n\n"
            "💡 Как узнать ID темы: перешлите сообщение из нужной темы боту "
            "@getidsbot или откройте тему и скопируйте номер из ссылки."
        )
    else:
        # Тем нет, сохраняем сразу
        await _save_botset_chat(message, state, topic_id=None)

@router.message(BotSetState.waiting_topic)
async def botset_receive_topic(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return

    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Настройка отменена", reply_markup=get_botset_keyboard())
        return

    try:
        topic_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введите числовой ID темы или 0 для общего чата")
        return

    topic_id = topic_id if topic_id != 0 else None
    await _save_botset_chat(message, state, topic_id=topic_id)

async def _save_botset_chat(message: Message, state: FSMContext, topic_id: Optional[int]):
    """Сохранить настройки чата"""
    data       = await state.get_data()
    chat_id    = data["botset_chat_id"]
    chat_title = data.get("botset_chat_title", str(chat_id))
    chat_type  = data["botset_type"]

    if chat_type == "admin":
        await save_settings({
            "admin_chat_id":   chat_id,
            "admin_topic_id":  topic_id,
            "admin_chat_title": chat_title,
        })
        label = "заявок"
    else:
        await save_settings({
            "guild_chat_id":   chat_id,
            "guild_topic_id":  topic_id,
            "guild_chat_title": chat_title,
        })
        label = "гильдии"

    await state.clear()

    topic_info = f"Тема ID: <code>{topic_id}</code>" if topic_id else "Без темы (общий чат)"
    await message.answer(
        f"✅ <b>Чат {label} настроен!</b>\n\n"
        f"Чат: <b>{chat_title}</b>\n"
        f"ID: <code>{chat_id}</code>\n"
        f"{topic_info}\n\n"
        "Теперь все уведомления будут приходить туда.",
        reply_markup=get_botset_keyboard()
    )

    # Тест-сообщение
    try:
        test_kwargs = {"text": f"✅ Тест: бот успешно подключён к чату {label}!"}
        if topic_id:
            test_kwargs["message_thread_id"] = topic_id
        await bot.send_message(chat_id=chat_id, **test_kwargs)
    except Exception as e:
        await message.answer(f"⚠️ Не удалось отправить тест-сообщение: {e}\n"
                             "Убедитесь что бот добавлен в чат и имеет права на отправку сообщений")

# ==================== /setguild ====================

@router.message(Command("setguild"))
async def cmd_setguild(message: Message):
    if message.from_user.id != OWNER_ID:
        await message.answer("❌ Только владелец может устанавливать гильдию")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "Использование: /setguild &lt;URL&gt;\n"
            "Пример: /setguild https://rucoyonline.com/guild/YourGuild"
        )
        return

    url  = args[1].strip()
    data = await parse_guild_page(url)   # переменная data, не guild_data!

    if not data:
        await message.answer("❌ Не удалось получить данные. Проверьте ссылку.")
        return

    await guild_col.update_one({}, {"$set": data}, upsert=True)

    await message.answer(
        f"✅ <b>Гильдия успешно подключена!</b>\n\n"
        f"🏰 Название: <b>{data['name']}</b>\n"
        f"👑 Лидер: <code>{data.get('leader', 'Не найден')}</code>\n"
        f"👥 Участников: <b>{data['member_count']}</b>\n"
        f"📈 Средний уровень: <b>{data['avg_lvl']}</b>\n"
        f"🔗 <a href='{url}'>Открыть на RucoyStats</a>",
        disable_web_page_preview=True
    )

# ==================== УПРАВЛЕНИЕ ПРАВАМИ ====================

@router.message(Command("makeadmin"))
async def cmd_makeadmin(message: Message):
    if message.from_user.id != OWNER_ID:
        await message.answer("❌ Только владелец может назначать админов")
        return
    if not message.reply_to_message:
        await message.answer("Ответьте на сообщение пользователя")
        return

    target_id = message.reply_to_message.from_user.id
    await users_col.update_one(
        {"tg_id": target_id},
        {"$set": {
            "role":     "admin",
            "username": message.reply_to_message.from_user.username or "unknown"
        }},
        upsert=True
    )
    await log_action("admin_promoted", message.from_user.id, target_user=target_id)
    await message.answer("✅ Пользователь назначен администратором")

@router.message(Command("ban"))
async def cmd_ban(message: Message):
    if not await is_admin(message.from_user.id):
        await message.answer("❌ У вас нет прав")
        return
    if not message.reply_to_message:
        await message.answer("Ответьте на сообщение пользователя")
        return

    target_id = message.reply_to_message.from_user.id
    await users_col.update_one({"tg_id": target_id}, {"$set": {"role": "banned"}}, upsert=True)
    await log_action("user_banned", message.from_user.id, target_user=target_id)
    await message.answer("✅ Пользователь заблокирован")

@router.message(Command("unban"))
async def cmd_unban(message: Message):
    if not await is_admin(message.from_user.id):
        await message.answer("❌ У вас нет прав")
        return
    if not message.reply_to_message:
        await message.answer("Ответьте на сообщение пользователя")
        return

    target_id = message.reply_to_message.from_user.id
    await users_col.update_one({"tg_id": target_id}, {"$set": {"role": "member"}})
    await log_action("user_unbanned", message.from_user.id, target_user=target_id)
    await message.answer("✅ Пользователь разблокирован")

@router.message(Command("addleader"))
async def add_leader(message: Message):
    if not await is_admin(message.from_user.id):
        await message.answer("❌ У вас нет прав")
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Использование: /addleader <ник игрока>")
        return
    nick   = args[1].strip()
    result = await guild_col.update_one(
        {"members.nick": nick},
        {"$set": {"members.$.is_leader": True}}
    )
    if result.modified_count > 0:
        await log_action("leader_added", message.from_user.id, details={"nick": nick})
        await message.answer(f"✅ Игрок <b>{nick}</b> назначен лидером")
    else:
        await message.answer(f"❌ Игрок <b>{nick}</b> не найден в гильдии")

@router.message(Command("removeleader"))
async def remove_leader(message: Message):
    if not await is_admin(message.from_user.id):
        await message.answer("❌ У вас нет прав")
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Использование: /removeleader <ник игрока>")
        return
    nick   = args[1].strip()
    result = await guild_col.update_one(
        {"members.nick": nick},
        {"$set": {"members.$.is_leader": False}}
    )
    if result.modified_count > 0:
        await log_action("leader_removed", message.from_user.id, details={"nick": nick})
        await message.answer(f"✅ С игрока <b>{nick}</b> снята роль лидера")
    else:
        await message.answer(f"❌ Игрок <b>{nick}</b> не найден в гильдии")

# ==================== CALLBACK ОБРАБОТЧИКИ ====================

@router.callback_query(F.data == "main_menu")
async def show_main_menu(callback: CallbackQuery):
    await callback.message.edit_text(
        "🏰 <b>Главное меню</b>\n\nВыберите действие:",
        reply_markup=get_main_keyboard()
    )
    await callback.answer()

@router.callback_query(F.data == "admin_panel")
async def show_admin_panel(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return
    await callback.message.edit_text(
        "⚙️ <b>АДМИН-ПАНЕЛЬ</b>\n\nУправление гильдией",
        reply_markup=get_admin_keyboard()
    )
    await callback.answer()

@router.callback_query(F.data == "apply")
async def start_application(callback: CallbackQuery, state: FSMContext):
    user = await users_col.find_one({"tg_id": callback.from_user.id})
    if user and user.get("role") == "banned":
        await callback.answer("⛔ Вы заблокированы", show_alert=True)
        return

    existing = await applications_col.find_one({
        "user_id": callback.from_user.id,
        "status":  "pending"
    })
    if existing:
        await callback.answer("❌ У вас уже есть активная заявка", show_alert=True)
        return

    await callback.message.edit_text(
        "📝 <b>Заявка на вступление в гильдию</b>\n\n"
        "Отправьте скриншот вашего персонажа из игры:"
    )
    await state.set_state(ApplicationForm.screenshot)
    await callback.answer()

@router.message(ApplicationForm.screenshot, F.photo)
async def process_screenshot(message: Message, state: FSMContext):
    await state.update_data(screenshot=message.photo[-1].file_id)
    await message.answer("✅ Скриншот получен!\n\nВведите ваш игровой ник:")
    await state.set_state(ApplicationForm.game_nick)

@router.message(ApplicationForm.screenshot)
async def process_screenshot_wrong(message: Message):
    await message.answer("❌ Пожалуйста, отправьте скриншот (фото)")

@router.message(ApplicationForm.game_nick, F.text)
async def process_game_nick(message: Message, state: FSMContext):
    await state.update_data(game_nick=message.text)
    await message.answer("Укажите ваш часовой пояс (например, UTC+3):")
    await state.set_state(ApplicationForm.timezone)

@router.message(ApplicationForm.timezone, F.text)
async def process_timezone(message: Message, state: FSMContext):
    await state.update_data(timezone=message.text)
    await message.answer("Есть ли у вас друзья в нашей гильдии? (укажите ники или «нет»):")
    await state.set_state(ApplicationForm.friends)

@router.message(ApplicationForm.friends, F.text)
async def process_friends(message: Message, state: FSMContext):
    await state.update_data(friends=message.text)
    await message.answer("В какой гильдии вы состояли ранее? (или «нигде»):")
    await state.set_state(ApplicationForm.prev_guild)

@router.message(ApplicationForm.prev_guild, F.text)
async def process_prev_guild(message: Message, state: FSMContext):
    await state.update_data(prev_guild=message.text)
    await message.answer("Каковы ваши цели в игре?")
    await state.set_state(ApplicationForm.goals)

@router.message(ApplicationForm.goals, F.text)
async def process_goals(message: Message, state: FSMContext):
    await state.update_data(goals=message.text)
    await message.answer("Почему вы хотите вступить в нашу гильдию?")
    await state.set_state(ApplicationForm.why_guild)

@router.message(ApplicationForm.why_guild, F.text)
async def process_why_guild(message: Message, state: FSMContext):
    await state.update_data(why_guild=message.text)
    await message.answer("Готовы ли вы участвовать в рейдах и помогать новичкам? (да/нет)")
    await state.set_state(ApplicationForm.ready_lead)

@router.message(ApplicationForm.ready_lead, F.text)
async def process_ready_lead(message: Message, state: FSMContext):
    await state.update_data(ready_lead=message.text)
    await message.answer("Сколько часов в день вы играете?")
    await state.set_state(ApplicationForm.play_time)

@router.message(ApplicationForm.play_time, F.text)
async def process_play_time(message: Message, state: FSMContext):
    await state.update_data(play_time=message.text)
    data = await state.get_data()

    text = (
        "📝 <b>Проверьте вашу заявку:</b>\n\n"
        f"🎮 Игровой ник: <b>{data['game_nick']}</b>\n"
        f"🕐 Часовой пояс: {data['timezone']}\n"
        f"👥 Друзья в гильдии: {data['friends']}\n"
        f"🏰 Предыдущая гильдия: {data['prev_guild']}\n"
        f"🎯 Цели: {data['goals']}\n"
        f"💭 Почему мы: {data['why_guild']}\n"
        f"⚔️ Рейды: {data['ready_lead']}\n"
        f"⏰ Время игры: {data['play_time']}\n\n"
        "Всё верно? Отправить заявку?"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Отправить", callback_data="submit_application"),
        InlineKeyboardButton(text="❌ Отмена",    callback_data="cancel_application"),
    ]])
    await message.answer(text, reply_markup=keyboard)
    await state.set_state(ApplicationForm.confirm)

@router.callback_query(F.data == "submit_application")
async def submit_application(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()

    application = {
        "user_id":      callback.from_user.id,
        "username":     callback.from_user.username or "unknown",
        "first_name":   callback.from_user.first_name or "",
        "data":         data,
        "status":       "pending",
        "submitted_at": datetime.now()
    }
    result = await applications_col.insert_one(application)

    # Отправка в чат заявок
    admin_text = (
        "📋 <b>НОВАЯ ЗАЯВКА</b>\n\n"
        f"👤 От: @{callback.from_user.username or 'unknown'} "
        f"({callback.from_user.first_name})\n"
        f"🆔 ID: <code>{callback.from_user.id}</code>\n\n"
        f"🎮 Ник: <b>{data['game_nick']}</b>\n"
        f"🕐 Часовой пояс: {data['timezone']}\n"
        f"👥 Друзья: {data['friends']}\n"
        f"🏰 Прошлая гильдия: {data['prev_guild']}\n"
        f"🎯 Цели: {data['goals']}\n"
        f"💭 Почему мы: {data['why_guild']}\n"
        f"⚔️ Рейды: {data['ready_lead']}\n"
        f"⏰ Время игры: {data['play_time']}"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Принять",   callback_data=f"approve_{result.inserted_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{result.inserted_id}"),
    ]])

    await send_photo_to_admin_chat(
        photo=data['screenshot'],
        caption=admin_text,
        reply_markup=keyboard
    )

    await callback.message.edit_text(
        "✅ <b>Заявка отправлена!</b>\n\nОжидайте решения администрации."
    )
    await state.clear()
    await callback.answer()

@router.callback_query(F.data == "cancel_application")
async def cancel_application(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Заявка отменена")
    await callback.answer()

@router.callback_query(F.data.startswith("approve_"))
async def approve_application(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    from bson import ObjectId
    app_id      = callback.data.split("_", 1)[1]
    application = await applications_col.find_one({"_id": ObjectId(app_id)})

    if not application:
        await callback.answer("❌ Заявка не найдена", show_alert=True)
        return

    await applications_col.update_one(
        {"_id": ObjectId(app_id)},
        {"$set": {"status": "approved", "reviewed_by": callback.from_user.id}}
    )

    try:
        await bot.send_message(
            application["user_id"],
            "🎉 <b>Поздравляем!</b>\n\nВаша заявка одобрена! Добро пожаловать в гильдию!"
        )
    except Exception:
        pass

    # Уведомление в чат гильдии
    nick = application["data"].get("game_nick", "Неизвестный")
    await send_to_guild_chat(f"🎉 В гильдию принят новый участник: <b>{nick}</b>!")

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.reply("✅ Заявка одобрена")
    await log_action("application_approved", callback.from_user.id,
                     target_user=application["user_id"])
    await callback.answer()

@router.callback_query(F.data.startswith("reject_"))
async def reject_application(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    from bson import ObjectId
    app_id      = callback.data.split("_", 1)[1]
    application = await applications_col.find_one({"_id": ObjectId(app_id)})

    if not application:
        await callback.answer("❌ Заявка не найдена", show_alert=True)
        return

    await applications_col.update_one(
        {"_id": ObjectId(app_id)},
        {"$set": {"status": "rejected", "reviewed_by": callback.from_user.id}}
    )

    try:
        await bot.send_message(
            application["user_id"],
            "😔 К сожалению, ваша заявка отклонена.\nВы можете попробовать снова позже."
        )
    except Exception:
        pass

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.reply("❌ Заявка отклонена")
    await log_action("application_rejected", callback.from_user.id,
                     target_user=application["user_id"])
    await callback.answer()

@router.callback_query(F.data == "admin_applications")
async def show_applications(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    pending  = await applications_col.count_documents({"status": "pending"})
    approved = await applications_col.count_documents({"status": "approved"})
    rejected = await applications_col.count_documents({"status": "rejected"})

    text = (
        "📋 <b>Статистика заявок</b>\n\n"
        f"⏳ Ожидают: <b>{pending}</b>\n"
        f"✅ Одобрено: <b>{approved}</b>\n"
        f"❌ Отклонено: <b>{rejected}</b>\n\n"
        "Новые заявки приходят в чат заявок"
    )
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 Админ-панель", callback_data="admin_panel")
        ]])
    )
    await callback.answer()

@router.callback_query(F.data == "admin_settings")
async def show_settings(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    guild_data = await guild_col.find_one()

    text = "⚙️ <b>Настройки гильдии</b>\n\n"
    if guild_data:
        text += (
            f"🏰 Гильдия: <b>{guild_data['name']}</b>\n"
            f"🔗 URL: {guild_data.get('url','—')}\n"
            f"👥 Участников: {len(guild_data.get('members', []))}\n\n"
        )
    else:
        text += "Гильдия не настроена\n\n"

    text += (
        "💡 <b>Команды:</b>\n"
        "/setguild &lt;URL&gt; — установить гильдию\n"
        "/makeadmin — назначить админа (reply)\n"
        "/ban — забанить (reply)\n"
        "/unban — разбанить (reply)\n"
        "/addleader &lt;ник&gt; — назначить лидера\n"
        "/removeleader &lt;ник&gt; — снять лидера\n"
        "/botset — настройка чатов бота"
    )
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 Админ-панель", callback_data="admin_panel")
        ]])
    )
    await callback.answer()

@router.callback_query(F.data == "guild_info")
async def show_guild_info(callback: CallbackQuery):
    guild_data = await guild_col.find_one()
    if not guild_data:
        await callback.answer("❌ Гильдия не настроена", show_alert=True)
        return

    members   = guild_data.get("members", [])
    total_lvl = sum(m["level"] for m in members)
    avg_lvl   = total_lvl // len(members) if members else 0

    threshold = datetime.now() - timedelta(days=7)
    inactive  = sum(1 for m in members if m.get("last_seen", datetime.now()) < threshold)

    last_update = guild_data.get("last_update", datetime.now())
    last_update_str = last_update.strftime("%H:%M %d.%m") if isinstance(last_update, datetime) else "—"

    text = (
        f"🏰 <b>{guild_data['name']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👑 Лидер: <b>{guild_data.get('leader','—')}</b>\n"
        f"👥 Участников: <b>{len(members)}</b>\n"
        f"📊 Суммарный lvl: <b>{total_lvl}</b>\n"
        f"📈 Средний lvl: <b>{avg_lvl}</b>\n"
        f"🟡 Неактивных: <b>{inactive}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🕒 Обновлено: {last_update_str}"
    )
    await callback.message.edit_text(text, reply_markup=get_main_keyboard())
    await callback.answer()

@router.callback_query(F.data == "guild_members")
async def show_guild_members(callback: CallbackQuery):
    guild_data = await guild_col.find_one()
    if not guild_data:
        await callback.answer("❌ Гильдия не настроена", show_alert=True)
        return

    members   = sorted(guild_data.get("members", []), key=lambda x: x["level"], reverse=True)
    threshold = datetime.now() - timedelta(days=7)

    text = f"👥 <b>Участники {guild_data['name']}</b>\n\n"
    for m in members[:30]:
        star   = "⭐" if m.get("is_leader") else ""
        status = "🟢" if m.get("last_seen", datetime.now()) > threshold else "🟡"
        text  += f"{star}{status} <b>{m['nick']}</b> — ур. {m['level']}\n"

    if len(members) > 30:
        text += f"\n... и ещё {len(members) - 30} участников"

    await callback.message.edit_text(text, reply_markup=get_main_keyboard())
    await callback.answer()

@router.callback_query(F.data == "stats")
async def show_stats(callback: CallbackQuery):
    guild_data = await guild_col.find_one()
    if not guild_data:
        await callback.answer("❌ Гильдия не настроена", show_alert=True)
        return

    members   = guild_data.get("members", [])
    total_lvl = sum(m["level"] for m in members)
    avg_lvl   = total_lvl // len(members) if members else 0
    threshold = datetime.now() - timedelta(days=7)
    inactive  = [m for m in members if m.get("last_seen", datetime.now()) < threshold]
    top10     = sorted(members, key=lambda x: x["level"], reverse=True)[:10]

    text = (
        f"📊 <b>Статистика {guild_data['name']}</b>\n\n"
        f"👥 Участников: {len(members)}\n"
        f"📊 Суммарный уровень: {total_lvl}\n"
        f"📈 Средний уровень: {avg_lvl}\n"
        f"🟡 Неактивных: {len(inactive)}\n\n"
        f"🏆 <b>Топ-10:</b>\n"
    )
    for i, p in enumerate(top10, 1):
        star  = "⭐" if p.get("is_leader") else ""
        text += f"{i}. {star}<b>{p['nick']}</b> — {p['level']}\n"

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")
        ]])
    )
    await callback.answer()

@router.callback_query(F.data == "admin_leaders")
async def manage_leaders(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    guild_data = await guild_col.find_one()
    if not guild_data:
        await callback.answer("❌ Гильдия не настроена", show_alert=True)
        return

    leaders = [m for m in guild_data.get("members", []) if m.get("is_leader")]
    text = "👑 <b>Лидеры гильдии</b>\n\n"
    if leaders:
        for l in leaders:
            text += f"⭐ {l['nick']} — ур. {l['level']}\n"
    else:
        text += "Лидеров пока нет\n"

    text += "\n💡 <b>Команды:</b>\n/addleader &lt;ник&gt;\n/removeleader &lt;ник&gt;"

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")
        ]])
    )
    await callback.answer()

# ==================== ЗАПУСК ====================

async def health_check(request):
    return web.Response(text="OK")

async def on_startup(app):
    logger.info("Запуск бота...")

    if WEBHOOK_URL:
        webhook_path = f"/{BOT_TOKEN}"
        url = f"{WEBHOOK_URL}{webhook_path}"
        await bot.set_webhook(url)
        logger.info(f"Webhook: {url}")
    else:
        logger.warning("WEBHOOK_URL не задан!")

    if not scheduler.running:
        scheduler.add_job(update_guild_data, "interval", minutes=10)
        scheduler.add_job(check_inactive_members, "interval", hours=12)
        scheduler.start()
        logger.info("Планировщик запущен")

    async def _set_owner():
        try:
            await users_col.update_one(
                {"tg_id": OWNER_ID},
                {"$set": {"role": "owner"}},
                upsert=True
            )
        except Exception as e:
            logger.warning(f"MongoDB недоступен при старте: {e}")

    if OWNER_ID:
        asyncio.create_task(_set_owner())

async def on_shutdown(app):
    await bot.session.close()
    if scheduler.running:
        scheduler.shutdown()

def main():
    dp.include_router(router)

    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)

    webhook_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_handler.register(app, path=f"/{BOT_TOKEN}")
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
