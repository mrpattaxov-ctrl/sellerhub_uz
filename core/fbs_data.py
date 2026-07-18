"""FBS / DBS data source seam.

Stage 4c (current): list + count + detail reads come from the local
``fbs_orders`` Postgres cache, populated every ~10 min by the background
worker in ``app.py``. ``?refresh=1`` on a list / count call forces a
just-in-time Uzum sync for that (shop, status) pair before the read,
so a seller hitting "Yangilash" sees up-to-the-second state without
waiting for the next worker tick.

History
-------
Stage 1 — every call went straight to Uzum via ``core.uzum_openapi``.
Stage 1.5 — added SWR caching on list + count to absorb burst load.
Stage 4c — flipped the cache filling: the worker writes to Postgres
on a schedule and reads come from there. SWR is still wrapped around
the DB read so dozens of concurrent /fbs pageloads don't fan out into
dozens of identical SQL queries.

The routes layer (``fbs/routes.py``) imports ONLY from this module —
never ``core.uzum_openapi`` directly (except the action endpoints,
which intentionally talk to Uzum in real time). Frontend never sees a
difference between Uzum-direct and DB-cached responses — the dicts we
return go through ``core.fbs_sync.row_to_dict`` to match Uzum's shape.

Cache keys (prefix ``fbs:``)
----------------------------
- ``fbs:count:{shop_id}:{status}``
- ``fbs:orders:{shop_id}:{status}:{scheme_or_ALL}:{page}:{size}``

Calls with ``date_from_ms`` / ``date_to_ms`` set are NEVER cached.
The route layer triggers ``invalidate_fbs_cache(shop_id)`` after an
action endpoint succeeds (confirm/cancel/etc) to wipe the warm cache.
"""
from __future__ import annotations

import io
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from datetime import datetime

from sqlalchemy import select, update, delete, func, and_, or_, cast, String
from sqlalchemy.orm import defer

from extensions import SessionLocal
from models import FbsOrder
from core.redis_client import redis_client
from core.swr import swr_get
from core.fbs_sync import (
    fetch_all_pages as _fbs_fetch_all_pages,
    # Drain + a "did we see the whole status?" flag. The interactive live path
    # PRUNES off the result, so it must know when the set is truncated.
    fetch_all_pages_checked as _fbs_fetch_all_pages_checked,
    upsert_orders as _fbs_upsert_orders,
    row_to_dict as _fbs_row_to_dict,
    # The button-prune (Yangilash) and the worker reconcile must agree on
    # which statuses are "active" — small queues whose JIT drain is
    # authoritative enough to prune phantoms from. Single source of truth.
    FBS_ACTIVE_SYNC_STATUSES as _FBS_ACTIVE_SYNC_STATUSES,
)
from core.uzum_openapi import (
    # Single-page fetch — JIT refresh path for the visible page of the
    # current chip; cheaper than draining all pages when the user just
    # wants "what's at the top right now."
    fetch_fbs_orders_page,
    extract_fbs_orders_list,
    # Count-only fetch — for the chip badge refresh. Uzum's /count
    # endpoint returns just an integer, ~5-10× faster than draining
    # the /orders pages just to count them.
    fetch_fbs_orders_count,
    # Detail fallback — DB miss falls through to a live Uzum lookup so
    # very old completed orders (outside the worker's 30-day window) are
    # still viewable. List + count have no fallback; if the worker is
    # behind, the user sees stale-but-warm data.
    fetch_fbs_order_detail,
    # Stage 2 action endpoints — write-side; never cached. Each call
    # invalidates the per-shop list/count cache so the next list view
    # reflects the new status.
    confirm_fbs_order,
    cancel_fbs_order,
    attach_fbs_identifiers,
    download_fbs_label,
    fetch_fbs_return_reasons,
    # Stage 3 DBS action endpoints — same write-side pattern as the
    # FBS actions above; never cached, each invalidates the per-shop
    # list cache so the next /fbs view shows the new status.
    mark_dbs_delivering,
    mark_dbs_completed,
    refund_dbs_order,
    UzumAPIError,
    FBS_ORDER_STATUSES,
    FBS_ORDER_SCHEMES,
)


# SWR TTLs (seconds). Now that reads are cheap DB queries the cache is
# mostly there to absorb thundering-herd refreshes (the /fbs counts-all
# endpoint fans out 11 calls at once). A short window is fine.
_COUNT_SOFT_TTL = 30
_COUNT_HARD_TTL = 120
_ORDERS_SOFT_TTL = 15
_ORDERS_HARD_TTL = 60


# ─────────────────────────────────────────────────────────────────────
# Read path — DB queries returning Uzum-shaped dicts.
# ─────────────────────────────────────────────────────────────────────


def _ms_to_naive_utc(ms: int | None):
    """Epoch milliseconds → naive UTC datetime. ``None`` passes through."""
    if ms is None:
        return None
    return datetime.utcfromtimestamp(int(ms) / 1000.0)


def _like_escape(s: str) -> str:
    """Escape LIKE/ILIKE metacharacters so a raw user query matches LITERALLY.

    Without this, a search for an SKU containing ``_`` or ``%`` (e.g. a seller
    SKU code like ``ABC_01``) would treat ``_`` as "any single char" and ``%``
    as "any run of chars", returning wrong rows. Pair with ``escape="\\"`` on
    the ``ilike()`` call. Not a SQL-injection fix (the bind is parameterised) —
    purely a correctness fix for the wildcard characters themselves.
    """
    return (
        s.replace("\\", "\\\\")
         .replace("%", "\\%")
         .replace("_", "\\_")
    )


# N5 (HAR audit A.13) — most-urgent-first ordering for the active queue.
# A seller racing a deadline needs the order about to breach its SLA at the
# TOP, not the newest order. The relevant clock depends on status:
#   CREATED                                   → accept_until (confirm before this)
#   PACKING / PENDING_DELIVERY / DELIVERING   → deliver_until (ship before this)
# Terminal/other statuses have no actionable deadline → see _EVENT_DATE_SORT_COLUMN.
_DEADLINE_SORT_COLUMN = {
    "CREATED": FbsOrder.accept_until,
    "PACKING": FbsOrder.deliver_until,
    "PENDING_DELIVERY": FbsOrder.deliver_until,
    "DELIVERING": FbsOrder.deliver_until,
}

# Terminal statuses carry no live deadline, but the seller cares about RECENT
# activity: the order most recently completed/cancelled/returned belongs at the
# TOP, not the one most recently *created*. An order created in October but
# completed yesterday should lead the COMPLETED tab — sorting by date_created
# would bury it under newer-but-older-event rows. So sort by the terminal event
# date DESC, with date_created DESC as the tiebreaker (covers legacy rows whose
# event date was never backfilled — nulls_last keeps them below dated rows).
_EVENT_DATE_SORT_COLUMN = {
    "COMPLETED": FbsOrder.completed_date,
    "CANCELED": FbsOrder.cancelled_date,
    "RETURNED": FbsOrder.return_date,
}


def _fbs_list_order_by(status: str):
    """ORDER BY clause for a single-status list page.

    Deadline-bearing statuses sort by their deadline ASC (soonest first) so
    the most-urgent order floats to the top. Terminal statuses
    (COMPLETED/CANCELED/RETURNED) sort by their event date DESC (most recently
    processed first). Every other status keeps ``date_created DESC``. In all
    cases ``date_created DESC`` is the tiebreaker and ``nulls_last`` keeps a
    missing date from jumping ahead of a real one.

    Note: this trades the composite-index sort order (shop_id, status,
    date_created DESC) for an explicit sort step, but a single-status page is
    small (one status, paginated), so the sort is cheap. The WHERE still rides
    the index on (shop_id, status).
    """
    col = _DEADLINE_SORT_COLUMN.get(status)
    if col is not None:
        return [col.asc().nulls_last(),
                FbsOrder.date_created.desc().nulls_last(),
                FbsOrder.id.desc()]
    event_col = _EVENT_DATE_SORT_COLUMN.get(status)
    if event_col is not None:
        return [event_col.desc().nulls_last(),
                FbsOrder.date_created.desc().nulls_last(),
                FbsOrder.id.desc()]
    return [FbsOrder.date_created.desc().nulls_last(), FbsOrder.id.desc()]


def _read_orders_from_db(
    shop_uzum_id: str | int,
    *,
    status: str,
    scheme: str | None,
    page: int,
    size: int,
    date_from_ms: int | None = None,
    date_to_ms: int | None = None,
    q: str | None = None,
) -> dict:
    """Read one page of orders from ``fbs_orders``. Returns a dict
    shaped for the SWR cache (``orders`` / ``total`` / ``used_url``)
    so callers don't have to know whether the value is fresh or cached.

    ``q`` (Bosqich 5a — search): case-insensitive substring match against
    the JSONB ``items_json`` column cast to text. Catches productTitle,
    skuTitle, and sku in one shot because each appears as a substring of
    the JSON wire form. Full-table scan today; if rows ever climb past
    ~10k consider a trigram GIN index on ``items_json::text``.
    """
    sid = str(shop_uzum_id)
    base_filters = [FbsOrder.shop_id == sid, FbsOrder.status == status]
    if scheme:
        base_filters.append(FbsOrder.order_type == scheme)
    if date_from_ms is not None:
        base_filters.append(FbsOrder.date_created >= _ms_to_naive_utc(date_from_ms))
    if date_to_ms is not None:
        base_filters.append(FbsOrder.date_created <= _ms_to_naive_utc(date_to_ms))
    if q:
        base_filters.append(
            cast(FbsOrder.items_json, String).ilike(f"%{_like_escape(q)}%", escape="\\")
        )

    with SessionLocal() as db:
        total = int(db.execute(
            select(func.count())
            .select_from(FbsOrder)
            .where(*base_filters)
        ).scalar() or 0)

        # The composite index (shop_id, status, date_created DESC) serves the
        # WHERE directly. N5 sorts deadline-bearing statuses by acceptUntil/
        # deliverUntil instead of date_created, so those add a small sort step
        # over the single-status page (cheap — one status, paginated).
        # Bosqich A.5 (#2): defer the heavy ``raw_json`` JSONB blob — the
        # list render never reads a raw_json-only field (only the explicit
        # projection + orderItems), so loading the full original payload
        # per row is pure waste. ``include_raw=False`` below matches: it
        # never touches the deferred column, so no lazy-load fires after
        # the session closes.
        rows = db.execute(
            select(FbsOrder)
            .options(defer(FbsOrder.raw_json))
            .where(*base_filters)
            .order_by(*_fbs_list_order_by(status))
            .offset(int(page) * int(size))
            .limit(int(size))
        ).scalars().all()

    return {
        "orders": [_fbs_row_to_dict(r, include_raw=False) for r in rows],
        "total": total,
        "used_url": "db://fbs_orders",
    }


def _read_count_from_db(
    shop_uzum_id: str | int,
    *,
    status: str,
    date_from_ms: int | None = None,
    date_to_ms: int | None = None,
) -> dict:
    """Read one count from ``fbs_orders``. SWR-friendly dict shape."""
    sid = str(shop_uzum_id)
    filters = [FbsOrder.shop_id == sid, FbsOrder.status == status]
    if date_from_ms is not None:
        filters.append(FbsOrder.date_created >= _ms_to_naive_utc(date_from_ms))
    if date_to_ms is not None:
        filters.append(FbsOrder.date_created <= _ms_to_naive_utc(date_to_ms))

    with SessionLocal() as db:
        n = int(db.execute(
            select(func.count())
            .select_from(FbsOrder)
            .where(*filters)
        ).scalar() or 0)
    return {"count": n, "used_url": "db://fbs_orders"}


# ─────────────────────────────────────────────────────────────────────
# Just-in-time refresh — used when ?refresh=1 hits a list/count endpoint.
# ─────────────────────────────────────────────────────────────────────


