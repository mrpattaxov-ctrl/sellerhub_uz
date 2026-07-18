"""App-wide user error capture + instant admin Telegram alerts.

Two capture points, both registered by ``init_error_monitor(app)``:

1. ``after_request`` — any 4xx/5xx response the app's own try/except
   handlers produced (the JSON ``{"error": ...}`` convention used across
   all blueprints). This is what makes "all the errors from each user"
   visible without touching every route.
2. ``errorhandler(Exception)`` — unhandled exceptions. Stores the full
   traceback and returns the same JSON 500 shape ``handle_500`` used.

Every captured error is written to ``error_events`` (deduplicated by
fingerprint inside a rolling window) and pushed IMMEDIATELY to every
Telegram chat connected to the admin alert bot (``ADMIN_TELEGRAM_TOKEN``,
see ``core.admin_bot``). A per-fingerprint rate limiter (Redis, in-memory
fallback) keeps a retry-spamming user from flooding the admin: the first
hit alerts instantly, repeats within the window only bump ``count``.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time as _time
import traceback as _traceback
from datetime import datetime, timedelta

import requests
from sqlalchemy import select

from extensions import SessionLocal
from models import AdminAlertChat, ErrorEvent, Shop, User

# Rolling window (seconds) for both DB dedup and Telegram re-alerting.
DEDUP_WINDOW_SEC = 600
# Background loops retry on a schedule (30 min / hourly), so their dedup and
# re-alert window is much longer: a dead token = 1 alert per 6 h, not 24/day.
BG_DEDUP_WINDOW_SEC = 6 * 3600

# Statuses that are "normal" app flow, not user-facing failures.
_SKIP_STATUSES = {401, 429}

# In-memory fallback rate limiter when Redis is down: fingerprint -> ts.
_local_alert_ts: dict[str, float] = {}
_local_alert_lock = threading.Lock()


def _admin_bot_token() -> str:
    return (os.environ.get("ADMIN_TELEGRAM_TOKEN") or "").strip()


def _tashkent_now() -> datetime:
    return datetime.utcnow() + timedelta(hours=5)


# ── capture policy ──────────────────────────────────────────────────────────

def _should_record(path: str, status: int, authenticated: bool) -> bool:
    if status < 400:
        return False
    if path.startswith("/static/") or path == "/favicon.ico":
        return False
    if status in _SKIP_STATUSES:
        return False
    # Unauthenticated traffic is mostly bots probing public URLs — only
    # real server failures matter there. For logged-in users capture
    # everything: 400 validation, 403 denied, 404 API misses, 5xx.
    if not authenticated and status < 500:
        return False
    # Anonymous HTML 404s (wp-admin scanners etc.) never reach here because
    # of the rule above; authenticated HTML 404s are kept — a user hitting
    # a dead link IS a problem worth seeing.
    return True


def _extract_error_message(response) -> str:
    """Pull the human part out of the JSON error body the app returns."""
    try:
        if response.is_json:
            data = response.get_json(silent=True) or {}
            if isinstance(data, dict):
                msg = data.get("error") or data.get("detail") or data.get("message")
                if msg:
                    return str(msg)[:500]
    except Exception:
        pass
    return (response.status or str(response.status_code))[:500]


def _fingerprint(user_id, method: str, path: str, status: int, message: str) -> str:
    raw = f"{user_id}|{method}|{path}|{status}|{message[:200]}"
    return hashlib.md5(raw.encode("utf-8", "replace")).hexdigest()


# ── persistence ─────────────────────────────────────────────────────────────

def record_error(
    *,
    user_id: int | None,
    username: str | None,
    path: str,
    method: str,
    endpoint: str | None,
    referer: str | None,
    status_code: int,
    error_message: str,
    tb: str | None = None,
    dedup_sec: int = DEDUP_WINDOW_SEC,
) -> None:
    """Insert (or dedup-bump) an error_events row, then alert the admin.

    Never raises — a failure in monitoring must not break the request.
    """
    try:
        fp = _fingerprint(user_id, method, path, status_code, error_message)
        now = datetime.utcnow()
        repeat_count = 1
        with SessionLocal() as db:
            recent = db.execute(
                select(ErrorEvent)
                .where(
                    ErrorEvent.fingerprint == fp,
                    ErrorEvent.last_seen_at > now - timedelta(seconds=dedup_sec),
                )
                .order_by(ErrorEvent.id.desc())
                .limit(1)
            ).scalar_one_or_none()
            if recent is not None:
                recent.count = int(recent.count or 1) + 1
                recent.last_seen_at = now
                if tb and not recent.traceback:
                    recent.traceback = tb
                repeat_count = recent.count
            else:
                db.add(ErrorEvent(
                    fingerprint=fp,
                    user_id=user_id,
                    username=username,
                    path=path[:500],
                    method=(method or "GET")[:10],
                    endpoint=(endpoint or None),
                    referer=(referer or None),
                    status_code=int(status_code),
                    error_message=error_message[:2000],
                    traceback=tb,
                ))
            db.commit()

        _alert_admin_async(
            fingerprint=fp,
            alert_window_sec=dedup_sec,
            user_id=user_id,
            username=username,
            path=path,
            method=method,
            referer=referer,
            status_code=status_code,
            error_message=error_message,
            repeat_count=repeat_count,
        )
    except Exception as exc:
        print(f"[ErrorMonitor] record_error failed: {exc!r}")


def report_background_error(source: str, shop_uzum_id, message: str,
                            *, user_id: int | None = None) -> None:
    """Report a background-loop failure (finance/products/FBS sync, add-shop
    backfill) as an error event + instant admin Telegram alert.

    Attribution: resolves the shop's owner so the alert carries the affected
    user's contacts — a per-user OpenAPI token that stopped working shows up
    as THAT user's problem, not an anonymous log line. Long dedup window
    (``BG_DEDUP_WINDOW_SEC``): a loop retrying every 30-60 min bumps one row
    and re-alerts at most every 6 h while the failure persists.

    Never raises — safe to call from any loop/thread.
    """
    try:
        username = None
        shop_label = str(shop_uzum_id).strip() if shop_uzum_id is not None else ""
        try:
            with SessionLocal() as db:
                if user_id is None and shop_label:
                    shop = db.execute(
                        select(Shop).where(Shop.uzum_id == shop_label)
                    ).scalar_one_or_none()
                    if shop is not None:
                        if shop.name:
                            shop_label = f"{shop.name} ({shop_label})"
                        if shop.owner_id:
                            user_id = int(shop.owner_id)
                if user_id is not None:
                    owner = db.get(User, int(user_id))
                    if owner is not None:
                        username = owner.username
        except Exception as exc:
            print(f"[ErrorMonitor] bg attribution failed: {exc!r}")

        msg = f"Магазин {shop_label}: {message}" if shop_label else str(message)
        record_error(
            user_id=user_id,
            username=username,
            path=f"background:{source}",
            method="BG",
            endpoint=None,
            referer=None,
            status_code=502,
            error_message=msg[:2000],
            dedup_sec=BG_DEDUP_WINDOW_SEC,
        )
    except Exception as exc:
        print(f"[ErrorMonitor] report_background_error failed: {exc!r}")


# ── Telegram alerting ───────────────────────────────────────────────────────

def _alert_rate_ok(fp: str, window_sec: int = DEDUP_WINDOW_SEC) -> bool:
    """One Telegram alert per fingerprint per window. Fails open to local."""
    try:
        from core.redis_client import redis_client
        return bool(redis_client.set(f"erralert:{fp}", "1", nx=True, ex=window_sec))
    except Exception:
        now = _time.time()
        with _local_alert_lock:
            last = _local_alert_ts.get(fp, 0)
            if now - last < window_sec:
                return False
            _local_alert_ts[fp] = now
            if len(_local_alert_ts) > 2000:
                cutoff = now - DEDUP_WINDOW_SEC
                for k in [k for k, v in _local_alert_ts.items() if v < cutoff]:
                    _local_alert_ts.pop(k, None)
            return True


def _esc(s) -> str:
    return (str(s or "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _user_contact_lines(user_id: int | None, username: str | None) -> list[str]:
    """👤 name / 📱 phone / ✈️ telegram — pulled fresh from users table."""
    name = username or "аноним"
    phone = tg_id = None
    if user_id:
        try:
            with SessionLocal() as db:
                u = db.get(User, int(user_id))
                if u is not None:
                    name = u.username or name
                    phone = (u.phone or "").strip() or None
                    tg_id = (u.telegram_id or "").strip() or None
        except Exception:
            pass
    lines = [f"👤 <b>{_esc(name)}</b>"]
    if phone:
        lines.append(f"📱 {_esc(phone)}")
    if tg_id:
        lines.append(f'✈️ <a href="tg://user?id={_esc(tg_id)}">Telegram</a> · id {_esc(tg_id)}')
    if not phone and not tg_id:
        lines.append("📵 контактов нет")
    return lines


def format_error_alert(
    *, user_id, username, path, method, referer,
    status_code, error_message, repeat_count,
) -> str:
    if method == "BG":
        # Background-loop failure (finance/products/FBS fetch, backfill).
        source = path.split(":", 1)[-1] if ":" in path else path
        parts = [f"⚙️ <b>Фоновая ошибка · {_esc(source)}</b>"]
        parts.append("")
        parts.extend(_user_contact_lines(user_id, username))
        parts.append("")
    else:
        sev = "🔴" if int(status_code) >= 500 else "🟠"
        parts = [f"{sev} <b>Ошибка {int(status_code)}</b>"]
        parts.append("")
        parts.extend(_user_contact_lines(user_id, username))
        parts.append("")
        parts.append(f"📍 <code>{_esc(method)} {_esc(path[:200])}</code>")
    ref = (referer or "").strip()
    if ref:
        # show only the path part of the referer — the page the user was on
        for pre in ("https://", "http://"):
            if ref.startswith(pre):
                ref = ref[len(pre):]
                ref = ref[ref.find("/"):] if "/" in ref else "/"
                break
        if ref and ref != path:
            parts.append(f"🖥 страница: <code>{_esc(ref[:200])}</code>")
    msg = (error_message or "").strip()
    if msg:
        parts.append(f"💬 {_esc(msg[:300])}")
    stamp = _tashkent_now().strftime("%d.%m %H:%M")
    tail = f"🕐 {stamp}"
    if repeat_count > 1:
        tail += f" · повтор ×{repeat_count}"
    parts.append(tail)
    return "\n".join(parts)


def _active_alert_chat_ids() -> list[str]:
    try:
        with SessionLocal() as db:
            return [
                row.chat_id for row in db.execute(
                    select(AdminAlertChat).where(AdminAlertChat.is_active == True)  # noqa: E712
                ).scalars()
            ]
    except Exception as exc:
        print(f"[ErrorMonitor] alert-chat lookup failed: {exc!r}")
        return []


def send_admin_alert(text: str) -> int:
    """Push ``text`` (HTML) to every connected admin chat. Returns sent count."""
    token = _admin_bot_token()
    if not token:
        return 0
    chat_ids = _active_alert_chat_ids()
    sent = 0
    for chat_id in chat_ids:
        for attempt in range(3):
            try:
                r = requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                    timeout=(5, 15),
                )
                if r.ok:
                    sent += 1
                    break
                print(f"[ErrorMonitor] sendMessage {chat_id} -> {r.status_code}: {r.text[:120]}")
                if r.status_code == 403:  # blocked the bot — don't retry
                    break
            except Exception as exc:
                print(f"[ErrorMonitor] sendMessage {chat_id} attempt {attempt + 1} failed: {exc!r}")
                _time.sleep(1 + attempt)
    return sent


def _alert_admin_async(*, fingerprint: str, alert_window_sec: int = DEDUP_WINDOW_SEC, **kw) -> None:
    if not _admin_bot_token():
        return
    if not _alert_rate_ok(fingerprint, alert_window_sec):
        return
    text = None
    try:
        text = format_error_alert(**kw)
    except Exception as exc:
        print(f"[ErrorMonitor] alert format failed: {exc!r}")
        return
    threading.Thread(
        target=send_admin_alert, args=(text,), daemon=True,
        name="error-alert-send",
    ).start()


# ── Admin Uzum token refresh status ─────────────────────────────────────────
# The auto-login scheduler (core.auth_helpers._uzum_auto_login, every 90 min)
# reports here. Alerts on the ok→fail transition, reminds every 6 h while
# still failing, and sends a recovery message on fail→ok — so a dead admin
# token can't go unnoticed, but a long outage doesn't spam every 90 min.

_TOKEN_STATE_KEY = "admtoken:state"
_TOKEN_REMIND_KEY = "admtoken:reminded"
_TOKEN_REMIND_SEC = 6 * 3600
_token_local_state: dict[str, str | None] = {"state": None}


def notify_admin_token_status(ok: bool, reason: str = "") -> None:
    """Record the latest admin-token refresh outcome and alert on changes."""
    try:
        state = "ok" if ok else "fail"
        prev = _token_local_state["state"]
        try:
            from core.redis_client import redis_client
            raw = redis_client.get(_TOKEN_STATE_KEY)
            if raw is not None:
                prev = raw.decode() if isinstance(raw, bytes) else str(raw)
            redis_client.set(_TOKEN_STATE_KEY, state)
        except Exception:
            pass
        _token_local_state["state"] = state

        if ok:
            if prev == "fail":
                text = (
                    "✅ <b>Токен Uzum снова в порядке</b>\n\n"
                    "Авто-логин прошёл успешно, токен администратора обновлён.\n"
                    f"🕐 {_tashkent_now().strftime('%d.%m %H:%M')}"
                )
                threading.Thread(target=send_admin_alert, args=(text,), daemon=True).start()
            return

        send_now = prev != "fail"
        try:
            from core.redis_client import redis_client
            if send_now:
                redis_client.set(_TOKEN_REMIND_KEY, "1", ex=_TOKEN_REMIND_SEC)
            else:
                send_now = bool(redis_client.set(
                    _TOKEN_REMIND_KEY, "1", nx=True, ex=_TOKEN_REMIND_SEC
                ))
        except Exception:
            pass  # Redis down → alert only on the in-process transition
        if send_now:
            text = (
                "🔑 <b>Проблема с токеном Uzum!</b>\n\n"
                "Не удалось обновить токен администратора (авто-логин каждые 90 мин).\n"
                + (f"💬 {_esc(reason[:300])}\n\n" if reason else "\n")
                + "Запросы к Uzum могут перестать работать, пока токен не обновится.\n"
                f"🕐 {_tashkent_now().strftime('%d.%m %H:%M')}"
            )
            threading.Thread(target=send_admin_alert, args=(text,), daemon=True).start()
    except Exception as exc:
        print(f"[ErrorMonitor] token-status notify failed: {exc!r}")


# ── Flask wiring ────────────────────────────────────────────────────────────

def init_error_monitor(app) -> None:
    from flask import g, request
    from flask_login import current_user

    def _who():
        try:
            if current_user.is_authenticated:
                return int(current_user.get_id()), getattr(current_user, "username", None)
        except Exception:
            pass
        return None, None

    @app.after_request
    def _capture_error_responses(response):
        try:
            if getattr(g, "_error_event_logged", False):
                return response
            uid, uname = _who()
            if not _should_record(request.path, response.status_code, uid is not None):
                return response
            record_error(
                user_id=uid,
                username=uname,
                path=request.path,
                method=request.method,
                endpoint=request.endpoint,
                referer=request.headers.get("Referer"),
                status_code=response.status_code,
                error_message=_extract_error_message(response),
            )
        except Exception as exc:
            print(f"[ErrorMonitor] after_request capture failed: {exc!r}")
        return response

    @app.errorhandler(Exception)
    def _capture_unhandled_exception(e):
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException):
            # abort(4xx/5xx) responses flow through after_request above.
            return e
        tb = _traceback.format_exc()
        print(tb)
        try:
            uid, uname = _who()
            record_error(
                user_id=uid,
                username=uname,
                path=request.path,
                method=request.method,
                endpoint=request.endpoint,
                referer=request.headers.get("Referer"),
                status_code=500,
                error_message=f"{type(e).__name__}: {e}"[:500],
                tb=tb,
            )
            g._error_event_logged = True
        except Exception as exc:
            print(f"[ErrorMonitor] exception capture failed: {exc!r}")
        from core.auth_helpers import _json_response
        return _json_response({"error": "Internal server error", "detail": str(e)}, 500)
