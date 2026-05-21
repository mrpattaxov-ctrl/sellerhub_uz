#!/usr/bin/env python3
"""Smoke-test the OpenAPI finance migration end-to-end.

Steps:
  1. Resolve shop 5983 (LUXUZ) → owner's uzum_openapi_token via DB.
  2. Fetch a TINY sales window: today 00:00 → today 12:00 Tashkent. Should
     yield real rows now that dateFrom/dateTo go in seconds (verified earlier).
  3. Call _ingest_sales_lines_window — verify it persists product_image
     and qty_cancelled correctly.
  4. Same for expenses: today's window via _ingest_expenses_window_for_shop.
  5. SELECT a sample row from each table to confirm new columns wrote.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, time as dt_time, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sqlalchemy import select  # noqa: E402

from app import (  # noqa: E402
    _fetch_sales_for_shop_window,
    _ingest_sales_lines_window,
    _ingest_expenses_window_for_shop,
    _owner_openapi_token_for_shop,
)
from config import APP_TZ  # noqa: E402
from extensions import SessionLocal  # noqa: E402
from models import ExpensesLedger, SalesLine  # noqa: E402


def main():
    SHOP_UZUM_ID = "5983"

    print(f"== Smoke test: OpenAPI finance migration, shop {SHOP_UZUM_ID} ==")

    token = _owner_openapi_token_for_shop(SHOP_UZUM_ID)
    print(f"owner OpenAPI token present: {bool(token)} (prefix={(token or '')[:8]}…)")

    # Today, 00:00 → 12:00 Tashkent (small window, plenty of rows for LUXUZ).
    today = datetime.now(APP_TZ).date()
    window_from = datetime.combine(today, dt_time(0, 0, 0))
    window_to = datetime.combine(today, dt_time(12, 0, 0))

    # ── Sales path ────────────────────────────────────────────
    print(f"\n[1] _fetch_sales_for_shop_window({SHOP_UZUM_ID}, {window_from}, {window_to})")
    rows = _fetch_sales_for_shop_window(SHOP_UZUM_ID, window_from, window_to)
    print(f"    returned {len(rows)} rows")
    if rows:
        sample = rows[0]
        print(f"    sample keys: {sorted(sample.keys())}")
        print(f"    sample.shop_id        = {sample.get('shop_id')}")
        print(f"    sample.sku_id         = {sample.get('sku_id')!r}")
        print(f"    sample.created_at     = {sample.get('created_at')}")
        print(f"    sample.qty_cancelled  = {sample.get('qty_cancelled')}")
        print(f"    sample.product_image  = "
              f"{'<dict with {} keys>'.format(len(sample['product_image'])) if isinstance(sample.get('product_image'), dict) else sample.get('product_image')}")

    print(f"\n[2] _ingest_sales_lines_window(rows, {window_from}, {window_to})")
    n = _ingest_sales_lines_window(rows, window_from, window_to)
    print(f"    inserted {n} rows")

    # Read one back to verify new columns wrote.
    with SessionLocal() as db:
        row = db.execute(
            select(SalesLine)
            .where(SalesLine.shop_id == int(SHOP_UZUM_ID))
            .where(SalesLine.created_at >= window_from)
            .where(SalesLine.created_at < window_to)
            .order_by(SalesLine.created_at.desc())
            .limit(1)
        ).scalars().first()
    if row:
        print(f"\n    DB sample row:")
        print(f"      shop_id        = {row.shop_id}")
        print(f"      order_id       = {row.order_id}")
        print(f"      sku_id         = {row.sku_id!r}")
        print(f"      sku_title      = {row.sku_title!r}")
        print(f"      qty            = {row.qty}")
        print(f"      qty_cancelled  = {row.qty_cancelled}  (new column)")
        pi = row.product_image
        print(f"      product_image  = "
              f"{('dict[' + ','.join(sorted(list(pi.keys())[:4])) + '...]') if isinstance(pi, dict) else pi}"
              f"  (new column)")
        print(f"      unit_price     = {row.unit_price}")
        print(f"      revenue        = {row.revenue}")
    else:
        print("    !! no SalesLine row found in window — expected ≥ 1")

    # ── Expenses path ─────────────────────────────────────────
    print(f"\n[3] _ingest_expenses_window_for_shop({SHOP_UZUM_ID}, today)")
    exp_to = datetime.combine(today + timedelta(days=1), dt_time(0, 0, 0))
    exp_from = datetime.combine(today, dt_time(0, 0, 0))
    try:
        n_exp = _ingest_expenses_window_for_shop(SHOP_UZUM_ID, exp_from, exp_to)
        print(f"    upserted {n_exp} rows")
    except Exception as e:
        print(f"    ERROR: {e!r}")
        n_exp = 0

    if n_exp > 0:
        with SessionLocal() as db:
            erow = db.execute(
                select(ExpensesLedger)
                .where(ExpensesLedger.shop_id == int(SHOP_UZUM_ID))
                .order_by(ExpensesLedger.charged_at.desc())
                .limit(1)
            ).scalars().first()
        if erow:
            print(f"\n    DB sample expense row:")
            print(f"      shop_id        = {erow.shop_id}")
            print(f"      operation_id   = {erow.operation_id}")
            print(f"      charged_at     = {erow.charged_at}")
            print(f"      source/service = {erow.source!r} / {erow.service!r}")
            print(f"      op_type        = {erow.op_type!r}")
            print(f"      amount         = {erow.amount}")
            print(f"      date_created   = {erow.date_created}  (new)")
            print(f"      date_updated   = {erow.date_updated}  (new)")
            print(f"      seller_id      = {erow.seller_id}  (new)")
            print(f"      external_id    = {erow.external_id!r}  (new)")
            print(f"      code           = {erow.code!r}  (new)")

    print("\n== Done ==")


if __name__ == "__main__":
    main()
