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
# Pool sizing bumped for PgBouncer readiness (Step 2-PRE). With PgBouncer
# in transaction mode, each gunicorn/worker process multiplexes these
# connections against a small real-Postgres pool (default_pool_size=25).
DB_POOL_SIZE = max(5, int(os.getenv("DB_POOL_SIZE", "50")))
DB_MAX_OVERFLOW = max(0, int(os.getenv("DB_MAX_OVERFLOW", "100")))
DB_POOL_RECYCLE_SECONDS = max(30, int(os.getenv("DB_POOL_RECYCLE_SECONDS", "1800")))
# Disable psycopg3 server-side prepared statements so PgBouncer txn-mode
# doesn't blow up with "prepared statement already exists" across
# connection reuse. None = never prepare. Honoured by extensions.py.
DB_PREPARE_THRESHOLD = os.getenv("DB_PREPARE_THRESHOLD", "none").strip().lower()

# ── Redis (shared cache / revoke blocklist) ───────────────────────────
REDIS_URL = (os.getenv("REDIS_URL") or "redis://localhost:6379/0").strip()

# ── Flask ──────────────────────────────────────────────────────────────
SECRET_KEY = os.environ.get("SECRET_KEY", "super-secret-key-change-this")
APP_PUBLIC_BASE_URL = str(os.getenv("APP_PUBLIC_BASE_URL", "")).strip().rstrip("/")
PAYME_MERCHANT_ID = str(os.getenv("PAYME_MERCHANT_ID", "")).strip()
PAYME_MERCHANT_LOGIN = str(os.getenv("PAYME_MERCHANT_LOGIN", "")).strip()
PAYME_KEY = str(os.getenv("PAYME_KEY", "")).strip()
PAYME_TEST_KEY = str(os.getenv("PAYME_TEST_KEY", "")).strip()
PAYME_USE_TEST = str(os.getenv("PAYME_USE_TEST", "1")).strip().lower() in ("1", "true", "yes")

# ── Finance sync ───────────────────────────────────────────────────────
# Start of history for the new-shop initial backfill (Phase 1 sales reports).
# Plan §10: default is 2022-01-01. Accepts an ISO date string from the env.
FINANCE_BACKFILL_START_DATE = os.getenv("FINANCE_BACKFILL_START_DATE", "2022-01-01").strip() or "2022-01-01"
# Size of each chunk enqueued by `_onboarding_backfill_loop`. Plan §10 says
# start at 60 days, shrink to 30 on OOM/timeout — this is the initial value.
SALES_BACKFILL_CHUNK_DAYS = max(1, int(os.getenv("SALES_BACKFILL_CHUNK_DAYS", "60")))
FINANCE_REFRESH_DAYS = max(1, int(os.getenv("FINANCE_REFRESH_DAYS", "45")))

# ── Hourly sales burst ────────────────────────────────────────────────
HOURLY_SALES_BURST_FETCH_ENABLED = str(os.getenv("HOURLY_SALES_BURST_FETCH_ENABLED", "1")).strip().lower() in ("1", "true", "yes")
HOURLY_SALES_BURST_FETCH_WORKERS = max(1, int(os.getenv("HOURLY_SALES_BURST_FETCH_WORKERS", "128")))

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
HTTP_POOL_MAXSIZE = max(4, int(os.getenv("HTTP_POOL_MAXSIZE", "128")))
HTTP_USER_AGENT = os.getenv(
    "HTTP_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
)
HTTP_ACCEPT_LANGUAGE = os.getenv("HTTP_ACCEPT_LANGUAGE", "en-US,en;q=0.9,ru;q=0.8")

# ── Backstage login ──────────────────────────────────────────────────
BACKSTAGE_LOGIN_SESSION_KEY = "backstage_login_allowed"
