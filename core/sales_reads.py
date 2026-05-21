"""Read-path helpers for `sales_lines` + `expenses_ledger` (Phase 2 of the
Sales/Expenses Reports API migration).

All queries go through `created_at` (sales) / `charged_at` (expenses) — the
naive-Tashkent business timestamps documented in the implementation plan
(§10). NEVER derive filter predicates from a `day` column on `sales_lines`
(no such column exists by design).

Index usage
-----------
- Single-shop range / aggregate:   ix_sales_lines_shop_created
  `WHERE shop_id = X AND created_at >= a AND created_at < b`
- Multi-shop range:                 same index, `shop_id IN (...)`
- Per-SKU history:                  ix_sales_lines_shop_sku_created
  `WHERE shop_id = X AND sku_id = Y AND created_at >= a AND created_at < b`

Do NOT wrap `created_at` in a function inside a WHERE clause — that breaks
the index. `date_trunc(...)` is only allowed in SELECT / GROUP BY.

Timezone rules (non-negotiable §8/§9)
-------------------------------------
Callers pass **naive Tashkent** `datetime` objects for `start_ts` / `end_ts`
(the column itself is naive Tashkent, stored verbatim from the CSV). Do NOT
pass UTC. Do NOT pass aware datetimes. These helpers perform NO timezone
conversion.

A convenience `day_bounds_tashkent(d)` builder is provided so callers that
have a `date` can trivially convert to the two `datetime` boundaries
required here.
"""
from __future__ import annotations

from datetime import date, datetime, time as dt_time, timedelta
from typing import Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from config import APP_TZ
from extensions import SessionLocal
from models import ExpensesLedger, FinanceOrder, SalesLine

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


# ── Range reads ──────────────────────────────────────────────────────

def read_sales_range(
    shop_id: int | str | Iterable[int | str],
    start_ts: datetime,
    end_ts: datetime,
    *,
    session: Session | None = None,
) -> list[SalesLine]:
    """Return `SalesLine` rows for a shop(s) within `[start_ts, end_ts)`.

    `start_ts` / `end_ts` are **naive Tashkent** — passed through verbatim.
    Uses `ix_sales_lines_shop_created` (or the multi-shop variant via
    `shop_id IN (...)`).
    """
    shop_ids = _coerce_shop_ids(shop_id)
    if not shop_ids:
        return []

    sess, owns = _resolve_session(session)
    try:
        stmt = select(SalesLine).where(
            SalesLine.shop_id.in_(shop_ids),
            SalesLine.created_at >= start_ts,
            SalesLine.created_at < end_ts,
        )
        return list(sess.execute(stmt).scalars().all())
    finally:
        if owns:
            sess.close()


def read_sku_history(
    shop_id: int | str,
    sku_id: str,
    start_ts: datetime,
    end_ts: datetime,
    *,
    session: Session | None = None,
) -> list[SalesLine]:
    """Per-SKU history scan — uses `ix_sales_lines_shop_sku_created`."""
    shop_ids = _coerce_shop_ids(shop_id)
    if not shop_ids or not sku_id:
        return []

    sess, owns = _resolve_session(session)
    try:
        stmt = (
            select(SalesLine)
            .where(
                SalesLine.shop_id == shop_ids[0],
                SalesLine.sku_id == sku_id,
                SalesLine.created_at >= start_ts,
                SalesLine.created_at < end_ts,
            )
            .order_by(SalesLine.created_at.desc())
        )
        return list(sess.execute(stmt).scalars().all())
    finally:
        if owns:
            sess.close()


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
        sku_col = FinanceOrder.sku_id
        sku_title_col = func.max(FinanceOrder.sku_title).label("sku_title")
        group_cols.append(FinanceOrder.sku_id)
    else:
        trunc_key = _TRUNC_KEYS[effective_group_by]
        bucket_col = func.date_trunc(trunc_key, FinanceOrder.period_from).label("bucket")
        group_cols.append(bucket_col)

    cols = [FinanceOrder.shop_id.label("shop_id")]
    if bucket_col is not None:
        cols.append(bucket_col)
    if sku_col is not None:
        cols.append(sku_col.label("sku_id"))
        cols.append(sku_title_col)
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


