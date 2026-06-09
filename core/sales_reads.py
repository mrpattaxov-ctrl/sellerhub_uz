"""Read-path helpers for `finance_orders`, `finance_hourly_snapshots`,
and `expenses_ledger`.

Sources by helper
-----------------
- ``read_sales_aggregated``    → ``finance_orders`` (per-shop, per-day,
  per-sku aggregates from /v1/finance/orders?group=true). Filtered by
  ``period_from`` (Date).
- ``read_expenses_range``      → ``expenses_ledger``. Filtered by
  ``charged_at`` (naive Tashkent datetime).
- ``read_hourly_sku_breakdown``→ ``finance_hourly_snapshots`` delta math
  between two ``snapshot_hour`` rows (naive UTC).
- ``read_daily_expense_breakdown`` → same as ``read_expenses_range``.

Timezone rules
--------------
Callers pass **naive Tashkent** `datetime` objects for `start_ts` / `end_ts`.
``expenses_ledger.charged_at`` is naive Tashkent and stored verbatim.
``finance_hourly_snapshots.snapshot_hour`` is naive UTC and the hourly
read helper converts the caller's Tashkent boundaries internally.
``finance_orders.period_from`` is a Date — callers should pass timestamps
that align to midnight Tashkent.

A convenience ``day_bounds_tashkent(d)`` builder is provided so callers that
have a `date` can trivially convert to the two `datetime` boundaries
required here.
"""
from __future__ import annotations

from datetime import date, datetime, time as dt_time, timedelta, timezone
from typing import Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from config import APP_TZ
from extensions import SessionLocal
from models import ExpensesLedger, FinanceHourlySnapshot, FinanceOrder


def _tashkent_naive_to_naive_utc(naive_tashkent: datetime) -> datetime:
    """Convert a naive-Tashkent datetime to naive UTC.

    `FinanceHourlySnapshot.snapshot_hour` is stored as naive UTC
    (legacy convention). Callers of the hourly read helpers pass naive
    Tashkent boundaries — this is the conversion they need.
    """
    return (
        naive_tashkent.replace(tzinfo=APP_TZ)
        .astimezone(timezone.utc)
        .replace(tzinfo=None)
    )

# ── Aggregation granularity → date_trunc key ─────────────────────────
# `sku` is the only non-date grouping supported here; combined modes (e.g.
# `day+sku`) are handled by passing the SKU dimension alongside a date key.
_TRUNC_KEYS: dict[str, str] = {
    "hour": "hour",
    "day": "day",
    "month": "month",
}

_SUPPORTED_GROUP_BYS = frozenset({"hour", "day", "month", "sku"})

# FinanceOrder is per-(shop, day, sku) — "hour" granularity is impossible
# from it. read_sales_aggregated downgrades "hour" to "day" with a warning
# rather than raising (no callers currently use "hour").
_FINANCE_ORDER_GROUP_BYS = frozenset({"day", "month", "sku"})


# ── Public helpers ───────────────────────────────────────────────────

def now_tashkent_naive() -> datetime:
    """Return current Tashkent-local wall time as a naive datetime.

    Use this (or `day_bounds_tashkent`) to build window boundaries that
    line up with `sales_lines.created_at` / `expenses_ledger.charged_at`.
    Banned elsewhere in Phase 2 reads: `datetime.utcnow()` and bare
    `datetime.now()` without APP_TZ.
    """
    return datetime.now(APP_TZ).replace(tzinfo=None)


def day_bounds_tashkent(d: date) -> tuple[datetime, datetime]:
    """Return `[00:00, 24:00)` Tashkent-naive bounds for a calendar date.

    The end bound is exclusive (`< end`), matching the DELETE+INSERT window
    convention used by the ingest pipeline.
    """
    start = datetime.combine(d, dt_time(0, 0, 0))
    end = datetime.combine(d + timedelta(days=1), dt_time(0, 0, 0))
    return start, end


