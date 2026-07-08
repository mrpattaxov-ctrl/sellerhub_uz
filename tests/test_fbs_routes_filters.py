"""Pure-function tests for the route-layer filter helpers in
``fbs.routes``.

We don't exercise the full Flask request cycle here — the SQL filter
behavior is covered implicitly by the day-to-day "search returns the
right rows" UI testing. What we DO cover is the boundary helper that
turns raw query-string input into the typed values the data layer
expects, because that's where regressions creep in silently (a wrong
end-of-day shift, for example, would quietly exclude orders).
"""
from __future__ import annotations

from fbs.routes import (
    _FBS_REFRESH_ON_PRESS_SYNC,
    _filter_invoices_to_owned,
    _filter_stock_skus_to_shops,
    _parse_live_count_statuses,
    _parse_yyyy_mm_dd_to_ms,
)


class TestParseLiveCountStatuses:
    """``/fbs/api/counts-all?statuses=`` chooses which chips get a LIVE
    /count from Uzum (Bosqich A.12). The page-entry load uses it to refresh
    ONLY Yangi + Yig'ilmoqda instead of all 4 active chips, killing the
    per-press burst. Terminal chips must never be live-refreshable through
    this param.
    """

    def test_empty_returns_all_active(self):
        # No param → keep the pre-A.12 "refresh every active chip" default.
        assert _parse_live_count_statuses(None) == _FBS_REFRESH_ON_PRESS_SYNC
        assert _parse_live_count_statuses("") == _FBS_REFRESH_ON_PRESS_SYNC
        assert _parse_live_count_statuses("   ") == _FBS_REFRESH_ON_PRESS_SYNC

    def test_entry_pair_only(self):
        # The exact call the page entry makes: just the 2 visible-live chips.
        assert _parse_live_count_statuses("CREATED,PACKING") == ("CREATED", "PACKING")

    def test_single_status(self):
        assert _parse_live_count_statuses("PACKING") == ("PACKING",)

    def test_order_follows_canonical_not_input(self):
        # Stable output regardless of input ordering.
        assert _parse_live_count_statuses("PACKING,CREATED") == ("CREATED", "PACKING")

    def test_terminal_status_dropped(self):
        # A terminal chip can NEVER be forced live — it stays on the DB read.
        assert _parse_live_count_statuses("COMPLETED") == ()
        assert _parse_live_count_statuses("CREATED,COMPLETED") == ("CREATED",)

    def test_unknown_status_dropped(self):
        assert _parse_live_count_statuses("CREATED,BOGUS") == ("CREATED",)
        assert _parse_live_count_statuses("BOGUS") == ()

    def test_case_and_whitespace_insensitive(self):
        assert _parse_live_count_statuses(" created , packing ") == ("CREATED", "PACKING")


class TestParseYyyyMmDdToMs:
    """``<input type="date">`` always emits ``YYYY-MM-DD``. The helper
    must turn that into epoch ms with the picked day interpreted in
    Asia/Tashkent (UTC+5) — not UTC — so the filter matches the date
    the seller actually sees on the list page.
    """

    def test_start_of_day_for_lower_bound(self):
        # 2026-05-22T00:00:00.000+05:00 = 2026-05-21T19:00:00.000Z
        # = 1779390000000 ms
        assert _parse_yyyy_mm_dd_to_ms("2026-05-22", end_of_day=False) == 1779390000000

    def test_end_of_day_for_upper_bound(self):
        # 2026-05-22T23:59:59.999+05:00 = 2026-05-22T18:59:59.999Z
        # = 1779476399999 ms
        # The 0.999s shift matters: an order created at 23:50 Tashkent
        # must still match "to=2026-05-22". A naive midnight cut would
        # drop it.
        assert _parse_yyyy_mm_dd_to_ms("2026-05-22", end_of_day=True) == 1779476399999

    def test_none_returns_none(self):
        assert _parse_yyyy_mm_dd_to_ms(None, end_of_day=False) is None

    def test_empty_string_returns_none(self):
        assert _parse_yyyy_mm_dd_to_ms("", end_of_day=False) is None

    def test_whitespace_only_returns_none(self):
        assert _parse_yyyy_mm_dd_to_ms("   ", end_of_day=True) is None

    def test_malformed_returns_none(self):
        # Bad input is treated as "no filter" rather than 400ed —
        # mirrors how the other optional list params behave.
        assert _parse_yyyy_mm_dd_to_ms("not-a-date", end_of_day=False) is None
        assert _parse_yyyy_mm_dd_to_ms("2026/05/22", end_of_day=False) is None
        assert _parse_yyyy_mm_dd_to_ms("22-05-2026", end_of_day=False) is None

    def test_tashkent_offset_applied(self):
        # 1970-01-01T00:00:00 *Tashkent* = 1969-12-31T19:00:00 UTC
        # = -18_000_000 ms. Proves the helper honours the +05:00
        # offset, not UTC.
        assert _parse_yyyy_mm_dd_to_ms("1970-01-01", end_of_day=False) == -18_000_000

    def test_end_of_day_is_strictly_later_than_start(self):
        # Regression guard: end_of_day MUST shift the timestamp, not
        # just return the same midnight value. A copy-paste mistake
        # there would silently break the upper-bound filter.
        lo = _parse_yyyy_mm_dd_to_ms("2026-05-22", end_of_day=False)
        hi = _parse_yyyy_mm_dd_to_ms("2026-05-22", end_of_day=True)
        assert hi > lo
        # Spans nearly a full day.
        assert (hi - lo) >= 86_300_000


