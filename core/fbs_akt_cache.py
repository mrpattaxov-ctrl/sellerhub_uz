"""DB cache + paced fetch for FBS invoice akt (Акт отправки) PDFs.

Uzum's ``/v1/fbs/invoice/{id}/print`` rate-limits hard (~4 quick calls →
HTTP 429, ~4s recovery — see the akt-print rate-limit reference). To keep
bulk printing instant and burst-free we cache each akt's PDF bytes in the
``fbs_invoice_akts`` table and pre-fetch them in the background sync worker.

This module is the SINGLE place that (a) reads/writes that cache and (b)
fetches an akt live with a short 429-aware retry (NOT the shared session's
60s backoff). Every caller — the interactive akt endpoints AND the
background prefetch — goes through here so the policy lives in one spot.
"""
from __future__ import annotations

import time as _time

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert

from extensions import SessionLocal
from models import FbsInvoiceAkt
from core.uzum_openapi import fetch_fbs_invoice_akt_pdf, UzumAPIError


def get_cached_akt(user_id, invoice_id, *, date_updated=None) -> bytes | None:
    """Return cached PDF bytes for this invoice, or None on miss/stale.

    Scoped by ``user_id`` so one seller can't read another's cached akt by
    guessing an invoice id. If ``date_updated`` is given and differs from the
    cached version stamp, the row is treated as STALE (None) so the caller
    re-fetches the current akt (e.g. after a drop-off/slot change).
    """
    with SessionLocal() as db:
        row = db.get(FbsInvoiceAkt, int(invoice_id))
        if row is None or int(row.user_id) != int(user_id):
            return None
        if (date_updated is not None and row.date_updated is not None
                and int(row.date_updated) != int(date_updated)):
            return None
        return bytes(row.pdf)


def store_akt(user_id, invoice_id, date_updated, pdf: bytes) -> None:
    """Upsert the akt PDF for this invoice (INSERT … ON CONFLICT DO UPDATE)."""
    stmt = pg_insert(FbsInvoiceAkt).values(
        invoice_id=int(invoice_id),
        user_id=int(user_id),
        date_updated=(int(date_updated) if date_updated is not None else None),
        pdf=pdf,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[FbsInvoiceAkt.invoice_id],
        set_={
            "user_id": stmt.excluded.user_id,
            "date_updated": stmt.excluded.date_updated,
            "pdf": stmt.excluded.pdf,
            "synced_at": func.now(),
        },
    )
    with SessionLocal() as db:
        db.execute(stmt)
        db.commit()


def delete_akt(invoice_id) -> None:
    """Drop the cached akt for one invoice (e.g. after a pickup change, which
    rewrites the akt's address/slot)."""
    with SessionLocal() as db:
        row = db.get(FbsInvoiceAkt, int(invoice_id))
        if row is not None:
            db.delete(row)
            db.commit()


def fetch_akt_live(token, invoice_id, *, attempts: int = 4, wait: float = 4.0) -> bytes:
    """Fetch one akt from Uzum with a SHORT 429-aware retry.

    Returns Uzum's akt PDF UNCHANGED — exactly the document Uzum's own web
    print produces (no cropping/resizing; the seller wanted it left as-is).

    ``fail_fast=True`` avoids the shared session's 60/120/180s backoff; on a
    429 we wait ``wait`` seconds (the print bucket refills in ~3–4s) and retry
    up to ``attempts`` times. Raises the last error if it never succeeds.
    """
    last_exc = None
    for attempt in range(attempts):
        try:
            pdf, _ = fetch_fbs_invoice_akt_pdf(token, invoice_id, fail_fast=True)
            return pdf
        except UzumAPIError as e:
            if getattr(e, "http_status", None) == 429 and attempt < attempts - 1:
                _time.sleep(wait)
                last_exc = e
                continue
            raise
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("fetch_akt_live: no result")


def get_or_fetch_akt(token, user_id, invoice_id, *, date_updated=None) -> bytes:
    """Cache-first akt: serve from DB when present (and fresh), else fetch it
    live (paced + 429-retry), store, and return. Raises on a live failure."""
    cached = get_cached_akt(user_id, invoice_id, date_updated=date_updated)
    if cached is not None:
        return cached
    pdf = fetch_akt_live(token, invoice_id)
    try:
        store_akt(user_id, invoice_id, date_updated, pdf)
    except Exception as e:  # caching is best-effort — never fail the request
        print(f"[akt-cache] store failed for invoice {invoice_id}: {e!r}")
    return pdf