# An order that joins a накладная LEAVES its active status on Uzum
# (PACKING → PENDING_DELIVERY) — it did not vanish. PENDING_DELIVERY is never
# background-synced (app.py: no entry in the status interval map), so a prune
# that DELETES the departed row destroys the only local trace of it — and that
# row's ``invoice_number`` is what proves the накладная belongs to this seller
# (``_user_owns_invoice``). Deleting it made the seller's own invoice detail
# 403 as "foreign shop" (2026-07-13).
#
# The rule must NOT be "keep the row if it carries an invoice_number": that
# column stays populated all the way through DELIVERED/COMPLETED, so keying off
# it would restamp a just-delivered order BACKWARDS into PENDING_DELIVERY.
#
# Instead we ask Uzum where the order actually went. A departed row triggers one
# PENDING_DELIVERY drain; Uzum's PENDING_DELIVERY payload carries the
# invoiceNumber (verified 2026-07-13 — the repair run restored 6 rows complete
# with their numbers), so the upsert re-homes the order into the right status
# with the right number, for invoices we created AND ones the seller built in
# the Uzum app. Whatever is STILL sitting in the drained status afterwards was
# not in an invoice — it is a genuine phantom, and the delete is safe.
_INVOICE_HOLDING_STATUS = "PENDING_DELIVERY"

# Page cap for the INTERACTIVE drain. The seller is waiting on this request and
# the browser aborts at 15s, while each page is paced ≥1s through the shared
# per-token bucket. 10 pages = up to 500 orders in one status — far above any
# real active queue — and bounds the worst case at ~10s. Hitting the cap marks
# the drain incomplete, so it degrades to "render what we got, prune nothing"
# instead of deleting the orders behind the cap.
_LIVE_DRAIN_MAX_PAGES = 10


def _settle_departed_status_rows(
    db, shop_uzum_ids, status: str, fresh_ids: set | None,
    *, token: str | None = None,
) -> tuple[int, int]:
    """Reconcile rows that Uzum no longer reports under ``status``.

    ``fresh_ids`` is the authoritative set Uzum just returned; ``None`` means
    Uzum reported the status EMPTY (the caller must have confirmed that
    independently — see :func:`_confirm_status_empty`), so every row in the
    status has departed.

    ``token`` enables the re-home step (one extra paced Uzum call, and ONLY
    when something actually departed). Without it the departed rows are simply
    deleted — the pre-2026-07-13 behaviour, which loses invoice ownership.

    Returns ``(rehomed, deleted)``.
    """
    ids = [str(s) for s in shop_uzum_ids if s is not None and str(s).strip()]
    if not ids:
        return (0, 0)

    def _departed():
        """WHERE clauses selecting the rows that left ``status``."""
        clauses = [FbsOrder.shop_id.in_(ids), FbsOrder.status == status]
        if fresh_ids:
            clauses.append(~FbsOrder.order_id.in_({str(i) for i in fresh_ids}))
        return clauses

    departed_ids = {
        str(r[0]) for r in db.execute(
            select(FbsOrder.order_id).where(*_departed())
        ).all()
    }
    if not departed_ids:
        return (0, 0)

    rehomed = 0
    # Only a non-invoice status can have lost orders TO an invoice.
    if token and status != _INVOICE_HOLDING_STATUS:
        try:
            pd_orders = _fbs_fetch_all_pages(
                token, ids, status=_INVOICE_HOLDING_STATUS, fail_fast=True,
            )
        except Exception as e:
            # Re-home unavailable → do NOT delete on a guess. The rows stay put
            # (a phantom lingers until the next press) rather than risk wiping
            # an invoice's ownership proof on a transient Uzum error.
            print(f"[fbs_data] prune: {_INVOICE_HOLDING_STATUS} re-home fetch "
                  f"failed ({e!r}) — keeping {len(departed_ids)} departed "
                  f"{status} row(s)")
            return (0, 0)
        if pd_orders:
            _fbs_upsert_orders(db, ids[0], orders=pd_orders)
            rehomed = len(
                departed_ids & {str(o.get("id")) for o in pd_orders
                                if o.get("id") is not None}
            )
            if rehomed:
                print(f"[fbs_data] prune: re-homed {rehomed} {status} row(s) → "
                      f"{_INVOICE_HOLDING_STATUS} (joined a накладная) "
                      f"shops={','.join(ids)}")

    # Re-homed rows no longer match ``status``, so this delete cannot touch
    # them. What remains departed is a phantom.
    res = db.execute(delete(FbsOrder).where(*_departed()))
    deleted = res.rowcount or 0
    if deleted:
        print(f"[fbs_data] prune: removed {deleted} phantom {status} row(s) "
              f"shops={','.join(ids)}")
    return (rehomed, deleted)


def _prune_departed_status_rows(
    db, shop_uzum_ids, status: str, fresh_ids: set,
    *, token: str | None = None,
) -> int:
    """Delete ``fbs_orders`` rows for these shops still sitting in
    ``status`` that Uzum's authoritative drain no longer returned — the
    "arvox"/phantom orders that left the status out-of-band (the seller
    cancelled in the Uzum app, or Uzum auto-cancelled an order whose
    deadline lapsed). Either way the order just stops appearing in the
    status list, and an UPSERT-only sync can never clear it — it freezes
    on the list forever. This is the interactive twin of the background
    worker's reconcile step (``app.py::_fbs_sync_one_token``), so a seller
    pressing "Yangilash" clears the phantom immediately instead of waiting
    up to ~10 min for the next worker tick.

    SAFETY MODEL — two guards make this incapable of deleting a live order:

      1. Caller restricts this to ACTIVE statuses only, whose queues are
         tiny so the drained set is reliably COMPLETE. A terminal status
         (COMPLETED/CANCELED) can exceed the paginator's page cap, so a
         prune there could delete real history — never call it for those.

      2. TARGETED prune only (``order_id NOT IN fresh_ids``): when
         ``fresh_ids`` is EMPTY we do NOTHING. An empty drain means "Uzum
         reports zero orders in this status" — trusting that to wipe the
         whole status would let a single fluke empty-200 nuke live orders.
         The all-phantom case is handled by the callers that can confirm it
         independently (:func:`refresh_shops_status_live` via ``/count``, the
         worker via its 2-strike counter), never here.

      3. A row that departed INTO a накладная is re-homed, not deleted — see
         :func:`_settle_departed_status_rows`. Pass ``token`` to enable that;
         without it a departed invoiced row is deleted and its накладная loses
         its ownership proof.

    Returns the number of rows deleted.
    """
    ids = [str(s) for s in shop_uzum_ids if s is not None and str(s).strip()]
    if not ids or not fresh_ids:
        return 0
    _rehomed, deleted = _settle_departed_status_rows(
        db, ids, status, fresh_ids, token=token,
    )
    return deleted


def _wipe_status_rows(db, shop_uzum_ids, status: str, *, token: str | None = None) -> int:
    """Clear a status Uzum reports as EMPTY: rows that joined a накладная are
    re-homed to PENDING_DELIVERY, the rest are phantoms and get deleted. Only
    ever called after :func:`_confirm_status_empty` has independently agreed the
    status is empty — see :func:`refresh_shops_status_live` for the two-source
    safety model.
    """
    ids = [str(s) for s in shop_uzum_ids if s is not None and str(s).strip()]
    if not ids:
        return 0
    _rehomed, deleted = _settle_departed_status_rows(
        db, ids, status, None, token=token,
    )
    return deleted


def _confirm_status_empty(token: str, shop_uzum_ids, status: str) -> bool:
    """Second opinion on "this status has zero orders", via the independent
    ``/v2/fbs/orders/count`` endpoint.

    The list drain returning an empty page is the one signal we must not trust
    on its own: a single fluke empty-200 from Uzum would otherwise wipe live
    orders. ``/count`` is a different endpoint with a different payload shape,
    so both lying with the same fake zero in the same second is not a failure
    mode we've ever seen. Requiring BOTH to say zero is what lets the
    interactive press clear an all-phantom status immediately instead of
    deferring to the worker's 2-strike reconcile (~10-20 min).

    Fails CLOSED: any error (429, 5xx, timeout) returns False → no wipe.
    """
    try:
        count, _ = fetch_fbs_orders_count(
            token, [str(s) for s in shop_uzum_ids], status=status, fail_fast=True,
        )
    except Exception as e:
        print(f"[fbs_data] live-prune: /count confirm FAILED for {status} "
              f"({e!r}) — keeping rows")
        return False
    return int(count or 0) == 0


def refresh_shops_status_live(
    token: str,
    shop_uzum_ids: list[str | int] | tuple,
    status: str,
) -> tuple[int, int]:
    """Make ``fbs_orders`` for this (shops, status) EXACTLY match Uzum, now.

    This is the interactive read path for the active chips (Yangi /
    Yig'ilmoqda / Yo'lda). It drains every page of the status — so the result
    is the COMPLETE authoritative set, not the visible page — then upserts it
    and deletes whatever local row Uzum no longer reports. After it returns,
    a plain DB read of this status yields precisely what Uzum just said, which
    is what makes the rendered list phantom-free by construction.

    Why the DB is still written at all when the goal is "live": the row is the
    only server-side source for things the orders list itself never shows —
    the label warm-set (``core.fbs_label_cache``), product-QR items, bulk-action
    ownership, invoice creation and time-slot deadlines. Those keep working
    because the mirror keeps being written; the *screen* just stops depending
    on it being fresh.

    Three prune modes, split by how much we trust the drain:

      * drain COMPLETE and non-empty → TARGETED prune (rows not in the fresh
        set). The set is the whole truth, so anything else in this status has
        departed.
      * drain complete and EMPTY → the risky case (clear the whole status). We
        require an independent ``/count`` to also report zero before deleting
        anything (:func:`_confirm_status_empty`). Previously this case pruned
        nothing at all, which is exactly why an all-phantom status (Uzum says
        0, we hold 1) could never be cleared by pressing the chip.
      * drain INCOMPLETE (hit the page cap, or an empty page landed mid-drain —
        see ``fetch_all_pages_checked``) → NO prune. The rows we didn't see are
        not phantoms, and deleting them would destroy live orders. The list
        still renders whatever Uzum did return.

    Returns ``(upserted, pruned)``.
    """
    ids = [str(s).strip() for s in shop_uzum_ids if s is not None and str(s).strip()]
    if not ids:
        return (0, 0)

    orders, complete = _fbs_fetch_all_pages_checked(
        token, ids, status=status, fail_fast=True,
        max_pages=_LIVE_DRAIN_MAX_PAGES,
    )
    fresh_ids = {str(o.get("id")) for o in orders if o.get("id") is not None}

    prunable = complete and status in _FBS_ACTIVE_SYNC_STATUSES
    if not complete:
        print(f"[fbs_data] live: {status} drain INCOMPLETE ({len(orders)} order(s) "
              f"in {_LIVE_DRAIN_MAX_PAGES} page(s) max) — skipping prune")
    # Second-source an empty drain BEFORE opening the session: this is a paced
    # network round-trip and must not sit inside an open transaction.
    empty_confirmed = (
        prunable and not fresh_ids and _confirm_status_empty(token, ids, status)
    )

    upserted = 0
    pruned = 0
    with SessionLocal() as db:
        if orders:
            upserted = _fbs_upsert_orders(db, ids[0], orders=orders)
        # Prune is for the active queues only: a terminal status can exceed the
        # paginator's cap, so its drain is not authoritative and a delete there
        # could destroy real history.
        if prunable:
            if fresh_ids:
                pruned = _prune_departed_status_rows(
                    db, ids, status, fresh_ids, token=token,
                )
            elif empty_confirmed:
                pruned = _wipe_status_rows(db, ids, status, token=token)
        db.commit()
    return (upserted, pruned)


def _refresh_shop_status(token: str, shop_uzum_id: str | int, status: str) -> int:
    """Fetch ``(shop, status)`` fresh from Uzum and UPSERT into
    ``fbs_orders``. Returns the number of orders upserted.

    Single-status — when the user views one chip's listing, we sync just
    that chip's data. The worker's full-shop tick still runs every 10
    min; this is the "right now" override.

    Bosqich 4f: upsert (not DELETE+INSERT) — historical orders stay
    intact, and orders that have moved away from this status keep their
    old row until the next worker tick rewrites it. The seller's
    Yangilash press only adds/updates rows for orders Uzum currently
    reports under this status.

    Caller passes the user's OpenAPI token. We don't fall back to anything
    sensible if Uzum is down (the caller already invalidated SWR cache,
    so the next DB read still works on whatever's there).

    Bosqich A.6: pacing is handled INSIDE ``_fbs_fetch_all_pages`` via the
    shared per-token gate (``core.fbs_locks.pace_uzum_call``), so this
    live refresh no longer takes a coarse lock and no longer skips when
    the worker is mid-tick. It interleaves with the worker, paced ``>=1s``
    apart, so the seller always gets a live read while staying under
    Uzum's per-token burst threshold.
    """
    # Bosqich A.9 (#5) — interactive refresh: fail fast on 429/5xx (~2-3s)
    # instead of pinning this request's worker thread for ~6 min of retry.
    orders = _fbs_fetch_all_pages(token, shop_uzum_id, status=status, fail_fast=True)
    with SessionLocal() as db:
        n = _fbs_upsert_orders(db, shop_uzum_id, orders=orders)
        # Yangilash-prune (active statuses only): the drain above is the
        # COMPLETE authoritative set for this (shop, status), so any local
        # row still in this status that Uzum no longer returns is a phantom
        # — remove it now instead of waiting for the worker's reconcile.
        # See _prune_departed_status_rows for why this can't drop a live row.
        if status in _FBS_ACTIVE_SYNC_STATUSES:
            fresh_ids = {str(o.get("id")) for o in orders if o.get("id") is not None}
            _prune_departed_status_rows(db, [shop_uzum_id], status, fresh_ids)
        db.commit()
    return n


