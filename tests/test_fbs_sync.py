"""Pure-function tests for ``core.fbs_sync``.

Covers the four conversion / parsing helpers that have no DB or HTTP
side effects:

* ``parse_iso_naive_utc`` — Uzum ISO 8601 → naive UTC datetime.
* ``_iso_z``              — naive UTC datetime → Uzum-shaped ISO string.
* ``dict_from_order``     — Uzum response dict → ``FbsOrder`` column dict.
* ``row_to_dict``         — ``FbsOrder`` row → Uzum-shaped dict (the
                            shape the templates render).

These are the fields-and-formats boundary between Uzum's API and the
local DB cache — a regression here silently corrupts every cached row.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from core.fbs_sync import (
    parse_iso_naive_utc,
    _iso_z,
    dict_from_order,
    row_to_dict,
    upsert_orders,
    _UPSERT_CHUNK_SIZE,
)
from models import FbsOrder


# ─────────────────────────────────────────────────────────────────────
# parse_iso_naive_utc
# ─────────────────────────────────────────────────────────────────────


class TestParseIsoNaiveUtc:
    """Uzum sends timestamps like ``2026-05-21T13:43:36.190Z``. The Z
    is UTC; we strip the tzinfo and store naive UTC so reads come back
    as plain ``datetime`` objects (Postgres TIMESTAMP without timezone).
    """

    def test_z_suffix_parses_as_utc(self):
        dt = parse_iso_naive_utc("2026-05-21T13:43:36.190Z")
        assert dt == datetime(2026, 5, 21, 13, 43, 36, 190000)
        # Naive — tzinfo stripped after UTC conversion.
        assert dt.tzinfo is None

    def test_offset_form_normalizes_to_utc(self):
        # +05:00 means 13:00 local == 08:00 UTC.
        dt = parse_iso_naive_utc("2026-05-21T13:00:00+05:00")
        assert dt == datetime(2026, 5, 21, 8, 0, 0)
        assert dt.tzinfo is None

    def test_no_timezone_returns_naive_as_is(self):
        dt = parse_iso_naive_utc("2026-05-21T13:43:36")
        assert dt == datetime(2026, 5, 21, 13, 43, 36)
        assert dt.tzinfo is None

    def test_none_returns_none(self):
        assert parse_iso_naive_utc(None) is None

    def test_empty_string_returns_none(self):
        assert parse_iso_naive_utc("") is None

    def test_whitespace_only_returns_none(self):
        # ``not s`` is the guard — whitespace-only strings pass that but
        # fail fromisoformat. Confirm we return None rather than raising.
        assert parse_iso_naive_utc("   ") is None

    def test_malformed_string_returns_none(self):
        # One bad timestamp shouldn't drop a whole batch — the helper
        # swallows ValueError and returns None.
        assert parse_iso_naive_utc("not-a-date") is None

    def test_integer_epoch_ms_parses(self):
        # /v2/fbs/orders and /v1/fbs/order/{id} return every date field
        # as int64 epoch milliseconds despite the ``$date-time`` swagger
        # annotation. Observed 2026-05-23.
        # 1779460587795 ms = 2026-05-22T14:36:27.795Z
        dt = parse_iso_naive_utc(1779460587795)
        assert dt == datetime(2026, 5, 22, 14, 36, 27, 795000)
        assert dt.tzinfo is None

    def test_float_epoch_ms_parses(self):
        # Floats are tolerated for the same reason as ints — JSON
        # decoders sometimes coerce ms counts through Number.
        dt = parse_iso_naive_utc(1779460587795.0)
        assert dt == datetime(2026, 5, 22, 14, 36, 27, 795000)

    def test_bool_returns_none(self):
        # ``bool`` is a subclass of ``int``. ``True`` would otherwise be
        # treated as 1ms past the epoch — not what we want.
        assert parse_iso_naive_utc(True) is None
        assert parse_iso_naive_utc(False) is None


# ─────────────────────────────────────────────────────────────────────
# _iso_z
# ─────────────────────────────────────────────────────────────────────


class TestIsoZ:
    """Inverse of parse_iso_naive_utc. Frontend's ``new Date(...)``
    requires the trailing Z to interpret the string as UTC; without it
    the browser would render local time and order timestamps would
    appear shifted.
    """

    def test_basic_naive_utc_gets_z(self):
        dt = datetime(2026, 5, 21, 13, 43, 36, 190000)
        assert _iso_z(dt) == "2026-05-21T13:43:36.190Z"

    def test_no_microseconds_still_has_milliseconds(self):
        dt = datetime(2026, 5, 21, 13, 43, 36)
        # timespec="milliseconds" forces the .000 segment so the format
        # stays stable across rows with and without sub-second precision.
        assert _iso_z(dt) == "2026-05-21T13:43:36.000Z"

    def test_none_returns_none(self):
        assert _iso_z(None) is None

    def test_round_trip_preserves_milliseconds(self):
        # parse → format → parse should be a fixed point (modulo the
        # microsecond → millisecond truncation _iso_z applies).
        original = "2026-05-21T13:43:36.190Z"
        dt = parse_iso_naive_utc(original)
        assert _iso_z(dt) == original


# ─────────────────────────────────────────────────────────────────────
# dict_from_order
# ─────────────────────────────────────────────────────────────────────


def _minimal_uzum_order(**overrides) -> dict:
    """Smallest Uzum order payload that dict_from_order accepts.

    Field set matches what real Uzum responses look like for a CREATED
    FBS order; tests can override individual fields to exercise edge
    cases without rewriting the whole dict.
    """
    base = {
        "id": 1234567890,
        "scheme": "FBS",
        "status": "CREATED",
        "price": 150000,
        "dateCreated": "2026-05-21T13:43:36.190Z",
        "acceptUntil": "2026-05-22T13:43:36.190Z",
        "deliverUntil": "2026-05-25T13:43:36.190Z",
        "identifierRequired": False,
        "invoiceNumber": "INV-001",
        "deliveryInfo": {
            "customerFullname": "Test Customer",
            "customerPhone": "+998901234567",
            "deliveryAddress": "Toshkent, Yunusobod",
            "deliveryComment": "2-qavat",
        },
        "stock": {
            "id": 42,
            "title": "Asosiy ombor",
        },
        "dropOffPoint": {
            "uuid": "dp-uuid-001",
            "address": "Chilonzor PVZ",
        },
        "orderItems": [{"sku": "SKU-1", "qty": 2}],
    }
    base.update(overrides)
    return base


class TestDictFromOrder:
    """Projection from Uzum's nested response shape onto the flat
    column set of ``fbs_orders``. Every mapping here is also what
    ``upsert_orders`` writes via the bulk INSERT, so a bug in this
    function silently corrupts every cached row.
    """

    def test_identity_fields_projected(self):
        # Bosqich 4l: order without ``shopId`` falls back to the param.
        # That's the old single-shop sync path (and most test fixtures).
        order = _minimal_uzum_order()
        order.pop("shopId", None)
        d = dict_from_order(5983, order)
        # shop_id is always a string even when the caller passes int —
        # the DB column is String(64) (matches Shop.uzum_id convention).
        assert d["shop_id"] == "5983"
        # order_id is also stringified — Uzum's int64 fits but we treat
        # the value as opaque ID, not a number to compute on.
        assert d["order_id"] == "1234567890"
        assert d["order_type"] == "FBS"
        assert d["status"] == "CREATED"

    def test_per_order_shopid_wins_over_param(self):
        # CRITICAL — Bosqich 4l token-batched sync. One Uzum call
        # returns orders from multiple shops mixed together; each
        # order's own ``shopId`` MUST determine which row's shop_id it
        # lands under. Falling back to the param here would cross-
        # contaminate the seller's shops (orders from shop 13505
        # showing up under shop 10945).
        order = _minimal_uzum_order(shopId=13505)
        d = dict_from_order(5983, order)
        assert d["shop_id"] == "13505"

    def test_param_used_when_shopid_missing(self):
        # Backward compat: legacy single-shop sync path still passes
        # the shop id as the param, with Uzum responses sometimes
        # omitting shopId in the body (older responses, test data).
        order = _minimal_uzum_order()
        order.pop("shopId", None)
        d = dict_from_order(5983, order)
        assert d["shop_id"] == "5983"

    def test_param_used_when_shopid_empty(self):
        # Defensive: Uzum has been observed to send "" for fields it
        # treats as null. An empty string would otherwise produce a
        # row with shop_id="" which no template can render.
        order = _minimal_uzum_order(shopId="")
        d = dict_from_order(5983, order)
        assert d["shop_id"] == "5983"
        order2 = _minimal_uzum_order(shopId="   ")
        d2 = dict_from_order(5983, order2)
        assert d2["shop_id"] == "5983"

    def test_scheme_defaults_to_fbs_when_missing(self):
        # Uzum has been observed to omit ``scheme`` for FBS orders; the
        # CHECK constraint only accepts ('FBS','DBS') so we fill in a
        # safe default rather than letting the row fail to insert.
        order = _minimal_uzum_order()
        del order["scheme"]
        assert dict_from_order(5983, order)["order_type"] == "FBS"

    def test_dbs_scheme_preserved(self):
        d = dict_from_order(5983, _minimal_uzum_order(scheme="DBS"))
        assert d["order_type"] == "DBS"

    def test_status_defaults_to_created_when_missing(self):
        order = _minimal_uzum_order()
        del order["status"]
        # Matches the CHECK constraint default — CREATED is the natural
        # starting state, safer than letting status be NULL/empty.
        assert dict_from_order(5983, order)["status"] == "CREATED"

    def test_customer_fields_unnested(self):
        d = dict_from_order(5983, _minimal_uzum_order())
        # deliveryInfo nested object is flattened onto sibling columns.
        assert d["customer_fullname"] == "Test Customer"
        assert d["customer_phone"] == "+998901234567"
        assert d["delivery_address"] == "Toshkent, Yunusobod"
        assert d["delivery_comment"] == "2-qavat"

    def test_missing_delivery_info_yields_none(self):
        # Some statuses (e.g. CANCELED before assignment) have no
        # delivery info — the column must be NULL-able, not crash.
        order = _minimal_uzum_order()
        del order["deliveryInfo"]
        d = dict_from_order(5983, order)
        assert d["customer_fullname"] is None
        assert d["customer_phone"] is None
        assert d["delivery_address"] is None
        assert d["delivery_comment"] is None

    def test_stock_and_dropoff_unnested(self):
        d = dict_from_order(5983, _minimal_uzum_order())
        # stock.id is stringified — same convention as shop_id /
        # order_id. We never do arithmetic on stock_id.
        assert d["stock_id"] == "42"
        assert d["stock_title"] == "Asosiy ombor"
        assert d["drop_off_point_uuid"] == "dp-uuid-001"
        assert d["drop_off_point_address"] == "Chilonzor PVZ"

    def test_stock_id_none_passes_through(self):
        # Stock not yet assigned — column allows NULL.
        order = _minimal_uzum_order(stock={"id": None, "title": None})
        d = dict_from_order(5983, order)
        assert d["stock_id"] is None
        assert d["stock_title"] is None

    def test_price_coerced_to_int(self):
        # Uzum returns whole-soum integers, but JSON could decode as
        # str/float depending on transport — force int() so column
        # type matches.
        d = dict_from_order(5983, _minimal_uzum_order(price="200000"))
        assert d["price"] == 200000

    def test_missing_price_defaults_to_zero(self):
        order = _minimal_uzum_order()
        del order["price"]
        # Column is NOT NULL with default 0; we mirror that here.
        assert dict_from_order(5983, order)["price"] == 0

    def test_identifier_required_coerced_to_bool(self):
        # JSON booleans round-trip fine, but defensively coerce in case
        # Uzum ever sends 0/1 or "true"/"false".
        d = dict_from_order(5983, _minimal_uzum_order(identifierRequired=True))
        assert d["identifier_required"] is True

    def test_missing_identifier_required_defaults_false(self):
        order = _minimal_uzum_order()
        del order["identifierRequired"]
        assert dict_from_order(5983, order)["identifier_required"] is False

    def test_invoice_number_stringified(self):
        # invoice_number is String(64). If Uzum ever returns an int,
        # we coerce to str rather than letting SQLAlchemy guess.
        d = dict_from_order(5983, _minimal_uzum_order(invoiceNumber=12345))
        assert d["invoice_number"] == "12345"

    def test_invoice_number_none_passes_through(self):
        # Falsy invoice (None / "") becomes NULL rather than the string
        # "None" — the conditional in dict_from_order guards against
        # str(None).
        d = dict_from_order(5983, _minimal_uzum_order(invoiceNumber=None))
        assert d["invoice_number"] is None

    def test_date_fields_parsed_to_naive_utc(self):
        d = dict_from_order(5983, _minimal_uzum_order())
        assert d["date_created"] == datetime(2026, 5, 21, 13, 43, 36, 190000)
        assert d["accept_until"] == datetime(2026, 5, 22, 13, 43, 36, 190000)
        assert d["deliver_until"] == datetime(2026, 5, 25, 13, 43, 36, 190000)

    def test_date_fields_parsed_from_epoch_ms(self):
        # Production reality (observed 2026-05-23): /v2/fbs/orders and
        # the order detail endpoint return every date field as int64
        # epoch milliseconds, not ISO strings. dict_from_order must
        # handle both shapes — pre-fix every cached row had date columns
        # silently set to NULL.
        order = _minimal_uzum_order(
            dateCreated=1779460587795,    # 2026-05-22T14:36:27.795Z
            acceptUntil=1779518187795,    # 2026-05-23T06:36:27.795Z
            deliverUntil=1779793200000,   # 2026-05-26T11:00:00.000Z
        )
        d = dict_from_order(5983, order)
        assert d["date_created"] == datetime(2026, 5, 22, 14, 36, 27, 795000)
        assert d["accept_until"] == datetime(2026, 5, 23, 6, 36, 27, 795000)
        assert d["deliver_until"] == datetime(2026, 5, 26, 11, 0, 0)

    def test_missing_dates_become_none(self):
        order = _minimal_uzum_order()
        # Most dates are status-conditional — DELIVERED orders have
        # delivery_date, CREATED orders don't. Missing date columns
        # must be NULL, not 1970-01-01.
        del order["dateCreated"]
        del order["acceptUntil"]
        d = dict_from_order(5983, order)
        assert d["date_created"] is None
        assert d["accept_until"] is None

    def test_items_json_passthrough(self):
        items = [{"sku": "A", "qty": 1}, {"sku": "B", "qty": 3}]
        d = dict_from_order(5983, _minimal_uzum_order(orderItems=items))
        # JSONB column — we hand the list straight through; SQLAlchemy
        # serializes it on insert.
        assert d["items_json"] == items

    def test_missing_order_items_defaults_to_empty_list(self):
        # Server default is '[]'::jsonb so the column never sees NULL.
        # We mirror that in the dict so bulk INSERT doesn't trigger
        # constraint errors.
        order = _minimal_uzum_order()
        del order["orderItems"]
        assert dict_from_order(5983, order)["items_json"] == []

    def test_raw_json_preserves_full_payload(self):
        # raw_json is the source of truth when a UI field doesn't have
        # its own column yet — keeping the original payload verbatim
        # means we can surface new fields without a migration.
        order = _minimal_uzum_order(experimentalField="value-xyz")
        d = dict_from_order(5983, order)
        assert d["raw_json"] == order
        assert d["raw_json"]["experimentalField"] == "value-xyz"

    def test_synced_at_set_to_current_time(self):
        # synced_at must be set per-row so the cleanup loop can detect
        # rows that haven't been refreshed. Within a 5-second window
        # of "now" is good enough — we don't care about clock precision.
        before = datetime.utcnow()
        d = dict_from_order(5983, _minimal_uzum_order())
        after = datetime.utcnow()
        assert before <= d["synced_at"] <= after


# ─────────────────────────────────────────────────────────────────────
# row_to_dict
# ─────────────────────────────────────────────────────────────────────


def _make_fbs_row(**overrides) -> FbsOrder:
    """Build an ``FbsOrder`` instance for row_to_dict tests.

    Constructing an ORM object without adding it to a session is a pure
    Python call — no DB connection needed. Default values match a typical
    CREATED FBS row produced by the worker.
    """
    defaults = dict(
        shop_id="5983",
        order_id="1234567890",
        order_type="FBS",
        status="CREATED",
        customer_fullname="Test Customer",
        customer_phone="+998901234567",
        delivery_address="Toshkent",
        delivery_comment="2-qavat",
        price=150000,
        date_created=datetime(2026, 5, 21, 13, 43, 36, 190000),
        accept_until=datetime(2026, 5, 22, 13, 43, 36, 190000),
        deliver_until=datetime(2026, 5, 25, 13, 43, 36, 190000),
        accepted_date=None,
        delivering_date=None,
        delivery_date=None,
        delivered_to_dp_date=None,
        completed_date=None,
        cancelled_date=None,
        return_date=None,
        cancel_reason=None,
        identifier_required=False,
        stock_id="42",
        stock_title="Asosiy ombor",
        drop_off_point_uuid="dp-uuid-001",
        drop_off_point_address="Chilonzor PVZ",
        invoice_number="INV-001",
        items_json=[{"sku": "SKU-1", "qty": 2}],
        raw_json={},
        synced_at=datetime(2026, 5, 21, 13, 50, 0),
    )
    defaults.update(overrides)
    return FbsOrder(**defaults)


class TestRowToDict:
    """Inverse of dict_from_order: builds the Uzum-shaped response
    the templates already render. Keys must match Uzum's wire format
    exactly — templates dereference these names without aliasing.
    """

    def test_top_level_keys_match_uzum_shape(self):
        d = row_to_dict(_make_fbs_row())
        # These names map directly to ``fbs_orders.html`` template
        # bindings — renaming any of them is a UI breakage.
        assert d["id"] == "1234567890"
        assert d["status"] == "CREATED"
        assert d["scheme"] == "FBS"
        assert d["shopId"] == "5983"
        assert d["price"] == 150000
        assert d["invoiceNumber"] == "INV-001"

    def test_dates_serialized_with_z_suffix(self):
        d = row_to_dict(_make_fbs_row())
        # Frontend parses these with ``new Date(...)``; without the Z
        # suffix the browser would read them as local time and the
        # order timeline would appear shifted by TZ offset.
        assert d["dateCreated"] == "2026-05-21T13:43:36.190Z"
        assert d["acceptUntil"] == "2026-05-22T13:43:36.190Z"
        assert d["deliverUntil"] == "2026-05-25T13:43:36.190Z"

    def test_null_dates_serialized_as_none(self):
        d = row_to_dict(_make_fbs_row())
        # CREATED order has no lifecycle dates yet — those must be JSON
        # null, not the string "None" or epoch zero.
        assert d["acceptedDate"] is None
        assert d["deliveringDate"] is None
        assert d["deliveryDate"] is None
        assert d["completedDate"] is None
        assert d["dateCancelled"] is None
        assert d["returnDate"] is None

    def test_delivery_info_renested(self):
        d = row_to_dict(_make_fbs_row())
        # Flat DB columns → Uzum's nested deliveryInfo object — exact
        # key names must match what the template renders.
        assert d["deliveryInfo"] == {
            "customerFullname": "Test Customer",
            "customerPhone": "+998901234567",
            "deliveryAddress": "Toshkent",
            "deliveryComment": "2-qavat",
        }

    def test_stock_and_dropoff_renested(self):
        d = row_to_dict(_make_fbs_row())
        assert d["stock"] == {"id": "42", "title": "Asosiy ombor"}
        assert d["dropOffPoint"] == {
            "uuid": "dp-uuid-001",
            "address": "Chilonzor PVZ",
        }

    def test_order_items_passed_through(self):
        d = row_to_dict(_make_fbs_row())
        assert d["orderItems"] == [{"sku": "SKU-1", "qty": 2}]

    def test_order_items_none_becomes_empty_list(self):
        # Older rows might have items_json = NULL (pre-server-default).
        # Defensive ``or []`` keeps the template's ``{% for %}`` happy.
        d = row_to_dict(_make_fbs_row(items_json=None))
        assert d["orderItems"] == []

    def test_identifier_required_coerced_to_bool(self):
        # Truthy non-bool DB values (e.g. raw 1 from a future schema
        # change) should still serialize as JSON true/false.
        d = row_to_dict(_make_fbs_row(identifier_required=True))
        assert d["identifierRequired"] is True

    def test_raw_json_fields_merged_into_top_level(self):
        # raw_json forward-compat: a Uzum response field that doesn't
        # have its own DB column today should still appear in the
        # output dict so the template can pick it up. The merge happens
        # at the bottom of row_to_dict, after the explicit projection.
        row = _make_fbs_row(raw_json={
            "futureField": "value-xyz",
            "anotherField": 42,
        })
        d = row_to_dict(row)
        assert d["futureField"] == "value-xyz"
        assert d["anotherField"] == 42

    def test_raw_json_does_not_overwrite_projected_columns(self):
        # If raw_json contains a key we explicitly project (e.g. a stale
        # ``status`` value from when the row was synced), the projection
        # wins. Otherwise raw_json could overwrite a coerced datetime
        # with the original string.
        row = _make_fbs_row(
            status="DELIVERING",
            raw_json={"status": "CREATED", "dateCreated": "stale-string"},
        )
        d = row_to_dict(row)
        assert d["status"] == "DELIVERING"
        assert d["dateCreated"] == "2026-05-21T13:43:36.190Z"

    def test_include_raw_false_skips_raw_merge(self):
        # Bosqich A.5 #2: list views call row_to_dict(include_raw=False)
        # and the query defers raw_json. The forward-compat merge must be
        # skipped so the payload doesn't carry the full duplicate original
        # order — but the projection + orderItems (which the list render
        # actually uses) must still be present.
        row = _make_fbs_row(raw_json={
            "futureField": "value-xyz",
            "carrierCode": "EXPRESS",
        })
        d = row_to_dict(row, include_raw=False)
        assert "futureField" not in d
        assert "carrierCode" not in d
        # Everything the list's renderOrders reads is still here.
        assert d["id"] == "1234567890"
        assert d["status"] == "CREATED"
        assert d["scheme"] == "FBS"
        assert d["price"] == 150000
        assert d["dateCreated"] == "2026-05-21T13:43:36.190Z"
        assert d["deliveryInfo"]["customerFullname"] == "Test Customer"
        assert d["orderItems"] == [{"sku": "SKU-1", "qty": 2}]

    def test_include_raw_default_true_still_merges(self):
        # Default (detail view) keeps the raw_json merge so a not-yet-
        # projected field is still surfaced. Explicit True == default.
        row = _make_fbs_row(raw_json={"futureField": "kept"})
        assert row_to_dict(row)["futureField"] == "kept"
        assert row_to_dict(row, include_raw=True)["futureField"] == "kept"

    def test_include_raw_false_never_dereferences_raw_json(self):
        # Critical for the defer() optimization in _read_orders_from_db /
        # get_fbs_orders_for_shops: with include_raw=False the function
        # must NOT touch row.raw_json at all. A deferred column would
        # otherwise lazy-load on a session that's already closed (the dict
        # conversion happens after the `with SessionLocal()` block), which
        # is either an N+1 query or a DetachedInstanceError. Guard it with
        # a row proxy whose raw_json access blows up.
        base = _make_fbs_row()

        class _RaisingRawRow:
            # No instance attrs → every access routes through __getattr__,
            # which proxies to `base` except for the forbidden raw_json.
            def __getattr__(self, name):
                if name == "raw_json":
                    raise AssertionError(
                        "raw_json must not be read when include_raw=False"
                    )
                return getattr(base, name)

        d = row_to_dict(_RaisingRawRow(), include_raw=False)
        assert d["id"] == "1234567890"
        assert d["orderItems"] == [{"sku": "SKU-1", "qty": 2}]

    # ── Bosqich A.14 (HAR audit B1/B2) — publicId + issueCodeRetriesLeft ──
    def test_public_id_surfaced_from_raw_json(self):
        # B1: the seller-facing short order number lives only in raw_json
        # (no column). The detail view (include_raw=True) must surface it so
        # the header can show "856114-0035" instead of the long internal id.
        row = _make_fbs_row(raw_json={"publicId": "856114-0035"})
        assert row_to_dict(row)["publicId"] == "856114-0035"

    def test_public_id_absent_when_include_raw_false(self):
        # List views defer raw_json — publicId simply isn't present there
        # (no crash, just missing). Header in the list keeps the internal id.
        row = _make_fbs_row(raw_json={"publicId": "856114-0035"})
        assert "publicId" not in row_to_dict(row, include_raw=False)

    def test_issue_code_retries_left_surfaced_into_delivery_info(self):
        # B2: DBS-only field nested under deliveryInfo, which the merge
        # excludes — must be surfaced explicitly onto the projected object.
        row = _make_fbs_row(
            order_type="DBS",
            raw_json={"deliveryInfo": {
                "customerFullname": "Anvar",
                "issueCodeRetriesLeft": 3,
            }},
        )
        di = row_to_dict(row)["deliveryInfo"]
        assert di["issueCodeRetriesLeft"] == 3
        # Projected columns still win over raw_json's copy.
        assert di["customerFullname"] == "Test Customer"

    def test_issue_code_retries_left_zero_is_surfaced(self):
        # 0 retries (about to lock) must pass through — `is not None` guard,
        # not a truthiness check that would drop a legitimate zero.
        row = _make_fbs_row(
            order_type="DBS",
            raw_json={"deliveryInfo": {"issueCodeRetriesLeft": 0}},
        )
        assert row_to_dict(row)["deliveryInfo"]["issueCodeRetriesLeft"] == 0

    def test_issue_code_retries_left_absent_for_fbs(self):
        # FBS orders have an empty deliveryInfo → no retries key leaks in.
        row = _make_fbs_row(order_type="FBS", raw_json={"deliveryInfo": {}})
        assert "issueCodeRetriesLeft" not in row_to_dict(row)["deliveryInfo"]


# ─────────────────────────────────────────────────────────────────────
# dict_from_order ↔ row_to_dict round trip
# ─────────────────────────────────────────────────────────────────────


class TestRoundTrip:
    """Uzum payload → dict_from_order → FbsOrder → row_to_dict should
    preserve every UI-visible field. This is the contract the cache
    relies on: a seller refreshing /fbs sees the same shape Uzum would
    have returned directly.
    """

    def test_uzum_dict_round_trip(self):
        original = _minimal_uzum_order()
        cols = dict_from_order(5983, original)
        # synced_at is added by dict_from_order — not part of the Uzum
        # payload, so strip it before constructing the ORM object's
        # mirror to make the test focused on the conversion contract.
        row = FbsOrder(**cols)
        out = row_to_dict(row)

        # Spot-check the round-tripped fields — full equality wouldn't
        # work because row_to_dict drops Python-side fields like
        # synced_at and adds raw_json merging.
        assert out["id"] == str(original["id"])
        assert out["scheme"] == original["scheme"]
        assert out["status"] == original["status"]
        assert out["price"] == original["price"]
        assert out["dateCreated"] == original["dateCreated"]
        assert out["deliveryInfo"]["customerFullname"] == \
            original["deliveryInfo"]["customerFullname"]
        assert out["stock"]["title"] == original["stock"]["title"]


# ─────────────────────────────────────────────────────────────────────
# upsert_orders chunking
# ─────────────────────────────────────────────────────────────────────


class _CountingDb:
    """Tiny fake of an SQLAlchemy session that only counts ``execute``
    calls. Inspecting bound parameters out of a pg_insert(...).values([...])
    statement is brittle across SQLAlchemy versions, so the tests below
    verify the chunk count + the function's return value (which already
    equals the total row count). Together they pin the chunking contract.
    """

    def __init__(self):
        self.n_calls: int = 0

    def execute(self, stmt) -> None:
        self.n_calls += 1


def _bulk_uzum_orders(n: int, start_id: int = 1) -> list[dict]:
    """N minimal Uzum orders with distinct IDs."""
    return [_minimal_uzum_order(id=start_id + i) for i in range(n)]


def _expected_chunks(n: int) -> int:
    """Number of execute() calls upsert_orders should issue for n rows."""
    if n <= 0:
        return 0
    return (n + _UPSERT_CHUNK_SIZE - 1) // _UPSERT_CHUNK_SIZE


class TestUpsertChunking:
    """``upsert_orders`` MUST split its INSERT into chunks of
    ``_UPSERT_CHUNK_SIZE`` rows to stay under Postgres's 65535 bound-
    parameter wire-protocol cap. A regression here means a freshly
    attached shop with full history dies on its first sync tick with
    ``too many parameters``.
    """

    def test_chunk_size_is_safe(self):
        # 28 projected columns × 1000 rows = 28000 parameters, well
        # under the 65535 cap with headroom for future column additions.
        assert _UPSERT_CHUNK_SIZE <= 2000

    def test_empty_orders_no_executes(self):
        db = _CountingDb()
        n = upsert_orders(db, 5983, orders=[])
        assert n == 0
        assert db.n_calls == 0

    def test_single_chunk_one_execute(self):
        db = _CountingDb()
        orders = _bulk_uzum_orders(50)
        n = upsert_orders(db, 5983, orders=orders)
        assert n == 50
        assert db.n_calls == 1

    def test_exact_chunk_boundary_one_execute(self):
        # Exactly _UPSERT_CHUNK_SIZE rows → exactly one execute,
        # not two (an off-by-one would issue an empty second statement).
        db = _CountingDb()
        orders = _bulk_uzum_orders(_UPSERT_CHUNK_SIZE)
        n = upsert_orders(db, 5983, orders=orders)
        assert n == _UPSERT_CHUNK_SIZE
        assert db.n_calls == 1

    def test_above_boundary_splits(self):
        # _UPSERT_CHUNK_SIZE + 1 → two executes (full chunk + remainder).
        db = _CountingDb()
        orders = _bulk_uzum_orders(_UPSERT_CHUNK_SIZE + 1)
        n = upsert_orders(db, 5983, orders=orders)
        assert n == _UPSERT_CHUNK_SIZE + 1
        assert db.n_calls == 2

    def test_large_payload_splits(self):
        # 2500 orders — the scale a fresh shop with full history hits
        # on its first tick. Must split into 3 statements.
        db = _CountingDb()
        total = 2500
        orders = _bulk_uzum_orders(total)
        n = upsert_orders(db, 5983, orders=orders)
        assert n == total
        assert db.n_calls == _expected_chunks(total)

    def test_huge_payload_does_not_overflow_param_limit(self):
        # 5500 rows is what an 11-status sync can easily produce. Pre-
        # chunking, this would be ~150k bound parameters — past the
        # 65535 wire-protocol cap. With chunking it must still split
        # cleanly into N statements of <= _UPSERT_CHUNK_SIZE rows each.
        db = _CountingDb()
        total = 5500
        orders = _bulk_uzum_orders(total)
        n = upsert_orders(db, 5983, orders=orders)
        assert n == total
        assert db.n_calls == _expected_chunks(total)

    def test_bad_status_skipped_does_not_break_chunking(self):
        # Orders with unknown status are dropped before chunking — the
        # return value and chunk count reflect only the valid rows.
        db = _CountingDb()
        orders = _bulk_uzum_orders(_UPSERT_CHUNK_SIZE + 5)
        for idx in (10, 20, 30):
            orders[idx]["status"] = "BOGUS_STATUS"
        n = upsert_orders(db, 5983, orders=orders)
        expected = _UPSERT_CHUNK_SIZE + 5 - 3
        assert n == expected
        assert db.n_calls == _expected_chunks(expected)
