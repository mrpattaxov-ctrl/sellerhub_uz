from __future__ import annotations

import json

# ── Imports from extracted core modules ──────────────────────────────
from config import (
    ENABLE_DEBUG_ROUTES, APP_DIR, DATA_DIR,
    DATABASE_URL, DB_POOL_SIZE, DB_MAX_OVERFLOW, DB_POOL_RECYCLE_SECONDS,
    SECRET_KEY,
    FINANCE_REFRESH_DAYS,
    HOURLY_SALES_BURST_FETCH_ENABLED, HOURLY_SALES_BURST_FETCH_WORKERS,
    APP_TIMEZONE_NAME, APP_TZ_OFFSET_HOURS, APP_TZ,
    NOTIFICATION_INTERVAL_OPTIONS, NOTIFICATION_SETTINGS_DEFAULTS,
    BACKSTAGE_LOGIN_SESSION_KEY,
    HTTP_USER_AGENT, HTTP_ACCEPT_LANGUAGE, HTTP_POOL_MAXSIZE,
)
from extensions import engine, SessionLocal, login_manager, DB_URL_DISPLAY
from core.parsers import _safe_qty, _extract_uzum_qty, _extract_sku, _collect_variant_rows, _safe_status_text, _safe_text
from core.time_helpers import (
    _now_app_tz, _today_app_tz, _app_dt, _app_day_start_ts, _app_day_end_ts,
    _app_naive, _notification_window_length_hours, _compatible_notification_intervals,
    _notification_daily_summary_send_hour, _notification_interval_send_hours,
    _recommended_window_lengths,
)
from core.http_client import _get_http_session, http_post_multipart
from core.uzum_openapi import fetch_products_page as _openapi_fetch_products_page
from core.auth_helpers import (
    _json_response, _jwt_expires_in_seconds, _get_fresh_api_key, _get_admin_token,
    _uzum_auto_login, _current_user_is_admin, admin_required, _user_shop_ids,
)
from core.subscriptions import (
    SESSION_SUB_EXPIRES_KEY,
    SESSION_SUB_IS_TRIAL_KEY,
    SESSION_SUB_PLAN_KEY,
    _ensure_user_trial_started,
    _get_or_create_subscription_settings,
    _get_subscription_context_for_user,
    _subscription_status_for_user,
    write_session_subscription,
)
from core.redis_client import is_user_revoked
from core import shop_lock

import io
import os
import time
import threading
from datetime import date, datetime, timedelta, timezone, time as dt_time
from urllib.error import HTTPError

import requests
from flask import Flask, jsonify, request, render_template, redirect, url_for, send_file, flash, session
from flask_cors import CORS
from flask_login import LoginManager, login_user, login_required, logout_user, current_user
from sqlalchemy import create_engine, select, func, desc, delete, update, text, insert, inspect
from sqlalchemy.orm import joinedload
from werkzeug.security import generate_password_hash, check_password_hash

try:
    import openpyxl
    from openpyxl.styles import Font, Alignment, Border, Side
except ImportError:
    openpyxl = None

from models import (
    Base,
    FinanceHourlySnapshot,
    FinanceOrder,
    NotificationSettings,
    ProductGroup,
    Shop,
    SubscriptionCode,
    SubscriptionCodeActivation,
    SubscriptionSettings,
    TelegramPending,
    User,
    Variant,
    VariantSale,
)

DB_URL = DATABASE_URL

def _safe_create_all():
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("SELECT pg_advisory_lock(922337203685477000)")
            try:
                Base.metadata.create_all(bind=conn)
            finally:
                conn.exec_driver_sql("SELECT pg_advisory_unlock(922337203685477000)")
    except Exception as e:
        print(f"[DB] Metadata create_all skipped: {e}")
        Base.metadata.create_all(engine)


_safe_create_all()


def _ensure_postgres_runtime_schema():
    statements = [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_started_at TIMESTAMP NULL",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_expires_at TIMESTAMP NULL",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_is_unlimited BOOLEAN NOT NULL DEFAULT FALSE",
        "UPDATE users SET trial_started_at = CURRENT_TIMESTAMP WHERE trial_started_at IS NULL AND COALESCE(is_admin, FALSE) = FALSE",
        "ALTER TABLE notification_settings ALTER COLUMN window_to_hour SET DEFAULT 20",
    ]
    try:
        with engine.begin() as conn:
            for stmt in statements:
                conn.exec_driver_sql(stmt)
    except Exception as e:
        print(f"[DB] Runtime schema ensure skipped: {e}")


def _bootstrap_default_admin():
    admin_password = (os.getenv("ADMIN_DEFAULT_PASSWORD") or "admin").strip() or "admin"
    try:
        with SessionLocal() as db:
            has_users = db.execute(select(User.id)).first() is not None
            if has_users:
                return
            admin = User(
                username="admin",
                password_hash=generate_password_hash(admin_password),
                is_admin=True,
                must_change_password=True,
            )
            db.add(admin)
            db.commit()
        print(
            f"[ADMIN] Default admin created. Username: admin / Password: {admin_password}. "
            "Change on first login.",
            flush=True,
        )
    except Exception as e:
        print(f"[ADMIN] Default admin bootstrap skipped: {e}", flush=True)


_ensure_postgres_runtime_schema()
_bootstrap_default_admin()


def _drop_legacy_product_tables():
    legacy_tables = [name for name in ("sales", "products") if inspect(engine).has_table(name)]
    if not legacy_tables:
        return
    try:
        with engine.begin() as conn:
            for table_name in legacy_tables:
                conn.exec_driver_sql(f"DROP TABLE IF EXISTS {table_name}")
        print(f"[DB] Dropped legacy tables: {', '.join(legacy_tables)}")
    except Exception as e:
        print(f"[DB] Legacy table cleanup skipped: {e}")


_drop_legacy_product_tables()

from concurrent.futures import ThreadPoolExecutor, as_completed

_hourly_burst_stats_lock = threading.Lock()
_hourly_burst_stats = {
    "enabled": HOURLY_SALES_BURST_FETCH_ENABLED,
    "workers": HOURLY_SALES_BURST_FETCH_WORKERS,
    "in_progress": False,
    "last_status": "idle",
    "last_started_at": None,
    "last_finished_at": None,
    "last_window_from": None,
    "last_window_to": None,
    "last_duration_seconds": None,
    "last_total_shops": 0,
    "last_success_count": 0,
    "last_failed_count": 0,
    "last_failed_shops": [],
}

def _ensure_common_db_indexes():
    """Create indexes that matter for the finance hot path."""
    statements = [
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_ns_user_id ON notification_settings(user_id)",
        "CREATE INDEX IF NOT EXISTS ix_sc_code ON subscription_codes(code)",
        "CREATE INDEX IF NOT EXISTS ix_sca_user_activated ON subscription_code_activations(user_id, activated_at)",
        # Product sync hot path: variant lookups during sync (eliminates N+1 seq-scans)
        "CREATE INDEX IF NOT EXISTS ix_variant_uzum_sku_id ON variants(uzum_sku_id)",
        "CREATE INDEX IF NOT EXISTS ix_variant_barcode ON variants(barcode)",
        "CREATE INDEX IF NOT EXISTS ix_variant_sku ON variants(sku)",
        "CREATE INDEX IF NOT EXISTS ix_variant_group_id ON variants(group_id)",
        "CREATE INDEX IF NOT EXISTS ix_pg_uzum_product_id_shop ON product_groups(uzum_product_id, shop_id)",
        # Groups page and warehouse queries
        "CREATE INDEX IF NOT EXISTS ix_pg_shop_id ON product_groups(shop_id)",
        "CREATE INDEX IF NOT EXISTS ix_pg_is_archived ON product_groups(is_archived)",
        "CREATE INDEX IF NOT EXISTS ix_variant_sale_variant_date ON variant_sales(variant_id, date)",
    ]
    try:
        with engine.begin() as conn:
            for stmt in statements:
                conn.exec_driver_sql(stmt)
    except Exception as e:
        print(f"[DB] Index ensure skipped: {e}")


_ensure_common_db_indexes()


app = Flask(__name__)
CORS(app)
app.secret_key = SECRET_KEY
# Persistent login: by default Flask issues a browser-session cookie that
# is deleted when the browser fully closes, so users had to re-login after
# quitting the browser. Making the session permanent with a 30-day lifetime
# (sliding — refreshed on each request) keeps users logged in across full
# browser restarts, like most web apps. Applies to Telegram + admin logins.
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
app.config["SESSION_REFRESH_EACH_REQUEST"] = True

_ADMIN_SECRET = os.environ.get("ADMIN_SECRET_PATH", "").strip()
if not _ADMIN_SECRET:
    import secrets as _secrets
    _ADMIN_SECRET = _secrets.token_urlsafe(16)
print(f"\n[ADMIN] Protected backstage route: /backstage/login", flush=True)
print(f"[ADMIN] Secret admin entry: /admin-{_ADMIN_SECRET}/login\n", flush=True)

login_manager.init_app(app)
login_manager.login_view = "auth_bp.login"

@app.before_request
def _make_session_permanent():
    """Mark every session permanent so the auth cookie survives a full
    browser quit/reopen. Lifetime is PERMANENT_SESSION_LIFETIME (30 days),
    refreshed on each request so active users are never logged out."""
    session.permanent = True


@app.before_request
def _block_debug_routes():
    """Return 404 for /debug/* and /lost-goods* when ENABLE_DEBUG_ROUTES is False."""
    if not ENABLE_DEBUG_ROUTES:
        path = request.path
        if path.startswith("/debug/") or path.startswith("/lost-goods") or path.startswith("/api/lost-goods"):
            return "Not found", 404


@app.before_request
def _force_password_change():
    if not current_user.is_authenticated:
        return None
    if not getattr(current_user, "must_change_password", False):
        return None
    if request.endpoint in ("auth_bp.change_password", "change_password", "auth_bp.logout", "logout", "static"):
        return None
    return redirect(url_for("auth_bp.change_password"))


@app.before_request
def _enforce_active_subscription():
    if not current_user.is_authenticated or getattr(current_user, "is_admin", False):
        return None

    allowed_endpoints = {
        "auth_bp.change_password",
        "auth_bp.logout",
        "auth_bp.subscription_page",
        "auth_bp.subscription_expired_page",
        "static",
    }
    if request.endpoint in allowed_endpoints:
        return None

    user_id = int(current_user.get_id())

    # Force-revoke blocklist: checked on EVERY gated request (even fresh
    # sessions) so admin-cancel / Payme chargeback propagates instantly.
    # If Redis is down this raises — we want loud failure, not silent bypass.
    if is_user_revoked(user_id):
        return _deny_expired_subscription()

    # Step 0 hot path: signed Flask session carries the user's expiry so we
    # skip Postgres on 99% of requests. See write_session_subscription() for
    # every place the session keys get (re)written.
    session_plan = session.get(SESSION_SUB_PLAN_KEY)
    session_expires_iso = session.get(SESSION_SUB_EXPIRES_KEY)

    if session_plan in ("admin", "unlimited"):
        return None

    if session_plan in ("trial", "paid") and session_expires_iso:
        try:
            expires_at = datetime.fromisoformat(str(session_expires_iso))
        except ValueError:
            expires_at = None
        if expires_at is not None and expires_at > datetime.utcnow():
            return None

    # Fall back to the slow path: recompute, refresh session, then decide.
    with SessionLocal() as db:
        user = db.get(User, user_id)
        if not user:
            return None
        settings = _get_or_create_subscription_settings(db)
        if _ensure_user_trial_started(db, user):
            db.commit()
            db.refresh(user)
        status = _subscription_status_for_user(user, settings=settings)

    write_session_subscription(session, status)

    if status["active"]:
        return None
    return _deny_expired_subscription()


def _deny_expired_subscription():
    if request.path.startswith("/api/"):
        return _json_response({
            "error": "Subscription expired",
            "redirect": url_for("auth_bp.subscription_page"),
        }, 402)
    return redirect(url_for("auth_bp.subscription_expired_page"))


from translations import get_translations


@app.context_processor
def _inject_lang():
    lang = session.get("lang", "ru")
    return {"lang": lang, "t": get_translations(lang)}


@app.context_processor
def _inject_subscription_context():
    if not current_user.is_authenticated or getattr(current_user, "is_admin", False):
        return {}
    return {
        "subscription_context": _get_subscription_context_for_user(int(current_user.get_id()))
    }

@login_manager.unauthorized_handler
def unauthorized():
    if request.path.startswith("/api/"):
        return _json_response({"error": "Unauthorized. Please log in."}, 401)
    return redirect(url_for("auth_bp.login"))

@login_manager.user_loader
def load_user(user_id):
    with SessionLocal() as db:
        return db.get(User, int(user_id))


# ----------------------------
# Auto-login scheduler (started only by the BG-owner worker — see background/startup.py)
# ----------------------------
_auto_login_scheduler = None

def _start_auto_login_scheduler():
    """Start the 90-minute auto-login loop + an immediate first login. Called once by the BG-owner worker."""
    global _auto_login_scheduler
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        _auto_login_scheduler = BackgroundScheduler(daemon=True)
        _auto_login_scheduler.add_job(_uzum_auto_login, "interval", minutes=90, id="uzum_auto_login", replace_existing=True)
        _auto_login_scheduler.start()
        _threading.Thread(target=_uzum_auto_login, daemon=True).start()
        print("[AutoLogin] Scheduler started (every 90 min)")
    except Exception as e:
        print(f"[AutoLogin] Scheduler not started: {e}")


def http_json(url: str, method: str = "GET", body: dict | None = None, headers: dict | None = None) -> dict:
    """Wrapper that auto-injects the admin token into core.http_client.http_json."""
    from core.http_client import http_json as _http_json_impl
    return _http_json_impl(url, method, body, headers, _get_admin_token=_get_admin_token)


def pick(obj: dict, keys: list[str], default=None):
    for k in keys:
        if obj is None:
            break
        if k in obj and obj[k] not in (None, ""):
            return obj[k]
    return default


def first_list_item(v):
    return v[0] if isinstance(v, list) and v else None


def find_first_array(obj, preferred_keys: list[str]):
    if obj is None:
        return None
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        # preferred keys first
        for k in preferred_keys:
            v = obj.get(k)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                found = find_first_array(v, preferred_keys)
                if found is not None:
                    return found
        # walk all values
        for v in obj.values():
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                found = find_first_array(v, preferred_keys)
                if found is not None:
                    return found
    return None


def _extract_url_from_value(val) -> str | None:
    """Helper to extract a URL string from a value (str, dict, or list item)."""
    s = None
    if isinstance(val, str) and val.strip():
        s = val.strip()
    elif isinstance(val, dict):
        for k in ["url", "link", "src", "path", "href", "content", "payload", "original", "preview"]:
            sub = val.get(k)
            if isinstance(sub, str) and sub.strip():
                s = sub.strip()
                break
    if s:
        if not s.startswith("http") and not s.startswith("data:") and not s.startswith("/"):
            return f"https://images.uzum.uz/{s}/t_product_540_high.jpg"
        # Fix for Uzum URLs that are missing the filename suffix
        if "images.uzum.uz" in s and not s.endswith(".jpg") and not s.endswith(".png"):
            return f"{s.rstrip('/')}/t_product_540_high.jpg"
        return s
    return None

def extract_group_image(p: dict) -> str | None:
    # Prefer primary/original-looking fields over preview/thumbnail fields.
    primary_keys = ["image", "imageUrl", "photo", "photoUrl", "picture"]
    preview_keys = ["thumbnail", "thumbnailUrl", "preview", "previewImage", "previewImg", "preview_image", "previewImages"]

    for k in primary_keys:
        val = p.get(k)
        url = _extract_url_from_value(val)
        if url:
            return url

    for k in ["images", "photos", "pictureUrls", "productImages"]:
        val = p.get(k)
        if isinstance(val, list) and val:
            url = _extract_url_from_value(val[0])
            if url:
                return url

    for k in preview_keys:
        val = p.get(k)
        url = _extract_url_from_value(val)
        if url:
            return url
    return None


def _extract_variant_image(obj: dict) -> str | None:
    """Helper to extract image for a variant, prioritizing real product images."""
    primary_keys = ["image", "photo", "imageUrl", "img", "picture", "pic"]
    preview_keys = ["previewImage", "previewImg", "preview_image", "preview", "previewPhoto", "preview_photo", "previewImages"]

    for k in primary_keys:
        val = obj.get(k)
        if not val:
            continue

        url = _extract_url_from_value(val)
        if url:
            return url

        if isinstance(val, list) and val:
            url = _extract_url_from_value(val[0])
            if url:
                return url

    for k in ["images", "photos", "pictureUrls", "productImages"]:
        val = obj.get(k)
        if isinstance(val, list) and val:
            url = _extract_url_from_value(val[0])
            if url:
                return url

    for k in preview_keys:
        val = obj.get(k)
        if not val:
            continue
        url = _extract_url_from_value(val)
        if url:
            return url
        if isinstance(val, list) and val:
            url = _extract_url_from_value(val[0])
            if url:
                return url

    return extract_group_image(obj)

def extract_variants(p: dict) -> list[dict]:
    # Uzum product detail often includes a list of SKUs/variants; we search common keys.
    keys = ["skuList", "skus", "variants", "offers", "items", "skuTable"]
    arr = None
    for k in keys:
        if isinstance(p.get(k), list):
            arr = p.get(k)
            break
    if arr is None:
        # Check specific wrappers only. Do NOT use recursive find_first_array here,
        # as it might pick up "relatedProducts" or "similar" lists from other parts of the JSON.
        for wrapper in ["payload", "data", "result", "content"]:
            w = p.get(wrapper)
            if isinstance(w, dict):
                for k in keys:
                    if isinstance(w.get(k), list):
                        arr = w.get(k)
                        break
            if arr:
                break
    return arr if isinstance(arr, list) else []


def variant_sales_last30_map(db, variant_ids: list[int]) -> dict:
    if not variant_ids:
        return {}
    since = date.today() - timedelta(days=30)
    stmt = (
        select(VariantSale.variant_id, func.coalesce(func.sum(VariantSale.qty_sold), 0))
        .where(VariantSale.variant_id.in_(variant_ids))
        .where(VariantSale.date >= since)
        .group_by(VariantSale.variant_id)
    )
    return {vid: int(total or 0) for (vid, total) in db.execute(stmt).all()}

def fetch_finance_sales_map(shop_id, api_key=None, days=30, date_from_ts=None, date_to_ts=None):
    """Helper to fetch sales from finance API for a given date range.
    Returns dict if successful, or None if API failed.
    """
    now_dt = _now_app_tz()
    now_ts = date_to_ts or int(now_dt.timestamp())
    past_ts = date_from_ts or int((now_dt - timedelta(days=days)).timestamp())
    
    auth_val = (api_key if api_key.startswith("Bearer ") else f"Bearer {api_key}") if api_key else None
    headers = {
        "Authorization": auth_val,
        "Origin": "https://seller.uzum.uz",
        "Referer": "https://seller.uzum.uz/",
    } if auth_val else None

    def _loop(param_name):
        local_map = {}  # {identifier: {"qty": int, "price": int}}
        s_page = 0
        success = False
        while True:
            s_url = f"https://api-seller.uzum.uz/api/seller/finance/orders?{param_name}={shop_id}&dateFrom={past_ts}&dateTo={now_ts}&group=true&page={s_page}&size=500"
            try:
                s_res = http_json(s_url, headers=headers)
                success = True
            except Exception as e:
                print(f"Finance fetch error ({param_name}): {e}")
                return None
            s_items = find_first_array(s_res, ["payload", "result", "content", "data", "items", "rows", "list", "orders", "orderItems"]) or []
            if not isinstance(s_items, list) or not s_items:
                break
            for row in s_items:
                # Check if row is an order containing items
                sub_items = find_first_array(row, ["items", "products", "rows", "lines", "positions", "orderItems"])
                loop_items = sub_items if sub_items else [row]

                for item in loop_items:
                    # Extract all possible identifiers to ensure a match
                    # Check item itself AND nested 'product' object for identifiers
                    sources = [item]
                    if "product" in item and isinstance(item["product"], dict):
                        sources.append(item["product"])
                    if "sku" in item and isinstance(item["sku"], dict):
                        sources.append(item["sku"])
                    
                    identifiers = set()
                    
                    for src in sources:
                        # 1. Try standard extraction
                        s_sku = _extract_sku(src)
                        if s_sku: identifiers.add(s_sku)
                        
                        # 2. Explicitly grab other common keys (including skuTitle as requested)
                        for k in ["skuFullTitle", "skuTitle", "sku", "offerId", "shopSku", "sellerSku", "barcode", "ean", "skuId", "id"]:
                            val = str(pick(src, [k], default="") or "").strip()
                            if val: identifiers.add(val)

                    # Qty extraction: Priority on explicit quantity keys
                    # We include "amount" as requested for finance data
                    q_keys = [
                        "amount", "quantity", "qty", "count", "productCount", "itemsCount", "itemCount",
                        "sold", "sales", "totalCount", "totalQuantity", "quantityToStock"
                    ]
                    
                    q_val = pick(item, q_keys)
                    if q_val is None and "product" in item and isinstance(item["product"], dict):
                        q_val = pick(item["product"], q_keys)
                    if q_val is None and "sku" in item and isinstance(item["sku"], dict):
                        q_val = pick(item["sku"], q_keys)

                    s_qty = int(_safe_qty(q_val or 0))

                    # sellPrice — actual sale price total for this order line
                    sell_val = pick(item, ["sellPrice", "sell_price", "salePrice", "price"])
                    s_sell_price = 0
                    if sell_val is not None:
                        try: s_sell_price = float(sell_val)
                        except: pass

                    # commission — total commission for this order line
                    comm_val = pick(item, ["commission", "commissionFee", "uzumCommission"])
                    s_commission = 0
                    if comm_val is not None:
                        try: s_commission = float(comm_val)
                        except: pass

                    # logisticDeliveryFee — total logistics fee for this order line
                    logi_val = pick(item, ["logisticDeliveryFee", "deliveryFee", "logisticFee", "logistics"])
                    s_logistics = 0
                    if logi_val is not None:
                        try: s_logistics = float(logi_val)
                        except: pass

                    # storageFee / warehouse cost
                    store_val = pick(item, [
                        "storageFee", "storage_fee", "warehouseFee", "warehouse_fee",
                        "storagePrice", "fulfillmentFee", "fulfillment_fee",
                        "keepingFee", "keeping_fee",
                    ])
                    if store_val is None and "product" in item and isinstance(item["product"], dict):
                        store_val = pick(item["product"], ["storageFee", "warehouseFee", "fulfillmentFee", "keepingFee"])
                    s_storage = 0
                    if store_val is not None:
                        try: s_storage = float(store_val)
                        except: pass

                    # purchasePrice (cost price)
                    p_val = pick(item, ["purchasePrice", "purchase_price"])
                    if p_val is None and "product" in item and isinstance(item["product"], dict):
                        p_val = pick(item["product"], ["purchasePrice", "purchase_price"])
                    if p_val is None and "sku" in item and isinstance(item["sku"], dict):
                        p_val = pick(item["sku"], ["purchasePrice", "purchase_price"])
                    s_price = 0
                    if p_val is not None:
                        try: s_price = float(p_val)
                        except: pass

                    # sellerProfit
                    sp_val = pick(item, ["sellerProfit", "seller_profit", "profit"])
                    if sp_val is None and "product" in item and isinstance(item["product"], dict):
                        sp_val = pick(item["product"], ["sellerProfit", "seller_profit", "profit"])
                    s_seller_profit = 0
                    if sp_val is not None:
                        try: s_seller_profit = float(sp_val)
                        except: pass

                    if s_qty > 0:
                        keys_to_update = set()
                        for ident in identifiers:
                            keys_to_update.add(ident)
                            keys_to_update.add(ident.upper())

                        for k in keys_to_update:
                            if k not in local_map:
                                local_map[k] = {"qty": 0, "price": 0,
                                                "sell_price": 0, "commission": 0,
                                                "logistics": 0, "seller_profit": 0,
                                                "storage": 0}
                            local_map[k]["qty"] += s_qty
                            # Per-unit values (divide totals by amount)
                            if s_price > 0:
                                local_map[k]["price"] = int(s_price / s_qty)
                            if s_sell_price > 0:
                                local_map[k]["sell_price"] = int(s_sell_price / s_qty)
                            if s_commission > 0:
                                local_map[k]["commission"] = int(s_commission / s_qty)
                            if s_logistics > 0:
                                local_map[k]["logistics"] = int(s_logistics / s_qty)
                            if s_seller_profit > 0:
                                local_map[k]["seller_profit"] = int(s_seller_profit / s_qty)
                            if s_storage > 0:
                                local_map[k]["storage"] = int(s_storage / s_qty)
            if len(s_items) < 500: break
            s_page += 1
            if s_page > 50: break
        return local_map if success else None

    sales_map = _loop("shopIds")
    
    # If error (None) or empty, try fallback parameter
    if sales_map is None or not sales_map:
        fallback = _loop("shopId")
        if fallback is not None:
            # If fallback succeeded (even if empty), use it
            sales_map = fallback
            
    return sales_map


_WAREHOUSE_EXPENSE_CODE_LABELS = {
    "\u0421000003": "Оплата за хранение",   # С000003 (Cyrillic)
    "\u0421000004": "Хранение возвратов",   # С000004 (Cyrillic)
    "C000003": "Оплата за хранение",        # C000003 (Latin)
    "C000004": "Хранение возвратов",        # C000004 (Latin)
    "C000007": "Штрафы",
}


def _fetch_warehouse_expense_payments(shop_ids: list[str], api_key: str | None = None) -> list[dict]:
    if not shop_ids or not api_key:
        return []

    auth_val = (api_key if api_key.startswith("Bearer ") else f"Bearer {api_key}") if api_key else None
    headers = {
        "Authorization": auth_val,
        "Origin": "https://seller.uzum.uz",
        "Referer": "https://seller.uzum.uz/",
    } if auth_val else None

    ids_str = ",".join(str(s) for s in shop_ids)
    from urllib.parse import quote

    all_payments: list[dict] = []
    for source_name in ["Склад", "Ombor"]:
        page = 0
        while True:
            url = (f"https://api.uzum.uz/api/seller/finance/expenses"
                   f"?page={page}&size=100"
                   f"&sources={quote(source_name)}"
                   f"&shopIds={ids_str}")
            try:
                data = http_json(url, headers=headers)
            except Exception as e:
                print(f"[Expenses] fetch error source={source_name} page={page}: {e}")
                break

            payments = []
            total_pages = 1
            if isinstance(data, dict):
                payload = data.get("payload")
                if isinstance(payload, dict):
                    payments = payload.get("payments") or []
                    total_pages = payload.get("totalPages", 1)
            if not payments:
                payments = find_first_array(data, [
                    "payload", "result", "content", "data", "items",
                    "rows", "list", "expenses", "payments",
                ]) or []
            if not isinstance(payments, list):
                payments = []

            all_payments.extend(p for p in payments if isinstance(p, dict))
            page += 1
            if page >= total_pages:
                break

    seen_ids = set()
    unique_payments: list[dict] = []
    for payment in all_payments:
        pid = payment.get("id")
        if pid and pid in seen_ids:
            continue
        if pid:
            seen_ids.add(pid)
        unique_payments.append(payment)
    return unique_payments


