"""Tests for Bosqich A.9 (#5) — interactive fail-fast retry.

The shared ``core.http_client`` session retries 429/5xx with 60/120/180s
additive backoff (total=3) INSIDE one ``sess.request()`` call — up to
~6 minutes. That is fine for the background fbs-sync worker but pins a
gunicorn worker thread for the whole interactive Yangilash/action path
(the browser's AbortController only frees the browser, not the server
thread). Under load that starves the pool and can hang the whole app.

#5 adds an FBS-scoped ``_get_fbs_fastfail_session()`` (``Retry(total=0)``
= zero retries, zero backoff) selected per-call via an explicit
``fail_fast`` argument. Interactive callers pass ``fail_fast=True``; the
background worker keeps the default ``False`` (patient). These tests pin:

  * the fast-fail session is genuinely zero-retry and a DISTINCT object
    from the shared session (so the app-wide patient policy is untouched);
  * the chokepoint selects the right session from the flag;
  * a 429 returns in exactly ONE round-trip (no retry loop in our code);
  * ``fail_fast`` is forwarded correctly through page/count/fetch_all_pages;
  * the interactive JIT refresh callers pass ``True`` while the worker
    path defaults to ``False`` (the interactive-vs-background split);
  * the route maps a 429 to the friendly "Uzum band, kuting" wait-hint
    with HTTP 429 — even when the 429 also carries a seller-order code.

Everything is mocked: no real Uzum, no real sleeping, no Postgres
(conftest stubs extensions+redis).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from core import uzum_openapi
from core import fbs_sync
from core import fbs_data


# ── helpers ───────────────────────────────────────────────────────────
def _fake_response(status=200, text='{"payload": {}}'):
    r = MagicMock(name="response")
    r.status_code = status
    r.text = text
    return r


def _fake_session(status=200, text='{"payload": {}}'):
    """A MagicMock standing in for a requests.Session whose .request()
    returns a single fake response (no retry loop of its own)."""
    s = MagicMock(name="session")
    s.request.return_value = _fake_response(status, text)
    return s


def _make_session_cm_mock():
    """``with SessionLocal() as db:`` context-manager mock (db is a no-op)."""
    db = MagicMock(name="db")
    session = MagicMock(name="SessionLocal")
    session.return_value.__enter__.return_value = db
    session.return_value.__exit__.return_value = False
    return session, db


# ── the fast-fail session itself ──────────────────────────────────────
class TestFastFailSession:
    def test_session_is_zero_retry(self):
        sess = uzum_openapi._get_fbs_fastfail_session()
        retry = sess.get_adapter("https://api-seller.uzum.uz").max_retries
        assert retry.total == 0, "fast-fail session must do ZERO retries"
        assert 429 in retry.status_forcelist
        assert retry.raise_on_status is False, (
            "raise_on_status must be False so the 429/5xx response is "
            "RETURNED as the normal tuple (error shape preserved)"
        )
        # Document the zero-sleep guarantee: no backoff on a fresh Retry.
        assert retry.get_backoff_time() == 0

    def test_session_is_cached_per_thread(self):
        a = uzum_openapi._get_fbs_fastfail_session()
        b = uzum_openapi._get_fbs_fastfail_session()
        assert a is b, "same thread must reuse the one cached session"

    def test_session_distinct_from_shared_http_client_session(self):
        """The FBS fast-fail session must be a DIFFERENT object from the
        shared core.http_client session — otherwise editing one mutates
        the other and finance/products/POS lose their patient retry."""
        from core import http_client
        fast = uzum_openapi._get_fbs_fastfail_session()
        shared = http_client._get_http_session()
        assert fast is not shared


# ── chokepoint session selection + one-shot 429 ───────────────────────
class TestChokepointSelection:
    def test_fail_fast_true_uses_fastfail_session(self):
        fast, shared = _fake_session(), _fake_session()
        with patch.object(uzum_openapi, "_get_fbs_fastfail_session", return_value=fast), \
             patch.object(uzum_openapi, "_get_http_session", return_value=shared):
            uzum_openapi._fbs_orders_request_with_auth(
                "https://api-seller.uzum.uz/x", "tok",
                accept_language=None, debug_label="t", fail_fast=True)
        assert fast.request.called
        assert not shared.request.called

    def test_default_uses_shared_patient_session(self):
        fast, shared = _fake_session(), _fake_session()
        with patch.object(uzum_openapi, "_get_fbs_fastfail_session", return_value=fast), \
             patch.object(uzum_openapi, "_get_http_session", return_value=shared):
            uzum_openapi._fbs_orders_request_with_auth(
                "https://api-seller.uzum.uz/x", "tok",
                accept_language=None, debug_label="t")  # default fail_fast=False
        assert shared.request.called
        assert not fast.request.called

    def test_429_returns_in_one_round_trip(self):
        """A 429 on the fast-fail path returns the (parsed, 429, text,
        label) tuple after EXACTLY ONE request — no retry loop in our
        code. (The adapter-level zero-backoff is pinned separately.)"""
        fast = _fake_session(status=429, text='{"errors":[{"code":"seller-order-02"}]}')
        with patch.object(uzum_openapi, "_get_fbs_fastfail_session", return_value=fast):
            parsed, status, text, _label = uzum_openapi._fbs_orders_request_with_auth(
                "https://api-seller.uzum.uz/x", "tok",
                accept_language=None, debug_label="t", fail_fast=True)
        assert status == 429
        assert fast.request.call_count == 1


# ── fail_fast forwarding through the public funcs ─────────────────────
class TestForwarding:
    def test_page_forwards_fail_fast_true(self):
        seen = {}

        def fake_req(url, token, **kw):
            seen["fail_fast"] = kw.get("fail_fast")
            return ({"payload": {"orders": []}}, 200, "", "raw")

        with patch.object(uzum_openapi, "_fbs_orders_request_with_auth", side_effect=fake_req):
            uzum_openapi.fetch_fbs_orders_page("tok", "123", status="CREATED", fail_fast=True)
        assert seen["fail_fast"] is True

    def test_page_defaults_fail_fast_false(self):
        seen = {}

        def fake_req(url, token, **kw):
            seen["fail_fast"] = kw.get("fail_fast")
            return ({"payload": {"orders": []}}, 200, "", "raw")

        with patch.object(uzum_openapi, "_fbs_orders_request_with_auth", side_effect=fake_req):
            uzum_openapi.fetch_fbs_orders_page("tok", "123", status="CREATED")
        assert seen["fail_fast"] is False

    def test_count_forwards_fail_fast_true(self):
        seen = {}

        def fake_req(url, token, **kw):
            seen["fail_fast"] = kw.get("fail_fast")
            return ({"payload": 0}, 200, "", "raw")

        with patch.object(uzum_openapi, "_fbs_orders_request_with_auth", side_effect=fake_req), \
             patch.object(uzum_openapi, "pace_uzum_call"):
            uzum_openapi.fetch_fbs_orders_count("tok", "123", status="CREATED", fail_fast=True)
        assert seen["fail_fast"] is True

    def test_count_defaults_fail_fast_false(self):
        seen = {}

        def fake_req(url, token, **kw):
            seen["fail_fast"] = kw.get("fail_fast")
            return ({"payload": 0}, 200, "", "raw")

        with patch.object(uzum_openapi, "_fbs_orders_request_with_auth", side_effect=fake_req), \
             patch.object(uzum_openapi, "pace_uzum_call"):
            uzum_openapi.fetch_fbs_orders_count("tok", "123", status="CREATED")
        assert seen["fail_fast"] is False


# ── interactive-vs-background split (the safety invariant) ─────────────
class TestBackgroundStaysPatient:
    def test_fetch_all_pages_defaults_to_patient(self):
        """The background worker calls fetch_all_pages with the default —
        which MUST forward fail_fast=False (patient) to the page fetch."""
        seen = []

        def fake_page(token, shop, **kw):
            seen.append(kw.get("fail_fast"))
            return ({"payload": {"orders": []}}, "url")  # empty → stop after page 0

        with patch.object(fbs_sync, "fetch_fbs_orders_page", side_effect=fake_page), \
             patch.object(fbs_sync, "pace_uzum_call"):
            fbs_sync.fetch_all_pages("tok", "123", status="CREATED")
        assert seen == [False], "worker path must stay patient (fail_fast=False)"

    def test_fetch_all_pages_forwards_fail_fast_true(self):
        seen = []

        def fake_page(token, shop, **kw):
            seen.append(kw.get("fail_fast"))
            return ({"payload": {"orders": []}}, "url")

        with patch.object(fbs_sync, "fetch_fbs_orders_page", side_effect=fake_page), \
             patch.object(fbs_sync, "pace_uzum_call"):
            fbs_sync.fetch_all_pages("tok", "123", status="CREATED", fail_fast=True)
        assert seen == [True]


# ── interactive JIT refresh callers pass fail_fast=True ───────────────
class TestJitRefreshIsFastFail:
    def test_first_page_refresh_passes_fail_fast_true(self):
        seen = {}

        def fake_page(token, ids, **kw):
            seen["fail_fast"] = kw.get("fail_fast")
            return ({"payload": {"orders": []}}, "url")

        with patch.object(fbs_data, "fetch_fbs_orders_page", side_effect=fake_page), \
             patch.object(fbs_data, "pace_uzum_call"):
            fbs_data._refresh_shops_status_first_page("tok", ["123"], "CREATED")
        assert seen["fail_fast"] is True

    def test_counts_refresh_passes_fail_fast_true(self):
        seen = {}

        def fake_count(token, ids, **kw):
            seen["fail_fast"] = kw.get("fail_fast")
            return (0, "url")

        with patch.object(fbs_data, "fetch_fbs_orders_count", side_effect=fake_count):
            fbs_data._refresh_shops_counts("tok", ["123"], ["CREATED"])
        assert seen["fail_fast"] is True

    def test_multishop_status_refresh_passes_fail_fast_true(self):
        seen = {}

        def fake_all_pages(token, ids, **kw):
            seen["fail_fast"] = kw.get("fail_fast")
            return []

        session, _db = _make_session_cm_mock()
        with patch.object(fbs_data, "_fbs_fetch_all_pages", side_effect=fake_all_pages), \
             patch.object(fbs_data, "SessionLocal", session), \
             patch.object(fbs_data, "_fbs_upsert_orders", return_value=0):
            fbs_data._refresh_shops_status("tok", ["123", "456"], "CREATED")
        assert seen["fail_fast"] is True


# ── route 429 → friendly wait-hint ────────────────────────────────────
class TestRoute429Mapping:
    def test_429_maps_to_friendly_hint_even_with_seller_code(self):
        """A 429 — even one carrying a seller-order code AND a Russian
        message — must surface the passive 'Uzum band, kuting' wait-hint
        with HTTP 429, NOT the per-order message or a generic 400. The
        429 branch runs FIRST, before the code/message lookups."""
        from flask import Flask
        from fbs.routes import _uzum_error_response
        from core.uzum_openapi import UzumAPIError

        err = UzumAPIError(
            429, "seller-order-02", "Некоторая ошибка",
            '{"errors":[{"code":"seller-order-02"}]}', "https://u/x")
        app = Flask(__name__)
        with app.app_context():
            resp = _uzum_error_response(err)
        assert resp.status_code == 429
        data = resp.get_json()
        assert "band" in (data.get("error") or "").lower()
        assert data.get("uzum_http") == 429
