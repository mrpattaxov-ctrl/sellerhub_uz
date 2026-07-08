"""Worker sync-cadence tests for ``core.fbs_sync.fbs_statuses_for_tick``.

Bosqich A.10 — the background worker used to re-fetch all 11 FBS statuses
every 10-min tick (~66 Uzum calls/hour/token), exhausting the per-token
OpenAPI quota and tripping 429. The cadence split keeps the seller's
action queue (4 active statuses) fresh every tick while the settled /
terminal tail refreshes only ~hourly. These pure-function tests pin:

  * the active set IS the action queue + immediate next hop,
  * active ⊂ all, and active ∪ slow == all with no overlap (no drift),
  * tick 0 and every Nth tick are full sweeps; the rest are active-only,
  * the per-N-tick call budget really is "active×N + slow once" — the
    saving we promised the user (31 vs 66 at N=6),
  * slow_every_n <= 1 (or non-positive) degrades safely to "always full".

No DB, no HTTP, no clock — fbs_statuses_for_tick is deliberately pure so
the cadence is deterministic and testable without the worker harness.
"""
from __future__ import annotations

import pytest

from core.fbs_sync import (
    fbs_statuses_for_tick,
    fbs_statuses_due,
    FBS_STATUS_SYNC_INTERVAL_MIN,
    FBS_ALL_SYNC_STATUSES,
    FBS_ACTIVE_SYNC_STATUSES,
    FBS_SLOW_SYNC_STATUSES,
)


# ─────────────────────────────────────────────────────────────────────
# Status set shape + invariants
# ─────────────────────────────────────────────────────────────────────


class TestStatusSets:
    def test_active_is_action_queue_plus_next_hop(self):
        # CREATED/PACKING/PENDING_DELIVERY are where the seller acts;
        # DELIVERING is the immediate next hop so an order leaves the
        # queue within one tick (PENDING_DELIVERY count self-corrects in
        # 10 min, not 1h). Compare as sets — tuple order is cosmetic.
        assert set(FBS_ACTIVE_SYNC_STATUSES) == {
            "CREATED", "PACKING", "PENDING_DELIVERY", "DELIVERING",
        }

    def test_active_count_four_slow_count_six(self):
        # The 4+6 split: 4 active every tick, 6 slow ~hourly. (SLOW was 7
        # before A.11 dropped PENDING_CANCELLATION from the sync set.)
        assert len(FBS_ACTIVE_SYNC_STATUSES) == 4
        assert len(FBS_SLOW_SYNC_STATUSES) == 6

    def test_all_has_ten_distinct_statuses(self):
        # 10, not 11: PENDING_CANCELLATION is intentionally excluded from
        # the worker sweep (A.11) — Uzum is never polled for it.
        assert len(FBS_ALL_SYNC_STATUSES) == 10
        assert len(set(FBS_ALL_SYNC_STATUSES)) == 10  # no dupes

    def test_pending_cancellation_never_synced(self):
        # A.11 invariant: the "Bekor qilinmoqda" status must not appear in
        # ANY sync set — not active, not slow, not the full sweep. This is
        # the "don't fetch it from Uzum even hourly" guarantee.
        assert "PENDING_CANCELLATION" not in FBS_ALL_SYNC_STATUSES
        assert "PENDING_CANCELLATION" not in FBS_ACTIVE_SYNC_STATUSES
        assert "PENDING_CANCELLATION" not in FBS_SLOW_SYNC_STATUSES

    def test_active_subset_of_all(self):
        assert set(FBS_ACTIVE_SYNC_STATUSES).issubset(set(FBS_ALL_SYNC_STATUSES))

    def test_slow_is_all_minus_active(self):
        # The point of deriving SLOW rather than hand-listing it: a
        # status added to ALL but not ACTIVE auto-falls into SLOW and so
        # can never be silently dropped from the worker entirely.
        assert set(FBS_SLOW_SYNC_STATUSES) == (
            set(FBS_ALL_SYNC_STATUSES) - set(FBS_ACTIVE_SYNC_STATUSES)
        )

    def test_active_and_slow_disjoint(self):
        assert not (set(FBS_ACTIVE_SYNC_STATUSES) & set(FBS_SLOW_SYNC_STATUSES))

    def test_active_plus_slow_covers_all(self):
        assert (
            set(FBS_ACTIVE_SYNC_STATUSES) | set(FBS_SLOW_SYNC_STATUSES)
        ) == set(FBS_ALL_SYNC_STATUSES)

    def test_terminal_statuses_are_slow_not_active(self):
        # Statuses that never change once reached must NOT be in the
        # every-tick set — that's exactly the quota we're reclaiming.
        for terminal in ("COMPLETED", "CANCELED", "RETURNED"):
            assert terminal in FBS_SLOW_SYNC_STATUSES
            assert terminal not in FBS_ACTIVE_SYNC_STATUSES


