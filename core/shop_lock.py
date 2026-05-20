"""Cluster-wide per-shop finance-sync lock backed by Redis.

Step 4 of project_scaling_roadmap_20k.md: replace the per-process
``_finance_active_shops`` set with a Redis SET NX EX key so that a shop
cannot be synced concurrently by two Gunicorn workers or two VPS nodes.

Keys are short-lived (300s TTL) so a crashed worker cannot deadlock a shop
forever. The TTL is a safety ceiling only — happy-path code always calls
``release_shop_lock`` inside a ``finally`` block.

Consistent with ``core/redis_client.py``: if Redis is unreachable a
``redis.RedisError`` propagates to the caller. We deliberately do NOT fall
back to an in-process dict — the whole point of this step is shared state
across workers and machines.
"""
from __future__ import annotations

from core.redis_client import redis_client

_SHOP_LOCK_PREFIX = "shop_lock:"
_SHOP_LOCK_TTL = 300  # seconds; crash-safety ceiling


def _key(shop_id: str) -> str:
    return f"{_SHOP_LOCK_PREFIX}{shop_id}"


def try_acquire_shop_lock(shop_id: str) -> bool:
    """Atomically claim the per-shop finance-sync lock.

    Executes ``SET shop_lock:<id> 1 NX EX 300``. Returns True if we now
    own the lock, False if another worker already holds it.
    """
    # nx=True + ex=... is atomic server-side; do NOT split into SETNX+EXPIRE.
    result = redis_client.set(_key(shop_id), 1, nx=True, ex=_SHOP_LOCK_TTL)
    return bool(result)


def release_shop_lock(shop_id: str) -> None:
    """Release the per-shop finance-sync lock. Idempotent."""
    redis_client.delete(_key(shop_id))


def is_shop_active(shop_id: str) -> bool:
    """Return True iff a lock exists for this shop right now."""
    return bool(redis_client.exists(_key(shop_id)))


def active_shops() -> list[str]:
    """Return sorted list of shop_ids that currently hold a sync lock.

    Uses SCAN (not KEYS) so we never block Redis on a large keyspace.
    """
    shops: list[str] = []
    prefix_len = len(_SHOP_LOCK_PREFIX)
    for key in redis_client.scan_iter(match=f"{_SHOP_LOCK_PREFIX}*", count=500):
        # decode_responses=True on the client -> key is already str.
        shops.append(key[prefix_len:])
    shops.sort()
    return shops