def _refresh_shops_status(
    token: str,
    shop_uzum_ids: list[str | int] | tuple,
    status: str,
) -> int:
    """Multi-shop variant of :func:`_refresh_shop_status`.

    Hits ``/v2/fbs/orders`` ONCE with ``shopIds=A&shopIds=B&...`` for
    this status, drains its pages, then upserts every returned order in
    a single transaction. For an N-shop user this collapses
    ``N × _refresh_shop_status`` (N round-trips) into one — same network
    cost regardless of how many shops the token sees.

    Per-order ``shopId`` from the response routes each order back to
    its own row (see ``core.fbs_sync.dict_from_order`` — "Bosqich 4l —
    token-batched sync" comment). The ``shop_uzum_ids[0]`` we pass to
    ``upsert_orders`` is the documented fallback for the rare case Uzum
    omits ``shopId`` from an order body; in production logs that path
    hasn't triggered.

    Bosqich A.6: pacing is handled inside ``_fbs_fetch_all_pages`` via the
    shared per-token gate, so this refresh no longer takes a coarse lock
    or skips when the worker is mid-tick — it interleaves with the worker,
    paced ``>=1s`` apart, under Uzum's per-token burst threshold.
    """
    ids = [str(s).strip() for s in shop_uzum_ids if s is not None and str(s).strip()]
    if not ids:
        return 0
    # Bosqich A.9 (#5) — interactive multi-shop refresh: fail fast (~2-3s).
    orders = _fbs_fetch_all_pages(token, ids, status=status, fail_fast=True)
    with SessionLocal() as db:
        n = _fbs_upsert_orders(db, ids[0], orders=orders)
        # Yangilash-prune across the shop group: same authoritative-drain
        # reasoning as the single-shop path. Each order routes back to its
        # own shop by id, so a phantom on any shop in the group is removed.
        if status in _FBS_ACTIVE_SYNC_STATUSES:
            fresh_ids = {str(o.get("id")) for o in orders if o.get("id") is not None}
            _prune_departed_status_rows(db, ids, status, fresh_ids)
        db.commit()
    return n


def _refresh_shops_counts(
    token: str,
    shop_uzum_ids: list[str | int] | tuple,
    statuses: list[str] | tuple,
) -> dict[str, int]:
    """Fetch fresh counts for several statuses via Uzum's ``/count``
    endpoint, return ``{status: count, ...}``.

    Used by the Yangilash press to refresh the chip badges of the 3
    active-work statuses (CREATED/PACKING/PENDING_DELIVERY). The count
    endpoint returns a single integer per call, ~5-10× faster than
    draining ``/v2/fbs/orders`` pages just to count them. No DB write —
    the response goes straight into the JSON the route emits.

    Bosqich A.8 — burst fix: these calls are now issued SEQUENTIALLY, each
    paced through the shared per-token gate (``pace_uzum_call`` lives inside
    ``fetch_fbs_orders_count``). The old code fired all statuses CONCURRENTLY
    via a thread pool on the assumption that ``/count`` is burst-exempt — an
    UNVERIFIED claim. Putting 3 ``/count`` calls on a token inside one second
    is exactly Uzum's per-token burst trigger (429), which then cost a
    60/120/180s retry storm (a multi-minute stuck spinner). Pacing makes a
    3-status press ~3s instead of ~0.5s, but it no longer risks the ban.
    """
    ids = [str(s).strip() for s in shop_uzum_ids if s is not None and str(s).strip()]
    if not ids or not statuses:
        return {}
    out: dict[str, int] = {}
    for status in statuses:
        try:
            # Bosqich A.9 (#5) — interactive count refresh: fail fast (~2-3s).
            count, _ = fetch_fbs_orders_count(token, ids, status=status, fail_fast=True)
            out[status] = int(count)
        except Exception as e:
            print(f"[fbs_data] counts refresh shops={ids} status={status}: {e!r}")
    return out


def _refresh_shops_status_first_page(
    token: str,
    shop_uzum_ids: list[str | int] | tuple,
    status: str,
    *,
    size: int = 50,
) -> int:
    """Lightweight JIT refresh: fetch just the FIRST page (multi-shop)
    of one status, upsert the returned orders. Returns the number of
    rows upserted.

    Used by the Yangilash press path on the orders endpoint: the seller
    is looking at page 0 and wants "what's at the top right now",
    regardless of which chip they're on. Draining all pages would be
    fine for CREATED/PACKING (low volume) but punishingly slow on
    COMPLETED/CANCELED (thousands of rows = dozens of pages). One page
    is ~1-2s for any status, every time.

    The chip *count* may not be exactly accurate for the non-active
    statuses (the bg worker keeps the full picture in DB); but the
    list the seller sees IS live from Uzum, which is what Abdulaziz
    asked for 2026-05-26: "yuzer har doim uzumdan olib ko'rsatsin,
    dbdan emas".
    """
    ids = [str(s).strip() for s in shop_uzum_ids if s is not None and str(s).strip()]
    if not ids:
        return 0
    # Bosqich A.11 — pacing is now handled centrally by the shared per-token
    # bucket inside _fbs_orders_request_with_auth (see uzum_openapi), so this
    # path no longer needs its own gate; the bucket interleaves it with the
    # worker + finance/products across all processes.
    # Bosqich A.9 (#5) — interactive first-page refresh: fail fast (~2-3s).
    body, _ = fetch_fbs_orders_page(
        token, ids, status=status, page=0, size=size,
        fail_fast=True,
    )
    orders, _ = extract_fbs_orders_list(body)
    if not orders:
        return 0
    # Yangilash-prune guard: this path fetches only ONE page, so the set is
    # authoritative-COMPLETE only when the page wasn't full. A full page
    # (== size) means page 1+ may hold orders we never fetched, and pruning
    # off a partial set could delete a real order → only prune on a short
    # page. Active queues are tiny (< size), so in practice this fires for
    # exactly the statuses where phantoms occur, and defers to the worker
    # for the rare full-page active status.
    page_is_complete = len(orders) < size
    with SessionLocal() as db:
        n = _fbs_upsert_orders(db, ids[0], orders=orders)
        if page_is_complete and status in _FBS_ACTIVE_SYNC_STATUSES:
            fresh_ids = {str(o.get("id")) for o in orders if o.get("id") is not None}
            _prune_departed_status_rows(db, ids, status, fresh_ids)
        db.commit()
    return n


# ─────────────────────────────────────────────────────────────────────
# Public API — same signatures as before, internals flipped to DB.
# ─────────────────────────────────────────────────────────────────────


def get_fbs_orders(
    token: str,
    shop_uzum_id: str | int,
    *,
    status: str = "CREATED",
    scheme: str | None = None,
    page: int = 0,
    size: int = 20,
    date_from_ms: int | None = None,
    date_to_ms: int | None = None,
    q: str | None = None,
    refresh: bool = False,
) -> tuple[list[dict], int, str]:
    """List FBS/DBS orders. Returns ``(orders, total, used_url)``.

    Stage 4c: read from ``fbs_orders`` DB cache. ``refresh=True``
    triggers a just-in-time Uzum sync for this (shop, status) before
    the DB read, so the seller's "Yangilash" press surfaces orders
    that were created between worker ticks.

    ``refresh=True`` is the LIVE path (Abdulaziz 2026-07-13: "har bosilganda
    Uzumdan jonli olinsin, Uzum bilan 100% bir xil bo'lsin"): it reconciles the
    status against Uzum — drain every page, upsert, delete what Uzum no longer
    reports — and then reads back WITHOUT the SWR cache. Reading a status that
    was just made byte-identical to Uzum is the same thing as rendering Uzum's
    reply, minus a second parse of the payload; going through the row also
    keeps the fields Uzum omits from the list (customer name, localized title).
    Serving that read from a 15s cache would hand back a pre-reconcile snapshot
    and put the phantom straight back on screen, so ``refresh`` skips SWR.

    Filter calls (``date_from_ms`` / ``date_to_ms`` / ``q``) bypass the
    SWR layer — the caller is asking for a custom slice we shouldn't
    share across sellers/requests. They still go through the DB.
    """
    if refresh:
        # Best-effort: a failed sync (Uzum down, token revoked) shouldn't
        # block the read — it just means the user sees whatever the
        # worker had last time. Logging the failure helps debugging.
        try:
            refresh_shops_status_live(token, [shop_uzum_id], status)
            # Wipe the SWR layer so the next loader actually hits DB,
            # not a stale cached value.
            invalidate_fbs_cache(shop_uzum_id)
        except Exception as e:
            print(f"[fbs_data] refresh shop={shop_uzum_id} status={status} ERROR: {e!r}")

    if refresh or date_from_ms is not None or date_to_ms is not None or q:
        result = _read_orders_from_db(
            shop_uzum_id,
            status=status, scheme=scheme,
            page=page, size=size,
            date_from_ms=date_from_ms, date_to_ms=date_to_ms,
            q=q,
        )
        return (result["orders"], result["total"], result["used_url"])

    key = f"fbs:orders:{shop_uzum_id}:{status}:{scheme or 'ALL'}:{page}:{size}"

    def _load():
        return _read_orders_from_db(
            shop_uzum_id,
            status=status, scheme=scheme,
            page=page, size=size,
        )

    cached = swr_get(
        key,
        soft_ttl=_ORDERS_SOFT_TTL,
        hard_ttl=_ORDERS_HARD_TTL,
        loader=_load,
    )
    return (cached["orders"], cached["total"], cached["used_url"])


def get_fbs_count(
    token: str,
    shop_uzum_id: str | int,
    *,
    status: str,
    date_from_ms: int | None = None,
    date_to_ms: int | None = None,
    refresh: bool = False,
) -> tuple[int, str]:
    """Single-status count. Returns ``(count, used_url)``.

    Stage 4c: SELECT COUNT(*) FROM ``fbs_orders``. ``refresh=True``
    runs the same JIT sync the list path uses.
    """
    if refresh:
        try:
            _refresh_shop_status(token, shop_uzum_id, status)
            invalidate_fbs_cache(shop_uzum_id)
        except Exception as e:
            print(f"[fbs_data] refresh-count shop={shop_uzum_id} status={status} ERROR: {e!r}")

    if date_from_ms is not None or date_to_ms is not None:
        result = _read_count_from_db(
            shop_uzum_id,
            status=status,
            date_from_ms=date_from_ms, date_to_ms=date_to_ms,
        )
        return (result["count"], result["used_url"])

    key = f"fbs:count:{shop_uzum_id}:{status}"

    def _load():
        return _read_count_from_db(shop_uzum_id, status=status)

    cached = swr_get(
        key,
        soft_ttl=_COUNT_SOFT_TTL,
        hard_ttl=_COUNT_HARD_TTL,
        loader=_load,
    )
    return (cached["count"], cached["used_url"])


def get_fbs_order_detail(
    token: str,
    order_id: str | int,
    *,
    fail_fast: bool = False,
) -> tuple[dict, str]:
    """Single-order detail. Returns ``(order_dict, used_url)``.

    Stage 4c: try DB first; fall back to Uzum on miss.

    Why a fallback exists: the worker only syncs the active lifecycle
    statuses + last 30 days of COMPLETED. An older completed/cancelled
    order won't be in the local cache — without the fallback the detail
    page would 404, even though the order does exist on Uzum.

    NOT wrapped in SWR — detail views are one-shot, caching a single
    seller's drill-down doesn't help anyone else.
    """
    oid = str(order_id)
    with SessionLocal() as db:
        row = db.execute(
            select(FbsOrder).where(FbsOrder.order_id == oid).limit(1)
        ).scalar_one_or_none()
    if row is not None:
        return (_fbs_row_to_dict(row), "db://fbs_orders")
    # DB miss → fall back to Uzum. Routes layer enforces the shop-scope
    # check against ``order["shopId"]`` after this returns.
    return fetch_fbs_order_detail(token, order_id, fail_fast=fail_fast)


