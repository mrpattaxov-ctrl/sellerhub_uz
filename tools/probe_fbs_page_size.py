"""READ-ONLY probe: what page ``size`` does /v2/fbs/orders actually accept?

Our code clamps ``size`` to 50 before the request ever leaves the process
(core/uzum_openapi.py: "Uzum caps at 50; pass-through would 400") — an
inherited claim that no log or test ever verified. Meanwhile the invoice
endpoint turned out to accept size=400 after we'd assumed 20. So the orders
cap may be equally wrong, and we can't know without asking Uzum directly.

This probe bypasses the clamp by building the URL itself, then walks
size = 50 → 100 → 200 → 500 → 1000 and reports, for each, what Uzum did:
HTTP status, how many orders actually came back, latency, payload size, and
the rate-limit headers.

SAFETY: GET only. Nothing is created, changed, or deleted. Each call goes
through the shared per-token bucket, so it cannot burst Uzum. The token is
read from our own DB and is NEVER printed.

Run (inside the app container):
    docker compose exec app python tools/probe_fbs_page_size.py
Optional:
    --status CREATED|PACKING|DELIVERING   (default: auto — picks the status
                                           with the most orders in our DB)
    --user 1                              (default: first user with a token)
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from sqlalchemy import func, select

from extensions import SessionLocal
from models import FbsOrder, Shop, User
from core.uzum_openapi import (
    OPENAPI_BASE,
    _fbs_orders_request_with_auth,
    _extract_ratelimit_headers,  # noqa: F401  (kept for symmetry / future use)
)

SIZES = [50, 100, 200, 500, 1000]


def pick_token_and_shops(user_id: int | None) -> tuple[str, list[str], int]:
    """Return (token, shop_uzum_ids, user_id) from our own DB. Token is never printed."""
    with SessionLocal() as db:
        q = select(User).where(func.length(func.coalesce(User.uzum_openapi_token, "")) > 0)
        if user_id is not None:
            q = q.where(User.id == int(user_id))
        user = db.execute(q.order_by(User.id)).scalars().first()
        if user is None:
            sys.exit("No user with an OpenAPI token found in the DB.")
        shops = db.execute(
            select(Shop.uzum_id).where(Shop.owner_id == user.id)
        ).scalars().all()
        shops = [str(s) for s in shops if s]
        if not shops:
            sys.exit(f"User {user.id} has no shops.")
        return (user.uzum_openapi_token or "").strip(), shops, int(user.id)


def busiest_status(shops: list[str]) -> str:
    """Pick the active status with the most rows in our DB — the probe is only
    meaningful against a status that actually has orders to page through."""
    with SessionLocal() as db:
        rows = db.execute(
            select(FbsOrder.status, func.count())
            .where(FbsOrder.shop_id.in_(shops))
            .where(FbsOrder.status.in_(("CREATED", "PACKING", "DELIVERING",
                                        "PENDING_DELIVERY", "COMPLETED")))
            .group_by(FbsOrder.status)
            .order_by(func.count().desc())
        ).all()
    if not rows:
        return "CREATED"
    for status, n in rows:
        print(f"    DB has {n:>5} rows in {status}")
    return rows[0][0]


def probe_one(token: str, shops: list[str], status: str, size: int) -> dict:
    """One GET at the requested size, with the 50-clamp bypassed."""
    qs = "&".join(
        [f"shopIds={s}" for s in shops]
        + [f"status={status}", "page=0", f"size={int(size)}"]
    )
    url = f"{OPENAPI_BASE}/v2/fbs/orders?{qs}"

    t0 = time.monotonic()
    parsed, http_status, text, _auth = _fbs_orders_request_with_auth(
        url, token,
        accept_language=None,
        debug_label=f"probe.size={size}",
        fail_fast=True,
    )
    elapsed = time.monotonic() - t0

    n_orders = None
    if isinstance(parsed, dict):
        payload = parsed.get("payload") or {}
        orders = payload.get("orders")
        if isinstance(orders, list):
            n_orders = len(orders)

    return {
        "size_requested": size,
        "http": http_status,
        "orders_returned": n_orders,
        "elapsed_s": round(elapsed, 2),
        "bytes": len(text or ""),
        "error_snippet": None if 200 <= http_status < 300 else (text or "")[:200],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", default=None)
    ap.add_argument("--user", type=int, default=None)
    # 50 works and 100 is rejected (illegal-argument-001), so the real ceiling is
    # somewhere in between — pass e.g. --sizes 51,60,75,90,99 to bisect it.
    ap.add_argument("--sizes", default=None,
                    help="comma list of page sizes to probe (default: 50,100,200,500,1000)")
    args = ap.parse_args()

    global SIZES
    if args.sizes:
        SIZES = [int(s) for s in args.sizes.split(",") if s.strip()]

    token, shops, uid = pick_token_and_shops(args.user)
    print(f"user_id={uid}  shops={shops}  (token loaded, not shown)")

    status = args.status
    if not status:
        print("  picking the status with the most DB rows:")
        status = busiest_status(shops)
    print(f"\nProbing GET /v2/fbs/orders  status={status}  page=0\n")

    results = []
    for size in SIZES:
        r = probe_one(token, shops, status, size)
        results.append(r)
        if 200 <= r["http"] < 300:
            print(f"  size={size:>5} → HTTP {r['http']}  "
                  f"orders={r['orders_returned']:>5}  "
                  f"{r['elapsed_s']:>5}s  {r['bytes']:>8} bytes")
        else:
            print(f"  size={size:>5} → HTTP {r['http']}  REJECTED  "
                  f"{r['elapsed_s']:>5}s  {r['error_snippet']}")

    print("\n--- VERDICT ---")
    ok = [r for r in results if 200 <= r["http"] < 300 and r["orders_returned"] is not None]
    if not ok:
        print("Every size failed — check the token/shops, not the cap.")
    else:
        best = max(ok, key=lambda r: r["orders_returned"])
        cap_hit = {r["orders_returned"] for r in ok}
        print(f"Max orders in ONE response: {best['orders_returned']} "
              f"(requested size={best['size_requested']})")
        if len(cap_hit) == 1:
            n = next(iter(cap_hit))
            print(f"All sizes returned {n} orders → either a hard server cap at {n}, "
                  f"or the status simply has only {n} orders. "
                  f"Re-run against a status with MORE rows than the biggest size to be sure.")
        else:
            print("Row count grew with size → the 50-cap in our code is NOT Uzum's cap.")
    print("\nraw:", json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