# ─────────────────────────────────────────────────────────────────────
# Cadence selection
# ─────────────────────────────────────────────────────────────────────


class TestCadence:
    N = 6  # default at the 10-min interval: round(3600 / 600)

    def test_tick_zero_is_full_sweep(self):
        # First tick after boot/restart re-backfills everything (so a
        # restart never leaves the slow tail stale until the next hour).
        assert fbs_statuses_for_tick(0, self.N) == FBS_ALL_SYNC_STATUSES

    def test_nth_ticks_are_full_sweeps(self):
        assert fbs_statuses_for_tick(self.N, self.N) == FBS_ALL_SYNC_STATUSES
        assert fbs_statuses_for_tick(2 * self.N, self.N) == FBS_ALL_SYNC_STATUSES

    def test_intermediate_ticks_are_active_only(self):
        for i in range(1, self.N):
            assert fbs_statuses_for_tick(i, self.N) == FBS_ACTIVE_SYNC_STATUSES

    def test_exactly_one_full_sweep_per_n_ticks(self):
        full = sum(
            1 for i in range(self.N)
            if fbs_statuses_for_tick(i, self.N) == FBS_ALL_SYNC_STATUSES
        )
        assert full == 1

    def test_call_budget_over_one_hour(self):
        # The actual saving: over N ticks active runs every tick and the
        # full set runs once. At N=6 → 4*5 + 10*1 = 30 calls vs the
        # pre-A.10 11*6 = 66 (all 11 statuses every tick). After A.11
        # dropped PENDING_CANCELLATION the full sweep is 10, so 30. If
        # someone changes the split, this test makes the budget explicit.
        calls = sum(
            len(fbs_statuses_for_tick(i, self.N)) for i in range(self.N)
        )
        pre_a10 = 11 * self.N  # original: all 11 statuses every tick
        assert calls == 30
        assert pre_a10 == 66
        assert calls < pre_a10  # strictly fewer — the whole point

    def test_slow_every_one_is_always_full(self):
        # Worker already ticks >= hourly → no sub-cadence needed, every
        # tick is a full sweep.
        for i in range(5):
            assert fbs_statuses_for_tick(i, 1) == FBS_ALL_SYNC_STATUSES

    def test_non_positive_cadence_degrades_to_full(self):
        # Defensive: a misconfigured / non-positive cadence must never
        # produce an empty or partial sweep — fall back to full.
        assert fbs_statuses_for_tick(3, 0) == FBS_ALL_SYNC_STATUSES
        assert fbs_statuses_for_tick(3, -5) == FBS_ALL_SYNC_STATUSES

    @pytest.mark.parametrize("n", [2, 3, 4, 6, 12, 240])
    def test_full_sweep_cadence_holds_for_various_intervals(self, n):
        # Whatever N the interval derives (15-min → 4, 5-min → 12,
        # 15-sec → 240), there must be exactly one full sweep per N ticks
        # at offsets 0 and N, active-only in between.
        is_full = [
            fbs_statuses_for_tick(i, n) == FBS_ALL_SYNC_STATUSES
            for i in range(2 * n)
        ]
        assert is_full.count(True) == 2          # two full sweeps in 2N ticks
        assert is_full[0] is True and is_full[n] is True
        # every other position is active-only
        assert all(
            fbs_statuses_for_tick(i, n) == FBS_ACTIVE_SYNC_STATUSES
            for i in range(2 * n) if i % n != 0
        )

    def test_active_only_ticks_never_return_slow_statuses(self):
        # An active-only tick must not touch any terminal/settled status
        # — otherwise the quota saving evaporates.
        active = fbs_statuses_for_tick(1, self.N)
        assert not (set(active) & set(FBS_SLOW_SYNC_STATUSES))


