"""«Ombor» portal READ path — core/fbs_portal_stock.py.

WHY pinned: the stock grid's data source moved from OpenAPI v3 (no search)
to Uzum's internal portal endpoint (HAR «Поставки22+», 2026-07-03). The
normalizer maps portal field names onto the OpenAPI shape the frontend and
the POST-save round-trip expect — a silent rename here breaks saving.
Probe-confirmed API rules (size=20 hard cap, amountFilter enum, unreliable
hasMore) are encoded here so a future edit can't quietly violate them.
"""
from __future__ import annotations

from unittest.mock import patch

from core.fbs_portal_stock import (
    PORTAL_STOCK_PAGE_SIZE,
    _thumb_from_photo,
    fetch_portal_sku_stocks_page,
    normalize_portal_sku,
)


PORTAL_ROW = {
    "id": 8816678,
    "skuTitle": "LUXUZ-RING30-ЧЕРН-16",
    "productTitle": "Кольцо антистресс",
    "barcode": "1000088166789",
    "amount": 7,
    "fbsLinked": True,
    "dbsLinked": False,
    "availableFbs": True,
    "availableDbs": False,
    "photo": {"photo": {
        "800": {"high": "https://images.uzum.uz/x/original.jpg",
                "low": "https://images.uzum.uz/x/t_product_low.jpg"},
        "240": {"high": "https://images.uzum.uz/x/t240.jpg", "low": None},
    }},
}


class TestNormalize:
    def test_field_mapping_matches_openapi_shape(self):
        row = normalize_portal_sku(PORTAL_ROW)
        # The frontend + POST /v2 save round-trip live on these exact names.
        assert row["skuId"] == 8816678
        assert row["skuTitle"] == "LUXUZ-RING30-ЧЕРН-16"
        assert row["barcode"] == "1000088166789"
        assert row["amount"] == 7
        assert row["fbsLinked"] is True and row["dbsLinked"] is False
        assert row["fbsAllowed"] is True and row["dbsAllowed"] is False
        assert row["image"] == "https://images.uzum.uz/x/t240.jpg"

    def test_missing_amount_becomes_zero(self):
        row = normalize_portal_sku({"id": 1, "amount": None})
        assert row["amount"] == 0

    def test_thumb_prefers_small_size_and_survives_garbage(self):
        assert _thumb_from_photo(PORTAL_ROW["photo"]).endswith("t240.jpg")
        assert _thumb_from_photo(None) is None
        assert _thumb_from_photo({"photo": {"999": {"high": "u"}}}) == "u"
        assert _thumb_from_photo({"photo": "garbage"}) is None


class TestFetchPage:
    def _call(self, mock_http, **kw):
        with patch("core.http_client.http_json", mock_http):
            return fetch_portal_sku_stocks_page(
                seller_id=95673, shop_uzum_ids=["5983", "51948"], **kw)

    def test_url_carries_probeconfirmed_params(self):
        seen = {}
        def fake(url, method="GET", **kw):
            seen["url"] = url
            return {"payload": {"skus": [PORTAL_ROW], "hasMore": False}}
        rows, has_more = self._call(fake, page=2, search="кольцо 16")
        u = seen["url"]
        assert "sellerId=95673" in u
        assert "shopIds=5983,51948" in u
        assert "amountFilter=ALL" in u                # default (not in_stock_only)
        assert f"size={PORTAL_STOCK_PAGE_SIZE}" in u  # hard cap 20 (21 → 400)
        assert "page=2" in u
        assert "searchText=%D0%BA%D0%BE%D0%BB%D1%8C%D1%86%D0%BE%2016" in u
        assert rows[0]["skuId"] == 8816678

    def test_linked_true_by_default_is_the_ombor_list(self):
        # `linked=true` = «Ombor» ro'yxatining O'ZAGI: faqat FBS/DBS sxemasiga
        # ulangan SKU. Busiz butun katalog (~2200) keladi VA ombordan
        # o'chirilgan (sxemadan uzilgan) tovar ro'yxatda qolib ketadi — aynan
        # shu bug 2026-07-13 da HAR bilan aniqlangan. Uzum'ning o'z sahifasi
        # ham shu parametrni yuboradi.
        seen = {}
        def fake(url, method="GET", **kw):
            seen["url"] = url
            return {"payload": {"skus": [PORTAL_ROW]}}
        self._call(fake)
        assert "linked=true" in seen["url"]

    def test_linked_false_returns_whole_catalogue(self):
        # «Barcha tovarlar» ko'rinishi — omborga YANGI SKU qo'shish uchun.
        seen = {}
        def fake(url, method="GET", **kw):
            seen["url"] = url
            return {"payload": {"skus": [PORTAL_ROW]}}
        self._call(fake, linked_only=False)
        assert "linked=" not in seen["url"]

    def test_in_stock_only_uses_in_stock_enum(self):
        # «Mavjud» → IN_STOCK (amount>0). SOLD_OUT is NEVER used (it's a
        # curated «manually zeroed» subset, not amount==0 — probe 2026-07-03).
        seen = {}
        def fake(url, method="GET", **kw):
            seen["url"] = url
            return {"payload": {"skus": [PORTAL_ROW]}}
        self._call(fake, in_stock_only=True)
        assert "amountFilter=IN_STOCK" in seen["url"]
        assert "SOLD_OUT" not in seen["url"]

    def test_has_more_ignores_portal_flag_uses_full_page_rule(self):
        # Portal hasMore is ALWAYS False (probe) — a full page means "more".
        full = [dict(PORTAL_ROW, id=i) for i in range(PORTAL_STOCK_PAGE_SIZE)]
        def fake_full(url, **kw):
            return {"payload": {"skus": full, "hasMore": False}}
        rows, has_more = self._call(fake_full)
        assert has_more is True and len(rows) == PORTAL_STOCK_PAGE_SIZE

        def fake_short(url, **kw):
            return {"payload": {"skus": full[:3], "hasMore": False}}
        rows, has_more = self._call(fake_short)
        assert has_more is False and len(rows) == 3

    def test_empty_shop_scope_never_calls_uzum(self):
        # No own shops → empty result WITHOUT a request (a seller-wide call
        # would leak foreign shops' SKUs into the grid).
        def boom(url, **kw):
            raise AssertionError("must not be called")
        with patch("core.http_client.http_json", boom):
            rows, has_more = fetch_portal_sku_stocks_page(
                seller_id=95673, shop_uzum_ids=[])
        assert rows == [] and has_more is False

    def test_rows_without_id_are_dropped(self):
        def fake(url, **kw):
            return {"payload": {"skus": [PORTAL_ROW, {"skuTitle": "no-id"}, "junk"]}}
        rows, _ = self._call(fake)
        assert [r["skuId"] for r in rows] == [8816678]
