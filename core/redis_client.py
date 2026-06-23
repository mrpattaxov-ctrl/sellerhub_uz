"""Shared Redis client and small helpers.

Step 1 of project_scaling_roadmap_20k.md: one process-wide ConnectionPool /
``redis.Redis`` singleton so Gunicorn workers and the background worker each
keep a single reusable pool instead of opening a socket per call.

If Redis is unreachable at runtime the client raises ``redis.RedisError`` to
the caller — we deliberately do NOT fall back to an in-process dict, because
the whole point of Step 1 is a single shared cache across workers.
"""
from __future__ import annotations

import logging

import redis

from config import REDIS_URL

logger = logging.getLogger(__name__)

# decode_responses=True so callers work with ``str`` and can json.loads directly.
# 2s timeouts keep a dead Redis from stalling a request thread.
_pool: redis.ConnectionPool = redis.ConnectionPool.from_url(
    REDIS_URL,
    decode_responses=True,
    socket_timeout=2.0,
    socket_connect_timeout=2.0,
    health_check_interval=30,
)

redis_client: redis.Redis = redis.Redis(connection_pool=_pool)


_REVOKED_USERS_KEY = "revoked_users"

# Users whose signed-session subscription cache must be recomputed from the DB
# on their next request. A subscription change (admin downgrade, code removal,
# Payme event, trial start) flags the user here so the gate bypasses the
# session fast-path exactly once and lets the change reach a live session —
# even a sticky "unlimited" one that would otherwise never be re-checked.
_RECHECK_USERS_KEY = "subscription_recheck_users"


def is_user_revoked(user_id: int | str) -> bool:
    return bool(redis_client.sismember(_REVOKED_USERS_KEY, str(user_id)))


def revoke_user(user_id: int | str) -> None:
    redis_client.sadd(_REVOKED_USERS_KEY, str(user_id))


def unrevoke_user(user_id: int | str) -> None:
    redis_client.srem(_REVOKED_USERS_KEY, str(user_id))


def mark_user_for_recheck(user_id: int | str) -> None:
    redis_client.sadd(_RECHECK_USERS_KEY, str(user_id))


def clear_user_recheck(user_id: int | str) -> None:
    redis_client.srem(_RECHECK_USERS_KEY, str(user_id))


def subscription_gate_state(user_id: int | str) -> tuple[bool, bool]:
    """Return ``(is_revoked, pending_recheck)`` in a single Redis round-trip.

    Used by the per-request subscription gate so checking both sets costs the
    same latency as the single SISMEMBER it replaced.
    """
    uid = str(user_id)
    pipe = redis_client.pipeline()
    pipe.sismember(_REVOKED_USERS_KEY, uid)
    pipe.sismember(_RECHECK_USERS_KEY, uid)
    revoked, recheck = pipe.execute()
    return bool(revoked), bool(recheck)
