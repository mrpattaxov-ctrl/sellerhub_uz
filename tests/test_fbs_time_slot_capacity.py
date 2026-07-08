"""Slot sig'imi (capacity) pass-through testi (2026-06-15).

Yangi «Split Spotlight» qabul-punkti modali har slotda **bo'sh/jami** sig'im
rozetkasini ko'rsatadi (masalan ``81/90``). Bu raqamlar Uzum'ning time-slot
javobidagi ``capacity`` + ``remainingCapacity`` maydonlaridan keladi (HAR
tahlili 2026-06-15 tasdiqladi). ``fetch_fbs_time_slots`` slotni BUTUN dict
sifatida qaytarishi shart — aks holda frontend rozetkani ko'rsata olmaydi.

Bu test tarmoq qatlamini (``_fbs_orders_request_with_auth``) mock qiladi va
``capacity``/``remainingCapacity``/``uuid`` maydonlari yo'qolmasligini
tekshiradi. (Uzum'ga real so'rov yubormaydi — penalty xavfi yo'q.)
"""
from __future__ import annotations

import core.uzum_openapi as uo


_FAKE_PAYLOAD = {
    "payload": {
        "timeSlots": [
            {
                "uuid": "d53d418d-5123-455a-b95c-a92174c17e66",
                "timeFrom": 1781503200000,
                "timeTo": 1781521200000,
                "capacity": 90,
                "remainingCapacity": 81,
                "enabledLastDayDelivery": True,
            }
        ]
    }
}


def _patch_request(monkeypatch, payload, status=200):
    def _fake(url, token, **kwargs):
        return (payload, status, "{}", None)
    monkeypatch.setattr(uo, "_fbs_orders_request_with_auth", _fake)


class TestTimeSlotCapacityPassthrough:
    def test_capacity_fields_survive(self, monkeypatch):
        _patch_request(monkeypatch, _FAKE_PAYLOAD)
        slots, _url = uo.fetch_fbs_time_slots("tok", "0025797b-851b-42df-8608-c3df34a06258", [111390713])
        assert len(slots) == 1
        s = slots[0]
        # Rozetka uchun zarur uchta maydon o'zgarmay yetib kelishi shart.
        assert s["capacity"] == 90
        assert s["remainingCapacity"] == 81
        assert s["uuid"] == "d53d418d-5123-455a-b95c-a92174c17e66"
        # Vaqt maydonlari ham saqlanadi.
        assert s["timeFrom"] == 1781503200000
        assert s["timeTo"] == 1781521200000

    def test_slot_without_capacity_still_returned(self, monkeypatch):
        # Rasmiy API sig'imni bermasa ham — slot baribir qaytadi (frontend
        # rozetkani yashiradi, lekin vaqt ko'rinadi). Hech narsa yo'qolmaydi.
        payload = {"payload": {"timeSlots": [
            {"timeFrom": 1781503200000, "timeTo": 1781521200000}
        ]}}
        _patch_request(monkeypatch, payload)
        slots, _ = uo.fetch_fbs_time_slots("tok", "dop-uuid-1234", [111390713])
        assert len(slots) == 1
        assert "capacity" not in slots[0]
        assert slots[0]["timeFrom"] == 1781503200000

    def test_empty_order_ids_short_circuits(self, monkeypatch):
        # order_ids bo'sh bo'lsa tarmoqqa chiqmaydi — bo'sh ro'yxat qaytadi.
        called = {"n": 0}
        def _fake(*a, **k):
            called["n"] += 1
            return ({}, 200, "{}", None)
        monkeypatch.setattr(uo, "_fbs_orders_request_with_auth", _fake)
        slots, url = uo.fetch_fbs_time_slots("tok", "dop-uuid-1234", [])
        assert slots == []
        assert called["n"] == 0
