"""Auth-related routes extracted from app.py as a Flask Blueprint."""
from __future__ import annotations

import threading

from flask import (
    Blueprint, flash, jsonify, make_response, redirect,
    render_template, request, session, url_for,
)
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import select
from werkzeug.security import check_password_hash, generate_password_hash

from config import (
    BACKSTAGE_LOGIN_SESSION_KEY,
    NOTIFICATION_INTERVAL_OPTIONS,
    NOTIFICATION_SETTINGS_DEFAULTS,
    PAYME_KEY,
    PAYME_MERCHANT_ID,
    PAYME_MERCHANT_LOGIN,
    PAYME_TEST_KEY,
)
from extensions import SessionLocal
from models import NotificationSettings, SubscriptionOrder, User
from core.auth_helpers import (
    _current_user_is_admin,
    _get_admin_token,
    _json_response,
    _jwt_expires_in_seconds,
    _uzum_auto_login,
)
from core.subscriptions import (
    _activate_subscription_code,
    _ensure_user_trial_started,
    _get_or_create_subscription_settings,
    _subscription_plan_rows,
    _subscription_settings_dict,
    _subscription_status_for_user,
)
from core.time_helpers import _recommended_window_lengths

auth_bp = Blueprint("auth_bp", __name__)

# Late-bound references from app module (set by init_auth_routes)
_app = None
_ADMIN_SECRET = None


def init_auth_routes(app_module):
    """Bind references from the main app module. Called once after app is created."""
    global _app, _ADMIN_SECRET
    _app = app_module
    _ADMIN_SECRET = app_module._ADMIN_SECRET


# ---------------------------------------------------------------------------
# Helpers (used only by auth routes)
# ---------------------------------------------------------------------------

def _safe_next_url() -> str | None:
    candidate = (request.args.get("next") or request.form.get("next") or "").strip()
    if candidate.startswith("/") and not candidate.startswith("//"):
        return candidate
    return None


def _finish_admin_login(*, default_endpoint: str):
    if current_user.is_authenticated and _current_user_is_admin():
        return redirect(url_for(default_endpoint))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        with SessionLocal() as db:
            user = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
            if user and check_password_hash(user.password_hash, password) and user.is_admin:
                session.pop(BACKSTAGE_LOGIN_SESSION_KEY, None)
                login_user(user)
                return redirect(_safe_next_url() or url_for(default_endpoint))
        flash("Invalid admin credentials")

    return render_template("admin_login.html", form_action=request.path)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@auth_bp.get("/login")
def login():
    if current_user.is_authenticated:
        return redirect(url_for("products_bp.groups_page"))
    return render_template("login.html")


@auth_bp.route("/backstage/login", methods=["GET", "POST"])
def backstage_login():
    if not session.get(BACKSTAGE_LOGIN_SESSION_KEY):
        return render_template("not_found.html", message="Page not found"), 404
    return _finish_admin_login(default_endpoint="products_bp.groups_page")


@auth_bp.get("/admin")
def admin_entry():
    if current_user.is_authenticated and _current_user_is_admin():
        return redirect(url_for("admin_bp.admin_subscriptions_page"))
    return redirect(url_for("auth_bp.admin_login_page", next=_safe_next_url()))


@auth_bp.route("/admin/login", methods=["GET", "POST"])
def admin_login_page():
    return _finish_admin_login(default_endpoint="admin_bp.admin_subscriptions_page")


@auth_bp.get("/set-lang/<string:lang>")
def set_lang(lang: str):
    if lang in ("ru", "uz"):
        session["lang"] = lang
    return redirect(request.referrer or url_for("products_bp.groups_page"))


@auth_bp.route("/admin-<string:secret>/login", methods=["GET", "POST"])
def admin_login(secret: str):
    if secret != _ADMIN_SECRET:
        return render_template("not_found.html", message="Page not found"), 404
    session[BACKSTAGE_LOGIN_SESSION_KEY] = True
    next_url = _safe_next_url()
    if next_url:
        return redirect(url_for("auth_bp.backstage_login", next=next_url))
    return redirect(url_for("auth_bp.backstage_login"))


@auth_bp.route("/api/auth/uzum-sso", methods=["POST", "OPTIONS"])
def api_auth_uzum_sso():
    """Chrome Extension calls this to push a fresh Uzum Bearer token to the admin account."""
    # Handle CORS preflight from the extension
    if request.method == "OPTIONS":
        resp = make_response("", 204)
        origin = request.headers.get("Origin", "")
        if origin.startswith("chrome-extension://"):
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
            resp.headers["Access-Control-Allow-Credentials"] = "true"
        return resp

    try:
        payload = request.get_json(force=True, silent=True) or {}
        token = str(payload.get("token") or "").strip()

        if not token:
            return _json_response({"error": "Missing token"}, 400)

        if not token.startswith("Bearer "):
            token = f"Bearer {token}"

        # Save the token to the admin user (no Uzum API verification needed)
        with SessionLocal() as db:
            admin = db.execute(select(User).where(User.is_admin == True)).scalars().first()
            if not admin:
                return _json_response({"error": "No admin user found"}, 500)
            admin.api_key = token
            db.commit()

        origin = request.headers.get("Origin", "")
        resp = jsonify({"ok": True})
        if origin.startswith("chrome-extension://"):
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Access-Control-Allow-Credentials"] = "true"
        return resp
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@auth_bp.route("/logout")
@login_required
def logout():
    session.pop(BACKSTAGE_LOGIN_SESSION_KEY, None)
    logout_user()
    return redirect(url_for("auth_bp.login"))


