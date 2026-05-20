"""Behavioral tests for Step 4 — core/shop_lock.py.

Runs every numbered check in the Step 4 testing plan and prints PASS/FAIL.
Cleans up all shop_lock:test_step4_* keys on exit, even on failure.
"""
from __future__ import annotations

import sys
import traceback

from core import shop_lock
from core.redis_client import redis_client


TEST_KEYS = [
    "test_step4_a",
    "test_step4_b",
    "test_step4_c",
    "test_step4_nope",
]

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {name} :: {detail}")


def cleanup() -> None:
    for k in TEST_KEYS:
        redis_client.delete(f"shop_lock:{k}")


try:
    cleanup()  # pre-clean in case a prior run died mid-test

    # 1. Fresh acquire returns True
    r = shop_lock.try_acquire_shop_lock("test_step4_a")
    record("1_fresh_acquire_True", r is True, f"returned {r!r}")

    # 2. Double acquire returns False
    r2 = shop_lock.try_acquire_shop_lock("test_step4_a")
    record("2_double_acquire_False", r2 is False, f"returned {r2!r}")

    # 3. TTL between 295 and 300
    ttl = redis_client.ttl("shop_lock:test_step4_a")
    record("3_ttl_295_to_300", 295 <= ttl <= 300, f"ttl={ttl}")

    # 4. is_shop_active matches
    act_a = shop_lock.is_shop_active("test_step4_a")
    act_n = shop_lock.is_shop_active("test_step4_nope")
    record(
        "4_is_shop_active_matches",
        act_a is True and act_n is False,
        f"active('a')={act_a}, active('nope')={act_n}",
    )

    # 5. active_shops includes it
    shops = shop_lock.active_shops()
    record(
        "5_active_shops_includes",
        "test_step4_a" in shops,
        f"active_shops()={shops}",
    )

    # 6. Release works; then re-acquire
    shop_lock.release_shop_lock("test_step4_a")
    after_release_active = shop_lock.is_shop_active("test_step4_a")
    reacq = shop_lock.try_acquire_shop_lock("test_step4_a")
    record(
        "6_release_and_reacquire",
        after_release_active is False and reacq is True,
        f"active_after_release={after_release_active}, reacquire={reacq}",
    )

    # 7. Release idempotent (call twice)
    err = None
    try:
        shop_lock.release_shop_lock("test_step4_a")
        shop_lock.release_shop_lock("test_step4_a")
    except Exception as exc:  # noqa: BLE001
        err = exc
    record("7_release_idempotent", err is None, f"err={err!r}")

    # 8. active_shops SCAN smoke check — 3 locks, all appear in sorted order.
    shop_lock.try_acquire_shop_lock("test_step4_a")
    shop_lock.try_acquire_shop_lock("test_step4_b")
    shop_lock.try_acquire_shop_lock("test_step4_c")
    shops = shop_lock.active_shops()
    test_subset = [s for s in shops if s.startswith("test_step4_")]
    expected = ["test_step4_a", "test_step4_b", "test_step4_c"]
    record(
        "8_active_shops_scan_sorted",
        test_subset == expected,
        f"test_subset={test_subset}, full_active_shops={shops}",
    )

finally:
    cleanup()
    # Verify cleanup
    leftovers = [k for k in TEST_KEYS if redis_client.exists(f"shop_lock:{k}")]
    if leftovers:
        print(f"[WARN] leftover keys: {leftovers}")
    else:
        print("[CLEAN] no leftover test keys")

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\nSUMMARY: {passed}/{total} behavioral checks passed")
    sys.exit(0 if passed == total else 1)