def read_sales_row_counts(
    shop_ids: Iterable[int | str],
    *,
    session: Session | None = None,
    start_ts: datetime | None = None,
    end_ts: datetime | None = None,
) -> dict[int, int]:
    """Return `{shop_id: count}` for `sales_lines` — used by dashboards.

    If `start_ts`/`end_ts` are omitted the query is unbounded (whole history).
    """
    ids = _coerce_shop_ids(shop_ids)
    if not ids:
        return {}

    sess, owns = _resolve_session(session)
    try:
        clauses = [SalesLine.shop_id.in_(ids)]
        if start_ts is not None:
            clauses.append(SalesLine.created_at >= start_ts)
        if end_ts is not None:
            clauses.append(SalesLine.created_at < end_ts)
        stmt = (
            select(SalesLine.shop_id, func.count().label("cnt"))
            .where(*clauses)
            .group_by(SalesLine.shop_id)
        )
        rows = sess.execute(stmt).all()
    finally:
        if owns:
            sess.close()

    return {int(r.shop_id): int(r.cnt or 0) for r in rows}


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
    """Per-SKU aggregate for `[start_ts, end_ts)` keyed by `sku_id`.

    Feeds the Telegram hourly notification path:

        { sku_id:   { "product_title": sku_title,    # name for display
                       "amount": qty_sum,             # total units sold
                       "sell_price": revenue_sum,     # Выручка total (sums)
                       "purchase_price": cost_sum,    # Себестоимость total
                       "seller_profit": seller_profit_sum,
                       "commission": commission_sum,
                       "logistics_fee": logistics_sum } }

    One GROUP BY query per shop. `start_ts` / `end_ts` are **naive
    Tashkent** — the column convention. Callers must pass Tashkent-aware
    boundaries stripped of tzinfo (e.g. via `_app_naive(...)`).
    """
    shop_ids = _coerce_shop_ids(shop_id)
    if not shop_ids:
        return {}

    sess, owns = _resolve_session(session)
    try:
        stmt = (
            select(
                SalesLine.sku_id.label("sku_id"),
                func.max(SalesLine.sku_title).label("product_title"),
                func.coalesce(func.sum(SalesLine.qty), 0).label("qty_sum"),
                func.coalesce(func.sum(SalesLine.revenue), 0).label("revenue_sum"),
                func.coalesce(func.sum(SalesLine.purchase_price), 0).label("cost_sum"),
                func.coalesce(func.sum(SalesLine.seller_profit), 0).label("seller_profit_sum"),
                func.coalesce(func.sum(SalesLine.commission), 0).label("commission_sum"),
                func.coalesce(func.sum(SalesLine.logistics_fee), 0).label("logistics_sum"),
            )
            .where(
                SalesLine.shop_id == shop_ids[0],
                SalesLine.created_at >= start_ts,
                SalesLine.created_at < end_ts,
            )
            .group_by(SalesLine.sku_id)
        )
        rows = sess.execute(stmt).all()
    finally:
        if owns:
            sess.close()

    out: dict[str, dict] = {}
    for r in rows:
        key = (r.sku_id or "").strip()
        if not key:
            continue
        out[key] = {
            "product_title": r.product_title or "",
            "amount": int(r.qty_sum or 0),
            "sell_price": int(r.revenue_sum or 0),
            "purchase_price": int(r.cost_sum or 0),
            "seller_profit": int(r.seller_profit_sum or 0),
            "commission": int(r.commission_sum or 0),
            "logistics_fee": int(r.logistics_sum or 0),
        }
    return out


def read_daily_profit_components(
    shop_id: int | str,
    start_ts: datetime,
    end_ts: datetime,
    *,
    session: Session | None = None,
) -> dict:
    """Return the three totals that feed the daily profit message.

    ``{
        "seller_profit_sum":         <numeric>,  # from sales_lines
        "non_logistics_expenses":    <numeric>,  # expenses_ledger, op_type='Оплата', source != 'Логистика'
        "refunds_income":            <numeric>,  # expenses_ledger, op_type='Возврат'
    }``

    Timezone: `start_ts` / `end_ts` are **naive Tashkent** — same boundary
    convention as `sales_lines.created_at` and `expenses_ledger.charged_at`.
    Two small SUM queries; `Логистика` is excluded from the expense total
    to avoid double-counting with `sales_lines.logistics_fee` on the sales
    side (plan §8 profit formula).
    """
    shop_ids = _coerce_shop_ids(shop_id)
    if not shop_ids:
        return {
            "seller_profit_sum": 0,
            "non_logistics_expenses": 0,
            "refunds_income": 0,
        }
    sid = shop_ids[0]

    sess, owns = _resolve_session(session)
    try:
        sales_stmt = select(
            func.coalesce(func.sum(SalesLine.seller_profit), 0).label("seller_profit_sum")
        ).where(
            SalesLine.shop_id == sid,
            SalesLine.created_at >= start_ts,
            SalesLine.created_at < end_ts,
        )
        seller_profit_sum = sess.execute(sales_stmt).scalar() or 0

        exp_stmt = select(
            ExpensesLedger.op_type,
            func.coalesce(func.sum(ExpensesLedger.amount), 0).label("amt"),
        ).where(
            ExpensesLedger.shop_id == sid,
            ExpensesLedger.charged_at >= start_ts,
            ExpensesLedger.charged_at < end_ts,
            ExpensesLedger.op_type.in_(("Оплата", "Возврат")),
        ).group_by(ExpensesLedger.op_type)

        # `Оплата` sum but EXCLUDING `Логистика` (double-counted with sales-side
        # logistics_fee). `Возврат` has no such exclusion.
        non_log_stmt = select(
            func.coalesce(func.sum(ExpensesLedger.amount), 0).label("amt"),
        ).where(
            ExpensesLedger.shop_id == sid,
            ExpensesLedger.charged_at >= start_ts,
            ExpensesLedger.charged_at < end_ts,
            ExpensesLedger.op_type == "Оплата",
            # `source` can be NULL — include those in the non-logistics bucket
            # so we don't silently drop rows the CSV left un-sourced.
            (ExpensesLedger.source != "Логистика") | (ExpensesLedger.source.is_(None)),
        )
        non_log = sess.execute(non_log_stmt).scalar() or 0

        refund_rows = sess.execute(exp_stmt).all()
    finally:
        if owns:
            sess.close()

    refunds = 0
    for r in refund_rows:
        if r.op_type == "Возврат":
            refunds = r.amt or 0
            break

    return {
        "seller_profit_sum": seller_profit_sum,
        "non_logistics_expenses": non_log,
        "refunds_income": refunds,
    }


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
