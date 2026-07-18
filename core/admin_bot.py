"""Admin-only Telegram alert bot (token: ``ADMIN_TELEGRAM_TOKEN`` in .env).

Separate bot from the user-facing login bot — this one talks ONLY to the
platform admin. Any chat that /start-s it must enter the admin password
(checked against the ``is_admin`` user's password hash); a correct
password stores the chat in ``admin_alert_chats``, and from then on
``core.error_monitor`` pushes every captured user error to that chat the
moment it happens. Wrong-password attempts are throttled per chat.

Runs as a long-polling daemon thread started from background/startup.py
(same single-owner lock as the other background loops).
"""
from __future__ import annotations

import os
import time as _time
from datetime import datetime, timedelta

from sqlalchemy import func, select
from werkzeug.security import check_password_hash

from extensions import SessionLocal
from models import AdminAlertChat, ErrorEvent, User

# chat_id -> [attempt timestamps] — wrong-password throttle (5 per 10 min).
_pw_attempts: dict[str, list[float]] = {}
_PW_MAX_ATTEMPTS = 5
_PW_WINDOW_SEC = 600

WELCOME = (
    "🛡 <b>Sellerhub · мониторинг ошибок</b>\n\n"
    "Этот бот отправляет уведомления об ошибках пользователей.\n"
    "Доступ только для администратора.\n\n"
    "🔐 Введите пароль администратора:"
)
CONNECTED = (
    "✅ <b>Пароль верный. Вы подключены к уведомлениям.</b>\n\n"
    "Каждая ошибка пользователя будет приходить сюда мгновенно.\n\n"
    "Команды:\n"
    "/status — сводка за 24 часа\n"
    "/test — тестовое уведомление\n"
    "/stop — отключить уведомления"
)
WRONG_PASSWORD = "❌ Неверный пароль. Попробуйте ещё раз."
THROTTLED = "⛔️ Слишком много попыток. Подождите 10 минут."
STOPPED = "🔕 Уведомления отключены. /start — включить снова."


def _is_connected(chat_id: str) -> bool:
    with SessionLocal() as db:
        row = db.get(AdminAlertChat, str(chat_id))
        return bool(row and row.is_active)


def _has_row(chat_id: str) -> bool:
    with SessionLocal() as db:
        return db.get(AdminAlertChat, str(chat_id)) is not None


def _connect_chat(chat_id: str, tg_username: str | None, first_name: str | None) -> None:
    with SessionLocal() as db:
        row = db.get(AdminAlertChat, str(chat_id))
        if row is None:
            db.add(AdminAlertChat(
                chat_id=str(chat_id),
                tg_username=tg_username,
                first_name=first_name,
                is_active=True,
            ))
        else:
            row.is_active = True
            row.tg_username = tg_username or row.tg_username
            row.first_name = first_name or row.first_name
        db.commit()


def _disconnect_chat(chat_id: str) -> None:
    with SessionLocal() as db:
        row = db.get(AdminAlertChat, str(chat_id))
        if row is not None:
            row.is_active = False
            db.commit()


def _check_admin_password(text: str) -> bool:
    text = (text or "").strip()
    if not text:
        return False
    with SessionLocal() as db:
        admins = db.execute(
            select(User).where(User.is_admin == True)  # noqa: E712
        ).scalars().all()
    return any(
        check_password_hash(a.password_hash or "", text) for a in admins
    )


def _pw_throttled(chat_id: str) -> bool:
    now = _time.time()
    attempts = [t for t in _pw_attempts.get(chat_id, []) if now - t < _PW_WINDOW_SEC]
    _pw_attempts[chat_id] = attempts
    return len(attempts) >= _PW_MAX_ATTEMPTS


def _pw_note_attempt(chat_id: str) -> None:
    _pw_attempts.setdefault(chat_id, []).append(_time.time())


