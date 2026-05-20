"""
Uzum API rate limit probe.

Tests increasing concurrency levels against the lightest possible Uzum endpoint
(/api/seller/shop/{shopId}/product/getProducts?page=0&size=1) to find where
Uzum starts returning 429s or where response time spikes sharply.

Usage (run on the server where the app is deployed):
    cd /opt/uzum
    python scripts/test_uzum_rate_limit.py --shop-id YOUR_SHOP_ID --token "Bearer YOUR_TOKEN"

Or let it read the token from the DB automatically:
    python scripts/test_uzum_rate_limit.py --shop-id YOUR_SHOP_ID --use-db
"""

import argparse
import os
import sys
import time
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# ── Config ────────────────────────────────────────────────────────────────────
# Concurrency levels to test (requests fired simultaneously at each level)
LEVELS = [1, 4, 8, 16, 32, 64, 96, 128]

# How many requests to fire at each concurrency level
REQUESTS_PER_LEVEL = 20

# Pause between levels to let Uzum cool down
PAUSE_BETWEEN_LEVELS = 3  # seconds

# Request timeout
TIMEOUT = 15  # seconds


def make_headers(token: str) -> dict:
    auth = f"Bearer {token}" if not token.startswith("Bearer ") else token
    return {
        "Authorization": auth,
        "Origin": "https://seller.uzum.uz",
        "Referer": "https://seller.uzum.uz/",
        "Accept": "application/json",
    }


def probe_request(url: str, headers: dict) -> dict:
    """Fire one request, return timing + status."""
    t0 = time.monotonic()
    try:
        resp = requests.get(url, headers=headers, timeout=TIMEOUT)
        elapsed = time.monotonic() - t0
        return {
            "status": resp.status_code,
            "elapsed": elapsed,
            "error": None,
        }
    except requests.exceptions.Timeout:
        return {"status": -1, "elapsed": TIMEOUT, "error": "timeout"}
    except Exception as e:
        return {"status": -1, "elapsed": time.monotonic() - t0, "error": str(e)}


def run_level(workers: int, url: str, headers: dict) -> dict:
    """Fire REQUESTS_PER_LEVEL requests at given concurrency, return stats."""
    results = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(probe_request, url, headers) for _ in range(REQUESTS_PER_LEVEL)]
        for f in as_completed(futures):
            results.append(f.result())

    statuses = [r["status"] for r in results]
    times = [r["elapsed"] for r in results]
    errors = [r for r in results if r["error"] or r["status"] not in (200, 201)]

    ok = sum(1 for s in statuses if s in (200, 201))
    rate_limited = sum(1 for s in statuses if s == 429)
    other_errors = sum(1 for s in statuses if s not in (200, 201, 429))
    timeouts = sum(1 for r in results if r["error"] == "timeout")

    return {
        "workers": workers,
        "total": len(results),
        "ok": ok,
        "rate_limited_429": rate_limited,
        "other_errors": other_errors,
        "timeouts": timeouts,
        "avg_ms": round(statistics.mean(times) * 1000, 1),
        "p95_ms": round(sorted(times)[int(len(times) * 0.95)] * 1000, 1),
        "max_ms": round(max(times) * 1000, 1),
        "error_details": [e for e in errors[:3]],  # first 3 errors for diagnosis
    }


def get_token_from_db() -> str:
    """Read admin token from app DB. Requires app to be importable."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        from core.auth_helpers import _get_admin_token
        token = _get_admin_token()
        if not token:
            raise RuntimeError("No admin token found in DB")
        return token
    except ImportError as e:
        raise RuntimeError(f"Could not import app: {e}")


def main():
    parser = argparse.ArgumentParser(description="Test Uzum API rate limits")
    parser.add_argument("--shop-id", required=True, help="Uzum shop ID to test against")
    parser.add_argument("--token", help="API token (Bearer ...)")
    parser.add_argument("--use-db", action="store_true", help="Read token from app DB automatically")
    parser.add_argument("--levels", help="Comma-separated concurrency levels, e.g. 8,16,32,64,128")
    parser.add_argument("--requests", type=int, default=REQUESTS_PER_LEVEL,
                        help=f"Requests per level (default: {REQUESTS_PER_LEVEL})")
    args = parser.parse_args()

    # Resolve token
    if args.use_db:
        print("[*] Reading token from DB...")
        token = get_token_from_db()
        print(f"[*] Got token: {token[:20]}...")
    elif args.token:
        token = args.token
    else:
        parser.error("Provide --token or --use-db")

    # Resolve levels
    levels = LEVELS
    if args.levels:
        levels = [int(x.strip()) for x in args.levels.split(",")]

    requests_per_level = args.requests

    # The lightest Uzum endpoint: 1 product, no heavy data
    url = (f"https://api-seller.uzum.uz/api/seller/shop/{args.shop_id}"
           f"/product/getProducts?page=0&size=1&filter=ALL&sortBy=id&order=descending")
    headers = make_headers(token)

    print(f"\n{'='*65}")
    print(f"  Uzum API Rate Limit Probe")
    print(f"  URL: {url}")
    print(f"  Requests per level: {requests_per_level}")
    print(f"  Levels: {levels}")
    print(f"{'='*65}\n")

    print(f"{'Workers':>8}  {'OK':>4}  {'429':>5}  {'Err':>4}  {'Avg ms':>8}  {'P95 ms':>8}  {'Max ms':>8}  Verdict")
    print("-" * 65)

    previous_avg = None
    safe_limit = None

    for workers in levels:
        r = run_level(workers, url, headers)

        # Verdict logic
        if r["rate_limited_429"] > 0:
            verdict = f"[RATE LIMITED] ({r['rate_limited_429']} x 429)"
        elif r["timeouts"] > 0:
            verdict = f"[TIMEOUT] ({r['timeouts']})"
        elif r["other_errors"] > 0:
            verdict = f"[ERROR] ({r['other_errors']})"
        elif previous_avg and r["avg_ms"] > previous_avg * 2.5:
            verdict = "[LATENCY SPIKE >2.5x]"
        else:
            verdict = "OK"
            safe_limit = workers

        print(
            f"{r['workers']:>8}  {r['ok']:>4}  {r['rate_limited_429']:>5}  "
            f"{r['other_errors'] + r['timeouts']:>4}  "
            f"{r['avg_ms']:>8}  {r['p95_ms']:>8}  {r['max_ms']:>8}  {verdict}"
        )

        # Print error details if any
        if r["error_details"]:
            for ed in r["error_details"]:
                print(f"           └─ status={ed['status']} error={ed['error']} time={round(ed['elapsed']*1000)}ms")

        previous_avg = r["avg_ms"]

        # Stop if we hit hard rate limits
        if r["rate_limited_429"] >= REQUESTS_PER_LEVEL // 2:
            print(f"\n[!] >50% of requests rate limited at workers={workers}. Stopping.")
            break

        if workers < levels[-1]:
            print(f"    [pause {PAUSE_BETWEEN_LEVELS}s before next level...]")
            time.sleep(PAUSE_BETWEEN_LEVELS)

    print(f"\n{'='*65}")
    if safe_limit:
        print(f"  RECOMMENDED: HOURLY_SALES_BURST_FETCH_WORKERS = {safe_limit}")
        print(f"  Set HTTP_POOL_MAXSIZE to at least {safe_limit} to match.")
    else:
        print("  WARNING: Could not determine safe limit -- check results above.")
    print(f"{'='*65}\n")


if __name__ == "__main__":
    main()
