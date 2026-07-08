"""Mocked tests for the Postavka slot-VOLUME analytics (Faza 0b, READ-ONLY).

The watcher never mutates Uzum — it only reads `time-slot`. The risk is
logging WRONG volume events and drawing a false conclusion. These pin the
pure core (no DB, no network):

  1. measure_volumes() — each slot's volume = the LARGEST ladder quantity it
     still appears for (capacity lower-bound).
  2. diff_snapshots() — emit an event ONLY when a slot's volume INCREASES
     (new or grew); unchanged/decreased/vanished → nothing (the dedup rule
     the whole "don't count the same volume twice" requirement rests on).
  3. format_volume_digest() — the design-C table (kun | hajm | +30daq |
     slot | hozir) the user signed off on.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

from postavki import slot_volume

_TZ = timezone(timedelta(hours=5))


def _ms(y, mo, d, h=12, mi=0) -> int:
    return int(datetime(y, mo, d, h, mi, tzinfo=_TZ).timestamp() * 1000)


# ── measure_volumes(): per-slot capacity bracket ─────────────────────


class TestMeasureVolumes:
    def test_volume_is_largest_quantity_slot_appears_for(self):
        # A fits up to 1500, B up to 500, C only up to 150.
        def fake(q):
            out = []
            if q <= 1500:
                out.append({"timeFrom": 1000, "timeTo": 2000})  # A
            if q <= 500:
                out.append({"timeFrom": 3000, "timeTo": 4000})  # B
            if q <= 150:
                out.append({"timeFrom": 5000, "timeTo": 6000})  # C
            return out

        snap = slot_volume.measure_volumes({"skuId": 1}, [150, 500, 750, 1500], fake)
        assert snap[1000]["vol"] == 1500
        assert snap[1000]["to"] == 2000
        assert snap[3000]["vol"] == 500
        assert snap[5000]["vol"] == 150

    def test_empty_response_is_empty_snapshot(self):
        assert slot_volume.measure_volumes({"skuId": 1}, [150, 500], lambda q: []) == {}

    def test_ladder_sorted_regardless_of_input_order(self):
        def fake(q):
            return [{"timeFrom": 1000, "timeTo": 2000}] if q <= 750 else []

        # pass ladder out of order — vol must still be the max (750), not the
        # first-seen quantity.
        snap = slot_volume.measure_volumes({"skuId": 1}, [750, 150, 500], fake)
        assert snap[1000]["vol"] == 750


# ── diff_snapshots(): the dedup / increase-only rule ─────────────────


class TestDiff:
    def test_event_only_on_increase(self):
        prev = {1000: {"vol": 500, "to": 2000}, 3000: {"vol": 150, "to": 0}}
        curr = {
            1000: {"vol": 1500, "to": 2000},  # 500 -> 1500  → +1000
            3000: {"vol": 150, "to": 0},       # unchanged    → nothing
            5000: {"vol": 750, "to": 0},       # new (0->750) → +750
        }
        by = {e["slot_from"]: e for e in slot_volume.diff_snapshots(prev, curr)}
        assert by[1000]["freed"] == 1000 and by[1000]["total"] == 1500
        assert 3000 not in by  # dedup: same volume → no event
        assert by[5000]["freed"] == 750 and by[5000]["total"] == 750
        assert len(by) == 2

    def test_decrease_and_vanish_ignored(self):
        prev = {1: {"vol": 1500, "to": 0}, 2: {"vol": 500, "to": 0}}
        curr = {1: {"vol": 500, "to": 0}}  # 1 shrank, 2 disappeared
        assert slot_volume.diff_snapshots(prev, curr) == []

    def test_same_volume_ten_minutes_one_event(self):
        # A slot sitting at 2000 across many polls must fire exactly once —
        # at the poll where it first reached 2000, never again.
        seed = {7: {"vol": 0, "to": 0}}
        first = {7: {"vol": 1500, "to": 0}}
        assert len(slot_volume.diff_snapshots(seed, first)) == 1   # appeared
        # subsequent identical polls produce nothing
        assert slot_volume.diff_snapshots(first, first) == []
        assert slot_volume.diff_snapshots(first, dict(first)) == []


# ── is_unreliable_drop(): the transient-glitch guard (anti-spike) ────


class TestUnreliableDrop:
    def _snap(self, n):
        return {i: {"vol": 150, "to": 0} for i in range(n)}

    def test_big_drop_flagged(self):
        # 50 -> 5 slot in one minute is a transient hiccup, not real.
        assert slot_volume.is_unreliable_drop(self._snap(50), self._snap(5)) is True

    def test_empty_response_flagged(self):
        assert slot_volume.is_unreliable_drop(self._snap(40), {}) is True

    def test_normal_shrink_not_flagged(self):
        # losing a couple of slots (people booking) is legitimate.
        assert slot_volume.is_unreliable_drop(self._snap(50), self._snap(48)) is False

    def test_growth_not_flagged(self):
        assert slot_volume.is_unreliable_drop(self._snap(30), self._snap(45)) is False

    def test_small_prev_not_flagged(self):
        # with a tiny prior snapshot the heuristic must not fire (no baseline).
        assert slot_volume.is_unreliable_drop(self._snap(5), self._snap(1)) is False

    def test_none_prev_not_flagged(self):
        assert slot_volume.is_unreliable_drop(None, self._snap(50)) is False


# ── format_volume_digest(): the design-C table ───────────────────────


class TestFormat:
    def test_columns_and_values(self):
        rows = [
            {"day": "27-iyun", "hajm": 18400, "d30": 1200, "slot": 14, "hozir": 12},
            {"day": "30-iyun", "hajm": 4200, "d30": 0, "slot": 3, "hozir": 3},
        ]
        txt = slot_volume.format_volume_digest("KrossFit", _ms(2026, 6, 20, 14, 30), rows)
        assert "KrossFit" in txt
        assert "27-iyun" in txt
        assert "18 400" in txt     # space thousands separator
        assert "+1 200" in txt     # +30daq with sign
        assert "hozir" in txt
        # JAMI footer sums the columns.
        assert "JAMI" in txt
        assert "22 600" in txt     # 18400 + 4200

    def test_zero_delta_shows_zero_not_plus(self):
        rows = [{"day": "30-iyun", "hajm": 4200, "d30": 0, "slot": 3, "hozir": 3}]
        txt = slot_volume.format_volume_digest("Shop", _ms(2026, 6, 20), rows)
        # the +30daq cell for a no-change day is "0", not "+0".
        assert "+0" not in txt