def _coerce_shop_ids(shop_id: int | str | Iterable[int | str]) -> list[int]:
    """Normalise a scalar or iterable of shop ids to a unique `list[int]`.

    `sales_lines.shop_id` is an integer column (unlike the prior shop_id
    convention which was a string) — callers often still pass strings
    because the rest of the codebase stores `Shop.uzum_id` as a string.
    Coerce to int here so the index is used correctly.
    """
    if isinstance(shop_id, (int, str)):
        try:
            return [int(shop_id)]
        except (TypeError, ValueError):
            return []
    seen: set[int] = set()
    out: list[int] = []
    for sid in shop_id:
        try:
            ival = int(sid)
        except (TypeError, ValueError):
            continue
        if ival in seen:
            continue
        seen.add(ival)
        out.append(ival)
    return out


def _resolve_session(session: Session | None) -> tuple[Session, bool]:
    """Return `(session, owns)` — `owns=True` means caller must close it."""
    if session is not None:
        return session, False
    return SessionLocal(), True


# ── Aggregated reads ─────────────────────────────────────────────────

def read_sales_aggregated(
    shop_id: int | str | Iterable[int | str],
    start_ts: datetime,
    end_ts: datetime,
    group_by: str = "day",
    *,
    session: Session | None = None,
) -> list[dict]:
    """Server-side aggregation over `sales_lines` for `[start_ts, end_ts)`.

    `group_by` ∈ {"hour", "day", "month", "sku"}. Always returns a list of
    dicts keyed by:

        * `shop_id`         — int (always present — callers filtering by a
                              single shop just ignore it)
        * `bucket`          — datetime for hour/day/month; None for sku
        * `sku_id`          — present when `group_by == "sku"`, else None
        * `sku_title`       — present when `group_by == "sku"` (MAX()), else None
        * `qty_sum`         — int
        * `qty_returns_sum` — int
        * `revenue_sum`         — numeric
        * `seller_profit_sum`   — numeric
        * `commission_sum`      — numeric
        * `logistics_sum`       — numeric
        * `purchase_price_sum`  — numeric
        * `promo_amount_sum`    — numeric
        * `row_count`            — int

    Performance: single GROUP BY query, one trip to the DB. No per-row
    Python work; callers should iterate the returned list.
    """
    if group_by not in _SUPPORTED_GROUP_BYS:
        raise ValueError(
            f"read_sales_aggregated: unsupported group_by={group_by!r}; "
            f"expected one of {sorted(_SUPPORTED_GROUP_BYS)}"
        )

    # "hour" granularity is impossible from FinanceOrder (per-day aggregates).
    # Downgrade silently to "day" — no caller currently uses "hour" here.
    effective_group_by = "day" if group_by == "hour" else group_by

    shop_ids_int = _coerce_shop_ids(shop_id)
    if not shop_ids_int:
        return []
    # FinanceOrder.shop_id is varchar — stringify ints for the IN clause.
    shop_ids_str = [str(sid) for sid in shop_ids_int]

    # Convert timestamp window to date window. FinanceOrder.period_from is
    # a Date column. Right-open semantics preserved: `period_from <
    # end_ts.date()` excludes the end day if the timestamp was midnight.
    d_from = start_ts.date()
    d_to_excl = end_ts.date()

    # Build the grouping columns list.
    bucket_col = None
    sku_col = None
    sku_title_col = None
    group_cols: list = [FinanceOrder.shop_id]

    if effective_group_by == "sku":
        # Group by sku_title (always populated, matches Variant.sku) rather
        # than sku_id (Integer, nullable). The group=false backfill path
        # cannot populate sku_id because Uzum's line item response omits
        # skuId — only skuTitle is present. Grouping by sku_id would
        # collapse every NULL-sku_id row into a single synthetic group,
        # showing the user one giant "SKU" with the sum of all sales.
        # sku_id is still returned via MAX() for callers that want it.
        sku_col = FinanceOrder.sku_title
        sku_id_col = func.max(FinanceOrder.sku_id).label("sku_id")
        group_cols.append(FinanceOrder.sku_title)
    else:
        trunc_key = _TRUNC_KEYS[effective_group_by]
        bucket_col = func.date_trunc(trunc_key, FinanceOrder.period_from).label("bucket")
        group_cols.append(bucket_col)

    cols = [FinanceOrder.shop_id.label("shop_id")]
    if bucket_col is not None:
        cols.append(bucket_col)
    if sku_col is not None:
        cols.append(sku_col.label("sku_title"))
        cols.append(sku_id_col)
    cols.extend(
        [
            func.coalesce(func.sum(FinanceOrder.amount), 0).label("qty_sum"),
            func.coalesce(func.sum(FinanceOrder.amount_returns), 0).label("qty_returns_sum"),
            func.coalesce(func.sum(FinanceOrder.sell_price), 0).label("revenue_sum"),
            func.coalesce(func.sum(FinanceOrder.seller_profit), 0).label("seller_profit_sum"),
            func.coalesce(func.sum(FinanceOrder.commission), 0).label("commission_sum"),
            func.coalesce(func.sum(FinanceOrder.logistics_fee), 0).label("logistics_sum"),
            func.coalesce(func.sum(FinanceOrder.purchase_price), 0).label("purchase_price_sum"),
            func.coalesce(func.sum(FinanceOrder.seller_discount), 0).label("promo_amount_sum"),
            func.count().label("row_count"),
        ]
    )

    sess, owns = _resolve_session(session)
    try:
        stmt = (
            select(*cols)
            .where(
                FinanceOrder.shop_id.in_(shop_ids_str),
                FinanceOrder.period_from >= d_from,
                FinanceOrder.period_from < d_to_excl,
            )
            .group_by(*group_cols)
        )
        rows = sess.execute(stmt).all()
    finally:
        if owns:
            sess.close()

    out: list[dict] = []
    for r in rows:
        # shop_id round-trips as int in the return for backward compat —
        # callers cast back to str when needed.
        try:
            shop_id_out = int(r.shop_id)
        except (TypeError, ValueError):
            shop_id_out = 0
        entry: dict = {
            "shop_id": shop_id_out,
            "bucket": getattr(r, "bucket", None),
            "sku_id": getattr(r, "sku_id", None),
            "sku_title": getattr(r, "sku_title", None),
            "qty_sum": int(r.qty_sum or 0),
            "qty_returns_sum": int(r.qty_returns_sum or 0),
            "revenue_sum": r.revenue_sum or 0,
            "seller_profit_sum": r.seller_profit_sum or 0,
            "commission_sum": r.commission_sum or 0,
            "logistics_sum": r.logistics_sum or 0,
            "purchase_price_sum": r.purchase_price_sum or 0,
            "promo_amount_sum": r.promo_amount_sum or 0,
            "row_count": int(r.row_count or 0),
        }
        out.append(entry)
    return out


