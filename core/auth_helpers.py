"""Authentication and authorization helpers."""
from __future__ import annotations

import json
from datetime import datetime
from functools import wraps
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from urllib.parse import urlencode

from flask import g, request, redirect, url_for
from flask_login import current_user
from sqlalchemy import select

from extensions import SessionLocal
from models import User, Shop


def _json_response(obj, status=200):
    """Return a JSON response using the current Flask app's response_class."""
    from flask import current_app
    return current_app.response_class(
        json.dumps(obj, ensure_ascii=False, indent=2),
        status=status,
        mimetype="application/json",
    )


def _current_user_is_admin() -> bool:
    try:
        return bool(getattr(current_user, "is_admin", False))
    except Exception:
        return False


def admin_required(f):
    """Decorator: blocks non-admin users with 403 (JSON for API routes, redirect for pages)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not _current_user_is_admin():
            if request.accept_mimetypes.accept_json and not request.accept_mimetypes.accept_html:
                return _json_response({"error": "Admin access required"}, 403)
            return redirect(url_for("products_bp.economics_page"))
        return f(*args, **kwargs)
    return decorated


def _get_fresh_api_key() -> str:
    """Load the current user's api_key directly from DB (always up to date)."""
    with SessionLocal() as db:
        user = db.get(User, int(current_user.get_id()))
        return (user.api_key or "").strip() if user else ""


def _get_admin_token() -> str:
    """Return the admin user's Uzum API token. All Uzum API calls must use this."""
    with SessionLocal() as db:
        admin = db.execute(select(User).where(User.is_admin == True)).scalars().first()
        return (admin.api_key or "").strip() if admin else ""

#Automatically refresh the Uzum API token if it's expired or missing, and save it to DB.
def _uzum_auto_login() -> bool:
    """Login to Uzum via OAuth2 password grant and save the fresh token. Returns True on success.

    Every outcome is reported to the admin Telegram bot via
    ``core.error_monitor.notify_admin_token_status`` (alert on failure,
    6h reminders while broken, recovery message when it works again)."""
    from core.error_monitor import notify_admin_token_status
    try:
        with SessionLocal() as db:
            admin = db.execute(select(User).where(User.is_admin == True)).scalars().first()
            if not admin or not admin.uzum_phone or not admin.uzum_password_plain:
                notify_admin_token_status(False, "У администратора не заполнены телефон/пароль Uzum — авто-логин невозможен")
                return False
            identifier = admin.uzum_phone.strip()
            password = admin.uzum_password_plain.strip()

        # OAuth2 password grant to /api/oauth/token
        form_data = urlencode({
            "grant_type": "password",
            "username": identifier,
            "password": password,
        }).encode()

        req = Request(
            "https://api-seller.uzum.uz/api/oauth/token",
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": "Basic YjJiLWZyb250OmNsaWVudFNlY3JldA==",
            },
            data=form_data,
        )
        with urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode())

        token = body.get("access_token") or ""
        if not token:
            print("[AutoLogin] OAuth succeeded but no access_token in response:", list(body.keys()))
            notify_admin_token_status(False, "Uzum OAuth ответил без access_token")
            return False

        if not token.startswith("Bearer "):
            token = f"Bearer {token}"

        with SessionLocal() as db:
            admin = db.execute(select(User).where(User.is_admin == True)).scalars().first()
            if admin:
                admin.api_key = token
                db.commit()

        expires_in = body.get("expires_in", "?")
        print(f"[AutoLogin] Token refreshed successfully at {datetime.utcnow().isoformat()} (expires_in={expires_in}s)")
        notify_admin_token_status(True)
        return True
    except HTTPError as e:
        resp_body = e.read().decode(errors="ignore")
        print(f"[AutoLogin] HTTP {e.code}: {resp_body[:300]}")
        notify_admin_token_status(False, f"Uzum OAuth HTTP {e.code} — вероятно, неверные логин/пароль или Uzum недоступен")
        return False
    except Exception as e:
        print(f"[AutoLogin] Error: {e}")
        notify_admin_token_status(False, f"Сбой авто-логина: {e}")
        return False


def _user_shop_ids(user_id: int) -> list[int]:
    """Return shop DB IDs visible to the given user.
    Admin sees their own + unassigned shops. Regular users see ONLY their own shops.
    Result is cached in Flask's g object so DB is hit at most once per request."""
    cache_key = f"_shop_ids_{user_id}"
    cached = getattr(g, cache_key, None)
    if cached is not None:
        return cached

    with SessionLocal() as db:
        user = db.get(User, user_id)
        if not user:
            result: list[int] = []
        elif user.is_admin:
            stmt = select(Shop).where(
                (Shop.owner_id == user_id) | (Shop.owner_id == None)
            )
            result = [s.id for s in db.execute(stmt).scalars().all()]
        else:
            stmt = select(Shop).where(Shop.owner_id == user_id)
            result = [s.id for s in db.execute(stmt).scalars().all()]

    setattr(g, cache_key, result)
    return result


def _jwt_expires_in_seconds(token: str) -> int | None:
    """Decode the 'exp' claim from a JWT without verifying its signature."""
    try:
        import base64
        raw = token.removeprefix("Bearer ").strip()
        parts = raw.split(".")
        if len(parts) < 2:
            return None
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64).decode())
        exp = payload.get("exp")
        if not exp:
            return None
        return int(exp) - int(datetime.utcnow().timestamp())
    except Exception:
        return None
