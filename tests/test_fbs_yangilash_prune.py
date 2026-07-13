"""Yangilash-prune — the "arvox" / phantom-order fix (2026-06-10).

Bug: an FBS order that left an active status out-of-band (seller cancels
in the Uzum app, or Uzum auto-cancels an overdue order) just stops
appearing in Uzum's status list. The UPSERT-only JIT refresh could never
clear it, so it froze on the seller's list forever — a phantom ("arvox")
order. Pressing "Yangilash" did nothing because the refresh path only
added/updated, never deleted.

Fix: the JIT refresh now prunes, for ACTIVE statuses, any local row Uzum's
authoritative drain no longer returns. These tests pin the two properties
that make the prune safe:

  * it removes a phantom that sits ALONGSIDE a still-present order
    (targeted ``NOT IN fresh_ids`` — never a blanket wipe),
  * it NEVER prunes a terminal status (page-capped drain isn't complete),
    NEVER prunes off a FULL first page (not authoritative), and NEVER
    wipes a whole status when Uzum returns empty (worker 2-strike owns
    that case).

No DB: the session is mocked at the ``SessionLocal()`` boundary and every
statement handed to ``db.execute`` is captured, so we assert on the
emitted SQL shape (matching the DB-free style of the other FBS tests).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import core.fbs_data as fbs_data


def _session_capturing_executes():
    """``with SessionLocal() as db:`` mock whose ``db.execute`` records every
    statement and returns a result reporting ``rowcount=1``.

    ``.all()`` answers the departed-row SELECT that the settle step runs before
    it deletes anything (see core.fbs_data._settle_departed_status_rows): one
    phantom, id 999.
    """
    executed = []

    def _execute(stmt, *a, **k):
        executed.append(stmt)
        res = MagicMock()
        res.rowcount = 1
        res.all.return_value = [("999",)]
        return res

    db = MagicMock(name="db")
    db.execute.side_effect = _execute
    session = MagicMock(name="SessionLocal")
    session.return_value.__enter__.return_value = db
    session.return_value.__exit__.return_value = False
    return session, db, executed


def _deletes(executed):
    return [s for s in executed
            if str(s).strip().lower().startswith("delete from fbs_orders")]


# ── single-shop refresh (get_fbs_orders / get_fbs_count refresh=True) ──
class TestSingleShopRefreshPrune:
    def test_prunes_phantom_alongside_a_real_order(self):
        # Uzum returns only the real order; the phantom (110388551) is gone.
        real = {"id": "111", "status": "PACKING"}
        session, _db, executed = _session_capturing_executes()
        with patch.object(fbs_data, "_fbs_fetch_all_pages", return_value=[real]), \
             patch.object(fbs_data, "SessionLocal", session), \
             patch.object(fbs_data, "_fbs_upsert_orders", return_value=1):
            fbs_data._refresh_shop_status("tok", "shopA", "PACKING")
        dels = _deletes(executed)
        assert len(dels) == 1, "exactly one phantom-prune DELETE expected"
        sql = str(dels[0]).lower()
        assert "status" in sql
        # Targeted: the real order survives via NOT IN fresh_ids — never a
        # blanket "delete every PACKING row".
        assert "not in" in sql

    def test_terminal_status_is_never_pruned(self):
        # A COMPLETED drain is page-capped (could be thousands), so pruning
        # off it could delete real history — must not emit any DELETE.
        real = {"id": "111", "status": "COMPLETED"}
        session, _db, executed = _session_capturing_executes()
        with patch.object(fbs_data, "_fbs_fetch_all_pages", return_value=[real]), \
             patch.object(fbs_data, "SessionLocal", session), \
             patch.object(fbs_data, "_fbs_upsert_orders", return_value=1):
            fbs_data._refresh_shop_status("tok", "shopA", "COMPLETED")
        assert _deletes(executed) == []


# ── the prune helper's own guards ─────────────────────────────────────
class TestPruneHelperGuards:
    def test_empty_fresh_set_does_not_wipe_the_status(self):
        # Uzum returned zero orders → do NOTHING (a fluke empty-200 must not
        # nuke live orders; the unattended worker owns that via 2-strike).
        db = MagicMock(name="db")
        pruned = fbs_data._prune_departed_status_rows(db, ["shopA"], "PACKING", set())
        assert pruned == 0
        db.execute.assert_not_called()

    def test_no_shops_is_a_noop(self):
        db = MagicMock(name="db")
        pruned = fbs_data._prune_departed_status_rows(db, [], "PACKING", {"111"})
        assert pruned == 0
        db.execute.assert_not_called()

    def test_targeted_delete_scopes_to_shops_and_status(self):
        session, _db, executed = _session_capturing_executes()
        with session() as db:
            fbs_data._prune_departed_status_rows(db, ["shopA"], "PACKING", {"111"})
        sql = str(_deletes(executed)[0]).lower()
        assert "shop_id in" in sql
        assert "status" in sql
        assert "order_id not in" in sql


# ── all-shops first-page refresh (the "Hammasi" view) ─────────────────
class TestFirstPageRefreshPrune:
    def test_short_page_is_authoritative_and_prunes(self):
        # One order back on a size-50 page → short page → complete → prune.
        orders = [{"id": "111", "status": "PACKING"}]
        session, _db, executed = _session_capturing_executes()
        with patch.object(fbs_data, "fetch_fbs_orders_page", return_value=({}, "url")), \
             patch.object(fbs_data, "extract_fbs_orders_list", return_value=(orders, None)), \
             patch.object(fbs_data, "SessionLocal", session), \
             patch.object(fbs_data, "_fbs_upsert_orders", return_value=1):
            fbs_data._refresh_shops_status_first_page("tok", ["shopA"], "PACKING", size=50)
        assert len(_deletes(executed)) == 1

    def test_full_page_is_not_authoritative_and_skips_prune(self):
        # size==len(orders) → page may be truncated → must NOT prune.
        orders = [{"id": "1", "status": "PACKING"}, {"id": "2", "status": "PACKING"}]
        session, _db, executed = _session_capturing_executes()
        with patch.object(fbs_data, "fetch_fbs_orders_page", return_value=({}, "url")), \
             patch.object(fbs_data, "extract_fbs_orders_list", return_value=(orders, None)), \
             patch.object(fbs_data, "SessionLocal", session), \
             patch.object(fbs_data, "_fbs_upsert_orders", return_value=2):
            fbs_data._refresh_shops_status_first_page("tok", ["shopA"], "PACKING", size=2)
        assert _deletes(executed) == []
