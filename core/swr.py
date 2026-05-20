"""Stale-while-revalidate helper for Redis-backed caches (Step 3 of project_scaling_roadmap_20k.md)."""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable

import redis

from core.redis_client import redis_client

logger = logging.getLogger(__name__)

_LOCK_KEY_PREFIX = "swr_lock:"
_LOCK_TTL_SECONDS = 60


def _encode(value: Any, fresh_until: float) -> str:
    return json.dumps({"value": value, "fresh_until": float(fresh_until)})


def _decode(raw: str) -> tuple[Any, float] | None:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or "value" not in payload or "fresh_until" not in payload:
        return None
    try:
        return payload["value"], float(payload["fresh_until"])
    except (TypeError, ValueError):
        return None


def _store(key: str, value: Any, *, soft_ttl: int, hard_ttl: int) -> None:
    fresh_until = time.time() + soft_ttl
    try:
        redis_client.setex(key, hard_ttl, _encode(value, fresh_until))
    except redis.RedisError:
        logger.warning("swr store failed key=%s", key, exc_info=True)


def _background_refresh(key: str, loader: Callable[[], Any], *, soft_ttl: int, hard_ttl: int) -> None:
    lock_key = f"{_LOCK_KEY_PREFIX}{key}"
    try:
        got_lock = redis_client.set(lock_key, "1", nx=True, ex=_LOCK_TTL_SECONDS)
    except redis.RedisError:
        logger.warning("swr lock acquire failed key=%s", key, exc_info=True)
        return
    if not got_lock:
        return
    try:
        fresh_value = loader()
        _store(key, fresh_value, soft_ttl=soft_ttl, hard_ttl=hard_ttl)
    except Exception:
        logger.exception("swr refresh loader failed key=%s", key)
    finally:
        try:
            redis_client.delete(lock_key)
        except redis.RedisError:
            logger.warning("swr lock release failed key=%s", key, exc_info=True)


def swr_get(
    key: str,
    *,
    soft_ttl: int,
    hard_ttl: int | None = None,
    loader: Callable[[], Any],
) -> Any:
    """Return cached value using stale-while-revalidate semantics."""
    effective_hard_ttl = hard_ttl if hard_ttl is not None else soft_ttl * 2

    try:
        raw = redis_client.get(key)
    except redis.RedisError:
        logger.warning("swr get failed; falling back to sync loader key=%s", key, exc_info=True)
        return loader()

    if raw:
        decoded = _decode(raw)
        if decoded is not None:
            value, fresh_until = decoded
            if time.time() < fresh_until:
                return value
            thread = threading.Thread(
                target=_background_refresh,
                args=(key, loader),
                kwargs={"soft_ttl": soft_ttl, "hard_ttl": effective_hard_ttl},
                name=f"swr-refresh-{key}",
                daemon=True,
            )
            thread.start()
            return value
        try:
            redis_client.delete(key)
        except redis.RedisError:
            logger.warning("swr delete of corrupt key failed key=%s", key, exc_info=True)

    value = loader()
    _store(key, value, soft_ttl=soft_ttl, hard_ttl=effective_hard_ttl)
    return value


def swr_invalidate(key: str) -> None:
    """Delete the cached entry so the next read synchronously recomputes."""
    try:
        redis_client.delete(key)
    except redis.RedisError:
        logger.warning("swr invalidate failed key=%s", key, exc_info=True)
