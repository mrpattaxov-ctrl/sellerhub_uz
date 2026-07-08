"""DB cache + paced fetch for FBS order shipping-label (jo'natma yorlig'i) PDFs.

Uzum's ``/v1/fbs/order/{id}/labels/print`` is a PER-ORDER call paced 1s/token
(burst → HTTP 429, ~3-4s recovery — see the akt-print rate-limit reference), so
bulk-printing 50 fresh labels costs ~50s. To keep bulk printing instant and
burst-free we cache each order's label PDF in the ``fbs_order_labels`` table and
pre-fetch them in the background sync worker.

This module is the SINGLE place that (a) reads/writes that cache and (b) fetches
a label live with a short 429-aware retry. It mirrors ``core.fbs_akt_cache``.

Key difference from the akt cache: the label is IMMUTABLE once the order is
confirmed (verified 2026-06-12 — it does NOT change when the накладна is created
or the drop-off point is edited), so there's no version stamp — a cached label
is never stale while the order is active. And the active set comes from our own
``fbs_orders`` DB table (already synced), so warming needs NO extra Uzum list
call (unlike the akt prefetch which walks the invoice list).
"""
from __future__ import annotations

import time as _time

from sqlalchemy import func, select, delete as _delete
from sqlalchemy.dialects.postgresql import insert as pg_insert

from extensions import SessionLocal
from models import FbsOrder, FbsOrderLabel
from core.uzum_openapi import download_fbs_label, UzumAPIError

# Only these statuses are printable / warmable. Labels for shipped+ orders are
# never reprinted, so they're pruned. FBS only — Uzum gives DBS no label.
WARM_STATUSES = ("PACKING", "PENDING_DELIVERY")


def get_cached_label(user_id, order_id, *, size="LARGE") -> bytes | None:
    """Return cached label PDF bytes for this order, or None on miss.

    Scoped by ``user_id`` so one seller can't read another's cached label by
    guessing an order id. ``size`` must match the cached entry (a BIG request
    never gets a LARGE cache hit).
    """
    with SessionLocal() as db:
        row = db.get(FbsOrderLabel, str(order_id))
        if row is None or int(row.user_id) != int(user_id):
            return None
        if str(row.size).upper() != str(size).upper():
            return None
        return bytes(row.pdf)


def store_label(user_id, order_id, size, pdf: bytes) -> None:
    """Upsert the label PDF for this order (INSERT … ON CONFLICT DO UPDATE)."""
    stmt = pg_insert(FbsOrderLabel).values(
        order_id=str(order_id),
        user_id=int(user_id),
        size=str(size).upper(),
        pdf=pdf,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[FbsOrderLabel.order_id],
        set_={
            "user_id": stmt.excluded.user_id,
            "size": stmt.excluded.size,
            "pdf": stmt.excluded.pdf,
            "synced_at": func.now(),
        },
    )
    with SessionLocal() as db:
        db.execute(stmt)
        db.commit()


def delete_label(order_id) -> None:
    """Drop the cached label for one order."""
    with SessionLocal() as db:
        row = db.get(FbsOrderLabel, str(order_id))
        if row is not None:
            db.delete(row)
            db.commit()


def fetch_label_live(token, order_id, *, size="LARGE", attempts: int = 4, wait: float = 4.0) -> bytes:
    """Fetch one order's label from Uzum with a SHORT 429-aware retry, returning
    the order's label PDF(s) merged into ONE document.

    ``fail_fast=True`` avoids the shared session's 60/120/180s backoff; on a 429
    we wait ``wait`` seconds (the print bucket refills in ~3-4s) and retry up to
    ``attempts`` times. ``download_fbs_label`` already paces through the per-token
    gate. Raises the last error if it never succeeds.
    """
    from core.fbs_data import merge_label_pdfs  # lazy — avoid import-time cycle

    last_exc = None
    for attempt in range(attempts):
        try:
            pdfs, _ = download_fbs_label(token, order_id, size=size, fail_fast=True)
            return merge_label_pdfs(pdfs) if len(pdfs) > 1 else (pdfs[0] if pdfs else b"")
        except UzumAPIError as e:
            if getattr(e, "http_status", None) == 429 and attempt < attempts - 1:
                _time.sleep(wait)
                last_exc = e
                continue
            raise
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("fetch_label_live: no result")


def get_or_fetch_label(token, user_id, order_id, *, size="LARGE") -> bytes:
    """Cache-first label: serve from DB when present, else fetch it live (paced +
    429-retry), store, and return. Raises on a live failure."""
    cached = get_cached_label(user_id, order_id, size=size)
    if cached is not None:
        return cached
    pdf = fetch_label_live(token, order_id, size=size)
    if pdf:
        try:
            store_label(user_id, order_id, size, pdf)
        except Exception as e:  # caching is best-effort — never fail the request
            print(f"[label-cache] store failed for order {order_id}: {e!r}")
    return pdf


