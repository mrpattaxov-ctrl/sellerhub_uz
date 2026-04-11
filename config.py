"""Centralized configuration constants for Uzum Seller Hub."""
from __future__ import annotations

import os
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

# ── App directory ──────────────────────────────────────────────────────
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

# ── Debug toggle ───────────────────────────────────────────────────────
ENABLE_DEBUG_ROUTES = True

# ── Database ───────────────────────────────────────────────────────────
DATABASE_URL = (os.getenv("DATABASE_URL") or os.getenv("DB_URL") or "").strip()
DB_POOL_SIZE = max(5, int(os.getenv("DB_POOL_SIZE", "10")))
DB_MAX_OVERFLOW = max(0, int(os.getenv("DB_MAX_OVERFLOW", "20")))
DB_POOL_RECYCLE_SECONDS = max(30, int(os.getenv("DB_POOL_RECYCLE_SECONDS", "1800")))

# ── Flask ──────────────────────────────────────────────────────────────
SECRET_KEY = os.environ.get("SECRET_KEY", "super-secret-key-change-this")
APP_PUBLIC_BASE_URL = str(os.getenv("APP_PUBLIC_BASE_URL", "")).strip().rstrip("/")
PAYME_MERCHANT_ID = str(os.getenv("PAYME_MERCHANT_ID", "")).strip()
PAYME_MERCHANT_LOGIN = str(os.getenv("PAYME_MERCHANT_LOGIN", "")).strip()
PAYME_KEY = str(os.getenv("PAYME_KEY", "")).strip()
PAYME_TEST_KEY = str(os.getenv("PAYME_TEST_KEY", "")).strip()
PAYME_USE_TEST = str(os.getenv("PAYME_USE_TEST", "1")).strip().lower() in ("1", "true", "yes")

# ── Finance sync ───────────────────────────────────────────────────────
FINANCE_REFRESH_DAYS = max(1, int(os.getenv("FINANCE_REFRESH_DAYS", "45")))
FINANCE_AUTO_REFRESH_ENABLED = str(os.getenv("FINANCE_AUTO_REFRESH_ENABLED", "1")).strip().lower() in ("1", "true", "yes")
FINANCE_RECENT_DAYS = max(1, min(FINANCE_REFRESH_DAYS, int(os.getenv("FINANCE_RECENT_DAYS", "7"))))
FINANCE_RECENT_REFRESH_HOURS = max(1, int(os.getenv("FINANCE_RECENT_REFRESH_HOURS", "6")))
FINANCE_AUTO_REFRESH_WORKERS = max(1, int(os.getenv("FINANCE_AUTO_REFRESH_WORKERS", "8")))
FINANCE_AUTO_SYNC_WORKERS_PER_SHOP = max(1, int(os.getenv("FINANCE_AUTO_SYNC_WORKERS_PER_SHOP", "4")))
FINANCE_AUTO_QUEUE_TICK_SECONDS = max(1, int(os.getenv("FINANCE_AUTO_QUEUE_TICK_SECONDS", "5")))

# ── Hourly sales burst ────────────────────────────────────────────────
HOURLY_SALES_BURST_FETCH_ENABLED = str(os.getenv("HOURLY_SALES_BURST_FETCH_ENABLED", "1")).strip().lower() in ("1", "true", "yes")
HOURLY_SALES_BURST_FETCH_WORKERS = max(1, int(os.getenv("HOURLY_SALES_BURST_FETCH_WORKERS", str(max(8, FINANCE_AUTO_REFRESH_WORKERS)))))

# ── Warehouse expense snapshots ───────────────────────────────────────
WAREHOUSE_EXPENSE_SNAPSHOT_HOUR = min(23, max(0, int(os.getenv("WAREHOUSE_EXPENSE_SNAPSHOT_HOUR", "23"))))
WAREHOUSE_EXPENSE_SNAPSHOT_MINUTE = min(59, max(0, int(os.getenv("WAREHOUSE_EXPENSE_SNAPSHOT_MINUTE", "30"))))

# ── Timezone ───────────────────────────────────────────────────────────
APP_TIMEZONE_NAME = str(os.getenv("APP_TIMEZONE", "Asia/Tashkent")).strip() or "Asia/Tashkent"
APP_TZ_OFFSET_HOURS = int(os.getenv("APP_TZ_OFFSET_HOURS", "5"))

try:
    APP_TZ = ZoneInfo(APP_TIMEZONE_NAME)
except Exception:
    APP_TZ = timezone(timedelta(hours=APP_TZ_OFFSET_HOURS))
    print(f"[Time] Invalid APP_TIMEZONE={APP_TIMEZONE_NAME!r}; falling back to UTC{APP_TZ_OFFSET_HOURS:+d}")

# ── Notification defaults ─────────────────────────────────────────────
NOTIFICATION_INTERVAL_OPTIONS = (1, 3, 4, 6, 8, 12)
NOTIFICATION_SETTINGS_DEFAULTS = {
    "hourly_enabled": True,
    "window_from_hour": 8,
    "window_to_hour": 20,
    "is_24h": False,
    "interval_hours": 1,
}

# ── Onboarding / manual sync ─────────────────────────────────────────
ONBOARD_MAX_CONCURRENT_SYNCS = max(1, int(os.getenv("ONBOARD_MAX_CONCURRENT_SYNCS", "8")))

# ── HTTP client defaults ──────────────────────────────────────────────
HTTP_POOL_MAXSIZE = max(4, int(os.getenv("HTTP_POOL_MAXSIZE", "20")))
HTTP_USER_AGENT = os.getenv(
    "HTTP_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
)
HTTP_ACCEPT_LANGUAGE = os.getenv("HTTP_ACCEPT_LANGUAGE", "en-US,en;q=0.9,ru;q=0.8")

# ── Backstage login ──────────────────────────────────────────────────
BACKSTAGE_LOGIN_SESSION_KEY = "backstage_login_allowed"