# ── Expenses ─────────────────────────────────────────────────────────

def read_expenses_range(
    shop_id: int | str | Iterable[int | str],
    start_date: date | datetime,
    end_date: date | datetime,
    *,
    session: Session | None = None,
) -> list[ExpensesLedger]:
    """Return `ExpensesLedger` rows for `[start, end)` by `charged_at`.

    Accepts either `date` or naive-Tashkent `datetime` bounds. `date` inputs
    are expanded to midnight — callers using `date` always get whole days.
    """
    shop_ids = _coerce_shop_ids(shop_id)
    if not shop_ids:
        return []

    if isinstance(start_date, date) and not isinstance(start_date, datetime):
        start_dt = datetime.combine(start_date, dt_time(0, 0, 0))
    else:
        start_dt = start_date
    if isinstance(end_date, date) and not isinstance(end_date, datetime):
        end_dt = datetime.combine(end_date, dt_time(0, 0, 0))
    else:
        end_dt = end_date

    sess, owns = _resolve_session(session)
    try:
        stmt = (
            select(ExpensesLedger)
            .where(
                ExpensesLedger.shop_id.in_(shop_ids),
                ExpensesLedger.charged_at >= start_dt,
                ExpensesLedger.charged_at < end_dt,
            )
            .order_by(ExpensesLedger.charged_at.asc())
        )
        return list(sess.execute(stmt).scalars().all())
    finally:
        if owns:
            sess.close()


