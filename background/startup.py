"""Background thread startup extracted from app.py."""
from __future__ import annotations

import os
import threading

_app = None
_bg_started = False
_bg_start_lock = threading.Lock()
_bg_lock_fd = None


def init_background_startup(app_module):
    global _app
    _app = app_module


def start_background_threads():
    global _bg_started, _bg_lock_fd

    with _bg_start_lock:
        if _bg_started:
            return
        _bg_started = True

    # If running as a dedicated worker process (uzum-worker.service), background
    # threads are already running there — Gunicorn web workers must not start them.
    if os.environ.get("BACKGROUND_WORKER_MODE", "").strip() in ("1", "true", "yes"):
        print("[Background] BACKGROUND_WORKER_MODE=1 detected — background threads managed by uzum-worker.service, skipping.")
        return

    lock_path = os.path.join(_app.DATA_DIR, ".bg_threads.lock")
    try:
        _bg_lock_fd = open(lock_path, "w")
        import fcntl

        fcntl.flock(_bg_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (ImportError, BlockingIOError, OSError):
        try:
            _bg_lock_fd = open(lock_path, "w")
            import msvcrt

            msvcrt.locking(_bg_lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
        except Exception:
            print("[Background] Another worker owns background threads, skipping.")
            return

    print(f"[Background] This worker owns background threads (PID {os.getpid()})")
    threading.Thread(target=_app._start_tg_bot, daemon=True).start()
    # Legacy-style finance pipeline (restored 2026-05-21):
    #   * hourly: refetch today + snapshot delta — sleeps until HH:00,
    #     no continuous ticking.
    #   * nightly: refetch last 45 days for drift correction — sleeps
    #     until 00:30.
    #   * expenses-daily: unchanged, still uses OpenAPI /v1/finance/expenses.
    # No onboarding-backfill loop — initial backfill fires at shop attach
    # in a one-shot daemon thread (see admin/routes.py::_fire_finance_seed).
    if os.environ.get("NEW_SALES_REPORTS_LOOPS", "1").strip().lower() not in ("0", "false", "no"):
        threading.Thread(target=_app._hourly_finance_loop, daemon=True, name="finance-hourly").start()
        threading.Thread(target=_app._nightly_finance_refetch_loop, daemon=True, name="finance-nightly-refetch").start()
        threading.Thread(target=_app._daily_expenses_loop, daemon=True, name="expenses-daily").start()
        print("[Background] Started: legacy-style finance loops (hourly, nightly-refetch, expenses-daily)")
    # Stage 4b — FBS/DBS sync loop. Off-switch via FBS_SYNC_LOOP=0 in .env
    # (useful if the loop ever destabilises and we want to keep the rest
    # of the app running while debugging).
    if os.environ.get("FBS_SYNC_LOOP", "1").strip().lower() not in ("0", "false", "no"):
        threading.Thread(target=_app._fbs_sync_loop, daemon=True, name="fbs-sync").start()
        print("[Background] Started: fbs-sync loop")
    # Stage 4d — daily DELETE of old terminal-state orders so fbs_orders
    # doesn't grow unbounded. Off-switch via FBS_CLEANUP_LOOP=0.
    if os.environ.get("FBS_CLEANUP_LOOP", "1").strip().lower() not in ("0", "false", "no"):
        threading.Thread(target=_app._fbs_cleanup_loop, daemon=True, name="fbs-cleanup").start()
        print("[Background] Started: fbs-cleanup loop")
    # sinxro_2 — server-side product sync loop. Replaces the old
    # browser-side setInterval in static/uzum_ui.js (removed 2026-05-24
    # because it froze the tab for 30-50s every 10 min). Off-switch via
    # PRODUCTS_SYNC_LOOP=0 in .env.
    if os.environ.get("PRODUCTS_SYNC_LOOP", "1").strip().lower() not in ("0", "false", "no"):
        threading.Thread(target=_app._products_sync_loop, daemon=True, name="products-sync").start()
        print("[Background] Started: products-sync loop (sinxro_2)")
    # FBO slot-kuzatuvi (Faza 0 — premissa sinovi, read-only). «Slotlar vaqt
    # o'tib o'zi bo'shaydimi?» savoliga isbot to'playdi; hech narsa
    # yaratmaydi/o'zgartirmaydi. Off-switch via POSTAVKA_SLOT_WATCH_LOOP=0.
    # Avto-slot ALLOKATOR konsumeri (Bosqich 4) — `bus`'dan event drain qiladi.
    # Monitor publish'idan OLDIN ishga tushadi (navbat to'lib ketmasin).
    # Off-switch via POSTAVKA_AUTOSLOT_LOOP=0.
    if os.environ.get("POSTAVKA_AUTOSLOT_LOOP", "1").strip().lower() not in ("0", "false", "no"):
        threading.Thread(target=_app._postavka_allocator_loop, daemon=True, name="postavka-allocator").start()
        print("[Background] Started: postavka avto-slot allocator (consumer)")
    if os.environ.get("POSTAVKA_SLOT_WATCH_LOOP", "1").strip().lower() not in ("0", "false", "no"):
        threading.Thread(target=_app._postavka_slot_watch_loop, daemon=True, name="postavka-slot-watch").start()
        print("[Background] Started: postavka slot watcher")
    # Avto-slot grabber (Faza 2D — накладной-bo'yicha, cross-shop). Off: POSTAVKA_GRAB_LOOP=0.
    if os.environ.get("POSTAVKA_GRAB_LOOP", "1").strip().lower() not in ("0", "false", "no"):
        threading.Thread(target=_app._postavka_grab_loop, daemon=True, name="postavka-grab").start()
        print("[Background] Started: postavka slot grabber")
    _app._start_auto_login_scheduler()
    print("[Background] Started: hourly finance, Telegram bot, auto-login")
