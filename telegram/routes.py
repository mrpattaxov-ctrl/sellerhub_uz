"""Telegram login routes extracted from app.py as a Flask Blueprint."""
from __future__ import annotations

import os
import secrets

from flask import Blueprint, jsonify, request, session, url_for
from flask_login import login_user
from sqlalchemy import select
from werkzeug.security import generate_password_hash

from extensions import SessionLocal
from models import User
from core.redis_client import unrevoke_user
from core.subscriptions import (
    _ensure_user_trial_started,
    _get_or_create_subscription_settings,
    _subscription_status_for_user,
    write_session_subscription,
)

telegram_bp = Blueprint("telegram_bp", __name__)

_app = None


def init_telegram_routes(app_module):
    global _app
    _app = app_module


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
            cfg = _app._tg_config()
            bot_username = cfg.get("bot_username", "")
            # Create a contact_link pending token — bot will confirm it when user shares contact
            token = secrets.token_hex(16)
            # Store the token with type "contact_link" so the approval-checking route can distinguish it from regular approvals and skip the waiting state.
            _app._tg_set(token, type="contact_link", tg_username=digits)
            return jsonify({
                "not_linked": True,
                "bot_username": bot_username,
                "token": token,
            }), 200
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

# user clicks the approval button in Telegram, which hits the callback route in the bot, and the bot updates the pending entry with "confirmed": True. The frontend polls the check-approval route to see when it's confirmed, then logs in the user.
@telegram_bp.get("/api/telegram/check-approval/<token>")
def api_tg_check_approval(token):
    _app._tg_clean_expired()
    entry = _app._tg_get(token)
    if not entry or entry.get("type") not in ("approval", "contact_link"):
        return jsonify({"status": "expired"})
    
    if not entry.get("confirmed"):
        return jsonify({"status": "waiting"})

    with SessionLocal() as db:
        user = db.get(User, entry["user_id"])
        if not user:
            return jsonify({"status": "error"})
        login_user(user)
        # Step 0: mirror subscription state into the signed session + clear
        # any stale revoke on re-login.
        settings = _get_or_create_subscription_settings(db)
        status = _subscription_status_for_user(user, settings=settings)
        write_session_subscription(session, status)
        unrevoke_user(user.id)

    _app._tg_delete(token)
    return jsonify({"status": "ok", "redirect": url_for("products_bp.economics_page")})
