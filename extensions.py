"""Shared extensions: database engine, session factory, login manager."""
from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from flask_login import LoginManager

from config import (
    DATABASE_URL,
    DB_POOL_SIZE,
    DB_MAX_OVERFLOW,
    DB_POOL_RECYCLE_SECONDS,
    DB_PREPARE_THRESHOLD,
)

# ── Validate DATABASE_URL ─────────────────────────────────────────────
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL (or DB_URL) is required. PostgreSQL is the only supported runtime database."
    )

try:
    PARSED_DB_URL = make_url(DATABASE_URL)
except Exception as exc:
    raise RuntimeError(f"Invalid DATABASE_URL/DB_URL: {exc}") from exc

if not PARSED_DB_URL.drivername.startswith("postgresql"):
    raise RuntimeError(
        "PostgreSQL is required. DATABASE_URL/DB_URL must use a PostgreSQL driver."
    )

DB_URL_DISPLAY = PARSED_DB_URL.render_as_string(hide_password=True)

# ── SQLAlchemy engine & session factory ───────────────────────────────
engine = create_engine(
    DATABASE_URL,
    future=True,
    pool_pre_ping=True,
    pool_size=DB_POOL_SIZE,
    max_overflow=DB_MAX_OVERFLOW,
    pool_recycle=DB_POOL_RECYCLE_SECONDS,
)


def _resolve_prepare_threshold(raw: str):
    """Map DB_PREPARE_THRESHOLD env value to psycopg3's prepare_threshold.

    - "none" / "off" / "disable" / ""  -> None  (never auto-prepare)
    - integer string                    -> int
    - anything else                     -> None (safe default for PgBouncer)
    """
    if raw in ("", "none", "off", "disable", "disabled", "null"):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


_PREPARE_THRESHOLD = _resolve_prepare_threshold(DB_PREPARE_THRESHOLD)


@event.listens_for(engine, "connect")
def _disable_psycopg_prepared_statements(dbapi_connection, connection_record):
    """Disable psycopg3 server-side prepared statements for PgBouncer txn-mode.

    psycopg3 auto-prepares after 5 executions of the same SQL text. Under
    PgBouncer transaction pooling the backend connection rotates between
    clients, so a prepared statement registered on one real connection is
    invisible to the next -> "prepared statement ... does not exist".
    Setting prepare_threshold = None on each new DB-API connection disables
    it entirely.
    """
    try:
        dbapi_connection.prepare_threshold = _PREPARE_THRESHOLD
    except Exception:
        # psycopg2 or other drivers don't have this attr; safe to ignore.
        pass


SessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)

# ── Flask-Login ───────────────────────────────────────────────────────
login_manager = LoginManager()
