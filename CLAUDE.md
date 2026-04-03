# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Uzum Seller Hub is a Flask warehouse management app for Uzum marketplace sellers in Uzbekistan. It syncs product, inventory, and finance data from Uzum's seller API, tracks warehouse stock, generates sales reports, sends Telegram notifications, and provides POS and invoicing flows. It supports multi-shop, multi-user admin and seller roles.

## Commands

```bash
# Development / direct run (PostgreSQL required)
pip install -r requirements.txt
export DATABASE_URL=postgresql+psycopg://uzum:YOUR_PASSWORD@127.0.0.1:5432/uzum
python app.py                    # http://127.0.0.1:5000

# Production (PostgreSQL + gunicorn)
docker compose up --build -d     # starts postgres + app
docker compose logs -f app       # tail logs
```

## Architecture

Single-file monolith: nearly all application logic lives in `app.py` (~8.7k lines). `models.py` defines SQLAlchemy ORM models separately.

### Key layers inside app.py

- Lines 1-220: Uzum API response parsing helpers such as `_safe_qty`, `_extract_uzum_qty`, `_extract_sku`, `_collect_variant_rows`
- Lines 220-420: PostgreSQL database setup, connection pooling, sync job tracking, HTTP session with retry
- Lines 420-700: Flask app creation, login manager, admin auth decorators
- Lines 700-1400: Uzum API client functions such as `http_json`, `fetch_finance_sales_map`, `fetch_warehouse_expenses`, `extract_variants`
- Lines 1400-2400: Debug routes under `/debug/*`
- Lines 2400-4200: Telegram bot, login codes, hourly sales image notifications, stock alerts, approval-based login, background thread management
- Lines 4200-4700: Auth routes, settings, home page, groups and products UI routes
- Lines 4700-5800: Finance sync engine, auto-refresh scheduler, manual sync, day-by-day fetching, hourly snapshots
- Lines 5800-7400: POS, invoices, lost goods tracking, returns analysis, debug reports
- Lines 7400-8700: Shop and user admin CRUD, Uzum product sync, variant sales, warehouse import/export, restock invoices

### Data model (models.py)

- `Shop -> ProductGroup -> Variant -> VariantSale`: product hierarchy per seller shop
- `FinanceOrder`: cached daily finance data from Uzum
- `FinanceHourlySnapshot`: hourly snapshots used by sales notifications
- `SyncJob`: cross-worker async sync state
- `User`: admin or seller with optional Uzum credentials and Telegram login
- `TelegramPending`: DB-backed pending login tokens

### Background threads

- Hourly sales loop: fetches finance data for the previous hour and sends Telegram image reports
- Finance auto-refresh: staggered refresh of all shops' finance cache
- Telegram bot: long-polling bot for login codes and approval flows
- Auto-login scheduler: refreshes Uzum seller tokens every 90 minutes

### Database strategy

- PostgreSQL only for every runtime
- `DATABASE_URL` is required; the app fails fast if it is missing
- PostgreSQL pool size, overflow, and recycle are configured via env vars

### Frontend

- Server-rendered Jinja2 templates in `templates/`
- `static/app.js`: main JS for groups, variants, and sales charts
- `static/uzum_ui.js`: Uzum-specific UI helpers
- `chrome_extension/`: captures Uzum seller token from the browser

## Environment Variables

See `.env.example` for all options. Key ones:

- `DATABASE_URL` - required PostgreSQL connection string
- `SECRET_KEY` - Flask session secret
- `FINANCE_AUTO_REFRESH_WORKERS` - parallel shop refresh count
- `TELEGRAM_BOT_TOKEN` - stored in `data/telegram_config.json`, not env vars
- `APP_TIMEZONE` / `APP_TZ_OFFSET_HOURS` - defaults to Asia/Tashkent (UTC+5)

## Important Patterns

- All Uzum quantity parsing must go through `_safe_qty` / `_extract_uzum_qty`
- Debug routes are gated by `ENABLE_DEBUG_ROUTES` in production
- Finance data uses `period_from` / `period_to` date ranges, not individual dates
- Timezone-aware logic should use `_now_app_tz()` / `_today_app_tz()`, not raw `datetime.now()`