def get_fbs_orders_for_shops(
    shop_uzum_ids: list[str],
    *,
    status: str = "CREATED",
    scheme: str | None = None,
    page: int = 0,
    size: int = 20,
    date_from_ms: int | None = None,
    date_to_ms: int | None = None,
    q: str | None = None,
) -> tuple[list[dict], int, str]:
    """Aggregate list of FBS/DBS orders across multiple shops.

    Used by the "Hammasi" view on /fbs — reads ``fbs_orders`` filtered
    by ``shop_id IN (...)``. No Uzum call, no token required: the
    worker keeps each shop's rows fresh in the DB. Refresh (?refresh=1)
    on this view is intentionally a no-op — fanning out 11×N Uzum
    calls (N = number of shops) would be slow and rate-limit-prone for
    a feature whose whole point is convenience.

    Pagination spans the union: page=0 returns the top ``size`` orders
    across all shops by ``date_created DESC``. Returns Uzum-shaped
    dicts via ``row_to_dict`` so the template needs no changes.

    Search/date filters (``q``, ``date_from_ms``, ``date_to_ms``) — same
    semantics as :func:`get_fbs_orders`. Apply across the union.
    """
    if not shop_uzum_ids:
        return ([], 0, "db://fbs_orders")
    sids = [str(s) for s in shop_uzum_ids]
    base_filters = [FbsOrder.shop_id.in_(sids), FbsOrder.status == status]
    if scheme:
        base_filters.append(FbsOrder.order_type == scheme)
    if date_from_ms is not None:
        base_filters.append(FbsOrder.date_created >= _ms_to_naive_utc(date_from_ms))
    if date_to_ms is not None:
        base_filters.append(FbsOrder.date_created <= _ms_to_naive_utc(date_to_ms))
    if q:
        base_filters.append(
            cast(FbsOrder.items_json, String).ilike(f"%{_like_escape(q)}%", escape="\\")
        )

    with SessionLocal() as db:
        total = int(db.execute(
            select(func.count())
            .select_from(FbsOrder)
            .where(*base_filters)
        ).scalar() or 0)
        # Bosqich A.5 (#2): defer ``raw_json`` (see _read_orders_from_db) —
        # the "Hammasi" list render uses only the projection + orderItems.
        rows = db.execute(
            select(FbsOrder)
            .options(defer(FbsOrder.raw_json))
            .where(*base_filters)
            .order_by(*_fbs_list_order_by(status))
            .offset(int(page) * int(size))
            .limit(int(size))
        ).scalars().all()
    return ([_fbs_row_to_dict(r, include_raw=False) for r in rows], total, "db://fbs_orders")


def get_fbs_counts_for_shops(shop_uzum_ids: list[str]) -> dict[str, int]:
    """All-11-statuses count for the "Hammasi" view.

    One SQL: ``GROUP BY status`` over ``shop_id IN (...)``. Returns the
    full status→count map with explicit zeros for empty buckets so the
    frontend's chip rendering doesn't have to special-case missing keys.
    """
    from core.uzum_openapi import FBS_ORDER_STATUSES as _ALL_STATUSES
    counts: dict[str, int] = {s: 0 for s in _ALL_STATUSES}
    if not shop_uzum_ids:
        return counts
    sids = [str(s) for s in shop_uzum_ids]
    with SessionLocal() as db:
        rows = db.execute(
            select(FbsOrder.status, func.count())
            .where(FbsOrder.shop_id.in_(sids))
            .group_by(FbsOrder.status)
        ).all()
    for status, n in rows:
        if status in counts:
            counts[status] = int(n or 0)
    return counts


def get_owned_invoice_numbers(shop_uzum_ids: list[str]) -> set[str]:
    """Distinct ``fbs_orders.invoice_number`` for the given shops.

    The shop-scope source of truth for FBS invoices: Uzum's invoice payload
    carries no ``shopId``, but every order carries its ``invoiceNumber`` and
    the order-sync writes only registered shops — so this set is exactly the
    invoices those shops own. Used by the накладные list filter, the by-id
    ownership guards (fbs/routes) and the worker's akt prefetch (app.py).
    Empty set for an empty shop list (callers treat that as "owns nothing").
    """
    sids = [str(s) for s in (shop_uzum_ids or []) if s is not None and str(s).strip()]
    if not sids:
        return set()
    with SessionLocal() as db:
        rows = db.execute(
            select(FbsOrder.invoice_number)
            .where(FbsOrder.shop_id.in_(sids))
            .where(FbsOrder.invoice_number.isnot(None))
            .distinct()
        ).all()
    return {str(r[0]) for r in rows if r[0] is not None}


def get_last_synced_at_for_shops(shop_uzum_ids: list[str]) -> str | None:
    """Most recent ``synced_at`` across the given shops. ``None`` if
    no shop has any synced rows yet.
    """
    if not shop_uzum_ids:
        return None
    sids = [str(s) for s in shop_uzum_ids]
    with SessionLocal() as db:
        ts = db.execute(
            select(func.max(FbsOrder.synced_at))
            .where(FbsOrder.shop_id.in_(sids))
        ).scalar()
    if ts is None:
        return None
    return ts.isoformat(timespec="milliseconds") + "Z"


def get_last_synced_at(shop_uzum_id: str | int) -> str | None:
    """Return the most recent ``synced_at`` for any row of this shop.

    Used by /fbs/api/orders to drive the "Oxirgi yangilangan: N daq
    oldin" indicator on the list page. Returns an ISO-8601 string with
    trailing ``Z`` so the browser can hand it to ``new Date(...)``
    directly. None if the shop has no synced rows yet.

    Cheap query — the ``ix_fbs_orders_shop_synced`` index makes this
    sub-millisecond. No SWR wrap needed.
    """
    sid = str(shop_uzum_id)
    with SessionLocal() as db:
        ts = db.execute(
            select(func.max(FbsOrder.synced_at))
            .where(FbsOrder.shop_id == sid)
        ).scalar()
    if ts is None:
        return None
    return ts.isoformat(timespec="milliseconds") + "Z"


def invalidate_fbs_cache(shop_uzum_id: str | int) -> None:
    """Delete all SWR cache entries for one shop. Called after an
    action endpoint mutates state (so the next list read picks up the
    new status) and after a ?refresh=1 sync (so the SWR layer's stale
    value doesn't shadow the freshly synced DB rows).

    Uses ``scan_iter`` rather than ``KEYS`` — the latter blocks the
    single-threaded Redis event loop on large keyspaces. Our cache
    keyspace is small today but this code outlives that assumption.
    """
    sid = str(shop_uzum_id)
    for pattern in (f"fbs:count:{sid}:*", f"fbs:orders:{sid}:*"):
        for key in redis_client.scan_iter(match=pattern, count=200):
            redis_client.delete(key)


# ─────────────────────────────────────────────────────────────────────
# Stage 2 action wrappers — pattern: call Uzum, then invalidate the
# per-shop cache so the list + count views show the new state on next
# load. Errors propagate as ``UzumAPIError`` for the route layer to map.
# ─────────────────────────────────────────────────────────────────────


# Return-reasons enum changes rarely — Uzum doesn't add cancel reasons
# every day. 1h soft / 2h hard means the cancel modal opens instantly
# for everyone after the first warm-up.
_RETURN_REASONS_SOFT_TTL = 3600
_RETURN_REASONS_HARD_TTL = 7200


def _apply_action_response_to_db(
    shop_uzum_id,
    response: dict,
    *,
    token: str | None = None,
    order_id: str | int | None = None,
    fail_fast: bool = False,
) -> bool:
    """UPSERT the post-action order row into ``fbs_orders``.

    Returns ``True`` when an UPSERT actually happened, ``False`` when we
    bailed out (silent-skip cases: refetch failed, Uzum returned nothing
    useful, DB write errored). Callers in the background-thread path
    use the bool to log a truthful outcome instead of always saying
    "OK" — see ``cancel_order._bg_refetch``.

    Uzum's confirm/cancel/etc endpoints sometimes echo back the full
    order body in its NEW state (same shape as ``/v1/fbs/order/{id}``),
    but ``cancel`` in particular usually returns ``{}`` — the meaningful
    signal is the 200 status. Without an explicit refetch the DB row
    keeps the pre-action status until the next worker tick (up to 10
    min away), so the seller cancels an order and the UI still shows
    ``Jo'natishga tayyor`` after reload.

    Strategy (echo shapes confirmed against the official swagger
    2026-06-01 — see fbs_docs/AUDIT_ROADMAP.md A.22):
      1. If ``response`` already contains an ``id``, upsert it directly
         (no extra Uzum call). Only ``confirm`` reaches this path: its
         echo is the FULL order body, so the upsert never blanks a
         column (no thin-echo risk).
      2. Else if ``token`` and ``order_id`` are provided, fetch the
         order detail and upsert that. This is the path for ``cancel``
         and ``dbs/refund`` (echo ``{}``) and ``identifier`` (echo is a
         ``[{type,required,values}]`` LIST, not a dict, so it never has
         an ``id``). One extra Uzum call, but the seller pressed a
         button so it's action-triggered, not periodic.
      3. Else (legacy call sites that don't pass token), skip and let
         the worker self-heal within 10 min.

    N4b (HAR audit): because every action echo is either FULL (confirm)
    or id-less (cancel/refund/identifier → refetch), the dangerous
    "thin-but-has-id" upsert that would blank stock/scheme/dates can
    never occur on these endpoints. No defensive merge needed today.

    Best-effort throughout: the Uzum action already succeeded, so any
    local DB hiccup is swallowed (logged, not raised) — the worker
    will reconcile on the next tick. Existing call sites that ignore
    the return value (confirm_order, attach_identifiers) are unaffected.
    """
    order_row: dict | None = None
    if isinstance(response, dict) and response.get("id"):
        order_row = response
    elif token and order_id is not None:
        try:
            fresh, _ = fetch_fbs_order_detail(token, order_id, fail_fast=fail_fast)
            if isinstance(fresh, dict) and fresh.get("id"):
                order_row = fresh
        except Exception as e:
            print(
                f"[fbs_data] action-refetch order={order_id!r} ERROR: {e!r} "
                f"(worker tick will reconcile within 10 min)"
            )
            return False

    if order_row is None:
        return False

    try:
        from core.fbs_sync import upsert_orders as _do_upsert
        with SessionLocal() as db:
            _do_upsert(db, shop_uzum_id, orders=[order_row])
            db.commit()
        return True
    except Exception as e:
        print(f"[fbs_data] action-upsert order={order_row.get('id')!r} ERROR: {e!r}")
        return False


def confirm_order(
    token: str,
    order_id: str | int,
    shop_uzum_id: str | int,
    *,
    fail_fast: bool = False,
) -> tuple[dict, str]:
    """Confirm an FBS order. On success, writes the new state into the
    DB row and wipes per-shop cache — so the list page AND the detail
    page reload both show the new PACKING state immediately, without
    waiting for the next 10-min worker tick.
    """
    order, used_url = confirm_fbs_order(token, order_id, fail_fast=fail_fast)
    _apply_action_response_to_db(
        shop_uzum_id, order, token=token, order_id=order_id,
        fail_fast=fail_fast,
    )
    invalidate_fbs_cache(shop_uzum_id)
    return (order, used_url)


