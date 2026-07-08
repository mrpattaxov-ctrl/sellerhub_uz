"""Tests for O6 — FBS/DBS SKU stock management (2026-06-02).

Two new pieces in ``core.uzum_openapi``:

* ``fetch_fbs_sku_stocks`` — GET /v3/fbs/sku/stocks paginated (read; needs
  SKU_READ). v2 GET was deprecated by Uzum → 404 since ~2026-07.
* ``update_fbs_sku_stocks`` — POST /v2/fbs/sku/stocks (MUTATION; SKU_UPDATE).

The POST writes the seller's real stock at Uzum, so — per the
action-endpoint policy — we prove with mocks BEFORE prod that the mutation
body is built EXACTLY from the rows passed (descriptive fields round-tripped,
amount coerced to a non-negative int) and that it is SKIPPED entirely when
nothing valid remains. No Postgres, no Uzum HTTP, no real stock is touched.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from core import uzum_openapi
from core.uzum_openapi import (
    fetch_fbs_sku_stocks,
    fetch_fbs_sku_stocks_page,
    update_fbs_sku_stocks,
)


class _Boom(Exception):
    """Sentinel for whatever Uzum/HTTP error bubbles up."""


class TestFetchSkuStocks:
    """``fetch_fbs_sku_stocks`` — parsing + error handling."""

    def test_parses_payload_sku_amount_list(self):
        # A single short page (< size=100) drains in one request.
        body = {"payload": {"skuAmountList": [
            {"skuId": 10312944, "skuTitle": "A", "amount": 0, "fbsLinked": True},
            {"skuId": 10312791, "skuTitle": "B", "amount": 5, "dbsLinked": False},
            "not-a-dict",  # dropped, not crash
        ]}}
        with patch.object(uzum_openapi, "_fbs_orders_request_with_auth",
                          return_value=(body, 200, "", None)) as mock_req:
            skus, url = fetch_fbs_sku_stocks("tok", fail_fast=True)

        assert [s["skuId"] for s in skus] == [10312944, 10312791]
        # Migrated off the deprecated v2 GET (404) onto paginated v3.
        assert "/v3/fbs/sku/stocks" in url
        assert mock_req.call_count == 1
        args, kwargs = mock_req.call_args
        assert kwargs.get("method") == "GET"
        assert kwargs.get("fail_fast") is True
        assert "/v3/fbs/sku/stocks" in args[0]
        assert "page=0" in args[0] and "size=100" in args[0]

    def test_drains_all_pages_until_short_page(self):
        # A full page (100 rows) must trigger a follow-up request; the loop
        # stops on the first short page. Two full pages + one short = 3 calls.
        full = [{"skuId": i, "amount": 0} for i in range(100)]
        tail = [{"skuId": 9001, "amount": 3}, {"skuId": 9002, "amount": 4}]
        pages = [
            ({"payload": {"skuAmountList": full}}, 200, "", None),
            ({"payload": {"skuAmountList": full}}, 200, "", None),
            ({"payload": {"skuAmountList": tail}}, 200, "", None),
        ]
        with patch.object(uzum_openapi, "_fbs_orders_request_with_auth",
                          side_effect=pages) as mock_req:
            skus, _ = fetch_fbs_sku_stocks("tok", fail_fast=True)

        assert mock_req.call_count == 3
        assert len(skus) == 202
        assert skus[-1]["skuId"] == 9002
        # page index advances 0 → 1 → 2 across the calls.
        pages_asked = [c.args[0] for c in mock_req.call_args_list]
        assert "page=0" in pages_asked[0]
        assert "page=1" in pages_asked[1]
        assert "page=2" in pages_asked[2]

    def test_page_fetches_one_page_and_clamps_size(self):
        # The interactive grid pages live — one request, size clamped to Uzum's
        # hard ceiling of 100 (asking for more returns 400 illegal-argument).
        body = {"payload": {"skuAmountList": [{"skuId": 1, "amount": 0}]}}
        with patch.object(uzum_openapi, "_fbs_orders_request_with_auth",
                          return_value=(body, 200, "", None)) as mock_req:
            rows, url = fetch_fbs_sku_stocks_page("tok", page=2, size=999, fail_fast=True)

        assert [r["skuId"] for r in rows] == [1]
        assert mock_req.call_count == 1        # exactly one page, no draining loop
        called_url = mock_req.call_args.args[0]
        assert "/v3/fbs/sku/stocks" in called_url
        assert "page=2" in called_url and "size=100" in called_url  # clamped 999→100

    def test_empty_for_unexpected_shapes(self):
        for body in (
            {"payload": {"skuAmountList": {"not": "a list"}}},
            {"payload": {"skuAmountList": None}},
            {"payload": {}},
            {"payload": None},
            {},
            ["bare-list"],
            None,
        ):
            with patch.object(uzum_openapi, "_fbs_orders_request_with_auth",
                              return_value=(body, 200, "", None)):
                skus, _ = fetch_fbs_sku_stocks("tok")
            assert skus == [], f"expected [] for payload {body!r}"

    def test_non_2xx_routes_through_error_raiser(self):
        # 403 (token lacks SKU_READ) must surface via the shared error path,
        # not be parsed as an empty SKU list.
        with patch.object(uzum_openapi, "_fbs_orders_request_with_auth",
                          return_value=({"errors": []}, 403, "forbidden", None)), \
             patch.object(uzum_openapi, "_raise_uzum_error",
                          side_effect=_Boom) as mock_raise:
            with pytest.raises(_Boom):
                fetch_fbs_sku_stocks("tok")
        assert mock_raise.called


class TestUpdateSkuStocks:
    """``update_fbs_sku_stocks`` — the MUTATION. The body must be built
    EXACTLY from valid rows, and the request must NEVER fire empty."""

    def test_builds_exact_body_and_roundtrips_fields(self):
        rows = [
            # Full GET row — extra fields (fbsAllowed/sellerSkuCode) are
            # dropped; amount string coerces; int barcode stringifies.
            {"skuId": 111, "skuTitle": "SKU-A", "productTitle": "Prod A",
             "barcode": 1000103129447, "amount": "12",
             "fbsLinked": True, "dbsLinked": False,
             "fbsAllowed": True, "sellerSkuCode": "X"},
            # ``id`` is accepted as a skuId alias; negative amount clamps to 0.
            {"id": 222, "skuTitle": "SKU-B", "productTitle": "Prod B",
             "barcode": "999", "amount": -3,
             "fbsLinked": False, "dbsLinked": True},
        ]
        with patch.object(uzum_openapi, "_fbs_orders_request_with_auth",
                          return_value=({"payload": {}}, 200, "", None)) as mock_req:
            result, url = update_fbs_sku_stocks("tok", sku_amounts=rows, fail_fast=True)

        assert result == {}
        assert "/v2/fbs/sku/stocks" in url
        args, kwargs = mock_req.call_args
        assert kwargs.get("method") == "POST"
        assert kwargs.get("fail_fast") is True
        assert kwargs["json_body"] == {"skuAmountList": [
            {"skuId": 111, "skuTitle": "SKU-A", "productTitle": "Prod A",
             "barcode": "1000103129447", "amount": 12,
             "fbsLinked": True, "dbsLinked": False},
            {"skuId": 222, "skuTitle": "SKU-B", "productTitle": "Prod B",
             "barcode": "999", "amount": 0,
             "fbsLinked": False, "dbsLinked": True},
        ]}

    def test_drops_rows_without_a_usable_sku_id(self):
        rows = [
            {"skuTitle": "no-id", "amount": 5},   # no skuId/id → dropped
            {"skuId": "abc", "amount": 5},        # non-int skuId → dropped
            {"skuId": 333, "amount": 7},          # kept
        ]
        with patch.object(uzum_openapi, "_fbs_orders_request_with_auth",
                          return_value=({"payload": {}}, 200, "", None)) as mock_req:
            update_fbs_sku_stocks("tok", sku_amounts=rows)
        body = mock_req.call_args.kwargs["json_body"]
        assert [r["skuId"] for r in body["skuAmountList"]] == [333]

    def test_empty_raises_and_skips_request(self):
        # Nothing valid to send → ValueError, and the HTTP layer is NEVER
        # touched (no empty/garbage mutation reaches Uzum).
        for rows in ([], [{"amount": 1}], [{"skuId": "x"}], None):
            with patch.object(uzum_openapi, "_fbs_orders_request_with_auth") as mock_req:
                with pytest.raises(ValueError):
                    update_fbs_sku_stocks("tok", sku_amounts=rows)
            mock_req.assert_not_called()

    def test_non_2xx_routes_through_error_raiser(self):
        # storage-service-05 (validation) etc. must surface via the shared
        # error path, not be swallowed as a success.
        with patch.object(uzum_openapi, "_fbs_orders_request_with_auth",
                          return_value=({"errors": []}, 400, "validation", None)), \
             patch.object(uzum_openapi, "_raise_uzum_error",
                          side_effect=_Boom) as mock_raise:
            with pytest.raises(_Boom):
                update_fbs_sku_stocks("tok", sku_amounts=[{"skuId": 1, "amount": 2}])
        assert mock_raise.called


# NOTE (2026-07-03): TestPatchedSkuAmounts removed — the «Ombor» page is
# live-only now (fbs_sku_stock_cache + _patched_sku_amounts deleted with it).
