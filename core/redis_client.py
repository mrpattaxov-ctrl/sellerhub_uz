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


def is_user_revoked(user_id: int | str) -> bool:
    return bool(redis_client.sismember(_REVOKED_USERS_KEY, str(user_id)))


def revoke_user(user_id: int | str) -> None:
    redis_client.sadd(_REVOKED_USERS_KEY, str(user_id))


def unrevoke_user(user_id: int | str) -> None:
    redis_client.srem(_REVOKED_USERS_KEY, str(user_id))
