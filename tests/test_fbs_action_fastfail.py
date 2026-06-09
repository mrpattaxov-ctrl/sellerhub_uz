"""Bosqich A.9 (#5b) — fail-fast for the FBS/DBS *action* endpoints.

These go through the 3-layer chain  routes → fbs_data WRAPPER → openapi
func → ``_fbs_orders_request_with_auth``. #5b threads ``fail_fast`` along
every link so an interactive confirm/cancel/label/identifier/DBS call
that hits a 429/5xx fails in ~2-3s (zero-retry FBS session) instead of
pinning the gunicorn worker thread for ~6 minutes of additive backoff.

The CRITICAL invariant (flagged by the design review): ``cancel_order``
spawns a Phase-4 background daemon that re-hits the chokepoint AFTER the
response is sent. That daemon MUST stay PATIENT (``fail_fast=False``) even
though the user-facing cancel POST is fail-fast — otherwise a flaky 429
makes the daemon hammer an already-throttled token. ``_apply_action_
response_to_db`` therefore takes its own ``fail_fast`` (default False) and
the daemon never overrides it.

Pytest-first: confirm/cancel/identifier/label are action endpoints with
Uzum penalty/ban risk, so these mocked tests (no real Uzum, no sleeping,
no Postgres) exist BEFORE any prod test. They assert the wiring and the
patient-daemon invariant — not real timing.
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from core import uzum_openapi
from core import fbs_data


def _make_session_cm_mock():
    db = MagicMock(name="db")
    session = MagicMock(name="SessionLocal")
    session.return_value.__enter__.return_value = db
    session.return_value.__exit__.return_value = False
    return session, db


# ── openapi action/DBS/detail funcs forward fail_fast to the chokepoint ─
def _capture_chokepoint_fail_fast(run):
    """Run ``run()`` with the chokepoint mocked to return a 429 (so the
    func raises right after recording) + pace_uzum_call stubbed. Returns
    the ``fail_fast`` value the func forwarded into the chokepoint."""
    seen = {}

    def fake_req(url, token, **kw):
        seen["fail_fast"] = kw.get("fail_fast")
        # 429 → the func raises (RuntimeError or UzumAPIError) AFTER
        # forwarding; we only care about the recorded value.
        return ({"errors": [{"code": "x"}]}, 429, "", "raw")

    with patch.object(uzum_openapi, "_fbs_orders_request_with_auth", side_effect=fake_req), \
         patch.object(uzum_openapi, "pace_uzum_call"):
        try:
            run()
        except Exception:
            pass
    return seen.get("fail_fast")


def _openapi_call(name, **extra):
    return {
        "confirm_fbs_order": lambda: uzum_openapi.confirm_fbs_order("tok", 42, **extra),
        "cancel_fbs_order": lambda: uzum_openapi.cancel_fbs_order("tok", 42, reason="OUT_OF_STOCK", **extra),
        "attach_fbs_identifiers": lambda: uzum_openapi.attach_fbs_identifiers(
            "tok", 42, items=[{"orderItemId": 1, "values": ["a"]}], **extra),
        "download_fbs_label": lambda: uzum_openapi.download_fbs_label("tok", 42, **extra),
        "fetch_fbs_order_detail": lambda: uzum_openapi.fetch_fbs_order_detail("tok", 42, **extra),
        "fetch_fbs_dropoff_points": lambda: uzum_openapi.fetch_fbs_dropoff_points("tok", [42], **extra),
        "fetch_fbs_return_reasons": lambda: uzum_openapi.fetch_fbs_return_reasons("tok", **extra),
        "mark_dbs_delivering": lambda: uzum_openapi.mark_dbs_delivering("tok", 42, **extra),
        "mark_dbs_completed": lambda: uzum_openapi.mark_dbs_completed("tok", 42, **extra),
        "refund_dbs_order": lambda: uzum_openapi.refund_dbs_order("tok", 42, **extra),
    }[name]()


_OPENAPI_ACTIONS = [
    "confirm_fbs_order", "cancel_fbs_order", "attach_fbs_identifiers",
    "download_fbs_label", "fetch_fbs_order_detail", "fetch_fbs_dropoff_points",
    "fetch_fbs_return_reasons", "mark_dbs_delivering", "mark_dbs_completed",
    "refund_dbs_order",
]


class TestOpenapiActionForwarding:
    @pytest.mark.parametrize("name", _OPENAPI_ACTIONS)
    def test_forwards_fail_fast_true(self, name):
        assert _capture_chokepoint_fail_fast(lambda: _openapi_call(name, fail_fast=True)) is True

    @pytest.mark.parametrize("name", _OPENAPI_ACTIONS)
    def test_defaults_to_patient(self, name):
        assert _capture_chokepoint_fail_fast(lambda: _openapi_call(name)) is False


# ── fbs_data wrapper layer forwards fail_fast ─────────────────────────
class TestWrapperForwarding:
    def test_confirm_order_forwards_to_both_calls(self):
        seen = {}
        with patch.object(fbs_data, "confirm_fbs_order",
                          side_effect=lambda t, o, **kw: (seen.__setitem__("confirm", kw.get("fail_fast")), ({}, "u"))[1]), \
             patch.object(fbs_data, "_apply_action_response_to_db",
                          side_effect=lambda *a, **kw: seen.__setitem__("apply", kw.get("fail_fast"))), \
             patch.object(fbs_data, "invalidate_fbs_cache"):
            fbs_data.confirm_order("tok", 42, "shop", fail_fast=True)
        assert seen["confirm"] is True
        assert seen["apply"] is True

    def test_attach_identifiers_forwards_to_both_calls(self):
        seen = {}
        with patch.object(fbs_data, "attach_fbs_identifiers",
                          side_effect=lambda t, o, **kw: (seen.__setitem__("attach", kw.get("fail_fast")), ({}, "u"))[1]), \
             patch.object(fbs_data, "_apply_action_response_to_db",
                          side_effect=lambda *a, **kw: seen.__setitem__("apply", kw.get("fail_fast"))), \
             patch.object(fbs_data, "invalidate_fbs_cache"):
            fbs_data.attach_identifiers("tok", 42, "shop", items=[{"orderItemId": 1, "values": ["a"]}], fail_fast=True)
        assert seen["attach"] is True
        assert seen["apply"] is True

    def test_get_label_pdfs_forwards_through_cache(self):
        seen = {}
        with patch.object(fbs_data, "download_fbs_label",
                          side_effect=lambda t, o, **kw: (seen.__setitem__("label", kw.get("fail_fast")), ([], "u"))[1]):
            fbs_data.get_label_pdfs("tok", 42, size="LARGE", fail_fast=True)
        assert seen["label"] is True

    def test_get_fbs_order_detail_db_miss_forwards(self):
        seen = {}
        session, db = _make_session_cm_mock()
        db.execute.return_value.scalar_one_or_none.return_value = None  # DB miss → Uzum fallback
        with patch.object(fbs_data, "SessionLocal", session), \
             patch.object(fbs_data, "fetch_fbs_order_detail",
                          side_effect=lambda t, o, **kw: (seen.__setitem__("detail", kw.get("fail_fast")), ({}, "u"))[1]):
            fbs_data.get_fbs_order_detail("tok", 42, fail_fast=True)
        assert seen["detail"] is True

    def test_apply_action_forwards_fail_fast(self):
        seen = {}
        with patch.object(fbs_data, "fetch_fbs_order_detail",
                          side_effect=lambda t, o, **kw: (seen.__setitem__("detail", kw.get("fail_fast")), ({}, "u"))[1]):
            # response has no "id" → goes to the token/order_id refetch branch.
            fbs_data._apply_action_response_to_db("shop", {}, token="tok", order_id=42, fail_fast=True)
        assert seen["detail"] is True

    def test_apply_action_defaults_patient(self):
        seen = {}
        with patch.object(fbs_data, "fetch_fbs_order_detail",
                          side_effect=lambda t, o, **kw: (seen.__setitem__("detail", kw.get("fail_fast")), ({}, "u"))[1]):
            fbs_data._apply_action_response_to_db("shop", {}, token="tok", order_id=42)
        assert seen["detail"] is False

    def test_get_return_reasons_forwards(self):
        seen = {}
        with patch.object(fbs_data, "fetch_fbs_return_reasons",
                          side_effect=lambda t, **kw: (seen.__setitem__("rr", kw.get("fail_fast")), ([], "u"))[1]), \
             patch.object(fbs_data, "swr_get", side_effect=lambda key, *, soft_ttl, hard_ttl, loader: loader()):
            fbs_data.get_return_reasons("tok", fail_fast=True)
        assert seen["rr"] is True

    @pytest.mark.parametrize("wrapper,openapi_name", [
        ("dbs_delivering", "mark_dbs_delivering"),
        ("dbs_refund", "refund_dbs_order"),
    ])
    def test_dbs_wrappers_forward(self, wrapper, openapi_name):
        seen = {}
        with patch.object(fbs_data, openapi_name,
                          side_effect=lambda t, o, **kw: (seen.__setitem__("ff", kw.get("fail_fast")), ({}, "u"))[1]), \
             patch.object(fbs_data, "_apply_action_response_to_db"), \
             patch.object(fbs_data, "invalidate_fbs_cache"):
            getattr(fbs_data, wrapper)("tok", 42, "shop", fail_fast=True)
        assert seen["ff"] is True


# ── THE invariant: cancel POST is fast, but the Phase-4 daemon is patient ─
class TestCancelDaemonStaysPatient:
    def test_sync_cancel_fast_but_daemon_refetch_patient(self):
        seen = {}
        bg_done = threading.Event()

        def fake_cancel(token, order_id, **kw):
            seen["sync_fail_fast"] = kw.get("fail_fast")
            return ({}, "url")

        def fake_apply(*a, **kw):
            seen["daemon_fail_fast"] = kw.get("fail_fast")
            bg_done.set()
            return True

        session, _db = _make_session_cm_mock()
        with patch.object(fbs_data, "cancel_fbs_order", side_effect=fake_cancel), \
             patch.object(fbs_data, "SessionLocal", session), \
             patch.object(fbs_data, "invalidate_fbs_cache"), \
             patch.object(fbs_data, "_apply_action_response_to_db", side_effect=fake_apply):
            fbs_data.cancel_order("tok", 42, "shop", reason="OUT_OF_STOCK", fail_fast=True)
            assert bg_done.wait(timeout=2.0), "Phase-4 daemon refetch never ran"

        assert seen["sync_fail_fast"] is True, \
            "the user-facing cancel POST must be FAIL-FAST"
        # The daemon passes fail_fast=False EXPLICITLY (it runs after the
        # response is sent, so it must never fast-fail on a throttled token).
        assert seen["daemon_fail_fast"] is False, \
            "the Phase-4 background daemon refetch MUST stay PATIENT (fail_fast=False)"
