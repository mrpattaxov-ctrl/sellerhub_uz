"""Shared FBS sync helpers used by both the background worker and the
on-demand refresh path.

This module owns the *machinery* of turning Uzum responses into
``FbsOrder`` rows (and back). The two consumers split as follows:

  * ``app.py`` background loop — calls :func:`fetch_all_pages` +
    :func:`replace_orders` for every shop, every 10 min.
  * ``core/fbs_data.py`` read path — calls the same helpers when the
    user passes ``?refresh=1`` to a list / count endpoint. Just-in-time
    sync for a single ``(shop, status)`` pair so the seller can force
    a fresh view without waiting up to 10 min for the worker tick.

Keeping this layer thin (no Flask, no Redis, no SWR) means the worker
and the request path can't drift out of sync — every order projection
goes through the same code.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, select, func
from sqlalchemy.dialects.postgresql import insert as pg_insert

from extensions import SessionLocal
from models import FbsOrder
from core.uzum_openapi import (
    fetch_fbs_orders_page,
    extract_fbs_orders_list,
    FBS_ORDER_STATUSES,
)


# Uzum's /v2/fbs/orders caps page size at 50; trying to ask for more
# would 400. Matches the constant inlined in the worker before this
# module existed.
_FBS_PAGE_SIZE = 50

# Postgres caps a single prepared statement at 65535 bound parameters
# (the ``uint16`` field in the wire protocol). ``FbsOrder`` projects 28
# values per row, so a naive bulk UPSERT would already trip at ~2340
# rows. A fresh shop with full history easily blows past that across
# 11 statuses. 1000 keeps each statement well under the wire-protocol
# limit and keeps Postgres's per-statement plan time reasonable.
_UPSERT_CHUNK_SIZE = 1000


# ── Sync cadence: which statuses to refresh on each worker tick ──────
#
# Bosqich A.10. Uzum's per-token OpenAPI quota is the binding constraint
# behind the 429s: every /v2/fbs/orders call (one per status, all shops
# batched) spends one quota unit, and the count endpoint can't return
# more than one status per call. Re-fetching all 11 statuses every
# 10-min tick burns ~66 calls/hour/token — most of it on statuses that
# don't change (the seller already acted, or the order reached a
# terminal state). That volume is what pushed the token over its quota
# and tripped 429.
#
# So the 11 split into two cadences (see :func:`fbs_statuses_for_tick`):
#
#   * ACTIVE — the seller's action queue + the immediate next hop.
#     Refreshed EVERY tick. CREATED/PACKING/PENDING_DELIVERY are where
#     the seller acts; DELIVERING is included so an order leaving the
#     queue flips out of PENDING_DELIVERY within one tick (keeps the
#     active-queue counts honest). Mirrors ``_ACTIVE_STATUSES`` /
#     ``_FBS_REFRESH_ON_PRESS_SYNC`` in fbs/routes.py, which already
#     decided these are the only statuses needing live freshness.
#   * SLOW — everything else (in-transit tail + terminal). Refreshed
#     only every Nth tick (≈ hourly at the default 10-min interval).
#     These rarely change and the seller doesn't watch them in real
#     time; an hour of staleness on a COMPLETED count is invisible, and
#     terminal orders never change once they land — we only need to
#     *catch* the transition, not poll it.
#
# Net: a steady tick drops to 4 calls (≈ -64%), and per-hour token load
# drops ~66 → ~30, pulling the worker back from the quota wall. Counts
# the seller doesn't act on lag up to ~1h, which matches what the press
# path already does (it never JIT-refreshes them either).
#
# Bosqich A.11 — PENDING_CANCELLATION ("Bekor qilinmoqda") is deliberately
# NOT in this list. It's a transient Uzum-internal state the seller can't
# act on; syncing it spent a quota unit on every full sweep for a chip
# nobody used. Dropping it here means Uzum is never polled for it (not even
# hourly). The chip is hidden too (FBS_HIDDEN_CHIP_STATUSES in
# fbs/routes.py). The canonical FBS_ORDER_STATUSES enum still lists it, so
# any legacy PENDING_CANCELLATION row already in the DB still renders in
# the detail/list view — we just stop fetching new ones.
FBS_ALL_SYNC_STATUSES = (
    "CREATED",
    "PACKING",
    "PENDING_DELIVERY",
    "DELIVERING",
    "DELIVERED",
    "ACCEPTED_AT_DP",
    "DELIVERED_TO_CUSTOMER_DELIVERY_POINT",
    "COMPLETED",
    "CANCELED",
    "RETURNED",
)

FBS_ACTIVE_SYNC_STATUSES = (
    "CREATED",
    "PACKING",
    "PENDING_DELIVERY",
    "DELIVERING",
)

# Derived, not hand-listed, so the two can never silently drift apart:
# a status added to ALL but not ACTIVE automatically becomes SLOW (it
# can never be dropped from the worker entirely by a careless edit).
FBS_SLOW_SYNC_STATUSES = tuple(
    s for s in FBS_ALL_SYNC_STATUSES if s not in FBS_ACTIVE_SYNC_STATUSES
)


def fbs_statuses_for_tick(tick_index: int, slow_every_n: int) -> tuple[str, ...]:
    """Which FBS statuses the worker should sync on tick ``tick_index``.

    * Tick 0 (the first tick after a process start, so a restart
      re-backfills the full set) and every ``slow_every_n``-th tick
      thereafter return the full :data:`FBS_ALL_SYNC_STATUSES`.
    * Every other tick returns just :data:`FBS_ACTIVE_SYNC_STATUSES`.

    ``slow_every_n <= 1`` disables the split (every tick is a full
    sweep) — the natural behaviour when the tick interval already meets
    or exceeds the slow cadence (e.g. an hourly worker needs no
    sub-cadence). A non-positive value is treated the same, defensively,
    so a misconfiguration can never produce an empty/partial sweep.

    Pure function: no DB, no clock, no globals. The caller owns the
    monotonically-increasing ``tick_index``, keeping this deterministic
    and trivially testable.
    """
    if slow_every_n <= 1:
        return FBS_ALL_SYNC_STATUSES
    if tick_index % slow_every_n == 0:
        return FBS_ALL_SYNC_STATUSES
    return FBS_ACTIVE_SYNC_STATUSES


# ── Per-status sync cadence (Abdulaziz 2026-06-16) ───────────────────
# The old two-tier model (active-every-tick / slow-every-Nth) is replaced
# by an explicit PER-STATUS interval in MINUTES. The worker now wakes on a
# fine heartbeat (≈1 min) and syncs each status only when its own interval
# has elapsed — so a status can be tuned independently without touching the
# others. A status ABSENT from this map (or mapped to a non-positive value)
# is NEVER background-synced:
#   * PENDING_DELIVERY — shown only via the live "Поставка"/накладные view.
#   * PENDING_CANCELLATION — transient; never listed.
# These minutes are honoured exactly only if the worker heartbeat divides
# them (run the loop every 60s); a coarser heartbeat rounds up to the next
# wake-up. Override any value via FBS_STATUS_SYNC_INTERVAL_MIN_<STATUS> at
# the call site if a deployment needs to retune without a code edit.
FBS_STATUS_SYNC_INTERVAL_MIN: dict[str, int] = {
    "CREATED": 23,
    "PACKING": 23,
    "DELIVERING": 63,
    "DELIVERED": 57,
    "ACCEPTED_AT_DP": 57,
    "DELIVERED_TO_CUSTOMER_DELIVERY_POINT": 57,
    "COMPLETED": 63,
    "CANCELED": 63,
    "RETURNED": 63,
    # PENDING_DELIVERY / PENDING_CANCELLATION omitted on purpose → never.
}


def fbs_statuses_due(
    now_ts: float,
    last_sync_ts: dict[str, float],
    *,
    intervals: dict[str, int] | None = None,
    first_run: bool = False,
) -> tuple[str, ...]:
    """Which statuses the worker should sync on this heartbeat.

    ``now_ts`` / ``last_sync_ts`` are epoch seconds (``time.time()``); the
    dict maps ``status -> last successful sync ts``. A status whose interval
    has elapsed since its last sync — or that has never been synced — is due.

    ``first_run=True`` forces every CONFIGURED status due, so a (re)start
    re-backfills the full set in one pass (the old "tick 0 = full sweep").

    Pure function: no DB, no clock, no globals — the caller owns the clock
    and the ``last_sync_ts`` book-keeping, keeping this deterministic and
    trivially testable. Result follows :data:`FBS_ALL_SYNC_STATUSES` order.
    """
    intervals = intervals if intervals is not None else FBS_STATUS_SYNC_INTERVAL_MIN
    due: set[str] = set()
    for status, interval_min in intervals.items():
        if not interval_min or interval_min <= 0:
            continue  # absent / non-positive → never synced
        if first_run:
            due.add(status)
            continue
        last = last_sync_ts.get(status)
        if last is None or (now_ts - last) >= interval_min * 60:
            due.add(status)
    return tuple(s for s in FBS_ALL_SYNC_STATUSES if s in due)


def parse_iso_naive_utc(s):
    """Parse Uzum's order timestamps into naive UTC datetimes.

    Two shapes have been observed in the wild on /v2/fbs/orders and
    /v1/fbs/order/{id}:

      * ISO 8601 string with trailing ``Z`` — e.g. ``"2026-05-21T13:43:36.190Z"``
      * Integer epoch milliseconds — e.g. ``1779460587795``

    Both come from Uzum's ``$date-time`` field (swagger calls it
    ISO 8601 but the actual JSON wire value is the int64 ms count for
    every date field on the FBS order endpoints). We accept either.
    Floats are tolerated too (some response coercions cast through
    ``Number``); they're interpreted as ms.

    Returns ``None`` for ``None`` / empty / unparseable input rather
    than raising — one bad timestamp shouldn't drop the whole batch.
    """
    if s is None:
        return None
    if isinstance(s, bool):
        # bool is a subclass of int — guard before the int branch.
        return None
    if isinstance(s, (int, float)):
        try:
            return datetime.utcfromtimestamp(s / 1000.0)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        text_in = str(s).strip()
        if not text_in:
            return None
        if text_in.endswith("Z"):
            text_in = text_in[:-1] + "+00:00"
        dt = datetime.fromisoformat(text_in)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except (TypeError, ValueError):
        return None


def _iso_z(dt) -> str | None:
    """Inverse of :func:`parse_iso_naive_utc` — naive-UTC → ``...Z``.

    Used by :func:`row_to_dict` so the dict returned to the route layer
    is byte-for-byte the same shape Uzum would have sent. Frontend
    parses these with ``new Date(...)`` and the trailing ``Z`` is what
    makes the result a UTC datetime instead of a local-time one.
    """
    if dt is None:
        return None
    return dt.isoformat(timespec="milliseconds") + "Z"


def _all_orders_known_in_status(shop_uzum_id, status: str, orders: list[dict]) -> bool:
    """True iff every ``order_id`` in ``orders`` is already in
    ``fbs_orders`` with the same ``status``.

    The incremental-sync stop signal. After Uzum returns a page, if all
    50 IDs match rows we already have (same status), there's no point
    paginating further — the orders on later pages are older and even
    more deeply known. Catches new orders at the top of page 0 and
    status flips on existing orders (the flipped status's page 0 will
    show the order as "not known in this new status").

    ``shop_uzum_id`` is kept in the signature for backward compatibility
    with the single-shop sync path, but the SQL doesn't filter on it:
    Uzum's ``order_id`` is globally unique across shops, so matching
    by (order_id, status) is equivalent and lets a token-batched call
    that mixes multiple shops use the same stop check.

    One indexed SELECT per page — cheap, sub-millisecond.
    """
    if not orders:
        return True
    order_ids = [str(o.get("id")) for o in orders if o.get("id")]
    if not order_ids:
        return True
    with SessionLocal() as db:
        n = int(db.execute(
            select(func.count()).select_from(FbsOrder).where(
                FbsOrder.status == status,
                FbsOrder.order_id.in_(order_ids),
            )
        ).scalar() or 0)
    return n == len(order_ids)


def fetch_all_pages(token: str, shop_uzum_id, *,
                    status: str, date_from_ms: int | None = None,
                    stop_on_known: bool = False,
                    fail_fast: bool = False) -> list[dict]:
    """Drain the /v2/fbs/orders paginator for a single (shop, status).

    Stops on the first short page (< _FBS_PAGE_SIZE) or once 50 pages
    have been pulled (a safety cap — 2500 orders for one status is a
    runaway signal, not normal). Caller usually wraps this in a
    try/except so one slow status doesn't block the whole sync.

    ``stop_on_known`` (Bosqich 4g — incremental sync): when True,
    additionally break out after any page whose orders are *all*
    already in ``fbs_orders`` with the same status. First-tick backfill
    still fetches everything because the DB is empty; subsequent ticks
    typically stop after the first page once steady-state.

    Bosqich A.6 — every page fetch is paced through
    :func:`core.fbs_locks.pace_uzum_call`, which enforces ``>= 1s``
    between consecutive Uzum ``/orders`` calls on the token across *all*
    callers in the process (the background worker and the JIT "Yangilash"
    refresh share the same gate, interleaving 1s apart instead of one
    skipping). This replaced the old ``inter_page_sleep`` parameter, which
    only spaced pages *within* one status and did nothing against the
    refresh path racing the worker across status boundaries.

    ``fail_fast`` (Bosqich A.9 / #5): forwarded to each page fetch. The
    background worker (app.py) keeps the default False → patient
    60/120/180s retry on a flaky 429/5xx. The interactive JIT refresh
    callers in ``core/fbs_data.py`` pass True so a 429 fails in ~2-3s
    instead of pinning the request's worker thread for ~6 minutes.
    """
    # Note on Uzum's ``totalAmount`` field: despite the name (and the
    # swagger description), observation on 2026-05-22 confirmed it
    # equals the page-size returned, NOT the total dataset across all
    # pages. Pagination drives off the short-page / empty-page signal,
    # never off totalAmount.
    all_orders: list[dict] = []
    page = 0
    MAX_PAGES = 50
    while page < MAX_PAGES:
        # Bosqich A.11 — pacing is handled centrally by the shared per-token
        # bucket inside _fbs_orders_request_with_auth, so the worker and a
        # live Yangilash are serialised cross-process without a gate here.
        body, _ = fetch_fbs_orders_page(
            token, shop_uzum_id,
            status=status, page=page, size=_FBS_PAGE_SIZE,
            date_from_ms=date_from_ms,
            fail_fast=fail_fast,
        )
        orders, _ = extract_fbs_orders_list(body)
        if not orders:
            break
        all_orders.extend(orders)
        if len(orders) < _FBS_PAGE_SIZE:
            break
        if stop_on_known and _all_orders_known_in_status(shop_uzum_id, status, orders):
            break
        page += 1
    return all_orders


def dict_from_order(shop_uzum_id, o: dict) -> dict:
    """Uzum order dict → flat dict of ``FbsOrder`` column values.

    Used by :func:`upsert_orders` for bulk INSERT ... ON CONFLICT DO
    UPDATE — bulk insert can't take ORM instances, it needs plain dicts
    that match the column names.

    Mirror of :func:`row_from_order` (which returns an ORM object). The
    field projection is identical; keep them in sync if either side
    grows new columns.

    Bosqich 4l — token-batched sync: when the worker fetches multiple
    shops in one Uzum call (``shopIds=1&shopIds=2&...``), each returned
    order carries its own ``shopId``. We honour the per-order value so
    rows land under the correct shop. The ``shop_uzum_id`` parameter
    stays as a fallback for old code paths and for the rare case Uzum
    omits ``shopId`` from the order body. CRITICAL: getting this wrong
    would cross-contaminate a user's shops — order from shop A landing
    in shop B's view — so the param is the *fallback*, not the override.
    """
    di = o.get("deliveryInfo") or {}
    stock = o.get("stock") or {}
    drop = o.get("dropOffPoint") or {}
    # Per-order shopId wins. Fall back to the caller-supplied default
    # only when Uzum omits it (legacy single-shop sync path).
    row_shop_id = o.get("shopId")
    if row_shop_id is None or str(row_shop_id).strip() == "":
        row_shop_id = shop_uzum_id
    return {
        "shop_id": str(row_shop_id),
        "order_id": str(o.get("id")),
        # Uzum returns "FBS" or "DBS" in `scheme`. Default to FBS when
        # absent so the CHECK constraint always passes (old responses
        # sometimes omit it for FBS orders).
        "order_type": str(o.get("scheme") or "FBS"),
        "status": str(o.get("status") or "CREATED"),
        "customer_fullname": di.get("customerFullname"),
        "customer_phone": di.get("customerPhone"),
        "delivery_address": di.get("deliveryAddress"),
        "delivery_comment": di.get("deliveryComment"),
        "price": int(o.get("price") or 0),
        "date_created": parse_iso_naive_utc(o.get("dateCreated")),
        "accept_until": parse_iso_naive_utc(o.get("acceptUntil")),
        "deliver_until": parse_iso_naive_utc(o.get("deliverUntil")),
        "accepted_date": parse_iso_naive_utc(o.get("acceptedDate")),
        "delivering_date": parse_iso_naive_utc(o.get("deliveringDate")),
        "delivery_date": parse_iso_naive_utc(o.get("deliveryDate")),
        "delivered_to_dp_date": parse_iso_naive_utc(o.get("deliveredToDeliveryPointDate")),
        "completed_date": parse_iso_naive_utc(o.get("completedDate")),
        "cancelled_date": parse_iso_naive_utc(o.get("dateCancelled")),
        "return_date": parse_iso_naive_utc(o.get("returnDate")),
        "cancel_reason": o.get("cancelReason"),
        "identifier_required": bool(o.get("identifierRequired") or False),
        "stock_id": (str(stock.get("id")) if stock.get("id") is not None else None),
        "stock_title": stock.get("title"),
        "drop_off_point_uuid": drop.get("uuid"),
        "drop_off_point_address": drop.get("address"),
        "invoice_number": (str(o.get("invoiceNumber")) if o.get("invoiceNumber") else None),
        "items_json": o.get("orderItems") or [],
        "raw_json": o,
        "synced_at": datetime.utcnow(),
    }


def row_from_order(shop_uzum_id, o: dict) -> FbsOrder:
    """Uzum order dict → ``FbsOrder`` row.

    Unknown / future fields stay in ``raw_json``; the projected columns
    are only the ones the UI cares about today. Bumping the column set
    means: add the column in models.py + migration, then update
    :func:`dict_from_order`.
    """
    return FbsOrder(**dict_from_order(shop_uzum_id, o))


def row_to_dict(row: FbsOrder, *, include_raw: bool = True) -> dict:
    """``FbsOrder`` row → Uzum-shaped dict the templates already render.

    Routes layer doesn't care whether the data came from Uzum or DB —
    this function bridges the gap. Field names + nesting match the
    response shape of ``/v2/fbs/orders`` and ``/v1/fbs/order/{id}`` so
    ``templates/fbs_orders.html`` and ``fbs_order_detail.html`` need
    zero changes.

    ``raw_json`` is the source of truth when we ever need a field that
    doesn't have its own column — it preserves the original response
    verbatim. The explicit projection here only covers the fields the
    UI uses today.

    ``include_raw`` (Bosqich A.5 #2): when False, the trailing
    ``raw_json`` forward-compat merge is skipped. The list views
    (``renderOrders`` in ``fbs_orders.html``) read only the explicit
    projection above plus ``orderItems`` — never a raw_json-only field —
    so dropping the merge lets the caller also ``defer(FbsOrder.raw_json)``
    in the query and avoid loading/shipping the full original Uzum
    payload (which duplicates orderItems + every other field) for every
    row in the list. Detail views keep the default (True) so a field we
    haven't projected yet is still surfaced.
    """
    d = {
        "id": row.order_id,
        "status": row.status,
        "scheme": row.order_type,
        "shopId": row.shop_id,
        "price": row.price,
        "identifierRequired": bool(row.identifier_required),
        "cancelReason": row.cancel_reason,
        "invoiceNumber": row.invoice_number,
        # Dates — matched against the same keys the templates dereference.
        "dateCreated": _iso_z(row.date_created),
        "acceptUntil": _iso_z(row.accept_until),
        "deliverUntil": _iso_z(row.deliver_until),
        "acceptedDate": _iso_z(row.accepted_date),
        "deliveringDate": _iso_z(row.delivering_date),
        "deliveryDate": _iso_z(row.delivery_date),
        "deliveredToDeliveryPointDate": _iso_z(row.delivered_to_dp_date),
        "completedDate": _iso_z(row.completed_date),
        "dateCancelled": _iso_z(row.cancelled_date),
        "returnDate": _iso_z(row.return_date),
        # Nested objects.
        "deliveryInfo": {
            "customerFullname": row.customer_fullname,
            "customerPhone": row.customer_phone,
            "deliveryAddress": row.delivery_address,
            "deliveryComment": row.delivery_comment,
        },
        "stock": {
            "id": row.stock_id,
            "title": row.stock_title,
        },
        "dropOffPoint": {
            "uuid": row.drop_off_point_uuid,
            "address": row.drop_off_point_address,
        },
        "orderItems": row.items_json or [],
    }
    # Surface everything else from raw_json so templates can pick up
    # fields we don't explicitly project yet. The explicit projection
    # above wins — we don't want raw_json to overwrite our coerced types
    # (e.g. parsed dates). Skipped entirely when ``include_raw`` is False
    # (list views), which also lets the query defer the column.
    if include_raw:
        raw = row.raw_json or {}
        for k, v in raw.items():
            if k not in (
                "id", "status", "scheme", "shopId", "price",
                "identifierRequired", "cancelReason", "invoiceNumber",
                "dateCreated", "acceptUntil", "deliverUntil",
                "acceptedDate", "deliveringDate", "deliveryDate",
                "deliveredToDeliveryPointDate", "completedDate",
                "dateCancelled", "returnDate",
                "deliveryInfo", "stock", "dropOffPoint", "orderItems",
            ):
                # Bosqich A.14 (HAR audit B1) — `publicId` (seller-facing
                # short order number, e.g. "856114-0035") rides through here
                # automatically; it has no column but lives in raw_json.
                d[k] = v
        # Bosqich A.14 (HAR audit B2) — `deliveryInfo` is excluded from the
        # merge above (explicit projection wins), so its DBS-only
        # `issueCodeRetriesLeft` ("kod uchun necha urinish qoldi") would be
        # lost. Surface it explicitly. None for FBS (no delivery code flow).
        # Read raw_json ONLY here (include_raw) — list views defer the column,
        # so touching it there would trigger an N+1 lazy load per row.
        raw_di = raw.get("deliveryInfo")
        if isinstance(raw_di, dict) and raw_di.get("issueCodeRetriesLeft") is not None:
            d["deliveryInfo"]["issueCodeRetriesLeft"] = raw_di["issueCodeRetriesLeft"]
    return d


def upsert_orders(db, shop_uzum_id, *, orders: list[dict]) -> int:
    """INSERT new + UPDATE existing by ``order_id`` — no DELETE.

    This is the Bosqich 4f write strategy: preserve historical orders
    in the DB indefinitely (subject to the cleanup loop) rather than
    wiping a (shop, status) bucket each tick. Status changes flow
    through as UPDATE of the ``status`` column on the existing row,
    keyed by Uzum's globally-unique ``order_id``.

    Returns the number of orders processed (insert + update count).
    PostgreSQL's ON CONFLICT path is one statement, so this is fast
    even for backfills of thousands of orders.

    Bad rows (unknown status, projection error) are logged and skipped;
    they don't fail the batch.
    """
    if not orders:
        return 0
    rows_data: list[dict] = []
    for o in orders:
        try:
            status = str(o.get("status") or "")
            if status not in FBS_ORDER_STATUSES:
                print(f"[FBS Sync] skipping order {o.get('id')!r} unknown status={status!r}")
                continue
            rows_data.append(dict_from_order(shop_uzum_id, o))
        except Exception as e:
            print(f"[FBS Sync] skip order {o.get('id')!r} project error: {e!r}")
    if not rows_data:
        return 0
    # Chunk to stay under Postgres's 65535-parameter wire-protocol cap.
    # All chunks run inside the caller's open transaction, so a crash
    # mid-batch rolls back the whole upsert — readers never see a
    # half-applied tick.
    total = 0
    for start in range(0, len(rows_data), _UPSERT_CHUNK_SIZE):
        chunk = rows_data[start:start + _UPSERT_CHUNK_SIZE]
        stmt = pg_insert(FbsOrder).values(chunk)
        # Update every column except the synthetic PK (`id`) and the
        # conflict target (`order_id`). `excluded.<col>` references the
        # candidate row from the failed INSERT, so this is "use the new
        # value Uzum just sent us" for every projected field. Rebuilt
        # per chunk because ``excluded`` is bound to the specific stmt.
        update_cols = {
            c.name: stmt.excluded[c.name]
            for c in FbsOrder.__table__.columns
            if c.name not in ("id", "order_id")
        }
        stmt = stmt.on_conflict_do_update(
            index_elements=["order_id"],
            set_=update_cols,
        )
        db.execute(stmt)
        total += len(chunk)
    return total


def replace_orders(db, shop_uzum_id, *, statuses, orders: list[dict]) -> int:
    """Idempotent per-(shop, statuses) DELETE+INSERT.

    Wipes ``fbs_orders`` rows for ``shop_uzum_id`` where ``status`` is
    in ``statuses``, then inserts the new set. Done in one DB
    transaction (the caller commits) so readers never see a half-
    replaced state. Returns the number of rows inserted.

    Rows whose ``status`` is outside the 11-value enum are skipped with
    a log line — Uzum has been seen to occasionally return junk in dev
    test data, and a single bad row shouldn't fail the whole batch.
    """
    db.execute(
        delete(FbsOrder)
        .where(FbsOrder.shop_id == str(shop_uzum_id))
        .where(FbsOrder.status.in_(list(statuses)))
    )
    if not orders:
        return 0
    rows: list[FbsOrder] = []
    for o in orders:
        try:
            status = str(o.get("status") or "")
            if status not in FBS_ORDER_STATUSES:
                print(f"[FBS Sync] skipping order {o.get('id')!r} unknown status={status!r}")
                continue
            rows.append(row_from_order(shop_uzum_id, o))
        except Exception as e:
            print(f"[FBS Sync] skip order {o.get('id')!r} project error: {e!r}")
    if not rows:
        return 0
    db.add_all(rows)
    return len(rows)


__all__ = [
    "parse_iso_naive_utc",
    "fetch_all_pages",
    "dict_from_order",
    "row_from_order",
    "row_to_dict",
    "upsert_orders",
    "replace_orders",
    "FBS_ALL_SYNC_STATUSES",
    "FBS_ACTIVE_SYNC_STATUSES",
    "FBS_SLOW_SYNC_STATUSES",
    "fbs_statuses_for_tick",
    "FBS_STATUS_SYNC_INTERVAL_MIN",
    "fbs_statuses_due",
]
