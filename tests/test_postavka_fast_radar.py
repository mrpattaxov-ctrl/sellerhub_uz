"""Mocked tests for the POYGA RADARI — fast detect + on-demand parallel measure.

The radar's contract (no DB, no network): a light 1-request detect runs every
poll; the expensive 15-probe ladder fires ONLY when a new slot appears. These
pin the wiring so the fast path can't silently regress into the slow one (ban
risk) or into missing events:

  1. _measure_parallel() == measure_volumes() (parallel snapshot is correct).
  2. seed poll = no events, a SINGLE request (no ladder).
  3. unchanged slots = ladder SKIPPED (cheap path — this is the ban-safety win).
  4. a NEW slot fires the full parallel ladder; freed events reach _persist_events.
  5. a detect 403 returns an error string (loop cooldown / kill-switch), no crash.
"""
from __future__ import annotations

import pytest

from postavki import slot_volume

_BASE = {"skuId": 1, "purchasePrice": 0}


def _boom(*a, **k):
    raise RuntimeError("HTTP 403: 403")


def _slots(*froms):
    return [{"timeFrom": f, "timeTo": f + 1000} for f in froms]


@pytest.fixture
def radar(monkeypatch):
    """Fresh module state + networkless SKU context + DB replaced by a capture.

    Returns the list that `_persist_events` writes into, so a test can assert
    exactly which freed events were recorded without touching the database.
    """
    slot_volume._SNAPSHOT.clear()
    slot_volume._FAST_SNAPSHOT.clear()
    slot_volume._PIPELINE_LAST_APPLIED.clear()
    monkeypatch.setattr(slot_volume.slot_watch, "_get_sku_context",
                        lambda shop, force=False: (_BASE, "SMALL", "FULLFILMENT"))
    recorded: list[dict] = []
    monkeypatch.setattr(slot_volume, "_persist_events",
                        lambda key, events, pool, dim, on_events: recorded.extend(events))
    return recorded


# ── _measure_parallel(): same result as the sequential ladder ────────


class TestMeasureParallel:
    def test_parallel_matches_sequential(self, monkeypatch):
        # A fits up to 1500, B up to 500 — same shape measure_volumes is pinned on.
        def fake(shop, lines, pool):
            q = lines[0]["quantityToStock"]
            out = []
            if q <= 1500:
                out.append({"timeFrom": 1000, "timeTo": 2000})
            if q <= 500:
                out.append({"timeFrom": 3000, "timeTo": 4000})
            return out

        monkeypatch.setattr(slot_volume.client, "get_time_slots", fake)
        snap = slot_volume._measure_parallel("51948", _BASE, "FULLFILMENT", [150, 500, 750, 1500])
        assert snap[1000]["vol"] == 1500 and snap[1000]["to"] == 2000
        assert snap[3000]["vol"] == 500

    def test_probe_error_propagates(self, monkeypatch):
        # A 403 inside the parallel ladder must surface (so the caller can cooldown).
        monkeypatch.setattr(slot_volume.client, "get_time_slots", _boom)
        with pytest.raises(RuntimeError):
            slot_volume._measure_parallel("51948", _BASE, "FULLFILMENT", [150, 500])


# ── poll_fast_once(): detect → (maybe) measure → record ──────────────


class TestPollFastOnce:
    def test_seed_no_events_single_request(self, monkeypatch, radar):
        calls = []
        def fake(shop, lines, pool):
            calls.append(1)
            return _slots(1000, 2000)
        monkeypatch.setattr(slot_volume.client, "get_time_slots", fake)

        assert slot_volume.poll_fast_once("51948") == (0, None)
        assert len(calls) == 1          # only the light detect — NO ladder
        assert radar == []

    def test_unchanged_skips_ladder(self, monkeypatch, radar):
        calls = []
        def fake(shop, lines, pool):
            calls.append(1)
            return _slots(1000, 2000)
        monkeypatch.setattr(slot_volume.client, "get_time_slots", fake)

        slot_volume.poll_fast_once("51948")   # seed
        assert slot_volume.poll_fast_once("51948") == (0, None)
        assert len(calls) == 2          # 2 detects, NO 15-probe burst (ban-safety)
        assert radar == []

    def test_new_slot_fires_ladder(self, monkeypatch, radar):
        state = {"slots": _slots(1000), "cap": {1000: 1000}}
        ladder_calls = []               # list.append is thread-safe (parallel probes)
        def fake(shop, lines, pool):
            q = lines[0]["quantityToStock"]
            if q == slot_volume._DETECT_Q:
                return list(state["slots"])
            ladder_calls.append(q)
            return [s for s in state["slots"] if state["cap"].get(s["timeFrom"], 0) >= q]
        monkeypatch.setattr(slot_volume.client, "get_time_slots", fake)

        slot_volume.poll_fast_once("51948")           # seed detect
        assert ladder_calls == []
        state["slots"] = _slots(1000, 2000)           # a NEW slot appears
        state["cap"][2000] = 500
        slot_volume.poll_fast_once("51948")
        assert len(ladder_calls) == len(slot_volume.LADDER)   # full 15-probe burst

    def test_freed_event_recorded_after_seed(self, monkeypatch, radar):
        # First measure only SEEDS the volume snapshot (no false spike); the
        # NEXT new slot is what produces a freed event.
        state = {"slots": _slots(1000), "cap": {1000: 1000}}
        def fake(shop, lines, pool):
            q = lines[0]["quantityToStock"]
            if q == slot_volume._DETECT_Q:
                return list(state["slots"])
            return [s for s in state["slots"] if state["cap"].get(s["timeFrom"], 0) >= q]
        monkeypatch.setattr(slot_volume.client, "get_time_slots", fake)

        slot_volume.poll_fast_once("51948")                          # seed detect
        state["slots"] = _slots(1000, 2000); state["cap"][2000] = 500
        slot_volume.poll_fast_once("51948")                          # measure #1 = volume seed
        assert radar == []                                           # seed → no event yet
        state["slots"] = _slots(1000, 2000, 3000); state["cap"][3000] = 700
        slot_volume.poll_fast_once("51948")                          # measure #2 → diff → event
        assert any(e["slot_from"] == 3000 and e["freed"] == 700 for e in radar)

    def test_detect_403_is_killswitch(self, monkeypatch, radar):
        monkeypatch.setattr(slot_volume.client, "get_time_slots", _boom)
        n, err = slot_volume.poll_fast_once("51948")
        assert n == 0
        assert err and "403" in err          # error string → loop cooldown fires
        assert radar == []


