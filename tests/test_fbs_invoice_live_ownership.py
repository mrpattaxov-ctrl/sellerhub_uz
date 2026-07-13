"""Live owned-invoice set for the накладная list filter (2026-07-13).

WHY: the DB-backed ``_owned_invoice_numbers`` misses a postavka the seller
created directly on Uzum — PENDING_DELIVERY is never background-synced, so the
order's ``invoice_number`` never lands in ``fbs_orders`` and the list filter
wrongly drops the seller's OWN накладная as "foreign" (verified live against
user=2 on 2026-07-12: invoices 120001136105 / …012 belong to shops 10945 /
5983 yet were flagged foreign).

The fix rebuilds the owned set from a LIVE batched ``/v2/fbs/orders?status=
PENDING_DELIVERY`` fetch. These tests prove the two new helpers:
  * ``_invoice_numbers_from_orders`` — pure projection, shop-scoped.
  * ``_live_owned_invoice_numbers`` — batched fetch → projection, best-effort.

Foreign shops must NEVER leak in; a just-created (un-synced) postavka MUST.
No Postgres, no Uzum HTTP.
"""
from __future__ import annotations

from unittest.mock import patch

from fbs.routes import (
    _invoice_numbers_from_orders,
    _live_owned_invoice_numbers,
)


class TestInvoiceNumbersFromOrders:
    """Pure projection: Uzum order dicts → owned invoice_number set."""

    # user=2's shops (the live-verified case).
    SHOPS = ["5983", "10945", "40571", "51948"]

    def test_owned_orders_collected(self):
        orders = [
            {"id": 116370106, "shopId": 10945, "invoiceNumber": "120001136105"},
            {"id": 116312451, "shopId": 5983, "invoiceNumber": "120001136012"},
        ]
        assert _invoice_numbers_from_orders(orders, self.SHOPS) == {
            "120001136105", "120001136012"}

    def test_foreign_shop_order_excluded(self):
        # An order whose shopId is NOT one of the user's shops is dropped —
        # fail-safe even though the batched fetch only requests owned shopIds.
        orders = [
            {"id": 1, "shopId": 10945, "invoiceNumber": "OWNED"},
            {"id": 2, "shopId": 99999, "invoiceNumber": "FOREIGN"},
        ]
        assert _invoice_numbers_from_orders(orders, self.SHOPS) == {"OWNED"}

    def test_order_without_invoice_number_skipped(self):
        orders = [
            {"id": 1, "shopId": 10945, "invoiceNumber": None},
            {"id": 2, "shopId": 10945},                       # key absent
            {"id": 3, "shopId": 10945, "invoiceNumber": ""},  # blank
            {"id": 4, "shopId": 10945, "invoiceNumber": "REAL"},
        ]
        assert _invoice_numbers_from_orders(orders, self.SHOPS) == {"REAL"}

    def test_int_shopid_matches_str_shop_list(self):
        # Uzum sends shopId as int; the shop list is strings.
        orders = [{"id": 1, "shopId": 10945, "invoiceNumber": "X"}]
        assert _invoice_numbers_from_orders(orders, ["10945"]) == {"X"}

    def test_int_invoice_number_coerced_to_str(self):
        orders = [{"id": 1, "shopId": 10945, "invoiceNumber": 120001136105}]
        assert _invoice_numbers_from_orders(orders, self.SHOPS) == {"120001136105"}

    def test_empty_shops_returns_empty(self):
        orders = [{"id": 1, "shopId": 10945, "invoiceNumber": "X"}]
        assert _invoice_numbers_from_orders(orders, []) == set()

    def test_empty_orders_returns_empty(self):
        assert _invoice_numbers_from_orders([], self.SHOPS) == set()

    def test_none_orders_returns_empty(self):
        assert _invoice_numbers_from_orders(None, self.SHOPS) == set()


class TestLiveOwnedInvoiceNumbers:
    """Batched fetch → projection, best-effort. Uzum I/O is mocked."""

    SHOPS = ["5983", "10945", "40571", "51948"]

    def test_fresh_postavka_included(self):
        # The live-verified regression: a postavka NOT in the DB is surfaced.
        orders = [
            {"id": 116370106, "shopId": 10945, "invoiceNumber": "120001136105"},
            {"id": 116312451, "shopId": 5983, "invoiceNumber": "120001136012"},
        ]
        with patch("core.fbs_sync.fetch_all_pages", return_value=orders) as fetch:
            got = _live_owned_invoice_numbers("tok", self.SHOPS)
        assert got == {"120001136105", "120001136012"}
        # Batched: ONE call carrying every shopId, status=PENDING_DELIVERY.
        fetch.assert_called_once()
        assert fetch.call_args.args[1] == self.SHOPS
        assert fetch.call_args.kwargs["status"] == "PENDING_DELIVERY"

    def test_no_token_no_fetch(self):
        with patch("core.fbs_sync.fetch_all_pages") as fetch:
            assert _live_owned_invoice_numbers("", self.SHOPS) == set()
            fetch.assert_not_called()

    def test_no_shops_no_fetch(self):
        with patch("core.fbs_sync.fetch_all_pages") as fetch:
            assert _live_owned_invoice_numbers("tok", []) == set()
            fetch.assert_not_called()

    def test_uzum_error_returns_empty_set(self):
        # Best-effort: a 429/5xx must not raise — caller falls back to DB set.
        with patch("core.fbs_sync.fetch_all_pages",
                   side_effect=RuntimeError("Uzum 429")):
            assert _live_owned_invoice_numbers("tok", self.SHOPS) == set()
