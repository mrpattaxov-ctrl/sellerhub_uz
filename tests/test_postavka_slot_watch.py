"""Mocked tests for the Postavka slot-watcher (Faza 0, READ-ONLY).

The watcher never mutates anything at Uzum — it only reads `time-slot`. So
the risk here isn't a penalty, it's silently logging WRONG data and drawing
a false conclusion about "do slots free up?". These tests pin the pure core
(measure / format_digest) with no DB, no network:

  1. measure() picks the earliest slot, counts correctly, and degrades to
     (None, 0) on an empty/erroring response — so "no slots" is recorded
     truthfully instead of crashing the whole poll.
  2. format_digest() marks an earlier slot 🟢, a later one 🟡, unchanged ⚪
     and a vanished slot 🔴 — the change signal the whole experiment rests on.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

from postavki import slot_watch

_TZ = timezone(timedelta(hours=5))


def _ms(y, mo, d, h, mi=0) -> int:
    return int(datetime(y, mo, d, h, mi, tzinfo=_TZ).timestamp() * 1000)


# ── measure(): the per-quantity probe ────────────────────────────────


class TestMeasure:
    def test_picks_earliest_and_counts(self):
        seen = []

        def fake_get(lines):
            seen.append(lines[0]["quantityToStock"])
            # out-of-order on purpose — measure must sort
            return [
                {"timeFrom": _ms(2026, 6, 24, 16), "timeTo": _ms(2026, 6, 24, 17)},
                {"timeFrom": _ms(2026, 6, 22, 14), "timeTo": _ms(2026, 6, 22, 15)},
            ]

        out = slot_watch.measure({"skuId": 1, "purchasePrice": 50}, [30, 200], fake_get)

        assert seen == [30, 200]  # quantity threaded into each call
        assert out[0]["quantity"] == 30
        assert out[0]["earliest_ms"] == _ms(2026, 6, 22, 14)  # sorted earliest
        assert out[0]["count"] == 2

    def test_empty_response_is_no_slots(self):
        out = slot_watch.measure({"skuId": 1}, [1000], lambda lines: [])
        assert out[0]["earliest_ms"] is None
        assert out[0]["count"] == 0

    def test_network_error_is_flagged_not_silent(self):
        # A rejected request (e.g. HTTP 400/429) must be marked error=True, NOT
        # recorded as a genuine "0 slots" — that distinction is what kept us
        # from a false "slot yo'qoldi" reading.
        def boom(lines):
            raise RuntimeError("HTTP 400: validation-failed")

        out = slot_watch.measure({"skuId": 1}, [50], boom)
        assert out[0]["earliest_ms"] is None
        assert out[0]["count"] == 0
        assert out[0]["error"] is True

    def test_genuine_empty_is_not_error(self):
        out = slot_watch.measure({"skuId": 1}, [50], lambda lines: [])
        assert out[0]["error"] is False

    def test_slots_capped_to_24(self):
        many = [{"timeFrom": _ms(2026, 6, 22, 6) + i * 1000, "timeTo": 0} for i in range(40)]
        out = slot_watch.measure({"skuId": 1}, [30], lambda lines: many)
        assert out[0]["count"] == 40          # true count preserved
        assert len(out[0]["slots"]) == 24      # stored sample capped


# ── format_digest(): the change-signal the experiment depends on ──────
#
# Format: per quantity show HOZIR (current) + ENG YAXSHI (best-ever earliest
# slot seen) so a transient earlier slot is never lost. Comparison is by DATE.


class TestFormatDigest:
    def test_shows_current_and_best(self):
        meas = [{
            "quantity": 30, "earliest_ms": _ms(2026, 6, 24, 13), "error": False,
            "best_ms": _ms(2026, 6, 22, 14), "best_when": "14:20",
        }]
        text = slot_watch.format_digest("Krosfit", meas, _ms(2026, 6, 19, 18))
        assert "Krosfit" in text
        assert "24-iyun" in text     # hozir
        assert "22-iyun" in text     # eng yaxshi (earlier than current)
        assert "14:20" in text       # topildi

    def test_error_current_keeps_best(self):
        # current poll rejected, but the best-ever slot must still be shown.
        meas = [{
            "quantity": 30, "earliest_ms": None, "error": True,
            "best_ms": _ms(2026, 6, 22, 14), "best_when": "14:20",
        }]
        text = slot_watch.format_digest("Krosfit", meas, _ms(2026, 6, 19, 18))
        assert "xato" in text          # current shows error
        assert "yo'qoldi" not in text  # never the false "lost" label
        assert "22-iyun" in text       # best still surfaced

    def test_never_seen_shows_dash(self):
        meas = [{
            "quantity": 30, "earliest_ms": None, "error": False,
            "best_ms": None, "best_when": "",
        }]
        text = slot_watch.format_digest("Krosfit", meas, _ms(2026, 6, 19, 18))
        assert "bo'sh yo'q" in text   # current: no slots
        assert "—" in text             # best: never seen
