"""
Telegram-бот для сбора идей по функционалу от пользователей.

Как это работает:
- Пользователь выбирает язык (RU/EN) при первом /start
- Пользователь присылает текст идеи
- Идея уходит в чат администратора с кнопками: Одобрить / Отклонить / Заблокировать
- При нажатии кнопки пользователь получает уведомление (кроме блокировки — блок тихий)
- Заблокированные пользователи не могут присылать новые идеи

Установка:
    pip install aiogram==3.13.1

Запуск:
    export BOT_TOKEN="токен_от_BotFather"
    export ADMIN_ID="твой_telegram_id"   # можно узнать у @userinfobot
    python idea_bot.py

Хранилище: SQLite файл ideas.db создаётся автоматически рядом со скриптом.

Защита от злоумышленников:
- Rate limiting (анти-флуд) на все сообщения и нажатия кнопок: не более N за окно времени
- Нарастающий мут при нарушениях (60с -> 120с -> 240с ...) и авто-блок за систематический флуд
- Лимиты на идеи: длина, кулдаун, максимум в час, максимум в ожидании, защита от дублей
- Экранирование HTML (иначе имя/текст вида <b> или <a href=...> ломает бота / подставляет разметку админу)
- Токен и ID администратора только из переменных окружения (в коде не хранятся)
"""

import asyncio
import html
import logging
import os
import sqlite3
import time
from collections import defaultdict, deque
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import ceil
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

# ---------- Конфигурация ----------

# ВАЖНО: токен и ID берём ТОЛЬКО из переменных окружения, в коде их не храним.
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", ""))  # твой Telegram ID
DB_PATH = os.environ.get("IDEA_BOT_DB", "ideas.db")

# ---------- Настройки защиты (можно менять) ----------

# Общий rate limit на ЛЮБЫЕ действия пользователя (сообщения + нажатия кнопок)
RATE_LIMIT_MESSAGES = 10      # не более 10 действий ...
RATE_LIMIT_WINDOW = 60        # ... за 60 секунд

# Наказание за превышение: мут растёт 60с -> 120с -> 240с ... (до MUTE_MAX)
MUTE_BASE = 60
MUTE_MAX = 3600
STRIKE_RESET = 3600           # через час без нарушений "страйки" обнуляются
AUTO_BLOCK_STRIKES = 4        # после 4 нарушений подряд — автоблокировка

# Лимиты на сами идеи
IDEA_MIN_LEN = 5
IDEA_MAX_LEN = 1000
IDEA_COOLDOWN = 30            # секунд между идеями
IDEA_MAX_PER_HOUR = 5
IDEA_MAX_PENDING = 10         # максимум неразобранных идей от одного человека

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("idea_bot")

# ---------- Тексты (RU / EN) ----------

