"""Tests for the N9 invoice→orders authoritative-membership fix (2026-06-01).

Two new pieces in ``core.uzum_openapi``:

* ``fetch_fbs_invoice_orders`` — thin wrapper over
  ``GET /v1/fbs/invoice/{invoiceId}/orders``. Replaces the old fragile
  joins (``raw_json->>'invoiceNumber'`` in the detail view and
  ``invoice_number LIKE '%suffix'`` in change-pickup) that fired before
  the sync worker had populated those columns.

* ``change_invoice_pickup`` — orchestration that reads the invoice's
  AUTHORITATIVE orders from Uzum, then moves the invoice to a new
  point/slot via ``create_fbs_invoice(update_only=True)``.

``change_invoice_pickup`` fires a real mutation against the seller's Uzum
account, so — per the action-endpoint policy — we prove with mocks BEFORE
prod that the mutation (a) receives exactly the order IDs Uzum reports and
(b) is SKIPPED entirely when there are no orders or the fetch fails. No
Postgres, no Uzum HTTP, no real invoice gets moved.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from core import uzum_openapi
from core.uzum_openapi import (
    fetch_fbs_invoice_orders,
    change_invoice_pickup,
    UzumAPIError,
)


class _Boom(Exception):
    """Sentinel — stands in for whatever Uzum/HTTP layer error bubbles up."""


class TestFetchInvoiceOrders:
    """``fetch_fbs_invoice_orders`` — parsing + error handling."""

    def test_parses_payload_list_and_filters_non_dicts(self):
        body = {"payload": [
            {"orderId": 111, "fullPrice": 39900, "items": [{"skuId": 1}]},
            {"orderId": 222, "fullPrice": 79800, "items": []},
            "not-a-dict",  # must be dropped, not crash
        ]}
        with patch.object(uzum_openapi, "_fbs_orders_request_with_auth",
                          return_value=(body, 200, "", None)) as mock_req:
            orders, url = fetch_fbs_invoice_orders("tok", 327497)

        assert [o["orderId"] for o in orders] == [111, 222]
        assert "/v1/fbs/invoice/327497/orders" in url
        # GET against the SHORT id, not the 12-digit number.
        args, kwargs = mock_req.call_args
        assert kwargs.get("method") == "GET"
        assert "/v1/fbs/invoice/327497/orders" in args[0]

    def test_empty_list_for_unexpected_payload_shapes(self):
        for body in (
            {"payload": {"not": "a list"}},
            {"payload": None},
            {},
            ["bare-list"],
            None,
        ):
            with patch.object(uzum_openapi, "_fbs_orders_request_with_auth",
                              return_value=(body, 200, "", None)):
                orders, _ = fetch_fbs_invoice_orders("tok", 1)
            assert orders == [], f"expected [] for payload {body!r}"

    def test_non_2xx_routes_through_error_raiser(self):
        # A 403 (token doesn't own the invoice) must surface via the
        # shared error path, not be parsed as an empty order list.
        with patch.object(uzum_openapi, "_fbs_orders_request_with_auth",
                          return_value=({"errors": []}, 403, "forbidden", None)), \
             patch.object(uzum_openapi, "_raise_uzum_error",
                          side_effect=_Boom) as mock_raise:
            with pytest.raises(_Boom):
                fetch_fbs_invoice_orders("tok", 327497)
        assert mock_raise.called

    def test_rejects_non_int_invoice_id(self):
        with pytest.raises(RuntimeError):
            fetch_fbs_invoice_orders("tok", "not-a-number")


class TestChangeInvoicePickup:
    """``change_invoice_pickup`` — the mutation wiring. The whole point is
    that ``create_fbs_invoice`` only ever fires with the authoritative
    order IDs, and never fires when it shouldn't."""

    def test_mutation_gets_exactly_the_authoritative_ids(self):
        orders = [
            {"orderId": 111},
            {"orderId": 222},
            {"orderId": None},  # coercion drops these two so a stray
            {"no": "id"},       # null never reaches the mutation body
        ]
        with patch.object(uzum_openapi, "fetch_fbs_invoice_orders",
                          return_value=(orders, "orders-url")) as mock_fetch, \
             patch.object(uzum_openapi, "create_fbs_invoice",
                          return_value=({"id": 327497}, "create-url")) as mock_create:
            invoice, url = change_invoice_pickup(
                "tok", invoice_id=327497, seller_id=95673,
                drop_off_point_uuid="dop-1", time_slot_uuid="slot-1",
            )

        assert invoice == {"id": 327497}
        assert url == "create-url"
        mock_fetch.assert_called_once()
        # The mutation receives EXACTLY the IDs Uzum linked (None/invalid
        # filtered) + update_only=True (PATCH the existing invoice, don't
        # create a new one) + the new point/slot/seller.
        cargs, ckwargs = mock_create.call_args
        assert cargs[0] == "tok"
        assert ckwargs["order_ids"] == [111, 222]
        assert ckwargs["update_only"] is True
        assert ckwargs["drop_off_point_uuid"] == "dop-1"
        assert ckwargs["time_slot_uuid"] == "slot-1"
        assert ckwargs["seller_id"] == 95673
        # fail_fast defaults False (worker-safe); only the interactive route opts in.
        assert ckwargs.get("fail_fast") is False

    def test_fail_fast_threads_through_to_both_uzum_calls(self):
        # Interactive callers pass fail_fast=True so a per-token burst-429
        # fails in one round-trip instead of stalling the page on the shared
        # session's 60/120/180s backoff. BOTH the membership read and the
        # mutation must honour it — otherwise change-pickup → list reload can
        # freeze the UI for minutes (the bug this guards against).
        orders = [{"orderId": 111}]
        with patch.object(uzum_openapi, "fetch_fbs_invoice_orders",
                          return_value=(orders, "orders-url")) as mock_fetch, \
             patch.object(uzum_openapi, "create_fbs_invoice",
                          return_value=({"id": 1}, "create-url")) as mock_create:
            change_invoice_pickup(
                "tok", invoice_id=1, seller_id=1,
                drop_off_point_uuid="d", time_slot_uuid="s",
                fail_fast=True,
            )
        _, fkwargs = mock_fetch.call_args
        _, ckwargs = mock_create.call_args
        assert fkwargs.get("fail_fast") is True
        assert ckwargs.get("fail_fast") is True

    def test_no_orders_raises_and_skips_mutation(self):
        # Empty membership → ValueError, and create_fbs_invoice must NOT
        # fire (an empty orderIds body would 400 at Uzum anyway, but the
        # real risk is firing a malformed mutation at all).
        with patch.object(uzum_openapi, "fetch_fbs_invoice_orders",
                          return_value=([], "orders-url")), \
             patch.object(uzum_openapi, "create_fbs_invoice") as mock_create:
            with pytest.raises(ValueError):
                change_invoice_pickup(
                    "tok", invoice_id=1, seller_id=1,
                    drop_off_point_uuid="d", time_slot_uuid="s",
                )
        mock_create.assert_not_called()

    def test_all_ids_invalid_raises_and_skips_mutation(self):
        # Orders present but every orderId is null/missing → still nothing
        # safe to move.
        with patch.object(uzum_openapi, "fetch_fbs_invoice_orders",
                          return_value=([{"orderId": None}, {"x": 1}], "u")), \
             patch.object(uzum_openapi, "create_fbs_invoice") as mock_create:
            with pytest.raises(ValueError):
                change_invoice_pickup(
                    "tok", invoice_id=1, seller_id=1,
                    drop_off_point_uuid="d", time_slot_uuid="s",
                )
        mock_create.assert_not_called()

    def test_fetch_error_propagates_without_mutating(self):
        # If reading the membership throws (403/timeout/...), the mutation
        # must not fire on a half-known order set.
        with patch.object(uzum_openapi, "fetch_fbs_invoice_orders",
                          side_effect=_Boom("uzum down")), \
             patch.object(uzum_openapi, "create_fbs_invoice") as mock_create:
            with pytest.raises(_Boom):
                change_invoice_pickup(
                    "tok", invoice_id=1, seller_id=1,
                    drop_off_point_uuid="d", time_slot_uuid="s",
                )
        mock_create.assert_not_called()