def prune_labels_not_in(user_id, keep_ids) -> int:
    """Delete this user's cached labels whose order_id is NOT in ``keep_ids``.

    Keeps the cache bounded to the user's CURRENTLY active (PACKING /
    PENDING_DELIVERY) FBS orders — once an order ships it's never reprinted, so
    its row is dead weight. Also catches ORPHANS: an order whose ``fbs_orders``
    row was pruned (status departure) leaves no active id, so its label is
    dropped here too. An empty ``keep_ids`` wipes the user's rows entirely.
    Returns rows deleted.
    """
    keep = [str(i) for i in keep_ids]
    with SessionLocal() as db:
        q = _delete(FbsOrderLabel).where(FbsOrderLabel.user_id == int(user_id))
        if keep:
            q = q.where(FbsOrderLabel.order_id.notin_(keep))
        res = db.execute(q)
        db.commit()
        return res.rowcount or 0


# Sync-warm throttle: the immediate on-confirm warm (fbs/routes.py
# ``_warm_labels_async``) covers app-confirmed orders the moment they're
# confirmed; this periodic sync-warm is the safety net for orders confirmed
# outside our app (Uzum's own site). 500s < the 600s sync tick, so the net
# fires on EVERY tick (~10 min — Abdulaziz 2026-06-12: site-confirmed orders
# should warm within one sync cycle; the extra cost is just 2 indexed local DB
# queries per tick, zero Uzum calls when nothing is missing). The throttle
# still guards against accidental double-runs inside one tick. Keyed per
# (user, shop-scope) — NOT per user alone: one owner's shops can be split
# across two token groups warmed by two parallel worker threads in the same
# tick. A user-only key would let the first thread's timestamp short-circuit
# the second group entirely, starving those shops' labels for ~25 min. The
# scope tuple makes each group throttle independently. An in-process dict is
# enough — the sync worker's background threads all live in ONE gunicorn
# worker.
_LAST_WARM_TS: dict[tuple, float] = {}
LABEL_WARM_MIN_INTERVAL_SEC = 500


def prefetch_labels_for_token(
    token, user_id, shop_uzum_ids, *,
    size="LARGE", max_labels=30, force=False,
) -> tuple[int, int]:
    """Background: warm the label cache for a token's active FBS orders, and
    prune labels that are no longer active so the table stays BOUNDED.

    Called from the FBS sync worker per token, AFTER the order sync has run (so
    ``fbs_orders`` is current). Cheap by design:

      * Throttled to once per ~25 min per user (every ~3rd sync tick) — the
        on-confirm immediate warm covers app-confirmed orders instantly, this
        is only the safety net for site-confirmed ones. ``force=True`` bypasses.
      * ONE LEFT-JOIN query yields the active set AND which of those orders
        lack a cached label — no per-order existence queries.

    For each missing label we fetch + store it (paced through the per-token
    gate, capped at ``max_labels`` per run so warming never hogs the print
    bucket from a live "Yorliq" click). Then it PRUNES every label whose
    order_id left the active set (shipped, cancelled, or its ``fbs_orders`` row
    was reconciled away). The active set is a local DB read that can't
    half-fail, so pruning is always safe. Returns ``(fetched, pruned)``.
    """
    shop_ids = [str(s) for s in (shop_uzum_ids or [])]
    if not shop_ids:
        return (0, 0)

    now = _time.time()
    # Throttle per (user, shop-scope): the sorted shop-id tuple identifies this
    # specific token group's warm so two groups for the same owner don't
    # short-circuit each other (see _LAST_WARM_TS note above).
    warm_key = (int(user_id), tuple(sorted(shop_ids)))
    last = _LAST_WARM_TS.get(warm_key)
    if not force and last is not None and (now - last) < LABEL_WARM_MIN_INTERVAL_SEC:
        return (0, 0)
    _LAST_WARM_TS[warm_key] = now

    size_norm = str(size).upper()
    # ONE query: active FBS orders LEFT-JOINed to their cached label (if any) —
    # ``cached_id`` is NULL exactly for the orders we still need to fetch.
    with SessionLocal() as db:
        rows = db.execute(
            select(
                FbsOrder.order_id,
                FbsOrderLabel.order_id.label("cached_id"),
            )
            .outerjoin(
                FbsOrderLabel,
                (FbsOrderLabel.order_id == FbsOrder.order_id)
                & (FbsOrderLabel.user_id == int(user_id))
                & (FbsOrderLabel.size == size_norm),
            )
            .where(FbsOrder.shop_id.in_(shop_ids))
            .where(FbsOrder.order_type == "FBS")
            .where(FbsOrder.status.in_(WARM_STATUSES))
        ).all()
    active_ids = [r.order_id for r in rows]
    missing_ids = [r.order_id for r in rows if r.cached_id is None]

    fetched = 0
    for oid in missing_ids:
        if fetched >= max_labels:
            break
        try:
            pdf = fetch_label_live(token, oid, size=size_norm)
            if pdf:
                store_label(user_id, oid, size_norm, pdf)
                fetched += 1
        except Exception as e:
            print(f"[label-prefetch] fetch failed for order {oid}: {e!r}")

    # Prune dead rows — active set is a local DB read, always complete/safe.
    pruned = 0
    try:
        pruned = prune_labels_not_in(user_id, active_ids)
    except Exception as e:
        print(f"[label-prefetch] prune failed for user {user_id}: {e!r}")
    return (fetched, pruned)


__all__ = [
    "get_cached_label", "store_label", "delete_label", "prune_labels_not_in",
    "fetch_label_live", "get_or_fetch_label", "prefetch_labels_for_token",
    "WARM_STATUSES",
]
