"""Mocked tests for the slot-grabber pure core (Faza 2D — per-накладной grab).

The grabber asks each waiting draft for ITS OWN available slots (time-slot/get),
then books the earliest slot that falls on the chosen target day. Uzum only
returns slots the накладной can actually use, so dim-group/size are implicitly
correct — the only pure decision left is "pick the earliest slot on target_day".
These tests pin `pick_slot_for_day` (no DB, no network).
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta, date

from postavki import slot_grabber

_TZ = timezone(timedelta(hours=5))


def _ms(y, mo, d, h=10, mi=0) -> int:
    return int(datetime(y, mo, d, h, mi, tzinfo=_TZ).timestamp() * 1000)


D = date(2026, 7, 2)


class TestPickSlotForDay:
    def test_picks_earliest_on_target_day(self):
        slots = [
            {"timeFrom": _ms(2026, 7, 2, 16), "timeTo": _ms(2026, 7, 2, 17)},
            {"timeFrom": _ms(2026, 7, 2, 9), "timeTo": _ms(2026, 7, 2, 10)},  # earliest
            {"timeFrom": _ms(2026, 7, 2, 13), "timeTo": _ms(2026, 7, 2, 14)},
        ]
        assert slot_grabber.pick_slot_for_day(slots, D) == _ms(2026, 7, 2, 9)

    def test_ignores_other_days(self):
        slots = [
            {"timeFrom": _ms(2026, 7, 1, 8), "timeTo": 0},   # day before
            {"timeFrom": _ms(2026, 7, 3, 8), "timeTo": 0},   # day after
            {"timeFrom": _ms(2026, 7, 2, 18), "timeTo": 0},  # target (only match)
        ]
        assert slot_grabber.pick_slot_for_day(slots, D) == _ms(2026, 7, 2, 18)

    def test_none_when_target_day_absent(self):
        slots = [{"timeFrom": _ms(2026, 7, 5, 8), "timeTo": 0}]
        assert slot_grabber.pick_slot_for_day(slots, D) is None

    def test_none_on_empty(self):
        assert slot_grabber.pick_slot_for_day([], D) is None

    def test_skips_garbage_entries(self):
        slots = [{"timeTo": 123}, {"foo": "bar"}, {"timeFrom": _ms(2026, 7, 2, 11), "timeTo": 0}]
        assert slot_grabber.pick_slot_for_day(slots, D) == _ms(2026, 7, 2, 11)

    def test_late_night_slot_counts_for_its_tashkent_day(self):
        # 23:30 Tashkent on Jul 2 must still count as Jul 2 (not roll over in UTC).
        slots = [{"timeFrom": _ms(2026, 7, 2, 23, 30), "timeTo": 0}]
        assert slot_grabber.pick_slot_for_day(slots, D) == _ms(2026, 7, 2, 23, 30)


# ── process_plan(): the live/dry booking path (mutational — pin before live) ──


def _plan(**kw):
    base = {"id": 1, "shop_uzum_id": "51948", "invoice_id": 99, "invoice_number": "N1",
            "draft_size": 10, "target_day": D, "pool_source": "FULLFILMENT", "stock_id": 34}
    base.update(kw)
    return base


class TestProcessPlan:
    def _wire(self, monkeypatch, *, slots, set_ret=None, set_raises=None):
        calls = {"set": [], "mark": []}
        monkeypatch.setattr(slot_grabber.client, "invoice_time_slots", lambda *a, **k: slots)

        def fake_set(shop, inv, tf, stock, pool):
            calls["set"].append((shop, inv, tf, stock, pool))
            if set_raises:
                raise set_raises
            return set_ret

        monkeypatch.setattr(slot_grabber.client, "set_time_slot", fake_set)
        monkeypatch.setattr(slot_grabber, "_mark_plan",
                            lambda pid, status, **k: calls["mark"].append((pid, status, k)))
        return calls

    def test_live_books_matching_slot(self, monkeypatch):
        tf = _ms(2026, 7, 2, 9)
        calls = self._wire(monkeypatch, slots=[{"timeFrom": tf, "timeTo": 0}], set_ret={"id": 555})
        r = slot_grabber.process_plan(_plan(), mode="live")
        assert r["booked"] is True
        assert calls["set"] == [("51948", 99, tf, 34, "FULLFILMENT")]   # set_time_slot to'g'ri argument
        assert calls["mark"][0][1] == "booked"

    def test_live_no_slot_does_not_book(self, monkeypatch):
        # faqat boshqa kun sloti → set_time_slot CHAQIRILMAYDI
        calls = self._wire(monkeypatch, slots=[{"timeFrom": _ms(2026, 7, 5, 9), "timeTo": 0}], set_ret={"id": 1})
        r = slot_grabber.process_plan(_plan(), mode="live")
        assert r["matched"] is False
        assert calls["set"] == []

    def test_live_set_error_marks_failed(self, monkeypatch):
        calls = self._wire(monkeypatch, slots=[{"timeFrom": _ms(2026, 7, 2, 9), "timeTo": 0}],
                           set_raises=RuntimeError("HTTP 429"))
        r = slot_grabber.process_plan(_plan(), mode="live")
        assert r["booked"] is False
        assert calls["mark"][0][1] == "failed"

    def test_live_missing_stock_no_call(self, monkeypatch):
        calls = self._wire(monkeypatch, slots=[{"timeFrom": _ms(2026, 7, 2, 9), "timeTo": 0}], set_ret={"id": 1})
        r = slot_grabber.process_plan(_plan(stock_id=None), mode="live")
        assert calls["set"] == []
        assert calls["mark"][0][1] == "failed"

    def test_dry_never_books(self, monkeypatch):
        calls = self._wire(monkeypatch, slots=[{"timeFrom": _ms(2026, 7, 2, 9), "timeTo": 0}], set_ret={"id": 1})
        r = slot_grabber.process_plan(_plan(), mode="dry")
        assert r["dry"] is True
        assert calls["set"] == []   # dry hech qachon band qilmaydi
