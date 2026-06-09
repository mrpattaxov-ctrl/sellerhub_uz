"""Disambiguation probe — checks whether Uzum's batched shopIds actually
returns multi-shop data, or silently filters to just one.

Steps:
  1. Call /v2/fbs/orders for EACH shop individually (COMPLETED status,
     size=5) — tells us which shops have any COMPLETED orders.
  2. If 2+ shops have COMPLETED, call the BATCHED endpoint with those
     same shops + COMPLETED. If the response includes orders from BOTH,
     batched works. If only one, Uzum is filtering.

Output is a JSON summary.
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
        print(json.dumps({"error": "need >=2 shop ids"}))
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
            admin = db.execute(
                select(User).where(User.uzum_openapi_token.is_not(None)).limit(1)
            ).scalar_one_or_none()
        token = admin.uzum_openapi_token

    out = {
        "user": admin.username,
        "per_shop_completed_counts": {},
        "batched_test": None,
    }

    # Step 1: per-shop probe with COMPLETED, size=5
    shops_with_data = []
    for sid in shop_ids:
        try:
            body, _ = fetch_fbs_orders_page(
                token, [sid], status="COMPLETED", page=0, size=5,
            )
            orders = (body.get("payload") or {}).get("orders") or []
            count = len(orders)
            out["per_shop_completed_counts"][sid] = count
            if count > 0:
                shops_with_data.append(sid)
        except Exception as e:
            out["per_shop_completed_counts"][sid] = f"ERROR: {e!r}"

    if len(shops_with_data) < 2:
        out["batched_test"] = {
            "skipped": True,
            "reason": f"only {len(shops_with_data)} shop(s) have COMPLETED orders — can't test batching"
        }
        # Try CREATED as a fallback — more likely to be active
        shops_with_data_created = []
        per_shop_created = {}
        for sid in shop_ids:
            try:
                body, _ = fetch_fbs_orders_page(
                    token, [sid], status="CREATED", page=0, size=5,
                )
                orders = (body.get("payload") or {}).get("orders") or []
                per_shop_created[sid] = len(orders)
                if len(orders) > 0:
                    shops_with_data_created.append(sid)
            except Exception as e:
                per_shop_created[sid] = f"ERROR: {e!r}"
        out["per_shop_created_counts"] = per_shop_created
        if len(shops_with_data_created) >= 2:
            shops_with_data = shops_with_data_created
            test_status = "CREATED"
        else:
            # Try ALL non-empty statuses
            out["batched_test"] = {
                "skipped": True,
                "reason": "no status has data in 2+ shops — checking deeper"
            }
            for status in ("PACKING", "DELIVERING", "DELIVERED", "ACCEPTED_AT_DP",
                           "DELIVERED_TO_CUSTOMER_DELIVERY_POINT", "CANCELED"):
                tmp = []
                for sid in shop_ids:
                    try:
                        body, _ = fetch_fbs_orders_page(
                            token, [sid], status=status, page=0, size=5,
                        )
                        if (body.get("payload") or {}).get("orders"):
                            tmp.append(sid)
                    except Exception:
                        pass
                if len(tmp) >= 2:
                    shops_with_data = tmp
                    test_status = status
                    break
            else:
                out["final_verdict"] = "NO_STATUS_HAS_2_SHOPS_WITH_DATA"
                print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
                return
    else:
        test_status = "COMPLETED"

    # Step 2: batched call with the shops we KNOW have data
    body, url = fetch_fbs_orders_page(
        token, shops_with_data, status=test_status, page=0, size=50,
    )
    orders = (body.get("payload") or {}).get("orders") or []
    seen = Counter()
    for o in orders:
        seen[str(o.get("shopId"))] += 1

    out["batched_test"] = {
        "status_used": test_status,
        "shops_in_batch": shops_with_data,
        "url": url,
        "orders_returned": len(orders),
        "distinct_shopIds_in_response": dict(seen),
        "final_verdict": (
            "BATCHED_WORKS — multiple shops returned together"
            if len(seen) >= 2
            else f"BATCHED_FILTERS — asked for {len(shops_with_data)} shops, got {len(seen)} shop"
        ),
    }

    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
