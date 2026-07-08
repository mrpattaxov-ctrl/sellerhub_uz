"""Scope-guard tests for the by-id FBS invoice/stock endpoints (2026-06-12).

WHY: ``GET /v1/fbs/invoice*`` and ``/v2/fbs/sku/stocks`` are TOKEN-scoped at
Uzum — one seller account, many shops, and the token serves (and MUTATES!)
data for ALL of them, including shops the user never registered in SellerHub.
The list endpoints were already filtered; these tests cover the by-id /
write paths that used to trust Uzum's scoping:

  * ``_decide_invoice_ownership`` — the pure ownership decision used by the
    detail / akt / change-pickup guards.
  * ``_foreign_sku_ids`` — the stock-write (MUTATION) rejector.
  * ``change_invoice_pickup(order_ids=...)`` — the pass-through that lets the
    route's guard supply the already-fetched orders (no duplicate Uzum call).

Per the action-endpoint policy, the MUTATION guards are proven with mocks
BEFORE prod: a foreign invoice/SKU must be denied, an owned one must pass.
No Postgres, no Uzum HTTP.
"""
from __future__ import annotations

from unittest.mock import patch

from fbs.routes import _decide_invoice_ownership, _foreign_sku_ids
from core import uzum_openapi
from core.uzum_openapi import change_invoice_pickup


class TestDecideInvoiceOwnership:
    """Pure two-signal ownership decision."""

    OWNED_NUMBERS = {"120000997273", "120001005732"}

    def test_owned_number_passes(self):
        assert _decide_invoice_ownership(
            "120000997273", [], self.OWNED_NUMBERS, []) is True

    def test_foreign_number_no_orders_denied(self):
        assert _decide_invoice_ownership(
            "999999999999", [], self.OWNED_NUMBERS, []) is False

    def test_fresh_invoice_owned_orders_pass(self):
        # Number not synced yet (created seconds ago), but its orders are
        # found locally under the user's shops → owned.
        assert _decide_invoice_ownership(
            "888888888888", ["5001", "5002"], self.OWNED_NUMBERS, {"5002"}) is True

    def test_foreign_invoice_foreign_orders_denied(self):
        # Orders exist at Uzum but none map to the user's shops → foreign.
        assert _decide_invoice_ownership(
            "888888888888", ["7001", "7002"], self.OWNED_NUMBERS, set()) is False

    def test_int_number_matches_str_owned(self):
        assert _decide_invoice_ownership(
            120000997273, [], self.OWNED_NUMBERS, []) is True

    def test_none_number_no_orders_denied(self):
        assert _decide_invoice_ownership(None, [], self.OWNED_NUMBERS, []) is False

    def test_blank_number_never_wildcards(self):
        # A NULL/blank invoice_number row in fbs_orders must not let a
        # numberless invoice through.
        assert _decide_invoice_ownership(
            "", [], {"", "  ", None}, []) is False

    def test_int_order_ids_match_str_owned(self):
        assert _decide_invoice_ownership(
            None, [5001], set(), {"5001"}) is True

    def test_empty_everything_denied(self):
        assert _decide_invoice_ownership(None, [], set(), set()) is False


class TestForeignSkuIds:
    """Stock-write rejector — same semantics as the read filter, fail-closed."""

    SHOP_BY_SKU = {"1": 6, "2": 6, "3": 7, "9": 99}

    def test_all_owned_returns_empty(self):
        items = [{"skuId": 1, "amount": 5}, {"skuId": 3, "amount": 0}]
        assert _foreign_sku_ids(items, self.SHOP_BY_SKU, [6, 7]) == []

    def test_foreign_shop_sku_rejected(self):
        items = [{"skuId": 1, "amount": 5}, {"skuId": 9, "amount": 0}]
        assert _foreign_sku_ids(items, self.SHOP_BY_SKU, [6, 7]) == ["9"]

    def test_unmapped_sku_rejected(self):
        # No local Variant → unregistered shop → fail-closed.
        items = [{"skuId": 555, "amount": 3}]
        assert _foreign_sku_ids(items, self.SHOP_BY_SKU, [6, 7]) == ["555"]

    def test_missing_skuid_rejected(self):
        assert _foreign_sku_ids([{"amount": 5}], self.SHOP_BY_SKU, [6]) == ["None"]

    def test_empty_allowed_rejects_all(self):
        items = [{"skuId": 1}, {"skuId": 2}]
        assert _foreign_sku_ids(items, self.SHOP_BY_SKU, []) == ["1", "2"]

    def test_int_skuid_matches_str_map(self):
        assert _foreign_sku_ids([{"skuId": 1}], {"1": 6}, [6]) == []

    def test_str_allowed_ids_accepted(self):
        assert _foreign_sku_ids([{"skuId": 1}], {"1": 6}, ["6"]) == []

    def test_empty_items_ok(self):
        assert _foreign_sku_ids([], self.SHOP_BY_SKU, [6]) == []


