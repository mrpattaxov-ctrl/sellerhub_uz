"""Tests for the per-token min-interval gate ``pace_uzum_call``.

Uzum trips a hidden per-token burst penalty when more than one
``/v2/fbs/orders`` call lands on a token within the same second. The
gate makes every caller (the background worker AND the JIT "Yangilash"
refresh) reserve a ``>= 1s``-spaced slot per token, so the two paths
interleave paced instead of bursting.

The pacing math is pure and deterministic — we inject a fake clock and a
recording sleep so the tests never actually sleep. ``_monotonic`` /
``_sleep`` are keyword-only injection points on ``pace_uzum_call``.
"""
from __future__ import annotations

import pytest

import core.fbs_locks as fbs_locks
from core.fbs_locks import pace_uzum_call, prune_token_slots


@pytest.fixture(autouse=True)
def _clear_gate_state():
    """The slot registry is a module global — reset it before each test
    so tests don't leak reserved slots into one another."""
    fbs_locks._token_next_slot.clear()
    yield
    fbs_locks._token_next_slot.clear()


class _AdvancingClock:
    """Fake clock whose ``sleep`` advances time — models real wall-clock
    where sleeping actually passes the requested duration."""

    def __init__(self, start: float = 1000.0):
        self.t = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, dur: float) -> None:
        self.sleeps.append(dur)
        self.t += dur


class _FrozenClock:
    """Fake clock whose ``sleep`` is recorded but does NOT advance time —
    models two callers reserving slots at the *same instant* (before
    either has actually slept), to prove the reservation math spaces them
    regardless of when the sleeps happen."""

    def __init__(self, start: float = 1000.0):
        self.t = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, dur: float) -> None:
        self.sleeps.append(dur)


def _pace(token, clock, **kw):
    return pace_uzum_call(token, _monotonic=clock.monotonic, _sleep=clock.sleep, **kw)


class TestPaceUzumCall:
    def test_first_call_never_waits(self):
        clock = _AdvancingClock(1000.0)
        slot = _pace("tok", clock)
        assert slot == 1000.0
        assert clock.sleeps == []  # no sleep on the first call

    def test_second_rapid_call_waits_one_interval(self):
        clock = _AdvancingClock(1000.0)
        _pace("tok", clock)
        slot2 = _pace("tok", clock)
        # Second call is forced to the next 1s slot.
        assert slot2 == 1001.0
        assert clock.sleeps == [pytest.approx(1.0)]

    def test_three_rapid_calls_space_one_second_each(self):
        clock = _AdvancingClock(1000.0)
        slots = [_pace("tok", clock) for _ in range(3)]
        assert slots == [1000.0, 1001.0, 1002.0]
        # Only the 2nd and 3rd calls sleep (the 1st is free).
        assert clock.sleeps == [pytest.approx(1.0), pytest.approx(1.0)]

    def test_gap_longer_than_interval_does_not_wait(self):
        clock = _AdvancingClock(1000.0)
        _pace("tok", clock)            # reserves 1000
        clock.t = 1005.0               # 5s pass (e.g. between worker ticks)
        slot = _pace("tok", clock)
        assert slot == 1005.0          # now >= last + 1s → no throttle
        assert clock.sleeps == []      # nothing slept

    def test_exactly_one_interval_later_does_not_wait(self):
        clock = _AdvancingClock(1000.0)
        _pace("tok", clock)            # reserves 1000
        clock.t = 1001.0               # exactly 1s later
        slot = _pace("tok", clock)
        assert slot == 1001.0
        assert clock.sleeps == []

    def test_different_tokens_are_independent(self):
        clock = _AdvancingClock(1000.0)
        slot_a = _pace("tokA", clock)
        slot_b = _pace("tokB", clock)
        # tokB is not paced behind tokA — different users never block
        # each other (Uzum's burst penalty is per-token).
        assert slot_a == 1000.0
        assert slot_b == 1000.0
        assert clock.sleeps == []

    def test_concurrent_reservations_at_same_instant_are_spaced(self):
        # Reservation correctness: even if two callers hit the gate at the
        # exact same monotonic instant (the worker and a Yangilash press),
        # they must get slots >= 1s apart. The _pace_lock serializes the
        # reservation; the slot math (max(now, last+interval)) does the
        # spacing. Frozen clock = neither has "slept" yet.
        clock = _FrozenClock(1000.0)
        s1 = _pace("tok", clock)
        s2 = _pace("tok", clock)
        s3 = _pace("tok", clock)
        assert [s1, s2, s3] == [1000.0, 1001.0, 1002.0]
        # Their computed waits grow because time hasn't advanced.
        assert clock.sleeps == [pytest.approx(1.0), pytest.approx(2.0)]

    def test_custom_min_interval_respected(self):
        clock = _AdvancingClock(1000.0)
        _pace("tok", clock, min_interval=2.0)
        slot2 = _pace("tok", clock, min_interval=2.0)
        assert slot2 == 1002.0
        assert clock.sleeps == [pytest.approx(2.0)]

    def test_slot_recorded_in_registry(self):
        clock = _AdvancingClock(1000.0)
        _pace("tok", clock)
        assert fbs_locks._token_next_slot["tok"] == 1000.0
        _pace("tok", clock)
        assert fbs_locks._token_next_slot["tok"] == 1001.0


class TestPruneTokenSlots:
    """The worker calls ``prune_token_slots`` once per tick so the slot
    registry doesn't accumulate entries for deactivated sellers forever.
    """

    def test_drops_only_stale_tokens(self):
        clock = _AdvancingClock(1000.0)
        _pace("keep1", clock)
        _pace("keep2", clock)
        _pace("gone", clock)
        dropped = prune_token_slots(["keep1", "keep2"])
        assert dropped == 1
        assert "gone" not in fbs_locks._token_next_slot
        assert "keep1" in fbs_locks._token_next_slot
        assert "keep2" in fbs_locks._token_next_slot

    def test_empty_active_drops_all(self):
        clock = _AdvancingClock(1000.0)
        _pace("a", clock)
        _pace("b", clock)
        dropped = prune_token_slots([])
        assert dropped == 2
        assert fbs_locks._token_next_slot == {}

    def test_extra_active_tokens_drop_nothing(self):
        clock = _AdvancingClock(1000.0)
        _pace("a", clock)
        # Active set may list tokens that never paced — they're simply not
        # present to drop; nothing is added, nothing erroneously removed.
        dropped = prune_token_slots(["a", "b", "c"])
        assert dropped == 0
        assert "a" in fbs_locks._token_next_slot

    def test_pacing_still_works_after_prune(self):
        # A token pruned mid-life just starts fresh (first-call-no-wait) on
        # its next pace — no crash, no stale slot influencing the new one.
        clock = _AdvancingClock(1000.0)
        _pace("tok", clock)
        prune_token_slots([])          # drop "tok"
        clock.t = 1000.5               # only 0.5s later
        slot = _pace("tok", clock)     # fresh token → no wait
        assert slot == 1000.5
        assert clock.sleeps == []