TEXTS = {
    "ru": {
        "choose_lang": "Выберите язык / Choose language:",
        "welcome": (
            "Здравствуйте! 👋\n"
            "Пришлите идею — какой функционал добавить в софт.\n"
            "Мы передадим её разработчику, и вы получите уведомление о решении."
        ),
        "idea_sent": "✅ Идея отправлена! Ожидайте уведомления о решении.",
        "send_another_btn": "📩 Отправить ещё одну",
        "send_another_prompt": "Ожидаем следующую идею — пишите в любой момент.",
        "admin_greeting": "Приветствую, {name}! 👋\n\n",
        "admin_welcome": (
            "🛠 Админ-панель бота идей\n\n"
            "Сюда будут падать идеи от пользователей с кнопками\n"
            "Доступные команды:\n"
            "/approved — список одобренных идей\n"
            "/export_approved — выгрузить одобренные идеи файлом\n"
            "/stats — статистика по идеям\n"
            "/blocked — заблокированые пользователя\n"
            "/lang — сменить язык интерфейса"
        ),
        "idea_approved": "🎉 Ваша идея одобрена и пойдёт в разработку!\n\nИдея: {text}",
        "idea_rejected": "❌ Ваша идея отклонена.\n\nИдея: {text}",
        "blocked_notice": "🚫 Вы заблокированы и больше не можете отправлять идеи.",
        "already_blocked": "🚫 Вы заблокированы и не можете отправлять идеи.",
        "empty_message": "Пожалуйста, пришлите текстовое сообщение с идеей.",
        "lang_set": "Язык установлен: Русский 🇷🇺",
        "admin_new_idea": (
            "💡 Новая идея\n\n"
            "От: {name} (@{username}, id={user_id})\n"
            "Язык пользователя: {user_lang}\n\n"
            "{text}"
        ),
        "admin_decided": "{verdict}\nИдея от {name} (id={user_id}):\n{text}",
        "verdict_approved": "✅ ОДОБРЕНО",
        "verdict_rejected": "❌ ОТКЛОНЕНО",
        "verdict_blocked": "🚫 ПОЛЬЗОВАТЕЛЬ ЗАБЛОКИРОВАН",
        "btn_approve": "✅ Одобрить",
        "btn_reject": "❌ Отклонить",
        "btn_block": "🚫 Заблокировать",
        "not_admin": "Эта команда доступна только администратору.",
        "stats": "Всего идей: {total}\nОжидают решения: {pending}\nЗаблокировано пользователей: {blocked}",
        "no_approved": "Пока нет одобренных идей.",
        "approved_header": "📋 Одобренные идеи ({count}):\n",
        "approved_item": "\n#{idea_id} от {name} (id={user_id}), {date}:\n{text}\n",
        "export_sent": "Файл с одобренными идеями отправлен.",
        "no_blocked": "Заблокированных пользователей нет.",
        "blocked_header": "🚫 Заблокированные пользователи ({count}):",
        "unblock_btn": "🔓 Разблокировать {name}",
        "unblocked_admin": "🔓 Пользователь {name} (id={user_id}) разблокирован.",
        "unblocked_notice": "🔓 Вы были разблокированы и снова можете отправлять идеи.",
        "reply_sent_admin": "✉️ Ответ отправлен пользователю {name} (id={user_id}).",
        "reply_to_user": "✉️ Ответ от разработчика по вашей идее:\n\n«{idea_text}»\n\n{reply_text}",
        "reply_failed": "⚠️ Не удалось отправить ответ — пользователь мог заблокировать бота.",
        "flood_warning": "⚠️ Слишком много запросов. Подождите {sec} сек.",
        "auto_blocked_notice": "🚫 Вы заблокированы за спам.",
        "idea_too_short": "Опишите идею подробнее — минимум {min} символов.",
        "idea_too_long": "✂️ Слишком длинная идея: максимум {max} символов (у вас {n}).",
        "idea_cooldown": "⏳ Подождите {sec} сек. перед отправкой следующей идеи.",
        "idea_hourly_limit": "⏳ Лимит: не более {max} идей в час. Попробуйте позже.",
        "idea_pending_limit": "⏳ У вас уже {max} идей на рассмотрении. Дождитесь решения по ним.",
        "idea_duplicate": "Вы уже присылали такую идею недавно.",
    },
    "en": {
        "choose_lang": "Выберите язык / Choose language:",
        "welcome": (
            "Hello! 👋\n"
            "Please send us an idea for what feature to add to the software.\n"
            "We'll pass it to the developer, and you will be notified of the decision."
        ),
        "idea_sent": "✅ Idea sent! Please wait for a decision notification.",
        "send_another_btn": "📩 Send another",
        "send_another_prompt": "Ready for the next idea — please send it whenever.",
        "admin_greeting": "Welcome, {name}! 👋\n\n",
        "admin_welcome": (
            "🛠 Idea bot admin panel\n\n"
            "Ideas from users will land here with Approve / Reject / Block buttons.\n\n"
            "Available commands:\n"
            "/approved — list of approved ideas\n"
            "/export_approved — export approved ideas as a file\n"
            "/stats — idea statistics\n"
            "/lang — change interface language"
        ),
        "idea_approved": "🎉 Your idea has been approved and will go into development!\n\nIdea: {text}",
        "idea_rejected": "❌ Your idea has been rejected.\n\nIdea: {text}",
        "blocked_notice": "🚫 You have been blocked and can no longer send ideas.",
        "already_blocked": "🚫 You are blocked and cannot send ideas.",
        "empty_message": "Please send a text message with your idea.",
        "lang_set": "Language set: English 🇬🇧",
        "admin_new_idea": (
            "💡 New idea\n\n"
            "From: {name} (@{username}, id={user_id})\n"
            "User language: {user_lang}\n\n"
            "{text}"
        ),
        "admin_decided": "{verdict}\nIdea from {name} (id={user_id}):\n{text}",
        "verdict_approved": "✅ APPROVED",
        "verdict_rejected": "❌ REJECTED",
        "verdict_blocked": "🚫 USER BLOCKED",
        "btn_approve": "✅ Approve",
        "btn_reject": "❌ Reject",
        "btn_block": "🚫 Block",
        "not_admin": "This command is only available to the admin.",
        "stats": "Total ideas: {total}\nPending: {pending}\nBlocked users: {blocked}",
        "no_approved": "No approved ideas yet.",
        "approved_header": "📋 Approved ideas ({count}):\n",
        "approved_item": "\n#{idea_id} from {name} (id={user_id}), {date}:\n{text}\n",
        "export_sent": "File with approved ideas sent.",
        "no_blocked": "No blocked users.",
        "blocked_header": "🚫 Blocked users ({count}):",
        "unblock_btn": "🔓 Unblock {name}",
        "unblocked_admin": "🔓 User {name} (id={user_id}) has been unblocked.",
        "unblocked_notice": "🔓 You have been unblocked and can send ideas again.",
        "reply_sent_admin": "✉️ Reply sent to {name} (id={user_id}).",
        "reply_to_user": "✉️ Reply from the developer about your idea:\n\n\"{idea_text}\"\n\n{reply_text}",
        "reply_failed": "⚠️ Could not deliver the reply — the user may have blocked the bot.",
        "flood_warning": "⚠️ Too many requests. Please wait {sec} sec.",
        "auto_blocked_notice": "🚫 You have been blocked for spam.",
        "idea_too_short": "Please describe your idea in more detail — at least {min} characters.",
        "idea_too_long": "✂️ Idea is too long: max {max} characters (yours is {n}).",
        "idea_cooldown": "⏳ Please wait {sec} sec. before sending the next idea.",
        "idea_hourly_limit": "⏳ Limit: no more than {max} ideas per hour. Try again later.",
        "idea_pending_limit": "⏳ You already have {max} ideas awaiting review. Please wait for decisions.",
        "idea_duplicate": "You've already sent this idea recently.",
    },
}


