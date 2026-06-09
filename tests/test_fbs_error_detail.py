"""Tests for the enriched Uzum FBS error handling (2026-06-02).

Background
----------
When Uzum rejects an FBS/DBS action it returns a structured envelope:

    {"payload": null,
     "errors": [{"code": "seller-order-03",
                 "message": "...",
                 "payload": {"deadline": "2026-06-01T12:00:00Z"}}],
     "timestamp": "2026-06-02T08:00:00Z",
     "trace": "abc123"}

Previously only ``errors[0].code`` + ``.message`` were read. This suite
covers the new diagnostic plumbing that ALSO reads ``errors[].payload``,
top-level ``trace`` and ``timestamp``:

  * ``core.uzum_openapi._extract_uzum_error_detail`` — the extractor.
  * ``core.uzum_openapi.UzumAPIError`` — now carries the extra fields.
  * ``core.uzum_openapi._raise_uzum_error`` — populates them.
  * ``fbs.routes._format_iso_to_tashkent`` — ISO → Tashkent-local string.
  * ``fbs.routes._format_uzum_payload_detail`` — payload → UZ suffix.
  * ``fbs.routes._uzum_error_response`` — surfaces trace/timestamp/payload
    in the JSON and enriches the message with the deadline.

All pure / mocked — no Uzum HTTP, no real action against a seller account.
"""
from __future__ import annotations

import json

import pytest

from core.uzum_openapi import (
    _extract_uzum_error_detail,
    _raise_uzum_error,
    UzumAPIError,
)
from fbs.routes import (
    _format_iso_to_tashkent,
    _format_uzum_payload_detail,
    _uzum_error_response,
)


# ── _extract_uzum_error_detail ───────────────────────────────────────
class TestExtractUzumErrorDetail:
    def test_full_envelope(self):
        parsed = {
            "errors": [{
                "code": "seller-order-03",
                "message": "Срок подтверждения истёк",
                "payload": {"deadline": "2026-06-01T12:00:00Z"},
            }],
            "timestamp": "2026-06-02T08:00:00Z",
            "trace": "abc123",
        }
        out = _extract_uzum_error_detail(parsed)
        assert out["code"] == "seller-order-03"
        assert out["message"] == "Срок подтверждения истёк"
        assert out["payload"] == {"deadline": "2026-06-01T12:00:00Z"}
        assert out["trace"] == "abc123"
        assert out["timestamp"] == "2026-06-02T08:00:00Z"

    def test_envelope_without_errors_still_yields_trace(self):
        # A 5xx or throttle envelope may carry trace/timestamp but no
        # per-error entry — trace must still come through for diagnostics.
        parsed = {"errors": [], "trace": "t-9", "timestamp": "2026-06-02T00:00:00Z"}
        out = _extract_uzum_error_detail(parsed)
        assert out["code"] is None
        assert out["message"] is None
        assert out["payload"] is None
        assert out["trace"] == "t-9"
        assert out["timestamp"] == "2026-06-02T00:00:00Z"

    def test_empty_payload_is_dropped(self):
        # Empty containers / empty string → treated as "no detail".
        for empty in ({}, [], ""):
            parsed = {"errors": [{"code": "x", "payload": empty}]}
            assert _extract_uzum_error_detail(parsed)["payload"] is None

    def test_non_dict_input(self):
        for bad in (None, "oops", 42, ["a"]):
            out = _extract_uzum_error_detail(bad)
            assert out == {
                "code": None, "message": None, "payload": None,
                "trace": None, "timestamp": None,
            }

    def test_first_error_not_a_dict(self):
        parsed = {"errors": ["just a string"], "trace": "z"}
        out = _extract_uzum_error_detail(parsed)
        assert out["code"] is None
        assert out["trace"] == "z"

    def test_list_payload_preserved(self):
        # payload need not be a dict — a non-empty list rides through verbatim.
        parsed = {"errors": [{"code": "c", "payload": [{"field": "qty"}]}]}
        assert _extract_uzum_error_detail(parsed)["payload"] == [{"field": "qty"}]


# ── UzumAPIError attributes ──────────────────────────────────────────
class TestUzumAPIErrorAttributes:
    def test_defaults_are_none(self):
        err = UzumAPIError(400, "code", "msg", "body", "http://u")
        assert err.error_payload is None
        assert err.trace is None
        assert err.timestamp is None

    def test_explicit_fields(self):
        err = UzumAPIError(
            400, "code", "msg", "body", "http://u",
            error_payload={"k": "v"}, trace="tr", timestamp="ts",
        )
        assert err.error_payload == {"k": "v"}
        assert err.trace == "tr"
        assert err.timestamp == "ts"
        # The trace shows up in the str() so logs are useful.
        assert "tr" in str(err)


