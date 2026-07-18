"""Worker sweeps EVERY shop on the token, and never prunes off a partial drain.

Two fixes, both landing in ``app._fbs_sync_one_token`` via the pure helpers in
``core.fbs_sync`` (2026-07-13):

1. **The shop gate is gone** (:func:`select_sweep_shops`). The worker used to
   sweep a shop only if it had FBS stock or an open active order. Measured on
   prod data: 4 of 7 shops failed that test and were never background-synced —
   one user's ONLY shop among them, so their FBS rows were refreshed exclusively
   by opening the page. The gate saved nothing: the sync is TOKEN-BATCHED (one
   Uzum call per status carries every shopId), so a skipped shop removed no
   request, only its own data. The chip press never had a gate; the worker now
   agrees with it. ``FBS_SYNC_ALL_SHOPS=0`` restores the old behaviour.

2. **Prune requires a COMPLETE drain** (:func:`may_prune_status`). The reconcile
   DELETES rows absent from the fetched set, so it may only run when the drain
   saw everything. The worker called ``fetch_all_pages``, which discards the
   ``complete`` flag, so a truncated drain (page cap, or a transient empty-200
   mid-drain) was treated as authoritative — and could delete live orders.

Pure functions: no DB, no env, no clock — same style as the fbs_statuses_due
tests next door.
"""
from __future__ import annotations

from core.fbs_sync import (
    FBS_ACTIVE_SYNC_STATUSES,
    may_prune_status,
    select_sweep_shops,
)

# One shop that WOULD pass the old gate (stock/open orders), one that would not.
ALL_SHOPS = ["5983", "40571", "10920"]
GATED = {"5983"}


class TestSelectSweepShops:
    def test_sweeps_every_shop_by_default(self):
        # The stockless, order-less shops (40571, 10920) must ride along — this
        # is the whole fix: they used to be invisible to the worker.
        assert select_sweep_shops(ALL_SHOPS, GATED, sweep_all=True) == ALL_SHOPS

    def test_kill_switch_restores_the_old_gate(self):
        assert select_sweep_shops(ALL_SHOPS, GATED, sweep_all=False) == ["5983"]

    def test_gate_can_starve_a_token_completely(self):
        # The user-4 case: their only shop fails the gate, so the old worker made
        # ZERO Uzum calls for that token. With the gate off, the shop is swept.
        assert select_sweep_shops(["40703"], set(), sweep_all=False) == []
        assert select_sweep_shops(["40703"], set(), sweep_all=True) == ["40703"]

    def test_order_is_preserved(self):
        # The id list is logged and keys the reconcile's shop-group strike
        # counter, so a stable order matters.
        assert select_sweep_shops(["c", "a", "b"], set(), sweep_all=True) == ["c", "a", "b"]

    def test_ids_are_normalised_to_strings(self):
        assert select_sweep_shops([5983, 40571], {"5983"}, sweep_all=False) == ["5983"]


class TestMayPruneStatus:
    def test_complete_active_drain_may_prune(self):
        for status in FBS_ACTIVE_SYNC_STATUSES:
            assert may_prune_status(status, complete=True) is True

    def test_incomplete_drain_may_never_prune(self):
        # A partial drain is not the truth — the rows it "didn't return" may be
        # live orders sitting behind the page cap.
        for status in FBS_ACTIVE_SYNC_STATUSES:
            assert may_prune_status(status, complete=False) is False

    def test_terminal_status_may_never_prune(self):
        # Terminal queues run to thousands of rows: their drain is page-capped,
        # so deleting off it would destroy history.
        for status in ("COMPLETED", "CANCELED", "RETURNED", "DELIVERED"):
            assert may_prune_status(status, complete=True) is False
