"""Burst-safety tests for the FBS per-token bucket (Bosqich A.11).

Context
-------
The bulk routes fan out action calls on ONE seller token:

  * ``POST /fbs/api/orders/bulk-confirm`` → up to 5 concurrent
    ``confirm_fbs_order`` calls (``fbs/routes.py`` ThreadPoolExecutor).
  * ``POST /fbs/api/orders/bulk-labels``  → up to 5 concurrent
    ``download_fbs_label`` calls.

Uzum trips its hidden per-token burst penalty (HTTP 429) when more than one
``/v*/fbs/...`` call lands on a token within the same second.

Bosqich A.11 moved pacing OUT of the old per-call-site
``core.fbs_locks.pace_uzum_call`` gate and INTO the single chokepoint
``_fbs_orders_request_with_auth``: it now reserves a slot in the shared
per-token bucket (``get_bucket_for_token(token).acquire()``) BEFORE every
HTTP attempt — for reads AND writes, fail-fast AND patient. The bucket is
Redis-backed (cross-process: gunicorn workers + bg worker + finance/products
all share one budget per Uzum token) and degrades to an in-process token
bucket if Redis is down, so FBS is never left unpaced.

These tests pin the wiring at the chokepoint (gate present, fires BEFORE the
request, keyed on the passed token) plus that the action endpoints route
through that chokepoint so they inherit the gate. The bucket's own pacing
math lives in ``core.http_client`` and is exercised there; the old
``pace_uzum_call`` spacing math is still covered by ``test_fbs_locks.py``.

Everything is mocked — no Uzum HTTP, no real sleeping, no Postgres. The
point of pytest-first here is the ban risk: we must NOT validate
confirm/cancel/label against a real seller account to learn the gate is
wired in.
"""
from __future__ import annotations

import base64
from unittest.mock import MagicMock, patch

import pytest

from core import uzum_openapi


# A tiny, valid base64 PDF blob so ``download_fbs_label`` can decode the
# mocked ``payload.document`` without raising.
_FAKE_PDF_B64 = base64.b64encode(b"%PDF-1.4 fake-label").decode()


def _fake_response(status=200, text='{"payload": {}}'):
    r = MagicMock(name="response")
    r.status_code = status
    r.text = text
    return r


class TestChokepointBucketWiring:
    """The chokepoint reserves a per-token bucket slot before every call."""

    def _drive(self, *, method="GET", token="  tok-xyz  ",
               status=200, text='{"payload": {}}'):
        """Run the REAL ``_fbs_orders_request_with_auth`` with a mocked
        session and a recording bucket.

        Returns ``(events, gate_tokens)`` where ``events`` is the ordered
        list of ``"acquire"`` / ``"request"`` strings and ``gate_tokens`` is
        every token ``get_bucket_for_token`` was handed.
        """
        events: list[str] = []
        gate_tokens: list[str] = []

        sess = MagicMock(name="session")
        sess.request.side_effect = lambda *a, **k: (
            events.append("request") or _fake_response(status, text)
        )

        bucket = MagicMock(name="bucket")
        bucket.acquire.side_effect = lambda: events.append("acquire")

        def _get_bucket(tok):
            gate_tokens.append(tok)
            return bucket

        with patch.object(uzum_openapi, "_get_fbs_fastfail_session", return_value=sess), \
             patch.object(uzum_openapi, "_get_http_session", return_value=sess), \
             patch.object(uzum_openapi, "get_bucket_for_token", side_effect=_get_bucket):
            uzum_openapi._fbs_orders_request_with_auth(
                "https://api-seller.uzum.uz/x", token,
                method=method,
                json_body=({"x": 1} if method == "POST" else None),
                accept_language=None, debug_label="t", fail_fast=True)
        return events, gate_tokens

    def test_acquire_fires_before_request_on_read(self):
        events, _ = self._drive(method="GET")
        assert events == ["acquire", "request"], events

    def test_acquire_fires_before_request_on_write(self):
        events, _ = self._drive(method="POST")
        assert events == ["acquire", "request"], events

    def test_bucket_keyed_on_passed_token(self):
        """The chokepoint hands the bucket the token it received (the public
        funcs clean it first — see TestActionsRouteThroughChokepoint)."""
        _events, gate_tokens = self._drive(token="tok-xyz")
        assert gate_tokens == ["tok-xyz"], gate_tokens


class TestActionsRouteThroughChokepoint:
    """Every action endpoint issues its Uzum call via the gated chokepoint
    (so it inherits the per-token bucket), using the cleaned token."""

    @pytest.mark.parametrize("action_name", [
        "confirm_fbs_order", "cancel_fbs_order",
        "download_fbs_label", "attach_fbs_identifiers",
    ])
    def test_action_uses_chokepoint_with_clean_token(self, action_name):
        dispatch = {
            "confirm_fbs_order": (
                lambda: uzum_openapi.confirm_fbs_order("  tok-xyz  ", 42),
                ({"payload": {"status": "PACKING"}}, 200, "", "raw"),
            ),
            "cancel_fbs_order": (
                lambda: uzum_openapi.cancel_fbs_order(
                    "  tok-xyz  ", 42, reason="OUT_OF_STOCK"),
                ({}, 200, "", "raw"),
            ),
            "download_fbs_label": (
                lambda: uzum_openapi.download_fbs_label("  tok-xyz  ", 42, size="LARGE"),
                ({"payload": {"document": _FAKE_PDF_B64}}, 200, "", "raw"),
            ),
            "attach_fbs_identifiers": (
                lambda: uzum_openapi.attach_fbs_identifiers(
                    "  tok-xyz  ", 42,
                    items=[{"orderItemId": 7, "values": ["IMEI-1"]}]),
                ({"payload": [{"type": "IMEI"}]}, 200, "", "raw"),
            ),
        }
        action_call, request_return = dispatch[action_name]

        seen: dict[str, str] = {}

        def fake_req(url, token, *args, **kwargs):
            seen["token"] = token
            return request_return

        with patch.object(uzum_openapi, "_fbs_orders_request_with_auth",
                          side_effect=fake_req):
            action_call()

        assert seen.get("token") == "tok-xyz", (
            f"{action_name}: chokepoint got token {seen.get('token')!r}, "
            "expected the cleaned 'tok-xyz'"
        )
