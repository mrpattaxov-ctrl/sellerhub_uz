"""Avto-slot KARKAS shartnomasi (Bosqich 2) — chok va event seriyalanishi.

Bu testlar arxitektura CHEGARASINI qotiradi: monitoring keyin alohida
serverga ko'chsa, event HTTP/Redis orqali JSON bo'lib uchadi — `to_dict`/
`from_dict` round-trip BUZILMASLIGI shart. Va in-process chok (publish →
consume) ishlashini tasdiqlaydi. DB/tarmoq yo'q — toza.
"""
from __future__ import annotations

import threading
from datetime import date

from postavki.autoslot import bus
from postavki.autoslot.events import SlotFreedEvent
from postavki.autoslot.day import day_of
from postavki.autoslot import monitor


# ── Event shartnomasi: JSON round-trip (tarmoq chegarasi) ────────────


class TestEventContract:
    def test_roundtrip_preserves_all_fields(self):
        ev = SlotFreedEvent(
            pool_source="FULLFILMENT", dim_group="SMALL", day=date(2026, 7, 1),
            free_volume=100, slot_from_ms=1751371200000, slot_to_ms=1751374800000,
            observed_at_ms=1751360000000, source_shop_id="51948",
        )
        back = SlotFreedEvent.from_dict(ev.to_dict())
        assert back == ev

    def test_day_serialized_as_iso_string(self):
        ev = SlotFreedEvent(pool_source="P", dim_group=None, day=date(2026, 7, 5),
                            free_volume=50, slot_from_ms=1)
        d = ev.to_dict()
        assert d["day"] == "2026-07-05"          # JSON-xavfsiz (date emas, str)
        assert d["dim_group"] is None


# ── Chok: in-process publish → consume ───────────────────────────────


class TestBus:
    def test_publish_then_consume_delivers_event(self):
        got: list[SlotFreedEvent] = []
        ev = SlotFreedEvent(pool_source="P", dim_group="SMALL",
                            day=date(2026, 7, 1), free_volume=100, slot_from_ms=1)
        t = threading.Thread(target=bus.consume_forever, args=(got.append,), daemon=True)
        t.start()
        bus.publish(ev)
        bus.stop()
        t.join(timeout=2)
        assert got == [ev]

    def test_handler_exception_does_not_kill_loop(self):
        seen: list[int] = []

        def handler(e):
            seen.append(e.free_volume)
            if e.free_volume == 1:
                raise ValueError("boom")  # birinchi event portlaydi

        t = threading.Thread(target=bus.consume_forever, args=(handler,), daemon=True)
        t.start()
        bus.publish(SlotFreedEvent(pool_source="P", dim_group=None, day=date(2026, 7, 1),
                                   free_volume=1, slot_from_ms=1))
        bus.publish(SlotFreedEvent(pool_source="P", dim_group=None, day=date(2026, 7, 1),
                                   free_volume=2, slot_from_ms=2))
        bus.stop()
        t.join(timeout=2)
        assert seen == [1, 2]  # portlash keyingi event'ni to'xtatmadi


# ── Monitor: volume-event dict → SlotFreedEvent ──────────────────────


class TestMonitorMapping:
    def test_to_event_maps_volume_dict(self):
        ms = 1751371200000  # 2026-07-01 18:00 Tashkent atrofida
        ve = {"slot_from": ms, "slot_to": ms + 3600000, "freed": 150, "total": 500}
        ev = monitor.to_event(ve, pool_source="FULLFILMENT", dim_group="SMALL",
                              source_shop_id="51948")
        assert ev.free_volume == 150
        assert ev.slot_from_ms == ms
        assert ev.day == day_of(ms)
        assert ev.pool_source == "FULLFILMENT" and ev.dim_group == "SMALL"

    def test_publish_freed_skips_rows_without_slot_from(self):
        published: list[SlotFreedEvent] = []
        orig = bus.publish
        bus.publish = lambda e: published.append(e)  # type: ignore
        try:
            n = monitor.publish_freed(
                [{"slot_from": 0, "freed": 50}, {"slot_from": 123, "freed": 50}],
                pool_source="P", dim_group=None,
            )
        finally:
            bus.publish = orig  # type: ignore
        assert n == 1 and len(published) == 1 and published[0].slot_from_ms == 123
