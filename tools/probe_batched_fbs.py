"""Probe whether /v2/fbs/orders returns shopId per order when called
with multiple shopIds in one request.

Usage (from project root):
    python tools/probe_batched_fbs.py <token> <shop_id_1> <shop_id_2> [shop_id_3 ...]

Example:
    python tools/probe_batched_fbs.py eyJ... 19621 10920 40571

The token is the User.uzum_openapi_token value (admin's Uzum Seller
OpenAPI token). Shop IDs are the Uzum numeric IDs of shops attached to
that token.

Read-only — does not touch DB or write anywhere.
"""
from __future__ import annotations

import json
import sys
from collections import Counter

import requests


OPENAPI_BASE = "https://api-seller.uzum.uz/api/seller-openapi"

# Same 11-status enum as core.uzum_openapi.FBS_ORDER_STATUSES.
STATUSES_TO_TRY = ("CREATED", "PACKING", "PENDING_DELIVERY",
                   "DELIVERING", "DELIVERED", "ACCEPTED_AT_DP",
                   "DELIVERED_TO_CUSTOMER_DELIVERY_POINT",
                   "PENDING_CANCELLATION", "COMPLETED",
                   "CANCELED", "RETURNED")


def fetch_one(token: str, shop_ids: list[str], status: str):
    """Call /v2/fbs/orders with batched shopIds. Mirrors
    core.uzum_openapi.fetch_fbs_orders_page's URL shape."""
    qs_parts = [f"shopIds={sid}" for sid in shop_ids]
    qs_parts.append(f"status={status}")
    qs_parts.append("page=0")
    qs_parts.append("size=50")
    url = f"{OPENAPI_BASE}/v2/fbs/orders?{'&'.join(qs_parts)}"

    # Auth: raw token, no Bearer prefix (per project memory + uzum_openapi.py)
    headers = {"Authorization": token, "Accept": "application/json"}
    r = requests.get(url, headers=headers, timeout=30)
    try:
        body = r.json()
    except Exception:
        body = {"_raw": r.text[:500]}
    return r.status_code, body, url


def main():
    if len(sys.argv) < 4:
        print("Usage: python tools/probe_batched_fbs.py <token> <shop_id_1> <shop_id_2> [...]")
        sys.exit(2)

    token = sys.argv[1].strip()
    shop_ids = [s.strip() for s in sys.argv[2:] if s.strip()]
    print(f"Testing batched call with {len(shop_ids)} shops: {shop_ids}")
    print(f"Token (first 20 chars): {token[:20]}...")
    print()

    for status in STATUSES_TO_TRY:
        print(f"=== status={status} ===")
        try:
            code, body, url = fetch_one(token, shop_ids, status)
        except Exception as e:
            print(f"  REQUEST FAIL: {e!r}")
            continue

        print(f"  URL: {url}")
        print(f"  HTTP {code}")

        if code != 200:
            print(f"  body: {json.dumps(body, ensure_ascii=False)[:400]}")
            print()
            continue

        payload = body.get("payload") or {}
        orders = payload.get("orders") or []
        print(f"  payload.totalAmount={payload.get('totalAmount')}")
        print(f"  orders returned: {len(orders)}")

        if not orders:
            print("  (empty — trying next status)")
            print()
            continue

        # Sample first order — show its top-level keys
        sample = orders[0]
        print(f"  first order top-level keys: {sorted(sample.keys())}")

        # Look for shopId in different possible shapes
        print(f"  sample['shopId']  = {sample.get('shopId')!r}")
        print(f"  sample['shop_id'] = {sample.get('shop_id')!r}")
        nested_shop = sample.get("shop") if isinstance(sample.get("shop"), dict) else None
        if nested_shop:
            print(f"  sample['shop']    = {nested_shop}")

        # Distinct shopIds across all orders in this response
        seen = Counter()
        for o in orders:
            sid = (
                o.get("shopId")
                or o.get("shop_id")
                or (o.get("shop") or {}).get("id")
            )
            seen[str(sid)] += 1
        print(f"  distinct shopIds in response: {dict(seen)}")

        if len(seen) == 1 and len(shop_ids) > 1:
            only = list(seen.keys())[0]
            if only in shop_ids:
                print(f"  NOTE: only 1 distinct shopId ({only}). Could be: "
                      f"(a) only this shop has {status} orders, "
                      "or (b) Uzum ignored the extra shopIds.")
            else:
                print(f"  WEIRD: response shopId ({only}) not in our request list — investigate.")
        elif len(seen) >= 2:
            print(f"  ✓ CONFIRMED: multiple shops in one batched response — batched approach works.")

        # First order JSON dump (truncated) so we can eyeball any other
        # shop-id-like fields the projection might be missing
        print()
        print("  first order JSON (truncated to 2000 chars):")
        s = json.dumps(sample, ensure_ascii=False, indent=2)
        print("  " + s[:2000])
        if len(s) > 2000:
            print("  ... (truncated)")
        print()
        return  # one populated status is enough

    print("All 11 statuses returned 0 orders. Either the shops are quiet or auth failed silently.")


if __name__ == "__main__":
    main()