def _filter_warehouse_expense_payments_for_day(
    payments: list[dict],
    target_day: date,
    *,
    allow_latest_fallback: bool = True,
) -> list[dict]:
    day_start_ms = int(_app_dt(target_day).timestamp() * 1000)
    day_end_ms = int(_app_dt(target_day + timedelta(days=1)).timestamp() * 1000)

    filtered: list[dict] = []
    for payment in payments:
        try:
            created_ts = int(payment.get("dateCreated") or payment.get("dateService") or 0)
        except (ValueError, TypeError):
            continue
        if day_start_ms <= created_ts < day_end_ms:
            filtered.append(payment)

    if filtered or not allow_latest_fallback or not payments:
        return filtered

    latest_ts = 0
    for payment in payments:
        try:
            created_ts = int(payment.get("dateCreated") or payment.get("dateService") or 0)
        except (ValueError, TypeError):
            continue
        if created_ts > latest_ts:
            latest_ts = created_ts

    if latest_ts <= 0:
        return []

    latest_day = datetime.fromtimestamp(latest_ts / 1000, APP_TZ).date()
    latest_start_ms = int(_app_dt(latest_day).timestamp() * 1000)
    latest_end_ms = int(_app_dt(latest_day + timedelta(days=1)).timestamp() * 1000)
    fallback: list[dict] = []
    for payment in payments:
        try:
            created_ts = int(payment.get("dateCreated") or payment.get("dateService") or 0)
        except (ValueError, TypeError):
            continue
        if latest_start_ms <= created_ts < latest_end_ms:
            fallback.append(payment)
    return fallback


def _build_warehouse_expense_snapshots_for_shops(payments: list[dict], shop_ids: list[str]) -> dict[str, dict]:
    grouped: dict[str, dict[str, int]] = {str(shop_id): {} for shop_id in shop_ids}

    for payment in payments:
        shop_id = str(payment.get("shopId") or "").strip()
        if not shop_id or shop_id not in grouped:
            continue
        code = str(payment.get("code") or "OTHER").strip() or "OTHER"
        price = payment.get("paymentPrice", 0)
        try:
            price = int(float(price))
        except (ValueError, TypeError):
            price = 0
        if price <= 0:
            continue
        label = _WAREHOUSE_EXPENSE_CODE_LABELS.get(code, code)
        grouped[shop_id][label] = grouped[shop_id].get(label, 0) + price

    snapshots: dict[str, dict] = {}
    for shop_id in shop_ids:
        bucket = grouped.get(str(shop_id), {})
        items = [
            {"name": name, "amount": amount}
            for name, amount in sorted(bucket.items(), key=lambda item: (-item[1], item[0]))
        ]
        snapshots[str(shop_id)] = {
            "items": items,
            "total": sum(item["amount"] for item in items),
        }
    return snapshots


def _merge_warehouse_expense_payloads(payloads: list[dict]) -> dict:
    merged: dict[str, int] = {}
    total = 0
    for payload in payloads:
        if not payload:
            continue
        total += int(payload.get("total", 0) or 0)
        for item in payload.get("items", []):
            name = str(item.get("name") or "").strip()
            amount = item.get("amount", 0)
            try:
                amount = int(amount)
            except (ValueError, TypeError):
                amount = 0
            if not name or amount <= 0:
                continue
            merged[name] = merged.get(name, 0) + amount
    items = [
        {"name": name, "amount": amount}
        for name, amount in sorted(merged.items(), key=lambda item: (-item[1], item[0]))
    ]
    return {"items": items, "total": total}


def fetch_warehouse_expenses(
    shop_ids: list[str],
    api_key: str | None = None,
    *,
    target_day: date | None = None,
    allow_latest_fallback: bool = True,
) -> dict:
    """Fetch warehouse-related expenses from the Uzum expenses API.

    Returns aggregated totals for the requested shops:
    {items: [{name, amount}], total: int}
    """
    if not shop_ids or not api_key:
        return {}

    target_day = target_day or _today_app_tz()
    payments = _fetch_warehouse_expense_payments(shop_ids, api_key=api_key)
    filtered = _filter_warehouse_expense_payments_for_day(
        payments,
        target_day,
        allow_latest_fallback=allow_latest_fallback,
    )
    by_shop = _build_warehouse_expense_snapshots_for_shops(filtered, [str(shop_id) for shop_id in shop_ids])
    merged = _merge_warehouse_expense_payloads(list(by_shop.values()))
    print(
        f"[Expenses] day={target_day.isoformat()} payments={len(filtered)} "
        f"categories={len(merged.get('items', []))} total={merged.get('total', 0)}"
    )
    return merged


# ---------------------------------------------------------------------------
# Register debug-routes Blueprint (extracted into debug_routes.py)
# ---------------------------------------------------------------------------
import debug_routes as _debug_mod
_debug_mod.init_debug_routes(__import__("sys").modules[__name__])
app.register_blueprint(_debug_mod.debug_bp)

# ---------------------------------------------------------------------------
# Register auth-routes Blueprint (extracted into auth/routes.py)
# ---------------------------------------------------------------------------
import auth.routes as _auth_mod
_auth_mod.init_auth_routes(__import__("sys").modules[__name__])
app.register_blueprint(_auth_mod.auth_bp)

# ---------------------------------------------------------------------------
# Register admin-routes Blueprint (extracted into admin/routes.py)
# ---------------------------------------------------------------------------
import admin.routes as _admin_mod
import sys as _sys
_admin_mod.init_admin_routes(_sys.modules[__name__])
app.register_blueprint(_admin_mod.admin_bp)

# ---------------------------------------------------------------------------
# Register Payme-routes Blueprint (extracted into payments/routes.py)
# ---------------------------------------------------------------------------
import payments.routes as _payme_mod
app.register_blueprint(_payme_mod.payme_bp)

# ---------------------------------------------------------------------------
# Register warehouse-routes Blueprint (extracted into warehouse/routes.py)
# ---------------------------------------------------------------------------
import warehouse.routes as _warehouse_mod
app.register_blueprint(_warehouse_mod.warehouse_bp)

# ---------------------------------------------------------------------------
# Register products-routes Blueprint (extracted into products/routes.py)
# ---------------------------------------------------------------------------
import products.routes as _products_mod
_products_mod.init_products_routes(__import__("sys").modules[__name__])
app.register_blueprint(_products_mod.products_bp)

# ---------------------------------------------------------------------------
# Register finance-routes Blueprint (extracted into finance/routes.py)
# ---------------------------------------------------------------------------
import finance.routes as _finance_mod
_finance_mod.init_finance_routes(__import__("sys").modules[__name__])
app.register_blueprint(_finance_mod.finance_bp)

# ---------------------------------------------------------------------------
# Register pos-routes Blueprint (extracted into pos/routes.py)
# ---------------------------------------------------------------------------
import pos.routes as _pos_mod
_pos_mod.init_pos_routes(__import__("sys").modules[__name__])
app.register_blueprint(_pos_mod.pos_bp)

# ---------------------------------------------------------------------------
# Register telegram-routes Blueprint (extracted into telegram/routes.py)
# ---------------------------------------------------------------------------
import telegram.routes as _telegram_mod
_telegram_mod.init_telegram_routes(__import__("sys").modules[__name__])
app.register_blueprint(_telegram_mod.telegram_bp)


# ----------------------------
# Pages (new)
# ----------------------------
# ----------------------------
# Telegram bot config + login-by-code
# ----------------------------
_TELEGRAM_CONFIG_PATH = os.path.join(DATA_DIR, "telegram_config.json")

def _tg_config() -> dict:
    # Environment variables take priority. Falls back to on-disk JSON for
    # legacy/dev setups that update the token via the admin UI.
    env_token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    env_username = (os.environ.get("TELEGRAM_BOT_USERNAME") or "").strip()
    if env_token:
        return {"bot_token": env_token, "bot_username": env_username}
    try:
        with open(_TELEGRAM_CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}

def _tg_save_config(cfg: dict):
    with open(_TELEGRAM_CONFIG_PATH, "w") as f:
        json.dump(cfg, f)

# In-memory store: code -> {tg_id, tg_username, created_at}
import secrets, time as _time
def _tg_clean_expired():
    """Remove pending Telegram login requests older than 5 minutes."""
    cutoff = datetime.utcnow() - timedelta(minutes=5)
    with SessionLocal() as db:
        db.execute(delete(TelegramPending).where(TelegramPending.created_at < cutoff))
        db.commit()

def _tg_get(token: str) -> dict | None:
    """Get a pending Telegram login entry from DB."""
    with SessionLocal() as db:
        row = db.get(TelegramPending, token)
        if not row:
            return None
        return {
            "token": row.token, "type": row.type, "user_id": row.user_id,
            "tg_id": row.tg_id, "tg_username": row.tg_username,
            "confirmed": row.confirmed,
        }

def _tg_set(token: str, *, type: str = "code", user_id: int | None = None,
            tg_id: str | None = None, tg_username: str | None = None,
            confirmed: bool = False):
    """Create or update a pending Telegram login entry in DB."""
    with SessionLocal() as db:
        row = db.get(TelegramPending, token)
        if row:
            row.type = type
            if user_id is not None: row.user_id = user_id
            if tg_id is not None: row.tg_id = tg_id
            if tg_username is not None: row.tg_username = tg_username
            row.confirmed = confirmed
        else:
            db.add(TelegramPending(
                token=token, type=type, user_id=user_id,
                tg_id=tg_id, tg_username=tg_username, confirmed=confirmed,
            ))
        db.commit()

def _tg_confirm(token: str, **updates):
    """Mark a pending entry as confirmed, optionally updating fields."""
    with SessionLocal() as db:
        row = db.get(TelegramPending, token)
        if row:
            row.confirmed = True
            for k, v in updates.items():
                if hasattr(row, k):
                    setattr(row, k, v)
            db.commit()

def _tg_delete(token: str):
    """Remove a pending entry."""
    with SessionLocal() as db:
        db.execute(delete(TelegramPending).where(TelegramPending.token == token))
        db.commit()

def _start_tg_bot():
    """Run the Telegram bot in a background thread (long-polling)."""
    try:
        import telebot
        cfg = _tg_config()
        token = cfg.get("bot_token", "")
        if not token:
            return
        bot = telebot.TeleBot(token, threaded=False)

        @bot.message_handler(commands=["start"])
        def handle_start(msg):
            tg_id = str(msg.from_user.id)
            user = _get_db_user(tg_id)
            if user:
                # Already linked — show main menu
                bot.send_message(
                    msg.chat.id,
                    f"👋 С возвращением, *{user.username}*!\nВыберите действие:",
                    parse_mode="Markdown",
                    reply_markup=_main_menu(user.is_admin),
                )
            else:
                # Not linked yet — ask to share phone
                share_markup = telebot.types.ReplyKeyboardMarkup(one_time_keyboard=True, resize_keyboard=True)
                share_markup.add(telebot.types.KeyboardButton("📱 Поделиться номером", request_contact=True))
                bot.send_message(
                    msg.chat.id,
                    "👋 Добро пожаловать в *Uzum Warehouse*!\n\nПоделитесь номером телефона, чтобы привязать Telegram к аккаунту и входить одним нажатием.",
                    parse_mode="Markdown",
                    reply_markup=share_markup,
                )

        @bot.message_handler(content_types=["contact"])
        def handle_contact(msg):
            tg_id = str(msg.from_user.id)
            tg_username = msg.from_user.username or f"tg_{tg_id}"
            phone_raw = (msg.contact.phone_number or "").strip().lstrip("+")
            phone_e164 = "+" + phone_raw

            with SessionLocal() as db:
                # Check if already linked by telegram_id
                user = db.execute(select(User).where(User.telegram_id == tg_id)).scalar_one_or_none()
                if not user:
                    # Try finding by phone
                    user = db.execute(select(User).where(User.phone == phone_e164)).scalar_one_or_none()

                if user:
                    # Update telegram_id and phone if needed
                    user.telegram_id = tg_id
                    user.phone = phone_e164
                    db.commit()
                    user_id = user.id
                    is_admin = user.is_admin
                else:
                    # Auto-create account for new user
                    from werkzeug.security import generate_password_hash as _gph
                    import os as _os
                    base = tg_username
                    username = base
                    suffix = 1
                    while db.execute(select(User).where(User.username == username)).scalar_one_or_none():
                        username = f"{base}_{suffix}"
                        suffix += 1
                    user = User(
                        username=username,
                        password_hash=_gph(_os.urandom(32).hex()),
                        telegram_id=tg_id,
                        phone=phone_e164,
                        is_admin=False,
                    )
                    from core.subscriptions import _ensure_user_trial_started
                    _ensure_user_trial_started(db, user)
                    db.add(user)
                    db.commit()
                    db.refresh(user)
                    user_id = user.id
                    is_admin = False

                # Find and confirm any pending contact_link token for this phone
                pending = db.execute(
                    select(TelegramPending).where(
                        TelegramPending.type == "contact_link",
                        TelegramPending.tg_username == phone_raw,
                        TelegramPending.confirmed == False,
                    )
                ).scalars().first()
                if pending:
                    pending.confirmed = True
                    pending.user_id = user_id
                    pending.tg_id = tg_id
                    db.commit()

            bot.send_message(
                msg.chat.id,
                "✅ *Вход подтверждён!* Возвращайтесь на страницу входа — вы будете автоматически авторизованы.",
                parse_mode="Markdown",
                reply_markup=_main_menu(is_admin),
            )

        # ---- helpers used by shop commands ----
        def _main_menu(is_admin=False):
            """Persistent bottom keyboard."""
            markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
            markup.row(telebot.types.KeyboardButton("🏪 Мои магазины"))
            markup.row(telebot.types.KeyboardButton("➕ Добавить магазин"))
            markup.row(telebot.types.KeyboardButton("❓ Помощь"))
            return markup

        def _get_db_user(tg_id: str):
            with SessionLocal() as db:
                return db.execute(select(User).where(User.telegram_id == tg_id)).scalar_one_or_none()

        # ---- /help ----
        @bot.message_handler(commands=["help"])
        def handle_help(msg):
            tg_id = str(msg.from_user.id)
            user = _get_db_user(tg_id)
            bot.send_message(msg.chat.id,
                "📋 *Доступные команды:*\n\n"
                "🏪 *Мои магазины* — просмотр ваших магазинов\n"
                "➕ *Добавить магазин* — добавить магазин _(только админ)_\n"
                "/start — привязать номер телефона\n",
                parse_mode="Markdown",
                reply_markup=_main_menu(user.is_admin if user else False))

        # ---- /testsales — manually fire the hourly check for the requesting user only ----
        @bot.message_handler(commands=["testsales"])
        def handle_testsales(msg):
            tg_id = str(msg.from_user.id)
            user = _get_db_user(tg_id)
            if not user:
                bot.send_message(msg.chat.id, "⚠️ Сначала привяжите аккаунт через /start.")
                return
            bot.send_message(msg.chat.id, "⏳ Проверяю продажи ваших магазинов за последний час...")
            import threading as _thr
            def _run():
                try:
                    total = _do_hourly_sales_check(target_user_id=user.id, target_tg_id=tg_id)
                    if total > 0:
                        bot.send_message(msg.chat.id, f"✅ Готово — отчёт отправлен выше только по вашим магазинам ({total} шт. за последний час, в карточке есть и столбец с 00:00).")
                    else:
                        bot.send_message(msg.chat.id, "ℹ️ По вашим магазинам продаж за последний час не найдено.")
                except Exception as _e:
                    bot.send_message(msg.chat.id, f"❌ Ошибка: {_e}")
            _thr.Thread(target=_run, daemon=True).start()

        # ---- menu button text handlers ----
        @bot.message_handler(func=lambda m: m.text == "🏪 Мои магазины")
        def btn_my_shops(msg):
            msg.text = "/shops"
            handle_shops(msg)

        # ---- multi-step add shop flow ----
        def _addshop_ask_name(msg, user_id, is_admin, uzum_id):
            name = (msg.text or "").strip()
            if name in ("—", "-", ""):
                name = ""
            with SessionLocal() as db:
                existing = db.execute(select(Shop).where(Shop.uzum_id == uzum_id)).scalar_one_or_none()
                if existing:
                    if existing.owner_id and existing.owner_id != user_id:
                        bot.send_message(msg.chat.id,
                            f"❌ Магазин `{uzum_id}` уже привязан к другому аккаунту.",
                            parse_mode="Markdown", reply_markup=_main_menu(is_admin))
                        return
                    if existing.owner_id == user_id:
                        bot.send_message(msg.chat.id,
                            f"ℹ️ Магазин *{existing.name or uzum_id}* уже добавлен.",
                            parse_mode="Markdown", reply_markup=_main_menu(is_admin))
                        return
                    # Unassigned shop — claim it
                    existing.owner_id = user_id
                    if name:
                        existing.name = name
                    db.commit()
                    bot.send_message(msg.chat.id,
                        f"✅ Магазин *{existing.name or uzum_id}* привязан к вашему аккаунту!",
                        parse_mode="Markdown", reply_markup=_main_menu(is_admin))
                else:
                    owner = None if is_admin else user_id
                    shop = Shop(uzum_id=uzum_id, name=name or None, owner_id=owner)
                    db.add(shop)
                    db.commit()
                    db.refresh(shop)
                    new_shop_pk = shop.id
                    assigned = "добавлен (не привязан к продавцу)" if is_admin else "добавлен и привязан к вашему аккаунту"
                    bot.send_message(msg.chat.id,
                        f"✅ Магазин *{name or uzum_id}* {assigned}!\n\n"
                        f"💰 Запускаю загрузку продаж и истории финансов (с 2022 г.)...",
                        parse_mode="Markdown", reply_markup=_main_menu(is_admin))
                    def _seed_finance(uzum_id=uzum_id, shop_pk=new_shop_pk, chat_id=msg.chat.id, shop_name=name or uzum_id):
                        try:
                            res = _sync_finance_for_shop(uzum_id, shop_pk)
                            bot.send_message(chat_id,
                                f"✅ Продажи загружены для *{shop_name}*! SKU обновлено: {res['updated']}",
                                parse_mode="Markdown")
                        except Exception as _e:
                            bot.send_message(chat_id,
                                f"⚠️ Не удалось загрузить продажи для *{shop_name}*: {_e}",
                                parse_mode="Markdown")
                    import threading as _threading
                    _threading.Thread(target=_seed_finance, daemon=True).start()

        def _addshop_ask_id(msg, user_id, is_admin):
            if msg.text and msg.text.startswith("/"):
                return  # user cancelled with another command
            uzum_id = (msg.text or "").strip()
            if not uzum_id:
                bot.send_message(msg.chat.id, "❌ ID не может быть пустым. Попробуйте снова:")
                bot.register_next_step_handler(msg, _addshop_ask_id, user_id=user_id, is_admin=is_admin)
                return
            bot.send_message(msg.chat.id,
                f"Введите *название* магазина `{uzum_id}` (или отправьте `—` чтобы пропустить):",
                parse_mode="Markdown")
            bot.register_next_step_handler(msg, _addshop_ask_name,
                user_id=user_id, is_admin=is_admin, uzum_id=uzum_id)

        @bot.message_handler(func=lambda m: m.text == "➕ Добавить магазин")
        def btn_add_shop(msg):
            tg_id = str(msg.from_user.id)
            user = _get_db_user(tg_id)
            if not user:
                bot.send_message(msg.chat.id, "⚠️ Аккаунт не привязан. Нажмите /start.")
                return
            bot.send_message(msg.chat.id,
                "Введите *ID магазина* (Uzum Shop ID):\n\n"
                "_(Найти его можно в личном кабинете продавца Uzum → URL страницы магазина)_",
                parse_mode="Markdown")
            bot.register_next_step_handler(msg, _addshop_ask_id,
                user_id=user.id, is_admin=user.is_admin)

        @bot.message_handler(func=lambda m: m.text == "❓ Помощь")
        def btn_help(msg):
            handle_help(msg)

        # ---- /shops ----
        @bot.message_handler(commands=["shops", "myshops"])
        def handle_shops(msg):
            tg_id = str(msg.from_user.id)
            with SessionLocal() as db:
                user = db.execute(select(User).where(User.telegram_id == tg_id)).scalar_one_or_none()
                if not user:
                    bot.send_message(msg.chat.id,
                        "⚠️ Аккаунт не привязан. Нажмите /start и поделитесь номером телефона.")
                    return
                if user.is_admin:
                    shops = db.execute(select(Shop).where(
                        (Shop.owner_id == user.id) | (Shop.owner_id == None))).scalars().all()
                else:
                    shops = db.execute(select(Shop).where(Shop.owner_id == user.id)).scalars().all()

                if not shops:
                    bot.send_message(msg.chat.id, "У вас нет привязанных магазинов.")
                    return

                markup = telebot.types.InlineKeyboardMarkup(row_width=1)
                for s in shops:
                    total = db.execute(
                        select(func.sum(Variant.sales_30d_finance))
                        .join(ProductGroup, Variant.group_id == ProductGroup.id)
                        .where(ProductGroup.shop_id == s.id)
                    ).scalar() or 0
                    label = f"🏪 {s.name or s.uzum_id}  •  {total} шт/30д"
                    markup.add(telebot.types.InlineKeyboardButton(label, callback_data=f"shop_menu:{s.id}"))

                # Send persistent menu first, then inline shop list
                bot.send_message(msg.chat.id, "🏪 *Ваши магазины:*", parse_mode="Markdown",
                                 reply_markup=_main_menu(user.is_admin))
                bot.send_message(msg.chat.id, "Выберите магазин:", reply_markup=markup)

        # ---- /addshop command (shortcut) ----
        @bot.message_handler(commands=["addshop"])
        def handle_addshop(msg):
            tg_id = str(msg.from_user.id)
            user = _get_db_user(tg_id)
            if not user:
                bot.send_message(msg.chat.id, "⚠️ Аккаунт не привязан. Нажмите /start.")
                return

            parts = msg.text.split(maxsplit=2)
            if len(parts) < 2:
                # No ID given — start interactive flow
                bot.send_message(msg.chat.id,
                    "Введите *ID магазина* (Uzum Shop ID):", parse_mode="Markdown")
                bot.register_next_step_handler(msg, _addshop_ask_id,
                    user_id=user.id, is_admin=user.is_admin)
                return

            # ID provided inline — go straight to name step
            uzum_id = parts[1].strip()
            name = parts[2].strip() if len(parts) > 2 else ""

            # Reuse name handler with a fake message carrying the name
            class _FakeMsg:
                text = name
            _addshop_ask_name(_FakeMsg(), user_id=user.id, is_admin=user.is_admin, uzum_id=uzum_id)

        # ---- shop menu callback ----
        @bot.callback_query_handler(func=lambda call: call.data.startswith("shop_menu:"))
        def handle_shop_menu(call):
            shop_id = int(call.data.split(":", 1)[1])
            tg_id = str(call.from_user.id)
            with SessionLocal() as db:
                user = db.execute(select(User).where(User.telegram_id == tg_id)).scalar_one_or_none()
                shop = db.get(Shop, shop_id)
                if not user or not shop:
                    bot.answer_callback_query(call.id, "Не найдено.")
                    return
                if not user.is_admin and shop.owner_id != user.id:
                    bot.answer_callback_query(call.id, "❌ Нет доступа.")
                    return

                markup = telebot.types.InlineKeyboardMarkup(row_width=2)
                markup.add(
                    telebot.types.InlineKeyboardButton("📊 Топ продаж", callback_data=f"shop_sales:{shop_id}"),
                    telebot.types.InlineKeyboardButton("📦 Остатки", callback_data=f"shop_stock:{shop_id}"),
                )
                markup.add(
                    telebot.types.InlineKeyboardButton("🔄 Синх. товары", callback_data=f"shop_sync_products:{shop_id}"),
                    telebot.types.InlineKeyboardButton("💰 Синх. продажи", callback_data=f"shop_sync_finance:{shop_id}"),
                )
                markup.add(telebot.types.InlineKeyboardButton("🔄🔄 Синх. всё", callback_data=f"shop_sync_all:{shop_id}"))
                markup.add(telebot.types.InlineKeyboardButton("⬅️ К списку магазинов", callback_data="back_shops"))
                bot.answer_callback_query(call.id)
                bot.edit_message_text(
                    f"🏪 *{shop.name or shop.uzum_id}*\nID: `{shop.uzum_id}`\n\nВыберите действие:",
                    call.message.chat.id, call.message.message_id,
                    parse_mode="Markdown", reply_markup=markup)

        # ---- back to shops list ----
        @bot.callback_query_handler(func=lambda call: call.data == "back_shops")
        def handle_back_shops(call):
            tg_id = str(call.from_user.id)
            with SessionLocal() as db:
                user = db.execute(select(User).where(User.telegram_id == tg_id)).scalar_one_or_none()
                if not user:
                    bot.answer_callback_query(call.id)
                    return
                if user.is_admin:
                    shops = db.execute(select(Shop).where(
                        (Shop.owner_id == user.id) | (Shop.owner_id == None))).scalars().all()
                else:
                    shops = db.execute(select(Shop).where(Shop.owner_id == user.id)).scalars().all()

                markup = telebot.types.InlineKeyboardMarkup(row_width=1)
                for s in shops:
                    total = db.execute(
                        select(func.sum(Variant.sales_30d_finance))
                        .join(ProductGroup, Variant.group_id == ProductGroup.id)
                        .where(ProductGroup.shop_id == s.id)
                    ).scalar() or 0
                    label = f"🏪 {s.name or s.uzum_id}  •  {total} шт/30д"
                    markup.add(telebot.types.InlineKeyboardButton(label, callback_data=f"shop_menu:{s.id}"))

                bot.answer_callback_query(call.id)
                bot.edit_message_text("🏪 *Ваши магазины:*", call.message.chat.id, call.message.message_id,
                                      parse_mode="Markdown", reply_markup=markup)

        # ---- shop_sales callback ----
        @bot.callback_query_handler(func=lambda call: call.data.startswith("shop_sales:"))
        def handle_shop_sales(call):
            shop_id = int(call.data.split(":", 1)[1])
            tg_id = str(call.from_user.id)
            with SessionLocal() as db:
                user = db.execute(select(User).where(User.telegram_id == tg_id)).scalar_one_or_none()
                shop = db.get(Shop, shop_id)
                if not user or not shop:
                    bot.answer_callback_query(call.id, "Не найдено.")
                    return
                if not user.is_admin and shop.owner_id != user.id:
                    bot.answer_callback_query(call.id, "❌ Нет доступа.")
                    return

                rows = db.execute(
                    select(Variant, ProductGroup)
                    .join(ProductGroup, Variant.group_id == ProductGroup.id)
                    .where(ProductGroup.shop_id == shop_id)
                    .where(Variant.sales_30d_finance > 0)
                    .order_by(Variant.sales_30d_finance.desc())
                    .limit(15)
                ).all()

                if not rows:
                    bot.answer_callback_query(call.id, "Нет данных о продажах.")
                    return

                lines = [f"📊 *Топ продаж — {shop.name or shop.uzum_id}* (30 дней)\n"]
                for i, (v, g) in enumerate(rows, 1):
                    label = v.sku or "—"
                    if v.color:
                        label += f" / {v.color}"
                    if v.size:
                        label += f" / {v.size}"
                    lines.append(f"{i}\\. {label}: *{v.sales_30d_finance}* шт.")

                markup = telebot.types.InlineKeyboardMarkup()
                markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data=f"shop_menu:{shop_id}"))
                bot.answer_callback_query(call.id)
                bot.send_message(call.message.chat.id, "\n".join(lines),
                                 parse_mode="MarkdownV2", reply_markup=markup)

        # ---- shared sync helper ----
        def _bot_check_shop_access(call, shop_id):
            """Returns (user, shop) or sends error and returns (None, None)."""
            tg_id = str(call.from_user.id)
            with SessionLocal() as db:
                user = db.execute(select(User).where(User.telegram_id == tg_id)).scalar_one_or_none()
                shop = db.get(Shop, shop_id)
            if not user or not shop:
                bot.answer_callback_query(call.id, "Не найдено.")
                return None, None
            if not user.is_admin and shop.owner_id != user.id:
                bot.answer_callback_query(call.id, "❌ Нет доступа.")
                return None, None
            return user, shop

        def _bot_run_sync(chat_id, shop_name, task_fn):
            """Run task_fn in background thread, send result to chat_id."""
            def _run():
                try:
                    result = task_fn()
                    bot.send_message(chat_id, result, parse_mode="Markdown")
                except Exception as e:
                    bot.send_message(chat_id, f"❌ Ошибка: {e}")
            _threading.Thread(target=_run, daemon=True).start()

        # ---- stock overview callback ----
        @bot.callback_query_handler(func=lambda call: call.data.startswith("shop_stock:"))
        def handle_shop_stock(call):
            shop_id = int(call.data.split(":", 1)[1])
            user, shop = _bot_check_shop_access(call, shop_id)
            if not shop:
                return
            bot.answer_callback_query(call.id, "⏳ Генерирую отчёт...")
            with SessionLocal() as db:
                db_rows = db.execute(
                    select(Variant, ProductGroup)
                    .join(ProductGroup, Variant.group_id == ProductGroup.id)
                    .where(ProductGroup.shop_id == shop_id)
                    .where(
                        (ProductGroup.is_archived == False) |
                        (ProductGroup.is_archived == None)
                    )
                    .order_by(Variant.uzum_quantity.asc())
                ).all()

                if not db_rows:
                    bot.send_message(call.message.chat.id, "📦 Нет данных об остатках.")
                    return

                rows_data = [
                    {
                        "group_name": g.name,
                        "sku":        v.sku or "—",
                        "color":      v.color,
                        "size":       v.size,
                        "uzum_qty":   v.uzum_quantity or 0,
                        "wh_qty":     v.warehouse_quantity or 0,
                        "sales_30d":  v.sales_30d_finance or 0,
                    }
                    for v, g in db_rows
                ]

            markup = telebot.types.InlineKeyboardMarkup()
            markup.add(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data=f"shop_menu:{shop_id}"))

            shop_name = shop.name or shop.uzum_id
            try:
                img_bytes = _render_stock_image(rows_data, shop_name)
                bot.send_photo(call.message.chat.id, __import__("io").BytesIO(img_bytes),
                               reply_markup=markup)
            except Exception as _e:
                print(f"[Bot/stock] image render failed: {_e}")
                # text fallback
                lines = [f"📦 *Остатки — {shop_name}*\n"]
                for r in rows_data:
                    label = r["sku"]
                    if r.get("color"): label += f" / {r['color']}"
                    if r.get("size"):  label += f" / {r['size']}"
                    lines.append(f"• {label}: Uzum {r['uzum_qty']} | Склад {r['wh_qty']}")
                bot.send_message(call.message.chat.id, "\n".join(lines),
                                 parse_mode="Markdown", reply_markup=markup)

        # ---- sync products callback ----
        @bot.callback_query_handler(func=lambda call: call.data.startswith("shop_sync_products:"))
        def handle_sync_products(call):
            shop_id = int(call.data.split(":", 1)[1])
            user, shop = _bot_check_shop_access(call, shop_id)
            if not shop:
                return
            uzum_id = shop.uzum_id
            shop_name = shop.name or shop.uzum_id
            bot.answer_callback_query(call.id, "🔄 Синхронизация товаров запущена...")
            bot.send_message(call.message.chat.id,
                f"🔄 Загрузка товаров для *{shop_name}*...", parse_mode="Markdown")
            def _task():
                res = _sync_products_for_shop(uzum_id)
                api_total = res.get('total_api_elements') or '?'
                return (f"✅ *{shop_name}* — товары обновлены!\n"
                        f"Страниц: {res['pages_synced']}, получено: {res['fetched']} SKU\n"
                        f"Всего в Uzum API: {api_total}")
            _bot_run_sync(call.message.chat.id, shop_name, _task)

        # ---- sync finance callback ----
        @bot.callback_query_handler(func=lambda call: call.data.startswith("shop_sync_finance:"))
        def handle_sync_finance(call):
            shop_id = int(call.data.split(":", 1)[1])
            user, shop = _bot_check_shop_access(call, shop_id)
            if not shop:
                return
            uzum_id = shop.uzum_id
            shop_name = shop.name or shop.uzum_id
            bot.answer_callback_query(call.id, "💰 Синхронизация продаж запущена...")
            bot.send_message(call.message.chat.id,
                f"💰 Загрузка продаж (30д) для *{shop_name}*...", parse_mode="Markdown")
            def _task():
                res = _sync_finance_for_shop(uzum_id, shop_id)
                return f"✅ *{shop_name}* — продажи обновлены! SKU обновлено: {res['updated']}"
            _bot_run_sync(call.message.chat.id, shop_name, _task)

        # ---- sync all callback (auto-discovers all shops) ----
        @bot.callback_query_handler(func=lambda call: call.data.startswith("shop_sync_all:"))
        def handle_sync_all(call):
            bot.answer_callback_query(call.id, "🔄🔄 Полная синхронизация всех магазинов...")
            bot.send_message(call.message.chat.id,
                "🔄🔄 Ищем все магазины и синхронизируем...\n_(может занять 1-3 минуты)_",
                parse_mode="Markdown")
            def _task():
                res = _sync_all_seller_shops()
                shops = res.get("shops_found") or []
                total_sku = res.get("fetched", 0)
                lines = [f"✅ Полная синхронизация завершена!",
                         f"Магазины: {', '.join(shops)}",
                         f"Всего SKU загружено: *{total_sku}*"]
                per_shop = res.get("per_shop") or {}
                for sid, sr in per_shop.items():
                    if "error" in sr:
                        lines.append(f"  ❌ {sid}: {sr['error']}")
                    else:
                        lines.append(f"  ✅ {sid}: {sr.get('fetched', 0)} SKU, {sr.get('pages_synced', 0)} стр.")
                return "\n".join(lines)
            _bot_run_sync(call.message.chat.id, "все магазины", _task)

        @bot.callback_query_handler(func=lambda call: call.data.startswith("approve:") or call.data.startswith("deny:"))
        def handle_approval_callback(call):
            parts = call.data.split(":", 1)
            action, token = parts[0], parts[1] if len(parts) > 1 else ""
            entry = _tg_get(token)
            if not entry or entry.get("type") != "approval":
                bot.answer_callback_query(call.id, "⏱ Запрос устарел или уже обработан.")
                return
            if action == "approve":
                _tg_confirm(token)
                bot.answer_callback_query(call.id, "✅ Вход подтверждён!")
                try:
                    bot.edit_message_text("✅ Вход подтверждён! Возвращайтесь в браузер.", call.message.chat.id, call.message.message_id)
                except Exception:
                    pass
            else:
                _tg_delete(token)
                bot.answer_callback_query(call.id, "❌ Вход отклонён.")
                try:
                    bot.edit_message_text("❌ Вход отклонён.", call.message.chat.id, call.message.message_id)
                except Exception:
                    pass

        @bot.message_handler(func=lambda m: True)
        def handle_code(msg):
            code = (msg.text or "").strip().upper()
            tg_id = str(msg.from_user.id)
            tg_username = msg.from_user.username or f"tg_{tg_id}"
            _tg_clean_expired()
            entry = _tg_get(code)
            if entry and entry.get("type") != "approval":
                _tg_confirm(code, tg_id=tg_id, tg_username=tg_username)
                bot.send_message(msg.chat.id, "✅ Вы успешно вошли в систему!")
            else:
                bot.send_message(msg.chat.id, "Отправьте код, который отображается на странице входа.")

        bot.infinity_polling(timeout=30, long_polling_timeout=20)
    except Exception as e:
        print(f"[TG Bot] Error: {e}")