# ── poll_deep_parallel_once(): NO detect gate — measure capacity every cycle ──
# The test-mode (POSTAVKA_DEEP_PARALLEL=1). Unlike the radar, it fires the full
# ladder EVERY poll, so a slot whose timeFrom is unchanged but whose CAPACITY
# grows (1→600 freeing) is caught — the radar's detect gate skips exactly that.


class TestPollDeepParallelOnce:
    def test_seed_fires_full_ladder_no_events(self, monkeypatch, radar):
        calls = []
        def fake(shop, lines, pool):
            calls.append(lines[0]["quantityToStock"])
            return _slots(1000)                  # one slot, fits every probe
        monkeypatch.setattr(slot_volume.client, "get_time_slots", fake)

        assert slot_volume.poll_deep_parallel_once("51948") == (0, None)
        assert len(calls) == len(slot_volume.LADDER)   # full ladder EVERY poll (no gate)
        assert radar == []                             # seed → no event

    def test_catches_growth_on_already_visible_slot(self, monkeypatch, radar):
        # THE point of deep-parallel: same timeFrom present in both snapshots,
        # capacity grows 50→600. The radar (detect on timeFrom) would skip it;
        # deep-parallel re-measures and emits the freed event.
        cap = {1000: 50}
        def fake(shop, lines, pool):
            q = lines[0]["quantityToStock"]
            return [s for s in _slots(1000) if cap[s["timeFrom"]] >= q]
        monkeypatch.setattr(slot_volume.client, "get_time_slots", fake)

        slot_volume.poll_deep_parallel_once("51948")    # seed: vol=50
        assert radar == []
        cap[1000] = 600                                 # SAME slot grows (no new timeFrom)
        n, err = slot_volume.poll_deep_parallel_once("51948")
        assert err is None
        assert any(e["slot_from"] == 1000 and e["freed"] == 550 and e["total"] == 600
                   for e in radar)

    def test_probe_403_returns_error(self, monkeypatch, radar):
        monkeypatch.setattr(slot_volume.client, "get_time_slots", _boom)
        n, err = slot_volume.poll_deep_parallel_once("51948")
        assert n == 0 and err and "403" in err
        assert radar == []


# ── poll_pipeline_measure(): staggered measures with an ordering guard ────────
# The pipeline test-mode fires a fresh full-ladder measure every ~0.2s WITHOUT
# waiting, so measures finish out of order. The guard keys on started_at: only
# the newest measure is applied; a late-arriving older one is dropped so it can't
# overwrite the snapshot and emit a phantom event.


class TestPollPipelineMeasure:
    def test_growth_event(self, monkeypatch, radar):
        cap = {1000: 50}
        def fake(shop, lines, pool):
            q = lines[0]["quantityToStock"]
            return [s for s in _slots(1000) if cap[s["timeFrom"]] >= q]
        monkeypatch.setattr(slot_volume.client, "get_time_slots", fake)

        slot_volume.poll_pipeline_measure("51948", started_at=1.0)   # seed vol=50
        assert radar == []
        cap[1000] = 600
        slot_volume.poll_pipeline_measure("51948", started_at=2.0)   # 50->600
        assert any(e["slot_from"] == 1000 and e["freed"] == 550 for e in radar)

    def test_stale_measure_dropped(self, monkeypatch, radar):
        # A NEWER measure applied first; a LATE OLDER one must be IGNORED (no
        # snapshot overwrite, no phantom event) — the core ordering guarantee.
        cap = {1000: 100}
        def fake(shop, lines, pool):
            q = lines[0]["quantityToStock"]
            return [s for s in _slots(1000) if cap[s["timeFrom"]] >= q]
        monkeypatch.setattr(slot_volume.client, "get_time_slots", fake)

        slot_volume.poll_pipeline_measure("51948", started_at=1.0)   # seed=100
        cap[1000] = 600
        slot_volume.poll_pipeline_measure("51948", started_at=3.0)   # newest: 100->600
        assert any(e["freed"] == 500 for e in radar)
        radar.clear()
        cap[1000] = 900
        # older measure (started_at=2.0 < 3.0) arrives late → dropped
        n, err = slot_volume.poll_pipeline_measure("51948", started_at=2.0)
        assert n == 0 and err is None
        assert radar == []                              # no phantom event
        assert slot_volume._SNAPSHOT["51948"][1000]["vol"] == 600   # snapshot intact

    def test_probe_403_returns_error(self, monkeypatch, radar):
        monkeypatch.setattr(slot_volume.client, "get_time_slots", _boom)
        n, err = slot_volume.poll_pipeline_measure("51948", started_at=1.0)
        assert n == 0 and err and "403" in err
        assert radar == []
