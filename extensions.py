"""Shared extensions: database engine, session factory, login manager."""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from flask_login import LoginManager

from config import DATABASE_URL, DB_POOL_SIZE, DB_MAX_OVERFLOW, DB_POOL_RECYCLE_SECONDS

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

SessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)

# ── Flask-Login ───────────────────────────────────────────────────────
login_manager = LoginManager()