def prune_akts_not_in(user_id, keep_ids) -> int:
    """Delete this user's cached akts whose invoice_id is NOT in ``keep_ids``.

    Keeps the cache bounded to the user's CURRENTLY active (CREATED) invoices
    — once an invoice leaves CREATED (accepted/cancelled) it's never reprinted,
    so its ~64 KB row is dead weight. An empty ``keep_ids`` wipes the user's
    rows entirely (no active invoices → nothing to keep). Returns rows deleted.
    """
    from sqlalchemy import delete as _delete
    keep = [int(i) for i in keep_ids]
    with SessionLocal() as db:
        q = _delete(FbsInvoiceAkt).where(FbsInvoiceAkt.user_id == int(user_id))
        if keep:
            q = q.where(FbsInvoiceAkt.invoice_id.notin_(keep))
        res = db.execute(q)
        db.commit()
        return res.rowcount or 0


def prefetch_akts_for_token(
    token, user_id, *,
    statuses=("CREATED",), max_pages=25, max_akts=40,
) -> tuple[int, int]:
    """Background: warm the akt cache for a token's active invoices, and prune
    akts that are no longer active so the table stays BOUNDED.

    Called from the FBS sync worker per token. Walks the invoice list for
    ``statuses`` (the printable ones — CREATED; that's what a seller hands over
    and prints) and, for any invoice whose akt is missing or whose
    ``dateUpdated`` changed, fetches + stores it. In steady state most akts are
    already cached & fresh, so a tick only re-fetches NEW or changed invoices.

    Then it PRUNES: the cache should mirror exactly the current active set, so
    rows for invoices that left CREATED are deleted — keeping the table sized
    to active work (a few MB), not full history. Pruning is skipped if the
    list couldn't be fully enumerated (a fetch failure), so we never drop akts
    we simply didn't see this tick.

    ``max_akts`` caps akt downloads per tick (the gate-heavy part); id
    enumeration for pruning continues up to ``max_pages``. All calls are paced
    and best-effort. Returns ``(fetched, pruned)``.
    """
    from core.uzum_openapi import fetch_fbs_invoices_list

    fetched = 0
    seen_ids: set[int] = set()
    complete = True  # full active list enumerated → safe to prune
    for status in statuses:
        page = 0
        status_done = False
        while page < max_pages:
            try:
                invs, _ = fetch_fbs_invoices_list(
                    token, statuses=[status], page=page, fail_fast=False
                )
            except Exception as e:
                print(f"[akt-prefetch] list failed (status={status} p={page}): {e!r}")
                complete = False
                break
            if not invs:
                status_done = True
                break
            for inv in invs:
                iid = inv.get("id")
                if iid is None:
                    continue
                seen_ids.add(int(iid))   # always collect ids (for prune)
                du = inv.get("dateUpdated")
                # Already cached & fresh, or per-tick download cap reached.
                if fetched >= max_akts:
                    continue
                if get_cached_akt(user_id, iid, date_updated=du) is not None:
                    continue
                try:
                    pdf = fetch_akt_live(token, iid)
                    store_akt(user_id, iid, du, pdf)
                    fetched += 1
                except Exception as e:
                    print(f"[akt-prefetch] akt fetch failed for invoice {iid}: {e!r}")
            if len(invs) < 20:           # short page → last page for this status
                status_done = True
                break
            page += 1
        if not status_done:              # hit max_pages without a short page
            complete = False

    # Prune dead rows — but ONLY when we have the COMPLETE active set, else a
    # transient list failure would wrongly wipe valid akts.
    pruned = 0
    if complete:
        try:
            pruned = prune_akts_not_in(user_id, seen_ids)
        except Exception as e:
            print(f"[akt-prefetch] prune failed for user {user_id}: {e!r}")
    return (fetched, pruned)


__all__ = [
    "get_cached_akt", "store_akt", "delete_akt", "prune_akts_not_in",
    "fetch_akt_live", "get_or_fetch_akt", "prefetch_akts_for_token",
]
