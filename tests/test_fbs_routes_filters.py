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

from fbs.routes import _filter_stock_skus_to_shops, _parse_yyyy_mm_dd_to_ms


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
    """``/v2/fbs/sku/stocks`` is SELLER-level — it returns every SKU the
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
