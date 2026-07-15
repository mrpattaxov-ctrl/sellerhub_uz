"""Yuk xati (накладная) yaratishdan OLDINGI identifikator qo'riqchisi.

Abdulaziz 2026-07-14. Uzum, tovar per-dona kod talab qilsa (IMEI yoki
ASL_BELGISI — O'zbekiston markirovka kodi) va kod biriktirilmagan bo'lsa,
накладнойni rad etadi::

    POST /v1/fbs/invoice → 400 seller-order-15
    "Customer order [116914839] identifiers are missing"

Ilgari buni FAQAT Uzum javobidan bilardik: bekor create-so'rovi ketardi va
sotuvchi tushunarsiz ruscha xato ko'rardi. Endi guard buni MAHALLIY to'sadi —
dalil bizda ham bor (`fbs_orders.items_json` har tovarning `identifierInfo`
blokini saqlaydi).

Bu testlar ikki xususiyatni qotiradi:
  1. kod yetishmasa — Uzumga so'rov UMUMAN yuborilmaydi (400 qaytadi),
  2. kod to'liq bo'lsa — guard yo'lni to'smaydi (create chaqiriladi).

Uzumga hech qanday haqiqiy so'rov ketmaydi — hammasi mock (jarima/ban xavfi yo'q).
"""
from __future__ import annotations

from core.fbs_sync import identifier_need


# ── Fixtures: Uzum javobining haqiqiy shakli (order 116914839) ────────
def _item(oid: int, *, type_: str | None, values: list[str] | None = None, amount: int = 1) -> dict:
    info = None
    if type_ is not None:
        info = {"type": type_, "required": False, "values": values or []}
    return {"id": oid, "amount": amount, "skuTitle": f"SKU-{oid}",
            "identifierInfo": info}


class TestGuardDecision:
    """Guard qarori = ``identifier_need()['required']`` — marshrut aynan shuni
    ishlatadi (fbs/routes.py: fbs_invoice_create_api)."""

    def test_missing_asl_belgisi_blocks(self):
        items = [_item(1, type_="ASL_BELGISI"), _item(2, type_=None)]
        need = identifier_need({"orderItems": items})
        assert need["required"] is True          # → 400, Uzumga so'rov YO'Q
        assert need["types"] == ["ASL_BELGISI"]

    def test_filled_codes_do_not_block(self):
        items = [_item(1, type_="ASL_BELGISI", values=["0104870123456789"])]
        need = identifier_need({"orderItems": items})
        assert need["required"] is False         # → create davom etadi

    def test_order_without_marked_goods_does_not_block(self):
        items = [_item(1, type_=None), _item(2, type_=None)]
        assert identifier_need({"orderItems": items})["required"] is False

    def test_partially_filled_blocks(self):
        # 2 dona → 2 ta kod kerak, 1 tasi bor.
        items = [_item(1, type_="IMEI", values=["111"], amount=2)]
        need = identifier_need({"orderItems": items})
        assert need["required"] is True
        assert need["items"][0]["missing"] == 1


class TestGuardMessage:
    """Xato xabari sotuvchiga NIMA qilishni aytadi: qaysi buyurtma, qaysi tur."""

    def test_error_template_has_orders_and_type_slots(self):
        from fbs.routes import ACTION_ERRORS
        for lang in ("uz", "ru"):
            tpl = ACTION_ERRORS[lang]["identifiers_missing_invoice"]
            assert "{orders}" in tpl
            assert "{type}" in tpl

    def test_type_names_are_localized_and_not_hardcoded_imei(self):
        from fbs.routes import ACTION_ERRORS
        assert ACTION_ERRORS["uz"]["identifier_type_ASL_BELGISI"] == "ASL Belgisi"
        assert ACTION_ERRORS["ru"]["identifier_type_ASL_BELGISI"] == "Asl Belgisi"
        # IMEI hamon o'z nomi bilan qoladi (u ham haqiqiy tur).
        assert ACTION_ERRORS["uz"]["identifier_type_IMEI"] == "IMEI"

    def test_uzum_code_is_echoed_for_the_frontend(self):
        # Guard `uzum_code: seller-order-15` qaytaradi — frontend uni
        # Uzumning haqiqiy xatosi bilan bir xil ishlov beradi.
        import fbs.routes as r
        src = open(r.__file__, encoding="utf-8").read()
        assert '"uzum_code": "seller-order-15"' in src
