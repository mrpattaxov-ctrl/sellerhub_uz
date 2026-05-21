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
    _app._start_auto_login_scheduler()
    print("[Background] Started: hourly finance, Telegram bot, auto-login")
