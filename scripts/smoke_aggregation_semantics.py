"""Side-by-side semantic probe: does aggregating group=false line items
produce the same per-SKU totals as group=true for one day?

CRITICAL: this is the correctness gate for the group=false backfill design.
If any field differs, we need to know BEFORE writing the aggregator into
production.

Run inside container:
    docker exec warehouse_app_uzum_qty_and_row_fix-app-1 python /app/scripts/smoke_aggregation_semantics.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import requests  # noqa: E402
from sqlalchemy import select  # noqa: E402

from config import APP_TZ  # noqa: E402
from extensions import SessionLocal  # noqa: E402
from models import User  # noqa: E402


def main():
    with SessionLocal() as db:
        u = db.execute(
            select(User).where(User.uzum_openapi_token.is_not(None))
        ).scalars().first()
        tok = u.uzum_openapi_token.strip()

    headers = {
        "Authorization": tok,
        "Accept": "application/json",
        "User-Agent": "uzum-warehouse-app/1.0 (+semantics-probe)",
    }

    # Discover shop
    r = requests.get(
        "https://api-seller.uzum.uz/api/seller-openapi/v1/shops",
        headers=headers, timeout=15,
    )
    data = r.json() if r.status_code == 200 else {}
    shops = data if isinstance(data, list) else data.get("shops") or data.get("payload") or []
    shop_id = shops[0].get("id") or shops[0].get("shopId") or shops[0].get("shop_id")

    target_day = date(2026, 5, 20)
    day_start = int(datetime.combine(target_day, datetime.min.time()).replace(tzinfo=APP_TZ).timestamp())
    day_end = int(datetime.combine(target_day, datetime.max.time()).replace(tzinfo=APP_TZ).timestamp())

    print(f"Target day: {target_day}, shop={shop_id}")

    # group=true
    url_g = (
        "https://api-seller.uzum.uz/api/seller-openapi/v1/finance/orders"
        f"?shopIds={shop_id}&dateFrom={day_start}&dateTo={day_end}"
        "&group=true&page=0&size=1000"
    )
    r1 = requests.get(url_g, headers=headers, timeout=60)
    print(f"group=true status={r1.status_code}")
    j1 = r1.json()
    time.sleep(1.5)

    # group=false
    url_u = (
        "https://api-seller.uzum.uz/api/seller-openapi/v1/finance/orders"
        f"?shopIds={shop_id}&dateFrom={day_start}&dateTo={day_end}"
        "&group=false&page=0&size=5000"
    )
    r2 = requests.get(url_u, headers=headers, timeout=60)
    j2 = r2.json()
    print(f"group=false status={r2.status_code}  totalElements={j2.get('totalElements')}")

    # Sample raw structures
    grp_items = j1.get("orderItems", [])
    print("\n=== RAW group=true SAMPLE (1st product, 1st SKU) ===")
    if grp_items:
        prod = grp_items[0]
        print(f"product top-level keys: {sorted(prod.keys())}")
        skus = prod.get("items") or []
        if skus:
            print(f"SKU keys: {sorted(skus[0].keys())}")
            print(f"SKU sample:\n{json.dumps(skus[0], indent=2, ensure_ascii=False)[:1500]}")

    ungr_items = j2.get("orderItems", [])
    print("\n=== RAW group=false SAMPLE (1st line item) ===")
    if ungr_items:
        print(f"item keys: {sorted(ungr_items[0].keys())}")
        print(f"item sample:\n{json.dumps(ungr_items[0], indent=2, ensure_ascii=False)[:1500]}")

    # Build group=true SKU map: sku_title -> aggregated row
    grouped_map = {}
    for prod in grp_items:
        prod_title = prod.get("productTitle")
        for sku in prod.get("items") or []:
            st = sku.get("skuTitle")
            grouped_map[st] = {
                "product_title": prod_title,
                "amount": sku.get("amount"),
                "amountReturns": sku.get("amountReturns"),
                "sellPrice": sku.get("sellPrice"),
                "sellerProfit": sku.get("sellerProfit"),
                "commission": sku.get("commission"),
                "purchasePrice": sku.get("purchasePrice"),
                "logisticDeliveryFee": sku.get("logisticDeliveryFee"),
                "sellerDiscountAmount": sku.get("sellerDiscountAmount"),
                "withdrawnProfit": sku.get("withdrawnProfit"),
            }

    # Aggregate group=false by skuTitle. DO NOT skip CANCELED — group=true
    # aggregates include cancelled rows in the rolled-up totals.
    ungrouped_agg = defaultdict(lambda: {
        "amount": 0, "amountReturns": 0,
        "sellPrice_per_unit_x_qty": 0, "sellPrice_sum_per_order": 0,
        "sellerProfit": 0, "commission": 0, "purchasePrice": 0,
        "logisticDeliveryFee": 0, "sellerDiscountAmount": 0,
        "withdrawnProfit": 0, "n_orders": 0, "n_cancelled": 0,
    })
    for item in ungr_items:
        st = item.get("skuTitle")
        if not st:
            continue
        g = ungrouped_agg[st]
        if (item.get("status") or "").upper() == "CANCELED":
            g["n_cancelled"] += 1
        qty = int(item.get("amount") or 0)
        sp = int(item.get("sellPrice") or 0)
        g["amount"] += qty
        g["amountReturns"] += int(item.get("amountReturns") or 0)
        g["sellPrice_per_unit_x_qty"] += sp * qty
        g["sellPrice_sum_per_order"] += sp
        g["sellerProfit"] += int(item.get("sellerProfit") or 0)
        g["commission"] += int(item.get("commission") or 0)
        # HYPOTHESIS: purchasePrice in line items is PER-UNIT cost basis.
        # group=true aggregates as sum(purchasePrice * amount). For return-only
        # rows (amount=0) cost contribution is naturally 0.
        g["purchasePrice"] += int(item.get("purchasePrice") or 0) * qty
        g["logisticDeliveryFee"] += int(item.get("logisticDeliveryFee") or 0)
        g["sellerDiscountAmount"] += int(item.get("sellerDiscountAmount") or 0)
        g["withdrawnProfit"] += int(item.get("withdrawnProfit") or 0)
        g["n_orders"] += 1

    print("\n=== SIDE-BY-SIDE COMPARISON ===")
    diffs_by_field = {}
    n_skus_compared = 0
    sellprice_hypothesis_votes = {"per_unit_x_qty": 0, "sum_per_order": 0, "neither": 0}

    for st, grouped in grouped_map.items():
        ungrp = ungrouped_agg.get(st)
        if not ungrp:
            print(f"\n  SKU {st!r}: in group=true, ZERO in group=false (CANCELED-only?)")
            continue
        n_skus_compared += 1
        print(f"\n  SKU: {st!r}  (n_orders={ungrp['n_orders']})")

        compares = [
            ("amount", "amount"),
            ("amountReturns", "amountReturns"),
            ("sellerProfit", "sellerProfit"),
            ("commission", "commission"),
            ("purchasePrice", "purchasePrice"),
            ("logisticDeliveryFee", "logisticDeliveryFee"),
            ("sellerDiscountAmount", "sellerDiscountAmount"),
            ("withdrawnProfit", "withdrawnProfit"),
        ]
        for g_field, u_field in compares:
            gv = grouped.get(g_field)
            uv = ungrp.get(u_field)
            mark = "OK" if gv == uv else "DIFF"
            if gv != uv:
                diffs_by_field[g_field] = diffs_by_field.get(g_field, 0) + 1
            print(f"    {g_field:25} grp={str(gv):>15}  ungrp_sum={str(uv):>15}  {mark}")

        # The big one: sellPrice
        g_sp = grouped.get("sellPrice")
        u_sp_a = ungrp.get("sellPrice_per_unit_x_qty")
        u_sp_b = ungrp.get("sellPrice_sum_per_order")
        if g_sp == u_sp_a:
            sellprice_hypothesis_votes["per_unit_x_qty"] += 1
            verdict = "REVENUE (sellPrice IS per-unit; sum(sp*qty) matches)"
        elif g_sp == u_sp_b:
            sellprice_hypothesis_votes["sum_per_order"] += 1
            verdict = "sellPrice in group=false IS per-order total"
        else:
            sellprice_hypothesis_votes["neither"] += 1
            verdict = "NEITHER hypothesis"
        print(f"    sellPrice:                grp={str(g_sp):>15}  per_unit*qty={str(u_sp_a):>15}  sum_per_order={str(u_sp_b):>15}")
        print(f"        verdict: {verdict}")

    print("\n=== SUMMARY ===")
    print(f"SKUs compared: {n_skus_compared}")
    if diffs_by_field:
        print("Field mismatches:")
        for f, n in diffs_by_field.items():
            print(f"  {f}: {n} SKU(s) differ")
    else:
        print("Every non-sellPrice field matches across all SKUs")
    print(f"sellPrice hypothesis votes: {sellprice_hypothesis_votes}")

    # Dump details of every SKU where purchasePrice differs
    print("\n=== purchasePrice MISMATCHES — line item dump ===")
    for st, grouped in grouped_map.items():
        ungrp = ungrouped_agg.get(st)
        if not ungrp:
            continue
        g_pp = grouped.get("purchasePrice")
        u_pp = ungrp.get("purchasePrice")
        if g_pp == u_pp:
            continue
        print(f"\n  SKU {st!r}: group=true purchasePrice={g_pp}  ungrouped_sum={u_pp}")
        print(f"    amount: grp={grouped.get('amount')}  amountReturns: grp={grouped.get('amountReturns')}")
        # Dump every line item for this SKU
        for item in ungr_items:
            if item.get("skuTitle") == st:
                print(f"    line: status={item.get('status'):>20}  "
                      f"amount={item.get('amount'):>3}  "
                      f"amountReturns={item.get('amountReturns'):>3}  "
                      f"cancelled={str(item.get('cancelled')):>5}  "
                      f"purchasePrice={item.get('purchasePrice'):>6}  "
                      f"sellPrice={item.get('sellPrice'):>6}")


if __name__ == "__main__":
    main()