class TestFilterStockSkusToShops:
    """``/v3/fbs/sku/stocks`` is SELLER-level — it returns every SKU the
    token's seller owns, including Uzum shops the seller never added to
    SellerHub. The «Ombor» grid and the Excel export must show ONLY the
    user's registered shops, so each row is kept only when its skuId maps
    (via the local Variant table) to one of the user's shop ids. A row with
    no mapping is a foreign / unregistered shop → dropped.
    """

    # skuId → local shop_id, exactly the shape ``_stock_shop_maps`` returns
    # (keys are str because Variant.uzum_sku_id is a VARCHAR column).
    SHOP_BY_SKU = {"1": 6, "2": 6, "3": 7, "9": 99}

    def test_keeps_only_user_shops(self):
        skus = [{"skuId": 1}, {"skuId": 2}, {"skuId": 3}, {"skuId": 9}]
        kept, hidden = _filter_stock_skus_to_shops(skus, self.SHOP_BY_SKU, [6, 7])
        assert [s["skuId"] for s in kept] == [1, 2, 3]   # shop 99 not the user's
        assert hidden == 1

    def test_drops_foreign_unmapped_sku(self):
        # LRINGS / WISH / SACVOYA: unregistered shop → no Variant → no map.
        skus = [{"skuId": 1}, {"skuId": 555}]
        kept, hidden = _filter_stock_skus_to_shops(skus, self.SHOP_BY_SKU, [6, 7])
        assert [s["skuId"] for s in kept] == [1]
        assert hidden == 1

    def test_drops_sku_mapped_to_non_allowed_shop(self):
        kept, hidden = _filter_stock_skus_to_shops([{"skuId": 9}], self.SHOP_BY_SKU, [6, 7])
        assert kept == []
        assert hidden == 1

    def test_empty_allowed_hides_everything(self):
        # A user with no registered shops sees an empty grid, never the
        # raw seller-level dump.
        kept, hidden = _filter_stock_skus_to_shops(
            [{"skuId": 1}, {"skuId": 2}], self.SHOP_BY_SKU, [])
        assert kept == []
        assert hidden == 2

    def test_int_skuid_matches_str_map_key(self):
        # skuId arrives as an int from Uzum; the map keys are str.
        kept, _ = _filter_stock_skus_to_shops([{"skuId": 1}], {"1": 6}, [6])
        assert len(kept) == 1

    def test_missing_skuid_is_dropped(self):
        kept, hidden = _filter_stock_skus_to_shops([{"amount": 5}], self.SHOP_BY_SKU, [6])
        assert kept == []
        assert hidden == 1

    def test_returns_same_row_objects(self):
        # Kept rows must be the SAME dicts — the route mutates them in place
        # afterwards to attach image / shopName.
        row = {"skuId": 1, "amount": 3}
        kept, _ = _filter_stock_skus_to_shops([row], {"1": 6}, [6])
        assert kept[0] is row

    def test_allowed_ids_accept_str_or_int(self):
        # _user_shop_ids yields ints, but be defensive about str ids too.
        kept, hidden = _filter_stock_skus_to_shops([{"skuId": 1}], {"1": 6}, ["6"])
        assert len(kept) == 1 and hidden == 0


class TestFilterInvoicesToOwned:
    """``GET /v1/fbs/invoice`` is TOKEN-scoped — it returns накладные for
    EVERY shop on the seller account, including shops the user never added to
    SellerHub (the "Do'konlar: 4/5" 5th shop). Uzum's invoice payload has no
    ``shopId`` and its ``stock`` warehouse is shared, so the only reliable
    discriminator is the invoice ``number``: an order carries its
    ``invoiceNumber`` and the order-sync writes ONLY registered shops, so the
    local ``fbs_orders.invoice_number`` set is exactly the user's invoices.
    A number outside that set is a foreign shop's накладная → dropped.
    """

    # Owned numbers come from a DISTINCT query over fbs_orders → strings.
    OWNED = {"120000997273", "120001005732"}

    def test_keeps_only_owned_numbers(self):
        invoices = [
            {"number": "120000997273"},   # registered shop
            {"number": "999999999999"},   # foreign 5th shop → drop
            {"number": "120001005732"},   # registered shop
        ]
        kept = _filter_invoices_to_owned(invoices, self.OWNED)
        assert [iv["number"] for iv in kept] == ["120000997273", "120001005732"]

    def test_int_number_matches_str_owned(self):
        # Be defensive: even if a number arrives as int, str-coercion matches.
        kept = _filter_invoices_to_owned([{"number": 120000997273}], self.OWNED)
        assert len(kept) == 1

    def test_owned_with_int_entries_coerced(self):
        # owned_numbers built defensively — int entries must still match.
        kept = _filter_invoices_to_owned([{"number": "120000997273"}], {120000997273})
        assert len(kept) == 1

    def test_empty_owned_hides_everything(self):
        # User with no synced orders (or no shops) sees an empty list, never
        # the raw token-level dump of foreign invoices.
        kept = _filter_invoices_to_owned(
            [{"number": "120000997273"}, {"number": "120001005732"}], set())
        assert kept == []

    def test_none_owned_hides_everything(self):
        kept = _filter_invoices_to_owned([{"number": "120000997273"}], None)
        assert kept == []

    def test_missing_number_is_dropped(self):
        kept = _filter_invoices_to_owned([{"status": "CREATED"}], self.OWNED)
        assert kept == []

    def test_blank_owned_entries_ignored(self):
        # A NULL/blank invoice_number row must never become a wildcard that
        # lets a numberless foreign invoice through.
        kept = _filter_invoices_to_owned(
            [{"number": ""}, {"number": None}], {"", "  ", None})
        assert kept == []

    def test_returns_same_invoice_objects(self):
        iv = {"number": "120000997273", "status": "CREATED"}
        kept = _filter_invoices_to_owned([iv], self.OWNED)
        assert kept[0] is iv