# ─────────────────────────────────────────────────────────────────────
# Per-status interval cadence (Abdulaziz 2026-06-16)
# ─────────────────────────────────────────────────────────────────────
#
# The worker now wakes on a ~1-min heartbeat and syncs each status only when
# its own FBS_STATUS_SYNC_INTERVAL_MIN has elapsed. fbs_statuses_due() is the
# pure decision function — no DB, no clock, no globals (the caller owns the
# clock + last-sync book-keeping), so the cadence is deterministic here.


class TestStatusIntervalMap:
    def test_pending_states_are_never_synced(self):
        # PENDING_DELIVERY is shown only via the live Поставка/накладные view;
        # PENDING_CANCELLATION is transient. Neither is background-synced, so
        # both must be ABSENT from the interval map (the "never" contract).
        assert "PENDING_DELIVERY" not in FBS_STATUS_SYNC_INTERVAL_MIN
        assert "PENDING_CANCELLATION" not in FBS_STATUS_SYNC_INTERVAL_MIN

    def test_configured_statuses_are_a_subset_of_all(self):
        assert set(FBS_STATUS_SYNC_INTERVAL_MIN).issubset(set(FBS_ALL_SYNC_STATUSES))

    def test_all_intervals_are_positive_minutes(self):
        assert all(v > 0 for v in FBS_STATUS_SYNC_INTERVAL_MIN.values())


class TestStatusesDue:
    INTERVALS = {"CREATED": 23, "PACKING": 23, "DELIVERING": 63}

    def test_first_run_forces_every_configured_status(self):
        # A (re)start backfills the full configured set in one pass,
        # regardless of last-sync state.
        due = fbs_statuses_due(1000.0, {}, intervals=self.INTERVALS, first_run=True)
        assert set(due) == set(self.INTERVALS)

    def test_never_synced_status_is_due(self):
        # A status with no last-sync entry is always due (cold start, no
        # first_run flag).
        due = fbs_statuses_due(1000.0, {}, intervals=self.INTERVALS)
        assert set(due) == {"CREATED", "PACKING", "DELIVERING"}

    def test_status_not_due_before_its_interval(self):
        now = 10_000.0
        # CREATED synced 10 min ago (< 23 min) → NOT due. DELIVERING synced
        # 10 min ago (< 63 min) → NOT due.
        last = {"CREATED": now - 10 * 60, "PACKING": now - 10 * 60,
                "DELIVERING": now - 10 * 60}
        due = fbs_statuses_due(now, last, intervals=self.INTERVALS)
        assert due == ()

    def test_status_due_exactly_at_its_interval(self):
        now = 10_000.0
        # CREATED synced 23 min ago → due (>=). DELIVERING 23 min ago → NOT
        # due (needs 63). This is the whole point: independent cadences.
        last = {"CREATED": now - 23 * 60, "PACKING": now - 5 * 60,
                "DELIVERING": now - 23 * 60}
        due = fbs_statuses_due(now, last, intervals=self.INTERVALS)
        assert set(due) == {"CREATED"}

    def test_each_status_keeps_its_own_clock(self):
        now = 100_000.0
        # CREATED overdue (30>23), PACKING fresh (1<23), DELIVERING overdue
        # (70>63). Only the overdue ones come back.
        last = {"CREATED": now - 30 * 60, "PACKING": now - 1 * 60,
                "DELIVERING": now - 70 * 60}
        due = fbs_statuses_due(now, last, intervals=self.INTERVALS)
        assert set(due) == {"CREATED", "DELIVERING"}

    def test_result_follows_all_status_order(self):
        # Determinism: result is ordered by FBS_ALL_SYNC_STATUSES, not dict
        # insertion or set iteration order.
        due = fbs_statuses_due(1000.0, {}, first_run=True)
        order = [s for s in FBS_ALL_SYNC_STATUSES if s in set(due)]
        assert list(due) == order

    def test_default_intervals_exclude_pending_states(self):
        # With the REAL map, a first-run sweep must never include the
        # never-sync statuses.
        due = fbs_statuses_due(1000.0, {}, first_run=True)
        assert "PENDING_DELIVERY" not in due
        assert "PENDING_CANCELLATION" not in due