# ── Notification-specific helpers (Phase 3) ──────────────────────────

def read_hourly_sku_breakdown(
    shop_id: int | str,
    start_ts: datetime,
    end_ts: datetime,
    *,
    session: Session | None = None,
) -> dict[str, dict]:
    """Per-SKU aggregate for `[start_ts, end_ts)` keyed by the seller SKU code.

    Feeds the Telegram hourly notification path. Output shape:

        { sku_title: { "product_title": <name>,
                       "amount": qty_sum,
                       "sell_price": revenue_sum,
                       "purchase_price": cost_sum,
                       "seller_profit": seller_profit_sum,
                       "commission": commission_sum,
                       "logistics_fee": logistics_sum } }

    `start_ts` / `end_ts` are **naive Tashkent** HH:00 boundaries.

    Data source: `FinanceHourlySnapshot` delta math (no API call, no
    `sales_lines` read). Each snapshot row holds cumulative totals for
    the calendar day in Tashkent that contains the snap_hour, so:

      * Same-day window `[A, B)`: delta = snapshot(B) - snapshot(A).
      * Window starting at Tashkent midnight: snapshot(A) is treated as
        zero (the 00:00 tick belongs to the previous day's EOD).
      * Cross-day window: split into per-day chunks at midnight; per-day
        deltas are summed across SKUs.
    """
    shop_ids = _coerce_shop_ids(shop_id)
    if not shop_ids or end_ts <= start_ts:
        return {}
    sid_str = str(shop_ids[0])

    sess, owns = _resolve_session(session)
    try:
        agg: dict[str, dict] = {}

        chunk_start = start_ts
        while chunk_start < end_ts:
            chunk_day = chunk_start.date()
            next_midnight = datetime.combine(chunk_day + timedelta(days=1), dt_time(0, 0, 0))
            chunk_end = min(next_midnight, end_ts)

            end_utc = _tashkent_naive_to_naive_utc(chunk_end)
            end_rows = sess.execute(
                select(FinanceHourlySnapshot).where(
                    FinanceHourlySnapshot.shop_id == sid_str,
                    FinanceHourlySnapshot.snapshot_hour == end_utc,
                )
            ).scalars().all()

            start_is_midnight = (
                chunk_start.hour == 0
                and chunk_start.minute == 0
                and chunk_start.second == 0
            )
            if start_is_midnight or not end_rows:
                start_by_sku: dict[str, FinanceHourlySnapshot] = {}
            else:
                start_utc = _tashkent_naive_to_naive_utc(chunk_start)
                start_rows = sess.execute(
                    select(FinanceHourlySnapshot).where(
                        FinanceHourlySnapshot.shop_id == sid_str,
                        FinanceHourlySnapshot.snapshot_hour == start_utc,
                    )
                ).scalars().all()
                start_by_sku = {r.sku_title: r for r in start_rows}

            for end_row in end_rows:
                key = (end_row.sku_title or "").strip()
                if not key:
                    continue
                start_row = start_by_sku.get(end_row.sku_title)
                d_amount = int(end_row.amount or 0) - (int(start_row.amount or 0) if start_row else 0)
                if d_amount <= 0:
                    continue
                d_sell  = int(end_row.sell_price or 0)     - (int(start_row.sell_price or 0)     if start_row else 0)
                d_cost  = int(end_row.purchase_price or 0) - (int(start_row.purchase_price or 0) if start_row else 0)
                d_prof  = int(end_row.seller_profit or 0)  - (int(start_row.seller_profit or 0)  if start_row else 0)
                d_comm  = int(end_row.commission or 0)     - (int(start_row.commission or 0)     if start_row else 0)
                d_log   = int(end_row.logistics_fee or 0)  - (int(start_row.logistics_fee or 0)  if start_row else 0)

                acc = agg.setdefault(key, {
                    "product_title": "",
                    "amount": 0,
                    "sell_price": 0,
                    "purchase_price": 0,
                    "seller_profit": 0,
                    "commission": 0,
                    "logistics_fee": 0,
                })
                acc["amount"]         += d_amount
                acc["sell_price"]     += d_sell
                acc["purchase_price"] += d_cost
                acc["seller_profit"]  += d_prof
                acc["commission"]     += d_comm
                acc["logistics_fee"]  += d_log

            chunk_start = chunk_end

        # Enrich product_title from finance_orders for the touched days.
        # The snapshot table has no product_title column; the consumer falls
        # back to the SKU code if missing, but we prefer the real RU name.
        if agg:
            day_from = start_ts.date()
            day_to   = (end_ts - timedelta(microseconds=1)).date()
            fo_rows = sess.execute(
                select(
                    FinanceOrder.sku_title.label("sku_title"),
                    func.max(FinanceOrder.product_title).label("pt"),
                )
                .where(
                    FinanceOrder.shop_id == sid_str,
                    FinanceOrder.period_from >= day_from,
                    FinanceOrder.period_from <= day_to,
                    FinanceOrder.sku_title.in_(list(agg.keys())),
                )
                .group_by(FinanceOrder.sku_title)
            ).all()
            for r in fo_rows:
                if r.sku_title in agg and r.pt:
                    agg[r.sku_title]["product_title"] = r.pt
            for key, entry in agg.items():
                if not entry["product_title"]:
                    entry["product_title"] = key

        return agg
    finally:
        if owns:
            sess.close()


