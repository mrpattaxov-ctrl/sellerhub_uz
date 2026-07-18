"""«Новый товар» — 3-QADAM «Свойства» client funksiyalari (mocked, tarmoqsiz).

Nega mocked: `save_filters` HAQIQIY yozadi (mahsulot xususiyatlarini saqlaydi).
Foydalanuvchi qoidasi — action endpoint'larni prodda sinashdan oldin mocked
test yozish. Bu yerda Uzum'ga BITTA ham so'rov ketmaydi: `_req` monkeypatch
qilinadi va faqat QAYSI URL/method/tana ketishi tekshiriladi.

⚠️ URL shakli dalili: jonli read-only probe (2026-07-18):
    GET .../api/assortment/v1/categories/14007/infomodel                → 200
    GET .../api/assortment/v1/attributes/enums?attrCode=filter_1687     → 200
    GET .../api/assortment/v1/public/api/v1/...  → 404 (gateway prefiksni ikkilaydi)
"""
from __future__ import annotations

import pytest

from noviy_tavar import client


@pytest.fixture
def calls(monkeypatch):
    """`_req` ni ushlab, chaqiruvlarni yig'adi; oldindan berilgan javob qaytaradi."""
    recorded = []
    responses = {"next": {}}

    def fake_req(method, url, *, json_body=None, timeout=30, lang=None):
        recorded.append({
            "method": method, "url": url, "json_body": json_body,
            "timeout": timeout, "lang": lang,
        })
        return responses["next"]

    monkeypatch.setattr(client, "_req", fake_req)
    return recorded, responses


# ── attr_enums ───────────────────────────────────────────────────────


def test_attr_enums_hits_assortment_without_public_prefix(calls):
    """⚠️ Gateway `/public/api/v1` ni O'ZI qo'shadi — yo'l prefiksSIZ bo'lsin."""
    recorded, responses = calls
    responses["next"] = {"attributeEnums": []}
    client.attr_enums("filter_1687")
    url = recorded[0]["url"]
    assert url.startswith("https://api.uzum.uz/api/assortment/v1/attributes/enums?")
    assert "attrCode=filter_1687" in url
    assert "/public/api/v1" not in url, "gateway prefiksi ikkilanadi -> 404"
    assert recorded[0]["method"] == "GET"


def test_attr_enums_returns_attribute_enums_list(calls):
    """Javob `attributeEnums[]` — jonli o'lchangan element shakli bilan."""
    recorded, responses = calls
    responses["next"] = {"attributeEnums": [
        {"id": 269154, "code": "filter_value_573874", "value": None,
         "localizedValue": {"uz": "Sintetik kauchuk", "ru": "Синтетический каучук"},
         "isCustom": False},
    ], "metadata": {}}
    rows = client.attr_enums("filter_1687")
    assert isinstance(rows, list) and len(rows) == 1
    assert rows[0]["code"] == "filter_value_573874"
    assert rows[0]["localizedValue"]["uz"] == "Sintetik kauchuk"


def test_attr_enums_defaults_size_and_page(calls):
    """Bandl `sellerSearchEnumValues({attrCode, page:0, size:1000, search:''})`."""
    recorded, responses = calls
    responses["next"] = {"attributeEnums": []}
    client.attr_enums("gender")
    url = recorded[0]["url"]
    assert "page=0" in url and "size=1000" in url and "search=" in url


def test_attr_enums_non_dict_response_is_empty_list(calls):
    """Kutilmagan javob (dict/list emas) -> bo'sh ro'yxat, UI tirik qoladi."""
    recorded, responses = calls
    responses["next"] = "shubhali"
    assert client.attr_enums("x") == []


# ── product_filters ──────────────────────────────────────────────────


def test_product_filters_uses_path_segment_not_query(calls):
    """`filters/product/{id}` — YO'L segmenti (2-qadamdagi `?productId=` EMAS)."""
    recorded, responses = calls
    responses["next"] = {"skuAttributes": [], "sku": []}
    client.product_filters("10945", 3068623)
    url = recorded[0]["url"]
    assert url.endswith("/seller/shop/10945/filters/product/3068623")
    assert "?productId=" not in url
    assert recorded[0]["method"] == "GET"


# ── save_filters (YOZUVCHI) ──────────────────────────────────────────


def test_save_filters_posts_to_filters_product(calls):
    """POST /filters/product/{id} — bandl `editSkuFilters`."""
    recorded, responses = calls
    responses["next"] = {}
    body = {"skuFilters": [], "skuAttributeValues": [{"skuId": 1, "attributes": []}]}
    client.save_filters("10945", 3068623, body)
    assert recorded[0]["method"] == "POST"
    assert recorded[0]["url"].endswith("/seller/shop/10945/filters/product/3068623")
    assert recorded[0]["json_body"] is body
