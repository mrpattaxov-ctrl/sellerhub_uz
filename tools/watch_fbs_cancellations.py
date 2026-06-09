"""Watch fbs_orders for cancellation-related status transitions.

Live-tails the local DB and prints a line each time an order's status,
cancelled_date, or cancel_reason changes. Used to follow a real
cancellation as it propagates from Uzum → background sync → DB.

Usage (from project root):
    python tools/watch_fbs_cancellations.py
    python tools/watch_fbs_cancellations.py --interval 15 --days 7

Flags:
    --interval N   Poll DB every N seconds (default 20).
    --days N       Only watch orders created in the last N days (default 14).
    --status LIST  Comma-separated statuses to watch. Default covers the
                   ones a cancellation passes through: CREATED, PACKING,
                   PENDING_DELIVERY, PENDING_CANCELLATION, CANCELED.

What it shows:
    * Initial snapshot — how many orders are currently in each watched
      status, plus the most recent 5 lines for context.
    * On each tick, only DIFF lines:
        NEW    №<id>  <status>  <shop>  <price> UZS  <customer>
        CHANGE №<id>  <old> → <new>  (cancel_reason=..., cancelled_date=...)
        GONE   №<id>  was <status>  — fell out of the window or got
                                      DELETEd by the cleanup loop.

Read-only — never writes to DB.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# Add project root to sys.path so ``from extensions import ...`` works
# regardless of where this script is invoked from.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Load .env so DATABASE_URL is set before extensions imports config.
try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except ImportError:
    pass

from sqlalchemy import select, func
from extensions import SessionLocal
from models import FbsOrder


_DEFAULT_STATUSES = (
    "CREATED",
    "PACKING",
    "PENDING_DELIVERY",
    "PENDING_CANCELLATION",
    "CANCELED",
)


def _fmt_ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _fmt_money(n: int | None) -> str:
    if n is None:
        return "—"
    return f"{int(n):,}".replace(",", " ") + " UZS"


def _fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return dt.strftime("%d.%m %H:%M")


def query_orders(days: int, statuses: tuple[str, ...]) -> dict[str, dict]:
    """Return {order_id: {status, shop_id, customer, price, cancelled_date,
    cancel_reason, date_created}} for the watched window.
    """
    cutoff = datetime.utcnow() - timedelta(days=days)
    out: dict[str, dict] = {}
    with SessionLocal() as db:
        rows = db.execute(
            select(
                FbsOrder.order_id,
                FbsOrder.status,
                FbsOrder.shop_id,
                FbsOrder.customer_fullname,
                FbsOrder.price,
                FbsOrder.cancelled_date,
                FbsOrder.cancel_reason,
                FbsOrder.date_created,
                FbsOrder.order_type,
            )
            .where(FbsOrder.date_created >= cutoff)
            .where(FbsOrder.status.in_(statuses))
            .order_by(FbsOrder.date_created.desc())
        ).all()
        for r in rows:
            out[str(r.order_id)] = {
                "status": r.status,
                "shop_id": r.shop_id,
                "customer": r.customer_fullname or "—",
                "price": r.price,
                "cancelled_date": r.cancelled_date,
                "cancel_reason": r.cancel_reason,
                "date_created": r.date_created,
                "order_type": r.order_type,
            }
    return out


def initial_summary(snap: dict[str, dict], statuses: tuple[str, ...]) -> None:
    counts: dict[str, int] = {s: 0 for s in statuses}
    for v in snap.values():
        counts[v["status"]] = counts.get(v["status"], 0) + 1
    print(f"[{_fmt_ts()}] Snapshot — {len(snap)} active orders in watched statuses:")
    for s in statuses:
        print(f"  · {s:<22} = {counts.get(s, 0)}")

    # Last 5 most-recent orders for context
    recent = sorted(snap.items(),
                    key=lambda kv: (kv[1]["date_created"] or datetime.min),
                    reverse=True)[:5]
    if recent:
        print(f"[{_fmt_ts()}] So'nggi {len(recent)} ta:")
        for oid, v in recent:
            print(f"  №{oid}  {v['order_type']}  {v['status']:<22}  "
                  f"shop={v['shop_id']}  {_fmt_money(v['price'])}  {v['customer']}  "
                  f"({_fmt_dt(v['date_created'])})")
    print(f"[{_fmt_ts()}] Watching for changes — Ctrl+C to stop.")
    print("-" * 78, flush=True)


def diff_and_print(prev: dict[str, dict], curr: dict[str, dict]) -> None:
    """Print only what changed since the previous poll."""
    prev_ids = set(prev)
    curr_ids = set(curr)

    # NEW — appeared in window
    for oid in sorted(curr_ids - prev_ids):
        v = curr[oid]
        print(f"[{_fmt_ts()}] NEW    №{oid}  {v['order_type']}  {v['status']}  "
              f"shop={v['shop_id']}  {_fmt_money(v['price'])}  {v['customer']}",
              flush=True)

    # GONE — dropped out of window (cleanup or > N days now)
    for oid in sorted(prev_ids - curr_ids):
        v = prev[oid]
        print(f"[{_fmt_ts()}] GONE   №{oid}  was {v['status']}  "
              f"(window/cleanup)",
              flush=True)

    # CHANGE — same id, different status/cancel fields
    for oid in sorted(prev_ids & curr_ids):
        p = prev[oid]
        c = curr[oid]
        diffs: list[str] = []
        if p["status"] != c["status"]:
            diffs.append(f"status: {p['status']} → {c['status']}")
        if p["cancel_reason"] != c["cancel_reason"]:
            diffs.append(f"cancel_reason: {p['cancel_reason']!r} → {c['cancel_reason']!r}")
        if p["cancelled_date"] != c["cancelled_date"]:
            diffs.append(f"cancelled_date: {_fmt_dt(p['cancelled_date'])} → {_fmt_dt(c['cancelled_date'])}")
        if diffs:
            print(f"[{_fmt_ts()}] CHANGE №{oid}  shop={c['shop_id']}  "
                  + "  ·  ".join(diffs),
                  flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--interval", type=int, default=20,
                    help="Poll interval in seconds (default 20).")
    ap.add_argument("--days", type=int, default=14,
                    help="Only watch orders created in the last N days (default 14).")
    ap.add_argument("--status", type=str, default=",".join(_DEFAULT_STATUSES),
                    help="Comma-separated statuses to watch.")
    args = ap.parse_args()

    statuses = tuple(s.strip() for s in args.status.split(",") if s.strip())
    print(f"[{_fmt_ts()}] watch_fbs_cancellations  interval={args.interval}s  "
          f"days={args.days}  statuses={','.join(statuses)}", flush=True)

    snapshot = query_orders(args.days, statuses)
    initial_summary(snapshot, statuses)

    try:
        while True:
            time.sleep(args.interval)
            try:
                curr = query_orders(args.days, statuses)
            except Exception as e:
                print(f"[{_fmt_ts()}] !! DB error: {e}", flush=True)
                continue
            diff_and_print(snapshot, curr)
            snapshot = curr
    except KeyboardInterrupt:
        print(f"\n[{_fmt_ts()}] Stopped by Ctrl+C.")


if __name__ == "__main__":
    main()