import threading as _threading
# Bot is started inside background/startup.py with a cross-process file lock
# to prevent 409 conflicts when multiple gunicorn workers exist.


# ----------------------------
# Hourly sales notifications
# ----------------------------

def _base_sku(skus: list[str]) -> str:
    """Return just the model/code component of the SKU (second dash-separated
    part). e.g. 'BLUMMY-KOMPLEKT6-БЕЛЫЙ' -> 'KOMPLEKT6'. Falls back to the
    whole token when the SKU doesn't follow the BRAND-MODEL-VARIANT shape.
    """
    clean = [s.strip() for s in skus if s and s.strip()]
    if not clean:
        return "—"
    parts = clean[0].split("-")
    if len(parts) >= 2 and parts[1]:
        return parts[1]
    return clean[0]


def _fmt_sum(v: int) -> str:
    """Format large integer as e.g. '1 234 500'."""
    return f"{v:,}".replace(",", " ")


def _render_sales_image(
    by_shop: dict,
    hour_label: str,
    *,
    period_qty_label: str = "За час",
    day_qty_label: str = "С 00:00",
    show_period_qty_column: bool = True,
) -> bytes:
    """Render sales report as a light-theme PNG.

    Each item in by_shop[shop_name] must have:
      group, base_sku, hour_qty, day_qty, revenue (int sums), payout (int sums), profit (int sums), margin (float %)
    by_shop['__totals__'][shop_name] = {total_hour, total_day, total_revenue, total_payout, total_profit, avg_margin}
    """
    from PIL import Image, ImageDraw, ImageFont
    import io as _io

    def _font(size, bold=False):
        candidates = (
            ["arialbd.ttf", "Arial Bold.ttf",
             "DejaVuSans-Bold.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"] if bold else
            ["arial.ttf", "Arial.ttf",
             "DejaVuSans.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
        )
        for name in candidates:
            try:
                return ImageFont.truetype(name, size)
            except Exception:
                pass
        return ImageFont.load_default()

    # Supersample the whole canvas at 2x so Telegram's photo-compression has
    # more detail to preserve. Fonts/layout/offsets all multiply through SCALE.
    SCALE = 2

    font_title  = _font(18 * SCALE, bold=True)
    font_sub    = _font(11 * SCALE)
    font_header = _font(11 * SCALE, bold=True)
    font_body   = _font(14 * SCALE)
    # Smaller font used only in the Описание column so a full 90-char
    # Uzum title can render on a single line without wrapping.
    font_body_desc = _font(10 * SCALE)
    font_small  = _font(10 * SCALE)
    font_bold   = _font(14 * SCALE, bold=True)
    font_shop   = _font(13 * SCALE, bold=True)
    font_exp_h  = _font(12 * SCALE, bold=True)

    # ── Modern palette ───────────────────────────────────────────────────
    BG          = (240, 242, 245)
    CARD        = (255, 255, 255)
    TITLE_BG1   = (55,  71, 133)   # gradient left
    TITLE_BG2   = (88, 120, 220)   # gradient right
    TITLE_FG    = (255, 255, 255)
    SUBTITLE_FG = (190, 200, 230)
    HEADER_BG   = (246, 247, 251)
    HEADER_FG   = (100, 110, 130)
    SHOP_BG     = (55,  71, 133)
    SHOP_FG     = (255, 255, 255)
    ROW_ODD     = (255, 255, 255)
    ROW_EVEN    = (249, 250, 253)
    BODY_FG     = (40,  44,  52)
    MUTED       = (140, 148, 165)
    DIVIDER     = (230, 232, 238)
    GREEN       = (16, 163,  90)
    BLUE        = (56, 114, 247)
    REVENUE_COL = (56, 114, 247)
    PAYOUT_COL  = (94, 104, 184)
    PROFIT_COL  = (16, 163,  90)
    MARGIN_COL  = (136,  84, 208)
    TOTAL_BG    = (238, 242, 255)
    TOTAL_ACCENT= (55,  71, 133)
    EXP_HEADER  = (245, 240, 252)
    EXP_RED     = (230,  67,  80)
    EXP_TOTAL   = (55,  71, 133)
    FOOTER_FG   = (180, 185, 195)
    SHADOW      = (215, 218, 225)

    # ── Columns ──────────────────────────────────────────────────────────
    col_specs = [
        ("Описание", 440, "left"),
        ("SKU", 118, "left"),
    ]
    if show_period_qty_column:
        col_specs.append((period_qty_label, 62, "center"))
    col_specs.extend([
        (day_qty_label, 72, "center"),
        ("Выручка, сум", 118, "right"),
        ("К выводу", 114, "right"),
        ("Прибыль, сум", 118, "right"),
        ("Маржа, %", 76, "center"),
    ])
    COL_LABELS = [label for label, _, _ in col_specs]
    COL_WIDTHS = [width * SCALE for _, width, _ in col_specs]
    COL_ALIGNS = [align for _, _, align in col_specs]

    MARGIN  = 16 * SCALE   # outer margin
    PAD_X   = 14 * SCALE
    PAD_Y   = 10 * SCALE
    ROW_H   = 34 * SCALE
    HDR_H   = 30 * SCALE   # column header row
    SHOP_H  = 36 * SCALE
    TITLE_H = 62 * SCALE
    SUMM_H  = 36 * SCALE
    FOOTER_H = 30 * SCALE
    CARD_R  = 14 * SCALE
    SVCLINE_H = 28 * SCALE
    SVC_HEADER_H = 32 * SCALE

    card_w  = sum(COL_WIDTHS) + 2 * PAD_X
    total_w = card_w + 2 * MARGIN

    sections = [(s, v) for s, v in by_shop.items() if s not in ("__totals__", "__expenses__")]
    total_rows = sum(len(r) for _, r in sections)

    # ── Description wrap (Uzum titles run up to 90 chars) ────────────
    # Word-wrap the name into the Описание column; break overlong tokens
    # by character so a single long word can't overflow the cell.
    def _text_w(t: str, f) -> float:
        try:    return f.getlength(t)
        except: return len(t) * 7

    def _wrap_to_width(text: str, f, max_w: int) -> list[str]:
        if not text:
            return [""]
        out: list[str] = []
        cur = ""
        for token in text.split(" "):
            if _text_w(token, f) > max_w:
                if cur:
                    out.append(cur); cur = ""
                buf = ""
                for ch in token:
                    if _text_w(buf + ch, f) <= max_w:
                        buf += ch
                    else:
                        out.append(buf); buf = ch
                cur = buf
                continue
            candidate = (cur + " " + token) if cur else token
            if _text_w(candidate, f) <= max_w:
                cur = candidate
            else:
                if cur: out.append(cur)
                cur = token
        if cur: out.append(cur)
        return out or [""]

    desc_usable_w = COL_WIDTHS[0] - 2 * PAD_X
    LINE_H_NAME   = 14 * SCALE
    MAX_NAME_CHARS = 90
    row_name_lines: list[list[list[str]]] = []
    row_heights: list[list[int]] = []
    for _sname, _rows in sections:
        _lines_shop: list[list[str]] = []
        _heights_shop: list[int] = []
        for _it in _rows:
            _nm = (_it.get("group") or "—")
            if len(_nm) > MAX_NAME_CHARS:
                _nm = _nm[:MAX_NAME_CHARS - 1] + "…"
            _wrapped = _wrap_to_width(_nm, font_body_desc, desc_usable_w)
            _lines_shop.append(_wrapped)
            _heights_shop.append(max(ROW_H, len(_wrapped) * LINE_H_NAME + PAD_Y))
        row_name_lines.append(_lines_shop)
        row_heights.append(_heights_shop)
    total_rows_h = sum(sum(hs) for hs in row_heights)

    expenses = by_shop.get("__expenses__", {})
    wh_exp_lines = []
    if expenses and expenses.get("items"):
        for ei in expenses["items"]:
            wh_exp_lines.append((ei["name"], ei["amount"]))
        if len(wh_exp_lines) > 1:
            wh_exp_lines.append(("Итого складские расходы", expenses.get("total", 0)))
    WH_BLOCK_H = (SVC_HEADER_H + len(wh_exp_lines) * SVCLINE_H + PAD_Y + 6 * SCALE) if wh_exp_lines else 0

    # Phase 3: "Возврат Денег" income bottom line (expenses_ledger op_type='Возврат').
    # Rendered as a standalone single-line card below the expenses block when > 0.
    # `refunds_income` is carried in the `__expenses__` payload but MUST NOT be
    # in `total` (that's outflow only) — see `read_daily_expense_breakdown`.
    refunds_income = int(expenses.get("refunds_income", 0) or 0) if expenses else 0
    REFUND_BLOCK_H = (SVCLINE_H + PAD_Y + 6 * SCALE) if refunds_income > 0 else 0

    # Grand total across all shops (show only if >1 shop)
    all_totals = by_shop.get("__totals__", {})
    g_hour = g_day = g_profit = g_revenue = g_payout = 0
    for st in all_totals.values():
        g_hour   += st.get("total_hour", 0)
        g_day    += st.get("total_day", 0)
        g_profit += st.get("total_profit", 0)
        g_revenue += st.get("total_revenue", 0)
        g_payout += st.get("total_payout", 0)
    g_avg_margin = round(g_profit / g_revenue * 100, 1) if g_revenue > 0 else 0.0
    show_grand = len(sections) > 1
    GRAND_H = (SUMM_H + PAD_Y * 2) if show_grand else 0

    total_h = (MARGIN + TITLE_H
               + len(sections) * (SHOP_H + HDR_H + SUMM_H + PAD_Y * 2)
               + total_rows_h
               + GRAND_H
               + WH_BLOCK_H
               + REFUND_BLOCK_H
               + FOOTER_H + MARGIN * 2)

    img  = Image.new("RGB", (total_w, total_h), BG)
    draw = ImageDraw.Draw(img)

    def tw(t, f):
        try:    return f.getlength(t)
        except: return len(t) * 7

    def draw_cell(x, y, w, h, text, font, color, align="left", bg=None):
        if bg:
            draw.rectangle([x, y, x + w - 1, y + h - 1], fill=bg)
        if align == "center":
            tx = x + (w - tw(text, font)) / 2
        elif align == "right":
            tx = x + w - tw(text, font) - PAD_X
        else:
            tx = x + PAD_X
        ty = y + (h - 13 * SCALE) / 2
        draw.text((tx, ty), text, font=font, fill=color)

    def draw_rounded_card(x, y, w, h, r=CARD_R, fill=CARD):
        # Shadow (offsets scale with SCALE so the drop shadow stays proportional).
        draw.rounded_rectangle([x + 2 * SCALE, y + 2 * SCALE, x + w + 1 * SCALE, y + h + 1 * SCALE], radius=r, fill=SHADOW)
        draw.rounded_rectangle([x, y, x + w - 1, y + h - 1], radius=r, fill=fill)

    def draw_gradient_rect(x, y, w, h, c1, c2, r=0):
        """Horizontal gradient from c1 to c2."""
        for i in range(w):
            ratio = i / max(w - 1, 1)
            c = tuple(int(c1[j] + (c2[j] - c1[j]) * ratio) for j in range(3))
            draw.line([(x + i, y), (x + i, y + h - 1)], fill=c)
        if r > 0:
            # Mask corners with BG
            mask = Image.new("L", (w, h), 255)
            mask_d = ImageDraw.Draw(mask)
            mask_d.rounded_rectangle([0, 0, w - 1, h - 1], radius=r, fill=255)
            # Invert: draw BG where mask is 0
            for corner in [(0,0,r,r), (w-r,0,w,r), (0,h-r,r,h), (w-r,h-r,w,h)]:
                for px in range(corner[0], corner[2]):
                    for py in range(corner[1], corner[3]):
                        if mask.getpixel((px, py)) == 255:
                            pass  # keep
                        # skip complex masking for performance

    cx_base = MARGIN  # left edge of card content

    # ── Title bar (gradient) ─────────────────────────────────────────────
    ty = MARGIN
    draw_gradient_rect(0, 0, total_w, TITLE_H + MARGIN, TITLE_BG1, TITLE_BG2)
    draw.text((MARGIN + PAD_X, ty + 12 * SCALE), "Продажи — статистика", font=font_title, fill=TITLE_FG)
    draw.text((MARGIN + PAD_X, ty + 36 * SCALE), hour_label, font=font_sub, fill=SUBTITLE_FG)

    cy = TITLE_H + MARGIN + PAD_Y

    for si, (shop_name, rows) in enumerate(sections):
        totals = by_shop.get("__totals__", {}).get(shop_name, {})

        # Card background for the whole shop section
        card_h = SHOP_H + HDR_H + sum(row_heights[si]) + SUMM_H + 2
        draw_rounded_card(cx_base, cy, card_w, card_h)

        # Shop header bar (dark accent)
        draw.rounded_rectangle(
            [cx_base, cy, cx_base + card_w - 1, cy + SHOP_H - 1],
            radius=CARD_R, fill=SHOP_BG)
        # Flatten bottom corners
        draw.rectangle([cx_base, cy + SHOP_H // 2, cx_base + card_w - 1, cy + SHOP_H - 1], fill=SHOP_BG)
        draw.text((cx_base + PAD_X + 4 * SCALE, cy + 10 * SCALE), shop_name, font=font_shop, fill=SHOP_FG)
        cy += SHOP_H

        # Column headers
        cx = cx_base
        for i, (lbl, w) in enumerate(zip(COL_LABELS, COL_WIDTHS)):
            draw_cell(cx, cy, w, HDR_H, lbl, font_header, HEADER_FG,
                      align=COL_ALIGNS[i], bg=HEADER_BG)
            cx += w
        draw.line([(cx_base, cy + HDR_H - 1), (cx_base + card_w, cy + HDR_H - 1)], fill=DIVIDER)
        cy += HDR_H

        # Data rows
        for ri, it in enumerate(rows):
            row_bg = ROW_ODD if ri % 2 == 0 else ROW_EVEN
            row_h  = row_heights[si][ri]
            wrapped_name = row_name_lines[si][ri]
            sku  = (it.get("base_sku") or "—")
            if len(sku) > 18: sku = sku[:16] + "…"
            # Leave column 0 blank here — multi-line name is drawn afterward
            # over the cell background so vertical centering works correctly.
            cells = [
                "",
                sku,
            ]
            colors = [BODY_FG, MUTED]
            if show_period_qty_column:
                cells.append(f"+{it['hour_qty']}")
                colors.append(GREEN)
            cells.extend([
                str(it["day_qty"]),
                _fmt_sum(int(it.get("revenue", 0))),
                _fmt_sum(int(it.get("payout", 0))),
                _fmt_sum(int(it.get("profit", 0))),
                f"{it.get('margin', 0):.1f}%",
            ])
            colors.extend([BLUE, REVENUE_COL, PAYOUT_COL, PROFIT_COL, MARGIN_COL])
            cx = cx_base
            for i, (cell, w) in enumerate(zip(cells, COL_WIDTHS)):
                draw_cell(cx, cy, w, row_h, cell, font_body, colors[i],
                          align=COL_ALIGNS[i], bg=row_bg)
                cx += w
            # Multi-line description, vertically centered within the row.
            total_text_h = len(wrapped_name) * LINE_H_NAME
            ty0 = cy + (row_h - total_text_h) / 2
            for li, ln in enumerate(wrapped_name):
                draw.text((cx_base + PAD_X, ty0 + li * LINE_H_NAME),
                          ln, font=font_body_desc, fill=BODY_FG)
            draw.line([(cx_base + PAD_X, cy + row_h - 1),
                       (cx_base + card_w - PAD_X, cy + row_h - 1)], fill=DIVIDER)
            cy += row_h

        # ── ИТОГО row ──────────────────────────────────────────────────
        if totals:
            draw.rectangle([cx_base, cy, cx_base + card_w - 1, cy + SUMM_H - 1], fill=TOTAL_BG)
            # Round bottom corners
            draw.rounded_rectangle(
                [cx_base, cy, cx_base + card_w - 1, cy + SUMM_H - 1],
                radius=CARD_R, fill=TOTAL_BG)
            draw.rectangle([cx_base, cy, cx_base + card_w - 1, cy + SUMM_H // 2], fill=TOTAL_BG)

            summ = [
                "ИТОГО", "",
            ]
            summ_colors = [TOTAL_ACCENT, MUTED]
            if show_period_qty_column:
                summ.append(f"+{totals.get('total_hour', 0)}")
                summ_colors.append(GREEN)
            summ.extend([
                str(totals.get('total_day', 0)),
                _fmt_sum(int(totals.get('total_revenue', 0))),
                _fmt_sum(int(totals.get('total_payout', 0))),
                _fmt_sum(int(totals.get('total_profit', 0))),
                f"{totals.get('avg_margin', 0):.1f}%",
            ])
            summ_colors.extend([BLUE, REVENUE_COL, PAYOUT_COL, PROFIT_COL, MARGIN_COL])
            cx = cx_base
            for i, (cell, w) in enumerate(zip(summ, COL_WIDTHS)):
                draw_cell(cx, cy, w, SUMM_H, cell, font_bold, summ_colors[i],
                          align=COL_ALIGNS[i])
                cx += w
        cy += SUMM_H + PAD_Y * 2

    # ── Grand Total card (all shops combined) ──────────────────────────
    if show_grand:
        # Background = TOTAL_BG (same as per-shop ИТОГО), border = SHOP_BG (dark blue)
        draw_rounded_card(cx_base, cy, card_w, SUMM_H)
        draw.rounded_rectangle(
            [cx_base, cy, cx_base + card_w - 1, cy + SUMM_H - 1],
            radius=CARD_R, fill=TOTAL_BG, outline=SHOP_BG, width=2 * SCALE)
        grand_cells = [
            "ВСЕГО", "",
        ]
        grand_colors = [TOTAL_ACCENT, MUTED]
        if show_period_qty_column:
            grand_cells.append(f"+{g_hour}")
            grand_colors.append(GREEN)
        grand_cells.extend([
            str(g_day),
            _fmt_sum(int(g_revenue)),
            _fmt_sum(int(g_payout)),
            _fmt_sum(int(g_profit)),
            f"{g_avg_margin:.1f}%",
        ])
        grand_colors.extend([BLUE, REVENUE_COL, PAYOUT_COL, PROFIT_COL, MARGIN_COL])
        cx = cx_base
        for i, (cell, w) in enumerate(zip(grand_cells, COL_WIDTHS)):
            draw_cell(cx, cy, w, SUMM_H, cell, font_bold, grand_colors[i],
                      align=COL_ALIGNS[i])
            cx += w
        cy += SUMM_H + PAD_Y * 2

    # ── Warehouse Expenses card ──────────────────────────────────────────
    if wh_exp_lines:
        exp_card_h = SVC_HEADER_H + len(wh_exp_lines) * SVCLINE_H + 4
        draw_rounded_card(cx_base, cy, card_w, exp_card_h)

        # Header
        draw.rounded_rectangle(
            [cx_base, cy, cx_base + card_w - 1, cy + SVC_HEADER_H - 1],
            radius=CARD_R, fill=EXP_HEADER)
        draw.rectangle([cx_base, cy + SVC_HEADER_H // 2, cx_base + card_w - 1, cy + SVC_HEADER_H - 1], fill=EXP_HEADER)
        draw.text((cx_base + PAD_X + 4 * SCALE, cy + 8 * SCALE), "Складские расходы", font=font_exp_h, fill=EXP_TOTAL)
        cy += SVC_HEADER_H

        for ei, (label, val) in enumerate(wh_exp_lines):
            row_bg = ROW_ODD if ei % 2 == 0 else ROW_EVEN
            is_total = (label == "Итого складские расходы")
            is_last  = (ei == len(wh_exp_lines) - 1)

            if is_last:
                # Round bottom corners for last row
                draw.rounded_rectangle(
                    [cx_base, cy, cx_base + card_w - 1, cy + SVCLINE_H + 3],
                    radius=CARD_R, fill=TOTAL_BG if is_total else row_bg)
                draw.rectangle([cx_base, cy, cx_base + card_w - 1, cy + SVCLINE_H // 2], fill=TOTAL_BG if is_total else row_bg)
            else:
                draw.rectangle([cx_base, cy, cx_base + card_w - 1, cy + SVCLINE_H - 1], fill=row_bg)

            fnt = font_bold if is_total else font_body
            clr = EXP_TOTAL if is_total else EXP_RED
            draw.text((cx_base + PAD_X + 8 * SCALE, cy + (SVCLINE_H - 12 * SCALE) / 2), label, font=fnt, fill=BODY_FG)
            val_str = _fmt_sum(val) + " сум"
            vw = tw(val_str, fnt)
            draw.text((cx_base + card_w - PAD_X - vw - 8 * SCALE, cy + (SVCLINE_H - 12 * SCALE) / 2),
                      val_str, font=fnt, fill=clr)
            if not is_last:
                draw.line([(cx_base + PAD_X, cy + SVCLINE_H - 1),
                           (cx_base + card_w - PAD_X, cy + SVCLINE_H - 1)], fill=DIVIDER)
            cy += SVCLINE_H
        cy += PAD_Y + 6 * SCALE

    # ── "Возврат Денег" income bottom line (Phase 3) ─────────────────────
    # Shown as its own single-line card when refunds_income > 0 so it reads
    # as income, not as another expense row. Amount rendered in the same
    # green used for profit (GREEN), opposite to EXP_RED used above.
    if refunds_income > 0:
        refund_card_h = SVCLINE_H + 4
        draw_rounded_card(cx_base, cy, card_w, refund_card_h)
        # Single rounded row — no header for brevity.
        draw.rounded_rectangle(
            [cx_base, cy, cx_base + card_w - 1, cy + SVCLINE_H + 3],
            radius=CARD_R, fill=TOTAL_BG)
        label = "Возврат Денег"
        draw.text(
            (cx_base + PAD_X + 8 * SCALE, cy + (SVCLINE_H - 12 * SCALE) / 2),
            label, font=font_bold, fill=BODY_FG,
        )
        val_str = "+" + _fmt_sum(refunds_income) + " сум"
        vw = tw(val_str, font_bold)
        draw.text(
            (cx_base + card_w - PAD_X - vw - 8 * SCALE, cy + (SVCLINE_H - 12 * SCALE) / 2),
            val_str, font=font_bold, fill=GREEN,
        )
        cy += SVCLINE_H + PAD_Y + 6 * SCALE

    # ── Footer ───────────────────────────────────────────────────────────
    footer_text = "SellerHub  ·  Uzum Analytics"
    ftw = tw(footer_text, font_small)
    draw.text(((total_w - ftw) / 2, cy + 6 * SCALE), footer_text, font=font_small, fill=FOOTER_FG)

    buf = _io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _coerce_notification_settings_payload(payload: dict | None) -> dict:
    data = dict(NOTIFICATION_SETTINGS_DEFAULTS)
    if payload:
        data.update({
            "hourly_enabled": bool(payload.get("hourly_enabled", data["hourly_enabled"])),
            "window_from_hour": int(payload.get("window_from_hour", data["window_from_hour"])),
            "window_to_hour": int(payload.get("window_to_hour", data["window_to_hour"])),
            "is_24h": bool(payload.get("is_24h", data["is_24h"])),
            "interval_hours": int(payload.get("interval_hours", data["interval_hours"])),
        })
    if bool(data["is_24h"]):
        data["hourly_enabled"] = True
    data["window_from_hour"] = max(0, min(23, int(data["window_from_hour"])))
    data["window_to_hour"] = max(0, min(23, int(data["window_to_hour"])))
    compatible_intervals = _compatible_notification_intervals(
        data["window_from_hour"],
        data["window_to_hour"],
        is_24h=bool(data["is_24h"]),
    )
    if int(data["interval_hours"]) not in compatible_intervals:
        data["interval_hours"] = compatible_intervals[0]
    data["window_length_hours"] = _notification_window_length_hours(
        data["window_from_hour"],
        data["window_to_hour"],
        is_24h=bool(data["is_24h"]),
    )
    data["compatible_intervals"] = list(compatible_intervals)
    data["daily_summary_hour"] = _notification_daily_summary_send_hour(
        data["window_from_hour"],
        data["window_to_hour"],
        is_24h=bool(data["is_24h"]),
    )
    data["interval_send_hours"] = list(_notification_interval_send_hours(
        data["window_from_hour"],
        data["window_to_hour"],
        is_24h=bool(data["is_24h"]),
        interval_hours=int(data["interval_hours"]),
    ))
    return data


def _get_user_notification_settings(user_id: int, *, db=None) -> dict:
    def _coerce(payload: dict | None) -> dict:
        return _coerce_notification_settings_payload(payload)

    if db is not None:
        row = db.execute(
            select(NotificationSettings).where(NotificationSettings.user_id == user_id)
        ).scalar_one_or_none()
        if not row:
            return _coerce(None)
        return _coerce({
            "hourly_enabled": row.hourly_enabled,
            "window_from_hour": row.window_from_hour,
            "window_to_hour": row.window_to_hour,
            "is_24h": row.is_24h,
            "interval_hours": row.interval_hours,
        })

    with SessionLocal() as local_db:
        return _get_user_notification_settings(user_id, db=local_db)


def _send_tg_photo(tg_id: str, image_bytes: bytes, pin: bool = False):
    """Send a photo to a Telegram user via the bot."""
    try:
        import telebot as _tb
        import io as _io
        cfg = _tg_config()
        token = cfg.get("bot_token", "")
        if not token or not tg_id:
            return
        bot = _tb.TeleBot(token, threaded=False)

        # Telegram recompresses photos and caps the longest side around 2560px.
        # Images above that are downsized server-side and look blurry. We
        # pre-downsize with LANCZOS so the SCALE=2 supersampling is folded
        # into a cap-fitting image before Telegram's own recompression.
        try:
            from PIL import Image as _PIL
            with _PIL.open(_io.BytesIO(image_bytes)) as _im:
                _w, _h = _im.size
                _longest = max(_w, _h)
                if _longest > 2560:
                    _ratio = 2560 / _longest
                    _new_size = (max(1, int(_w * _ratio)), max(1, int(_h * _ratio)))
                    _im = _im.convert("RGB") if _im.mode not in ("RGB", "RGBA") else _im
                    _resized = _im.resize(_new_size, _PIL.LANCZOS)
                    _buf = _io.BytesIO()
                    _resized.save(_buf, format="PNG", optimize=True)
                    image_bytes = _buf.getvalue()
        except Exception as _resize_err:
            print(f"[HourlySales] resize-before-send skipped: {_resize_err}")

        message = bot.send_photo(tg_id, _io.BytesIO(image_bytes))
        if pin and message and getattr(message, "message_id", None):
            try:
                bot.pin_chat_message(tg_id, message.message_id, disable_notification=True)
            except Exception as pin_error:
                print(f"[HourlySales] pin_chat_message skipped for {tg_id}: {pin_error}")
    except Exception as e:
        print(f"[HourlySales] send_photo error: {e}")


def _send_tg_message(tg_id: str, text: str):
    """Send a plain text message to a Telegram user via the bot."""
    try:
        import telebot as _tb
        cfg = _tg_config()
        token = cfg.get("bot_token", "")
        if not token or not tg_id:
            return
        bot = _tb.TeleBot(token, threaded=False)
        bot.send_message(tg_id, text)
    except Exception as e:
        print(f"[Subscription] send_message error for {tg_id}: {e}")


def _render_stock_image(rows: list, shop_name: str) -> bytes:
    """Render a stock table (Uzum qty vs Warehouse qty) as a PNG image."""
    from PIL import Image, ImageDraw, ImageFont
    import io as _io

    def _font(size, bold=False):
        candidates = (
            ["arialbd.ttf", "Arial Bold.ttf",
             "DejaVuSans-Bold.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"] if bold else
            ["arial.ttf", "Arial.ttf",
             "DejaVuSans.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
        )
        for name in candidates:
            try:
                return ImageFont.truetype(name, size)
            except Exception:
                pass
        return ImageFont.load_default()

    font_title  = _font(18, bold=True)
    font_header = _font(13, bold=True)
    font_body   = _font(13)
    font_small  = _font(11)

    BG        = (18,  18,  32)
    CARD      = (28,  28,  46)
    HEADER_BG = (45,  45,  80)
    ROW_ODD   = (33,  33,  55)
    ROW_EVEN  = (28,  28,  50)
    UZUM_COL  = (120, 100, 220)   # purple for uzum
    WH_GREEN  = (72,  199, 142)
    WH_WARN   = (240, 160,  40)
    WH_DANGER = (240,  80,  70)
    WHITE     = (240, 240, 255)
    MUTED     = (150, 150, 190)
    DIVIDER   = (60,  60,  90)
    ACCENT    = (99,  179, 237)

    COL_LABELS = ["Товар", "SKU", "Цвет/Размер", "Uzum", "Склад", "30д"]
    COL_WIDTHS = [230, 130, 120, 80, 80, 70]
    COL_ALIGNS = ["left", "left", "center", "center", "center", "center"]

    PAD_X, PAD_Y = 14, 9
    ROW_H   = 32
    TITLE_H = 52
    SECTION_H = 28
    FOOTER_H  = 28
    BORDER_R  = 14

    total_w = sum(COL_WIDTHS) + 2 * PAD_X

    # sort: danger first, then warn, then ok
    def _sort_key(r):
        s30 = r["sales_30d"] or 0
        wh  = r["wh_qty"]
        if s30 > 0:
            if wh < s30 / 2:  return 0
            if wh < s30:      return 1
        return 2

    sorted_rows = sorted(rows, key=_sort_key)

    total_h = (TITLE_H + SECTION_H + ROW_H          # header area
               + len(sorted_rows) * ROW_H
               + FOOTER_H + PAD_Y * 2)

    img  = Image.new("RGB", (total_w, total_h), BG)
    draw = ImageDraw.Draw(img)

    def text_w(t, f):
        try:    return f.getlength(t)
        except: return len(t) * 8

    def draw_cell(x, y, w, h, text, font, color, align="left", bg=None):
        if bg:
            draw.rectangle([x, y, x + w - 1, y + h - 1], fill=bg)
        if align == "center":
            tx = x + (w - text_w(text, font)) / 2
        elif align == "right":
            tx = x + w - text_w(text, font) - PAD_X
        else:
            tx = x + PAD_X
        draw.text((tx, y + (h - 14) / 2), text, font=font, fill=color)

    # title
    draw.rounded_rectangle([0, 0, total_w - 1, TITLE_H - 1], radius=BORDER_R, fill=CARD)
    draw.text((PAD_X + 4, 10), "  Остатки", font=font_title, fill=WHITE)
    draw.text((PAD_X + 4, 32), shop_name, font=font_small, fill=ACCENT)

    cy = TITLE_H + PAD_Y

    # column headers
    cx = 0
    for i, (label, w) in enumerate(zip(COL_LABELS, COL_WIDTHS)):
        draw_cell(cx, cy, w, ROW_H, label, font_header, WHITE,
                  align=COL_ALIGNS[i], bg=HEADER_BG)
        cx += w
    cy += ROW_H

    # legend row
    draw.rounded_rectangle([0, cy, total_w - 1, cy + SECTION_H - 1],
                            radius=4, fill=(35, 35, 58))
    legend = "  Красный = критично (<15д)    Жёлтый = мало (<30д)    Зелёный = норма"
    draw.text((PAD_X, cy + 7), legend, font=font_small, fill=MUTED)
    cy += SECTION_H

    # data rows
    for ri, r in enumerate(sorted_rows):
        row_bg = ROW_ODD if ri % 2 == 0 else ROW_EVEN
        s30 = r["sales_30d"] or 0
        wh  = r["wh_qty"]

        if s30 > 0:
            wh_color = WH_DANGER if wh < s30 / 2 else (WH_WARN if wh < s30 else WH_GREEN)
        else:
            wh_color = WHITE

        color_size = ""
        if r.get("color") and r.get("size"):
            color_size = f"{r['color']} / {r['size']}"
        elif r.get("color"):
            color_size = r["color"]
        elif r.get("size"):
            color_size = r["size"]

        name = r["group_name"]
        if len(name) > 28:
            name = name[:26] + "…"

        cells = [name, r["sku"] or "—", color_size or "—",
                 str(r["uzum_qty"]), str(wh), str(s30)]
        colors = [WHITE, WHITE, MUTED, UZUM_COL, wh_color, MUTED]

        cx = 0
        for i, (cell, w) in enumerate(zip(cells, COL_WIDTHS)):
            draw_cell(cx, cy, w, ROW_H, cell, font_body, colors[i],
                      align=COL_ALIGNS[i], bg=row_bg)
            cx += w

        draw.line([(0, cy + ROW_H - 1), (total_w, cy + ROW_H - 1)], fill=DIVIDER)
        cy += ROW_H

    draw.text((PAD_X, cy + 8), "SellerHub · Uzum Analytics", font=font_small, fill=MUTED)

    buf = _io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _lookup_sales_map_entry(sales_map, *, sku: str | None = None, barcode: str | None = None, sku_id: str | None = None):
    if not sales_map:
        return None
    for key in (
        (sku or "").strip(),
        (sku or "").strip().upper(),
        (barcode or "").strip(),
        (barcode or "").strip().upper(),
        (sku_id or "").strip(),
    ):
        if key and key in sales_map:
            return sales_map[key]
    return None


def _hourly_burst_stats_snapshot() -> dict:
    with _hourly_burst_stats_lock:
        return {
            **_hourly_burst_stats,
            "last_failed_shops": [dict(item) for item in _hourly_burst_stats.get("last_failed_shops", [])],
        }


def _mark_hourly_burst_started(*, started_at: datetime, shop_ids: list[str], date_from_ts: int, date_to_ts: int) -> None:
    with _hourly_burst_stats_lock:
        _hourly_burst_stats.update({
            "enabled": HOURLY_SALES_BURST_FETCH_ENABLED,
            "workers": HOURLY_SALES_BURST_FETCH_WORKERS,
            "in_progress": True,
            "last_status": "running",
            "last_started_at": started_at.isoformat(timespec="seconds"),
            "last_finished_at": None,
            "last_window_from": datetime.fromtimestamp(date_from_ts, APP_TZ).isoformat(timespec="seconds"),
            "last_window_to": datetime.fromtimestamp(date_to_ts, APP_TZ).isoformat(timespec="seconds"),
            "last_duration_seconds": None,
            "last_total_shops": len(shop_ids),
            "last_success_count": 0,
            "last_failed_count": 0,
            "last_failed_shops": [],
        })


def _mark_hourly_burst_finished(
    *,
    started_monotonic: float,
    finished_at: datetime,
    total_shops: int,
    results: dict[str, dict],
    errors: dict[str, str],
    status: str,
) -> None:
    failed_shops = [
        {"shop_id": shop_id, "error": (err or "")[:200]}
        for shop_id, err in sorted(errors.items())
    ][:20]
    with _hourly_burst_stats_lock:
        _hourly_burst_stats.update({
            "enabled": HOURLY_SALES_BURST_FETCH_ENABLED,
            "workers": HOURLY_SALES_BURST_FETCH_WORKERS,
            "in_progress": False,
            "last_status": status,
            "last_finished_at": finished_at.isoformat(timespec="seconds"),
            "last_duration_seconds": round(max(0.0, time.monotonic() - started_monotonic), 2),
            "last_total_shops": total_shops,
            "last_success_count": len(results),
            "last_failed_count": len(errors),
            "last_failed_shops": failed_shops,
        })


def _synthesize_shop_data_from_sales_maps(
    variants: list[tuple],
    *,
    day_map: dict | None = None,
    hour_map: dict | None = None,
) -> dict[str, dict]:
    synthesized_shop_data: dict[str, dict] = {}
    for v, g in variants:
        day_entry = _lookup_sales_map_entry(
            day_map,
            sku=v.sku,
            barcode=v.barcode,
            sku_id=v.uzum_sku_id,
        ) if day_map is not None else None
        hour_entry = _lookup_sales_map_entry(
            hour_map,
            sku=v.sku,
            barcode=v.barcode,
            sku_id=v.uzum_sku_id,
        ) if hour_map is not None else None
        base_entry = day_entry or hour_entry
        qty = int(base_entry.get("qty") or 0) if base_entry else 0
        if qty <= 0:
            continue
        row_key = str(v.sku or v.uzum_sku_id or v.barcode or f"group:{g.id}").strip()
        if not row_key:
            continue
        synthesized_shop_data[row_key] = {
            "amount": qty,
            "sell_price": int(base_entry.get("sell_price") or 0) * qty,
            "purchase_price": int(base_entry.get("price") or 0) * qty,
            "seller_profit": int(base_entry.get("seller_profit") or 0) * qty,
            "commission": int(base_entry.get("commission") or 0) * qty,
            "logistics_fee": int(base_entry.get("logistics") or 0) * qty,
            "product_title": g.name,
        }
    return synthesized_shop_data


def _fetch_exact_hour_sales_for_all_shops(
    shop_ids: list[str],
    *,
    api_key: str,
    date_from_ts: int,
    date_to_ts: int,
    track_stats: bool = True,
) -> tuple[dict[str, dict], dict[str, str]]:
    """Fetch exact previous-hour finance data for every shop in parallel."""
    if not HOURLY_SALES_BURST_FETCH_ENABLED or not shop_ids:
        return {}, {}

    started_dt = _now_app_tz()
    started_at = time.monotonic()
    if track_stats:
        _mark_hourly_burst_started(
            started_at=started_dt,
            shop_ids=shop_ids,
            date_from_ts=date_from_ts,
            date_to_ts=date_to_ts,
        )
    workers = min(HOURLY_SALES_BURST_FETCH_WORKERS, max(1, len(shop_ids)))
    results: dict[str, dict] = {}
    errors: dict[str, str] = {}

    def _task(shop_id: str):
        try:
            hour_map = fetch_finance_sales_map(
                shop_id,
                api_key=api_key,
                date_from_ts=date_from_ts,
                date_to_ts=date_to_ts,
            ) or {}
            return shop_id, hour_map, None
        except Exception as exc:
            return shop_id, {}, str(exc)

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_task, shop_id): shop_id for shop_id in shop_ids}
            for future in as_completed(futures):
                shop_id, hour_map, err = future.result()
                if err:
                    errors[shop_id] = err
                else:
                    results[shop_id] = hour_map
    except Exception as exc:
        errors["system"] = str(exc)
        if track_stats:
            _mark_hourly_burst_finished(
                started_monotonic=started_at,
                finished_at=_now_app_tz(),
                total_shops=len(shop_ids),
                results=results,
                errors=errors,
                status="failed",
            )
        raise

    elapsed = time.monotonic() - started_at
    if track_stats:
        _mark_hourly_burst_finished(
            started_monotonic=started_at,
            finished_at=_now_app_tz(),
            total_shops=len(shop_ids),
            results=results,
            errors=errors,
            status="partial_failure" if errors else "completed",
        )
    print(
        f"[HourlySales] Exact-hour burst: shops={len(shop_ids)} workers={workers} "
        f"ok={len(results)} failed={len(errors)} elapsed={elapsed:.2f}s"
    )
    return results, errors


def _format_sales_window_label(window_start: datetime, window_end_exclusive: datetime) -> str:
    closed_end = window_end_exclusive - timedelta(seconds=1)
    if window_start.date() == closed_end.date():
        return f"{window_start.strftime('%H:%M')} — {window_end_exclusive.strftime('%H:%M')}  ·  {window_start.strftime('%d.%m.%Y')}"
    return (
        f"{window_start.strftime('%d.%m.%Y %H:%M')} — "
        f"{window_end_exclusive.strftime('%d.%m.%Y %H:%M')}"
    )


def _do_hourly_sales_check(
    window_hours: int = 1,
    *,
    target_user_ids: list[int] | None = None,
    target_user_id: int | None = None,
    target_tg_id: str | None = None,
    snapshot_time: datetime | None = None,
    day_anchor: date | None = None,
    day_start_hour: int = 0,
    period_qty_label: str | None = None,
    day_qty_label: str = "С 00:00",
    show_period_qty_column: bool = True,
    warehouse_expense_snapshot_day: date | None = None,
    warehouse_expense_capture_if_missing: bool = False,
    pin: bool = False,
) -> int:
    """Build hourly sales notifications using exact previous-hour data when enabled.

    When target_user_id/target_user_ids are provided, the report is scoped to those users
    and sent only to their Telegram chats.
    """
    import datetime as _dt

    _t0 = time.monotonic()

    api_key = _get_admin_token()
    if not api_key:
        print("[HourlySales] No admin token, skipping.")
        return 0

    now = snapshot_time or _now_app_tz()
    snap_hour = now.replace(minute=0, second=0, microsecond=0)
    hour_start = snap_hour - _dt.timedelta(hours=window_hours)
    today = day_anchor or snap_hour.date()
    day_start_hour = max(0, min(23, int(day_start_hour or 0)))
    # "За час" / "С 00:00" come straight from sales_lines. We still keep
    # the api_key guard above so shops with no token get skipped early.
    hour_label = _format_sales_window_label(hour_start, snap_hour)
    period_qty_label = period_qty_label or ("За час" if window_hours == 1 else "За период")
    requested_user_ids = sorted({
        int(uid)
        for uid in (target_user_ids or [])
        if uid is not None
    })
    if target_user_id is not None and int(target_user_id) not in requested_user_ids:
        requested_user_ids.append(int(target_user_id))
        requested_user_ids.sort()

    with SessionLocal() as db:
        admin = db.execute(select(User).where(User.is_admin == True)).scalars().first()
        admin_id = admin.id if admin else None
        requested_users: dict[int, User] = {}
        if requested_user_ids:
            requested_users = {
                user.id: user
                for user in db.execute(
                    select(User).where(User.id.in_(requested_user_ids))
                ).scalars().all()
            }
        shops = db.execute(select(Shop)).scalars().all()

    shop_recipient_ids: dict[int, set[int]] = {}
    uid_shop_ids: dict[int, list[str]] = {}
    visible_shops: list[Shop] = []
    for shop in shops:
        if requested_user_ids:
            recipients = {
                uid for uid, user in requested_users.items()
                if user.is_admin or shop.owner_id == uid
            }
        else:
            recipients = set()
            if shop.owner_id:
                recipients.add(int(shop.owner_id))
            if admin_id:
                recipients.add(int(admin_id))

        if not recipients:
            continue

        visible_shops.append(shop)
        shop_recipient_ids[shop.id] = recipients
        if shop.uzum_id:
            for uid in recipients:
                uid_shop_ids.setdefault(uid, []).append(shop.uzum_id)

    if requested_user_ids and not visible_shops:
        print(f"[HourlySales] No visible shops for target users={requested_user_ids}, skipping.")
        return 0

    # ── Phase 3: read hour/day totals directly from `sales_lines`. ──
    # Window boundaries are **naive Tashkent** (NN8): `sales_lines.created_at`
    # is stored verbatim from the Tashkent CSV — no UTC shift at query time.
    from core.sales_reads import read_hourly_sku_breakdown as _read_hourly_sku_breakdown

    period_start_naive = _app_naive(hour_start)       # "За час" / "За период" lower bound
    period_end_naive = _app_naive(snap_hour)          # exclusive upper bound for both windows
    day_start_naive = _app_naive(_app_dt(today, day_start_hour))

    _t_fetch = time.monotonic()
    print(f"[HourlySales] TIMING: setup (no burst fetch) = {_t_fetch - _t0:.2f}s")

    total_hour_sold = 0
    uid_data: dict = {}   # uid -> shop_label -> group_id -> data for items sold since midnight
    uid_totals: dict = {} # uid -> shop_label -> full-shop day/hour totals

    with SessionLocal() as db:
        # ── 1-2. Read per-shop day + hour SKU breakdowns from sales_lines ──
        # Two GROUP BY queries per shop: one for the day-window ("С 00:00"),
        # one for the period-window ("За час" / "За период").
        #
        # For the daily summary case (period and day windows are identical)
        # we avoid the second query — just reuse the day map.
        same_window = period_start_naive == day_start_naive
        day_maps: dict[str, dict[str, dict]] = {}
        hour_maps: dict[str, dict[str, dict]] = {}
        for shop in visible_shops:
            if not shop.uzum_id:
                continue
            try:
                sid_int = int(shop.uzum_id)
            except (TypeError, ValueError):
                continue
            day_map = _read_hourly_sku_breakdown(
                sid_int, day_start_naive, period_end_naive, session=db,
            )
            day_maps[shop.uzum_id] = day_map
            if same_window:
                hour_maps[shop.uzum_id] = day_map
            else:
                hour_maps[shop.uzum_id] = _read_hourly_sku_breakdown(
                    sid_int, period_start_naive, period_end_naive, session=db,
                )

        # ── 3. Build notification data from DB ───────────────────────────
        for shop in visible_shops:
            shop_data = dict(day_maps.get(shop.uzum_id, {}))
            shop_hour_map = hour_maps.get(shop.uzum_id, {})
            shop_label = shop.name or shop.uzum_id

            # Load variants for SKU matching
            variants = db.execute(
                select(Variant, ProductGroup)
                .join(ProductGroup, Variant.group_id == ProductGroup.id)
                .where(ProductGroup.shop_id == shop.id)
            ).all()

            # Build sku_title → (variant, group) lookup
            sku_to_vg: dict[str, tuple] = {}
            for v, g in variants:
                if v.sku:
                    sku_to_vg[v.sku.strip()] = (v, g)
                    sku_to_vg[v.sku.strip().upper()] = (v, g)

            if not shop_data:
                continue

            for sku_id, fo_data in shop_data.items():
                d_qty = fo_data["amount"]
                sell_price     = fo_data["sell_price"]
                purchase_price = fo_data["purchase_price"]
                seller_profit  = fo_data["seller_profit"]
                commission_u   = fo_data["commission"]
                logistics_u    = fo_data["logistics_fee"]
                product_title  = fo_data.get("product_title") or sku_id

                # Match SKU to local variant for grouping + price fallbacks;
                # display name always comes from sales_lines.sku_title (Russian
                # from Uzum) so the notification doesn't pick up whatever
                # language the seller typed into ProductGroup.name locally.
                vg = sku_to_vg.get(sku_id) or sku_to_vg.get(sku_id.upper() if sku_id else "")
                if vg:
                    v, g = vg
                    group_id = g.id
                    sku_label = v.sku or sku_id
                    if sell_price == 0:
                        sell_price = v.sell_price_uzum or v.price_sum or 0
                    if purchase_price == 0:
                        purchase_price = v.purchase_price or 0
                else:
                    group_id = sku_id
                    sku_label = sku_id

                group_name = product_title or sku_id

                if d_qty <= 0:
                    continue

                # "За час" / "За период" qty comes from the period-window
                # aggregate keyed by sku_id. For daily summary the two
                # maps are identical, so h_qty == d_qty.
                h_entry = shop_hour_map.get(sku_id)
                h_qty = int(h_entry.get("amount") or 0) if h_entry else 0

                # Finance seller_profit is treated as Uzum payout / withdrawable amount.
                # Actual profit for the report is payout minus product cost.
                if d_qty > 0:
                    revenue_unit = sell_price / d_qty
                    cost_unit = purchase_price / d_qty
                    comm_unit = commission_u / d_qty
                    log_unit = logistics_u / d_qty
                    payout_unit = seller_profit / d_qty if seller_profit else (revenue_unit - comm_unit - log_unit)
                    profit_unit = payout_unit - cost_unit
                else:
                    revenue_unit = cost_unit = comm_unit = log_unit = payout_unit = profit_unit = 0

                user_ids = shop_recipient_ids.get(shop.id, set())
                if not user_ids:
                    continue
                row_profit = d_qty * profit_unit
                row_revenue = d_qty * revenue_unit
                row_payout = d_qty * payout_unit

                for uid in user_ids:
                    shop_totals = uid_totals.setdefault(uid, {}).setdefault(shop_label, {
                        "total_hour": 0,
                        "total_day": 0,
                        "total_profit": 0.0,
                        "total_revenue": 0.0,
                        "total_payout": 0.0,
                        "total_seller_profit": 0.0,
                        "total_cost": 0.0,
                        "total_commission": 0.0,
                        "total_logistics": 0.0,
                        "total_storage": 0.0,
                    })
                    shop_totals["total_day"] += d_qty
                    shop_totals["total_profit"] += row_profit
                    shop_totals["total_revenue"] += row_revenue
                    shop_totals["total_payout"] += row_payout
                    shop_totals["total_seller_profit"] += seller_profit
                    shop_totals["total_cost"] += purchase_price
                    shop_totals["total_commission"] += commission_u
                    shop_totals["total_logistics"] += logistics_u

                for uid in user_ids:
                    uid_totals[uid][shop_label]["total_hour"] += h_qty
                    grp = uid_data.setdefault(uid, {}) \
                                  .setdefault(shop_label, {}) \
                                  .setdefault(group_id, {
                                      "name": group_name, "skus": set(),
                                      "h_qty": 0, "d_qty": 0,
                                      "profit": 0.0, "revenue": 0.0, "payout": 0.0,
                                      "seller_profit_tot": 0.0, "cost_tot": 0.0,
                                      "commission_tot": 0.0, "logistics_tot": 0.0,
                                      "storage_tot": 0.0,
                                  })
                    grp["h_qty"]             += h_qty
                    grp["d_qty"]             += d_qty
                    grp["profit"]            += row_profit
                    grp["revenue"]           += row_revenue
                    grp["payout"]            += row_payout
                    grp["seller_profit_tot"] += seller_profit
                    grp["cost_tot"]          += purchase_price
                    grp["commission_tot"]    += commission_u
                    grp["logistics_tot"]     += logistics_u
                    grp["skus"].add(sku_label)

                total_hour_sold += h_qty

    _t1 = time.monotonic()
    print(f"[HourlySales] TIMING: DB queries + data build = {_t1 - _t0:.2f}s")

    if not uid_data:
        return 0

    uid_expenses: dict[int, dict] = {}
    _t2_start = time.monotonic()
    if warehouse_expense_snapshot_day is not None:
        # Phase 3: daily-summary expenses + refunds come from `expenses_ledger`.
        # Window = `[day 00:00, day+1 00:00)` Tashkent-naive (match charged_at).
        from core.sales_reads import (
            read_daily_expense_breakdown as _read_daily_expense_breakdown,
        )

        exp_day_start = datetime.combine(warehouse_expense_snapshot_day, dt_time(0, 0, 0))
        exp_day_end = datetime.combine(
            warehouse_expense_snapshot_day + timedelta(days=1), dt_time(0, 0, 0)
        )
        per_shop_exp: dict[str, dict] = {}
        for _sid_str in sorted({sid for sid_list in uid_shop_ids.values() for sid in sid_list}):
            try:
                _sid_int = int(_sid_str)
            except (TypeError, ValueError):
                continue
            per_shop_exp[_sid_str] = _read_daily_expense_breakdown(
                _sid_int, exp_day_start, exp_day_end,
            )

        # Merge per-user across all shops the user owns. `amount` stays
        # positive (coder rule §10); refunds_income is a separate bottom
        # line and MUST NOT be added into `total`.
        for _uid, _sid_list in uid_shop_ids.items():
            merged_items: dict[str, int] = {}
            merged_total = 0
            merged_refunds = 0
            for sid in _sid_list:
                bucket = per_shop_exp.get(str(sid)) or {"items": [], "total": 0, "refunds_income": 0}
                for it in bucket.get("items", []):
                    nm = str(it.get("name") or "").strip()
                    amt = int(it.get("amount") or 0)
                    if not nm or amt <= 0:
                        continue
                    merged_items[nm] = merged_items.get(nm, 0) + amt
                merged_total += int(bucket.get("total", 0) or 0)
                merged_refunds += int(bucket.get("refunds_income", 0) or 0)
            items_sorted = [
                {"name": nm, "amount": amt}
                for nm, amt in sorted(merged_items.items(), key=lambda kv: (-kv[1], kv[0]))
            ]
            uid_expenses[_uid] = {
                "items": items_sorted,
                "total": merged_total,
                "refunds_income": merged_refunds,
            }
        _t2 = time.monotonic()
        print(
            f"[HourlySales] TIMING: expenses_ledger read "
            f"({warehouse_expense_snapshot_day.isoformat()}) = {_t2 - _t2_start:.2f}s"
        )
    else:
        _t2 = time.monotonic()
        print(f"[HourlySales] TIMING: expenses skipped for interval notification = {_t2 - _t2_start:.2f}s")

    # ── Build per-user notification payloads ────────────────────────
    user_payloads: list[tuple[str, dict, str, str, str, bool, bool]] = []  # (telegram_id, by_shop, hour_label, period_qty_label, day_qty_label, show_period_qty_column, pin)

    with SessionLocal() as db:
        for uid, shops_d in uid_data.items():
            if requested_user_ids and uid not in requested_user_ids:
                continue
            user = requested_users.get(uid) if requested_user_ids else db.get(User, uid)
            resolved_tg_id = target_tg_id if target_user_id is not None and uid == target_user_id and target_tg_id else (user.telegram_id if user else None)
            if not user or not resolved_tg_id:
                continue

            by_shop:    dict = {}
            totals_map: dict = {}
            full_shop_totals = uid_totals.get(uid, {})

            for shop_label, groups_d in shops_d.items():
                items = []
                t_hour = 0

                for gid, gd in sorted(
                    groups_d.items(),
                    key=lambda x: (x[1]["d_qty"], x[1]["h_qty"], str(x[1]["name"]).lower()),
                ):
                    rev  = gd["revenue"]
                    profit = gd["profit"]
                    margin = round(profit / rev * 100, 1) if rev > 0 else 0.0
                    items.append({
                        "group":    gd["name"],
                        "base_sku": _base_sku(list(gd["skus"])),
                        "hour_qty": gd["h_qty"],
                        "day_qty":  gd["d_qty"],
                        "revenue":  int(rev),
                        "payout":   int(gd["payout"]),
                        "profit":   int(profit),
                        "margin":   margin,
                    })
                    t_hour       += gd["h_qty"]

                shop_totals = full_shop_totals.get(shop_label, {})
                t_hour = int(shop_totals.get("total_hour", t_hour))
                t_day = int(shop_totals.get("total_day", 0))
                t_profit = float(shop_totals.get("total_profit", 0.0))
                t_rev = float(shop_totals.get("total_revenue", 0.0))
                t_payout = float(shop_totals.get("total_payout", 0.0))
                t_commission = float(shop_totals.get("total_commission", 0.0))
                t_logistics = float(shop_totals.get("total_logistics", 0.0))
                t_storage = float(shop_totals.get("total_storage", 0.0))

                avg_margin = round(t_profit / t_rev * 100, 1) if t_rev > 0 else 0.0
                by_shop[shop_label] = items
                totals_map[shop_label] = {
                    "total_hour":       t_hour,
                    "total_day":        t_day,
                    "total_profit":     int(t_profit),
                    "avg_margin":       avg_margin,
                    "total_revenue":    int(t_rev),
                    "total_payout":     int(t_payout),
                    "total_commission": int(t_commission),
                    "total_logistics":  int(t_logistics),
                    "total_storage":    int(t_storage),
                }

            by_shop["__totals__"] = totals_map
            by_shop["__expenses__"] = uid_expenses.get(uid, {})
            user_payloads.append((resolved_tg_id, by_shop, hour_label, period_qty_label, day_qty_label, show_period_qty_column, pin))

    # ── Parallel render + send for ALL users ──────────────────────
    # At 10 000 users: 20 workers × ~1.6s each = ~13 min (fits in 1 hour)
    _sent = _failed = 0

    def _render_and_send(tg_id, by_shop_data, h_label, period_label, day_label, show_period_col, should_pin):
        try:
            img_bytes = _render_sales_image(
                by_shop_data,
                h_label,
                period_qty_label=period_label,
                day_qty_label=day_label,
                show_period_qty_column=show_period_col,
            )
            _send_tg_photo(tg_id, img_bytes, pin=should_pin)
            return True
        except Exception as e:
            print(f"[HourlySales] render/send failed for {tg_id}: {e}")
            return False

    notify_workers = min(100, max(1, len(user_payloads)))
    with ThreadPoolExecutor(max_workers=notify_workers) as pool:
        futures = {
            pool.submit(_render_and_send, tg_id, bs, hl, period_label, day_label, show_period_col, should_pin): tg_id
            for tg_id, bs, hl, period_label, day_label, show_period_col, should_pin in user_payloads
        }
        for future in as_completed(futures):
            if future.result():
                _sent += 1
            else:
                _failed += 1

    _t_end = time.monotonic()
    print(f"[HourlySales] TIMING: total={_t_end-_t0:.2f}s | sent={_sent} failed={_failed} users={len(user_payloads)}")
    return total_hour_sold


def _run_scheduled_hourly_sales_check(snap_hour: datetime | None = None) -> int:
    snap_hour = (snap_hour or _now_app_tz()).replace(minute=0, second=0, microsecond=0)

    dispatch_groups: dict[tuple, dict] = {}

    def _queue_dispatch(
        user_id: int,
        *,
        window_hours: int,
        snapshot_time: datetime,
        day_anchor: date,
        day_start_hour: int = 0,
        period_qty_label: str,
        day_qty_label: str = "С 00:00",
        show_period_qty_column: bool = True,
        warehouse_expense_snapshot_day: date | None = None,
        warehouse_expense_capture_if_missing: bool = False,
        pin: bool = False,
    ) -> None:
        key = (
            window_hours,
            snapshot_time.isoformat(),
            day_anchor.isoformat(),
            day_start_hour,
            period_qty_label,
            day_qty_label,
            show_period_qty_column,
            warehouse_expense_snapshot_day.isoformat() if warehouse_expense_snapshot_day else "",
            warehouse_expense_capture_if_missing,
            pin,
        )
        group = dispatch_groups.setdefault(key, {
            "user_ids": [],
            "window_hours": window_hours,
            "snapshot_time": snapshot_time,
            "day_anchor": day_anchor,
            "day_start_hour": day_start_hour,
            "period_qty_label": period_qty_label,
            "day_qty_label": day_qty_label,
            "show_period_qty_column": show_period_qty_column,
            "warehouse_expense_snapshot_day": warehouse_expense_snapshot_day,
            "warehouse_expense_capture_if_missing": warehouse_expense_capture_if_missing,
            "pin": pin,
        })
        group["user_ids"].append(user_id)

    with SessionLocal() as db:
        users = [
            user for user in db.execute(
                select(User).where(User.telegram_id.is_not(None))
            ).scalars().all()
            if (user.telegram_id or "").strip()
        ]

        sub_settings = _get_or_create_subscription_settings(db)

        # Subscription dates are stored as naive UTC (datetime.utcnow()).
        # snap_hour is timezone-aware (Tashkent). Convert to naive UTC so comparisons
        # don't raise TypeError when mixing aware and naive datetimes.
        from datetime import timezone as _utc_tz
        snap_hour_utc = snap_hour.astimezone(_utc_tz.utc).replace(tzinfo=None)

        for user in users:
            # Skip expired users; send a one-time expiry notice in the first hour after expiry
            if not user.is_admin:
                status = _subscription_status_for_user(user, settings=sub_settings, now=snap_hour_utc)
                if not status["active"]:
                    effective_end = status.get("effective_end_at")
                    if effective_end:
                        just_expired = (snap_hour_utc - timedelta(hours=1)) <= effective_end < snap_hour_utc
                        if just_expired:
                            tg_id = (user.telegram_id or "").strip()
                            if tg_id:
                                _send_tg_message(
                                    tg_id,
                                    "⚠️ Ваша подписка на SellerHub истекла.\n\n"
                                    "Уведомления о продажах приостановлены. "
                                    "Перейдите на страницу подписки, чтобы продлить доступ.",
                                )
                                print(f"[Subscription] Sent expiry notice to user_id={user.id} tg={tg_id}")
                    continue  # Do not send sales notifications to expired users

            prefs = _get_user_notification_settings(user.id, db=db)
            if not prefs["hourly_enabled"]:
                continue

            hour = snap_hour.hour
            window_from = int(prefs["window_from_hour"])
            window_to = int(prefs["window_to_hour"])
            interval_hours = int(prefs["interval_hours"])
            is_24h = bool(prefs["is_24h"])
            window_length = int(prefs.get("window_length_hours") or 0)
            if not is_24h and window_length <= 0:
                continue

            daily_summary_hour = int(prefs.get("daily_summary_hour", 0))
            if hour == daily_summary_hour:
                summary_day_anchor = (snap_hour - timedelta(days=1)).date()
                _queue_dispatch(
                    user.id,
                    window_hours=24,
                    snapshot_time=_app_dt(snap_hour.date(), 0),
                    day_anchor=summary_day_anchor,
                    day_start_hour=0,
                    period_qty_label="За день",
                    day_qty_label="С 00:00",
                    show_period_qty_column=False,
                    warehouse_expense_snapshot_day=summary_day_anchor,
                    warehouse_expense_capture_if_missing=True,
                    pin=True,
                )

            interval_send_hours = set(prefs.get("interval_send_hours") or [])
            if hour in interval_send_hours or (hour == daily_summary_hour and daily_summary_hour != 0):
                _queue_dispatch(
                    user.id,
                    window_hours=interval_hours,
                    snapshot_time=snap_hour,
                    day_anchor=snap_hour.date(),
                    day_start_hour=0,
                    period_qty_label="За час" if interval_hours == 1 else "За период",
                    day_qty_label="С 00:00",
                )

    if not dispatch_groups:
        print(f"[HourlySales] No scheduled recipients at {snap_hour.isoformat()}. Snapshots saved.")
        return 0

    recipient_count = sum(len(set(group["user_ids"])) for group in dispatch_groups.values())
    total_sent_qty = 0
    for group in dispatch_groups.values():
        group_user_ids = sorted(set(group.pop("user_ids")))
        total_sent_qty += _do_hourly_sales_check(
            target_user_ids=group_user_ids,
            **group,
        )

    print(
        f"[HourlySales] Scheduled dispatch complete at {snap_hour.isoformat()} "
        f"groups={len(dispatch_groups)} recipients={recipient_count}"
    )
    return total_sent_qty


# ─────────────────────────────────────────────────────────────────────
# Phase 1 — four new Reports-API loops (dual-write; existing loops stay).
#
# Per-shop loops acquire Redis shop_lock before fetching and release in
# `finally` (coder rule §5). Nightly is ONE API call per shop for the full
# 45-day range — NOT chunked (coder rule §6). The onboarding backfill
# chunks 2022→today in 60-day windows, drained via SELECT ... FOR UPDATE
# SKIP LOCKED LIMIT 1.
# ─────────────────────────────────────────────────────────────────────

# Path C: max shops per /documents/create call. Probes (scripts/
# probe_uzum_bulk_shop_limit.py) showed no array-length cap up to N=10000
# from the 4-shop cabinet, *and* Uzum dedups shopIds server-side, so we
# can't measure the distinct-shop business cap from here. Keep this as a
# sharding escape hatch in case Uzum ever rejects huge arrays at real scale.
SHARD_BY_K = max(1, int(os.getenv("UZUM_SELLS_SHARD_BY_K", "200")))

# Probe used 65s between same-token creates to dodge Uzum's 60s exact-args
# dedup window. Bulk Path C only fires once per hour per chunk, so this
# only matters when SHARD_BY_K splits a hour into >1 chunk.
_BULK_CREATE_BETWEEN_CHUNKS_S = 65


# ── OpenAPI primary path: per-shop, per-owner-token ──────────────────
# When the shop's owner has a `User.uzum_openapi_token`, we route finance
# fetches through /v1/finance/orders + /v1/finance/expenses (per-user, no
# race bug, richer fields). Shops whose owner has no token fall back to
# the legacy bulk SELLS_REPORT / EXPENSES_REPORT CSV pipeline below.
#
# The CSV pipeline stays exactly as it was — we just bolt the routing
# wrapper on top so callers don't have to choose. See:
#   - core/uzum_finance_openapi.py for the per-shop fetchers + normalizers
#   - migration 20260520_0004 for the new sales_lines / expenses_ledger cols

# ─────────────────────────────────────────────────────────────────────
# Legacy-style finance pipeline (restored 2026-05-21).
#
# Daily aggregates per (shop, day, sku) → FinanceOrder, fetched from
# OpenAPI /v1/finance/orders?group=true. Hourly cumulative snapshots →
# FinanceHourlySnapshot, used by Telegram for delta-based "За час"
# notifications. Backfill on shop attach runs in a single daemon thread
# from first-sale-year → today (no chunk queue, no continuous tick).
# ─────────────────────────────────────────────────────────────────────


def _ingest_finance_orders_for_day(
    rows: list[dict],
    shop_uzum_id: str,
    period_day: date,
) -> int:
    """Idempotent DELETE+INSERT for a single (shop, day) into finance_orders.

    Drops the date window's existing rows for this shop, then bulk-inserts
    the fresh aggregates. Empty `rows` is allowed — that means the day had
    no sales and we still wipe stale rows (handles a day going from N
    products sold to 0 due to refunds after-the-fact).
    """
    shop_str = str(shop_uzum_id)
    now_utc = datetime.utcnow()

    payload: list[dict] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        sku_title = (r.get("sku_title") or "").strip()
        if not sku_title:
            continue
        payload.append({
            "shop_id":         shop_str,
            "period_from":     period_day,
            "period_to":       period_day,
            "sku_title":       sku_title[:300],
            "sku_id":          r.get("sku_id"),
            "product_id":      r.get("product_id"),
            "product_title":   r.get("product_title"),
            "product_title_ru": r.get("product_title_ru"),
            "image_url":       r.get("image_url"),
            "characteristics": r.get("characteristics"),
            "amount":          int(r.get("amount") or 0),
            "amount_returns":  int(r.get("amount_returns") or 0),
            "sell_price":      int(r.get("sell_price") or 0),
            "purchase_price":  int(r.get("purchase_price") or 0),
            "seller_discount": int(r.get("seller_discount") or 0),
            "seller_profit":   int(r.get("seller_profit") or 0),
            "commission":      int(r.get("commission") or 0),
            "withdrawn_profit": int(r.get("withdrawn_profit") or 0),
            "logistics_fee":   int(r.get("logistics_fee") or 0),
            "synced_at":       now_utc,
        })

    with SessionLocal() as db:
        with db.begin():
            db.execute(
                delete(FinanceOrder).where(
                    FinanceOrder.shop_id == shop_str,
                    FinanceOrder.period_from == period_day,
                    FinanceOrder.period_to == period_day,
                )
            )
            if payload:
                db.execute(insert(FinanceOrder), payload)

    return len(payload)


def _refresh_finance_for_shop_day(
    shop_uzum_id: str,
    day_tashkent: date,
    *,
    token: str | None = None,
) -> int:
    """Fetch + ingest one day's aggregates. Holds shop_lock for the duration.

    Returns the number of SKU rows written. Returns 0 (and logs) if no
    OpenAPI token is available for the shop — caller decides what to do.
    """
    tok = token or _owner_openapi_token_for_shop(shop_uzum_id)
    if not tok:
        print(f"[FinanceFetch] no OpenAPI token for shop={shop_uzum_id} — skipping day {day_tashkent}")
        return 0

    if not shop_lock.try_acquire_shop_lock(str(shop_uzum_id)):
        print(f"[FinanceFetch] lock contention shop={shop_uzum_id} day={day_tashkent} — skipping")
        return 0
    try:
        from core import uzum_finance_openapi as _ufo
        rows = _ufo.fetch_daily_aggregates_for_shop_day(
            tok, shop_uzum_id, day_tashkent,
        )
        n = _ingest_finance_orders_for_day(rows, shop_uzum_id, day_tashkent)
        return n
    finally:
        shop_lock.release_shop_lock(str(shop_uzum_id))


def _run_full_backfill_for_shop(shop_uzum_id: str, shop_pk: int) -> dict:
    """Daemon-thread entrypoint for the initial backfill of a newly-added shop.

    1. Detect first-sale year via yearly probes on /v1/finance/orders.
    2. For each day from first-year Jan 1 → today, fetch group=true
       aggregates and write to finance_orders. Parallel via a thread pool
       (FINANCE_BACKFILL_PARALLELISM, default 32). Each thread works on a
       different day so no (shop, day) collision — the per-shop Redis lock
       is BYPASSED here (it would serialize the pool back down to 1).
    3. Returns a summary dict.

    Cost (~870 days, busy shop) with default parallelism=32: ~1-3 minutes
    if Uzum doesn't throttle; longer if 429s + additive backoff kick in.
    """
    import os as _os
    from concurrent.futures import ThreadPoolExecutor
    from threading import Lock as _Lock
    from core import uzum_finance_openapi as _ufo
    from config import FINANCE_BACKFILL_START_DATE

    summary = {
        "shop_id": shop_uzum_id,
        "first_year": None,
        "days_attempted": 0,
        "days_written": 0,
        "rows_written": 0,
        "errors": 0,
    }

    token = _owner_openapi_token_for_shop(shop_uzum_id)
    if not token:
        print(f"[FinanceBackfill] no OpenAPI token for shop={shop_uzum_id} — backfill SKIPPED")
        summary["errors"] = 1
        return summary

    # Parse hard-floor fallback year from FINANCE_BACKFILL_START_DATE env var.
    try:
        fallback_year = int(str(FINANCE_BACKFILL_START_DATE).split("-")[0])
    except (ValueError, IndexError):
        fallback_year = 2022

    try:
        first_year = _ufo.detect_first_sale_year(
            token, shop_uzum_id, fallback_year=fallback_year,
        )
    except Exception as e:
        print(f"[FinanceBackfill] first-year probe failed shop={shop_uzum_id}: {e!r} — falling back to {fallback_year}")
        first_year = fallback_year
    summary["first_year"] = first_year

    today_tashkent = _now_app_tz().date()
    start_day = date(first_year, 1, 1)

    try:
        parallelism = int(_os.environ.get("FINANCE_BACKFILL_PARALLELISM", "32"))
    except (TypeError, ValueError):
        parallelism = 32
    parallelism = max(1, min(parallelism, 64))

    # Materialize the day list so the pool can fan out.
    days: list[date] = []
    cur = start_day
    while cur <= today_tashkent:
        days.append(cur)
        cur += timedelta(days=1)

    print(f"[FinanceBackfill] shop={shop_uzum_id} window=[{start_day}, {today_tashkent}] "
          f"days={len(days)} parallelism={parallelism} starting…")

    summary_lock = _Lock()

    def _backfill_one_day(day: date) -> None:
        try:
            # Fetch + ingest directly, bypassing the per-shop Redis lock —
            # each thread works on a distinct day so (shop, day) ingest
            # writes don't collide. The hourly/nightly loops only touch
            # today, no overlap with historical backfill days.
            rows = _ufo.fetch_daily_aggregates_for_shop_day(
                token, shop_uzum_id, day,
            )
            n = _ingest_finance_orders_for_day(rows, shop_uzum_id, day)
            with summary_lock:
                summary["days_attempted"] += 1
                summary["rows_written"] += n
                if n > 0:
                    summary["days_written"] += 1
        except Exception as exc:
            with summary_lock:
                summary["days_attempted"] += 1
                summary["errors"] += 1
            print(f"[FinanceBackfill] shop={shop_uzum_id} day={day} ERROR: {exc!r}")

    with ThreadPoolExecutor(max_workers=parallelism,
                            thread_name_prefix=f"backfill-{shop_uzum_id}") as pool:
        # list(...) forces consumption so we wait for all futures.
        list(pool.map(_backfill_one_day, days))

    print(f"[FinanceBackfill] shop={shop_uzum_id} DONE: "
          f"days_attempted={summary['days_attempted']} "
          f"days_written={summary['days_written']} "
          f"rows={summary['rows_written']} "
          f"errors={summary['errors']}")
    return summary


def _save_hourly_snapshots(snap_hour_tashkent: datetime) -> None:
    """Snapshot today's cumulative finance_orders totals at the HH:00 boundary.

    Reads today's finance_orders rows and inserts ONE
    FinanceHourlySnapshot per (shop, sku) holding the cumulative totals
    as of `snap_hour_tashkent` (the boundary that just ticked). Pure DB
    math, no API call. Delete snapshots older than 25 hours.

    `snap_hour_tashkent` is naive Tashkent. We convert to naive UTC for
    storage (legacy convention) so the snapshot is comparable across
    Daylight-saving boundaries.
    """
    # Today in Tashkent = the day the just-closed hour belongs to. For
    # the midnight tick (snap_hour = today 00:00) the just-closed hour
    # is yesterday's 23:00 → 00:00, so use snap_hour - 1s to pick the day.
    snapshot_day = (snap_hour_tashkent - timedelta(seconds=1)).date()

    # Tashkent → UTC for the column (matches legacy schema).
    snap_hour_utc = (
        snap_hour_tashkent.replace(tzinfo=APP_TZ)
        .astimezone(timezone.utc)
        .replace(tzinfo=None)
    )

    with SessionLocal() as db:
        snapshot_rows = db.execute(
            select(FinanceOrder).where(
                FinanceOrder.period_from == snapshot_day,
                FinanceOrder.period_to == snapshot_day,
            )
        ).scalars().all()

        cutoff = snap_hour_utc - timedelta(hours=25)
        db.execute(
            delete(FinanceHourlySnapshot)
            .where(FinanceHourlySnapshot.snapshot_hour < cutoff)
        )
        # Re-running the hourly twice in the same hour shouldn't double-write.
        db.execute(
            delete(FinanceHourlySnapshot)
            .where(FinanceHourlySnapshot.snapshot_hour == snap_hour_utc)
        )

        payload = []
        for row in snapshot_rows:
            payload.append({
                "shop_id":        row.shop_id,
                "sku_title":      row.sku_title,
                "snapshot_hour":  snap_hour_utc,
                "amount":         row.amount,
                "sell_price":     row.sell_price,
                "purchase_price": row.purchase_price,
                "seller_profit":  row.seller_profit,
                "commission":     row.commission,
                "logistics_fee":  row.logistics_fee,
            })
        if payload:
            db.execute(insert(FinanceHourlySnapshot), payload)
        db.commit()

    print(f"[FinanceSnapshot] saved {len(payload)} rows for snap_hour={snap_hour_tashkent.isoformat()} "
          f"(UTC={snap_hour_utc.isoformat()})")


def _hourly_finance_loop():
    """Sleep until next HH:00 Tashkent; refresh today's finance for every shop.

    Per HH:00 boundary:
      1. For every active shop, call _refresh_finance_for_shop_day(today)
         → fetches group=true aggregates for today and DELETE+INSERTs
           finance_orders rows.
      2. Save FinanceHourlySnapshot (cumulative-today totals per shop+sku
         at this snap_hour).
      3. Telegram hourly notifications — TODO wire up to read snapshot
         delta (current hour row − previous hour row); for now we only
         keep the data fresh and the existing Telegram code keeps reading
         its prior sources until we migrate it.

    No 5s tick: `time.sleep(seconds_until_next_HH:00)` and wake up
    exactly when needed.
    """
    import time as _t

    def _next_hour_target() -> datetime:
        now = _now_app_tz()
        nxt = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        return nxt

    next_hour = _next_hour_target()
    while True:
        try:
            now = _now_app_tz()
            sleep_seconds = (next_hour - now).total_seconds()
            if sleep_seconds > 0:
                _t.sleep(sleep_seconds)

            snap_hour = _now_app_tz().replace(minute=0, second=0, microsecond=0)
            today = snap_hour.date()
            # The boundary we just crossed = snap_hour. The data we
            # refresh is for the calendar day `today` (or `today-1d` if
            # we just crossed midnight). We always refresh today's
            # cumulative data — at midnight that means yesterday's full
            # day is captured.
            refresh_day = (snap_hour - timedelta(seconds=1)).date()

            print(f"[FinanceHourly] tick snap_hour={snap_hour.isoformat()} day={refresh_day}")
            shops = _active_shop_ids_for_sales()
            for s in shops:
                try:
                    n = _refresh_finance_for_shop_day(s, refresh_day)
                    print(f"[FinanceHourly] shop={s} day={refresh_day} rows={n}")
                except Exception as e:
                    print(f"[FinanceHourly] shop={s} day={refresh_day} ERROR: {e!r}")

            # 2. Cumulative snapshot for delta math.
            try:
                _save_hourly_snapshots(snap_hour)
            except Exception as e:
                print(f"[FinanceHourly] _save_hourly_snapshots ERROR: {e!r}")

            next_hour = _next_hour_target()
        except Exception as e:
            print(f"[FinanceHourly] unexpected error: {e!r}")
            _t.sleep(60)
            next_hour = _next_hour_target()


def _nightly_finance_refetch_loop():
    """Sleep until next 00:30 Tashkent; refetch last 45 days for every shop.

    Re-aligns finance_orders with Uzum's truth — catches late status
    flips, refunds, and price corrections that landed on past days.
    Sequential per shop, day-by-day. shop_lock acquired per day inside
    _refresh_finance_for_shop_day so the loop plays nicely with the
    hourly tick if they overlap.
    """
    import time as _t

    def _next_run_target() -> datetime:
        now = _now_app_tz()
        target = now.replace(hour=0, minute=30, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        return target

    next_run = _next_run_target()
    while True:
        try:
            now = _now_app_tz()
            sleep_seconds = (next_run - now).total_seconds()
            if sleep_seconds > 0:
                _t.sleep(sleep_seconds)

            today_tashkent = _now_app_tz().date()
            window_start = today_tashkent - timedelta(days=FINANCE_REFRESH_DAYS)
            window_end = today_tashkent - timedelta(days=1)
            print(f"[FinanceNightly] tick window=[{window_start}, {window_end}]")

            shops = _active_shop_ids_for_sales()
            for s in shops:
                cur = window_start
                while cur <= window_end:
                    try:
                        n = _refresh_finance_for_shop_day(s, cur)
                        print(f"[FinanceNightly] shop={s} day={cur} rows={n}")
                    except Exception as e:
                        print(f"[FinanceNightly] shop={s} day={cur} ERROR: {e!r}")
                    cur += timedelta(days=1)

            next_run = _next_run_target()
        except Exception as e:
            print(f"[FinanceNightly] unexpected error: {e!r}")
            _t.sleep(60)
            next_run = _next_run_target()


def _owner_openapi_token_for_shop(shop_uzum_id: str | int) -> str | None:
    """Return User.uzum_openapi_token of the shop's owner, or None."""
    try:
        sid_str = str(shop_uzum_id).strip()
    except Exception:
        return None
    if not sid_str:
        return None
    with SessionLocal() as db:
        row = db.execute(
            select(User.uzum_openapi_token)
            .join(Shop, Shop.owner_id == User.id)
            .where(Shop.uzum_id == sid_str)
            .where(User.uzum_openapi_token.is_not(None))
            .limit(1)
        ).first()
    if not row:
        return None
    tok = (row[0] or "").strip()
    return tok or None


def _fetch_sales_for_shop_window(
    shop_uzum_id: str | int,
    date_from_tashkent: datetime,
    date_to_tashkent: datetime,
) -> list[dict]:
    """Single-shop sales fetch — OpenAPI primary, browser CSV fallback.

    Returns canonical-key dicts (same shape as _SELLS_HEADER_MAP output) so
    the existing _ingest_sales_lines_window function consumes both sources
    uniformly. OpenAPI rows carry the extra keys `shop_id`, `product_image`,
    `qty_cancelled` and `product_id` which the ingest writes to the new
    sales_lines columns added in migration 20260520_0004.
    """
    token = _owner_openapi_token_for_shop(shop_uzum_id)
    if token:
        from core import uzum_finance_openapi as _ufo
        try:
            rows = _ufo.fetch_finance_orders_for_shop_window(
                token, shop_uzum_id, date_from_tashkent, date_to_tashkent,
            )
            print(f"[SalesFetch] OpenAPI shop={shop_uzum_id} rows={len(rows)}")
            return rows
        except Exception as e:
            print(f"[SalesFetch] OpenAPI failed shop={shop_uzum_id}: {e!r} — falling back to CSV")
    return _fetch_sells_report_rows_for_shops(
        [int(shop_uzum_id)], date_from_tashkent, date_to_tashkent,
    )


def _fetch_sells_report_rows_for_shops(
    shop_ids: list[int],
    date_from_tashkent: datetime,
    date_to_tashkent: datetime,
) -> list[dict]:
    """Run the Uzum 4-step SELLS_REPORT flow for ``shop_ids`` and return rows.

    Used by the hourly bulk loop AND the per-shop nightly / backfill / sync
    paths (latter pass ``[shop_id]``). Concurrent same-token creates are the
    cross-wired-fileUrl race (see project_uzum_race_bug_fix.md) — callers
    must serialize / chunk. Routing to per-shop rows happens at ingest time
    via ``Variant.sku → ProductGroup.shop_id``.
    """
    from core import uzum_reports as _ur

    def _to_ms(dt: datetime) -> int:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=APP_TZ)
        return int(dt.timestamp() * 1000)

    date_from_ms = _to_ms(date_from_tashkent)
    # dateTo is exclusive on our side; Uzum treats it inclusive-ish — subtract
    # 1 ms so re-run on the same [a,b) window doesn't pull the next bucket's
    # first millisecond.
    date_to_ms = _to_ms(date_to_tashkent) - 1

    request_id = _ur.create_report(
        shop_ids,
        "SELLS_REPORT",
        date_from_ms,
        date_to_ms,
        token_getter=_get_admin_token,
    )
    file_url = _ur.wait_for_report(request_id, token_getter=_get_admin_token)
    raw = _ur.download_csv(file_url, token_getter=_get_admin_token)
    return _ur.parse_sells_csv(raw)

def _active_shop_ids_for_sales() -> list[str]:
    """Return distinct shop uzum_ids currently owned by a user."""
    with SessionLocal() as db:
        rows = db.execute(
            select(Shop.uzum_id)
            .where(Shop.uzum_id.is_not(None))
            .where(Shop.owner_id.is_not(None))
        ).all()
    seen: set[str] = set()
    out: list[str] = []
    for (uid,) in rows:
        s = str(uid or "").strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _update_shop_sync_state(shop_id_int: int, **fields) -> None:
    """Upsert a column on shop_sync_state for the given shop."""
    from models import ShopSyncState
    if not fields:
        return
    with SessionLocal() as db:
        existing = db.get(ShopSyncState, shop_id_int)
        if existing is None:
            db.add(ShopSyncState(shop_id=shop_id_int, **fields))
        else:
            for k, v in fields.items():
                setattr(existing, k, v)
        db.commit()


def _run_hourly_bulk_chunk(
    chunk_shop_ids: list[int],
    hour_start: datetime,
    hour_end: datetime,
) -> bool:
    """Hourly fetch for ``chunk_shop_ids``, per-shop with OpenAPI primary.

    Path C's bulk SELLS_REPORT was a workaround for the cross-wired-fileUrl
    race specific to the shared admin token (see project_uzum_race_bug_fix).
    Per-user OpenAPI tokens have their own rate-limit buckets per token, so
    that race doesn't apply — we can fan out per-shop concurrently here.
    Shops whose owner hasn't set an OpenAPI token transparently fall back
    to per-shop SELLS_REPORT inside ``_fetch_sales_for_shop_window``.

    Returns True if at least one shop's fetch+ingest succeeded.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    held: list[str] = []
    try:
        for sid in chunk_shop_ids:
            sid_str = str(sid)
            if shop_lock.try_acquire_shop_lock(sid_str):
                held.append(sid_str)
        if not held:
            print(f"[SalesHourly] chunk size={len(chunk_shop_ids)} skipped — no locks acquired")
            return False

        # Fan out per-shop fetches. Workers bounded so we don't stampede
        # any single owner's 2-burst rate budget if many of their shops
        # land in the same chunk.
        all_rows: list[dict] = []
        success_shops: list[str] = []

        def _one(sid_str: str):
            try:
                return sid_str, _fetch_sales_for_shop_window(sid_str, hour_start, hour_end), None
            except Exception as exc:
                return sid_str, [], exc

        max_workers = max(1, min(16, len(held)))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futs = [pool.submit(_one, s) for s in held]
            for fut in as_completed(futs):
                sid_str, rows, err = fut.result()
                if err is not None:
                    print(f"[SalesHourly] fetch ERROR shop={sid_str}: {err!r}")
                    continue
                success_shops.append(sid_str)
                all_rows.extend(rows or [])

        if not success_shops:
            print(f"[SalesHourly] chunk size={len(held)} all fetches failed")
            return False

        try:
            n = _ingest_sales_lines_window(all_rows, hour_start, hour_end)
        except Exception as e:
            print(f"[SalesHourly] chunk size={len(success_shops)} ingest ERROR: {e}")
            return False

        for sid_str in success_shops:
            try:
                _update_shop_sync_state(int(sid_str), last_hourly_at=datetime.utcnow())
            except Exception as e:
                print(f"[SalesHourly] sync_state update failed shop={sid_str}: {e}")

        print(
            f"[SalesHourly] chunk shops={len(success_shops)} "
            f"window=[{hour_start.isoformat()},{hour_end.isoformat()}) rows={n}"
        )
        return True
    finally:
        for sid_str in held:
            shop_lock.release_shop_lock(sid_str)


# Window strategy: at every HH:00 we re-fetch the FULL day-so-far
# ([00:00, HH:00)), not just the closed hour. Reason: an order fetched
# earlier today as 'В обработке' may flip to 'Отменен' later in Uzum;
# a narrow [HH-1, HH) window would never see that flip and the stale
# row would sit until the 00:30 nightly refetch (~24h drift).
# _ingest_sales_lines_window does DELETE+INSERT scoped to the window
# AND the shops present in the rows-set. Cancelled rows DO appear in
# the CSV (status='Отменен'), so the cancelled order's shop is in
# rows-set, the DELETE wipes today's stale rows for that shop, and
# INSERT proceeds with cancellations filtered out (coder rule §2).
# At the midnight tick we still wrap up the previous full day in one
# 24h window, then resume today-so-far at 01:00.
def _hourly_sales_reports_loop():
    """HH:00 Tashkent — bulk-fetch today's day-so-far for ALL active shops.

    Window semantics:
      * Mid-day tick (HH != 0): ``[today 00:00, today HH:00)`` — re-fetch
        all of today so cancellations on earlier-hour orders self-correct.
      * Midnight tick (HH == 0): ``[yesterday 00:00, today 00:00)`` — wrap
        up the previous full day's truth in one 24h window.

    Path C: replaces the per-shop ThreadPoolExecutor fan-out (cross-wired
    fileUrl race; see project_uzum_race_bug_fix.md). Issues one
    ``create_report(shop_ids=chunk)`` per chunk of ``SHARD_BY_K`` shops,
    sequentially, with ``_BULK_CREATE_BETWEEN_CHUNKS_S`` between chunks to
    dodge Uzum's 60s exact-args dedup. Per-row routing happens at ingest.
    Status drift is corrected by ``_ingest_sales_lines_window``'s
    DELETE+INSERT, scoped to (window, shops-in-rows-set).
    """
    import time as _t

    next_hour = _now_app_tz().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

    while True:
        try:
            now = _now_app_tz()
            sleep_seconds = (next_hour - now).total_seconds()
            if sleep_seconds > 0:
                _t.sleep(sleep_seconds)

            while _now_app_tz() >= next_hour:
                snap_hour_tz = next_hour  # save TZ-aware boundary BEFORE we mutate
                hour_end = next_hour.replace(tzinfo=None)
                if hour_end.hour == 0:
                    # Midnight tick — wrap up the previous full day.
                    hour_start = hour_end - timedelta(days=1)
                else:
                    # Mid-day tick — re-fetch from today 00:00 so cancellations
                    # on earlier-hour orders get caught (status drift correction).
                    hour_start = datetime.combine(hour_end.date(), dt_time(0, 0, 0))
                shops = _active_shop_ids_for_sales()
                bulk_ok = True
                if shops:
                    try:
                        # Convert to ints, drop unparseable.
                        shop_ints: list[int] = []
                        for s in shops:
                            try:
                                shop_ints.append(int(str(s).strip()))
                            except (TypeError, ValueError):
                                continue

                        # Chunk into SHARD_BY_K groups. Sequential — never concurrent
                        # under the same token (race condition).
                        chunks = [
                            shop_ints[i:i + SHARD_BY_K]
                            for i in range(0, len(shop_ints), SHARD_BY_K)
                        ]
                        print(
                            f"[SalesHourly] bulk window=[{hour_start.isoformat()},"
                            f"{hour_end.isoformat()}) shops={len(shop_ints)} "
                            f"chunks={len(chunks)} shard_by_k={SHARD_BY_K}"
                        )
                        for idx, chunk in enumerate(chunks):
                            if not _run_hourly_bulk_chunk(chunk, hour_start, hour_end):
                                bulk_ok = False
                            if idx < len(chunks) - 1:
                                _t.sleep(_BULK_CREATE_BETWEEN_CHUNKS_S)
                    except Exception as e:
                        bulk_ok = False
                        print(
                            f"[SalesHourly] bulk fetch failed for "
                            f"{hour_end.isoformat()}: {e!r}"
                        )

                # Dispatch the per-user Telegram notification for this closed hour.
                # Folded in here (was a separate _hourly_sales_loop thread) so the
                # TG read happens AFTER the bulk INSERT completes — fixes the
                # HH:00 race where TG reported stale/empty sales_lines.
                # Gated on bulk_ok: if bulk failed, sales_lines holds prior data
                # (or nothing); dispatching would re-send stale numbers, which is
                # the exact bug we're fixing. Skip TG for that hour and log.
                # Pass the TZ-aware boundary — _run_scheduled_hourly_sales_check
                # uses .hour for per-user gating (smoke-test verified).
                if bulk_ok:
                    try:
                        _run_scheduled_hourly_sales_check(snap_hour_tz)
                    except Exception as e:
                        print(
                            f"[SalesHourly] Telegram dispatch failed for "
                            f"{snap_hour_tz.isoformat()}: {e!r}"
                        )
                else:
                    print(
                        f"[SalesHourly] skipping Telegram dispatch for "
                        f"{snap_hour_tz.isoformat()} — bulk fetch failed"
                    )
                next_hour += timedelta(hours=1)
        except Exception as e:
            print(f"[SalesHourly] Unexpected error: {e}")
            _t.sleep(60)


def _run_nightly_refetch_for_shop(shop_id: str, today_tashkent: date) -> None:
    """ONE API call per shop for [today-45d, today-1d]. NOT chunked (coder rule §6).

    Path C: single-shop is just a 1-element bulk call — same code path. The
    nightly loop already runs shops sequentially (see ``_nightly_refetch_loop``),
    so the same-token race is not triggered.
    """
    if not shop_lock.try_acquire_shop_lock(shop_id):
        return
    try:
        start_day = today_tashkent - timedelta(days=FINANCE_REFRESH_DAYS)
        end_day = today_tashkent - timedelta(days=1)
        window_from = datetime.combine(start_day, dt_time(0, 0, 0))
        # End-of-day boundary for end_day — [start_day 00:00, today 00:00).
        window_to = datetime.combine(today_tashkent, dt_time(0, 0, 0))
        rows = _fetch_sales_for_shop_window(shop_id, window_from, window_to)
        n = _ingest_sales_lines_window(rows, window_from, window_to)
        _update_shop_sync_state(int(shop_id), last_nightly_refetch_at=datetime.utcnow())
        print(f"[SalesNightly] shop={shop_id} window=[{window_from.date()},{end_day}] rows={n}")
    except Exception as e:
        print(f"[SalesNightly] shop={shop_id} ERROR: {e}")
    finally:
        shop_lock.release_shop_lock(shop_id)


def _nightly_refetch_loop():
    """00:30 Tashkent — per shop, ONE API call covering the full 45-day range."""
    import time as _t

    def _next_run() -> datetime:
        now = _now_app_tz()
        target = now.replace(hour=0, minute=30, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        return target

    next_run = _next_run()
    while True:
        try:
            now = _now_app_tz()
            sleep_seconds = (next_run - now).total_seconds()
            if sleep_seconds > 0:
                _t.sleep(sleep_seconds)

            today_tashkent = _now_app_tz().date()
            for s in _active_shop_ids_for_sales():
                # Run sequentially — nightly is big (~5MB CSV) and we don't
                # want to stampede Uzum. Each call still hits shop_lock.
                _run_nightly_refetch_for_shop(s, today_tashkent)
            next_run = _next_run()
        except Exception as e:
            print(f"[SalesNightly] Unexpected error: {e}")
            _t.sleep(60)


def _ingest_expenses_window_for_shop(
    shop_id: str,
    date_from_tashkent: datetime,
    date_to_tashkent: datetime,
) -> int:
    """Fetch expenses for [from, to) Tashkent and UPSERT on (shop_id, operation_id).

    Source selection:
      * If the shop's owner has a `uzum_openapi_token`, fetch from
        /v1/finance/expenses (per-user OpenAPI) and persist the extra
        OpenAPI-only columns (date_created, date_updated, seller_id,
        external_id, code) alongside the browser-matched fields.
      * Otherwise fall back to the legacy EXPENSES_REPORT CSV pipeline.

    Stores ALL rows including Логистика and Возврат (coder rule §3).
    ``amount`` stays positive — direction lives in ``op_type`` (coder rule §10).
    """
    from decimal import Decimal, InvalidOperation
    from models import ExpensesLedger

    if date_from_tashkent >= date_to_tashkent:
        return 0

    shop_id_int = int(shop_id)

    # ── 1. Pick the source. OpenAPI primary, CSV fallback. ────────
    rows: list[dict] = []
    source_label = "csv"
    token = _owner_openapi_token_for_shop(shop_id)
    if token:
        from core import uzum_finance_openapi as _ufo
        try:
            rows = _ufo.fetch_finance_expenses_for_shop_window(
                token, shop_id_int, date_from_tashkent, date_to_tashkent,
            )
            source_label = "openapi"
        except Exception as e:
            print(f"[ExpensesIngest] OpenAPI failed shop={shop_id}: {e!r} — falling back to CSV")
            rows = []
            source_label = "csv"

    if source_label == "csv":
        from core import uzum_reports as _ur

        def _to_ms(dt: datetime) -> int:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=APP_TZ)
            return int(dt.timestamp() * 1000)

        request_id = _ur.create_report(
            [shop_id_int],
            "EXPENSES_REPORT",
            _to_ms(date_from_tashkent),
            _to_ms(date_to_tashkent) - 1,
            token_getter=_get_admin_token,
        )
        file_url = _ur.wait_for_report(request_id, token_getter=_get_admin_token)
        raw = _ur.download_csv(file_url, token_getter=_get_admin_token)
        rows = _ur.parse_expenses_csv(raw)

    # ── 2. Coercion helpers — accept str (CSV) or already-typed (OpenAPI). ──

    def _parse_dt(val) -> datetime | None:
        if val is None:
            return None
        if isinstance(val, datetime):
            return val
        s = str(val).strip()
        if not s:
            return None
        for fmt in (
            "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y",
            "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
        ):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        return None

    def _parse_int(val) -> int:
        if val is None:
            return 0
        if isinstance(val, int):
            return val
        s = str(val).strip().replace(" ", "").replace("\u00a0", "").replace(",", ".")
        if not s:
            return 0
        try:
            return int(float(s))
        except (ValueError, InvalidOperation):
            return 0

    def _parse_dec(val) -> Decimal:
        if val is None:
            return Decimal("0")
        if isinstance(val, Decimal):
            return val
        if isinstance(val, (int, float)):
            return Decimal(str(val))
        s = str(val).strip().replace(" ", "").replace("\u00a0", "").replace(",", ".")
        if not s:
            return Decimal("0")
        try:
            return Decimal(s)
        except (ValueError, InvalidOperation):
            return Decimal("0")

    now_utc = datetime.utcnow()
    n = 0
    # UPSERT one-by-one: volume per (shop, window) is small. ON CONFLICT
    # would be marginally faster but adds complexity not worth the win here.
    with SessionLocal() as db:
        with db.begin():
            for r in rows:
                op_id = str(r.get("operation_id") or "").strip()
                if not op_id:
                    continue
                charged = _parse_dt(r.get("charged_at"))
                if charged is None:
                    continue
                amount = _parse_dec(r.get("amount"))
                # amount in DB must stay non-negative (coder rule §10).
                if amount < 0:
                    amount = -amount
                vals = dict(
                    charged_at=charged,
                    day=charged.date(),
                    source=(str(r.get("source") or ""))[:120] or None,
                    service=(str(r.get("service") or ""))[:300] or None,
                    status=(str(r.get("status") or ""))[:40] or None,
                    op_type=(str(r.get("op_type") or ""))[:40] or None,
                    unit_cost=_parse_dec(r.get("unit_cost")),
                    qty=_parse_int(r.get("qty")),
                    amount=amount,
                    # New OpenAPI-only fields (None for CSV rows that
                    # don't supply them).
                    date_created=_parse_dt(r.get("date_created")),
                    date_updated=_parse_dt(r.get("date_updated")),
                    seller_id=(_parse_int(r.get("seller_id")) or None) if r.get("seller_id") is not None else None,
                    external_id=(str(r.get("external_id") or ""))[:120] or None,
                    code=(str(r.get("code") or ""))[:80] or None,
                    synced_at=now_utc,
                )
                existing = db.get(ExpensesLedger, (shop_id_int, op_id))
                if existing is None:
                    db.add(ExpensesLedger(shop_id=shop_id_int, operation_id=op_id, **vals))
                else:
                    for k, v in vals.items():
                        setattr(existing, k, v)
                n += 1
    print(f"[ExpensesIngest] shop={shop_id} src={source_label} window=["
          f"{date_from_tashkent.isoformat()},{date_to_tashkent.isoformat()}) rows={n}")
    return n


def _run_daily_expenses_for_shop(shop_id: str, target_day: date) -> None:
    if not shop_lock.try_acquire_shop_lock(shop_id):
        return
    try:
        window_from = datetime.combine(target_day, dt_time(0, 0, 0))
        window_to = datetime.combine(target_day + timedelta(days=1), dt_time(0, 0, 0))
        n = _ingest_expenses_window_for_shop(shop_id, window_from, window_to)
        _update_shop_sync_state(int(shop_id), last_expenses_at=datetime.utcnow())
        print(f"[ExpensesDaily] shop={shop_id} day={target_day} rows={n}")
    except Exception as e:
        print(f"[ExpensesDaily] shop={shop_id} ERROR: {e}")
    finally:
        shop_lock.release_shop_lock(shop_id)


def _daily_expenses_loop():
    """23:45 Tashkent — per shop, ingest today's EXPENSES_REPORT rows."""
    import time as _t

    def _next_run() -> datetime:
        now = _now_app_tz()
        target = now.replace(hour=23, minute=45, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        return target

    next_run = _next_run()
    while True:
        try:
            now = _now_app_tz()
            sleep_seconds = (next_run - now).total_seconds()
            if sleep_seconds > 0:
                _t.sleep(sleep_seconds)

            target_day = _now_app_tz().date()
            for s in _active_shop_ids_for_sales():
                _run_daily_expenses_for_shop(s, target_day)
            next_run = _next_run()
        except Exception as e:
            print(f"[ExpensesDaily] Unexpected error: {e}")
            _t.sleep(60)


def _parse_backfill_start_date() -> date:
    """Parse FINANCE_BACKFILL_START_DATE env var (YYYY-MM-DD)."""
    from config import FINANCE_BACKFILL_START_DATE as _cfg
    try:
        return datetime.strptime(str(_cfg).strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return date(2022, 1, 1)


def _enqueue_backfill_chunks_for_shop(shop_id_int: int, today_tashkent: date) -> int:
    """Seed shop_backfill_chunks rows covering [FINANCE_BACKFILL_START_DATE, today].

    Last chunk ends at ``today`` (NOT today-45d) per plan §5 rationale —
    overlap with nightly is safe (DELETE+INSERT + shop_lock). Returns the
    number of chunks newly inserted.
    """
    from config import SALES_BACKFILL_CHUNK_DAYS
    from models import ShopBackfillChunk, ShopSyncState

    start = _parse_backfill_start_date()
    if start > today_tashkent:
        return 0

    chunks: list[tuple[date, date]] = []
    cur = start
    while cur <= today_tashkent:
        end = min(cur + timedelta(days=SALES_BACKFILL_CHUNK_DAYS - 1), today_tashkent)
        chunks.append((cur, end))
        cur = end + timedelta(days=1)

    added = 0
    with SessionLocal() as db:
        with db.begin():
            existing_rows = db.execute(
                select(ShopBackfillChunk.chunk_start, ShopBackfillChunk.chunk_end)
                .where(ShopBackfillChunk.shop_id == shop_id_int)
            ).all()
            existing = {(cs, ce) for cs, ce in existing_rows}
            for cs, ce in chunks:
                if (cs, ce) in existing:
                    continue
                db.add(ShopBackfillChunk(
                    shop_id=shop_id_int,
                    chunk_start=cs,
                    chunk_end=ce,
                    status="pending",
                ))
                added += 1

            state = db.get(ShopSyncState, shop_id_int)
            if state is None:
                db.add(ShopSyncState(
                    shop_id=shop_id_int,
                    backfill_status="running" if added else "done",
                ))
    return added


def _claim_next_backfill_chunk():
    """Lock and return one pending chunk via SELECT ... FOR UPDATE SKIP LOCKED.

    Returns the row dict (or None). Caller must mark status=done/failed in a
    later transaction and refresh last_attempt_at + attempts.
    """
    with SessionLocal() as db:
        with db.begin():
            row = db.execute(
                text(
                    "SELECT shop_id, chunk_start, chunk_end, attempts "
                    "FROM shop_backfill_chunks "
                    "WHERE status = 'pending' "
                    "ORDER BY shop_id, chunk_start "
                    "LIMIT 1 "
                    "FOR UPDATE SKIP LOCKED"
                )
            ).first()
            if row is None:
                return None
            db.execute(
                text(
                    "UPDATE shop_backfill_chunks SET status='running', "
                    "attempts = attempts + 1, last_attempt_at = :now "
                    "WHERE shop_id=:sid AND chunk_start=:cs AND chunk_end=:ce"
                ),
                {
                    "now": datetime.utcnow(),
                    "sid": row.shop_id,
                    "cs": row.chunk_start,
                    "ce": row.chunk_end,
                },
            )
            return {
                "shop_id": row.shop_id,
                "chunk_start": row.chunk_start,
                "chunk_end": row.chunk_end,
                "attempts": row.attempts + 1,
            }


def _mark_backfill_chunk(shop_id_int: int, cs: date, ce: date, *, status: str, error: str | None = None) -> None:
    with SessionLocal() as db:
        with db.begin():
            db.execute(
                text(
                    "UPDATE shop_backfill_chunks SET status=:st, last_error=:err, "
                    "last_attempt_at=:now "
                    "WHERE shop_id=:sid AND chunk_start=:cs AND chunk_end=:ce"
                ),
                {
                    "st": status,
                    "err": (error or "")[:500] or None,
                    "now": datetime.utcnow(),
                    "sid": shop_id_int,
                    "cs": cs,
                    "ce": ce,
                },
            )


def _onboarding_backfill_loop():
    """Watch for shops without backfill, enqueue 60-day chunks, drain them.

    Chunks are drained via SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1 so two
    worker processes never claim the same chunk. Each drained chunk runs the
    same _ingest_sales_lines_window path as hourly / nightly.
    """
    import time as _t
    from models import ShopSyncState

    while True:
        try:
            today_tashkent = _now_app_tz().date()
            # 1. Seed chunks for any new shop that has no sync_state yet.
            shop_ids = _active_shop_ids_for_sales()
            with SessionLocal() as db:
                have_state = {
                    r[0] for r in db.execute(select(ShopSyncState.shop_id)).all()
                }
            for s in shop_ids:
                try:
                    sid = int(s)
                except (TypeError, ValueError):
                    continue
                if sid in have_state:
                    continue
                added = _enqueue_backfill_chunks_for_shop(sid, today_tashkent)
                if added:
                    print(f"[SalesBackfill] shop={sid} enqueued {added} chunks")

            # 2. Drain one chunk per tick.
            chunk = _claim_next_backfill_chunk()
            if chunk is None:
                _t.sleep(30)
                continue

            sid = int(chunk["shop_id"])
            cs = chunk["chunk_start"]
            ce = chunk["chunk_end"]
            shop_id_str = str(sid)

            if not shop_lock.try_acquire_shop_lock(shop_id_str):
                # Some other loop owns it — mark pending again, try later.
                _mark_backfill_chunk(sid, cs, ce, status="pending")
                _t.sleep(5)
                continue
            try:
                window_from = datetime.combine(cs, dt_time(0, 0, 0))
                window_to = datetime.combine(ce + timedelta(days=1), dt_time(0, 0, 0))
                rows = _fetch_sales_for_shop_window(
                    shop_id_str, window_from, window_to
                )
                n = _ingest_sales_lines_window(rows, window_from, window_to)
                _mark_backfill_chunk(sid, cs, ce, status="done")
                print(f"[SalesBackfill] shop={sid} chunk=[{cs},{ce}] rows={n}")
            except Exception as e:
                _mark_backfill_chunk(sid, cs, ce, status="failed", error=str(e))
                print(f"[SalesBackfill] shop={sid} chunk=[{cs},{ce}] ERROR: {e}")
            finally:
                shop_lock.release_shop_lock(shop_id_str)
        except Exception as e:
            print(f"[SalesBackfill] Unexpected error: {e}")
            _t.sleep(60)


# Background thread startup extracted into background/startup.py

# Telegram auth routes extracted into telegram/routes.py


def _has_any_users() -> bool:
    with SessionLocal() as db:
        return db.execute(select(User.id)).first() is not None


# ── Finance data sync & query ────────────────────────────────────────

# Finance page route extracted into finance/routes.py


# ─────────────────────────────────────────────────────────────────────
# Phase 1 — sales_lines ingest (SELLS_REPORT group=false).
#
# Shared by the hourly, nightly-refetch, and backfill loops — same code
# path, different [from, to) window. Sole writer to sales_lines now that
# the legacy /finance/orders pipeline has been retired (step 4).
# ─────────────────────────────────────────────────────────────────────

def _ingest_sales_lines_window(
    rows: list[dict],
    date_from_tashkent: datetime,
    date_to_tashkent: datetime,
) -> int:
    """Ingest pre-parsed SELLS_REPORT rows for [from, to) Tashkent.

    Path C: rows may originate from a bulk create covering many shops.
    Routing is done per-row via a ``Variant.sku → ProductGroup.shop_id``
    catalog dict (NOT ``Variant.uzum_sku_id`` — see project_uzum_race_bug_fix.md
    correction 2026-04-21: CSV's "SKU" column is the seller code).

    Flow:
      1. Build sku→shop and sku→product_id dicts ONCE (per call, not per row).
      2. DROP rows where status == "Отменен" BEFORE INSERT (coder rule §2).
      3. Per-row route: ``shop_id = sku_to_shop.get(sku_id)``. If ``None``
         (unknown SKU), skip + count for the WARN log; never invent a shop.
      4. DELETE + bulk INSERT inside ONE ``with session.begin():`` block
         (coder rule §9). DELETE scopes to the shops that actually appeared
         in this window's rows so we stay idempotent without touching shops
         the caller didn't intend to refresh.

    Caller is responsible for create → poll → download → parse. Move that
    work outside so concurrent same-token creates can be batched into one
    bulk request (cross-wired ``fileUrl`` race; see memory).

    Returns the number of rows INSERTed (post-filter).
    """
    import logging as _logging
    from decimal import Decimal, InvalidOperation
    from models import SalesLine

    _logger = _logging.getLogger(__name__)

    if date_from_tashkent >= date_to_tashkent:
        return 0

    rows = rows or []

    def _parse_dt(val) -> datetime | None:
        """Parse 'YYYY-MM-DD HH:MM:SS' style CSV timestamp to naive Tashkent."""
        if not val:
            return None
        s = str(val).strip()
        if not s:
            return None
        for fmt in (
            "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y",
            "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
        ):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        return None

    def _parse_int(val) -> int:
        if val is None:
            return 0
        s = str(val).strip().replace(" ", "").replace("\u00a0", "")
        if not s:
            return 0
        # CSV may use "," as decimal separator; treat as decimal then truncate.
        s = s.replace(",", ".")
        try:
            return int(float(s))
        except (ValueError, InvalidOperation):
            return 0

    def _parse_dec(val) -> Decimal:
        if val is None:
            return Decimal("0")
        s = str(val).strip().replace(" ", "").replace("\u00a0", "").replace(",", ".")
        if not s:
            return Decimal("0")
        try:
            return Decimal(s)
        except (ValueError, InvalidOperation):
            return Decimal("0")

    # ── 1. Build sku→shop and sku→product_id dicts ONCE (per call) ─
    # IMPORTANT: route on Variant.sku (the seller code, VARCHAR), NOT
    # Variant.uzum_sku_id (Uzum internal numeric id). The CSV's "SKU" column
    # holds the seller code — verified 2026-04-21 dry-run.
    sku_to_shop: dict[str, int] = {}
    product_by_sku: dict[str, int] = {}
    with SessionLocal() as db:
        catalog_rows = db.execute(
            select(Variant.sku, Shop.uzum_id, ProductGroup.uzum_product_id)
            .join(ProductGroup, Variant.group_id == ProductGroup.id)
            .join(Shop, ProductGroup.shop_id == Shop.id)
            .where(Variant.sku.isnot(None))
            .where(Shop.uzum_id.isnot(None))
        ).all()
    for sku_raw, uzum_id_raw, pid_raw in catalog_rows:
        sku_key = str(sku_raw or "").strip()
        if not sku_key:
            continue
        try:
            sku_to_shop[sku_key] = int(str(uzum_id_raw).strip())
        except (TypeError, ValueError):
            continue
        try:
            pid_int = int(str(pid_raw).strip()) if pid_raw is not None else None
        except (TypeError, ValueError):
            pid_int = None
        if pid_int is not None:
            product_by_sku[sku_key] = pid_int

    # ── 2. Drop Отменен, validate, route, build payload ───────────
    n_in = len(rows)
    n_dropped_cancelled = 0
    n_skipped_unknown_sku = 0
    n_skipped_malformed = 0

    now_utc = datetime.utcnow()  # infra timestamp — naive UTC (coder rule §8).
    payload: list[dict] = []

    for r in rows:
        status = (r.get("status") or "").strip()
        if status == "Отменен":
            n_dropped_cancelled += 1
            continue

        order_id = (r.get("order_id") or "").strip()
        sku_key = str(r.get("sku_id") or "").strip()
        # `created_at` may already be a datetime (OpenAPI normalizer pre-parses)
        # or a string (CSV path). Accept either.
        raw_created = r.get("created_at")
        created = raw_created if isinstance(raw_created, datetime) else _parse_dt(raw_created)
        if not order_id or not sku_key or created is None:
            n_skipped_malformed += 1
            continue  # drop malformed rows; Uzum occasionally returns blanks.

        # ── 3. Per-row routing ────────────────────────────────────
        # OpenAPI rows carry shop_id directly. The browser CSV doesn't, so
        # we fall back to the SKU→shop catalog dict built above.
        row_shop_id = r.get("shop_id")
        if row_shop_id is not None:
            try:
                shop_id_int = int(row_shop_id)
            except (TypeError, ValueError):
                shop_id_int = None
        else:
            shop_id_int = sku_to_shop.get(sku_key)
        if shop_id_int is None:
            n_skipped_unknown_sku += 1
            continue

        # received_at may also be a datetime (OpenAPI) or string (CSV).
        raw_received = r.get("received_at")
        received = (
            raw_received if isinstance(raw_received, datetime)
            else _parse_dt(raw_received)
        )

        # product_id: OpenAPI surfaces it directly; CSV path resolves via catalog.
        raw_pid = r.get("product_id")
        try:
            row_pid = int(raw_pid) if raw_pid is not None else product_by_sku.get(sku_key)
        except (TypeError, ValueError):
            row_pid = product_by_sku.get(sku_key)

        # New OpenAPI-only fields (None / 0 for CSV-sourced rows).
        prod_image = r.get("product_image")
        if not isinstance(prod_image, dict):
            prod_image = None

        payload.append({
            "shop_id": shop_id_int,
            "order_id": order_id,
            "sku_id": sku_key,
            "sku_title": (r.get("sku_title") or "")[:500] or None,
            "barcode": (r.get("barcode") or "")[:120] or None,
            "category": (r.get("category") or "")[:300] or None,
            "product_id": row_pid,
            "status": (status or "")[:40] or None,
            "created_at": created,            # naive Tashkent — verbatim CSV.
            "received_at": received,
            "qty": _parse_int(r.get("qty")),
            "qty_returns": _parse_int(r.get("qty_returns")),
            "revenue": _parse_dec(r.get("revenue")),
            "seller_profit": _parse_dec(r.get("seller_profit")),
            "commission": _parse_dec(r.get("commission")),
            "unit_price": _parse_dec(r.get("unit_price")),
            "promo_amount": _parse_dec(r.get("promo_amount")),
            "purchase_price": _parse_dec(r.get("purchase_price")),
            "logistics_fee": _parse_dec(r.get("logistics_fee")),
            "product_image": prod_image,
            "qty_cancelled": _parse_int(r.get("qty_cancelled")),
            "synced_at": now_utc,
        })

    # ── 4. DELETE + INSERT in ONE transaction (coder rule §9) ─────
    # Scope DELETE to shops that actually appear in this batch's payload, so
    # idempotent re-runs don't nuke unrelated shops on a partial-coverage call.
    shops_in_payload: set[int] = {row["shop_id"] for row in payload}
    from models import SalesLine as _SalesLine
    with SessionLocal() as db:
        with db.begin():
            if shops_in_payload:
                db.execute(
                    delete(_SalesLine).where(
                        _SalesLine.shop_id.in_(list(shops_in_payload)),
                        _SalesLine.created_at >= date_from_tashkent,
                        _SalesLine.created_at < date_to_tashkent,
                    )
                )
            if payload:
                db.execute(insert(_SalesLine), payload)

    if n_skipped_unknown_sku:
        _logger.warning(
            "uzum sales ingest unknown_sku skipped window=[%s,%s) rows_in=%s "
            "written=%s skipped_unknown_sku=%s dropped_cancelled=%s skipped_malformed=%s",
            date_from_tashkent.isoformat(), date_to_tashkent.isoformat(),
            n_in, len(payload), n_skipped_unknown_sku,
            n_dropped_cancelled, n_skipped_malformed,
        )
    print(
        f"[SalesIngest] window=[{date_from_tashkent.isoformat()},"
        f"{date_to_tashkent.isoformat()}) rows_in={n_in} written={len(payload)} "
        f"skipped_unknown_sku={n_skipped_unknown_sku} "
        f"dropped_cancelled={n_dropped_cancelled} "
        f"skipped_malformed={n_skipped_malformed}"
    )

    return len(payload)


# Finance reporting routes extracted into finance/routes.py

# ----------------------------
# POS (Point of Sale)
# ----------------------------
# POS routes extracted into pos/routes.py

def _discover_seller_shops(api_key: str) -> list[str]:
    """
    Fetch /api/seller/product to discover all shopIds the seller actually has products in.
    Returns a list of unique shop uzum_id strings.
    """
    auth = f"Bearer {api_key}" if not api_key.startswith("Bearer ") else api_key
    headers = {"Authorization": auth, "Origin": "https://seller.uzum.uz", "Referer": "https://seller.uzum.uz/"}
    shop_ids: set[str] = set()
    page = 0
    total_amount = None
    collected = 0
    while True:
        url = f"https://api-seller.uzum.uz/api/seller/product?page={page}&size=100"
        try:
            raw = http_json(url, headers=headers)
        except Exception as e:
            print(f"[Discover] /api/seller/product page {page} error: {e}")
            break
        payload = raw.get("payload") or {}
        products = payload.get("products") or []
        if total_amount is None:
            total_amount = payload.get("totalProductAmount") or 0
            print(f"[Discover] totalProductAmount={total_amount}")
        for p in products:
            sid = str(p.get("shopId") or "").strip()
            if sid and sid != "0":
                shop_ids.add(sid)
        collected += len(products)
        if not products or (total_amount and collected >= total_amount):
            break
        page += 1
    print(f"[Discover] shops found: {shop_ids}")
    return list(shop_ids)


def _sync_all_seller_shops() -> dict:
    """
    Full sync for all shops using getProducts endpoint.
    Discovers all shop IDs, then syncs each shop via _sync_products_for_shop.
    """
    api_key = _get_admin_token()
    if not api_key:
        raise RuntimeError("Uzum token not configured.")

    shop_ids = _discover_seller_shops(api_key)
    if not shop_ids:
        raise RuntimeError("No shops found. Check your token.")

    print(f"[SyncAll] Found {len(shop_ids)} shops: {shop_ids}")
    total_variants = 0
    total_products = 0
    total_active = 0
    per_shop = {}

    for sid in shop_ids:
        try:
            result = _sync_products_for_shop(sid)
            per_shop[sid] = result
            total_variants += result.get("fetched", 0)
            total_products += result.get("total_products", 0)
            total_active += result.get("active_groups", 0)
        except Exception as e:
            per_shop[sid] = {"error": str(e)}
            print(f"[SyncAll] Error syncing shop {sid}: {e}")

    print(f"[SyncAll] Done: {len(shop_ids)} shops, {total_products} products, {total_variants} variants")

    return {
        "shops_found": shop_ids,
        "products_mapped": total_products,
        "variants_fetched": total_variants,
        "fetched": total_variants,
        "active_groups": total_active,
        "per_shop": per_shop,
    }


def _sync_products_for_shop(shop_uzum_id: str, size: int = 100,
                             sync_all: bool = True, max_pages: int = 500,
                             skip_pass2: bool = False) -> dict:
    """
    Single-endpoint sync using /api/seller/shop/{shopId}/product/getProducts.
    Fetches all products + nested SKUs in one loop. No separate steps needed.
    Finance data (avg_daily_sales, purchase_price, sell_price_uzum) still from finance API.
    """
    api_key = _get_admin_token()
    if not api_key:
        raise RuntimeError("Uzum token not configured.")

    auth = f"Bearer {api_key}" if not api_key.startswith("Bearer ") else api_key
    headers = {"Authorization": auth, "Origin": "https://seller.uzum.uz", "Referer": "https://seller.uzum.uz/"}

    # Ensure Shop record exists
    with SessionLocal() as db:
        shop_obj = db.execute(
            select(Shop).where(Shop.uzum_id == shop_uzum_id)
        ).scalar_one_or_none()
        is_new_shop = shop_obj is None
        if is_new_shop:
            shop_obj = Shop(uzum_id=shop_uzum_id,
                            name=f"Shop {shop_uzum_id}",
                            owner_id=None)
            db.add(shop_obj)
            db.commit()
            db.refresh(shop_obj)
        current_shop_pk = shop_obj.id

    if is_new_shop:
        import threading as _t
        def _seed(uzum_id=shop_uzum_id, pk=current_shop_pk):
            try:
                _sync_finance_for_shop(uzum_id, pk)
            except Exception as _e:
                print(f"[FetchSync] Finance seed (variants) failed for {uzum_id}: {_e}")
        _t.Thread(target=_seed, daemon=True).start()

    # Fetch finance sales map (avg_daily_sales, purchase_price, sell_price come from here)
    sales_map = None
    try:
        sales_map = fetch_finance_sales_map(shop_uzum_id, api_key=api_key)
    except Exception as e:
        print(f"[Sync] finance fetch error (non-fatal): {e}")

    # ── Page through getProducts endpoint ────────────────────────────────────────
    print(f"[Sync] Fetching products for shop {shop_uzum_id} via getProducts ...")
    page = 0
    total_products_amount = None
    product_counter = 0
    total_variants = 0
    active_group_ids: set[int] = set()

    with SessionLocal() as db:
        while True:
            url = (f"https://api-seller.uzum.uz/api/seller/shop/{shop_uzum_id}"
                   f"/product/getProducts?page={page}&size={size}"
                   f"&filter=ALL&sortBy=id&order=descending")
            try:
                raw = http_json(url, headers=headers)
            except Exception as e:
                print(f"[Sync] getProducts page {page} error: {e}")
                break

            products = raw.get("productList") or []
            if total_products_amount is None:
                total_products_amount = raw.get("totalProductsAmount") or 0
                print(f"[Sync] totalProductsAmount={total_products_amount}")

            if not products:
                break

            for p in products:
                prod_id = str(p.get("productId") or "").strip()
                if not prod_id or prod_id in ("0", "0.0"):
                    continue

                product_counter += 1
                title = p.get("title") or f"Product {prod_id}"
                image = p.get("image") or p.get("previewImg") or None
                if image and "images.uzum.uz" in image and "/t_" not in image:
                    image = image.rstrip("/") + "/t_product_540_high.jpg"
                p_category = p.get("category") or None
                # Check product status from API (e.g. ARCHIVE, BLOCKED, etc.)
                p_status_obj = p.get("status") or {}
                p_status_val = (p_status_obj.get("value")
                                if isinstance(p_status_obj, dict)
                                else str(p_status_obj or "")).upper()
                p_is_archived = p_status_val in ("ARCHIVE", "ARCHIVED", "BLOCKED", "REMOVED")

                # ── Product-level fields ─────────────────────────────────────
                p_viewers = p.get("viewers")
                p_conversion = p.get("conversion")
                p_roi = p.get("roi")                    # string like "308.89..."
                p_rating = p.get("rating")              # string like "4.6"
                p_feedback_qty = p.get("feedbackQuantity")
                # commission: product-level `commission` is often null,
                # use commissionDto.minCommission instead
                commission_dto = p.get("commissionDto") or {}
                p_commission = (commission_dto.get("minCommission")
                                or commission_dto.get("maxCommission")
                                or p.get("commission"))
                # rank from product-level rankInfo
                p_rank_info = p.get("rankInfo") or {}
                p_rank = p_rank_info.get("rank") or p_rank_info.get("rankValue") or None

                # ── Upsert ProductGroup ──────────────────────────────────────
                group = db.execute(
                    select(ProductGroup).where(
                        ProductGroup.uzum_product_id == prod_id,
                        ProductGroup.shop_id == current_shop_pk,
                    )
                ).scalar_one_or_none()

                if group is None:
                    group = ProductGroup(
                        uzum_product_id=prod_id,
                        name=title,
                        image_url=image,
                        shop_id=current_shop_pk,
                        is_archived=p_is_archived,
                        uzum_sort_order=product_counter,
                    )
                    db.add(group)
                    db.flush()
                else:
                    group.name = title
                    group.uzum_sort_order = product_counter
                    group.is_archived = p_is_archived
                    if image:
                        group.image_url = image

                if p_category is not None:
                    group.category = p_category
                if p_viewers is not None:
                    try: group.viewers = int(p_viewers)
                    except Exception: pass
                if p_conversion is not None:
                    try: group.conversion = float(p_conversion)
                    except Exception: pass
                if p_roi is not None:
                    try: group.roi = float(p_roi)
                    except Exception: pass
                if p_rating is not None:
                    try: group.rating = float(p_rating)
                    except Exception: pass
                if p_feedback_qty is not None:
                    try: group.feedback_quantity = int(p_feedback_qty)
                    except Exception: pass
                if p_commission is not None:
                    try: group.commission = int(p_commission)
                    except Exception: pass
                if p_rank is not None:
                    group.rank = str(p_rank)

                if not p_is_archived:
                    active_group_ids.add(group.id)

                # ── Process nested SKUs ──────────────────────────────────────
                # Preload all existing variants for this group to avoid N+1 DB queries
                existing_variants = db.execute(
                    select(Variant).where(Variant.group_id == group.id)
                ).scalars().all()
                _v_by_uzum_id = {v.uzum_sku_id: v for v in existing_variants if v.uzum_sku_id}
                _v_by_barcode = {v.barcode: v for v in existing_variants if v.barcode}
                _v_by_sku = {v.sku: v for v in existing_variants if v.sku}

                sku_list = p.get("skuList") or []
                for s in sku_list:
                    # Prefer skuFullTitle (e.g. "LUXUZ-RING31-ЧЕРН-17") over skuTitle ("ЧЕРН-17")
                    sku_title = str(s.get("skuFullTitle") or s.get("skuTitle") or "").strip()
                    if not sku_title:
                        continue

                    barcode = str(s.get("barcode") or "").strip() or None
                    uzum_sku_id = str(s.get("skuId") or "").strip() or None
                    uz_qty = s.get("quantityActive")
                    characteristics = str(s.get("characteristics") or "").strip() or None
                    price = s.get("price") or s.get("marketPrice")
                    sku_image = s.get("previewImage") or None
                    # Ensure image URL has proper suffix for rendering
                    if sku_image and "images.uzum.uz" in sku_image and "/t_" not in sku_image:
                        sku_image = sku_image.rstrip("/") + "/t_product_540_high.jpg"
                    # status is an object: {value: "IN_STOCK", title: "...", color: "..."}
                    status_obj = s.get("status")
                    status_val = (status_obj.get("value")
                                  if isinstance(status_obj, dict)
                                  else str(status_obj or ""))

                    # SKU-level new fields (different from product-level!)
                    s_turnover = s.get("turnover")
                    s_qty_sold = s.get("quantitySold")
                    s_qty_returned = s.get("quantityReturned")
                    s_returned_pct = s.get("returnedPercentage")
                    s_has_discount = s.get("hasActiveDiscount")
                    s_rank_info = s.get("rankInfo") or {}
                    s_rank = s_rank_info.get("rank") or s_rank_info.get("rankValue") or None
                    s_paid_storage = s.get("paidStorageAmount")
                    s_paid_dim_group = s.get("paidStorageDimensionalGroup")
                    s_paid_price_item = s.get("paidStoragePriceItem")

                    # ── Upsert Variant (match by uzum_sku_id, then barcode, then sku) ──
                    # Uses preloaded dicts to avoid N+1 DB queries
                    v = None
                    if uzum_sku_id:
                        v = _v_by_uzum_id.get(uzum_sku_id)
                    if v is None and barcode:
                        v = _v_by_barcode.get(barcode)
                    if v is None:
                        v = _v_by_sku.get(sku_title)
                    if v is None:
                        v = Variant(group_id=group.id, sku=sku_title)
                        db.add(v)
                        _v_by_sku[sku_title] = v
                    else:
                        v.sku = sku_title  # Update SKU to latest from API

                    if uzum_sku_id:
                        v.uzum_sku_id = uzum_sku_id
                        _v_by_uzum_id[uzum_sku_id] = v
                    if barcode:
                        v.barcode = barcode
                        _v_by_barcode[barcode] = v
                    if sku_image:
                        v.image_url = sku_image
                    if characteristics:
                        v.color = characteristics
                    if status_val:
                        v.status = status_val
                    if uz_qty is not None:
                        try: v.uzum_quantity = int(uz_qty)
                        except Exception: pass
                    if price is not None:
                        try: v.price_sum = int(price)
                        except Exception: pass

                    # New getProducts fields
                    if s_turnover is not None:
                        try: v.turnover = int(s_turnover)
                        except Exception: pass
                    if s_qty_sold is not None:
                        try: v.quantity_sold = int(s_qty_sold)
                        except Exception: pass
                    if s_qty_returned is not None:
                        try: v.quantity_returned = int(s_qty_returned)
                        except Exception: pass
                    if s_returned_pct is not None:
                        try: v.returned_percentage = float(s_returned_pct)
                        except Exception: pass
                    if s_has_discount is not None:
                        v.has_active_discount = bool(s_has_discount)
                    if s_rank is not None:
                        v.rank = str(s_rank)
                    if s_paid_storage is not None:
                        try: v.paid_storage_amount = int(s_paid_storage)
                        except Exception: pass
                    if s_paid_dim_group is not None:
                        v.paid_storage_dimensional_group = str(s_paid_dim_group)
                    if s_paid_price_item is not None:
                        try: v.paid_storage_price_item = int(s_paid_price_item)
                        except Exception: pass

                    # Finance data (avg_daily_sales, purchase_price, sell_price from finance API)
                    qty_val = 0
                    if sales_map is not None:
                        data_fin = sales_map.get(sku_title) or sales_map.get(sku_title.upper())
                        if data_fin is None and barcode:
                            data_fin = (sales_map.get(barcode) or
                                        sales_map.get(barcode.upper()))
                        if data_fin is None and uzum_sku_id:
                            data_fin = sales_map.get(uzum_sku_id)
                        if data_fin:
                            qty_val = data_fin.get("qty") or 0
                            v.sales_30d_finance = qty_val
                            v.avg_daily_sales = qty_val / 30.0
                            if data_fin.get("sell_price"):
                                v.sell_price_uzum = int(data_fin["sell_price"])
                            if data_fin.get("commission"):
                                v.commission_per_unit = int(data_fin["commission"])
                            if data_fin.get("logistics"):
                                v.logistics_per_unit = int(data_fin["logistics"])
                            if data_fin.get("price"):
                                v.purchase_price = int(data_fin["price"])
                        else:
                            v.sales_30d_finance = 0
                            v.avg_daily_sales = 0.0

                    db.add(v)
                    # Upsert today's VariantSale
                    if sales_map is not None:
                        db.flush()
                        _today = date.today()
                        db.execute(delete(VariantSale).where(
                            VariantSale.variant_id == v.id,
                            VariantSale.date == _today
                        ))
                        if qty_val > 0:
                            db.add(VariantSale(variant_id=v.id, date=_today, qty_sold=qty_val))
                    total_variants += 1

            db.commit()

            if len(products) < size:
                break
            if max_pages and page >= max_pages:
                break
            page += 1

    # ── Archive reconciliation ───────────────────────────────────────────────────
    print(f"[Sync] Reconciling is_archived for shop_pk={current_shop_pk} ...")
    with SessionLocal() as db:
        if active_group_ids:
            active_list = list(active_group_ids)
            db.execute(
                update(ProductGroup)
                .where(ProductGroup.id.in_(active_list))
                .values(is_archived=False)
            )
            db.execute(
                update(ProductGroup)
                .where(ProductGroup.shop_id == current_shop_pk)
                .where(~ProductGroup.id.in_(active_list))
                .values(is_archived=True)
            )
        else:
            print(f"[Sync] WARNING — no active products found for shop_pk={current_shop_pk}, "
                  f"skipping archive reconciliation")
        db.commit()

    print(f"[Sync] Done for shop {shop_uzum_id}: {product_counter} products, {total_variants} variants")

    # Populate sales_lines:
    #   1. Seed backfill chunks from FINANCE_BACKFILL_START_DATE (2022-01-01) → today.
    #      The onboarding backfill loop drains them in the background.
    #   2. Synchronously fetch the last 45 days so the economics page has recent
    #      data immediately after the button returns. shop_lock serializes with
    #      the hourly / nightly / backfill loops.
    sales_lines_rows = 0
    backfill_chunks_enqueued = 0
    try:
        _today_tashkent = _now_app_tz().date()
        try:
            backfill_chunks_enqueued = _enqueue_backfill_chunks_for_shop(
                int(shop_uzum_id), _today_tashkent
            )
            if backfill_chunks_enqueued:
                print(f"[Sync] backfill enqueued shop={shop_uzum_id} "
                      f"chunks={backfill_chunks_enqueued} (drained in background)")
        except Exception as _e:
            print(f"[Sync] backfill enqueue failed for shop={shop_uzum_id}: {_e}")

        _start = datetime.combine(_today_tashkent - timedelta(days=45), dt_time(0, 0, 0))
        _end = datetime.combine(_today_tashkent + timedelta(days=1), dt_time(0, 0, 0))
        if shop_lock.try_acquire_shop_lock(shop_uzum_id):
            try:
                sales_rows = _fetch_sales_for_shop_window(
                    shop_uzum_id, _start, _end
                )
                sales_lines_rows = _ingest_sales_lines_window(sales_rows, _start, _end)
                print(f"[Sync] sales_lines ingest shop={shop_uzum_id} rows={sales_lines_rows} "
                      f"window=[{_start}, {_end})")
            finally:
                shop_lock.release_shop_lock(shop_uzum_id)
        else:
            print(f"[Sync] sales_lines ingest skipped for shop={shop_uzum_id} — lock held by another loop")
    except Exception as _e:
        print(f"[Sync] sales_lines ingest failed for shop={shop_uzum_id}: {_e}")

    return {
        "pages_synced": page + 1,
        "fetched": total_variants,
        "active_groups": len(active_group_ids),
        "total_products": product_counter,
        "sales_lines_rows": sales_lines_rows,
        "backfill_chunks_enqueued": backfill_chunks_enqueued,
    }


def _sync_products_via_openapi(shop_uzum_id: str, openapi_token: str,
                                size: int = 100, max_pages: int = 500,
                                fetch_uz_titles: bool = True) -> dict:
    """OpenAPI-driven counterpart to :func:`_sync_products_for_shop`.

    Uses the per-user seller-openapi token (NOT the admin api_key) against
    GET /v1/product/shop/{shopId}. Populates the same Variant / ProductGroup
    rows as the browser flow, plus the new columns added in migration
    20260520_0002 (quantity_* breakdown, blocked / blocking_reason / ikpu,
    product_title_ru / _uz).

    Localization: when ``fetch_uz_titles`` is true we re-page the same shop
    a second time with ``Accept-Language: uz`` and store the title into
    ``product_title_uz``. The first pass uses ``Accept-Language: ru`` and
    populates ``product_title_ru``. The OpenAPI swagger does not document
    whether the server honors Accept-Language; if both passes return the
    same string the columns will simply mirror each other.

    Fields the OpenAPI endpoint does NOT return (viewers, conversion, roi,
    feedbackQuantity, hasActiveDiscount, product-level rankInfo) are left
    untouched on existing rows.
    """
    if not openapi_token:
        raise RuntimeError("Uzum OpenAPI token is empty.")

    # Ensure Shop record exists.
    with SessionLocal() as db:
        shop_obj = db.execute(
            select(Shop).where(Shop.uzum_id == shop_uzum_id)
        ).scalar_one_or_none()
        is_new_shop = shop_obj is None
        if is_new_shop:
            shop_obj = Shop(uzum_id=shop_uzum_id,
                            name=f"Shop {shop_uzum_id}",
                            owner_id=None)
            db.add(shop_obj)
            db.commit()
            db.refresh(shop_obj)
        current_shop_pk = shop_obj.id

    if is_new_shop:
        import threading as _t
        def _seed(uzum_id=shop_uzum_id, pk=current_shop_pk):
            try:
                _sync_finance_for_shop(uzum_id, pk)
            except Exception as _e:
                print(f"[OpenAPISync] Finance seed (variants) failed for {uzum_id}: {_e}")
        _t.Thread(target=_seed, daemon=True).start()

    print(f"[OpenAPISync] Fetching products for shop {shop_uzum_id} via OpenAPI ...")

    # ── Pass 1: Russian titles + all numeric fields ─────────────────────────────
    page = 0
    total_products_amount = None
    product_counter = 0
    total_variants = 0
    active_group_ids: set[int] = set()
    # Track uzum_sku_id → product_title for the second (UZ) pass so we don't
    # have to re-query the DB to find which Variant row to update.
    sku_id_to_variant_pk: dict[str, int] = {}

    with SessionLocal() as db:
        while True:
            try:
                raw = _openapi_fetch_products_page(
                    openapi_token, shop_uzum_id,
                    page=page, size=size,
                    accept_language="ru",
                )
            except Exception as e:
                print(f"[OpenAPISync] page {page} (ru) error: {e}")
                break

            products = raw.get("productList") or []
            if total_products_amount is None:
                total_products_amount = raw.get("totalProductsAmount") or 0
                print(f"[OpenAPISync] totalProductsAmount={total_products_amount}")

            if not products:
                break

            for p in products:
                prod_id = str(p.get("productId") or "").strip()
                if not prod_id or prod_id in ("0", "0.0"):
                    continue

                product_counter += 1
                title = p.get("title") or f"Product {prod_id}"
                image = p.get("image") or p.get("previewImg") or None
                if image and "images.uzum.uz" in image and "/t_" not in image:
                    image = image.rstrip("/") + "/t_product_540_high.jpg"
                p_category = p.get("category") or None

                p_status_obj = p.get("status") or {}
                p_status_val = (p_status_obj.get("value")
                                if isinstance(p_status_obj, dict)
                                else str(p_status_obj or "")).upper()
                p_is_archived = p_status_val in ("ARCHIVE", "ARCHIVED", "BLOCKED", "REMOVED", "DELETED", "PERM_BANNED")

                commission_dto = p.get("commissionDto") or {}
                p_commission = (commission_dto.get("minCommission")
                                or commission_dto.get("maxCommission")
                                or p.get("commission"))

                # ── Upsert ProductGroup ─────────────────────────────────────
                group = db.execute(
                    select(ProductGroup).where(
                        ProductGroup.uzum_product_id == prod_id,
                        ProductGroup.shop_id == current_shop_pk,
                    )
                ).scalar_one_or_none()

                if group is None:
                    group = ProductGroup(
                        uzum_product_id=prod_id,
                        name=title,
                        image_url=image,
                        shop_id=current_shop_pk,
                        is_archived=p_is_archived,
                        uzum_sort_order=product_counter,
                    )
                    db.add(group)
                    db.flush()
                else:
                    group.name = title
                    group.uzum_sort_order = product_counter
                    group.is_archived = p_is_archived
                    if image:
                        group.image_url = image

                if p_category is not None:
                    group.category = p_category
                if p_commission is not None:
                    try: group.commission = int(p_commission)
                    except Exception: pass
                # NOTE: OpenAPI lacks viewers/conversion/roi/feedbackQuantity
                # and product-level rankInfo — those fields keep their last
                # browser-sync values.

                if not p_is_archived:
                    active_group_ids.add(group.id)

                # ── Process nested SKUs ─────────────────────────────────────
                existing_variants = db.execute(
                    select(Variant).where(Variant.group_id == group.id)
                ).scalars().all()
                _v_by_uzum_id = {v.uzum_sku_id: v for v in existing_variants if v.uzum_sku_id}
                _v_by_barcode = {v.barcode: v for v in existing_variants if v.barcode}
                _v_by_sku = {v.sku: v for v in existing_variants if v.sku}

                sku_list = p.get("skuList") or []
                for s in sku_list:
                    sku_title = str(s.get("skuFullTitle") or s.get("skuTitle") or "").strip()
                    if not sku_title:
                        continue

                    barcode_raw = s.get("barcode")
                    barcode = str(barcode_raw).strip() if barcode_raw is not None else None
                    barcode = barcode or None
                    uzum_sku_id = str(s.get("skuId") or "").strip() or None
                    uz_qty = s.get("quantityActive")
                    characteristics = str(s.get("characteristics") or "").strip() or None
                    price = s.get("price")
                    sku_image = s.get("previewImage") or None
                    if sku_image and "images.uzum.uz" in sku_image and "/t_" not in sku_image:
                        sku_image = sku_image.rstrip("/") + "/t_product_540_high.jpg"

                    # SKU-level fields the browser sync also has
                    s_turnover = s.get("turnover")
                    s_qty_sold = s.get("quantitySold")
                    s_qty_returned = s.get("quantityReturned")
                    s_returned_pct = s.get("returnedPercentage")
                    s_rank_info = s.get("rankInfo") or {}
                    s_rank = s_rank_info.get("rank") or s_rank_info.get("rankValue") or None
                    s_paid_storage = s.get("paidStorageAmount")
                    s_paid_dim_group = s.get("paidStorageDimensionalGroup")
                    s_paid_price_item = s.get("paidStoragePriceItem")

                    # NEW OpenAPI-only fields
                    s_product_title = (s.get("productTitle") or "").strip() or None
                    s_purchase_price = s.get("purchasePrice")
                    s_avgdsales = s.get("avgdsales")
                    s_qty_created = s.get("quantityCreated")
                    s_qty_fbs = s.get("quantityFbs")
                    s_qty_additional = s.get("quantityAdditional")
                    s_qty_archived = s.get("quantityArchived")
                    s_qty_pending = s.get("quantityPending")
                    s_qty_defected = s.get("quantityDefected")
                    s_qty_missing = s.get("quantityMissing")
                    s_blocked = s.get("blocked")
                    s_blocking_reason = (s.get("blockingReason") or "").strip() or None
                    s_block_reason_obj = s.get("skuBlockReason")
                    s_ikpu = (s.get("ikpu") or "").strip() or None

                    # Upsert Variant — match by uzum_sku_id, then barcode, then sku
                    v = None
                    if uzum_sku_id:
                        v = _v_by_uzum_id.get(uzum_sku_id)
                    if v is None and barcode:
                        v = _v_by_barcode.get(barcode)
                    if v is None:
                        v = _v_by_sku.get(sku_title)
                    if v is None:
                        v = Variant(group_id=group.id, sku=sku_title)
                        db.add(v)
                        _v_by_sku[sku_title] = v
                    else:
                        v.sku = sku_title

                    if uzum_sku_id:
                        v.uzum_sku_id = uzum_sku_id
                        _v_by_uzum_id[uzum_sku_id] = v
                    if barcode:
                        v.barcode = barcode
                        _v_by_barcode[barcode] = v
                    if sku_image:
                        v.image_url = sku_image
                    if characteristics:
                        v.color = characteristics
                    # OpenAPI status object lives at product-level only,
                    # not per-SKU. Use blocked/archived booleans below.
                    if uz_qty is not None:
                        try: v.uzum_quantity = int(uz_qty)
                        except Exception: pass
                    if price is not None:
                        try: v.price_sum = int(price)
                        except Exception: pass

                    # Browser-parity fields
                    if s_turnover is not None:
                        try: v.turnover = int(float(s_turnover))
                        except Exception: pass
                    if s_qty_sold is not None:
                        try: v.quantity_sold = int(s_qty_sold)
                        except Exception: pass
                    if s_qty_returned is not None:
                        try: v.quantity_returned = int(s_qty_returned)
                        except Exception: pass
                    if s_returned_pct is not None:
                        try: v.returned_percentage = float(s_returned_pct)
                        except Exception: pass
                    if s_rank is not None:
                        v.rank = str(s_rank)
                    if s_paid_storage is not None:
                        try: v.paid_storage_amount = int(s_paid_storage)
                        except Exception: pass
                    if s_paid_dim_group is not None:
                        # OpenAPI returns an object here; stringify whatever
                        # field looks most title-like for parity with the
                        # browser flow which stored a plain string.
                        if isinstance(s_paid_dim_group, dict):
                            v.paid_storage_dimensional_group = str(
                                s_paid_dim_group.get("title")
                                or s_paid_dim_group.get("value")
                                or s_paid_dim_group.get("name")
                                or ""
                            ) or None
                        else:
                            v.paid_storage_dimensional_group = str(s_paid_dim_group)
                    if s_paid_price_item is not None:
                        try: v.paid_storage_price_item = int(s_paid_price_item)
                        except Exception: pass

                    # NEW OpenAPI-only columns
                    if s_product_title:
                        v.product_title_ru = s_product_title
                    if s_purchase_price is not None:
                        try: v.purchase_price = int(s_purchase_price)
                        except Exception: pass
                    if s_avgdsales is not None:
                        try: v.avg_daily_sales = float(s_avgdsales)
                        except Exception: pass
                    if s_qty_created is not None:
                        try: v.quantity_created = int(s_qty_created)
                        except Exception: pass
                    if s_qty_fbs is not None:
                        try: v.quantity_fbs = int(s_qty_fbs)
                        except Exception: pass
                    if s_qty_additional is not None:
                        try: v.quantity_additional = int(s_qty_additional)
                        except Exception: pass
                    if s_qty_archived is not None:
                        try: v.quantity_archived = int(s_qty_archived)
                        except Exception: pass
                    if s_qty_pending is not None:
                        try: v.quantity_pending = int(s_qty_pending)
                        except Exception: pass
                    if s_qty_defected is not None:
                        try: v.quantity_defected = int(s_qty_defected)
                        except Exception: pass
                    if s_qty_missing is not None:
                        try: v.quantity_missing = int(s_qty_missing)
                        except Exception: pass
                    if s_blocked is not None:
                        v.blocked = bool(s_blocked)
                    if s_blocking_reason:
                        v.blocking_reason = s_blocking_reason[:500]
                    if s_block_reason_obj is not None:
                        import json as _json_mod
                        try:
                            v.sku_block_reason = (
                                _json_mod.dumps(s_block_reason_obj, ensure_ascii=False)
                                if isinstance(s_block_reason_obj, (dict, list))
                                else str(s_block_reason_obj)
                            )
                        except Exception:
                            v.sku_block_reason = str(s_block_reason_obj)
                    if s_ikpu:
                        v.ikpu = s_ikpu

                    # Map archived/blocked flags into status string so the
                    # rest of the app (which reads Variant.status) keeps
                    # working.
                    if s_blocked:
                        v.status = "BLOCKED"
                    elif bool(s.get("archived")):
                        v.status = "ARCHIVED"

                    db.flush()
                    if uzum_sku_id and v.id is not None:
                        sku_id_to_variant_pk[uzum_sku_id] = v.id
                    total_variants += 1

            db.commit()

            if len(products) < size:
                break
            if max_pages and page >= max_pages:
                break
            page += 1

    # ── Pass 2: Uzbek titles only (cheap update — fills product_title_uz) ───────
    uz_pages = 0
    uz_updates = 0
    if fetch_uz_titles and sku_id_to_variant_pk:
        with SessionLocal() as db:
            page = 0
            while True:
                try:
                    raw = _openapi_fetch_products_page(
                        openapi_token, shop_uzum_id,
                        page=page, size=size,
                        accept_language="uz",
                    )
                except Exception as e:
                    print(f"[OpenAPISync] page {page} (uz) error: {e}")
                    break

                products = raw.get("productList") or []
                if not products:
                    break

                uz_pages += 1
                for p in products:
                    for s in (p.get("skuList") or []):
                        uzum_sku_id = str(s.get("skuId") or "").strip()
                        if not uzum_sku_id:
                            continue
                        variant_pk = sku_id_to_variant_pk.get(uzum_sku_id)
                        if not variant_pk:
                            continue
                        title_uz = (s.get("productTitle") or "").strip()
                        if not title_uz:
                            continue
                        db.execute(
                            update(Variant)
                            .where(Variant.id == variant_pk)
                            .values(product_title_uz=title_uz)
                        )
                        uz_updates += 1

                db.commit()
                if len(products) < size:
                    break
                if max_pages and page >= max_pages:
                    break
                page += 1

    # ── Archive reconciliation ───────────────────────────────────────────────
    print(f"[OpenAPISync] Reconciling is_archived for shop_pk={current_shop_pk} ...")
    with SessionLocal() as db:
        if active_group_ids:
            active_list = list(active_group_ids)
            db.execute(
                update(ProductGroup)
                .where(ProductGroup.id.in_(active_list))
                .values(is_archived=False)
            )
            db.execute(
                update(ProductGroup)
                .where(ProductGroup.shop_id == current_shop_pk)
                .where(~ProductGroup.id.in_(active_list))
                .values(is_archived=True)
            )
        else:
            print(f"[OpenAPISync] WARNING — no active products found for shop_pk={current_shop_pk}, "
                  f"skipping archive reconciliation")
        db.commit()

    print(f"[OpenAPISync] Done for shop {shop_uzum_id}: "
          f"{product_counter} products, {total_variants} variants, "
          f"uz_updates={uz_updates}")

    # ── sales_lines seed (mirrors the tail of _sync_products_for_shop) ──────────
    # Without this block the Finance page stays empty for the new shop until
    # the background backfill loop crawls forward to today — for a big shop
    # that's hours. Synchronously fetch the last 45 days here so the page
    # works immediately, and enqueue the chunked historical backfill so the
    # loop drains 2022 → today on its own schedule.
    sales_lines_rows = 0
    backfill_chunks_enqueued = 0
    try:
        _today_tashkent = _now_app_tz().date()
        try:
            backfill_chunks_enqueued = _enqueue_backfill_chunks_for_shop(
                int(shop_uzum_id), _today_tashkent
            )
            if backfill_chunks_enqueued:
                print(f"[OpenAPISync] backfill enqueued shop={shop_uzum_id} "
                      f"chunks={backfill_chunks_enqueued} (drained in background)")
        except Exception as _e:
            print(f"[OpenAPISync] backfill enqueue failed for shop={shop_uzum_id}: {_e}")

        _start = datetime.combine(_today_tashkent - timedelta(days=45), dt_time(0, 0, 0))
        _end = datetime.combine(_today_tashkent + timedelta(days=1), dt_time(0, 0, 0))
        if shop_lock.try_acquire_shop_lock(shop_uzum_id):
            try:
                sales_rows = _fetch_sales_for_shop_window(
                    shop_uzum_id, _start, _end
                )
                sales_lines_rows = _ingest_sales_lines_window(sales_rows, _start, _end)
                print(f"[OpenAPISync] sales_lines ingest shop={shop_uzum_id} rows={sales_lines_rows} "
                      f"window=[{_start}, {_end})")
            finally:
                shop_lock.release_shop_lock(shop_uzum_id)
        else:
            print(f"[OpenAPISync] sales_lines ingest skipped for shop={shop_uzum_id} — lock held by another loop")
    except Exception as _e:
        print(f"[OpenAPISync] sales_lines ingest failed for shop={shop_uzum_id}: {_e}")

    return {
        "pages_synced": page + 1,
        "fetched": total_variants,
        "active_groups": len(active_group_ids),
        "total_products": product_counter,
        "uz_pages_synced": uz_pages,
        "uz_titles_updated": uz_updates,
        "sales_lines_rows": sales_lines_rows,
        "backfill_chunks_enqueued": backfill_chunks_enqueued,
        "source": "openapi",
    }


def _sync_finance_for_shop(shop_uzum_id: str, shop_pk: int) -> dict:
    """Standalone finance-only sync — uses the live SELLS_REPORT pipeline.

    Single Uzum /documents/v2 call covers BOTH:
      • sales_lines for the new shop's last 30 days (so Finance page works
        immediately — closes the new-shop UX regression that would
        otherwise wait for the next HH:00 bulk).
      • Variant.avg_daily_sales / sales_30d_finance (so Warehouse / POS
        reorder logic works immediately).

    Returns {"updated": <variant rows touched>}.
    """
    today = _today_app_tz()
    window_from = datetime.combine(today - timedelta(days=30), dt_time(0, 0, 0))
    window_to   = datetime.combine(today + timedelta(days=1),  dt_time(0, 0, 0))

    try:
        shop_id_int = int(str(shop_uzum_id).strip())
    except (TypeError, ValueError):
        raise RuntimeError(f"Invalid shop uzum_id: {shop_uzum_id!r}")

    # 1. Per-shop fetch — OpenAPI primary (/v1/finance/orders), legacy
    #    SELLS_REPORT CSV pipeline as fallback. The routing wrapper
    #    handles source selection.
    rows = _fetch_sales_for_shop_window(
        shop_uzum_id, window_from, window_to,
    ) or []

    # 2. Write the rows to sales_lines so the Finance page is populated
    #    within seconds (otherwise empty until the next HH:00 hourly loop).
    #    Idempotent DELETE+INSERT scoped to this window/shop.
    try:
        _ingest_sales_lines_window(rows, window_from, window_to)
    except Exception as e:
        # Don't fail the whole onboarding if sales_lines write fails — the
        # hourly bulk will catch up at HH:00. Log and continue so Variant
        # seeding still happens.
        print(f"[ShopAdd] sales_lines seed failed for shop={shop_uzum_id}: {e!r}")

    # 3. Aggregate qty per SKU from the same parsed rows. Use sku_id (the
    #    "SKU" column from CSV — the seller code, NOT skuTitle), matching
    #    the Variant.sku field convention.
    qty_by_sku: dict[str, int] = {}
    for r in rows:
        sku = str(r.get("sku_id") or "").strip()
        if not sku:
            continue
        try:
            q = int(r.get("qty") or 0)
        except (TypeError, ValueError):
            q = 0
        if q <= 0:
            continue
        qty_by_sku[sku] = qty_by_sku.get(sku, 0) + q

    # 4. Populate Variant rows for this shop. Behavior matches legacy:
    #    look up by sku first, then barcode (uppercase fallback).
    updated = 0
    with SessionLocal() as db:
        variants = db.execute(
            select(Variant).join(ProductGroup).where(ProductGroup.shop_id == shop_pk)
        ).scalars().all()
        for v in variants:
            sku_key = (v.sku or "").strip()
            qty = qty_by_sku.get(sku_key) or qty_by_sku.get(sku_key.upper()) or 0
            if qty == 0 and v.barcode:
                bc_key = v.barcode.strip()
                qty = qty_by_sku.get(bc_key) or qty_by_sku.get(bc_key.upper()) or 0
            v.sales_30d_finance = qty
            v.avg_daily_sales = qty / 30.0
            updated += 1
        db.commit()

    return {"updated": updated}


@app.errorhandler(500)
def handle_500(e):
    import traceback
    traceback.print_exc()
    return _json_response({"error": "Internal server error", "detail": str(e)}, 500)

import background.startup as _background_mod
_background_mod.init_background_startup(__import__("sys").modules[__name__])
_background_mod.start_background_threads()


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    print(f" * Warehouse Data Page: http://{host}:{port}/warehouse/data")
    app.run(host=host, port=port, debug=True, use_reloader=False)