@auth_bp.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not current_password or not new_password or not confirm_password:
            flash("Заполните все поля.")
            return render_template("change_password.html")
        if new_password != confirm_password:
            flash("Новый пароль и подтверждение не совпадают.")
            return render_template("change_password.html")
        if len(new_password) < 6:
            flash("Новый пароль должен содержать минимум 6 символов.")
            return render_template("change_password.html")

        with SessionLocal() as db:
            user = db.get(User, int(current_user.get_id()))
            if not user or not check_password_hash(user.password_hash, current_password):
                flash("Текущий пароль указан неверно.")
                return render_template("change_password.html")
            user.password_hash = generate_password_hash(new_password)
            user.must_change_password = False
            db.commit()

        flash("Пароль обновлён.")
        return redirect(url_for("products_bp.groups_page"))

    return render_template("change_password.html")

@auth_bp.route("/settings/api-key", methods=["GET", "POST"])
@login_required
def settings_api_key():
    if not _current_user_is_admin():
        flash("Только администратор может изменять Uzum API-токен.")
        return redirect(url_for("products_bp.groups_page"))
    if request.method == "POST":
        api_key = (request.form.get("api_key") or "").strip()
        uzum_phone = (request.form.get("uzum_phone") or "").strip()
        uzum_password = (request.form.get("uzum_password") or "").strip()
        with SessionLocal() as db:
            user = db.get(User, int(current_user.get_id()))
            if api_key:
                user.api_key = api_key
            if uzum_phone:
                user.uzum_phone = uzum_phone
            if uzum_password:
                user.uzum_password_plain = uzum_password
            db.add(user)
            db.commit()
        if uzum_phone or uzum_password:
            flash("Учётные данные Uzum сохранены. Выполняется автоматический вход...")
            threading.Thread(target=_uzum_auto_login, daemon=True).start()
        elif api_key:
            flash("API key saved")
        return redirect(url_for("auth_bp.settings_api_key"))
    with SessionLocal() as db:
        user = db.get(User, int(current_user.get_id()))
        current_key = user.api_key or ""
        uzum_phone = user.uzum_phone or ""
        has_password = bool(user.uzum_password_plain)
    return render_template("settings_api_key.html", api_key=current_key,
                           uzum_phone=uzum_phone, has_password=has_password)


@auth_bp.route("/settings/notifications", methods=["GET", "POST"])
@login_required
def settings_notifications():
    user_id = int(current_user.get_id())
    hours = list(range(24))

    if request.method == "POST":
        try:
            submitted = _app._coerce_notification_settings_payload({
                "hourly_enabled": request.form.get("hourly_enabled") == "on",
                "is_24h": request.form.get("is_24h") == "on",
                "window_from_hour": int(request.form.get("window_from_hour") or NOTIFICATION_SETTINGS_DEFAULTS["window_from_hour"]),
                "window_to_hour": int(request.form.get("window_to_hour") or NOTIFICATION_SETTINGS_DEFAULTS["window_to_hour"]),
                "interval_hours": int(request.form.get("interval_hours") or NOTIFICATION_SETTINGS_DEFAULTS["interval_hours"]),
            })
        except ValueError:
            flash("Некорректные параметры уведомлений.")
            return render_template(
                "settings_notifications.html",
                settings=_app._get_user_notification_settings(user_id),
                hours=hours,
                interval_options=NOTIFICATION_INTERVAL_OPTIONS,
                recommended_window_lengths=_recommended_window_lengths,
            )

        if not submitted["is_24h"] and submitted["window_length_hours"] <= 0:
            flash("Окно уведомлений должно быть положительным. Для конца дня выберите 00:00.")
            return render_template(
                "settings_notifications.html",
                settings=submitted,
                hours=hours,
                interval_options=NOTIFICATION_INTERVAL_OPTIONS,
                recommended_window_lengths=_recommended_window_lengths,
            )

        requested_interval = int(request.form.get("interval_hours") or submitted["interval_hours"])
        interval_adjusted = requested_interval != int(submitted["interval_hours"])

        with SessionLocal() as db:
            row = db.execute(
                select(NotificationSettings).where(NotificationSettings.user_id == user_id)
            ).scalar_one_or_none()
            if row is None:
                row = NotificationSettings(user_id=user_id)
                db.add(row)
            row.hourly_enabled = submitted["hourly_enabled"]
            row.window_from_hour = submitted["window_from_hour"]
            row.window_to_hour = submitted["window_to_hour"]
            row.is_24h = submitted["is_24h"]
            row.interval_hours = submitted["interval_hours"]
            db.commit()

        if interval_adjusted:
            flash("Интервал был скорректирован под длину выбранного окна уведомлений.")
        else:
            flash("Настройки уведомлений сохранены.")
        return redirect(url_for("auth_bp.settings_notifications"))

    settings = _app._get_user_notification_settings(user_id)
    return render_template(
        "settings_notifications.html",
        settings=settings,
        hours=hours,
        interval_options=NOTIFICATION_INTERVAL_OPTIONS,
        recommended_window_lengths=_recommended_window_lengths,
    )


