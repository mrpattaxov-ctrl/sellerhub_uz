"""Shared per-token mutexes for the FBS sync paths.

Both the background worker (``app.py::_fbs_sync_one_shop``) and the
on-demand JIT refresh (``core/fbs_data.py::_refresh_shop_status``) issue
Uzum ``/v2/fbs/orders`` calls under the same OpenAPI token when a user
has multiple shops attached. Uzum's hidden per-token burst pattern
trips when more than one call lands on a token within the same second,
so both call sites must serialise through the same lock.

Living in its own module keeps the registry above ``app.py`` and
``core/fbs_data.py`` in the import graph — neither imports the other
yet, and we want to keep it that way.
"""
from __future__ import annotations

import threading
import time as _time

_token_locks: dict[str, threading.Lock] = {}
_creation_lock = threading.Lock()

# ── Bosqich A.6 — per-token min-interval gate ─────────────────────────
# Uzum trips its hidden per-token burst penalty when more than one
# ``/v2/fbs/orders`` call lands on a token within the same second. The
# old approach held ``get_token_lock`` across a whole 11-status worker
# sweep so the JIT refresh ("Yangilash") would just time out and skip
# during a tick — the seller saw the refresh do nothing.
#
# This gate replaces that: every caller (worker AND Yangilash) reserves
# the next ``>= MIN_INTERVAL``-second slot for the token and sleeps until
# it. The two paths then INTERLEAVE — paced exactly 1s apart — instead of
# one starving the other. Reservation is O(1) dict math under a brief
# lock; the wait happens OUTSIDE the lock so a sleeping caller never
# blocks others from reserving their own (later) slots.
#
# SCOPE: this is in-process (one ``_token_next_slot`` dict per process),
# the same coordination scope as the old ``threading.Lock``. The fbs-sync
# worker runs in one gunicorn process and a given Yangilash runs in
# whichever process served the request, so cross-process pacing is NOT
# guaranteed here — a Redis-backed limiter would be needed for that. In
# practice Uzum tolerates the occasional cross-process near-collision
# (the app has run on the coarse in-process lock without bans), so we
# keep the no-new-infra in-process design.
_MIN_UZUM_CALL_INTERVAL_SEC = 1.0
_token_next_slot: dict[str, float] = {}
_pace_lock = threading.Lock()


# ── Background yield gate (2026-07-13) ───────────────────────────────
# Uzum's real budget, read off its own response headers, is 2 req/s per token
# (replenish-rate 2, burst-capacity 2) — and the shared bucket is configured to
# spend exactly that. So a background sweep, whose calls land ~0.5s apart, sits
# ON the ceiling: the seller's click arrives as the third request in that second
# and takes a 429 (absorbed by a retry, but the press stretches to ~2.5s).
#
# The worker has no deadline — its statuses are on 23-63 minute cadences — so it
# gives way: background callers pace themselves to ONE call per second, leaving
# the other half of the budget permanently free for whoever is actually waiting
# on a screen. Abdulaziz 2026-07-13: "worker 1/s da qilsin, bizga tezlik muhim
# emas."
#
# Keyed separately (``bg:<token>``) so an interactive call NEVER waits behind a
# worker slot — the two paths reserve from different sequences.
_BG_MIN_UZUM_CALL_INTERVAL_SEC = 1.0


# ── Interactive priority (2026-07-13) ────────────────────────────────
# Pacing the worker to 1/s leaves half the budget free, but a seller clicking
# through chips can want more than that half, and then the two still collide
# (measured: 1 press in 5 took a 429 + retry → ~2s instead of ~0.3s).
#
# So the worker doesn't just walk slower, it STEPS ASIDE: while an FBS screen is
# actively talking to Uzum, background calls wait and the seller gets the whole
# 2/s. The worker loses nothing — its statuses are on 23-63 minute cadences.
#
# The flag lives in Redis, not in memory: the seller's request is served by some
# gunicorn worker process while the sync loop runs in another, so an in-process
# flag would be invisible to exactly the process that must see it. No Redis →
# the yield is skipped (the 1/s pacing still applies), never an error.
#
# _BG_MAX_YIELD_SEC caps the stepping-aside so a seller who never stops clicking
# cannot starve the worker: past that, background work proceeds regardless.
_ACTIVE_TTL_SEC = 5          # how long one interactive call keeps the lane busy
_BG_MAX_YIELD_SEC = 30.0     # never let background starve longer than this
_BG_YIELD_POLL_SEC = 0.25


def _active_key(token: str) -> str:
    """Redis key for "an FBS screen is using this token right now".

    The token is hashed — a raw OpenAPI token must not sit in a Redis key.
    """
    import hashlib
    return f"fbs:interactive:{hashlib.sha1((token or '').encode()).hexdigest()[:16]}"


