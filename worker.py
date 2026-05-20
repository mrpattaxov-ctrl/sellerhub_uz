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

# Phase 1 sales/expenses Reports API loops. Disable via NEW_SALES_REPORTS_LOOPS=0.
if os.environ.get("NEW_SALES_REPORTS_LOOPS", "1").strip().lower() not in ("0", "false", "no"):
    t5 = threading.Thread(target=_app._hourly_sales_reports_loop, daemon=True, name="sales-reports-hourly")
    t5.start()
    threads.append(t5)
    print("[Worker] Started: sales reports hourly loop")

    t6 = threading.Thread(target=_app._nightly_refetch_loop, daemon=True, name="sales-nightly-refetch")
    t6.start()
    threads.append(t6)
    print("[Worker] Started: sales nightly refetch loop")

    t7 = threading.Thread(target=_app._daily_expenses_loop, daemon=True, name="expenses-daily")
    t7.start()
    threads.append(t7)
    print("[Worker] Started: daily expenses loop")

    t8 = threading.Thread(target=_app._onboarding_backfill_loop, daemon=True, name="sales-onboarding-backfill")
    t8.start()
    threads.append(t8)
    print("[Worker] Started: sales onboarding backfill loop")

_app._start_auto_login_scheduler()
print("[Worker] Started: auto-login scheduler")

print(f"[Worker] All background jobs running. Monitoring {len(threads)} threads.")

# Keep the process alive — if all daemon threads die, restart them
while True:
    time.sleep(60)
    alive = [t.name for t in threads if t.is_alive()]
    dead  = [t.name for t in threads if not t.is_alive()]
    if dead:
        print(f"[Worker] WARNING: dead threads: {dead}, alive: {alive}")
    else:
        print(f"[Worker] Heartbeat OK — {len(alive)} threads alive")
