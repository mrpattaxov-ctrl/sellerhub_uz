"""End-to-end smoke test for the Path C hourly bulk-fetch flow.

Manually invokes the inner functions of `_hourly_sales_reports_loop` for the
LAST COMPLETED hour (Tashkent), then triggers the existing per-shop hourly
Telegram dispatch — exactly as the scheduler would have done it at HH:00.

Run on the VPS (where DATABASE_URL + admin token + bot token live):

    python scripts/trigger_last_hour_test.py

Reports timing of bulk fetch, ingest counts, sample (shop_id, count) routing
breakdown, and Telegram dispatch outcome. Idempotent — DELETE+INSERT in
`_ingest_sales_lines_window` means re-running on a window already populated
by the scheduler is safe. Per-shop locks are still acquired (best-effort).
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta

# Project root importable.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sqlalchemy import select, func

from extensions import SessionLocal
from models import Shop, SalesLine
from core.time_helpers import _now_app_tz


def main() -> int:
    # Lazy-import app.py: it pulls the Flask app + Bot, which is heavy.
    # Prints from app.py module-init are expected.
    import app as _app

    # ── 1. Bound the LAST COMPLETED hour, Tashkent. ──
    # Mirror _hourly_sales_reports_loop exactly: hour_end is the hour boundary
    # we just crossed; hour_start is one hour before; both are NAIVE Tashkent
    # (consistent with sales_lines.created_at and _ingest_sales_lines_window).
    now_tz = _now_app_tz()
    hour_end_tz = now_tz.replace(minute=0, second=0, microsecond=0)
    hour_end = hour_end_tz.replace(tzinfo=None)
    hour_start = hour_end - timedelta(hours=1)

    # ── 2. Active shops. ──
    shops = _app._active_shop_ids_for_sales()
    shop_ints: list[int] = []
    for s in shops:
        try:
            shop_ints.append(int(str(s).strip()))
        except (TypeError, ValueError):
            continue
    if not shop_ints:
        print("[Smoke] No active shops; abort.")
        return 1

    print(
        f"[Smoke] window=[{hour_start.isoformat()},{hour_end.isoformat()}) "
        f"shops={shop_ints} shard_by_k={_app.SHARD_BY_K}"
    )

    # ── 3. Snapshot row count BEFORE bulk fetch. ──
    test_started_at = datetime.utcnow()
    with SessionLocal() as db:
        before_total = db.execute(
            select(func.count()).select_from(SalesLine).where(
                SalesLine.created_at >= hour_start,
                SalesLine.created_at < hour_end,
            )
        ).scalar_one()
    print(f"[Smoke] sales_lines rows in window BEFORE bulk: {before_total}")

    # ── 4. Bulk fetch + ingest (single chunk — 4 shops <= SHARD_BY_K). ──
    chunk = shop_ints[: _app.SHARD_BY_K]
    if len(shop_ints) > _app.SHARD_BY_K:
        print(
            f"[Smoke] WARNING: {len(shop_ints)} shops > SHARD_BY_K={_app.SHARD_BY_K}; "
            "loop would split into multiple chunks. Running first chunk only."
        )

    t0 = time.monotonic()
    try:
        _app._run_hourly_bulk_chunk(chunk, hour_start, hour_end)
    except Exception as e:
        print(f"[Smoke] _run_hourly_bulk_chunk RAISED: {e!r}")
        return 2
    elapsed = time.monotonic() - t0
    print(f"[Smoke] _run_hourly_bulk_chunk total elapsed={elapsed:.2f}s")

    # ── 5. Verify ingest landed and routing wrote to uzum_id space. ──
    with SessionLocal() as db:
        after_total = db.execute(
            select(func.count()).select_from(SalesLine).where(
                SalesLine.created_at >= hour_start,
                SalesLine.created_at < hour_end,
            )
        ).scalar_one()
        per_shop = db.execute(
            select(SalesLine.shop_id, func.count())
            .where(
                SalesLine.created_at >= hour_start,
                SalesLine.created_at < hour_end,
            )
            .group_by(SalesLine.shop_id)
            .order_by(SalesLine.shop_id)
        ).all()
        # PK→uzum_id sanity: shop.uzum_id should match what we see in
        # sales_lines.shop_id (Path C contract).
        shop_meta = db.execute(
            select(Shop.id, Shop.uzum_id, Shop.name).where(Shop.uzum_id.is_not(None))
        ).all()

    print(f"[Smoke] sales_lines rows in window AFTER bulk:  {after_total} (delta={after_total - before_total})")
    print(f"[Smoke] per-shop breakdown in window: {per_shop}")
    print("[Smoke] shop catalog (pk → uzum_id):")
    for pk, uzid, name in shop_meta:
        print(f"          pk={pk} uzum_id={uzid} name={name!r}")
    pks = {row[0] for row in shop_meta}
    sales_shop_ids = {sid for sid, _ in per_shop}
    if sales_shop_ids and sales_shop_ids.issubset(pks):
        print(
            f"[Smoke] FAIL: sales_lines.shop_id values {sorted(sales_shop_ids)} look like "
            f"local PKs ({sorted(pks)}), NOT uzum_id space. Path C routing regressed."
        )

    # ── 6. Trigger the per-shop hourly Telegram dispatch for THIS hour. ──
    # _run_scheduled_hourly_sales_check expects a Tashkent-aware snap_hour:
    # it calls .replace(minute=0, ...) and uses snap_hour.hour for the
    # per-user notification gate. Pass `hour_end_tz` (the closed-hour boundary
    # we just bulk-fetched).
    print(f"[Smoke] Triggering hourly Telegram dispatch for snap_hour={hour_end_tz.isoformat()}")
    t1 = time.monotonic()
    try:
        total = _app._run_scheduled_hourly_sales_check(hour_end_tz)
    except Exception as e:
        print(f"[Smoke] _run_scheduled_hourly_sales_check RAISED: {e!r}")
        return 3
    elapsed_tg = time.monotonic() - t1
    print(f"[Smoke] Telegram dispatch elapsed={elapsed_tg:.2f}s total_qty_reported={total}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
