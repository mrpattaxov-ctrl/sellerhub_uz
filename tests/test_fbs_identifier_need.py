"""Identifikator (IMEI / ASL Belgisi) talabini aniqlash — core.fbs_sync.identifier_need.

Kelib chiqishi (Abdulaziz, 2026-07-14): «Магнит браслет» tovari uchun postavka
yaratilmadi —

    POST /v1/fbs/invoice → 400 seller-order-15
    "Customer order [116914839] identifiers are missing"

— holbuki Uzum O'ZI o'sha buyurtma uchun ``identifierRequired: false`` (buyurtma
darajasi) VA ``identifierInfo.required: false`` (tovar darajasi) deb aytgan edi.
Ikkala bayroq ham YOLG'ON. Jonli probe (tools/probe_fbs_identifier_type.py)
tasdiqladi: yagona ishonchli belgi — ``identifierInfo`` blokining MAVJUDLIGI.

Ustiga, kod «IMEI» emas: turi ``identifierInfo.type`` da keladi va bu tovar
uchun u ``ASL_BELGISI`` (O'zbekiston markirovka kodi).

Bu testlar aynan shu ikki xulosani qotiradi. DB yo'q, Uzum yo'q — toza funksiya.
"""
from __future__ import annotations

from core.fbs_sync import identifier_need


# Jonli javobdan olingan haqiqiy shakl (order 116914839, shop 7138).
LIVE_ORDER = {
    "id": 116914839,
    "identifierRequired": False,          # ← Uzum yolg'on aytadi
    "orderItems": [
        {
            "id": 231141537,
            "skuTitle": "SACVOYA-BRASL01",
            "amount": 1,
            "identifierInfo": {
                "type": "ASL_BELGISI",
                "required": False,        # ← bu ham yolg'on
                "values": [],
            },
        },
        {
            "id": 231141538,
            "skuTitle": "SACVOYA-AKG",
            "amount": 1,
            "identifierInfo": None,       # bu tovar hech narsa talab qilmaydi
        },
    ],
}


class TestLiveOrderShape:
    def test_required_despite_uzums_false_flags(self):
        # Uzum ikkala joyda ham "false" dedi, lekin накладнойni RAD ETDI.
        # Biz `identifierInfo` mavjudligiga qaraymiz, bayroqqa emas.
        need = identifier_need(LIVE_ORDER)
        assert need["required"] is True

    def test_type_is_asl_belgisi_not_imei(self):
        need = identifier_need(LIVE_ORDER)
        assert need["types"] == ["ASL_BELGISI"]
        assert "IMEI" not in need["types"]

    def test_only_the_item_with_identifier_info_is_listed(self):
        need = identifier_need(LIVE_ORDER)
        assert len(need["items"]) == 1
        it = need["items"][0]
        assert it["orderItemId"] == 231141537
        assert it["type"] == "ASL_BELGISI"
        assert it["needed"] == 1 and it["filled"] == 0 and it["missing"] == 1


class TestSatisfied:
    def test_filled_values_clear_the_requirement(self):
        order = {
            "orderItems": [{
                "id": 1, "amount": 1,
                "identifierInfo": {"type": "ASL_BELGISI", "required": True,
                                   "values": ["0104870123456789"]},
            }],
        }
        need = identifier_need(order)
        assert need["required"] is False
        assert need["items"][0]["missing"] == 0

    def test_partial_fill_still_required(self):
        # 3 dona tovar → 3 ta kod kerak; 1 tasi kiritilgan.
        order = {
            "orderItems": [{
                "id": 1, "amount": 3,
                "identifierInfo": {"type": "IMEI", "required": True,
                                   "values": ["111"]},
            }],
        }
        need = identifier_need(order)
        assert need["required"] is True
        assert need["items"][0] == {
            "orderItemId": 1, "type": "IMEI",
            "needed": 3, "filled": 1, "missing": 2,
        }

    def test_blank_values_do_not_count_as_filled(self):
        order = {
            "orderItems": [{
                "id": 1, "amount": 1,
                "identifierInfo": {"type": "IMEI", "values": ["  ", ""]},
            }],
        }
        assert identifier_need(order)["required"] is True


class TestNoIdentifiers:
    def test_order_without_identifier_info_needs_nothing(self):
        order = {"orderItems": [{"id": 1, "amount": 2, "identifierInfo": None}]}
        need = identifier_need(order)
        assert need == {"required": False, "types": [], "items": []}

    def test_empty_and_malformed_orders_are_safe(self):
        assert identifier_need({})["required"] is False
        assert identifier_need({"orderItems": []})["required"] is False
        # Kutilmagan shakl — yiqilmasin, shunchaki e'tiborsiz qoldirsin.
        assert identifier_need({"orderItems": ["shovqin", None]})["items"] == []


class TestMixedTypes:
    def test_distinct_types_are_reported_sorted(self):
        order = {
            "orderItems": [
                {"id": 1, "amount": 1,
                 "identifierInfo": {"type": "IMEI", "values": []}},
                {"id": 2, "amount": 1,
                 "identifierInfo": {"type": "ASL_BELGISI", "values": []}},
            ],
        }
        assert identifier_need(order)["types"] == ["ASL_BELGISI", "IMEI"]
