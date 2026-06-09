"""Tests for O6 — FBS/DBS SKU stock management (2026-06-02).

Two new pieces in ``core.uzum_openapi``:

* ``fetch_fbs_sku_stocks`` — GET /v2/fbs/sku/stocks (read; needs SKU_READ).
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
    update_fbs_sku_stocks,
)


class _Boom(Exception):
    """Sentinel for whatever Uzum/HTTP error bubbles up."""


class TestFetchSkuStocks:
    """``fetch_fbs_sku_stocks`` — parsing + error handling."""

    def test_parses_payload_sku_amount_list(self):
        body = {"payload": {"skuAmountList": [
            {"skuId": 10312944, "skuTitle": "A", "amount": 0, "fbsLinked": True},
            {"skuId": 10312791, "skuTitle": "B", "amount": 5, "dbsLinked": False},
            "not-a-dict",  # dropped, not crash
        ]}}
        with patch.object(uzum_openapi, "_fbs_orders_request_with_auth",
                          return_value=(body, 200, "", None)) as mock_req:
            skus, url = fetch_fbs_sku_stocks("tok", fail_fast=True)

        assert [s["skuId"] for s in skus] == [10312944, 10312791]
        assert "/v2/fbs/sku/stocks" in url
        args, kwargs = mock_req.call_args
        assert kwargs.get("method") == "GET"
        assert kwargs.get("fail_fast") is True
        assert "/v2/fbs/sku/stocks" in args[0]

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
