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
    threading.Thread(target=_app._hourly_sales_loop, daemon=True).start()
    threading.Thread(target=_app._warehouse_expense_snapshot_loop, daemon=True).start()
    if _app.FINANCE_AUTO_REFRESH_ENABLED:
        threading.Thread(target=_app._finance_auto_refresh_loop, daemon=True).start()
    threading.Thread(target=_app._start_tg_bot, daemon=True).start()
    _app._start_auto_login_scheduler()
    print("[Background] Started: hourly sales, warehouse expense snapshot, finance auto-refresh, Telegram bot, auto-login")
