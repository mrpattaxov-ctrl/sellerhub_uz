"""Benchmark the hourly-notification Stage 2: COLLECTION vs COMPUTING.

Runs the same per-shop work `_do_hourly_sales_check` does, but times the two
halves separately:

  * COLLECTION = the DB read  (read_hourly_sku_breakdown → snapshot + finance_orders)
  * COMPUTING  = the per-SKU arithmetic (revenue/cost/profit/margin) in pure Python

Usage (inside the app environment / container, where the DB is reachable):

    python -m scripts.bench_notif_collection
    python -m scripts.bench_notif_collection 2026-06-22 09   # day + hour (Tashkent)
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, date, time as dt_time, timedelta

from sqlalchemy import select, func
from app import (
    SessionLocal, _active_shop_ids_for_sales, _app_naive, _app_dt, _now_app_tz, APP_TZ,
)
from core.sales_reads import read_hourly_sku_breakdown
from models import FinanceHourlySnapshot


def _latest_populated_snap_hour() -> datetime | None:
    """Find the most recent snapshot hour that has sales (sum(amount) > 0).

    snapshot_hour is stored naive-UTC; convert to a naive-Tashkent datetime
    so it can flow through the same _app_naive / read path as a real tick.
    """
    from datetime import timezone as _utc
    with SessionLocal() as db:
        row = db.execute(
            select(FinanceHourlySnapshot.snapshot_hour)
            .group_by(FinanceHourlySnapshot.snapshot_hour)
            .having(func.sum(FinanceHourlySnapshot.amount) > 0)
            .order_by(FinanceHourlySnapshot.snapshot_hour.desc())
            .limit(1)
        ).first()
    if not row or row[0] is None:
        return None
    utc_naive = row[0]
    tashkent = utc_naive.replace(tzinfo=_utc.utc).astimezone(APP_TZ).replace(tzinfo=None)
    return tashkent


def _window():
    """Pick the window to benchmark.

    * `YYYY-MM-DD HH` args   → that exact Tashkent hour.
    * no args                → auto-detect the most recent hour WITH sales.
    """
    if len(sys.argv) >= 3:
        d = datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
        h = int(sys.argv[2])
        snap = _app_dt(d, h)
    else:
        auto = _latest_populated_snap_hour()
        if auto is not None:
            snap = _app_dt(auto.date(), auto.hour)
            print(f"[auto] most recent hour WITH sales = {snap}")
        else:
            snap = _now_app_tz().replace(minute=0, second=0, microsecond=0)
            print("[auto] no populated snapshot hour found — falling back to current hour")
    hour_start = snap - timedelta(hours=1)
    day_start = _app_dt(snap.date(), 0)
    return _app_naive(hour_start), _app_naive(snap), _app_naive(day_start)


def main():
    period_start, period_end, day_start = _window()
    shops = _active_shop_ids_for_sales()
    print(f"window period=[{period_start} .. {period_end})  day_start={day_start}")
    print(f"shops={len(shops)}\n")

    collect_total = 0.0
    compute_total = 0.0
    sku_total = 0
    per_shop = []

    with SessionLocal() as db:
        for sid in shops:
            try:
                sid_int = int(sid)
            except (TypeError, ValueError):
                continue

            # ── COLLECTION: the DB read (day window + period window) ──
            t0 = time.monotonic()
            day_map = read_hourly_sku_breakdown(sid_int, day_start, period_end, session=db)
            hour_map = read_hourly_sku_breakdown(sid_int, period_start, period_end, session=db)
            t1 = time.monotonic()

            # ── COMPUTING: the per-SKU arithmetic over what we just read ──
            t2 = time.monotonic()
            n_sku = 0
            rows = []  # the computed sales table for this shop (like the TG image)
            for sku_id, fo in day_map.items():
                d_qty = fo["amount"]
                if d_qty <= 0:
                    continue
                sell_price = fo["sell_price"]
                purchase_price = fo["purchase_price"]
                seller_profit = fo["seller_profit"]
                commission_u = fo["commission"]
                logistics_u = fo["logistics_fee"]
                h_entry = hour_map.get(sku_id)
                h_qty = int(h_entry.get("amount") or 0) if h_entry else 0
                revenue_unit = sell_price / d_qty
                cost_unit = purchase_price / d_qty
                comm_unit = commission_u / d_qty
                log_unit = logistics_u / d_qty
                payout_unit = seller_profit / d_qty if seller_profit else (revenue_unit - comm_unit - log_unit)
                profit_unit = payout_unit - cost_unit
                row_profit = d_qty * profit_unit
                row_revenue = d_qty * revenue_unit
                row_payout = d_qty * payout_unit
                margin = round(row_profit / row_revenue * 100, 1) if row_revenue > 0 else 0.0
                rows.append({
                    "name": (fo.get("product_title") or sku_id)[:34],
                    "hour_qty": h_qty,
                    "day_qty": d_qty,
                    "revenue": int(row_revenue),
                    "payout": int(row_payout),
                    "profit": int(row_profit),
                    "margin": margin,
                })
                n_sku += 1
            t3 = time.monotonic()

            collect = t1 - t0
            compute = t3 - t2
            collect_total += collect
            compute_total += compute
            sku_total += n_sku
            per_shop.append((sid, n_sku, collect, compute))

            # ── Print the sales table for this shop (the TG-style report) ──
            if rows:
                rows.sort(key=lambda r: r["day_qty"], reverse=True)
                print(f"\n  === shop {sid}  --  {len(rows)} SKU  "
                      f"(collect {collect * 1000:.1f} ms, compute {compute * 1000:.3f} ms) ===")
                print(f"    {'product':<34} {'за час':>7} {'с 00:00':>8} "
                      f"{'выручка':>12} {'выплата':>12} {'прибыль':>12} {'марж%':>7}")
                tot_h = tot_d = tot_rev = tot_pay = tot_prof = 0
                for r in rows:
                    print(f"    {r['name']:<34} {r['hour_qty']:>7} {r['day_qty']:>8} "
                          f"{r['revenue']:>12,} {r['payout']:>12,} {r['profit']:>12,} {r['margin']:>7}")
                    tot_h += r["hour_qty"]; tot_d += r["day_qty"]
                    tot_rev += r["revenue"]; tot_pay += r["payout"]; tot_prof += r["profit"]
                avg_m = round(tot_prof / tot_rev * 100, 1) if tot_rev > 0 else 0.0
                print(f"    {'ИТОГО':<34} {tot_h:>7} {tot_d:>8} "
                      f"{tot_rev:>12,} {tot_pay:>12,} {tot_prof:>12,} {avg_m:>7}")

    print(f"{'shop':>10} {'skus':>6} {'collect_ms':>12} {'compute_ms':>12}")
    for sid, n, c, k in per_shop:
        print(f"{sid:>10} {n:>6} {c * 1000:>12.3f} {k * 1000:>12.3f}")

    n_shops = max(1, len(per_shop))
    print("\n── TOTALS ──")
    print(f"shops={len(per_shop)}  skus={sku_total}")
    print(f"COLLECTION (DB read): total={collect_total * 1000:.1f} ms  "
          f"avg/shop={collect_total / n_shops * 1000:.3f} ms")
    print(f"COMPUTING  (Python ): total={compute_total * 1000:.1f} ms  "
          f"avg/shop={compute_total / n_shops * 1000:.3f} ms")
    print(f"STAGE 2 TOTAL: {(collect_total + compute_total) * 1000:.1f} ms")

    # Linear projection for scaling.
    per_shop_ms = (collect_total + compute_total) / n_shops * 1000
    print("\n── PROJECTION (linear in shop count) ──")
    for n in (500, 1000, 5000, 10000):
        print(f"  {n:>6} shops → {per_shop_ms * n / 1000:7.1f} s serial")


if __name__ == "__main__":
    main()
