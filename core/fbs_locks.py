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