# ── _raise_uzum_error ────────────────────────────────────────────────
class TestRaiseUzumError:
    def test_populates_from_parsed(self):
        parsed = {
            "errors": [{"code": "seller-order-03", "message": "m",
                        "payload": {"deadline": "2026-06-01T12:00:00Z"}}],
            "trace": "T1", "timestamp": "2026-06-02T00:00:00Z",
        }
        with pytest.raises(UzumAPIError) as ei:
            _raise_uzum_error(parsed, 400, json.dumps(parsed), "http://u")
        err = ei.value
        assert err.code == "seller-order-03"
        assert err.error_payload == {"deadline": "2026-06-01T12:00:00Z"}
        assert err.trace == "T1"
        assert err.timestamp == "2026-06-02T00:00:00Z"

    def test_reparses_text_when_parsed_is_none(self):
        # Mirrors the real path: _fbs_orders_request_with_auth sometimes
        # returns parsed=None even though the body holds the errors[].
        body = json.dumps({"errors": [{"code": "seller-order-13"}], "trace": "T2"})
        with pytest.raises(UzumAPIError) as ei:
            _raise_uzum_error(None, 400, body, "http://u")
        assert ei.value.code == "seller-order-13"
        assert ei.value.trace == "T2"


# ── _format_iso_to_tashkent ──────────────────────────────────────────
class TestFormatIsoToTashkent:
    def test_utc_z_suffix_shifts_plus5(self):
        assert _format_iso_to_tashkent("2026-06-01T12:00:00Z") == "01.06.2026 17:00"

    def test_explicit_tashkent_offset_unchanged(self):
        assert _format_iso_to_tashkent("2026-06-01T12:00:00+05:00") == "01.06.2026 12:00"

    def test_naive_assumed_tashkent(self):
        assert _format_iso_to_tashkent("2026-06-01T12:00:00") == "01.06.2026 12:00"

    def test_garbage_returns_none(self):
        for bad in (None, "", "nope", 123, "2026", "2026/06/01"):
            assert _format_iso_to_tashkent(bad) is None


# ── _format_uzum_payload_detail ──────────────────────────────────────
class TestFormatUzumPayloadDetail:
    def test_deadline_key(self):
        out = _format_uzum_payload_detail({"deadline": "2026-06-01T12:00:00Z"})
        assert out == "muddat: 01.06.2026 17:00"

    def test_value_looks_like_date_without_date_key(self):
        # Key name gives no hint but the value is clearly an ISO timestamp.
        out = _format_uzum_payload_detail({"acceptBy": "2026-06-01T09:00:00Z"})
        assert out == "muddat: 01.06.2026 14:00"

    def test_no_date_returns_none(self):
        assert _format_uzum_payload_detail({"field": "quantity", "max": "5"}) is None

    def test_non_dict_and_empty(self):
        for bad in (None, {}, [], "x", 5, [{"a": 1}]):
            assert _format_uzum_payload_detail(bad) is None


# ── _uzum_error_response (needs Flask app context) ───────────────────
@pytest.fixture()
def app_ctx():
    from flask import Flask
    app = Flask(__name__)
    with app.app_context():
        yield app


def _body(resp):
    return json.loads(resp.get_data(as_text=True))


class TestUzumErrorResponse:
    def test_known_code_enriched_with_deadline(self, app_ctx):
        err = UzumAPIError(
            400, "seller-order-03", "Срок истёк", "{}", "http://u",
            error_payload={"deadline": "2026-06-01T12:00:00Z"},
            trace="TR", timestamp="2026-06-02T00:00:00Z",
        )
        resp = _uzum_error_response(err)
        assert resp.status_code == 400
        data = _body(resp)
        # Curated UZ message + the deadline distilled from payload.
        assert data["error"] == "Tasdiqlash muddati o'tib ketgan (muddat: 01.06.2026 17:00)"
        assert data["uzum_code"] == "seller-order-03"
        assert data["uzum_trace"] == "TR"
        assert data["uzum_timestamp"] == "2026-06-02T00:00:00Z"
        assert data["uzum_payload"] == {"deadline": "2026-06-01T12:00:00Z"}

    def test_429_carries_trace_and_waits(self, app_ctx):
        err = UzumAPIError(
            429, None, None, "", "http://u",
            trace="TR429", timestamp="2026-06-02T00:00:00Z",
        )
        resp = _uzum_error_response(err)
        assert resp.status_code == 429
        data = _body(resp)
        assert "band" in data["error"]
        assert data["uzum_http"] == 429
        assert data["uzum_trace"] == "TR429"

    def test_unknown_code_falls_back_to_uzum_message(self, app_ctx):
        err = UzumAPIError(
            400, "totally-new-code", "Незнакомая ошибка", "{}", "http://u",
            trace="TRx",
        )
        resp = _uzum_error_response(err)
        data = _body(resp)
        assert data["error"] == "Незнакомая ошибка"
        assert data["uzum_trace"] == "TRx"

    def test_5xx_maps_to_502(self, app_ctx):
        err = UzumAPIError(503, None, None, "<html>502</html>", "http://u",
                           trace="T5")
        resp = _uzum_error_response(err)
        assert resp.status_code == 502
        data = _body(resp)
        assert "vaqtincha ishlamayapti" in data["error"]
        # No code → raw body surfaced for diagnosis.
        assert data["uzum_raw"]
        assert data["uzum_trace"] == "T5"

    def test_no_payload_message_not_enriched(self, app_ctx):
        err = UzumAPIError(400, "seller-order-13", None, "{}", "http://u")
        resp = _uzum_error_response(err)
        data = _body(resp)
        assert data["error"] == "Buyurtma allaqachon bekor qilingan"
        assert data["uzum_payload"] is None
