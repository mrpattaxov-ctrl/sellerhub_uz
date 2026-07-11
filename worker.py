"""
Standalone background worker process.

Runs all background jobs (hourly sales, finance auto-refresh, Telegram bot,
auto-login scheduler) in a dedicated process completely separate from
Gunicorn web workers.

This means:
- 128-worker burst fetches never compete with web requests
- Gunicorn workers serve ONLY HTTP — no background thread overhead
- Worker can be restarted independently without affecting the web app

Usage (systemd manages this automatically via uzum-worker.service):
    /opt/uzum/venv/bin/python /opt/uzum/worker.py
"""
from __future__ import annotations

import os
import sys
import time
import threading

# Ensure app directory is on path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

print(f"[Worker] Starting background worker (PID {os.getpid()})")

# Bootstrap the Flask app context without starting a web server
import app as _app

print("[Worker] App module loaded.")
print(f"[Worker] HOURLY_SALES_BURST_FETCH_WORKERS = {_app.HOURLY_SALES_BURST_FETCH_WORKERS}")
print(f"[Worker] HTTP_POOL_MAXSIZE = {_app.HTTP_POOL_MAXSIZE}")

# Start all background threads
threads = []

t4 = threading.Thread(target=_app._start_tg_bot, daemon=True, name="telegram-bot")
t4.start()
threads.append(t4)
print("[Worker] Started: Telegram bot")

# Legacy-style finance pipeline loops (restored 2026-05-21, sales_lines pipeline
# fully retired 2026-05-23). Disable via NEW_SALES_REPORTS_LOOPS=0.
if os.environ.get("NEW_SALES_REPORTS_LOOPS", "1").strip().lower() not in ("0", "false", "no"):
    t5 = threading.Thread(target=_app._hourly_finance_loop, daemon=True, name="finance-hourly")
    t5.start()
    threads.append(t5)
    print("[Worker] Started: finance hourly loop (group=true today refresh + snapshot)")

    t6 = threading.Thread(target=_app._nightly_finance_refetch_loop, daemon=True, name="finance-nightly-refetch")
    t6.start()
    threads.append(t6)
    print("[Worker] Started: finance nightly refetch loop (last 45 days)")

    t7 = threading.Thread(target=_app._daily_expenses_loop, daemon=True, name="expenses-daily")
    t7.start()
    threads.append(t7)
    print("[Worker] Started: daily expenses loop")

# Background products/stock sync (replaces old browser 10-min auto-refresh).
# Disable via PRODUCTS_SYNC_LOOP=0.
if os.environ.get("PRODUCTS_SYNC_LOOP", "1").strip().lower() not in ("0", "false", "no"):
    t8 = threading.Thread(target=_app._products_sync_loop, daemon=True, name="products-sync")
    t8.start()
    threads.append(t8)
    print("[Worker] Started: products sync loop")

# FBO slot-kuzatuvi (Faza 0 — premissa sinovi, read-only). Disable via
# POSTAVKA_SLOT_WATCH_LOOP=0.
if os.environ.get("POSTAVKA_SLOT_WATCH_LOOP", "1").strip().lower() not in ("0", "false", "no"):
    t9 = threading.Thread(target=_app._postavka_slot_watch_loop, daemon=True, name="postavka-slot-watch")
    t9.start()
    threads.append(t9)
    print("[Worker] Started: postavka slot watcher")

# Avto-slot grabber (Faza 2D — накладной-bo'yicha, cross-shop). Off: POSTAVKA_GRAB_LOOP=0.
if os.environ.get("POSTAVKA_GRAB_LOOP", "1").strip().lower() not in ("0", "false", "no"):
    t10 = threading.Thread(target=_app._postavka_grab_loop, daemon=True, name="postavka-grab")
    t10.start()
    threads.append(t10)
    print("[Worker] Started: postavka slot grabber")

# Avto-band PROTSESSORI (yakuniy reja — detektorsiz, 10ms non-blocking). Har
# invoice o'zi slotini so'rab o'zini band qiladi. Off: POSTAVKA_BOOKING_LOOP=0.
if os.environ.get("POSTAVKA_BOOKING_LOOP", "1").strip().lower() not in ("0", "false", "no"):
    t11 = threading.Thread(target=_app._postavka_booking_loop, daemon=True, name="postavka-booking")
    t11.start()
    threads.append(t11)
    print("[Worker] Started: postavka avto-band processor")

# Auto-slot alohida servisiga TOKEN PUSH (AUTOSLOT_URL qo'yilgan bo'lsa) — Uzum
# token rotatsiya qilinganda alohida autoslot servisi ham yangisini olsin.
# AUTOSLOT_URL bo'sh (hozirgi prod) → loop darhol chiqadi (no-op).
if os.environ.get("AUTOSLOT_URL", "").strip():
    from postavki import autoslot_client
    t12 = threading.Thread(target=autoslot_client.token_push_loop, daemon=True, name="autoslot-token-push")
    t12.start()
    threads.append(t12)
    print("[Worker] Started: autoslot token-push")

_app._start_auto_login_scheduler()
print("[Worker] Started: auto-login scheduler")

print(f"[Worker] All background jobs running. Monitoring {len(threads)} threads.")

# Keep the process alive — if all daemon threads die
while True:
    time.sleep(60)
    alive = [t.name for t in threads if t.is_alive()]
    dead  = [t.name for t in threads if not t.is_alive()]
    if dead:
        print(f"[Worker] WARNING: dead threads: {dead}, alive: {alive}")
    else:
        print(f"[Worker] Heartbeat OK — {len(alive)} threads alive")
