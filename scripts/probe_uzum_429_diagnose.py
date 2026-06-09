"""Diagnose WHY a seller's OpenAPI token keeps getting HTTP 429.

Bosqich A.9 (#5) follow-up. Unlike ``test_uzum_rate_limit.py`` (which
fires CONCURRENT bursts to find the threshold), this probe is deliberately
SLOW and PACED — it never bursts, so it won't itself trip the sub-second
burst penalty. It answers three questions:

  1. Is it the 5-shop BATCH? — compares a single-shop ``/count`` against a
     5-shop batched ``/count`` (the shape the app actually uses).
  2. Is there a rolling-WINDOW quota? — if a call 429s, it polls a light
     ``/count`` every POLL_GAP seconds and reports time-to-first-200
     (the real throttle window) plus any ``Retry-After`` header.
  3. What headers does Uzum send on 429? — dumps Retry-After + any
     RateLimit-*/X-RateLimit-* headers (the app's BURST log now does this
     too, but here we see them for a controlled call).

READ-ONLY: only GET /v2/fbs/orders/count. No writes, no confirms.

Run INSIDE the app container (same egress IP + token as production):
    docker compose exec app python scripts/probe_uzum_429_diagnose.py

WHEN to run: ideally when you are NOT actively pressing Yangilash, so the
probe's own calls (and any 429 it triggers) don't collide with real work.
It makes at most ~2 + POLL_TRIES calls, each >= GAP seconds apart.
"""
from __future__ import annotations

import os
import sys
import time

# Make the app root importable regardless of how the script is launched
# (``python scripts/foo.py`` only puts scripts/ on sys.path, not the root).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

OPENAPI_BASE = "https://api-seller.uzum.uz/api/seller-openapi"
GAP = 3.0          # seconds between the single-shop and batch probe calls
POLL_GAP = 8.0     # seconds between recovery-poll calls
POLL_TRIES = 12    # max recovery polls (12 * 8s = ~96s observation window)

_RATE_HEADER_KEYS = (
    "retry-after", "ratelimit-limit", "ratelimit-remaining", "ratelimit-reset",
    "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset",
    "x-rate-limit-limit", "x-rate-limit-remaining",
)


def _load_token_and_shops() -> tuple[str, list[str]]:
    """Read the first seller OpenAPI token + that user's shop uzum_ids."""
    env_tok = os.environ.get("UZUM_PROBE_TOKEN", "").strip()
    from app import app as flask_app
    from models import User, Shop
    from extensions import SessionLocal
    from sqlalchemy import select
    with flask_app.app_context():
        with SessionLocal() as db:
            token = env_tok
            if not token:
                token = (db.execute(
                    select(User.uzum_openapi_token).where(
                        User.uzum_openapi_token.isnot(None)
                    ).limit(1)
                ).scalar_one_or_none() or "").strip()
            shop_ids = [
                str(s) for s in db.execute(
                    select(Shop.uzum_id).where(Shop.uzum_id.isnot(None)).limit(10)
                ).scalars().all()
            ]
    return token, shop_ids


def _rate_headers(resp: requests.Response) -> dict:
    return {k: v for k, v in resp.headers.items() if k.lower() in _RATE_HEADER_KEYS}


def _count_call(token: str, shop_ids: list[str], *, label: str) -> requests.Response:
    """One paced GET /v2/fbs/orders/count for the given shop id(s)."""
    qs = "&".join(f"shopIds={s}" for s in shop_ids) + "&status=PACKING"
    url = f"{OPENAPI_BASE}/v2/fbs/orders/count?{qs}"
    headers = {
        "Accept": "application/json",
        "User-Agent": "uzum-warehouse-app/1.0 (+openapi-client)",
        "Authorization": token,  # raw-token variant (the app's primary auth)
    }
    t0 = time.monotonic()
    resp = requests.get(url, headers=headers, timeout=30)
    dt = time.monotonic() - t0
    body = (resp.text or "")[:120]
    print(f"  [{label}] shops={len(shop_ids)} -> HTTP {resp.status_code} "
          f"in {dt:.2f}s  rate_headers={_rate_headers(resp)}  body={body!r}")
    return resp


def main() -> None:
    token, shop_ids = _load_token_and_shops()
    if not token:
        print("[probe] no token found (set UZUM_PROBE_TOKEN or add one in DB)")
        return
    if not shop_ids:
        print("[probe] no shop uzum_ids found in DB")
        return
    print(f"[probe] token={token[:8]}..  shops={shop_ids}")
    print(f"[probe] paced, read-only. GAP={GAP}s POLL_GAP={POLL_GAP}s\n")

    print("[probe] Q1: single-shop vs 5-shop batch")
    r_single = _count_call(token, shop_ids[:1], label="single")
    time.sleep(GAP)
    r_batch = _count_call(token, shop_ids, label="batch ")

    hit_429 = (r_single.status_code == 429) or (r_batch.status_code == 429)
    if not hit_429:
        print("\n[probe] no 429 on either call — the token is NOT currently "
              "throttled. The 429s seen in logs are likely a rolling-window "
              "quota hit during the worker's cold backfill (~130s, many calls) "
              "or a cross-process worker+Yangilash collision. Re-run right "
              "after a worker tick to try to reproduce.")
        if r_single.status_code == 200 and r_batch.status_code != 200:
            print("[probe] NOTE: single-shop OK but batch failed -> the 5-shop "
                  "batch shape itself is the trigger.")
        return

    # A 429 happened — measure the recovery window.
    print(f"\n[probe] Q2: 429 hit — polling recovery every {POLL_GAP}s "
          f"(up to {POLL_TRIES} tries, single light /count)")
    t_start = time.monotonic()
    for i in range(1, POLL_TRIES + 1):
        time.sleep(POLL_GAP)
        r = _count_call(token, shop_ids[:1], label=f"poll{i:02d}")
        if r.status_code == 200:
            print(f"\n[probe] RECOVERED after ~{time.monotonic()-t_start:.0f}s "
                  f"(= the throttle window for this token).")
            return
    print(f"\n[probe] still throttled after ~{time.monotonic()-t_start:.0f}s "
          f"— window is longer than the observation budget; check Retry-After "
          f"above for the real value.")


if __name__ == "__main__":
    main()