def t(lang: str, key: str, **kwargs) -> str:
    lang = lang if lang in TEXTS else "en"
    return TEXTS[lang][key].format(**kwargs)


# ---------- Хранилище (SQLite) ----------

def db_init() -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                lang TEXT NOT NULL DEFAULT 'en',
                blocked INTEGER NOT NULL DEFAULT 0,
                username TEXT,
                full_name TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ideas (
                idea_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_messages (
                message_id INTEGER PRIMARY KEY,
                idea_id INTEGER NOT NULL
            )
            """
        )
        conn.commit()


def db_get_user(user_id: int) -> dict | None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row else None


def db_upsert_user(user_id: int, username: str | None, full_name: str, lang: str | None = None) -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        existing = conn.execute(
            "SELECT lang FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if existing:
            if lang is not None:
                conn.execute(
                    "UPDATE users SET lang=?, username=?, full_name=? WHERE user_id=?",
                    (lang, username, full_name, user_id),
                )
            else:
                conn.execute(
                    "UPDATE users SET username=?, full_name=? WHERE user_id=?",
                    (username, full_name, user_id),
                )
        else:
            conn.execute(
                "INSERT INTO users (user_id, lang, blocked, username, full_name) VALUES (?, ?, 0, ?, ?)",
                (user_id, lang or "en", username, full_name),
            )
        conn.commit()


def db_set_blocked(user_id: int, blocked: bool) -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "UPDATE users SET blocked=? WHERE user_id=?", (1 if blocked else 0, user_id)
        )
        conn.commit()


def db_add_idea(user_id: int, text: str) -> int:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            "INSERT INTO ideas (user_id, text, status, created_at) VALUES (?, ?, 'pending', ?)",
            (user_id, text, datetime.utcnow().isoformat()),
        )
        conn.commit()
        return cur.lastrowid


def db_set_idea_status(idea_id: int, status: str) -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("UPDATE ideas SET status=? WHERE idea_id=?", (status, idea_id))
        conn.commit()


def db_get_idea(idea_id: int) -> dict | None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM ideas WHERE idea_id = ?", (idea_id,)
        ).fetchone()
        return dict(row) if row else None


def db_get_approved_ideas() -> list[dict]:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT ideas.idea_id, ideas.text, ideas.created_at,
                   users.user_id, users.username, users.full_name
            FROM ideas
            JOIN users ON users.user_id = ideas.user_id
            WHERE ideas.status = 'approved'
            ORDER BY ideas.idea_id ASC
            """
        ).fetchall()
        return [dict(r) for r in rows]


