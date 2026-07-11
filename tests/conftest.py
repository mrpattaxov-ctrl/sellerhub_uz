"""Pytest configuration shared by all FBS tests.

The pure-function tests in ``test_fbs_sync.py`` don't touch the DB at
all, but importing ``core.fbs_sync`` pulls in ``extensions`` which
greedily calls ``create_engine`` at import time. That fails in a bare
test environment that has SQLAlchemy but no Postgres driver installed.

To keep the test environment minimal — no Postgres, no flask_login
runtime requirements — we stub ``extensions`` with a fake module before
any test module imports ``core.fbs_sync``. ``SessionLocal`` ends up as
None; if a future test accidentally calls it, the AttributeError makes
the mistake obvious.
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://test:test@localhost:5432/test_fbs",
)

# config.py refuses to import without a real SECRET_KEY (session-signing
# guard added on the env-token branch). Tests don't sign anything real, so a
# deterministic throwaway key is enough to get past the import-time check.
os.environ.setdefault(
    "SECRET_KEY",
    "test-only-secret-key-not-used-in-production-0000000000000000",
)

# Make the project root importable so ``import core.fbs_sync`` works
# when pytest is run from anywhere.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Stub ``extensions`` before fbs_sync imports it. The pure functions
# under test never touch SessionLocal — they take a session-or-None
# argument or operate on plain dicts.
if "extensions" not in sys.modules:
    _ext = types.ModuleType("extensions")
    _ext.SessionLocal = None  # accessed-but-not-called by our tests
    sys.modules["extensions"] = _ext

# Stub the ``redis`` driver too. ``core.redis_client`` builds a
# ConnectionPool + Redis client at import time, and ``core.fbs_data``
# (imported transitively by the route-layer tests) pulls it in. The
# real driver isn't installed in the bare test env, and even if it were
# there's no Redis server to connect to. The pure-function tests never
# touch the cache, so a no-op fake that survives import is enough; any
# accidental real call returns None rather than raising, keeping the
# failure mode obvious without a hard crash at collection time.
if "redis" not in sys.modules:
    _redis = types.ModuleType("redis")

    class _FakeConnectionPool:
        @classmethod
        def from_url(cls, *args, **kwargs):
            return cls()

    class _FakeRedis:
        def __init__(self, *args, **kwargs):
            pass

        def __getattr__(self, _name):
            # Any method (sismember/get/set/...) → no-op returning None.
            return lambda *a, **k: None

    class _RedisError(Exception):
        pass

    _redis.ConnectionPool = _FakeConnectionPool
    _redis.Redis = _FakeRedis
    _redis.RedisError = _RedisError
    _redis.exceptions = types.SimpleNamespace(RedisError=_RedisError)
    sys.modules["redis"] = _redis
