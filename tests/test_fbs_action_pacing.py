"""Burst-safety tests for the FBS *action* endpoints (Bosqich A.8 / #4).

Context
-------
The bulk routes fan out action calls on ONE seller token:

  * ``POST /fbs/api/orders/bulk-confirm`` → up to 5 concurrent
    ``confirm_fbs_order`` calls (``fbs/routes.py`` ThreadPoolExecutor).
  * ``POST /fbs/api/orders/bulk-labels``  → up to 5 concurrent
    ``download_fbs_label`` calls.

Uzum trips its hidden per-token burst penalty (HTTP 429 → 60-180s of
throttling) when more than one ``/v*/fbs/...`` call lands on a token
within the same second. The list/count paths already serialise through
``core.fbs_locks.pace_uzum_call`` (#1-#3), but the ACTION endpoints
(confirm / cancel / label / identifier) historically issued their Uzum
HTTP call WITHOUT that gate — so a 5-wide bulk confirm bursted the token.

These tests pin the fix: every action endpoint MUST reserve a per-token
slot via ``pace_uzum_call`` BEFORE issuing the HTTP request, using the
same (cleaned) token string the request uses — otherwise the gate would
key on a different token and not serialise against the real call.

Everything is mocked — no Uzum HTTP, no real sleeping, no Postgres. We
assert *wiring* (gate present, correct order, correct token), not timing;
the gate's spacing math is proven separately in ``test_fbs_locks.py``.
The point of pytest-first here is the ban risk: we must NOT validate
confirm/cancel/label against a real seller account to learn the gate is
wired in.
"""
from __future__ import annotations

import base64
from unittest.mock import patch

import pytest

from core import uzum_openapi


# A tiny, valid base64 PDF blob so ``download_fbs_label`` can decode the
# mocked ``payload.document`` without raising.
_FAKE_PDF_B64 = base64.b64encode(b"%PDF-1.4 fake-label").decode()


def _run_action_capturing_order(action_call, *, request_return):
    """Patch ``pace_uzum_call`` + ``_fbs_orders_request_with_auth`` on the
    ``core.uzum_openapi`` module, record the order in which they fire and
    the token each received, then run ``action_call()``.

    Returns ``(events, result)`` where ``events`` is a list like
    ``[("pace", token), ("request", token)]`` in call order.
    """
    events: list[tuple[str, str]] = []

    def fake_pace(token, *args, **kwargs):
        events.append(("pace", token))
        return 0.0

    def fake_request(url, token, *args, **kwargs):
        events.append(("request", token))
        return request_return

    with patch.object(uzum_openapi, "pace_uzum_call", side_effect=fake_pace), \
         patch.object(uzum_openapi, "_fbs_orders_request_with_auth",
                      side_effect=fake_request):
        result = action_call()
    return events, result


class TestActionEndpointPacing:
    """Each FBS action endpoint paces its token before the HTTP call."""

    def test_confirm_paces_before_request(self):
        events, result = _run_action_capturing_order(
            lambda: uzum_openapi.confirm_fbs_order("  tok-xyz  ", 108130896),
            request_return=({"payload": {"status": "PACKING"}}, 200, "", "raw"),
        )
        # Gate fires exactly once, BEFORE the request, on the cleaned token.
        assert events == [("pace", "tok-xyz"), ("request", "tok-xyz")], events
        # Sanity: the confirm still returns Uzum's echoed payload.
        payload, _url = result
        assert payload == {"status": "PACKING"}

    def test_cancel_paces_before_request(self):
        events, result = _run_action_capturing_order(
            lambda: uzum_openapi.cancel_fbs_order(
                "  tok-xyz  ", 42, reason="OUT_OF_STOCK"),
            request_return=({}, 200, "", "raw"),
        )
        assert events == [("pace", "tok-xyz"), ("request", "tok-xyz")], events

    def test_label_paces_before_request(self):
        events, result = _run_action_capturing_order(
            lambda: uzum_openapi.download_fbs_label("  tok-xyz  ", 42, size="LARGE"),
            request_return=(
                {"payload": {"document": _FAKE_PDF_B64}}, 200, "", "raw",
            ),
        )
        assert events == [("pace", "tok-xyz"), ("request", "tok-xyz")], events
        # Sanity: the label still decodes to the fake PDF bytes.
        pdfs, _url = result
        assert pdfs == [b"%PDF-1.4 fake-label"]

    def test_identifier_paces_before_request(self):
        events, _result = _run_action_capturing_order(
            lambda: uzum_openapi.attach_fbs_identifiers(
                "  tok-xyz  ", 42,
                items=[{"orderItemId": 7, "values": ["IMEI-1"]}]),
            request_return=({"payload": [{"type": "IMEI"}]}, 200, "", "raw"),
        )
        assert events == [("pace", "tok-xyz"), ("request", "tok-xyz")], events

    @pytest.mark.parametrize("action_name", [
        "confirm_fbs_order", "cancel_fbs_order",
        "download_fbs_label", "attach_fbs_identifiers",
    ])
    def test_pace_token_matches_request_token(self, action_name):
        """The token handed to the gate must be byte-for-byte the token
        handed to the HTTP layer — otherwise the gate keys on a different
        string and never serialises the real call.

        Parametrized so adding a new action endpoint without pacing it
        will surface here (the dispatch below builds the right call)."""
        dispatch = {
            "confirm_fbs_order": (
                lambda: uzum_openapi.confirm_fbs_order("tok-xyz", 42),
                ({"payload": {}}, 200, "", "raw"),
            ),
            "cancel_fbs_order": (
                lambda: uzum_openapi.cancel_fbs_order(
                    "tok-xyz", 42, reason="OUT_OF_STOCK"),
                ({}, 200, "", "raw"),
            ),
            "download_fbs_label": (
                lambda: uzum_openapi.download_fbs_label("tok-xyz", 42),
                ({"payload": {"document": _FAKE_PDF_B64}}, 200, "", "raw"),
            ),
            "attach_fbs_identifiers": (
                lambda: uzum_openapi.attach_fbs_identifiers(
                    "tok-xyz", 42,
                    items=[{"orderItemId": 7, "values": ["IMEI-1"]}]),
                ({"payload": []}, 200, "", "raw"),
            ),
        }
        action_call, request_return = dispatch[action_name]
        events, _ = _run_action_capturing_order(
            action_call, request_return=request_return)

        pace_events = [e for e in events if e[0] == "pace"]
        request_events = [e for e in events if e[0] == "request"]
        assert len(pace_events) == 1, f"{action_name}: expected 1 pace, got {pace_events}"
        assert len(request_events) == 1, f"{action_name}: expected 1 request, got {request_events}"
        assert pace_events[0][1] == request_events[0][1], (
            f"{action_name}: gate token {pace_events[0][1]!r} != "
            f"request token {request_events[0][1]!r}"
        )
