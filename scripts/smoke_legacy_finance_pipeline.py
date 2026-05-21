#!/usr/bin/env python3
"""Smoke test for the restored legacy-shape finance pipeline (2026-05-21).

Three checks against shop 5983 (LUXUZ):
  1. detect_first_sale_year — should return an early year (2022/2023ish).
  2. _refresh_finance_for_shop_day for today — should INSERT FinanceOrder
     rows + nothing else.
  3. _save_hourly_snapshots for this hour's snap mark — should INSERT
     FinanceHourlySnapshot rows that mirror today's FinanceOrder.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sqlalchemy import func, select  # noqa: E402

from app import (  # noqa: E402
    _owner_openapi_token_for_shop,
    _refresh_finance_for_shop_day,
    _save_hourly_snapshots,
    _now_app_tz,
)
from core import uzum_finance_openapi as _ufo  # noqa: E402
from extensions import SessionLocal  # noqa: E402
from models import FinanceHourlySnapshot, FinanceOrder  # noqa: E402


SHOP = "5983"


def main():
    print(f"== Smoke test: legacy-shape finance pipeline, shop {SHOP} ==")
    token = _owner_openapi_token_for_shop(SHOP)
    print(f"token present: {bool(token)}  (prefix={(token or '')[:8]}…)")
    if not token:
        print("ABORT: no token available")
        return 1

    # 1. First-sale-year probe (cheap — yearly probes).
    print("\n[1] detect_first_sale_year() ...")
    first_year = _ufo.detect_first_sale_year(token, SHOP)
    print(f"    first sale year for shop {SHOP}: {first_year}")

    # 2. Refresh today's aggregates.
    today = _now_app_tz().date()
    print(f"\n[2] _refresh_finance_for_shop_day({SHOP}, {today}) ...")
    n = _refresh_finance_for_shop_day(SHOP, today, token=token)
    print(f"    wrote {n} FinanceOrder rows for {today}")

    # Read back to verify.
    with SessionLocal() as db:
        rows = db.execute(
            select(FinanceOrder).where(
                FinanceOrder.shop_id == SHOP,
                FinanceOrder.period_from == today,
            ).limit(2)
        ).scalars().all()
    print(f"    DB read-back: {len(rows)} sample rows")
    for r in rows:
        print(f"      sku_title={r.sku_title!r}  qty={r.amount}  sell_price={r.sell_price}  "
              f"profit={r.seller_profit}  comm={r.commission}  logi={r.logistics_fee}  "
              f"image={'yes' if r.image_url else 'no'}  chars={r.characteristics!r}")

    # 3. Snapshot for the current hour.
    print(f"\n[3] _save_hourly_snapshots() for current HH:00 ...")
    snap_hour = _now_app_tz().replace(minute=0, second=0, microsecond=0)
    _save_hourly_snapshots(snap_hour)

    # Read back snapshot.
    with SessionLocal() as db:
        count = db.execute(
            select(func.count(FinanceHourlySnapshot.id))
            .where(FinanceHourlySnapshot.shop_id == SHOP)
        ).scalar_one()
        latest = db.execute(
            select(FinanceHourlySnapshot)
            .where(FinanceHourlySnapshot.shop_id == SHOP)
            .order_by(FinanceHourlySnapshot.snapshot_hour.desc())
            .limit(1)
        ).scalars().first()
    print(f"    DB read-back: {count} total snapshot rows for shop {SHOP}")
    if latest:
        print(f"    latest: snap_hour={latest.snapshot_hour}  sku={latest.sku_title!r}  "
              f"amount={latest.amount}  sell_price={latest.sell_price}  "
              f"profit={latest.seller_profit}")

    print("\n== Done ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