class TestChangePickupOrderIdsPassThrough:
    """``order_ids=...`` skips the duplicate fetch; ``None`` keeps it."""

    def test_supplied_order_ids_skip_fetch(self):
        with patch.object(uzum_openapi, "fetch_fbs_invoice_orders") as fetch, \
             patch.object(uzum_openapi, "create_fbs_invoice",
                          return_value=({"id": 327497}, "url")) as create:
            change_invoice_pickup(
                "tok", invoice_id=327497, seller_id=95673,
                drop_off_point_uuid="dp-uuid", time_slot_uuid="ts-uuid",
                order_ids=[5001, 5002],
            )
            fetch.assert_not_called()
            assert create.call_args.kwargs["order_ids"] == [5001, 5002]
            assert create.call_args.kwargs["update_only"] is True

    def test_none_order_ids_fetches_from_uzum(self):
        with patch.object(uzum_openapi, "fetch_fbs_invoice_orders",
                          return_value=([{"orderId": 7001}], "url")) as fetch, \
             patch.object(uzum_openapi, "create_fbs_invoice",
                          return_value=({"id": 327497}, "url")) as create:
            change_invoice_pickup(
                "tok", invoice_id=327497, seller_id=95673,
                drop_off_point_uuid="dp-uuid", time_slot_uuid="ts-uuid",
            )
            fetch.assert_called_once()
            assert create.call_args.kwargs["order_ids"] == [7001]

    def test_empty_supplied_order_ids_raises(self):
        # Explicit empty list = nothing to move — must raise, never fire the
        # mutation with an empty orderIds (Uzum would 400 anyway).
        import pytest
        with patch.object(uzum_openapi, "create_fbs_invoice") as create:
            with pytest.raises(ValueError):
                change_invoice_pickup(
                    "tok", invoice_id=327497, seller_id=95673,
                    drop_off_point_uuid="dp-uuid", time_slot_uuid="ts-uuid",
                    order_ids=[],
                )
            create.assert_not_called()


class TestPrefetchScopeFilter:
    """Worker akt prefetch must skip foreign-shop invoices entirely:
    no akt download, no cache row, and NOT in the prune keep-set (so stale
    foreign rows cached before the filter get cleaned up)."""

    INVOICES = [
        {"id": 111, "number": "120000997273", "dateUpdated": 1},   # owned
        {"id": 222, "number": "999999999999", "dateUpdated": 1},   # foreign
    ]

    def _run(self, owned_numbers):
        # fetch_fbs_invoices_list is imported INSIDE prefetch_akts_for_token,
        # so patching it at its source module is sufficient.
        from core import fbs_akt_cache as mod
        fetched_ids = []
        with patch("core.uzum_openapi.fetch_fbs_invoices_list",
                   return_value=(self.INVOICES, "url")), \
             patch.object(mod, "get_cached_akt", return_value=None), \
             patch.object(mod, "fetch_akt_live",
                          side_effect=lambda t, i: fetched_ids.append(int(i)) or b"%PDF"), \
             patch.object(mod, "store_akt"), \
             patch.object(mod, "prune_akts_not_in", return_value=0) as prune:
            mod.prefetch_akts_for_token(
                "tok", 1, owned_numbers=owned_numbers)
        return fetched_ids, prune

    def test_foreign_invoice_not_fetched_not_kept(self):
        fetched_ids, prune = self._run({"120000997273"})
        assert fetched_ids == [111]                     # foreign 222 never printed
        keep = prune.call_args.args[1]
        assert 111 in keep and 222 not in keep          # 222 will be pruned

    def test_none_filter_keeps_old_behaviour(self):
        fetched_ids, prune = self._run(None)
        assert sorted(fetched_ids) == [111, 222]
        keep = prune.call_args.args[1]
        assert {111, 222} <= set(keep)