def read_daily_expense_breakdown(
    shop_id: int | str,
    start_ts: datetime,
    end_ts: datetime,
    *,
    session: Session | None = None,
) -> dict:
    """Per-source expense breakdown + refunds income, for daily Telegram.

    Returns the `__expenses__`-shape payload consumed by
    ``_render_sales_image``::

        {
            "items":          [{"name": <source>, "amount": <sum>}, ...],
            "total":          <int — SUM(Оплата) excluding Логистика>,
            "refunds_income": <int — SUM(Возврат)>,
        }

    Filtering (plan §8):
      * ``Оплата`` rows with ``source != 'Логистика'`` are shown as expense
        line items (NULL source bucketed under "Прочее" to avoid drop).
      * ``Возврат`` rows are summed into ``refunds_income`` — rendered as
        the "Возврат Денег" bottom line when > 0.
    """
    shop_ids = _coerce_shop_ids(shop_id)
    if not shop_ids:
        return {"items": [], "total": 0, "refunds_income": 0}
    sid = shop_ids[0]

    sess, owns = _resolve_session(session)
    try:
        # Оплата grouped by source (excluding Логистика).
        op_stmt = (
            select(
                ExpensesLedger.source,
                func.coalesce(func.sum(ExpensesLedger.amount), 0).label("amt"),
            )
            .where(
                ExpensesLedger.shop_id == sid,
                ExpensesLedger.charged_at >= start_ts,
                ExpensesLedger.charged_at < end_ts,
                ExpensesLedger.op_type == "Оплата",
                (ExpensesLedger.source != "Логистика") | (ExpensesLedger.source.is_(None)),
            )
            .group_by(ExpensesLedger.source)
        )
        op_rows = sess.execute(op_stmt).all()

        # Возврат — one sum for the entire window.
        ref_stmt = select(
            func.coalesce(func.sum(ExpensesLedger.amount), 0).label("amt")
        ).where(
            ExpensesLedger.shop_id == sid,
            ExpensesLedger.charged_at >= start_ts,
            ExpensesLedger.charged_at < end_ts,
            ExpensesLedger.op_type == "Возврат",
        )
        refunds_amt = sess.execute(ref_stmt).scalar() or 0
    finally:
        if owns:
            sess.close()

    items: list[dict] = []
    total = 0
    for r in op_rows:
        amt = int(r.amt or 0)
        if amt <= 0:
            continue
        name = (r.source or "Прочее").strip() or "Прочее"
        items.append({"name": name, "amount": amt})
        total += amt
    items.sort(key=lambda x: (-x["amount"], x["name"]))

    return {
        "items": items,
        "total": total,
        "refunds_income": int(refunds_amt or 0),
    }