def db_get_blocked_users() -> list[dict]:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT user_id, username, full_name FROM users WHERE blocked = 1"
        ).fetchall()
        return [dict(r) for r in rows]


def db_link_admin_message(message_id: int, idea_id: int) -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO admin_messages (message_id, idea_id) VALUES (?, ?)",
            (message_id, idea_id),
        )
        conn.commit()


def db_get_idea_by_message(message_id: int) -> dict | None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT ideas.* FROM admin_messages
            JOIN ideas ON ideas.idea_id = admin_messages.idea_id
            WHERE admin_messages.message_id = ?
            """,
            (message_id,),
        ).fetchone()
        return dict(row) if row else None


def db_stats() -> dict:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        total = conn.execute("SELECT COUNT(*) FROM ideas").fetchone()[0]
        pending = conn.execute(
            "SELECT COUNT(*) FROM ideas WHERE status='pending'"
        ).fetchone()[0]
        blocked = conn.execute(
            "SELECT COUNT(*) FROM users WHERE blocked=1"
        ).fetchone()[0]
        return {"total": total, "pending": pending, "blocked": blocked}


def db_get_last_idea_time(user_id: int) -> datetime | None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT created_at FROM ideas WHERE user_id=? ORDER BY idea_id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        return datetime.fromisoformat(row[0]) if row else None


def db_count_ideas_since(user_id: int, since_iso: str) -> int:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM ideas WHERE user_id=? AND created_at >= ?",
            (user_id, since_iso),
        ).fetchone()[0]


def db_count_pending(user_id: int) -> int:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM ideas WHERE user_id=? AND status='pending'",
            (user_id,),
        ).fetchone()[0]


def db_has_duplicate(user_id: int, text: str, since_iso: str) -> bool:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT 1 FROM ideas WHERE user_id=? AND LOWER(text)=LOWER(?) AND created_at >= ? LIMIT 1",
            (user_id, text, since_iso),
        ).fetchone()
        return row is not None


def esc(value: object) -> str:
    """Экранирует пользовательский текст, т.к. бот работает в parse_mode=HTML."""
    return html.escape(str(value), quote=False)


# ---------- Клавиатуры ----------

def lang_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang:ru"),
                InlineKeyboardButton(text="🇬🇧 English", callback_data="lang:en"),
            ]
        ]
    )


def send_another_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t(lang, "send_another_btn"), callback_data="send_another")]
        ]
    )


def welcome_text(user_id: int, lang: str, name: str = "") -> str:
    if user_id == ADMIN_ID:
        greeting = t(lang, "admin_greeting", name=name or "boss")
        return greeting + t(lang, "admin_welcome")
    return t(lang, "welcome")


def admin_decision_keyboard(idea_id: int, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t(lang, "btn_approve"), callback_data=f"approve:{idea_id}"
                ),
                InlineKeyboardButton(
                    text=t(lang, "btn_reject"), callback_data=f"reject:{idea_id}"
                ),
                InlineKeyboardButton(
                    text=t(lang, "btn_block"), callback_data=f"block:{idea_id}"
                ),
            ]
        ]
    )


# ---------- Anti-flood / Rate limiting ----------

class AntiFloodMiddleware(BaseMiddleware):
    """
    Скользящее окно: не более RATE_LIMIT_MESSAGES действий за RATE_LIMIT_WINDOW секунд.
    При превышении — мут с нарастающим временем, а при систематическом флуде — автоблок.
    Админ не ограничивается. Во время мута апдейты молча отбрасываются (бот не тратит
    ресурсы и не даёт спамеру повода продолжать).
    """

    def __init__(self) -> None:
        self._hits: dict[int, deque[float]] = defaultdict(deque)
        self._muted_until: dict[int, float] = {}
        self._strikes: dict[int, tuple[int, float]] = {}
        self._calls = 0

    def _cleanup(self, now: float) -> None:
        """Чистим память от давно неактивных пользователей."""
        for uid in list(self._hits):
            hits = self._hits[uid]
            if not hits or now - hits[-1] > RATE_LIMIT_WINDOW:
                del self._hits[uid]
        for uid in [u for u, until in self._muted_until.items() if until < now]:
            del self._muted_until[uid]
        for uid in [u for u, (_, last) in self._strikes.items() if now - last > STRIKE_RESET]:
            del self._strikes[uid]

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None or user.id == ADMIN_ID:
            return await handler(event, data)

        uid = user.id
        now = time.monotonic()

        self._calls += 1
        if self._calls % 500 == 0:
            self._cleanup(now)

        # Пользователь в муте — молча игнорируем
        if now < self._muted_until.get(uid, 0.0):
            if isinstance(event, CallbackQuery):
                try:
                    await event.answer()
                except Exception:
                    pass
            return None

        hits = self._hits[uid]
        while hits and now - hits[0] > RATE_LIMIT_WINDOW:
            hits.popleft()

        if len(hits) >= RATE_LIMIT_MESSAGES:
            await self._punish(user, event, data, now)
            return None

        hits.append(now)
        return await handler(event, data)

    async def _punish(self, user, event: TelegramObject, data: dict[str, Any], now: float) -> None:
        uid = user.id
        count, last = self._strikes.get(uid, (0, now))
        if now - last > STRIKE_RESET:
            count = 0
        count += 1
        self._strikes[uid] = (count, now)

        mute = min(MUTE_BASE * 2 ** (count - 1), MUTE_MAX)
        self._muted_until[uid] = now + mute
        self._hits[uid].clear()

        db_user = db_get_user(uid)
        lang = db_user["lang"] if db_user else "en"
        bot: Bot = data["bot"]

        if count >= AUTO_BLOCK_STRIKES:
            db_upsert_user(uid, user.username, user.full_name)
            db_set_blocked(uid, True)
            logger.warning("Auto-blocked user %s for flooding", uid)
            text = t(lang, "auto_blocked_notice")
            try:
                await bot.send_message(
                    ADMIN_ID,
                    f"🚨 Автоблок за флуд: {esc(user.full_name)} "
                    f"(@{esc(user.username or '—')}, id={uid}). "
                    f"Разблокировать: /blocked",
                )
            except Exception:
                pass
        else:
            logger.info("Rate limit hit: user %s, strike %s, mute %ss", uid, count, mute)
            text = t(lang, "flood_warning", sec=mute)

        try:
            if isinstance(event, CallbackQuery):
                await event.answer(text, show_alert=True)
            elif isinstance(event, Message):
                await event.answer(text)
        except Exception:
            pass


# ---------- Роутер / Хендлеры ----------

router = Router()

# Один общий экземпляр на сообщения и кнопки — лимит считается суммарно
flood_guard = AntiFloodMiddleware()
router.message.middleware(flood_guard)
router.callback_query.middleware(flood_guard)


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    user = db_get_user(message.from_user.id)
    db_upsert_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name,
        lang=user["lang"] if user else None,
    )
    if user is None:
        await message.answer(t("ru", "choose_lang"), reply_markup=lang_keyboard())
    else:
        await message.answer(
            welcome_text(message.from_user.id, user["lang"], message.from_user.full_name)
        )


@router.callback_query(F.data.startswith("lang:"))
async def cb_set_lang(callback: CallbackQuery) -> None:
    lang = callback.data.split(":", 1)[1]
    if lang not in TEXTS:
        await callback.answer()
        return
    db_upsert_user(
        callback.from_user.id,
        callback.from_user.username,
        callback.from_user.full_name,
        lang=lang,
    )
    await callback.message.edit_text(t(lang, "lang_set"))
    await callback.message.answer(
        welcome_text(callback.from_user.id, lang, callback.from_user.full_name)
    )
    await callback.answer()


@router.message(Command("lang"))
async def cmd_lang(message: Message) -> None:
    await message.answer(t("ru", "choose_lang"), reply_markup=lang_keyboard())


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if message.from_user.id != ADMIN_ID:
        await message.answer(t("en", "not_admin"))
        return
    s = db_stats()
    await message.answer(
        t("ru", "stats", total=s["total"], pending=s["pending"], blocked=s["blocked"])
    )


@router.message(Command("approved"))
async def cmd_approved(message: Message) -> None:
    if message.from_user.id != ADMIN_ID:
        await message.answer(t("en", "not_admin"))
        return

    approved = db_get_approved_ideas()
    admin_lang = "ru"

    if not approved:
        await message.answer(t(admin_lang, "no_approved"))
        return

    chunks = [t(admin_lang, "approved_header", count=len(approved))]
    for idea in approved:
        chunks.append(
            t(
                admin_lang,
                "approved_item",
                idea_id=idea["idea_id"],
                name=esc(idea["full_name"] or idea["username"] or str(idea["user_id"])),
                user_id=idea["user_id"],
                date=idea["created_at"][:19].replace("T", " "),
                text=esc(idea["text"]),
            )
        )

    # Telegram режет сообщения на 4096 символов — бьём на части
    buffer = ""
    for chunk in chunks:
        if len(buffer) + len(chunk) > 3500:
            await message.answer(buffer)
            buffer = ""
        buffer += chunk
    if buffer:
        await message.answer(buffer)


@router.message(Command("export_approved"))
async def cmd_export_approved(message: Message, bot: Bot) -> None:
    if message.from_user.id != ADMIN_ID:
        await message.answer(t("en", "not_admin"))
        return

    approved = db_get_approved_ideas()
    admin_lang = "ru"

    if not approved:
        await message.answer(t(admin_lang, "no_approved"))
        return

    lines = []
    for idea in approved:
        lines.append(
            f"#{idea['idea_id']} | {idea['created_at'][:19].replace('T', ' ')} | "
            f"{idea['full_name'] or idea['username'] or idea['user_id']} (id={idea['user_id']})\n"
            f"{idea['text']}\n{'-' * 40}"
        )
    file_text = "\n".join(lines)

    from aiogram.types import BufferedInputFile

    file = BufferedInputFile(
        file_text.encode("utf-8"), filename="approved_ideas.txt"
    )
    await message.answer_document(file, caption=t(admin_lang, "export_sent"))


@router.message(Command("blocked"))
async def cmd_blocked(message: Message) -> None:
    if message.from_user.id != ADMIN_ID:
        await message.answer(t("en", "not_admin"))
        return

    blocked = db_get_blocked_users()
    admin_lang = "ru"

    if not blocked:
        await message.answer(t(admin_lang, "no_blocked"))
        return

    await message.answer(t(admin_lang, "blocked_header", count=len(blocked)))
    for user in blocked:
        name = esc(user["full_name"] or user["username"] or str(user["user_id"]))
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=t(admin_lang, "unblock_btn", name=html.unescape(name)),
                        callback_data=f"unblock:{user['user_id']}",
                    )
                ]
            ]
        )
        await message.answer(f"{name} (id={user['user_id']})", reply_markup=keyboard)


@router.callback_query(F.data.startswith("unblock:"))
async def cb_unblock(callback: CallbackQuery, bot: Bot) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer(t("en", "not_admin"), show_alert=True)
        return

    target_user_id = int(callback.data.split(":", 1)[1])
    db_set_blocked(target_user_id, False)

    target = db_get_user(target_user_id)
    admin_lang = "ru"
    name = esc((target["full_name"] or target["username"] or str(target_user_id)) if target else str(target_user_id))

    await callback.message.edit_text(
        t(admin_lang, "unblocked_admin", name=name, user_id=target_user_id)
    )

    user_lang = target["lang"] if target else "en"
    try:
        await bot.send_message(target_user_id, t(user_lang, "unblocked_notice"))
    except Exception:
        pass
    await callback.answer()


@router.message(F.reply_to_message & F.text & (F.from_user.id == ADMIN_ID))
async def handle_admin_reply(message: Message, bot: Bot) -> None:
    idea = db_get_idea_by_message(message.reply_to_message.message_id)
    admin_lang = "ru"

    if idea is None:
        return  # не ответ на идею — пропускаем, обычным хендлерам тоже можно его забрать

    target = db_get_user(idea["user_id"])
    user_lang = target["lang"] if target else "en"
    name = esc((target["full_name"] or target["username"] or str(idea["user_id"])) if target else str(idea["user_id"]))

    try:
        await bot.send_message(
            idea["user_id"],
            t(
                user_lang,
                "reply_to_user",
                idea_text=esc(idea["text"]),
                reply_text=esc(message.text),
            ),
        )
        await message.answer(
            t(admin_lang, "reply_sent_admin", name=name, user_id=idea["user_id"])
        )
    except Exception:
        await message.answer(t(admin_lang, "reply_failed"))


@router.message(F.text & ~F.text.startswith("/"))
async def handle_idea(message: Message, bot: Bot) -> None:
    user = db_get_user(message.from_user.id)
    if user is None:
        db_upsert_user(
            message.from_user.id, message.from_user.username, message.from_user.full_name
        )
        user = db_get_user(message.from_user.id)

    lang = user["lang"]

    if user["blocked"]:
        await message.answer(t(lang, "already_blocked"))
        return

    idea_text = message.text.strip()
    if not idea_text:
        await message.answer(t(lang, "empty_message"))
        return

    if len(idea_text) > IDEA_MAX_LEN:
        await message.answer(t(lang, "idea_too_long", max=IDEA_MAX_LEN, n=len(idea_text)))
        return
    if len(idea_text) < IDEA_MIN_LEN:
        await message.answer(t(lang, "idea_too_short", min=IDEA_MIN_LEN))
        return

    # Лимиты на количество идей (админ не ограничивается)
    if message.from_user.id != ADMIN_ID:
        uid = message.from_user.id
        now_utc = datetime.utcnow()

        last = db_get_last_idea_time(uid)
        if last is not None:
            elapsed = (now_utc - last).total_seconds()
            if elapsed < IDEA_COOLDOWN:
                await message.answer(
                    t(lang, "idea_cooldown", sec=ceil(IDEA_COOLDOWN - elapsed))
                )
                return

        hour_ago = (now_utc - timedelta(hours=1)).isoformat()
        if db_count_ideas_since(uid, hour_ago) >= IDEA_MAX_PER_HOUR:
            await message.answer(t(lang, "idea_hourly_limit", max=IDEA_MAX_PER_HOUR))
            return

        if db_count_pending(uid) >= IDEA_MAX_PENDING:
            await message.answer(t(lang, "idea_pending_limit", max=IDEA_MAX_PENDING))
            return

        day_ago = (now_utc - timedelta(days=1)).isoformat()
        if db_has_duplicate(uid, idea_text, day_ago):
            await message.answer(t(lang, "idea_duplicate"))
            return

    idea_id = db_add_idea(message.from_user.id, idea_text)

    await message.answer(t(lang, "idea_sent"), reply_markup=send_another_keyboard(lang))

    admin_text = t(
        lang,
        "admin_new_idea",
        name=esc(message.from_user.full_name),
        username=esc(message.from_user.username or "—"),
        user_id=message.from_user.id,
        user_lang=lang,
        text=esc(idea_text),
    )
    sent = await bot.send_message(
        ADMIN_ID,
        admin_text,
        reply_markup=admin_decision_keyboard(idea_id, lang),
    )
    db_link_admin_message(sent.message_id, idea_id)


@router.callback_query(F.data == "send_another")
async def cb_send_another(callback: CallbackQuery) -> None:
    user = db_get_user(callback.from_user.id)
    lang = user["lang"] if user else "en"
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(t(lang, "send_another_prompt"))
    await callback.answer()


@router.callback_query(F.data.startswith(("approve:", "reject:", "block:")))
async def cb_decision(callback: CallbackQuery, bot: Bot) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer(t("en", "not_admin"), show_alert=True)
        return

    action, idea_id_str = callback.data.split(":", 1)
    idea_id = int(idea_id_str)
    idea = db_get_idea(idea_id)

    if idea is None:
        await callback.answer("Idea not found", show_alert=True)
        return

    target_user = db_get_user(idea["user_id"])
    user_lang = target_user["lang"] if target_user else "en"

    if action == "approve":
        db_set_idea_status(idea_id, "approved")
        try:
            await bot.send_message(
                idea["user_id"],
                t(user_lang, "idea_approved", text=esc(idea["text"])),
            )
        except Exception:
            pass  # пользователь мог заблокировать бота — решение всё равно сохраняем
        verdict_key = "verdict_approved"

    elif action == "reject":
        db_set_idea_status(idea_id, "rejected")
        try:
            await bot.send_message(
                idea["user_id"],
                t(user_lang, "idea_rejected", text=esc(idea["text"])),
            )
        except Exception:
            pass
        verdict_key = "verdict_rejected"

    else:  # block
        db_set_idea_status(idea_id, "rejected")
        db_set_blocked(idea["user_id"], True)
        try:
            await bot.send_message(idea["user_id"], t(user_lang, "blocked_notice"))
        except Exception:
            pass  # пользователь мог уже заблокировать бота у себя
        verdict_key = "verdict_blocked"

    admin_lang = "ru"
    name = esc(target_user["full_name"] if target_user else idea["user_id"])
    await callback.message.edit_text(
        t(
            admin_lang,
            "admin_decided",
            verdict=t(admin_lang, verdict_key),
            name=name,
            user_id=idea["user_id"],
            text=esc(idea["text"]),
        )
    )
    await callback.answer()


# ---------- Точка входа ----------

async def main() -> None:
    if BOT_TOKEN == "PUT_YOUR_TOKEN_HERE" or not BOT_TOKEN:
        raise RuntimeError("Установи переменную окружения BOT_TOKEN")
    if ADMIN_ID == 0:
        raise RuntimeError("Установи переменную окружения ADMIN_ID (твой Telegram id)")

    db_init()

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    logger.info("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
