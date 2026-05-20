"""Step 3 SWR live-verification harness.

Checks:
  A) Stale-but-alive path returns stale value immediately AND spawns a background
     refresh thread named 'swr-refresh-<key>' that writes the new value.
  B) Fresh path returns value with zero loader calls.
  C) Missing-key path calls loader synchronously and SETEXes payload.
  D) swr_invalidate DELs the key.
  E) hard_ttl defaults to soft_ttl * 2.
  F) Background refresh lock key 'swr_lock:<key>' is created with EX=60 then released.
  G) Throughput microbenchmark: 50 swr_get calls on a near-expiry key.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

# Ensure project root is importable
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.redis_client import redis_client  # noqa: E402
from core.swr import swr_get, swr_invalidate  # noqa: E402


TEST_KEY = "test_swr_key_step3"
LOCK_KEY = f"swr_lock:{TEST_KEY}"

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}: {detail}")


def cleanup() -> None:
    redis_client.delete(TEST_KEY)
    redis_client.delete(LOCK_KEY)


# ---------- Check C: missing-key synchronous path ----------
cleanup()
calls = {"n": 0}


def loader_initial():
    calls["n"] += 1
    return "seed-value"


v = swr_get(TEST_KEY, soft_ttl=60, loader=loader_initial)
raw = redis_client.get(TEST_KEY)
ttl = redis_client.ttl(TEST_KEY)
payload = json.loads(raw) if raw else None
record(
    "missing_key_sync_load",
    v == "seed-value" and calls["n"] == 1 and payload and payload["value"] == "seed-value",
    f"value={v!r} calls={calls['n']} stored={payload} ttl={ttl}",
)
# hard_ttl default (soft*2 = 120); Redis TTL should be <= 120 and > 60
record(
    "hard_ttl_default_soft_x2",
    110 <= ttl <= 120,
    f"ttl={ttl}s (expected ~120 = soft_ttl*2)",
)

# ---------- Check B: fresh path, zero loader calls ----------
calls2 = {"n": 0}


def loader_must_not_run():
    calls2["n"] += 1
    return "should-not-happen"


v2 = swr_get(TEST_KEY, soft_ttl=60, loader=loader_must_not_run)
record(
    "fresh_path_no_loader",
    v2 == "seed-value" and calls2["n"] == 0,
    f"value={v2!r} loader_calls={calls2['n']}",
)

# ---------- Check A + F: stale path serves stale + background refresh + lock ----------
# Forge a stale-but-alive payload directly.
stale_payload = {"value": "old", "fresh_until": time.time() - 10}
redis_client.setex(TEST_KEY, 3600, json.dumps(stale_payload))

loader_calls = {"n": 0}


def loader_refresh():
    # Pause briefly so we can observe both the thread name and the lock key.
    time.sleep(0.3)
    loader_calls["n"] += 1
    return "new"


t0 = time.time()
v3 = swr_get(TEST_KEY, soft_ttl=60, loader=loader_refresh)
elapsed_ms = (time.time() - t0) * 1000
record(
    "stale_serves_immediately",
    v3 == "old" and elapsed_ms < 100,  # should be essentially instant
    f"value={v3!r} elapsed_ms={elapsed_ms:.1f}",
)

# While loader sleeps 0.3s, thread should be alive and lock key present.
time.sleep(0.1)
live_threads = [t.name for t in threading.enumerate()]
swr_threads = [n for n in live_threads if n.startswith("swr-refresh-")]
lock_exists = redis_client.exists(LOCK_KEY)
lock_ttl = redis_client.ttl(LOCK_KEY)
record(
    "background_thread_spawned",
    any(n == f"swr-refresh-{TEST_KEY}" for n in swr_threads),
    f"swr_threads={swr_threads}",
)
record(
    "lock_key_with_ex60",
    bool(lock_exists) and 50 <= lock_ttl <= 60,
    f"lock_exists={bool(lock_exists)} lock_ttl={lock_ttl}",
)

# Wait for loader thread to finish and write new payload.
time.sleep(0.8)
raw_after = redis_client.get(TEST_KEY)
payload_after = json.loads(raw_after) if raw_after else None
record(
    "background_refresh_writes_new_value",
    payload_after is not None
    and payload_after["value"] == "new"
    and payload_after["fresh_until"] > time.time(),
    f"payload_after={payload_after} loader_calls={loader_calls['n']}",
)
# Lock should be released by now.
lock_released = not redis_client.exists(LOCK_KEY)
record(
    "lock_released_after_refresh",
    lock_released,
    f"lock_exists_after_refresh={not lock_released}",
)

# ---------- Check D: swr_invalidate DELs ----------
assert redis_client.exists(TEST_KEY)
swr_invalidate(TEST_KEY)
record(
    "swr_invalidate_deletes_key",
    not redis_client.exists(TEST_KEY),
    f"exists_after_invalidate={bool(redis_client.exists(TEST_KEY))}",
)

# ---------- Check E: explicit hard_ttl honored ----------
cleanup()
swr_get(TEST_KEY, soft_ttl=30, hard_ttl=300, loader=lambda: "x")
ttl_custom = redis_client.ttl(TEST_KEY)
record(
    "explicit_hard_ttl_honored",
    290 <= ttl_custom <= 300,
    f"ttl={ttl_custom}s (expected ~300)",
)

# ---------- Check G: microbenchmark — 50 stale-path calls ----------
# Force every call to hit the stale branch so we prove hot-path never calls loader synchronously.
redis_client.setex(
    TEST_KEY,
    3600,
    json.dumps({"value": "bench", "fresh_until": time.time() - 1000}),
)
bench_loader_calls = {"n": 0}


def bench_loader():
    bench_loader_calls["n"] += 1
    # The real loader for settings hits Postgres; simulate a tiny bit of work.
    time.sleep(0.05)
    return "bench-new"


durations_ms: list[float] = []
for _ in range(50):
    # Re-forge stale payload each time so every iteration is a stale read (not fresh),
    # but leave the lock alone so only the first iteration spawns a thread.
    redis_client.setex(
        TEST_KEY,
        3600,
        json.dumps({"value": "bench", "fresh_until": time.time() - 1000}),
    )
    t = time.time()
    swr_get(TEST_KEY, soft_ttl=60, loader=bench_loader)
    durations_ms.append((time.time() - t) * 1000)

# Wait for any outstanding refresh thread to drain before cleanup.
time.sleep(0.2)
p95 = sorted(durations_ms)[int(0.95 * len(durations_ms)) - 1]
mean = sum(durations_ms) / len(durations_ms)
record(
    "microbench_50_stale_calls_under_5ms_p95",
    p95 < 5.0,
    f"p95={p95:.2f}ms mean={mean:.2f}ms max={max(durations_ms):.2f}ms",
)

cleanup()

# ---------- Summary ----------
passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
print("\n" + "=" * 60)
print(f"SWR Step 3 verification: {passed}/{total} checks passed")
print("=" * 60)
for name, ok, detail in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
sys.exit(0 if passed == total else 1)