def _status_text() -> str:
    now = datetime.utcnow()
    with SessionLocal() as db:
        day_rows = db.execute(
            select(
                func.count(ErrorEvent.id),
                func.coalesce(func.sum(ErrorEvent.count), 0),
                func.count(func.distinct(ErrorEvent.user_id)),
            ).where(ErrorEvent.last_seen_at > now - timedelta(hours=24))
        ).one()
        unresolved = db.execute(
            select(func.count(ErrorEvent.id)).where(ErrorEvent.resolved == False)  # noqa: E712
        ).scalar_one()
    uniq, hits, users = int(day_rows[0]), int(day_rows[1]), int(day_rows[2])
    if uniq == 0:
        head = "🟢 <b>За 24 часа ошибок нет.</b>"
    else:
        head = f"📊 <b>Сводка за 24 часа</b>"
    return (
        f"{head}\n\n"
        f"⚠️ Ошибок: <b>{uniq}</b> (повторов всего: {hits})\n"
        f"👥 Затронуто пользователей: <b>{users}</b>\n"
        f"📌 Не решено всего: <b>{unresolved}</b>"
    )


def run_admin_bot() -> None:
    """Long-polling loop. Never returns; safe to run in a daemon thread."""
    token = (os.environ.get("ADMIN_TELEGRAM_TOKEN") or "").strip()
    if not token:
        print("[AdminBot] ADMIN_TELEGRAM_TOKEN not set — admin alert bot disabled.")
        return
    try:
        import telebot
    except Exception as exc:
        print(f"[AdminBot] telebot import failed: {exc!r}")
        return

    bot = telebot.TeleBot(token, threaded=False, parse_mode="HTML")

    @bot.message_handler(commands=["start"])
    def handle_start(msg):
        chat_id = str(msg.chat.id)
        if _is_connected(chat_id):
            bot.send_message(msg.chat.id, "✅ Вы уже подключены к уведомлениям.\n\n" + _status_text())
        elif _has_row(chat_id):
            # Known chat that pressed /stop earlier — no password re-entry.
            _connect_chat(chat_id, msg.from_user.username, msg.from_user.first_name)
            bot.send_message(msg.chat.id, "🔔 Уведомления снова включены.")
        else:
            bot.send_message(msg.chat.id, WELCOME)

    @bot.message_handler(commands=["stop"])
    def handle_stop(msg):
        chat_id = str(msg.chat.id)
        if _is_connected(chat_id):
            _disconnect_chat(chat_id)
            bot.send_message(msg.chat.id, STOPPED)

    @bot.message_handler(commands=["status"])
    def handle_status(msg):
        if _is_connected(str(msg.chat.id)):
            bot.send_message(msg.chat.id, _status_text())

    @bot.message_handler(commands=["test"])
    def handle_test(msg):
        if _is_connected(str(msg.chat.id)):
            from core.error_monitor import format_error_alert
            bot.send_message(msg.chat.id, format_error_alert(
                user_id=None, username="test_user",
                path="/api/test/error", method="POST", referer="/economics",
                status_code=500, error_message="Тестовое уведомление — всё работает",
                repeat_count=1,
            ))

    @bot.message_handler(content_types=["text"])
    def handle_text(msg):
        chat_id = str(msg.chat.id)
        if _is_connected(chat_id):
            bot.send_message(msg.chat.id, "Команды: /status · /test · /stop")
            return
        # Not connected — treat the message as a password attempt.
        if _pw_throttled(chat_id):
            bot.send_message(msg.chat.id, THROTTLED)
            return
        if _check_admin_password(msg.text):
            _connect_chat(chat_id, msg.from_user.username, msg.from_user.first_name)
            _pw_attempts.pop(chat_id, None)
            # Best effort: wipe the plaintext password from the chat history.
            try:
                bot.delete_message(msg.chat.id, msg.message_id)
            except Exception:
                pass
            bot.send_message(msg.chat.id, CONNECTED)
        else:
            _pw_note_attempt(chat_id)
            bot.send_message(msg.chat.id, WRONG_PASSWORD)

    print("[AdminBot] admin alert bot polling started")
    while True:
        try:
            bot.infinity_polling(timeout=30, long_polling_timeout=20)
        except Exception as exc:
            print(f"[AdminBot] polling crashed, restarting in 10s: {exc!r}")
            _time.sleep(10)