@auth_bp.route("/subscription", methods=["GET", "POST"])
@login_required
def subscription_page():
    if _current_user_is_admin():
        return redirect(url_for("admin_bp.admin_subscriptions_page"))

    user_id = int(current_user.get_id())
    with SessionLocal() as db:
        user = db.get(User, user_id)
        settings = _get_or_create_subscription_settings(db)
        if user is not None and _ensure_user_trial_started(db, user):
            db.commit()
            db.refresh(user)

        payme_order_id = request.args.get("payme_order")
        if request.method == "GET" and payme_order_id:
            try:
                order = db.get(SubscriptionOrder, int(payme_order_id))
            except Exception:
                order = None
            if order is not None and int(order.user_id) == user_id:
                if order.status == "paid":
                    flash("Оплата Payme подтверждена. Подписка активирована.")
                elif order.status == "cancelled":
                    flash("Оплата Payme была отменена.")
                else:
                    flash("Платёж Payme обрабатывается. Если статус не обновился, откройте страницу ещё раз.")
            return redirect(url_for("auth_bp.subscription_page"))

        if request.method == "POST":
            ok, message, _ = _activate_subscription_code(
                db,
                user=user,
                raw_code=request.form.get("activation_code"),
                settings=settings,
            )
            flash(message)
            if ok:
                db.commit()
                return redirect(url_for("auth_bp.subscription_page"))

        status = _subscription_status_for_user(user, settings=settings)
        return render_template(
            "subscription.html",
            subscription_status=status,
            subscription_settings=_subscription_settings_dict(settings),
            subscription_plans=_subscription_plan_rows(settings=settings),
            payme_enabled=bool(PAYME_MERCHANT_ID and PAYME_MERCHANT_LOGIN and (PAYME_KEY or PAYME_TEST_KEY)),
        )


@auth_bp.get("/subscription/expired")
@login_required
def subscription_expired_page():
    if _current_user_is_admin():
        return redirect(url_for("products_bp.groups_page"))

    user_id = int(current_user.get_id())
    with SessionLocal() as db:
        user = db.get(User, user_id)
        settings = _get_or_create_subscription_settings(db)
        if user is not None and _ensure_user_trial_started(db, user):
            db.commit()
            db.refresh(user)
        status = _subscription_status_for_user(user, settings=settings)

    if status["active"]:
        return redirect(url_for("products_bp.groups_page"))

    return render_template("subscription_expired.html", subscription_status=status)


@auth_bp.post("/api/admin/uzum-credentials")
@login_required
def api_save_uzum_credentials():
    """Save Uzum login credentials for auto-refresh and trigger an immediate login."""
    if not _current_user_is_admin():
        return _json_response({"error": "Admin only"}, 403)
    payload = request.get_json(force=True, silent=True) or {}
    phone = (payload.get("phone") or "").strip()
    password = (payload.get("password") or "").strip()
    if not phone or not password:
        return _json_response({"error": "phone and password required"}, 400)
    with SessionLocal() as db:
        admin = db.execute(select(User).where(User.is_admin == True)).scalars().first()
        if not admin:
            return _json_response({"error": "No admin"}, 500)
        admin.uzum_phone = phone
        admin.uzum_password_plain = password
        db.commit()
    t = threading.Thread(target=_uzum_auto_login, daemon=True)
    t.start()
    t.join(timeout=20)
    token = _get_admin_token()
    if token:
        return _json_response({"ok": True, "message": "Вход выполнен, токен обновлён"})
    return _json_response({"ok": False, "message": "Credentials saved but login failed — check phone/password"}, 400)

@auth_bp.get("/api/user/api-key")
@login_required
def api_user_api_key_status():
    # Always report the admin token status (it's the one used for all Uzum calls).
    key = _get_admin_token()
    expires_in = _jwt_expires_in_seconds(key) if key else None
    return jsonify({
        "has_key": bool(key),
        "is_admin": _current_user_is_admin(),
        "expires_in_seconds": expires_in,
        "is_expired": (expires_in is not None and expires_in <= 0),
        "expires_soon": (expires_in is not None and 0 < expires_in < 1800),
    })

@auth_bp.post("/api/user/api-key")
@login_required
def api_user_api_key_set():
    if not _current_user_is_admin():
        return jsonify({"error": "Admin only"}), 403
    payload = request.get_json(force=True, silent=True) or {}
    api_key = str(payload.get("api_key") or "").strip()
    with SessionLocal() as db:
        user = db.get(User, int(current_user.get_id()))
        user.api_key = api_key if api_key else None
        db.add(user)
        db.commit()
    return jsonify({"ok": True, "has_key": bool(api_key)})