def mark_interactive_activity(token: str) -> None:
    """Claim the token's Uzum budget for a live FBS screen for the next few
    seconds. Called on every interactive FBS request; best-effort by design —
    if Redis is down the worker simply doesn't yield.
    """
    if not token:
        return
    try:
        from core.redis_client import redis_client
        redis_client.setex(_active_key(token), _ACTIVE_TTL_SEC, "1")
    except Exception:
        pass


def _interactive_active(token: str) -> bool:
    try:
        from core.redis_client import redis_client
        return bool(redis_client.exists(_active_key(token)))
    except Exception:
        return False


def pace_background_call(token: str, *, _sleep=_time.sleep,
                         _monotonic=_time.monotonic) -> float:
    """Gate a BACKGROUND (worker/prefetch) Uzum call.

    Two steps: step aside while a seller is actively using this token (bounded
    by ``_BG_MAX_YIELD_SEC``), then hold to ≤1 call/sec so even an idle-looking
    moment keeps headroom.

    Interactive requests must never call this — they are what it protects.
    """
    waited = 0.0
    while waited < _BG_MAX_YIELD_SEC and _interactive_active(token):
        _sleep(_BG_YIELD_POLL_SEC)
        waited += _BG_YIELD_POLL_SEC
    if waited:
        print(f"[fbs_locks] background yielded {waited:.1f}s to a live FBS screen")
    return pace_uzum_call(
        f"bg:{token}", min_interval=_BG_MIN_UZUM_CALL_INTERVAL_SEC,
    )


def get_token_lock(token: str) -> threading.Lock:
    """Return the lock for ``token`` (creating it on first use).

    Lookup-after-write is dict-only (atomic in CPython); the creation
    lock prevents two threads racing to construct a fresh ``Lock`` for
    the same token at the same time. Different tokens always get
    different locks, so users don't block each other.
    """
    lock = _token_locks.get(token)
    if lock is not None:
        return lock
    with _creation_lock:
        lock = _token_locks.get(token)
        if lock is None:
            lock = threading.Lock()
            _token_locks[token] = lock
    return lock


def pace_uzum_call(
    token: str,
    *,
    min_interval: float = _MIN_UZUM_CALL_INTERVAL_SEC,
    _monotonic=_time.monotonic,
    _sleep=_time.sleep,
) -> float:
    """Block until it is safe to issue the next Uzum ``/orders`` call on
    ``token``, enforcing ``>= min_interval`` seconds between consecutive
    calls across every caller in this process (worker + JIT refresh).

    Reserves a monotonically increasing time slot per token: the next
    slot is ``max(now, last_slot + min_interval)``. Concurrent callers
    therefore get distinct, 1-second-spaced slots and never burst Uzum.
    The first call for a token never waits.

    Returns the reserved slot time (monotonic seconds) — handy for tests
    and logging. ``_monotonic`` / ``_sleep`` are injectable so the pacing
    math can be unit-tested with a fake clock (no real sleeping).

    NOTE: ALL Uzum FBS calls on a token share this gate (Bosqich A.8) —
    both ``/v2/fbs/orders`` page fetches and ``/v2/fbs/orders/count`` calls.
    The earlier "/count is burst-exempt" assumption was unverified, and
    firing 3 concurrent /count calls per Yangilash press was a prime 429
    (burst-penalty) source, so /count is now paced like everything else.
    """
    with _pace_lock:
        now = _monotonic()
        last = _token_next_slot.get(token)
        if last is None or now >= last + min_interval:
            slot = now
        else:
            slot = last + min_interval
        _token_next_slot[token] = slot
    wait = slot - _monotonic()
    if wait > 0:
        _sleep(wait)
    return slot


def prune_token_slots(active_tokens) -> int:
    """Drop slot bookkeeping for tokens not in ``active_tokens``.

    ``_token_next_slot`` gains one entry per token that ever calls
    :func:`pace_uzum_call` and never loses one, so on a long-lived
    multi-tenant process it would slowly accumulate entries for
    deactivated sellers (their OpenAPI token never reappears). The worker
    calls this once per tick with the current active-token set to reclaim
    those. Guarded by ``_pace_lock`` so it is safe to run concurrently
    with :func:`pace_uzum_call`. Returns the number of entries dropped.
    """
    keep = set(active_tokens)
    with _pace_lock:
        stale = [t for t in _token_next_slot if t not in keep]
        for t in stale:
            del _token_next_slot[t]
    return len(stale)


__all__ = ["get_token_lock", "pace_uzum_call", "prune_token_slots"]
