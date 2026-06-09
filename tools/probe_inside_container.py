"""One-shot probe — runs INSIDE the app container so it has DB + deps.

Reads the first admin's uzum_openapi_token from the DB, then calls
/v2/fbs/orders with multiple shopIds. Prints a compact JSON summary
showing whether per-order shopId comes back.

Usage:
    docker exec sellerhub_uz-app-1 python /app/tools/probe_inside_container.py 19621 10920 40571 51948 81374
"""
from __future__ import annotations

import json
import sys
from collections import Counter

from sqlalchemy import select

from extensions import SessionLocal
from models import User
from core.uzum_openapi import fetch_fbs_orders_page


def main():
    if len(sys.argv) < 3:
        print(json.dumps({"error": "need >=2 shop ids as CLI args"}))
        sys.exit(2)
    shop_ids = [s.strip() for s in sys.argv[1:] if s.strip()]

    with SessionLocal() as db:
        admin = db.execute(
            select(User).where(
                User.is_admin == True,
                User.uzum_openapi_token.is_not(None),
            ).limit(1)
        ).scalar_one_or_none()
        if admin is None:
            # Fallback — any user with a token
            admin = db.execute(
                select(User).where(User.uzum_openapi_token.is_not(None)).limit(1)
            ).scalar_one_or_none()
        if admin is None:
            print(json.dumps({"error": "no user with uzum_openapi_token in DB"}))
            sys.exit(1)
        token = admin.uzum_openapi_token

    statuses_to_try = (
        "CREATED", "PACKING", "PENDING_DELIVERY", "DELIVERING",
        "DELIVERED", "ACCEPTED_AT_DP", "DELIVERED_TO_CUSTOMER_DELIVERY_POINT",
        "PENDING_CANCELLATION", "COMPLETED", "CANCELED", "RETURNED",
    )

    summary = {
        "tested_shop_ids": shop_ids,
        "user": admin.username,
        "token_prefix": (token[:20] + "...") if token else None,
        "attempts": [],
    }

    for status in statuses_to_try:
        attempt = {"status": status}
        try:
            body, url = fetch_fbs_orders_page(
                token, shop_ids, status=status, page=0, size=50,
            )
        except Exception as e:
            attempt["error"] = repr(e)
            summary["attempts"].append(attempt)
            continue

        payload = body.get("payload") or {}
        orders = payload.get("orders") or []
        attempt["url"] = url
        attempt["totalAmount"] = payload.get("totalAmount")
        attempt["orders_count"] = len(orders)

        if not orders:
            summary["attempts"].append(attempt)
            continue

        sample = orders[0]
        seen = Counter()
        for o in orders:
            sid = (
                o.get("shopId")
                or o.get("shop_id")
                or (o.get("shop") or {}).get("id")
            )
            seen[str(sid)] += 1

        attempt["first_order_keys"] = sorted(sample.keys())
        attempt["sample_shopId"] = sample.get("shopId")
        attempt["sample_shop_id"] = sample.get("shop_id")
        attempt["sample_shop_nested"] = (
            sample.get("shop") if isinstance(sample.get("shop"), dict) else None
        )
        attempt["distinct_shopIds_in_response"] = dict(seen)
        if len(seen) >= 2:
            attempt["verdict"] = "MULTIPLE_SHOPS_IN_ONE_RESPONSE"
        elif len(seen) == 1 and list(seen.keys())[0] in [str(s) for s in shop_ids]:
            attempt["verdict"] = "ONLY_ONE_SHOPID_FOUND"
        else:
            attempt["verdict"] = "WEIRD_SHOPID"
        attempt["first_order_sample"] = sample
        summary["attempts"].append(attempt)
        summary["found_populated_status"] = status
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
        return

    summary["found_populated_status"] = None
    summary["message"] = "All 11 statuses returned 0 orders for these shops."
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
