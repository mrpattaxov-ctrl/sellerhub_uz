"""Tests for the time-slots scoping fix (2026-06-06).

Background — the "Yetkazib berish muddati o'tib ketgan" (fbs-19-time-is-up)
bug: clicking «Slotlar» on a drop-off point fetched delivery slots using
EVERY active order in the account (PACKING + PENDING_DELIVERY + DELIVERING,
up to 50). Uzum computes the slot window as ``[now .. earliest deliverUntil]``
across the orders it's handed, so a single overdue order anywhere in that set
pushes the earliest deadline into the past and Uzum rejects the whole batch —
even though the seller had only selected valid PACKING orders.

The fix lets the frontend pass ``?order_ids=…`` (the SELECTED PACKING orders,
the exact set that lands on the накладная). ``_parse_order_ids_param`` turns
that query string into the canonical id set the route uses to scope the DB
query. ``FbsOrder.order_id`` is a numeric STRING column, so the parser must
emit strings — comparing ints against a varchar column would match nothing.
"""
from __future__ import annotations

from fbs.routes import _parse_order_ids_param


class TestParseOrderIdsParam:
    def test_basic_comma_list_to_string_set(self):
        # Output MUST be strings — the column is VARCHAR(64). Ints here would
        # silently match zero rows and reintroduce the legacy fall-through.
        assert _parse_order_ids_param("111,222,333") == {"111", "222", "333"}

    def test_normalises_whitespace_and_leading_zeros(self):
        # " 007 " and "7" are the same order; int() canonicalises both so the
        # set doesn't carry two spellings of one id.
        assert _parse_order_ids_param(" 007 , 7 , 010 ") == {"7", "10"}

    def test_drops_non_numeric_and_empty_tokens(self):
        # Trailing comma, blanks, and junk are tolerated (not 400'd) — the
        # route treats a malformed list as "scope to whatever's valid".
        assert _parse_order_ids_param("111,,abc, ,222,") == {"111", "222"}

    def test_none_and_blank_yield_empty_set(self):
        # Empty set is the signal the route uses to fall back to the legacy
        # all-active-orders query, so these must NOT raise.
        assert _parse_order_ids_param(None) == set()
        assert _parse_order_ids_param("") == set()
        assert _parse_order_ids_param("   ") == set()
        assert _parse_order_ids_param(",,, ,") == set()

    def test_deduplicates(self):
        assert _parse_order_ids_param("5,5,5,6") == {"5", "6"}

    def test_negative_and_signed_are_canonicalised_not_dropped(self):
        # int() accepts "+5"/"-5"; they're numeric so they survive. Real Uzum
        # ids are never negative, but the parser's job is numeric validation,
        # not range-checking — ownership/status is re-checked in the DB query.
        assert _parse_order_ids_param("+5,-3") == {"5", "-3"}
