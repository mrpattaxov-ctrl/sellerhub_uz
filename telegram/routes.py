"""Telegram login routes extracted from app.py as a Flask Blueprint."""
from __future__ import annotations

import os
import secrets

from flask import Blueprint, jsonify, request, url_for
from flask_login import login_user
from sqlalchemy import select
from werkzeug.security import generate_password_hash

from extensions import SessionLocal
from models import User
from core.subscriptions import _ensure_user_trial_started


telegram_bp = Blueprint("telegram_bp", __name__)

_app = None


def init_telegram_routes(app_module):
    global _app
    _app = app_module


@telegram_bp.get("/api/telegram/code")
def api_tg_generate_code():
    _app._tg_clean_expired()
    code = secrets.token_hex(3).upper()
    _app._tg_set(code, type="code")
    return jsonify({"code": code})


@telegram_bp.get("/api/telegram/check/<code>")
def api_tg_check_code(code):
    code = code.upper()
    _app._tg_clean_expired()
    entry = _app._tg_get(code)
    if not entry:
        return jsonify({"status": "expired"})
    if not entry.get("confirmed"):
        return jsonify({"status": "waiting"})

    tg_id = entry["tg_id"]
    tg_username = entry["tg_username"] or f"tg_{tg_id}"

    with SessionLocal() as db:
        user = db.execute(select(User).where(User.telegram_id == tg_id)).scalar_one_or_none()
        if user is None:
            base = tg_username
            username = base
            suffix = 1
            while db.execute(select(User).where(User.username == username)).scalar_one_or_none():
                username = f"{base}_{suffix}"
                suffix += 1
            user = User(
                username=username,
                password_hash=generate_password_hash(os.urandom(32).hex()),
                telegram_id=tg_id,
                is_admin=False,
            )
            _ensure_user_trial_started(db, user)
            db.add(user)
            db.commit()
            db.refresh(user)
        login_user(user)

    _app._tg_delete(code)
    return jsonify({"status": "ok", "redirect": url_for("products_bp.groups_page")})


@telegram_bp.post("/api/telegram/send-approval")
def api_tg_send_approval():
    payload = request.get_json(force=True, silent=True) or {}
    phone_raw = (payload.get("phone") or "").strip()
    if not phone_raw:
        return jsonify({"error": "Номер телефона обязателен"}), 400

    import re as _re

    digits = _re.sub(r"[^\d]", "", phone_raw)
    phone_variants = ["+" + digits, digits]

    with SessionLocal() as db:
        user = None
        for phone_variant in phone_variants:
            user = db.execute(select(User).where(User.phone == phone_variant)).scalar_one_or_none()
            if user:
                break
        if not user or not user.telegram_id:
            return jsonify({"error": "Телефон не найден или Telegram не привязан к аккаунту. Войдите через имя пользователя."}), 404
        user_id = user.id
        tg_id = user.telegram_id

    token = secrets.token_hex(16)
    _app._tg_set(token, type="approval", user_id=user_id, tg_id=tg_id)

    try:
        import telebot as _tb

        cfg = _app._tg_config()
        bot_token = cfg.get("bot_token", "")
        if not bot_token:
            return jsonify({"error": "Бот не настроен"}), 500
        bot = _tb.TeleBot(bot_token, threaded=False)
        markup = _tb.types.InlineKeyboardMarkup()
        markup.add(
            _tb.types.InlineKeyboardButton("✅ Подтвердить вход", callback_data=f"approve:{token}"),
            _tb.types.InlineKeyboardButton("❌ Отклонить", callback_data=f"deny:{token}"),
        )
        bot.send_message(
            tg_id,
            "🔐 *Запрос на вход в Uzum Warehouse*\n\nКто-то входит с вашим номером телефона.\nЕсли это вы — нажмите «Подтвердить».",
            parse_mode="Markdown",
            reply_markup=markup,
        )
    except Exception as exc:
        _app._tg_delete(token)
        return jsonify({"error": f"Не удалось отправить сообщение: {exc}"}), 500

    return jsonify({"ok": True, "token": token})


@telegram_bp.get("/api/telegram/check-approval/<token>")
def api_tg_check_approval(token):
    _app._tg_clean_expired()
    entry = _app._tg_get(token)
    if not entry or entry.get("type") != "approval":
        return jsonify({"status": "expired"})
    if not entry.get("confirmed"):
        return jsonify({"status": "waiting"})

    with SessionLocal() as db:
        user = db.get(User, entry["user_id"])
        if not user:
            return jsonify({"status": "error"})
        login_user(user)

    _app._tg_delete(token)
    return jsonify({"status": "ok", "redirect": url_for("products_bp.groups_page")})