def cancel_order(
    token: str,
    order_id: str | int,
    shop_uzum_id: str | int,
    *,
    reason: str,
    comment: str | None = None,
    fail_fast: bool = False,
) -> tuple[dict, str]:
    """Cancel an FBS order. Returns ASAP; reconciles the DB row in a
    background thread.

    Why the background thread (2026-05-26): a naive synchronous "cancel
    + refetch + upsert" took ~4-5 sec end-to-end because Uzum's cancel
    response is an empty body, so we had to do a second HTTP roundtrip
    (GET /v1/fbs/order/{id}) to learn the post-cancel state before
    writing to DB. Sellers perceived this as "stuck" / "long thinking".

    Now the flow is:

      1. Sync: POST /cancel to Uzum (~1-2s — the only network hop the
         caller waits for).
      2. Sync: update the local row with what we already KNOW
         (status=CANCELED, cancelled_date=now, cancel_reason=<the code
         we just submitted>). This is what makes the seller's reload
         feel instant.
      3. Sync: invalidate the per-shop cache so /fbs reads see the
         new state immediately.
      4. Background daemon thread: refetch GET /v1/fbs/order/{id} and
         UPSERT — overwrites our local guess with Uzum's canonical
         row (notably, replacing the English reason code with the
         localized title like "Tovar yo'q"). Errors here are logged
         and dropped; the 10-min worker tick reconciles.

    If the seller reloads within the ~1-2 sec the background thread
    takes, they may briefly see the English reason code. Cheap UX cost
    vs. the seconds saved on every cancel.

    Timing logs are tagged ``[fbs.cancel]`` so the breakdown is visible
    via ``docker compose logs -f app | grep fbs.cancel``.
    """
    # ── Phase 1: synchronous Uzum cancel ──────────────────────────
    t0 = time.perf_counter()
    body, used_url = cancel_fbs_order(
        token, order_id, reason=reason, comment=comment,
        fail_fast=fail_fast,
    )
    print(f"[fbs.cancel] phase=uzum_post order={order_id} "
          f"took={time.perf_counter()-t0:.2f}s", flush=True)

    # ── Phase 2: synchronous manual DB update ─────────────────────
    # Uzum cancel returned 2xx, so we KNOW the order is cancelled
    # server-side. Write that to the local row without waiting for a
    # refetch. Best-effort: if the row doesn't exist or the UPDATE
    # fails, log and move on — the background refetch will create /
    # repair the row.
    t1 = time.perf_counter()
    try:
        with SessionLocal() as db:
            res = db.execute(
                update(FbsOrder)
                .where(FbsOrder.order_id == str(order_id))
                .values(
                    status="CANCELED",
                    cancelled_date=datetime.utcnow(),
                    cancel_reason=reason,
                )
            )
            db.commit()
            # rowcount==0 ⇒ the order isn't in our DB yet (not synced). The
            # cancel DID succeed on Uzum, but it left no local trace, so the
            # row stays uncancelled until the next worker tick. Surface it so
            # a "cancelled on Uzum but still shows active locally" report is
            # diagnosable from the log instead of looking like a silent no-op.
            if (res.rowcount or 0) == 0:
                print(f"[fbs.cancel] manual-update order={order_id!r} "
                      f"matched 0 rows (not yet synced) — relying on worker "
                      f"refetch to create the row", flush=True)
    except Exception as e:
        print(f"[fbs.cancel] manual-update order={order_id!r} ERROR: {e!r}",
              flush=True)
    print(f"[fbs.cancel] phase=db_update order={order_id} "
          f"took={time.perf_counter()-t1:.2f}s", flush=True)

    # ── Phase 3: invalidate cache so the next /fbs read hits DB ───
    invalidate_fbs_cache(shop_uzum_id)

    # ── Phase 4: background refetch ────────────────────────────────
    # Daemon thread so the API caller doesn't wait. Captures the few
    # variables it needs by closure; nothing here mutates caller state.
    # The bool returned by ``_apply_action_response_to_db`` lets us log
    # OK vs DEGRADED honestly — silent refetch failures used to log OK
    # which made debugging "why is cancel_reason still the English code"
    # impossible.
    _bg_oid, _bg_shop, _bg_token = order_id, shop_uzum_id, token
    def _bg_refetch():
        t_bg = time.perf_counter()
        try:
            # Bosqich A.9 (#5) — this refetch runs AFTER the response is
            # already sent, so it stays PATIENT (fail_fast=False): a flaky
            # 429 here must NOT make the daemon hammer an already-throttled
            # token. Explicit, not relying on the default, so a future
            # change to the default can't silently make the daemon fast-fail.
            upserted = _apply_action_response_to_db(
                _bg_shop, {}, token=_bg_token, order_id=_bg_oid,
                fail_fast=False,
            )
            invalidate_fbs_cache(_bg_shop)
            outcome = "OK" if upserted else "DEGRADED"
            print(f"[fbs.cancel] phase=bg_refetch order={_bg_oid} "
                  f"took={time.perf_counter()-t_bg:.2f}s {outcome}",
                  flush=True)
            if not upserted:
                print(f"[fbs.cancel] phase=bg_refetch order={_bg_oid} note: "
                      f"local manual UPDATE stands; worker tick will reconcile",
                      flush=True)
        except Exception as e:
            print(f"[fbs.cancel] phase=bg_refetch order={_bg_oid} ERROR: {e!r}",
                  flush=True)
    threading.Thread(
        target=_bg_refetch, daemon=True,
        name=f"fbs-cancel-bg-{order_id}",
    ).start()

    return (body, used_url)


def attach_identifiers(
    token: str,
    order_id: str | int,
    shop_uzum_id: str | int,
    *,
    items: list[dict],
    fail_fast: bool = False,
) -> tuple[dict, str]:
    """Attach IMEIs/serials. Identifier attachment can advance the order
    out of CREATED (via implicit confirm in some flows), so we wipe the
    cache here too."""
    body, used_url = attach_fbs_identifiers(token, order_id, items=items, fail_fast=fail_fast)
    _apply_action_response_to_db(
        shop_uzum_id, body, token=token, order_id=order_id,
        fail_fast=fail_fast,
    )
    invalidate_fbs_cache(shop_uzum_id)
    return (body, used_url)


# ── Shipping-label download cache ────────────────────────────────────
# In-process LRU keyed by (order_id, size). Skips the Uzum HTTP call
# on re-clicks — sellers often press Yorliq → QR → Yorliq+QR within
# seconds, and without this cache each click adds 1+ Uzum API calls
# per selected order, which trips Uzum's burst-penalty (60–180s of
# throttling). TTL is short (5 min) because labels effectively never
# change once the order is past CREATED — what's printed is what's
# shipped. Max 200 entries keeps memory bounded.
_label_cache: dict[tuple[str, str], tuple[float, list[bytes], str]] = {}
_label_cache_lock = threading.Lock()
_LABEL_CACHE_TTL = 300
_LABEL_CACHE_MAX = 200


def _get_label_pdfs_cached(token: str, order_id, size: str, *, fail_fast: bool = False) -> tuple[list[bytes], str]:
    key = (str(order_id), str(size).upper())
    now = time.time()
    with _label_cache_lock:
        cached = _label_cache.get(key)
        if cached is not None and (now - cached[0]) < _LABEL_CACHE_TTL:
            return cached[1], cached[2]
    # Cache miss / stale → real Uzum call (outside the lock so concurrent
    # misses on different orders don't serialise).
    pdfs, used_url = download_fbs_label(token, order_id, size=size, fail_fast=fail_fast)
    # Stamp the entry with the time the download FINISHED, not when the
    # function started. The Uzum call (with its 429 retries) can take several
    # seconds; using the pre-call ``now`` would shorten the 300s TTL window by
    # that duration, expiring the cache early.
    stored_at = time.time()
    with _label_cache_lock:
        _label_cache[key] = (stored_at, pdfs, used_url)
        if len(_label_cache) > _LABEL_CACHE_MAX:
            # Drop the single oldest entry. Not strictly LRU but close
            # enough — we just need to keep the dict from growing without
            # bound.
            oldest_key = min(_label_cache, key=lambda k: _label_cache[k][0])
            _label_cache.pop(oldest_key, None)
    return pdfs, used_url


def get_label_pdfs(
    token: str,
    order_id: str | int,
    *,
    size: str = "LARGE",
    fail_fast: bool = False,
) -> tuple[list[bytes], str]:
    """Download label PDFs. NOT cached — printing should always produce
    a fresh PDF (the seller might re-print after a paper jam, etc.).

    Side effect: parses the customer name + city out of the first PDF
    and UPDATEs the FbsOrder row. Uzum's API never returns ``deliveryInfo``
    for FBS orders; the label is the only place the customer name shows
    up, so we grab it here. The seller pressing "Etiketka chop etish"
    is effectively a "reveal customer info" trigger — and that matches
    the workflow: they print the label, then look up who it's for.

    The parse + DB update is best-effort. If it fails, the seller still
    gets the PDF — we never block the print on a parse hiccup.

    Caching note: the actual Uzum HTTP call goes through
    ``_get_label_pdfs_cached`` (5-min TTL per order_id+size). Sellers
    routinely click "Yorliq → QR → Yorliq + QR" in quick succession,
    and without caching each click would trigger 1+ Uzum API call per
    selected order. Uzum's per-token burst penalty kicks in after a
    few rapid calls and adds 60–180s of throttling — making the
    seller's UX feel "stuck". Caching avoids that entirely.
    """
    pdfs, used_url = _get_label_pdfs_cached(token, order_id, size, fail_fast=fail_fast)

    if pdfs:
        try:
            from core.fbs_label_parser import parse_label_pdf
            info = parse_label_pdf(pdfs[0])
            name = info.get("customer_fullname")
            address = info.get("delivery_address")
            if name or address:
                from models import FbsOrder
                with SessionLocal() as db:
                    # Conditional UPDATE: only fill the fields if they
                    # were NULL — never overwrite something Uzum gave us
                    # (paranoid; today's API never sends these fields,
                    # but defensive against a future Uzum policy change).
                    updates = {}
                    if name:
                        updates["customer_fullname"] = name
                    if address:
                        updates["delivery_address"] = address
                    if updates:
                        db.execute(
                            update(FbsOrder)
                            .where(FbsOrder.order_id == str(order_id))
                            .values(**updates)
                        )
                        db.commit()
                        # Wipe the per-shop SWR cache so the list page's
                        # MIJOZ column reflects the just-extracted name
                        # on the next reload, not a stale cached row.
                        with SessionLocal() as _db2:
                            row = _db2.execute(
                                select(FbsOrder).where(FbsOrder.order_id == str(order_id))
                            ).scalar_one_or_none()
                            if row is not None:
                                invalidate_fbs_cache(row.shop_id)
        except Exception as e:
            # Print succeeded — never let a parse error reach the seller.
            print(f"[fbs_data] label parse order={order_id} ERROR: {e!r}")

    return (pdfs, used_url)


# Label-size presets — the small thermal-printer formats mirror the FBO
# product-labels sheet (``templates/print_labels.html`` /
# ``products/routes.py``) so the FBS sticker layout matches the
# warehouse's existing thermal rolls. The ``uzum`` preset matches Uzum's
# shipping-label physical dimensions (685×472pt landscape ≈ 242×166mm)
# so that when "label + product QR" is merged into one PDF the seller
# gets a clean print with EVERY page the same size (no mid-roll size
# jumps, no awkward A4 padding).
#
# Dimensions are in mm; ``tcol`` is the side-column width holding the
# rotated SKU / barcode-number text; ``qr`` is the QR square side.
_PRODUCT_LABEL_SIZES = {
    # NOTE: these MUST stay identical to fbs/routes.py `_QR_PRINT_SIZES` (the
    # standalone «QR» button's HTML print) so a «Yorliq + QR» merge renders the
    # product QR at the EXACT same proportions as «QR» (Abdulaziz 2026-06-17:
    # "QR pechat joyini QR+yorliq joyiga nusxala"). 30×20 va 40×30 oldin
    # kattaroq qr/tcol ishlatardi → endi tugma bilan bir xil.
    "30x20":  {"w": 30,  "h": 20,  "tcol": 7,  "qr": 14,  "sku_fs": 5.4, "num_fs": 6,  "num_last4_fs": 8},
    "43x25":  {"w": 43,  "h": 25,  "tcol": 8,  "qr": 20,  "sku_fs": 6,   "num_fs": 7,  "num_last4_fs": 9},
    "40x30":  {"w": 40,  "h": 30,  "tcol": 9,  "qr": 20,  "sku_fs": 6.5, "num_fs": 7,  "num_last4_fs": 9},
    "60x60":  {"w": 60,  "h": 60,  "tcol": 12, "qr": 34,  "sku_fs": 8,   "num_fs": 8,  "num_last4_fs": 11},
    "70x37":  {"w": 70,  "h": 37,  "tcol": 12, "qr": 40,  "sku_fs": 8,   "num_fs": 8,  "num_last4_fs": 11},
    # ``uzum`` = same physical PAGE size as the Uzum shipping label
    # (685×472pt landscape ≈ 242×166mm) so the merged «Yorliq + QR» PDF has
    # every page the same dimensions — no "tiny sticker squished between huge
    # pages". The QR/text PROPORTIONS mirror the standalone «QR» print's 40×30
    # preset (qr/h = 20/30 = 0.667, tcol/w = 9/40 = 0.225, fonts scaled by
    # h-ratio 166/30) so the merged QR prints the SAME relative size as the
    # «QR» button output — earlier qr=130 (qr/h=0.78) printed noticeably bigger
    # (Abdulaziz 2026-06-17: "sal kattaroq bo'lib di"). 111 ≈ 0.667×166,
    # 54 ≈ 0.225×242, 39 ≈ 7×166/30, 50 ≈ 9×166/30.
    "uzum":   {"w": 242, "h": 166, "tcol": 54, "qr": 111, "sku_fs": 36,  "num_fs": 39, "num_last4_fs": 50},
}
# Default = uzum so the merged "label + product QR" PDF has consistent
# page sizes across all pages.
_DEFAULT_PRODUCT_LABEL_SIZE = "uzum"
_MM_TO_PT = 72.0 / 25.4

