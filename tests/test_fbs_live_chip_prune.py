"""Live chip reads — the phantom ("arvox") fix (2026-07-13).

Abdulaziz: "yangi / yig'ilmoqda / yo'lda — har bosilganda Uzumdan jonli
olinsin, Uzum bilan 100% bir xil bo'lsin, arvox qolmasin."

Two holes let a phantom survive every press before this:

  1. The "Hammasi" press refreshed only page 0 and skipped the prune whenever
     that page came back FULL — so any status holding more orders than the page
     size could never shed a phantom.
  2. When Uzum reported ZERO orders in a status, the prune did nothing at all
     (an empty drain was distrusted, to protect against a fluke empty-200), so
     "Uzum says 0, we hold 1" was unfixable by pressing the chip.

``refresh_shops_status_live`` closes both, and the settle step below decides a
departed row's fate by ASKING UZUM where it went rather than guessing from the
row's contents. These tests pin the guards that keep it from destroying data:

  * a departed row is only deleted once a PENDING_DELIVERY drain proves it did
    not simply join a накладная (deleting it broke invoice ownership → the
    seller's own invoice detail 403'd as "foreign shop");
  * an invoice_number is NOT the signal — it survives into DELIVERED/COMPLETED,
    so keying off it restamped delivered orders backwards into PENDING_DELIVERY;
  * an empty drain is only trusted when /count independently agrees;
  * a TRUNCATED drain never prunes at all.

No DB: SessionLocal is mocked and every statement is captured, so we assert on
the emitted SQL shape (same style as test_fbs_yangilash_prune.py).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import core.fbs_data as fbs_data


def _session_capturing_executes(departed_ids=("999",)):
    """``with SessionLocal() as db:`` mock. SELECTs return ``departed_ids`` (the
    rows the settle step is about to act on); writes report rowcount=1."""
    executed = []

    def _execute(stmt, *a, **k):
        executed.append(stmt)
        res = MagicMock()
        res.rowcount = 1
        res.all.return_value = [(str(i),) for i in departed_ids]
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


def _updates(executed):
    return [s for s in executed
            if str(s).strip().lower().startswith("update fbs_orders")]


class TestDepartedRowsAreResolvedAgainstUzum:
    """A departed row is NOT assumed to be a phantom. We drain PENDING_DELIVERY
    first: if Uzum reports the order there, it joined a накладная and the upsert
    re-homes it (so the DELETE, which is scoped to the drained status, can no
    longer match it). Only what is still sitting in the status gets deleted.
    """

    def _settle(self, *, departed_ids, pd_orders, status="PACKING", token="tok"):
        session, _db, executed = _session_capturing_executes(departed_ids)
        with patch.object(fbs_data, "_fbs_fetch_all_pages", return_value=pd_orders), \
             patch.object(fbs_data, "_fbs_upsert_orders", return_value=len(pd_orders)) as ups, \
             patch.object(fbs_data, "SessionLocal", session):
            with session() as db:
                rehomed, deleted = fbs_data._settle_departed_status_rows(
                    db, ["shopA"], status, {"111"}, token=token,
                )
        return rehomed, deleted, executed, ups

    def test_departed_order_found_in_pending_delivery_is_rehomed(self):
        # Order 999 left PACKING and Uzum reports it under PENDING_DELIVERY →
        # it joined a накладная. Upsert re-homes it; it must NOT be deleted as
        # a phantom (that is what destroyed invoice ownership).
        rehomed, _deleted, executed, ups = self._settle(
            departed_ids=["999"],
            pd_orders=[{"id": "999", "status": "PENDING_DELIVERY",
                        "invoiceNumber": "120001141434"}],
        )
        assert rehomed == 1
        ups.assert_called_once()
        # The DELETE is scoped to status == PACKING, so the re-homed row (now
        # PENDING_DELIVERY) cannot match it.
        sql = str(_deletes(executed)[0]).lower()
        assert "status" in sql
        # No status-blind restamp: invoice_number must not appear in a WHERE/SET.
        assert _updates(executed) == []

    def test_departed_order_absent_from_pending_delivery_is_deleted(self):
        # Uzum has it nowhere → a real phantom (cancelled in the Uzum app).
        rehomed, deleted, executed, _ups = self._settle(
            departed_ids=["999"], pd_orders=[],
        )
        assert rehomed == 0
        assert deleted == 1
        assert len(_deletes(executed)) == 1

    def test_delivered_order_is_never_restamped_backwards(self):
        # THE REGRESSION THIS GUARDS: invoice_number stays set through
        # DELIVERED/COMPLETED. Keying the "it joined an invoice" decision off
        # that column moved delivered orders back into PENDING_DELIVERY. Here a
        # DELIVERING order departs and Uzum does NOT report it in
        # PENDING_DELIVERY → it must be deleted (worker re-adds it as DELIVERED),
        # never restamped.
        rehomed, deleted, executed, _ups = self._settle(
            departed_ids=["999"], pd_orders=[], status="DELIVERING",
        )
        assert rehomed == 0 and deleted == 1
        assert _updates(executed) == []

    def test_pending_delivery_itself_does_not_re_home(self):
        session, _db, executed = _session_capturing_executes(["999"])
        with patch.object(fbs_data, "_fbs_fetch_all_pages") as fetch, \
             patch.object(fbs_data, "SessionLocal", session):
            with session() as db:
                fbs_data._settle_departed_status_rows(
                    db, ["shopA"], "PENDING_DELIVERY", {"111"}, token="tok",
                )
        fetch.assert_not_called()          # no pointless self-drain
        assert len(_deletes(executed)) == 1

    def test_rehome_fetch_failure_keeps_the_rows(self):
        # Fail CLOSED: without Uzum's answer we cannot tell a phantom from an
        # invoiced order, so we delete nothing rather than risk the latter.
        session, _db, executed = _session_capturing_executes(["999"])
        with patch.object(fbs_data, "_fbs_fetch_all_pages", side_effect=RuntimeError("429")), \
             patch.object(fbs_data, "SessionLocal", session):
            with session() as db:
                rehomed, deleted = fbs_data._settle_departed_status_rows(
                    db, ["shopA"], "PACKING", {"111"}, token="tok",
                )
        assert (rehomed, deleted) == (0, 0)
        assert _deletes(executed) == []

    def test_nothing_departed_is_a_noop(self):
        session, _db, executed = _session_capturing_executes(departed_ids=[])
        with patch.object(fbs_data, "_fbs_fetch_all_pages") as fetch, \
             patch.object(fbs_data, "SessionLocal", session):
            with session() as db:
                rehomed, deleted = fbs_data._settle_departed_status_rows(
                    db, ["shopA"], "PACKING", {"111"}, token="tok",
                )
        assert (rehomed, deleted) == (0, 0)
        fetch.assert_not_called()          # no Uzum call when nothing left
        assert _deletes(executed) == []


def _run_live(orders, status="PACKING", *, complete=True, count=None,
              count_raises=False, departed_ids=("999",)):
    """Drive refresh_shops_status_live with a fake Uzum."""
    session, _db, executed = _session_capturing_executes(departed_ids)

    def _count(*a, **k):
        if count_raises:
            raise RuntimeError("Uzum 429")
        return (count, "count-url")

    with patch.object(fbs_data, "_fbs_fetch_all_pages_checked",
                      return_value=(orders, complete)), \
         patch.object(fbs_data, "_fbs_fetch_all_pages", return_value=[]), \
         patch.object(fbs_data, "fetch_fbs_orders_count", side_effect=_count), \
         patch.object(fbs_data, "SessionLocal", session), \
         patch.object(fbs_data, "_fbs_upsert_orders", return_value=len(orders)):
        fbs_data.refresh_shops_status_live("tok", ["shopA"], status)
    return _deletes(executed)


class TestEmptyDrain:
    def test_count_agrees_zero_then_status_is_cleared(self):
        # Uzum's list says empty AND /count says 0 → the status really is empty,
        # so every local row in it has departed. This is the case the seller was
        # stuck on: "Yangi 1" with nothing behind it.
        assert len(_run_live([], count=0)) == 1

    def test_count_disagrees_then_nothing_is_deleted(self):
        # The empty list was a lie (fluke empty-200): /count still sees 3 orders.
        assert _run_live([], count=3) == []

    def test_count_call_fails_then_nothing_is_deleted(self):
        assert _run_live([], count_raises=True) == []


class TestTruncatedDrain:
    def test_incomplete_drain_never_prunes(self):
        # An empty page mid-drain (or the page cap) yields a TRUNCATED set. The
        # orders we never saw are not phantoms — pruning off this would delete
        # live orders.
        orders = [{"id": str(i)} for i in range(1, 51)]
        assert _run_live(orders, complete=False) == []

    def test_complete_drain_prunes(self):
        orders = [{"id": str(i)} for i in range(1, 51)]
        assert len(_run_live(orders, complete=True)) == 1


class TestTerminalStatuses:
    def test_terminal_status_is_never_pruned(self):
        assert _run_live([{"id": "111"}], status="COMPLETED") == []

    def test_terminal_status_empty_drain_never_wipes(self):
        assert _run_live([], status="COMPLETED", count=0) == []


class TestLiveReadSkipsCache:
    def test_refresh_read_does_not_go_through_swr(self):
        # Serving the post-reconcile read from the 15s SWR cache would hand back
        # a PRE-reconcile snapshot and put the phantom straight back on screen.
        read_result = {"orders": [], "total": 0, "used_url": "db://fbs_orders"}
        with patch.object(fbs_data, "refresh_shops_status_live", return_value=(1, 0)), \
             patch.object(fbs_data, "invalidate_fbs_cache"), \
             patch.object(fbs_data, "_read_orders_from_db", return_value=read_result) as read, \
             patch.object(fbs_data, "swr_get") as swr:
            fbs_data.get_fbs_orders("tok", "shopA", status="PACKING", refresh=True)
        swr.assert_not_called()
        read.assert_called_once()

    def test_non_refresh_read_still_uses_swr(self):
        cached = {"orders": [], "total": 0, "used_url": "db://fbs_orders"}
        with patch.object(fbs_data, "swr_get", return_value=cached) as swr:
            fbs_data.get_fbs_orders("tok", "shopA", status="COMPLETED", refresh=False)
        swr.assert_called_once()
