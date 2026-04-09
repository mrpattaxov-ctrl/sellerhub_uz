from __future__ import annotations

import json
import uuid as _uuid
from collections import deque

# ── Imports from extracted core modules ──────────────────────────────
from config import (
    ENABLE_DEBUG_ROUTES, APP_DIR, DATA_DIR,
    DATABASE_URL, DB_POOL_SIZE, DB_MAX_OVERFLOW, DB_POOL_RECYCLE_SECONDS,
    SECRET_KEY,
    FINANCE_REFRESH_DAYS, FINANCE_AUTO_REFRESH_ENABLED, FINANCE_RECENT_DAYS,
    FINANCE_RECENT_REFRESH_HOURS, FINANCE_AUTO_REFRESH_WORKERS,
    FINANCE_AUTO_SYNC_WORKERS_PER_SHOP, FINANCE_AUTO_QUEUE_TICK_SECONDS,
    HOURLY_SALES_BURST_FETCH_ENABLED, HOURLY_SALES_BURST_FETCH_WORKERS,
    WAREHOUSE_EXPENSE_SNAPSHOT_HOUR, WAREHOUSE_EXPENSE_SNAPSHOT_MINUTE,
    APP_TIMEZONE_NAME, APP_TZ_OFFSET_HOURS, APP_TZ,
    NOTIFICATION_INTERVAL_OPTIONS, NOTIFICATION_SETTINGS_DEFAULTS,
    ONBOARD_MAX_CONCURRENT_SYNCS, BACKSTAGE_LOGIN_SESSION_KEY,
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
from core.auth_helpers import (
    _json_response, _jwt_expires_in_seconds, _get_fresh_api_key, _get_admin_token,
    _uzum_auto_login, _current_user_is_admin, admin_required, _user_shop_ids,
)
from core.subscriptions import (
    _ensure_user_trial_started,
    _get_or_create_subscription_settings,
    _get_subscription_context_for_user,
    _subscription_status_for_user,
)

import io
import os
import time
import threading
import hashlib
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
    FinanceSyncLog,
    NotificationSettings,
    ProductGroup,
    Shop,
    SubscriptionCode,
    SubscriptionCodeActivation,
    SubscriptionSettings,
    SyncJob,
    TelegramPending,
    User,
    Variant,
    VariantSale,
    WarehouseExpenseSnapshot,
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

_finance_active_lock = threading.Lock()
_finance_active_shops: set[str] = set()
_finance_queue_lock = threading.Lock()
_finance_pending_shop_ids: deque[str] = deque()
_finance_pending_jobs: dict[str, dict] = {}
_finance_last_enqueue_hour: str | None = None

# Persistent executor for auto-refresh (fire-and-forget, no global lock)
from concurrent.futures import ThreadPoolExecutor, as_completed
_finance_executor = ThreadPoolExecutor(
    max_workers=FINANCE_AUTO_REFRESH_WORKERS,
    thread_name_prefix="finance-sync",
)
_finance_stats_lock = threading.Lock()
_finance_stats = {
    "total_dispatched": 0,
    "total_completed": 0,
    "total_failed": 0,
    "last_dispatch_time": None,
    "last_dispatch_count": 0,
    "last_enqueue_time": None,
    "last_enqueue_hour": None,
    "last_enqueue_count": 0,
    "last_drain_time": None,
    "pending_queue_size": 0,
    "queue_tick_seconds": FINANCE_AUTO_QUEUE_TICK_SECONDS,
}
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

# ── Separate executor for manual / onboarding syncs ──────────────────
# Isolated from the auto-refresh executor so new-user full imports
# (2022-01-01 → today ≈ 1 500+ days) never starve hourly background jobs.
_onboard_executor = ThreadPoolExecutor(
    max_workers=ONBOARD_MAX_CONCURRENT_SYNCS,
    thread_name_prefix="onboard-sync",
)

def _serialize_sync_job(job: SyncJob) -> dict:
    return {
        "job_id": job.job_id,
        "shop_id": job.shop_id,
        "sync_type": job.sync_type,
        "status": job.status,
        "progress_days": int(job.progress_days or 0),
        "total_days": int(job.total_days or 0),
        "records_fetched": int(job.records_fetched or 0),
        "date_from": job.date_from,
        "date_to": job.date_to,
        "error": job.error,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def _create_sync_job(shop_id: str, sync_type: str) -> str:
    job_id = _uuid.uuid4().hex[:12]
    with SessionLocal() as db:
        db.add(SyncJob(
            job_id=job_id,
            shop_id=shop_id,
            sync_type=sync_type,
            status="queued",
        ))
        db.commit()
    return job_id


def _update_sync_job(job_id: str, **fields):
    if not fields:
        return
    with SessionLocal() as db:
        job = db.get(SyncJob, job_id)
        if not job:
            return
        for key, value in fields.items():
            if hasattr(job, key):
                setattr(job, key, value)
        db.commit()


def _get_sync_job(job_id: str) -> dict | None:
    with SessionLocal() as db:
        job = db.get(SyncJob, job_id)
        return _serialize_sync_job(job) if job else None


def _list_sync_jobs(*, statuses: tuple[str, ...] | None = None) -> list[dict]:
    with SessionLocal() as db:
        stmt = select(SyncJob)
        if statuses:
            stmt = stmt.where(SyncJob.status.in_(statuses))
        stmt = stmt.order_by(desc(SyncJob.created_at))
        return [_serialize_sync_job(job) for job in db.execute(stmt).scalars().all()]


def _clean_old_sync_jobs():
    """Remove completed/failed jobs older than 1 hour from shared storage."""
    cutoff = datetime.utcnow() - timedelta(hours=1)
    with SessionLocal() as db:
        db.execute(
            delete(SyncJob).where(
                SyncJob.finished_at.is_not(None),
                SyncJob.finished_at < cutoff,
            )
        )
        db.commit()


def _ensure_common_db_indexes():
    """Create indexes that matter for the finance hot path."""
    statements = [
        "CREATE INDEX IF NOT EXISTS ix_fo_shop_period ON finance_orders(shop_id, period_from, period_to)",
        "CREATE INDEX IF NOT EXISTS ix_fo_shop_sku ON finance_orders(shop_id, sku_title)",
        "CREATE INDEX IF NOT EXISTS ix_fo_shop_day_sku_id ON finance_orders(shop_id, period_from, sku_id)",
        "CREATE INDEX IF NOT EXISTS ix_fhs_shop_hour ON finance_hourly_snapshots(shop_id, snapshot_hour)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_ns_user_id ON notification_settings(user_id)",
        "CREATE INDEX IF NOT EXISTS ix_wes_day_shop ON warehouse_expense_snapshots(expense_date, shop_id)",
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

_ADMIN_SECRET = os.environ.get("ADMIN_SECRET_PATH", "").strip()
if not _ADMIN_SECRET:
    import secrets as _secrets
    _ADMIN_SECRET = _secrets.token_urlsafe(16)
print(f"\n[ADMIN] Protected backstage route: /backstage/login", flush=True)
print(f"[ADMIN] Secret admin entry: /admin-{_ADMIN_SECRET}/login\n", flush=True)

login_manager.init_app(app)
login_manager.login_view = "auth_bp.login"

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

    with SessionLocal() as db:
        user = db.get(User, int(current_user.get_id()))
        if not user:
            return None
        settings = _get_or_create_subscription_settings(db)
        if _ensure_user_trial_started(db, user):
            db.commit()
            db.refresh(user)
        status = _subscription_status_for_user(user, settings=settings)

    if status["active"]:
        return None

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


def _store_warehouse_expense_snapshot_for_day(expense_day: date, snapshots_by_shop: dict[str, dict]) -> int:
    captured_at = _app_naive(_now_app_tz())
    shop_ids = list(snapshots_by_shop.keys())
    with SessionLocal() as db:
        if shop_ids:
            db.execute(
                delete(WarehouseExpenseSnapshot).where(
                    WarehouseExpenseSnapshot.expense_date == expense_day,
                    WarehouseExpenseSnapshot.shop_id.in_(shop_ids),
                )
            )
        rows = []
        for shop_id, payload in snapshots_by_shop.items():
            rows.append({
                "expense_date": expense_day,
                "shop_id": shop_id,
                "items_json": json.dumps(payload.get("items", []), ensure_ascii=False),
                "total_amount": int(payload.get("total", 0) or 0),
                "captured_at": captured_at,
            })
        if rows:
            db.execute(insert(WarehouseExpenseSnapshot), rows)
        db.commit()
    return len(rows)


def _load_warehouse_expense_snapshots(expense_day: date, shop_ids: list[str]) -> dict[str, dict]:
    if not shop_ids:
        return {}
    with SessionLocal() as db:
        rows = db.execute(
            select(WarehouseExpenseSnapshot).where(
                WarehouseExpenseSnapshot.expense_date == expense_day,
                WarehouseExpenseSnapshot.shop_id.in_(shop_ids),
            )
        ).scalars().all()

    snapshots: dict[str, dict] = {}
    for row in rows:
        try:
            items = json.loads(row.items_json or "[]")
        except Exception:
            items = []
        if not isinstance(items, list):
            items = []
        normalized_items = []
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            try:
                amount = int(item.get("amount", 0) or 0)
            except (ValueError, TypeError):
                amount = 0
            if not name or amount <= 0:
                continue
            normalized_items.append({"name": name, "amount": amount})
        snapshots[str(row.shop_id)] = {
            "items": normalized_items,
            "total": int(row.total_amount or 0),
        }
    return snapshots


def _capture_warehouse_expense_snapshot_for_day(
    expense_day: date,
    *,
    allow_latest_fallback: bool = False,
) -> dict:
    api_key = _get_admin_token()
    if not api_key:
        print(f"[ExpensesSnapshot] No admin token, skipping snapshot for {expense_day.isoformat()}.")
        return {"ok": False, "reason": "no_token", "expense_day": expense_day.isoformat()}

    with SessionLocal() as db:
        shops = db.execute(select(Shop)).scalars().all()
        shop_ids = [str(shop.uzum_id).strip() for shop in shops if shop.uzum_id]

    if not shop_ids:
        print(f"[ExpensesSnapshot] No shops found, skipping snapshot for {expense_day.isoformat()}.")
        return {"ok": False, "reason": "no_shops", "expense_day": expense_day.isoformat()}

    started_at = time.monotonic()
    payments = _fetch_warehouse_expense_payments(shop_ids, api_key=api_key)
    filtered = _filter_warehouse_expense_payments_for_day(
        payments,
        expense_day,
        allow_latest_fallback=allow_latest_fallback,
    )
    snapshots_by_shop = _build_warehouse_expense_snapshots_for_shops(filtered, shop_ids)
    row_count = _store_warehouse_expense_snapshot_for_day(expense_day, snapshots_by_shop)
    total_amount = sum(int(payload.get("total", 0) or 0) for payload in snapshots_by_shop.values())
    elapsed = time.monotonic() - started_at
    print(
        f"[ExpensesSnapshot] day={expense_day.isoformat()} shops={len(shop_ids)} "
        f"payments={len(filtered)} rows={row_count} total={total_amount} elapsed={elapsed:.2f}s"
    )
    return {
        "ok": True,
        "expense_day": expense_day.isoformat(),
        "shop_count": len(shop_ids),
        "payment_count": len(filtered),
        "row_count": row_count,
        "total_amount": total_amount,
        "elapsed_seconds": round(elapsed, 2),
    }


def _warehouse_expense_snapshot_progress(expense_day: date) -> tuple[int, int]:
    with SessionLocal() as db:
        expected_rows = int(db.execute(
            select(func.count()).select_from(Shop).where(Shop.uzum_id != None)
        ).scalar() or 0)
        actual_rows = int(db.execute(
            select(func.count()).select_from(WarehouseExpenseSnapshot).where(
                WarehouseExpenseSnapshot.expense_date == expense_day
            )
        ).scalar() or 0)
    return expected_rows, actual_rows


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
    try:
        with open(_TELEGRAM_CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}

def _tg_save_config(cfg: dict):
    with open(_TELEGRAM_CONFIG_PATH, "w") as f:
        json.dump(cfg, f)

if not os.path.exists(_TELEGRAM_CONFIG_PATH):
    _tg_save_config({
        "bot_token": "8657801234:AAE21INFXemj1q8IEySIbp5D17Y9_10GWKE",
        "bot_username": "SellerHub_uz_bot",
    })

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
                        # Also populate FinanceOrder table (finance page) from 2022-01-01
                        try:
                            _job_id = _create_sync_job(uzum_id, "full")
                            _run_manual_sync_job(_job_id, uzum_id, False)
                            bot.send_message(chat_id,
                                f"📊 История финансов загружена для *{shop_name}* (с 2022 г.).",
                                parse_mode="Markdown")
                        except Exception as _e:
                            bot.send_message(chat_id,
                                f"⚠️ Не удалось загрузить историю финансов для *{shop_name}*: {_e}",
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
    """Return the common dash-prefix shared by all SKUs in a group.
    e.g. ['luxuz-stres02-синий-17', 'luxuz-stres02-красный-18'] -> 'luxuz-stres02'
    """
    clean = [s.strip() for s in skus if s and s.strip()]
    if not clean:
        return "—"
    if len(clean) == 1:
        return clean[0]
    parts_list = [s.split("-") for s in clean]
    min_len = min(len(p) for p in parts_list)
    common = []
    for i in range(min_len):
        if all(p[i] == parts_list[0][i] for p in parts_list):
            common.append(parts_list[0][i])
        else:
            break
    return "-".join(common) if common else clean[0].split("-")[0]


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

    font_title  = _font(18, bold=True)
    font_sub    = _font(11)
    font_header = _font(11, bold=True)
    font_body   = _font(12)
    font_small  = _font(10)
    font_bold   = _font(12, bold=True)
    font_shop   = _font(13, bold=True)
    font_exp_h  = _font(12, bold=True)

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
        ("Описание", 160, "left"),
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
    COL_WIDTHS = [width for _, width, _ in col_specs]
    COL_ALIGNS = [align for _, _, align in col_specs]

    MARGIN  = 16           # outer margin
    PAD_X   = 14
    PAD_Y   = 10
    ROW_H   = 34
    HDR_H   = 30           # column header row
    SHOP_H  = 36
    TITLE_H = 62
    SUMM_H  = 36
    FOOTER_H = 30
    CARD_R  = 14
    SVCLINE_H = 28
    SVC_HEADER_H = 32

    card_w  = sum(COL_WIDTHS) + 2 * PAD_X
    total_w = card_w + 2 * MARGIN

    sections = [(s, v) for s, v in by_shop.items() if s not in ("__totals__", "__expenses__")]
    total_rows = sum(len(r) for _, r in sections)
    expenses = by_shop.get("__expenses__", {})
    wh_exp_lines = []
    if expenses and expenses.get("items"):
        for ei in expenses["items"]:
            wh_exp_lines.append((ei["name"], ei["amount"]))
        if len(wh_exp_lines) > 1:
            wh_exp_lines.append(("Итого складские расходы", expenses.get("total", 0)))
    WH_BLOCK_H = (SVC_HEADER_H + len(wh_exp_lines) * SVCLINE_H + PAD_Y + 6) if wh_exp_lines else 0

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
               + total_rows * ROW_H
               + GRAND_H
               + WH_BLOCK_H
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
        ty = y + (h - 13) / 2
        draw.text((tx, ty), text, font=font, fill=color)

    def draw_rounded_card(x, y, w, h, r=CARD_R, fill=CARD):
        # Shadow
        draw.rounded_rectangle([x + 2, y + 2, x + w + 1, y + h + 1], radius=r, fill=SHADOW)
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
    draw.text((MARGIN + PAD_X, ty + 12), "Продажи — статистика", font=font_title, fill=TITLE_FG)
    draw.text((MARGIN + PAD_X, ty + 36), hour_label, font=font_sub, fill=SUBTITLE_FG)

    cy = TITLE_H + MARGIN + PAD_Y

    for shop_name, rows in sections:
        totals = by_shop.get("__totals__", {}).get(shop_name, {})

        # Card background for the whole shop section
        card_h = SHOP_H + HDR_H + len(rows) * ROW_H + SUMM_H + 2
        draw_rounded_card(cx_base, cy, card_w, card_h)

        # Shop header bar (dark accent)
        draw.rounded_rectangle(
            [cx_base, cy, cx_base + card_w - 1, cy + SHOP_H - 1],
            radius=CARD_R, fill=SHOP_BG)
        # Flatten bottom corners
        draw.rectangle([cx_base, cy + SHOP_H // 2, cx_base + card_w - 1, cy + SHOP_H - 1], fill=SHOP_BG)
        draw.text((cx_base + PAD_X + 4, cy + 10), shop_name, font=font_shop, fill=SHOP_FG)
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
            name = (it.get("group") or "—")
            if len(name) > 24: name = name[:22] + "…"
            sku  = (it.get("base_sku") or "—")
            if len(sku) > 18: sku = sku[:16] + "…"
            cells = [
                name,
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
                draw_cell(cx, cy, w, ROW_H, cell, font_body, colors[i],
                          align=COL_ALIGNS[i], bg=row_bg)
                cx += w
            draw.line([(cx_base + PAD_X, cy + ROW_H - 1),
                       (cx_base + card_w - PAD_X, cy + ROW_H - 1)], fill=DIVIDER)
            cy += ROW_H

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
            radius=CARD_R, fill=TOTAL_BG, outline=SHOP_BG, width=2)
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
        draw.text((cx_base + PAD_X + 4, cy + 8), "Складские расходы", font=font_exp_h, fill=EXP_TOTAL)
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
            draw.text((cx_base + PAD_X + 8, cy + (SVCLINE_H - 12) / 2), label, font=fnt, fill=BODY_FG)
            val_str = _fmt_sum(val) + " сум"
            vw = tw(val_str, fnt)
            draw.text((cx_base + card_w - PAD_X - vw - 8, cy + (SVCLINE_H - 12) / 2),
                      val_str, font=fnt, fill=clr)
            if not is_last:
                draw.line([(cx_base + PAD_X, cy + SVCLINE_H - 1),
                           (cx_base + card_w - PAD_X, cy + SVCLINE_H - 1)], fill=DIVIDER)
            cy += SVCLINE_H
        cy += PAD_Y + 6

    # ── Footer ───────────────────────────────────────────────────────────
    footer_text = "SellerHub  ·  Uzum Analytics"
    ftw = tw(footer_text, font_small)
    draw.text(((total_w - ftw) / 2, cy + 6), footer_text, font=font_small, fill=FOOTER_FG)

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


def _save_hourly_snapshots(snap_hour: datetime) -> None:
    import datetime as _dt

    snap_hour_db = _app_naive(snap_hour)
    snapshot_day = (snap_hour - _dt.timedelta(seconds=1)).date()

    with SessionLocal() as db:
        snapshot_rows = db.execute(
            select(FinanceOrder).where(
                FinanceOrder.period_from == snapshot_day,
                FinanceOrder.period_to == snapshot_day,
            )
        ).scalars().all()

        cutoff = snap_hour_db - _dt.timedelta(hours=25)
        db.execute(
            delete(FinanceHourlySnapshot)
            .where(FinanceHourlySnapshot.snapshot_hour < cutoff)
        )
        db.execute(
            delete(FinanceHourlySnapshot)
            .where(FinanceHourlySnapshot.snapshot_hour == snap_hour_db)
        )

        payload = []
        for row in snapshot_rows:
            payload.append({
                "shop_id": row.shop_id,
                "sku_title": row.sku_title,
                "snapshot_hour": snap_hour_db,
                "amount": row.amount,
                "sell_price": row.sell_price,
                "purchase_price": row.purchase_price,
                "seller_profit": row.seller_profit,
                "commission": row.commission,
                "logistics_fee": row.logistics_fee,
            })
        if payload:
            db.execute(insert(FinanceHourlySnapshot), payload)
        db.commit()


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
    prev_hour = _app_naive(snap_hour - _dt.timedelta(hours=1))
    hour_label = _format_sales_window_label(hour_start, snap_hour)
    hour_from_ts = int(hour_start.timestamp())
    # Use the last second of the closed hour to avoid leaking fresh-hour sales
    # into the previous-hour notification if the API treats dateTo as inclusive.
    hour_to_ts = int((snap_hour - _dt.timedelta(seconds=1)).timestamp())
    day_from_ts = int(_app_dt(today, day_start_hour).timestamp())
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

    burst_shop_ids = [shop.uzum_id for shop in visible_shops if shop.uzum_id]

    if requested_user_ids and not visible_shops:
        print(f"[HourlySales] No visible shops for target users={requested_user_ids}, skipping.")
        return 0

    exact_hour_map, exact_hour_errors = _fetch_exact_hour_sales_for_all_shops(
        burst_shop_ids,
        api_key=api_key,
        date_from_ts=hour_from_ts,
        date_to_ts=hour_to_ts,
    )
    if exact_hour_errors:
        print(f"[HourlySales] Falling back to snapshot delta for {len(exact_hour_errors)} shops with burst fetch errors.")
    exact_day_map, exact_day_errors = _fetch_exact_hour_sales_for_all_shops(
        burst_shop_ids,
        api_key=api_key,
        date_from_ts=day_from_ts,
        date_to_ts=hour_to_ts,
        track_stats=False,
    )
    if exact_day_errors:
        print(f"[HourlySales] Falling back to cached day totals for {len(exact_day_errors)} shops with day-range fetch errors.")
    _t_fetch = time.monotonic()
    print(f"[HourlySales] TIMING: exact hour + local-day fetch = {_t_fetch - _t0:.2f}s")

    total_hour_sold = 0
    uid_data: dict = {}   # uid -> shop_label -> group_id -> data for items sold since midnight
    uid_totals: dict = {} # uid -> shop_label -> full-shop day/hour totals

    with SessionLocal() as db:
        # ── 1. Read today's totals from finance_orders (already synced) ──
        day_dates = {today}
        if day_start_hour > 0:
            day_dates.add((snap_hour - _dt.timedelta(seconds=1)).date())
        today_rows = db.execute(
            select(FinanceOrder)
            .where(
                FinanceOrder.period_from.in_(sorted(day_dates)),
                FinanceOrder.period_to.in_(sorted(day_dates)),
            )
        ).scalars().all()

        # Build: {shop_id: {sku_title: FinanceOrder}}
        current_map: dict[str, dict[str, dict]] = {}
        for fo in today_rows:
            sku_bucket = current_map.setdefault(fo.shop_id, {}).setdefault(fo.sku_title, {
                "amount": 0,
                "sell_price": 0,
                "purchase_price": 0,
                "seller_profit": 0,
                "commission": 0,
                "logistics_fee": 0,
            })
            sku_bucket["amount"] += fo.amount or 0
            sku_bucket["sell_price"] += fo.sell_price or 0
            sku_bucket["purchase_price"] += fo.purchase_price or 0
            sku_bucket["seller_profit"] += fo.seller_profit or 0
            sku_bucket["commission"] += fo.commission or 0
            sku_bucket["logistics_fee"] += fo.logistics_fee or 0

        # ── 2. Read previous hourly snapshot ─────────────────────────────
        prev_rows = db.execute(
            select(FinanceHourlySnapshot)
            .where(FinanceHourlySnapshot.snapshot_hour == prev_hour)
        ).scalars().all()

        prev_map: dict[str, dict[str, int]] = {}  # {shop_id: {sku_title: amount}}
        for ps in prev_rows:
            prev_map.setdefault(ps.shop_id, {})[ps.sku_title] = ps.amount

        # ── 3. Build notification data from DB ───────────────────────────
        for shop in visible_shops:
            shop_data = dict(current_map.get(shop.uzum_id, {}))
            prev_shop = prev_map.get(shop.uzum_id, {})
            shop_day_map = exact_day_map.get(shop.uzum_id)
            shop_hour_map = exact_hour_map.get(shop.uzum_id)
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

            if not shop_data and (shop_day_map is not None or shop_hour_map is not None):
                shop_data = _synthesize_shop_data_from_sales_maps(
                    variants,
                    day_map=shop_day_map,
                    hour_map=shop_hour_map,
                )
            elif shop_day_map is not None:
                for row_key, row_data in _synthesize_shop_data_from_sales_maps(variants, day_map=shop_day_map).items():
                    shop_data.setdefault(row_key, row_data)

            if not shop_data:
                continue

            for sku_title, fo_data in shop_data.items():
                d_qty = fo_data["amount"]
                sell_price     = fo_data["sell_price"]
                purchase_price = fo_data["purchase_price"]
                seller_profit  = fo_data["seller_profit"]
                commission_u   = fo_data["commission"]
                logistics_u    = fo_data["logistics_fee"]

                # Try to find matching variant for group name
                vg = sku_to_vg.get(sku_title) or sku_to_vg.get(sku_title.upper() if sku_title else "")
                if vg:
                    v, g = vg
                    group_name = g.name
                    group_id = g.id
                    sku_label = v.sku or sku_title
                    # Fallback to variant DB fields if finance record has 0
                    if sell_price == 0:
                        sell_price = v.sell_price_uzum or v.price_sum or 0
                    if purchase_price == 0:
                        purchase_price = v.purchase_price or 0
                else:
                    group_name = fo_data.get("product_title") or sku_title
                    group_id = sku_title  # use sku as group key
                    sku_label = sku_title

                day_entry = _lookup_sales_map_entry(
                    shop_day_map,
                    sku=sku_title,
                    barcode=v.barcode if vg else None,
                    sku_id=v.uzum_sku_id if vg and v.uzum_sku_id else None,
                ) if shop_day_map is not None else None

                if day_entry is not None:
                    d_qty = int(day_entry.get("qty") or 0)
                    sell_price = int(day_entry.get("sell_price") or 0) * d_qty
                    purchase_price = int(day_entry.get("price") or 0) * d_qty
                    seller_profit = int(day_entry.get("seller_profit") or 0) * d_qty
                    commission_u = int(day_entry.get("commission") or 0) * d_qty
                    logistics_u = int(day_entry.get("logistics") or 0) * d_qty

                if d_qty <= 0:
                    continue

                hour_entry = _lookup_sales_map_entry(
                    shop_hour_map,
                    sku=sku_title,
                    barcode=v.barcode if vg else None,
                    sku_id=v.uzum_sku_id if vg and v.uzum_sku_id else None,
                ) if shop_hour_map is not None else None

                if shop_hour_map is not None:
                    h_qty = int(hour_entry.get("qty") or 0) if hour_entry else 0
                else:
                    if window_hours == 1 and today == snap_hour.date():
                        prev_qty = prev_shop.get(sku_title, 0)
                        h_qty = max(0, d_qty - prev_qty)
                    else:
                        h_qty = d_qty

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
        all_shop_ids = sorted({shop_id for shop_ids in uid_shop_ids.values() for shop_id in shop_ids})
        stored_by_shop = _load_warehouse_expense_snapshots(warehouse_expense_snapshot_day, all_shop_ids)
        if warehouse_expense_capture_if_missing and len(stored_by_shop) < len(all_shop_ids):
            capture_result = _capture_warehouse_expense_snapshot_for_day(
                warehouse_expense_snapshot_day,
                allow_latest_fallback=False,
            )
            if capture_result.get("ok"):
                stored_by_shop = _load_warehouse_expense_snapshots(warehouse_expense_snapshot_day, all_shop_ids)
        for _uid, _sid_list in uid_shop_ids.items():
            uid_expenses[_uid] = _merge_warehouse_expense_payloads([
                stored_by_shop.get(str(shop_id), {"items": [], "total": 0})
                for shop_id in _sid_list
            ])
        _t2 = time.monotonic()
        print(
            f"[HourlySales] TIMING: warehouse expenses DB snapshot "
            f"({warehouse_expense_snapshot_day.isoformat()}) = {_t2 - _t2_start:.2f}s"
        )
    else:
        _t2 = time.monotonic()
        print(f"[HourlySales] TIMING: warehouse expenses skipped for interval notification = {_t2 - _t2_start:.2f}s")

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
    _save_hourly_snapshots(snap_hour)

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


def _hourly_sales_loop():
    """Background thread: evaluates per-user notification settings at each closed hour."""
    import time as _t

    next_hour = _now_app_tz().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

    while True:
        try:
            now = _now_app_tz()
            sleep_seconds = (next_hour - now).total_seconds()

            if sleep_seconds > 0:
                _t.sleep(sleep_seconds)

            # Use the scheduled hour boundary, not the wall clock at wake time.
            # If a previous run finished late, immediately catch up missing slots
            # instead of drifting or skipping an hour.
            while _now_app_tz() >= next_hour:
                _run_scheduled_hourly_sales_check(next_hour)
                next_hour += timedelta(hours=1)
        except Exception as e:
            print(f"[HourlySales] Unexpected error: {e}")
            _t.sleep(60)  # If error, wait 60s before retrying


def _warehouse_expense_snapshot_loop():
    """Background thread: capture warehouse-expense snapshots once daily around 23:30."""
    import time as _t

    while True:
        try:
            now = _now_app_tz()
            if (
                now.hour == WAREHOUSE_EXPENSE_SNAPSHOT_HOUR
                and now.minute >= WAREHOUSE_EXPENSE_SNAPSHOT_MINUTE
            ):
                expense_day = now.date()
                expected_rows, actual_rows = _warehouse_expense_snapshot_progress(expense_day)
                if expected_rows > 0 and actual_rows < expected_rows:
                    print(
                        f"[ExpensesSnapshot] Capturing day={expense_day.isoformat()} "
                        f"progress={actual_rows}/{expected_rows}"
                    )
                    _capture_warehouse_expense_snapshot_for_day(
                        expense_day,
                        allow_latest_fallback=False,
                    )
            _t.sleep(60)
        except Exception as e:
            print(f"[ExpensesSnapshot] Unexpected error: {e}")
            _t.sleep(60)


def _finance_auto_refresh_loop():
    """Background thread: queue hourly finance refresh at HH:00 and drain it safely."""
    import time as _t

    next_hour = _now_app_tz().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

    while True:
        try:
            now = _now_app_tz()
            if not FINANCE_AUTO_REFRESH_ENABLED:
                _t.sleep(60)
                continue

            while now >= next_hour:
                _dispatch_finance_jobs(now_dt=next_hour)
                next_hour += timedelta(hours=1)

            _dispatch_finance_jobs(now_dt=now.replace(second=0, microsecond=0))

            sleep_seconds = min(
                FINANCE_AUTO_QUEUE_TICK_SECONDS,
                max(1.0, (next_hour - now).total_seconds()),
            )
            _t.sleep(sleep_seconds)
        except Exception as e:
            print(f"[FinanceAutoRefresh] Unexpected error: {e}")
            _t.sleep(60)


# Background thread startup extracted into background/startup.py

# Telegram auth routes extracted into telegram/routes.py


def _has_any_users() -> bool:
    with SessionLocal() as db:
        return db.execute(select(User.id)).first() is not None


# ── Finance data sync & query ────────────────────────────────────────

# Finance page route extracted into finance/routes.py


def _recent_finance_refresh_window(reference_day: date | None = None) -> tuple[date, date]:
    """Return the inclusive rolling finance refresh window."""
    end_day = reference_day or _today_app_tz()
    start_day = end_day - timedelta(days=FINANCE_REFRESH_DAYS - 1)
    return start_day, end_day


def _stable_slot(key: str, modulo: int, salt: str = "") -> int:
    """Stable hash slot so shop scheduling stays consistent across restarts."""
    if modulo <= 1:
        return 0
    payload = f"{salt}:{key}".encode("utf-8", "ignore")
    return int(hashlib.sha1(payload).hexdigest()[:12], 16) % modulo


def _try_begin_finance_shop_sync(shop_id: str) -> bool:
    """Guard against overlapping finance syncs for the same shop."""
    with _finance_active_lock:
        if shop_id in _finance_active_shops:
            return False
        _finance_active_shops.add(shop_id)
        return True


def _finish_finance_shop_sync(shop_id: str) -> None:
    with _finance_active_lock:
        _finance_active_shops.discard(shop_id)


def _prepare_finance_sync_state(shop_id: str) -> dict:
    """Normalize legacy finance cache state before syncing."""
    state = {
        "existing": 0,
        "upgraded_to_daily": False,
        "needs_full_rebuild": False,
    }

    with SessionLocal() as db:
        existing = db.execute(
            select(func.count(FinanceOrder.id))
            .where(FinanceOrder.shop_id == shop_id)
        ).scalar() or 0
        state["existing"] = existing

        has_non_daily = False
        if existing > 0:
            non_daily_count = db.execute(
                select(func.count(FinanceOrder.id))
                .where(
                    FinanceOrder.shop_id == shop_id,
                    FinanceOrder.period_from != FinanceOrder.period_to,
                )
            ).scalar() or 0
            has_non_daily = non_daily_count > 0

        # Old period-spanning rows cannot be mixed safely with daily rows.
        # A partial refresh would otherwise double-count overlapping dates.
        if has_non_daily:
            db.execute(delete(FinanceOrder).where(FinanceOrder.shop_id == shop_id))
            db.execute(delete(FinanceSyncLog).where(FinanceSyncLog.shop_id == shop_id))
            db.commit()
            state["existing"] = 0
            state["upgraded_to_daily"] = True
            state["needs_full_rebuild"] = True

    return state


def _finance_auto_refresh_shop_ids() -> list[str]:
    """Auto-refresh every active shop that has an Uzum shop ID."""
    with SessionLocal() as db:
        return sorted({
            str(shop_id).strip()
            for shop_id in db.execute(select(Shop.uzum_id)).scalars().all()
            if str(shop_id or "").strip()
        })


def _scheduled_auto_finance_windows(shop_id: str, now_dt: datetime) -> list[tuple[str, date, date]]:
    """Return the finance windows due for this shop at the top of the hour."""
    if now_dt.minute != 0:
        return []

    today = now_dt.date()
    windows: list[tuple[str, date, date]] = []

    recent_due = (
        FINANCE_RECENT_DAYS > 1
        and (now_dt.hour % FINANCE_RECENT_REFRESH_HOURS) == _stable_slot(shop_id, FINANCE_RECENT_REFRESH_HOURS, "finance-recent-hour")
    )
    if recent_due:
        recent_start = today - timedelta(days=FINANCE_RECENT_DAYS - 1)
        windows.append(("auto_recent", recent_start, today))
    else:
        windows.append(("auto_today", today, today))

    if FINANCE_REFRESH_DAYS > FINANCE_RECENT_DAYS:
        backfill_hour = _stable_slot(shop_id, 24, "finance-backfill-hour")
        if now_dt.hour == backfill_hour:
            backfill_start = today - timedelta(days=FINANCE_REFRESH_DAYS - 1)
            backfill_end = today - timedelta(days=FINANCE_RECENT_DAYS)
            if backfill_start <= backfill_end:
                windows.append(("auto_backfill", backfill_start, backfill_end))

    return windows


def _finance_queue_hour_key(run_at: datetime) -> str:
    return run_at.replace(minute=0, second=0, microsecond=0).isoformat()


def _finance_rotated_shop_ids(shop_ids: list[str], run_at: datetime) -> list[str]:
    """Rotate queue order each hour so the same shops are not always first."""
    if not shop_ids:
        return []

    ordered = sorted(shop_ids)
    offset = int(run_at.timestamp() // 3600) % len(ordered)
    return ordered[offset:] + ordered[:offset]


def _enqueue_finance_jobs_for_hour(run_at: datetime) -> int:
    """Queue all finance refresh jobs that officially become due at HH:00."""
    global _finance_last_enqueue_hour

    hour_start = run_at.replace(minute=0, second=0, microsecond=0)
    hour_key = _finance_queue_hour_key(hour_start)

    with _finance_queue_lock:
        if _finance_last_enqueue_hour == hour_key:
            return 0
        _finance_last_enqueue_hour = hour_key

    shop_ids = _finance_rotated_shop_ids(_finance_auto_refresh_shop_ids(), hour_start)
    if not shop_ids:
        with _finance_stats_lock:
            _finance_stats["last_enqueue_time"] = hour_start.isoformat()
            _finance_stats["last_enqueue_hour"] = hour_key
            _finance_stats["last_enqueue_count"] = 0
            _finance_stats["pending_queue_size"] = len(_finance_pending_shop_ids)
        return 0

    due_counts = {"auto_today": 0, "auto_recent": 0, "auto_backfill": 0}
    enqueued = 0

    with _finance_queue_lock:
        for shop_id in shop_ids:
            windows = _scheduled_auto_finance_windows(shop_id, hour_start)
            if not windows:
                continue

            for sync_type, _, _ in windows:
                due_counts[sync_type] = due_counts.get(sync_type, 0) + 1

            entry = _finance_pending_jobs.get(shop_id)
            if entry:
                entry["windows"] = windows
                entry["queued_hour"] = hour_key
                entry["queued_at"] = hour_start.isoformat()
                continue

            _finance_pending_jobs[shop_id] = {
                "shop_id": shop_id,
                "windows": windows,
                "queued_hour": hour_key,
                "queued_at": hour_start.isoformat(),
            }
            _finance_pending_shop_ids.append(shop_id)
            enqueued += 1

        pending_size = len(_finance_pending_shop_ids)

    print(
        f"[FinanceAutoRefresh] {hour_start:%Y-%m-%d %H:%M}: queued {enqueued} shops at hour start — "
        f"today={due_counts['auto_today']}, recent={due_counts['auto_recent']}, backfill={due_counts['auto_backfill']}, "
        f"pending={pending_size}"
    )

    with _finance_stats_lock:
        _finance_stats["last_enqueue_time"] = hour_start.isoformat()
        _finance_stats["last_enqueue_hour"] = hour_key
        _finance_stats["last_enqueue_count"] = enqueued
        _finance_stats["pending_queue_size"] = pending_size

    return enqueued


def _requeue_finance_job(shop_id: str, windows: list[tuple[str, date, date]]) -> None:
    """Return a shop to the pending queue if it lost the race to an overlapping sync."""
    if not windows:
        return

    with _finance_queue_lock:
        entry = _finance_pending_jobs.get(shop_id)
        if entry:
            entry["windows"] = windows
        else:
            _finance_pending_jobs[shop_id] = {
                "shop_id": shop_id,
                "windows": windows,
                "queued_hour": _finance_last_enqueue_hour,
                "queued_at": _now_app_tz().isoformat(),
            }
            _finance_pending_shop_ids.append(shop_id)
        pending_size = len(_finance_pending_shop_ids)

    with _finance_stats_lock:
        _finance_stats["pending_queue_size"] = pending_size


def _drain_finance_job_queue() -> int:
    """Submit queued finance jobs without overfilling executor capacity."""
    with _finance_active_lock:
        active_snapshot = set(_finance_active_shops)

    capacity = max(0, FINANCE_AUTO_REFRESH_WORKERS - len(active_snapshot))
    if capacity <= 0:
        with _finance_stats_lock:
            _finance_stats["last_drain_time"] = _now_app_tz().isoformat()
        return 0

    submissions: list[tuple[str, list[tuple[str, date, date]]]] = []
    with _finance_queue_lock:
        scan_limit = len(_finance_pending_shop_ids)
        while scan_limit > 0 and len(submissions) < capacity:
            shop_id = _finance_pending_shop_ids.popleft()
            entry = _finance_pending_jobs.get(shop_id)
            scan_limit -= 1

            if not entry:
                continue

            if shop_id in active_snapshot:
                _finance_pending_shop_ids.append(shop_id)
                continue

            submissions.append((shop_id, entry["windows"]))
            del _finance_pending_jobs[shop_id]
            active_snapshot.add(shop_id)

        pending_size = len(_finance_pending_shop_ids)

    if not submissions:
        with _finance_stats_lock:
            _finance_stats["last_drain_time"] = _now_app_tz().isoformat()
            _finance_stats["pending_queue_size"] = pending_size
        return 0

    print(
        f"[FinanceAutoRefresh] {_now_app_tz():%Y-%m-%d %H:%M:%S}: submitting {len(submissions)} queued shops "
        f"(pending={pending_size}, active={len(_finance_active_shops)})"
    )

    def _wrapped_shop_sync(shop_id, windows):
        """Run shop sync and update global stats."""
        try:
            results = _run_auto_finance_refresh_for_shop(shop_id, windows=windows)
            if results == [] and windows:
                _requeue_finance_job(shop_id, windows)
                return None

            with _finance_stats_lock:
                _finance_stats["total_completed"] += 1
            return results
        except Exception as e:
            print(f"[FinanceAutoRefresh] Shop {shop_id} failed: {e}")
            with _finance_stats_lock:
                _finance_stats["total_failed"] += 1
            return None

    for shop_id, windows in submissions:
        _finance_executor.submit(_wrapped_shop_sync, shop_id, windows)

    with _finance_stats_lock:
        _finance_stats["total_dispatched"] += len(submissions)
        _finance_stats["last_dispatch_time"] = _now_app_tz().isoformat()
        _finance_stats["last_dispatch_count"] = len(submissions)
        _finance_stats["last_drain_time"] = _now_app_tz().isoformat()
        _finance_stats["pending_queue_size"] = pending_size

    return len(submissions)


def _run_auto_finance_refresh_for_shop(shop_id: str, *, windows: list[tuple[str, date, date]]) -> list[dict]:
    """Refresh the due finance windows for one shop."""
    if not windows:
        return []
    if not _try_begin_finance_shop_sync(shop_id):
        return []

    try:
        state = _prepare_finance_sync_state(shop_id)
        today = _today_app_tz()
        results: list[dict] = []

        # Auto-refresh seeds only the rolling window; full history stays manual.
        if state["needs_full_rebuild"] or state["existing"] <= 0:
            d_from, d_to = _recent_finance_refresh_window(today)
            total_fetched = _finance_sync_range(shop_id, d_from, d_to, "auto_seed")
            results.append({
                "shop_id": shop_id,
                "sync_type": "auto_seed",
                "date_from": d_from,
                "date_to": d_to,
                "records_fetched": total_fetched,
                "upgraded_to_daily": state["upgraded_to_daily"],
            })
            return results

        for sync_type, d_from, d_to in windows:
            total_fetched = _finance_sync_range(shop_id, d_from, d_to, sync_type)
            results.append({
                "shop_id": shop_id,
                "sync_type": sync_type,
                "date_from": d_from,
                "date_to": d_to,
                "records_fetched": total_fetched,
                "upgraded_to_daily": state["upgraded_to_daily"],
            })

        return results
    finally:
        _finish_finance_shop_sync(shop_id)


def _dispatch_finance_jobs(*, now_dt: datetime | None = None) -> bool:
    """Queue hourly finance work at HH:00 and drain it safely with bounded concurrency."""
    if not FINANCE_AUTO_REFRESH_ENABLED:
        return False
    if not _get_admin_token():
        print("[FinanceAutoRefresh] Skipped: no admin Uzum token configured.")
        return False

    now_dt = now_dt or _now_app_tz().replace(second=0, microsecond=0)
    if now_dt.minute == 0:
        _enqueue_finance_jobs_for_hour(now_dt)

    _drain_finance_job_queue()
    return True


def _run_manual_sync_job(job_id: str, shop_id: str, force_full: bool = False):
    """Background worker for manual / onboarding sync jobs."""
    _clean_old_sync_jobs()
    if not _try_begin_finance_shop_sync(shop_id):
        _update_sync_job(job_id, status="failed", error="sync already running for this shop",
                         finished_at=datetime.utcnow())
        return
    try:
        _update_sync_job(job_id, status="running")
        state = _prepare_finance_sync_state(shop_id)
        today = _today_app_tz()

        if force_full:
            with SessionLocal() as db:
                db.execute(delete(FinanceOrder).where(FinanceOrder.shop_id == shop_id))
                db.execute(delete(FinanceSyncLog).where(FinanceSyncLog.shop_id == shop_id))
                db.commit()
            sync_type = "full"
            d_from = date(2022, 1, 1)
            d_to = today
        elif state["existing"] > 0 and not state["needs_full_rebuild"]:
            sync_type = "refresh"
            d_from, d_to = _recent_finance_refresh_window(today)
        else:
            sync_type = "full"
            d_from = date(2022, 1, 1)
            d_to = today

        total_days = (d_to - d_from).days + 1
        _update_sync_job(job_id, sync_type=sync_type,
                         date_from=d_from.isoformat(), date_to=d_to.isoformat(),
                         total_days=total_days)

        total_fetched = _finance_sync_range(shop_id, d_from, d_to, sync_type, job_id=job_id)
        _update_sync_job(job_id, status="completed", records_fetched=total_fetched,
                         progress_days=total_days, finished_at=datetime.utcnow())
    except Exception as exc:
        _update_sync_job(job_id, status="failed", error=str(exc)[:500],
                         finished_at=datetime.utcnow())
    finally:
        _finish_finance_shop_sync(shop_id)


# Finance sync routes extracted into finance/routes.py


def _finance_sync_range(shop_id: str, d_from: date, d_to: date, sync_type: str, job_id: str | None = None) -> int:
    """Fetch grouped finance data from Uzum API and save to DB.
    Uses group=true to get per-SKU aggregates.
    Fetches in daily chunks so cached rows can power day-by-day analytics.
    """
    from_ts_fn = _app_day_start_ts
    to_ts_fn = _app_day_end_ts
    auto_sync = sync_type.startswith("auto_")
    verbose_sync = str(os.environ.get("FINANCE_SYNC_VERBOSE", "0")).strip().lower() in ("1", "true", "yes")
    fetch_ru_titles = (
        str(os.environ.get("FINANCE_FETCH_RU_TITLES", "1")).strip().lower() in ("1", "true", "yes")
        and not auto_sync
    )

    total_fetched = 0

    # Build daily chunks so each stored record represents exactly one day.
    chunks = []
    current = d_from
    while current <= d_to:
        chunks.append((current, current))
        current += timedelta(days=1)

    def _load_cached_title_map() -> dict[str, dict]:
        with SessionLocal() as db:
            rows = db.execute(
                select(
                    FinanceOrder.product_id,
                    func.max(FinanceOrder.product_title_ru),
                    func.max(FinanceOrder.product_title),
                    func.max(FinanceOrder.image_url),
                )
                .where(
                    FinanceOrder.shop_id == shop_id,
                    FinanceOrder.product_id.is_not(None),
                )
                .group_by(FinanceOrder.product_id)
            ).all()
        return {
            str(product_id): {
                "product_title_ru": product_title_ru or "",
                "product_title": product_title or "",
                "image_url": image_url or "",
            }
            for product_id, product_title_ru, product_title, image_url in rows
            if product_id is not None
        }

    def _fetch_shop_ru_titles() -> dict[str, str]:
        """Fetch Russian product titles once per shop sync, not once per day."""
        if not fetch_ru_titles:
            return {}

        ru_titles: dict[str, str] = {}
        page = 0
        total_products = None
        collected = 0
        headers = {"Accept-Language": "ru-RU,ru;q=0.9", "x-language": "ru"}

        while True:
            url = f"https://api-seller.uzum.uz/api/seller/product?page={page}&size=100"
            try:
                raw = http_json(url, headers=headers)
            except Exception as e:
                print(f"[FinanceSync] RU title fetch page {page} failed for shop {shop_id}: {e}")
                break

            payload = raw.get("payload") or {}
            products = payload.get("products") or []
            if total_products is None:
                total_products = int(payload.get("totalProductAmount") or 0)

            for p in products:
                if str(p.get("shopId") or "").strip() != shop_id:
                    continue
                pid = str(p.get("id") or "").strip()
                title = str(p.get("title") or "").strip()
                if pid and title:
                    ru_titles[pid] = title

            collected += len(products)
            if not products or (total_products and collected >= total_products):
                break
            page += 1

        if verbose_sync:
            print(f"[FinanceSync] {shop_id}: loaded {len(ru_titles)} Russian product titles")
        return ru_titles

    cached_titles = _load_cached_title_map()
    shop_ru_titles = _fetch_shop_ru_titles()

    def _extract_order_items(resp):
        """Extract orderItems list from API response."""
        items = []
        if isinstance(resp, dict):
            for k in ["orderItems", "content", "items", "data", "orders"]:
                v = resp.get(k)
                if isinstance(v, list):
                    items = v
                    break
            if not items:
                p = resp.get("payload")
                if isinstance(p, list):
                    items = p
                elif isinstance(p, dict):
                    for k in ["orderItems", "content", "items", "data", "orders"]:
                        v = p.get(k)
                        if isinstance(v, list):
                            items = v
                            break
        elif isinstance(resp, list):
            items = resp
        return items

    def _fetch_chunk(chunk_start, chunk_end):
        """Fetch all pages for one daily chunk. Returns list of record dicts."""
        from_ts = from_ts_fn(chunk_start)
        to_ts = to_ts_fn(chunk_end)

        ru_titles = shop_ru_titles

        # Second pass: fetch with default (Uzbek) language — this is the main data
        records = []
        page = 0
        while True:
            url = (
                f"https://api-seller.uzum.uz/api/seller/finance/orders"
                f"?shopIds={shop_id}&dateFrom={from_ts}&dateTo={to_ts}"
                f"&group=true&page={page}&size=500"
            )
            try:
                resp = http_json(url)
            except Exception as e:
                print(f"[FinanceSync] Error page {page} for {chunk_start}-{chunk_end}: {e}")
                break

            items = _extract_order_items(resp)
            if not items:
                break

            for product_group in items:
                if not isinstance(product_group, dict):
                    continue
                group_pid = product_group.get("productId")
                group_title = product_group.get("productTitle", "")
                sku_items = product_group.get("items") or []
                if not sku_items:
                    sku_items = [product_group]
                for item in sku_items:
                    if not isinstance(item, dict):
                        continue
                    img_url = None
                    img = item.get("image") or item.get("productImage")
                    if isinstance(img, dict):
                        pk = img.get("photoKey", "")
                        if pk:
                            img_url = f"https://images.uzum.uz/{pk}/t_product_240_high.jpg"
                    chars = item.get("characteristics")
                    chars_str = ", ".join(str(c) for c in chars) if isinstance(chars, list) else None
                    pid = item.get("productId") or group_pid
                    cached_meta = cached_titles.get(str(pid or "")) or {}
                    base_product_title = item.get("productTitle") or group_title or cached_meta.get("product_title") or ""
                    records.append({
                        "shop_id": str(item.get("shopId", shop_id)),
                        "period_from": chunk_start,
                        "period_to": chunk_end,
                        "sku_title": item.get("skuTitle", ""),
                        "sku_id": item.get("skuId"),
                        "product_id": pid,
                        "product_title": base_product_title,
                        "product_title_ru": ru_titles.get(str(pid or "")) or cached_meta.get("product_title_ru") or base_product_title,
                        "image_url": img_url or cached_meta.get("image_url") or None,
                        "characteristics": chars_str,
                        "amount": item.get("amount", 0) or 0,
                        "amount_returns": item.get("amountReturns", 0) or 0,
                        "sell_price": item.get("sellPrice", 0) or 0,
                        "purchase_price": item.get("purchasePrice", 0) or 0,
                        "seller_discount": item.get("sellerDiscountAmount", 0) or 0,
                        "seller_profit": item.get("sellerProfit", 0) or 0,
                        "commission": item.get("commission", 0) or 0,
                        "withdrawn_profit": item.get("withdrawnProfit", 0) or 0,
                        "logistics_fee": item.get("logisticDeliveryFee", 0) or 0,
                    })

            if len(items) < 500:
                break
            page += 1
            if page > 100:
                break

        # Deduplicate by sku_title — API sometimes returns same SKU in multiple groups
        seen: dict[str, int] = {}
        deduped: list[dict] = []
        for rec in records:
            key = rec["sku_title"]
            if key in seen:
                # Keep the one with higher amount (more complete data)
                idx = seen[key]
                if rec["amount"] > deduped[idx]["amount"]:
                    deduped[idx] = rec
            else:
                seen[key] = len(deduped)
                deduped.append(rec)
        records = deduped

        if verbose_sync and (not auto_sync or sync_type != "auto_today"):
            print(f"[FinanceSync] {shop_id}: {chunk_start}: {len(records)} records")
        return chunk_start, chunk_end, records

    # Fetch chunks in parallel. Keep this configurable so multi-user deployments
    # can tune throughput vs. API pressure without changing code.
    max_workers = FINANCE_AUTO_SYNC_WORKERS_PER_SHOP if auto_sync else max(1, int(os.getenv("FINANCE_SYNC_WORKERS", "50")))
    total_fetched = 0
    total_chunks = len(chunks)

    # For large syncs (1 500+ days), write to DB every WRITE_BATCH days
    # instead of buffering everything in memory.
    WRITE_BATCH = 30
    batch_buffer: list[tuple[date, date, list[dict]]] = []
    days_completed = 0

    def _flush_batch(buffer):
        nonlocal total_fetched
        if not buffer:
            return
        with SessionLocal() as db:
            for chunk_start, chunk_end, records in buffer:
                db.execute(
                    delete(FinanceOrder).where(
                        FinanceOrder.shop_id == shop_id,
                        FinanceOrder.period_from == chunk_start,
                        FinanceOrder.period_to == chunk_end,
                    )
                )
                db.execute(insert(FinanceOrder), records)
            batch_count = sum(len(r) for _, _, r in buffer)
            total_fetched += batch_count
            db.commit()

    with ThreadPoolExecutor(max_workers=min(max_workers, max(1, len(chunks)))) as pool:
        futures = {pool.submit(_fetch_chunk, cs, ce): (cs, ce) for cs, ce in chunks}
        for future in as_completed(futures):
            chunk_start, chunk_end, records = future.result()
            days_completed += 1
            if records:
                batch_buffer.append((chunk_start, chunk_end, records))

            # Flush to DB every WRITE_BATCH completed chunks
            if len(batch_buffer) >= WRITE_BATCH:
                _flush_batch(batch_buffer)
                batch_buffer = []

            # Report progress for async jobs
            if job_id and days_completed % 10 == 0:
                _update_sync_job(job_id, progress_days=days_completed,
                                 records_fetched=total_fetched)

    # Flush remaining
    _flush_batch(batch_buffer)

    # Write sync log
    if total_fetched or True:  # always log, even if 0 records
        with SessionLocal() as db:
            db.add(FinanceSyncLog(
                shop_id=shop_id,
                sync_type=sync_type,
                date_from=d_from,
                date_to=d_to,
                records_fetched=total_fetched,
            ))
            db.commit()

    return total_fetched


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
            # Also populate FinanceOrder table (finance page) from 2022-01-01
            try:
                _job_id = _create_sync_job(uzum_id, "full")
                _run_manual_sync_job(_job_id, uzum_id, False)
            except Exception as _e:
                print(f"[FetchSync] FinanceOrder seed failed for {uzum_id}: {_e}")
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

    return {
        "pages_synced": page + 1,
        "fetched": total_variants,
        "active_groups": len(active_group_ids),
        "total_products": product_counter,
    }


def _sync_finance_for_shop(shop_uzum_id: str, shop_pk: int) -> dict:
    """Standalone finance-only sync. Returns {updated}."""
    api_key = _get_admin_token()
    sales_map = fetch_finance_sales_map(shop_uzum_id, api_key=api_key)
    if sales_map is None:
        raise RuntimeError("Не удалось получить данные о продажах.")
    updated = 0
    with SessionLocal() as db:
        variants = db.execute(
            select(Variant).join(ProductGroup).where(ProductGroup.shop_id == shop_pk)
        ).scalars().all()
        for v in variants:
            sku_key = (v.sku or "").strip()
            data = sales_map.get(sku_key) or sales_map.get(sku_key.upper())
            if data is None and v.barcode:
                bc_key = v.barcode.strip()
                data = sales_map.get(bc_key) or sales_map.get(bc_key.upper())
            v.sales_30d_finance = data["qty"] if data else 0
            v.avg_daily_sales = v.sales_30d_finance / 30.0
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