# Process-level cache of fetched qrserver PNGs, keyed by (content, px). The same
# SKU/barcode appears across many orders in a bulk print; caching avoids
# re-hitting api.qrserver.com for an identical QR. Bounded by content variety
# (a few hundred SKUs at most) so a plain dict is fine.
_QR_PNG_CACHE: dict = {}


def render_product_qr_pdf(item: dict, size: str = _DEFAULT_PRODUCT_LABEL_SIZE,
                          *, match_uzum_label: bool = False) -> bytes:
    """Render a single product-QR sticker PDF matching the FBO format.

    ``match_uzum_label`` — when True, the page is emitted PORTRAIT-mediabox
    + ``/Rotate 90`` so it has the SAME geometry as Uzum's LARGE shipping
    label (472×685pt portrait + /Rotate 90). Used by the merged «Yorliq + QR»
    PDF: if the QR page stayed landscape-mediabox while the label page is
    portrait+rotate, the browser fits the mixed orientations to the FIRST
    page's portrait sheet → the QR shrank with whitespace (Abdulaziz
    2026-06-17). Matching the geometry makes every merged page print
    identically. Standalone QR prints (all-landscape) leave this False.

    Layout — identical to the existing FBO product-labels sheet
    (``templates/print_labels.html``) so warehouse staff see the same
    sticker shape they're used to:

        ┌──────┬──────────────┬──────┐
        │  S   │              │      │
        │  K   │  [QR code]   │  ◾   │  (S=SKU rotated -90°,
        │  U   │   centered   │  ◾   │   ◾=barcode digits rotated -90°)
        │      │              │      │
        └──────┴──────────────┴──────┘

    The QR encodes the item's ``barcode`` (Uzum's 13-digit code) when
    present, falling back to ``skuTitle`` — same content as the FBO
    sticker, so the same warehouse scanner recognises it. Returns
    ``b""`` on failure (caller skips this page).

    ``size`` is one of ``30x20 / 43x25 / 40x30 / 60x60 / 70x37`` mm.
    """
    try:
        import qrcode
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("[fbs_data] qrcode / PIL not installed — product QR unavailable")
        return b""

    sku = str(item.get("skuTitle") or "").strip()
    barcode = str(item.get("barcode") or "").strip()
    qr_content = barcode or sku
    if not qr_content:
        return b""

    lbl = _PRODUCT_LABEL_SIZES.get(size, _PRODUCT_LABEL_SIZES[_DEFAULT_PRODUCT_LABEL_SIZE])
    # SCALE chosen per-preset: small thermal stickers (e.g. 43×25mm)
    # need a high SCALE to produce a crisp printable image; the big
    # "uzum" preset (~242×166mm) would explode memory at high scale,
    # so cap it.
    SCALE = 4 if lbl["w"] > 100 else 8
    W_pt = int(round(lbl["w"] * _MM_TO_PT))
    H_pt = int(round(lbl["h"] * _MM_TO_PT))
    W_px = int(round(W_pt * SCALE))
    H_px = int(round(H_pt * SCALE))
    tcol_px = int(round(lbl["tcol"] * _MM_TO_PT * SCALE))
    qr_side_target = int(round(lbl["qr"] * _MM_TO_PT * SCALE))
    # Auto-fit: clamp QR side so it never overflows the page. The
    # preset's ``qr`` mm value is a TARGET — for 70×37mm the preset
    # says qr=40 but the page is only 37mm tall, so the QR has to
    # shrink to fit. Width limit = page minus the two text columns ONLY
    # (no extra inner margin) so the QR reaches the SAME size as the HTML
    # «QR» print (templates/fbs_qr_print.html), whose flex layout is just
    # [tcol][qr][tcol]. Subtracting an extra margin here shrank the merged
    # QR ~1 module below the «QR» button → seller saw a size diff
    # (Abdulaziz 2026-06-17: "QR bilan bir xil qil"). Height keeps a tiny
    # margin (the QR is centred with vertical slack anyway).
    inner_margin_px = max(1, int(round(1.5 * _MM_TO_PT * SCALE)))  # ~1.5mm
    max_qr_h = H_px - 2 * inner_margin_px
    max_qr_w = W_px - 2 * tcol_px
    qr_side_target = max(20, min(qr_side_target, max_qr_h, max_qr_w))

    def _font(size_pt: float, bold: bool = False):
        path = (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            if bold else
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        )
        # Font sizes are in PIL pixel units; convert pt → px via SCALE.
        try:
            return ImageFont.truetype(path, max(6, int(round(size_pt * SCALE))))
        except Exception:
            return ImageFont.load_default()

    def _wrap_text_to_lines(draw, text: str, font, max_width_px: int) -> list[str]:
        """Break `text` into lines that each fit within `max_width_px`.

        Prefers natural break points (hyphens, spaces); falls back to a
        hard character break only when a single token is wider than the
        column (rare for SKUs).
        """
        if not text:
            return [text]
        # Quick path: single line fits.
        if (draw.textbbox((0, 0), text, font=font))[2] <= max_width_px:
            return [text]
        # Build candidate split points after every hyphen / space.
        breaks = [i + 1 for i, ch in enumerate(text) if ch in "-_ /"]
        if breaks and breaks[-1] != len(text):
            breaks.append(len(text))
        elif not breaks:
            # No natural breaks — fall back to greedy character wrap.
            lines: list[str] = []
            cur = ""
            for ch in text:
                cand = cur + ch
                if draw.textbbox((0, 0), cand, font=font)[2] > max_width_px and cur:
                    lines.append(cur)
                    cur = ch
                else:
                    cur = cand
            if cur:
                lines.append(cur)
            return lines
        # Greedy fit using natural break points.
        lines: list[str] = []
        start = 0
        for i, b in enumerate(breaks):
            seg = text[start:b]
            # Look ahead — does start..next_break still fit?
            nxt = breaks[i + 1] if i + 1 < len(breaks) else len(text)
            cand = text[start:nxt]
            if draw.textbbox((0, 0), cand, font=font)[2] <= max_width_px:
                continue  # keep extending
            # cand too wide — commit the current seg up to b.
            lines.append(text[start:b])
            start = b
        if start < len(text):
            lines.append(text[start:])
        return lines

    def _vertical_text_strip(text: str, col_w: int, col_h: int,
                              font_pt: float, bold: bool) -> "Image.Image":
        """Draw `text` horizontally (autoshrink + wrap if needed), then
        rotate 90° CCW to produce a (col_w × col_h) strip with vertical
        text. Matches the FBO `print_labels.html` overflow-wrap style.
        """
        # Try the nominal font first, then a couple smaller steps, with
        # automatic line-wrap. Wrap target = FULL col_h (the strip's long
        # side), matching the HTML «QR» print's `.sku-top { width: lbl.h }`.
        # An earlier 0.92 margin made the SKU wrap one line MORE than the
        # button (3 lines vs 2, sitting high) — Abdulaziz 2026-06-17.
        chosen = None
        for ratio in (1.0, 0.9, 0.8):
            f = _font(font_pt * ratio, bold=bold)
            tmp = Image.new("RGB", (10, 10), "white")
            d = ImageDraw.Draw(tmp)
            lines = _wrap_text_to_lines(d, text, f, col_h)
            # Estimate total height = num_lines * line-height. If it
            # fits in col_w (= eventual strip width), keep it.
            ascent, descent = f.getmetrics()
            line_h = ascent + descent + 2  # small leading
            total_h = line_h * len(lines)
            if total_h <= col_w * 0.95:
                chosen = (f, lines, line_h, ascent)
                break
        if chosen is None:
            # Last-ditch: smaller font, accept whatever wraps.
            f = _font(font_pt * 0.7, bold=bold)
            tmp = Image.new("RGB", (10, 10), "white")
            d = ImageDraw.Draw(tmp)
            lines = _wrap_text_to_lines(d, text, f, col_h)
            ascent, descent = f.getmetrics()
            chosen = (f, lines, ascent + descent + 2, ascent)
        f, lines, line_h, ascent = chosen

        # Horizontal canvas: width=col_h, height=col_w (rotated later).
        canvas = Image.new("RGB", (col_h, col_w), "white")
        cd = ImageDraw.Draw(canvas)
        block_h = line_h * len(lines)
        y0 = max(0, (col_w - block_h) // 2)
        for li, line in enumerate(lines):
            bbox = cd.textbbox((0, 0), line, font=f)
            lw = bbox[2] - bbox[0]
            x = (col_h - lw) // 2
            y = y0 + li * line_h
            cd.text((x, y), line, fill="black", font=f)
        return canvas.rotate(90, expand=True)

    def _vertical_number_strip(text: str, col_w: int, col_h: int,
                                font_pt: float, last4_font_pt: float) -> "Image.Image":
        """Like _vertical_text_strip but the last 4 chars are drawn in
        a bigger/bolder font (matches FBO ``.number .last4`` rule).
        """
        if len(text) <= 4:
            return _vertical_text_strip(text, col_w, col_h, last4_font_pt, bold=True)
        head = text[:-4]
        tail = text[-4:]
        # Pick fonts that together fit horizontally; shrink both
        # proportionally if needed.
        chosen = None
        for ratio in (1.0, 0.9, 0.8, 0.7, 0.6):
            f_head = _font(font_pt * ratio, bold=False)
            f_tail = _font(last4_font_pt * ratio, bold=True)
            tmp = Image.new("RGB", (10, 10), "white")
            d = ImageDraw.Draw(tmp)
            w_head = d.textbbox((0, 0), head, font=f_head)[2]
            w_tail = d.textbbox((0, 0), tail, font=f_tail)[2]
            if (w_head + w_tail) <= col_h * 0.95:
                # Use the taller of the two for vertical centering.
                a_head, d_head = f_head.getmetrics()
                a_tail, d_tail = f_tail.getmetrics()
                chosen = (f_head, f_tail, w_head, w_tail,
                          max(a_head, a_tail), max(d_head, d_tail))
                break
        if chosen is None:
            f_head = _font(font_pt * 0.5, bold=False)
            f_tail = _font(last4_font_pt * 0.5, bold=True)
            tmp = Image.new("RGB", (10, 10), "white")
            d = ImageDraw.Draw(tmp)
            w_head = d.textbbox((0, 0), head, font=f_head)[2]
            w_tail = d.textbbox((0, 0), tail, font=f_tail)[2]
            a_head, d_head = f_head.getmetrics()
            a_tail, d_tail = f_tail.getmetrics()
            chosen = (f_head, f_tail, w_head, w_tail,
                      max(a_head, a_tail), max(d_head, d_tail))
        f_head, f_tail, w_head, w_tail, ascent, descent = chosen

        canvas = Image.new("RGB", (col_h, col_w), "white")
        cd = ImageDraw.Draw(canvas)
        total_w = w_head + w_tail
        x0 = (col_h - total_w) // 2
        # Baseline-align head and tail.
        a_head = f_head.getmetrics()[0]
        a_tail = f_tail.getmetrics()[0]
        baseline = (col_w + ascent - descent) // 2
        cd.text((x0, baseline - a_head), head, fill="black", font=f_head)
        cd.text((x0 + w_head, baseline - a_tail), tail, fill="black", font=f_tail)
        return canvas.rotate(90, expand=True)

    try:
        # ─── QR code ─────────────────────────────────────────────
        # PRIMARY: api.qrserver.com — the EXACT service the FBO «QR Chop etish»
        # page (templates/print_labels.html) uses. The seller confirmed its QR
        # prints crisp, so we generate the FBS sticker QR the same way instead of
        # the local raster. Request it at the target pixel size (qrserver caps at
        # 1000px) so no resampling is needed → clean modules. margin=0 matches the
        # FBO page (the white sticker around the QR is the quiet zone).
        # Cached per (content, px) for bulk prints; falls back to the local
        # qrcode lib if the network call fails so printing never breaks
        # (Abdulaziz 2026-06-16).
        qr_px = qr_side_target
        qr_img = None
        # INTEGER-upscale plan. qrserver caps the PNG at 1000px and we upscale by
        # an INTEGER factor (even modules → razor-sharp). When the target slot is
        # BIGGER than 1000px (the "uzum" preset: target≈1474) we must request a
        # SMALLER clean grid and multiply it back up — otherwise the old code
        # requested min(1000, target)=1000, then factor=round(1474/1000)=1 left
        # the QR at 1000px (~88mm) inside a 130mm slot → it printed ~60% too small
        # with a huge quiet zone (Abdulaziz 2026-06-17). ceil(target/1000) picks
        # the factor; target/factor is the source size to fetch.
        _qr_factor = max(1, (qr_side_target + 999) // 1000)
        _px = max(160, min(1000, int(round(qr_side_target / _qr_factor))))
        _ckey = (qr_content, _px)
        try:
            _raw = _QR_PNG_CACHE.get(_ckey)
            if _raw is None:
                import requests as _rq
                from urllib.parse import quote as _quote
                _url = (
                    "https://api.qrserver.com/v1/create-qr-code/"
                    "?ecc=M&margin=0&size=%dx%d&data=%s"
                    % (_px, _px, _quote(qr_content))
                )
                _resp = _rq.get(_url, timeout=8)
                if _resp.ok and _resp.content:
                    _raw = _resp.content
                    if len(_QR_PNG_CACHE) < 2000:
                        _QR_PNG_CACHE[_ckey] = _raw
            if _raw:
                qr_img = Image.open(io.BytesIO(_raw)).convert("L")
        except Exception as _qe:
            print(f"[fbs_data] qrserver fetch failed ({_qe!r}) — local QR fallback")
        if qr_img is not None:
            # qrserver gives a clean grid at `_px`. Scale to the target slot by an
            # INTEGER factor ONLY — a fractional NEAREST resize makes modules 1px
            # uneven again (the "uzum" preset, target≈1474 > qrserver's 1000px cap,
            # showed exactly this wobble). factor≥1 upscales evenly; when the source
            # already covers the slot we keep it and just centre it (slightly
            # smaller = larger quiet zone, still razor-sharp).
            src = qr_img.size[0]
            factor = max(1, int(round(qr_side_target / src)))
            if factor > 1:
                qr_img = qr_img.resize((src * factor, src * factor), Image.NEAREST)
            qr_px = qr_img.size[0]
            # Safety: never overflow the slot (rare tiny presets where src > target).
            if qr_px > qr_side_target:
                qr_img = qr_img.resize((qr_side_target, qr_side_target), Image.NEAREST)
                qr_px = qr_side_target
        else:
            # FALLBACK (offline / API down): local qrcode lib, scaled to an EVEN
            # multiple of the module grid so every module is the same px size.
            qr = qrcode.QRCode(
                version=None,
                error_correction=qrcode.constants.ERROR_CORRECT_M,
                box_size=10, border=1,
            )
            qr.add_data(qr_content)
            qr.make(fit=True)
            qr_img = qr.make_image(fill_color="black", back_color="white").convert("L")
            total_mod = qr.modules_count + 2 * qr.border
            box = max(1, qr_side_target // total_mod)
            qr_px = box * total_mod
            qr_img = qr_img.resize((qr_px, qr_px), Image.NEAREST)

        # ─── Compose ─────────────────────────────────────────────
        page = Image.new("RGB", (W_px, H_px), "white")
        page.paste(qr_img.convert("RGB"), ((W_px - qr_px) // 2,
                                           (H_px - qr_px) // 2))

        # SKU on left column, rotated -90°.
        sku_strip = _vertical_text_strip(sku or qr_content,
                                          tcol_px, H_px,
                                          lbl["sku_fs"], bold=True)
        page.paste(sku_strip, (0, 0))

        # Barcode number on right column, rotated -90°. Uses
        # _vertical_number_strip so the LAST 4 chars render in a bigger
        # bold font — matches the FBO ``.number .last4`` CSS rule.
        num_strip = _vertical_number_strip(
            qr_content, tcol_px, H_px,
            lbl["num_fs"], lbl.get("num_last4_fs", lbl["num_fs"] * 1.3),
        )
        page.paste(num_strip, (W_px - tcol_px, 0))

        # Save as PDF. resolution=72*SCALE so the page becomes exactly
        # W_pt × H_pt regardless of the pixel count.
        #
        # LOSSLESS — PIL's default PDF encoder stores RGB images as JPEG
        # (DCTDecode), which smears the QR's sharp black/white edges into grey
        # fuzz → THE "past sifat / xira" QR the seller saw (the even-module fix
        # above wasn't enough on its own). Convert to 1-bit with NO dither so PIL
        # stores it CCITT-lossless: every module stays a clean square, exactly
        # like the FBO «QR Chop etish» PNG. The page is pure B/W (black QR + text
        # on white), so 1-bit loses nothing (Abdulaziz 2026-06-16).
        bw = page.convert("1", dither=Image.NONE)
        if match_uzum_label:
            # PDF /Rotate 90 = ko'rsatishda 90° SOAT STRELKASI bo'yicha aylantiradi.
            # To'g'ri landshaft ko'rinishni olish uchun kontentni 90° CCW aylantirib
            # PORTRET qilib saqlaymiz; keyin /Rotate 90 qo'ysak ko'rsatishda yana
            # landshaftga qaytadi — Uzum yorlig'i (portret + /Rotate 90) bilan
            # bir xil tuzilish → birlashtirilgan PDF har sahifani bir xil bosadi.
            bw = bw.rotate(90, expand=True)
        buf = io.BytesIO()
        bw.save(buf, format="PDF", resolution=72 * SCALE)
        data = buf.getvalue()
        if match_uzum_label:
            try:
                from pypdf import PdfReader, PdfWriter
                reader = PdfReader(io.BytesIO(data))
                writer = PdfWriter()
                for p in reader.pages:
                    p.rotate(90)   # /Rotate 0 → 90 (soat strelkasi bo'yicha)
                    writer.add_page(p)
                obuf = io.BytesIO()
                writer.write(obuf)
                data = obuf.getvalue()
            except Exception as _re:
                # Aylantirish chiqmasa — landshaft variantni qaytaramiz (chop
                # baribir ishlaydi, faqat eski xatti-harakat).
                print(f"[fbs_data] QR portrait-rotate failed (using landscape): {_re!r}")
        return data
    except Exception as e:
        print(f"[fbs_data] product QR render failed for sku={sku!r}: {e!r}")
        return b""


def crop_label_to_qr(pdf_bytes: bytes) -> bytes:
    """Crop a label PDF to just the QR column (right side of display).

    Uzum's LARGE label is stored as a 472×685pt PORTRAIT page with
    ``/Rotate 90`` — viewers (Chrome, Acrobat) rotate it 90° clockwise
    to display landscape 685×472. The QR + delivery date + tracking
    number sit in the right ~42% of the visible (rotated) page, which
    corresponds to the TOP slice (high y) of the underlying portrait
    PDF when ``/Rotate=90``. Cropping the wrong axis would keep the
    customer-name strip instead of the QR — observed bug 2026-05-24.

    Implementation note: we don't extract the QR raster — we just shift
    the page's mediabox + cropbox to the right-visible slice in the
    appropriate axis based on the page's ``/Rotate`` value. The content
    stream is untouched, so the QR bitmap stays pixel-perfect (no
    resample, no quality loss).

    Returns ``b""`` on any failure — caller decides whether to fall back
    to the full label or surface an error.
    """
    try:
        from pypdf import PdfReader, PdfWriter
        from pypdf.generic import RectangleObject
    except ImportError:
        print("[fbs_data] pypdf not installed — QR crop unavailable")
        return b""

    # Keep the rightmost 42% of the *visible* page. Tweak this if Uzum
    # changes the layout — e.g. wider QR column.
    KEEP_FRACTION = 0.42

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        writer = PdfWriter()
        for page in reader.pages:
            rotate = int(page.get("/Rotate", 0) or 0) % 360
            mb = page.mediabox
            x_lo, y_lo = float(mb[0]), float(mb[1])
            x_hi, y_hi = float(mb[2]), float(mb[3])
            w = x_hi - x_lo
            h = y_hi - y_lo

            # Map visible-right → which underlying PDF edge to keep.
            if rotate == 90:
                # 90° CW display: visible right = PDF top (high y).
                box = RectangleObject((x_lo, y_hi - h * KEEP_FRACTION, x_hi, y_hi))
            elif rotate == 270:
                # 270° CW (= 90° CCW) display: visible right = PDF bottom.
                box = RectangleObject((x_lo, y_lo, x_hi, y_lo + h * KEEP_FRACTION))
            elif rotate == 180:
                # 180° display flips L↔R: visible right = PDF left.
                box = RectangleObject((x_lo, y_lo, x_lo + w * KEEP_FRACTION, y_hi))
            else:
                # Unrotated landscape (rotate=0): keep PDF right slice.
                box = RectangleObject((x_hi - w * KEEP_FRACTION, y_lo, x_hi, y_hi))

            page.mediabox = box
            page.cropbox = box
            writer.add_page(page)
        out = io.BytesIO()
        writer.write(out)
        return out.getvalue()
    except Exception as e:
        print(f"[fbs_data] QR crop failed: {e!r}")
        return b""


# Uzum's LARGE FBS label dimensions — 685×472pt landscape ≈ 242×166mm.
# We use these for the enlarged-label renderer so the output matches
# Uzum's standard label size (couriers / drop-off staff are used to it).
_UZUM_LABEL_W_PT = 685
_UZUM_LABEL_H_PT = 472


def render_enlarged_label_pdf(uzum_pdf_bytes: bytes,
                               order_row: dict | None = None) -> bytes:
    """Render a re-styled shipping label with BIGGER left-side text.

    The original Uzum label has small customer info (name, ID, city, SKU,
    quantity) that drop-off staff find hard to read at a glance. This
    function:

    1. Parses the original Uzum PDF for the data fields (uses the
       extended ``parse_label_pdf``).
    2. Renders a NEW 685×472pt landscape PDF with the same logical
       layout but much bigger fonts on the left half. The right half
       (QR + delivery date + tracking number) is also redrawn from the
       parsed data, with a fresh QR encoding the tracking number — same
       content as Uzum's QR, so couriers / scanners still recognise it.

    Falls back to ``order_row`` (DB row) for SKU/qty when the parser
    can't extract them. Returns ``b""`` on failure.

    Design notes:
      * We render everything ourselves rather than overlaying on Uzum's
        PDF — Uzum's underlying PDF is a 472×685 portrait with
        ``/Rotate 90``, which makes pypdf overlay coordinates fragile
        across viewers. A full re-render is simpler and more robust.
      * Page size stays at Uzum's standard 685×472pt landscape so the
        thermal-printer / paper layout sellers already use keeps working.
      * No external barcode library — the on-label tracking number is
        also encoded in the right-side QR, which is the canonical
        scannable. The visual "barcode bars" from Uzum's label are
        omitted; sellers / couriers scan the QR.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
        import qrcode
    except ImportError:
        print("[fbs_data] PIL / qrcode missing — enlarged label unavailable")
        return b""

    # Parse the source label for display fields. Fall back to order_row
    # for whatever the parser missed.
    try:
        from core.fbs_label_parser import parse_label_pdf
        parsed = parse_label_pdf(uzum_pdf_bytes) or {}
    except Exception as e:
        print(f"[fbs_data] enlarged-label parse failed: {e!r}")
        parsed = {}

    row = order_row or {}
    items = row.get("items") or row.get("items_json") or []
    first_item = (items[0] if items else {}) if isinstance(items, list) else {}

    customer = parsed.get("customer_fullname") or row.get("customer_fullname") or ""
    city = parsed.get("delivery_address") or row.get("delivery_address") or ""
    dropoff_id = parsed.get("dropoff_id") or str(row.get("order_id") or "")
    delivery_date = parsed.get("delivery_date") or ""
    sku = parsed.get("sku") or str(first_item.get("skuTitle") or "")
    qty = parsed.get("quantity") or ""
    if not qty and isinstance(items, list):
        try:
            qty = str(sum(int(it.get("amount") or 0) for it in items))
        except Exception:
            qty = ""
    tracking = parsed.get("tracking_number") or str(row.get("order_id") or "")
    # Tracking with no space: used as QR payload (matches Uzum's encoding).
    tracking_digits = tracking.replace(" ", "")

    # Render at 4× scale for crisp text on thermal printers (300+ DPI
    # equivalent). PIL→PDF saves at resolution=72*SCALE so the final
    # PDF page is exactly 685×472pt regardless of pixel count.
    SCALE = 4
    W_px = _UZUM_LABEL_W_PT * SCALE
    H_px = _UZUM_LABEL_H_PT * SCALE

    def _f(pt: float, bold: bool = False):
        path = (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            if bold else
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        )
        try:
            return ImageFont.truetype(path, max(8, int(round(pt * SCALE))))
        except Exception:
            return ImageFont.load_default()

    try:
        page = Image.new("RGB", (W_px, H_px), "white")
        draw = ImageDraw.Draw(page)

        # Layout columns: left half ~55%, right half ~45%.
        LEFT_W = int(W_px * 0.55)
        PADDING = int(8 * _MM_TO_PT * SCALE)  # 8mm inner padding
        x_left = PADDING
        y_cursor = PADDING

        # ── Logo zone: "ⓤ FBS" — simple text badge, no SVG ────────
        f_logo = _f(20, bold=True)
        draw.text((x_left, y_cursor), "ⓤ  FBS", fill="black", font=f_logo)
        y_cursor += int(28 * _MM_TO_PT * SCALE * 0.4)  # ~11mm spacing

        # ── Customer name: BIG ─────────────────────────────────────
        f_name = _f(40, bold=True)
        # Truncate if too long for the left column.
        max_text_w = LEFT_W - PADDING * 2
        name_text = customer or "—"
        while draw.textbbox((0, 0), name_text, font=f_name)[2] > max_text_w and len(name_text) > 4:
            name_text = name_text[:-1]
        if name_text != (customer or "—"):
            name_text = name_text.rstrip() + "…"
        draw.text((x_left, y_cursor), name_text, fill="black", font=f_name)
        ascent_name, descent_name = f_name.getmetrics()
        y_cursor += ascent_name + descent_name + int(3 * _MM_TO_PT * SCALE)

        # ── Drop-off ID ────────────────────────────────────────────
        f_id = _f(20, bold=True)
        id_text = f"ID: {dropoff_id}" if dropoff_id else "ID: —"
        draw.text((x_left, y_cursor), id_text, fill="black", font=f_id)
        ascent_id, descent_id = f_id.getmetrics()
        y_cursor += ascent_id + descent_id + int(2 * _MM_TO_PT * SCALE)

        # ── City (BIG, accent color) ────────────────────────────────
        f_city = _f(28, bold=True)
        city_text = city or "—"
        # Uzum uses an orange-ish accent for city; #ec5b1f matches their UI.
        draw.text((x_left, y_cursor), city_text, fill="#ec5b1f", font=f_city)
        ascent_city, descent_city = f_city.getmetrics()
        y_cursor += ascent_city + descent_city + int(8 * _MM_TO_PT * SCALE)

        # ── SKU + quantity ──────────────────────────────────────────
        # Push these to the bottom-left so they don't crowd the customer
        # info above. Use a fixed y position based on page height.
        bottom_y = H_px - PADDING - int(30 * _MM_TO_PT * SCALE)
        f_sku = _f(36, bold=True)
        sku_text = sku or "—"
        # Truncate SKU if wider than ~70% of the left column.
        sku_max_w = int(LEFT_W * 0.7) - PADDING
        while draw.textbbox((0, 0), sku_text, font=f_sku)[2] > sku_max_w and len(sku_text) > 4:
            sku_text = sku_text[:-1]
        draw.text((x_left, bottom_y), sku_text, fill="black", font=f_sku)
        # Quantity in a filled black circle on the right side of SKU.
        if qty:
            f_qty = _f(28, bold=True)
            circle_r = int(11 * _MM_TO_PT * SCALE)  # ~11mm radius
            circle_cx = x_left + int(LEFT_W * 0.78)
            circle_cy = bottom_y + int(7 * _MM_TO_PT * SCALE)
            draw.ellipse(
                [circle_cx - circle_r, circle_cy - circle_r,
                 circle_cx + circle_r, circle_cy + circle_r],
                fill="black",
            )
            qbb = draw.textbbox((0, 0), qty, font=f_qty)
            qw = qbb[2] - qbb[0]
            qh = qbb[3] - qbb[1]
            draw.text(
                (circle_cx - qw // 2, circle_cy - qh // 2 - qbb[1]),
                qty, fill="white", font=f_qty,
            )

        # ── RIGHT HALF: delivery date + QR + tracking ───────────────
        x_right = LEFT_W + int(2 * _MM_TO_PT * SCALE)
        right_w = W_px - x_right - PADDING
        right_cx = x_right + right_w // 2

        # Delivery date at top-right ("Д:DD.MM.YY")
        if delivery_date:
            f_date = _f(32, bold=True)
            date_str = f"Д:{delivery_date}"
            dbb = draw.textbbox((0, 0), date_str, font=f_date)
            dw = dbb[2] - dbb[0]
            draw.text(
                (right_cx - dw // 2, PADDING),
                date_str, fill="black", font=f_date,
            )

        # QR code in the middle of the right half.
        qr_content = tracking_digits or dropoff_id or "—"
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=10, border=1,
        )
        qr.add_data(qr_content)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        qr_target = int(70 * _MM_TO_PT * SCALE)  # 70mm square — bigger than Uzum's
        qr_img = qr_img.resize((qr_target, qr_target), Image.NEAREST)
        qr_y = PADDING + int(20 * _MM_TO_PT * SCALE)
        page.paste(qr_img, (right_cx - qr_target // 2, qr_y))

        # Tracking number text under the QR. Last 4 digits highlighted
        # by a filled black rectangle with white text — matches Uzum's
        # visual treatment so couriers immediately spot the scan-target.
        if tracking:
            f_track = _f(28, bold=True)
            track_y = qr_y + qr_target + int(3 * _MM_TO_PT * SCALE)
            # Split into head + last4 for the highlight.
            digits = tracking.replace(" ", "")
            if len(digits) > 4:
                head = digits[:-4]
                # Re-insert a single space before the last 4 (matches
                # Uzum's "10811 7292" formatting).
                head_disp = head[:5] + " " + head[5:] if len(head) > 5 else head
                tail = digits[-4:]
            else:
                head_disp = ""
                tail = digits
            head_bb = draw.textbbox((0, 0), head_disp, font=f_track)
            tail_bb = draw.textbbox((0, 0), tail, font=f_track)
            head_w = head_bb[2] - head_bb[0]
            tail_w = tail_bb[2] - tail_bb[0]
            tail_h = tail_bb[3] - tail_bb[1]
            gap = int(2 * _MM_TO_PT * SCALE)
            total_w = head_w + gap + tail_w + int(4 * _MM_TO_PT * SCALE)  # rect padding
            tx = right_cx - total_w // 2
            if head_disp:
                draw.text((tx, track_y), head_disp, fill="black", font=f_track)
                tx += head_w + gap
            # Black rect behind the last 4 digits.
            rect_pad = int(2 * _MM_TO_PT * SCALE)
            draw.rectangle(
                [tx - rect_pad, track_y - rect_pad,
                 tx + tail_w + rect_pad, track_y + tail_h + rect_pad],
                fill="black",
            )
            draw.text((tx, track_y), tail, fill="white", font=f_track)

        # Save as PDF — resolution=72*SCALE produces the exact 685×472pt
        # page size regardless of pixel count.
        buf = io.BytesIO()
        page.save(buf, format="PDF", resolution=72 * SCALE)
        return buf.getvalue()
    except Exception as e:
        print(f"[fbs_data] enlarged label render failed: {e!r}")
        return b""


def merge_label_pdfs(pdf_bytes_list: list[bytes]) -> bytes:
    """Concatenate multiple label PDFs into one downloadable file.

    Uses pypdf. Each input is one order's label (which itself may be
    multi-page for multi-package orders) — we keep every page so the
    seller doesn't lose a single label in the merge.

    Empty/None entries are skipped silently. If the whole input list is
    empty (or pypdf fails for every entry), returns ``b""`` rather than
    raising — the route layer turns an empty body into a 502.
    """
    try:
        from pypdf import PdfWriter, PdfReader  # lazy
    except ImportError:
        print("[fbs_data] pypdf not installed — bulk label merge unavailable")
        return b""

    writer = PdfWriter()
    appended = 0
    for raw in pdf_bytes_list:
        if not raw:
            continue
        try:
            reader = PdfReader(io.BytesIO(raw))
            for page in reader.pages:
                writer.add_page(page)
            appended += 1
        except Exception as e:
            print(f"[fbs_data] merge: skipped 1 PDF ({len(raw)} bytes): {e!r}")
            continue

    if appended == 0:
        return b""

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def get_return_reasons(token: str, *, fail_fast: bool = False) -> tuple[list[dict], str]:
    """List of valid cancel-reason enum entries. SWR cached globally
    because the answer doesn't depend on the user — whichever request
    warms the cache feeds all subsequent ones."""
    key = "fbs:return-reasons"

    def _load():
        reasons, used_url = fetch_fbs_return_reasons(token, fail_fast=fail_fast)
        return {"reasons": reasons, "used_url": used_url}

    cached = swr_get(
        key,
        soft_ttl=_RETURN_REASONS_SOFT_TTL,
        hard_ttl=_RETURN_REASONS_HARD_TTL,
        loader=_load,
    )
    return (cached["reasons"], cached["used_url"])


# ─────────────────────────────────────────────────────────────────────
# Stage 3 DBS action wrappers — call Uzum, then invalidate the per-shop
# cache so /fbs and the count chips reflect the new status on next load.
# ─────────────────────────────────────────────────────────────────────


def dbs_delivering(
    token: str,
    order_id: str | int,
    shop_uzum_id: str | int,
    *,
    fail_fast: bool = False,
) -> tuple[dict, str]:
    """Seller marks the DBS order as picked up (PACKING → DELIVERING)."""
    order, used_url = mark_dbs_delivering(token, order_id, fail_fast=fail_fast)
    _apply_action_response_to_db(
        shop_uzum_id, order, token=token, order_id=order_id,
        fail_fast=fail_fast,
    )
    invalidate_fbs_cache(shop_uzum_id)
    return (order, used_url)


def dbs_completed(
    token: str,
    order_id: str | int,
    shop_uzum_id: str | int,
    *,
    issue_code: int | None = None,
    fail_fast: bool = False,
) -> tuple[dict, str]:
    """Seller marks the DBS order as delivered (DELIVERING → COMPLETED)."""
    order, used_url = mark_dbs_completed(
        token, order_id, issue_code=issue_code, fail_fast=fail_fast,
    )
    _apply_action_response_to_db(
        shop_uzum_id, order, token=token, order_id=order_id,
        fail_fast=fail_fast,
    )
    invalidate_fbs_cache(shop_uzum_id)
    return (order, used_url)


def dbs_refund(
    token: str,
    order_id: str | int,
    shop_uzum_id: str | int,
    *,
    fail_fast: bool = False,
) -> tuple[dict, str]:
    """Open a refund flow on a completed DBS order. Single-button —
    Uzum doesn't ask for items or reasons here."""
    body, used_url = refund_dbs_order(token, order_id, fail_fast=fail_fast)
    _apply_action_response_to_db(
        shop_uzum_id, body, token=token, order_id=order_id,
        fail_fast=fail_fast,
    )
    invalidate_fbs_cache(shop_uzum_id)
    return (body, used_url)


__all__ = [
    "get_fbs_orders",
    "get_fbs_orders_for_shops",
    "get_fbs_count",
    "get_fbs_counts_for_shops",
    "get_fbs_order_detail",
    "get_last_synced_at",
    "get_last_synced_at_for_shops",
    "invalidate_fbs_cache",
    # Stage 2 action seams
    "confirm_order",
    "cancel_order",
    "attach_identifiers",
    "get_label_pdfs",
    "get_return_reasons",
    # Stage 3 DBS action seams
    "dbs_delivering",
    "dbs_completed",
    "dbs_refund",
    "UzumAPIError",
    "FBS_ORDER_STATUSES",
    "FBS_ORDER_SCHEMES",
]
