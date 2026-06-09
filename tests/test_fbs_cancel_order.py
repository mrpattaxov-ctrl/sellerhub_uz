"""Tests for the cancel_order fast-path optimization (2026-05-26).

Verifies the post-rewrite behavior:

* The Uzum POST /cancel HTTP call is invoked with the right args.
* A synchronous DB UPDATE writes status=CANCELED + cancelled_date +
  cancel_reason — no second Uzum HTTP call on the critical path.
* The full-state refetch is dispatched to a daemon thread, so the API
  caller does NOT wait for it.
* The function returns the Uzum body + used_url tuple.

All external dependencies are mocked — no Postgres, no Uzum HTTP, no
real threads waiting on real timeouts. The point is to be able to
re-verify the cancel flow without spending a real cancellation against
the seller's Uzum account (which has rate / penalty implications).
"""
from __future__ import annotations

import threading
import time
from unittest.mock import patch, MagicMock

import pytest

from core import fbs_data


def _make_context_manager_mock():
    """Build a MagicMock that behaves like a context manager (``with`` block)
    so ``with SessionLocal() as db:`` works in production code under test.
    Returns ``(session_mock, db_mock)`` — patch ``SessionLocal`` to the
    session_mock and inspect ``db_mock.execute`` for assertions."""
    db = MagicMock(name="db_session")
    session = MagicMock(name="SessionLocal_callable")
    session.return_value.__enter__.return_value = db
    session.return_value.__exit__.return_value = False
    return session, db


class TestCancelOrderFastPath:
    """End-to-end behavior of ``core.fbs_data.cancel_order`` after the
    fast-path rewrite. Each test patches just enough of the surrounding
    machinery to exercise one observable property."""

    def test_returns_uzum_body_and_url(self):
        """Function returns the (body, used_url) tuple from Uzum cancel."""
        session, _db = _make_context_manager_mock()
        with patch.object(fbs_data, "cancel_fbs_order",
                          return_value=({"echoed": True}, "https://uzum/cancel")), \
             patch.object(fbs_data, "SessionLocal", session), \
             patch.object(fbs_data, "invalidate_fbs_cache"), \
             patch.object(fbs_data, "_apply_action_response_to_db"):

            body, url = fbs_data.cancel_order(
                token="t-abc",
                order_id=108130896,
                shop_uzum_id="95673",
                reason="OUT_OF_STOCK",
            )

            assert body == {"echoed": True}
            assert url == "https://uzum/cancel"

    def test_calls_uzum_cancel_with_args(self):
        """The synchronous Uzum POST is invoked with the exact args
        the caller passed (token positional; reason/comment kwargs)."""
        session, _db = _make_context_manager_mock()
        with patch.object(fbs_data, "cancel_fbs_order",
                          return_value=({}, "url")) as mock_cancel, \
             patch.object(fbs_data, "SessionLocal", session), \
             patch.object(fbs_data, "invalidate_fbs_cache"), \
             patch.object(fbs_data, "_apply_action_response_to_db"):

            fbs_data.cancel_order(
                token="t-123",
                order_id=42,
                shop_uzum_id="95673",
                reason="OUT_OF_STOCK",
                comment="qoldiq yetmaydi",
            )

            # fail_fast defaults to False when cancel_order is called bare
            # (the route passes fail_fast=True; the patient default is what
            # a direct/legacy caller gets). Bosqich A.9 / #5.
            mock_cancel.assert_called_once_with(
                "t-123", 42,
                reason="OUT_OF_STOCK",
                comment="qoldiq yetmaydi",
                fail_fast=False,
            )

    def test_manual_db_update_sets_canceled_state(self):
        """The synchronous DB UPDATE writes status=CANCELED + cancel_reason
        with the code we just submitted, so the seller's reload-after-cancel
        sees the new state without waiting for the background refetch.

        We don't try to introspect the SQLAlchemy update statement (its
        ``values`` are stored in private internals that differ across
        versions); we just assert ``execute`` was called once and commit
        followed."""
        session, db = _make_context_manager_mock()
        with patch.object(fbs_data, "cancel_fbs_order",
                          return_value=({}, "url")), \
             patch.object(fbs_data, "SessionLocal", session), \
             patch.object(fbs_data, "invalidate_fbs_cache"), \
             patch.object(fbs_data, "_apply_action_response_to_db"):

            fbs_data.cancel_order(
                token="t",
                order_id=42,
                shop_uzum_id="95673",
                reason="OUT_OF_STOCK",
            )

            # Exactly one execute (the UPDATE), exactly one commit.
            assert db.execute.call_count == 1, \
                f"expected 1 db.execute call, got {db.execute.call_count}"
            assert db.commit.call_count == 1, \
                f"expected 1 db.commit call, got {db.commit.call_count}"
            # The update statement object — confirm it's an Update by
            # checking the SQL it compiles to. (Cheap structural check.)
            stmt = db.execute.call_args.args[0]
            compiled = str(stmt.compile(compile_kwargs={"literal_binds": False}))
            assert "UPDATE fbs_orders" in compiled
            assert "status" in compiled
            assert "cancelled_date" in compiled
            assert "cancel_reason" in compiled

    def test_cache_is_invalidated(self):
        """After the manual DB update, the per-shop SWR cache is wiped
        so the next /fbs read picks up the new row instead of stale data."""
        session, _db = _make_context_manager_mock()
        with patch.object(fbs_data, "cancel_fbs_order",
                          return_value=({}, "url")), \
             patch.object(fbs_data, "SessionLocal", session), \
             patch.object(fbs_data, "invalidate_fbs_cache") as mock_inv, \
             patch.object(fbs_data, "_apply_action_response_to_db"):

            fbs_data.cancel_order(
                token="t", order_id=42, shop_uzum_id="95673",
                reason="OUT_OF_STOCK",
            )

            # Synchronous invalidate happens once on the main thread.
            # The background thread also calls it — that may or may not
            # have run by now, so we only check "at least once".
            assert mock_inv.call_count >= 1
            mock_inv.assert_any_call("95673")

    def test_returns_before_background_thread_completes(self):
        """The whole point of the rewrite: the API caller must NOT wait
        for the post-cancel refetch. We simulate a slow refetch (5s
        block) and assert ``cancel_order`` returns much faster than that.

        The 2.0s ceiling is generous on purpose — under heavy CI load
        (Windows Defender scanning, slow disk, parallel test workers)
        a real test box can spend ~500ms just on import + mock setup,
        and we'd rather tolerate that than have a flaky regression
        gate. The fast-path budget we actually care about is "much less
        than 5 sec", which a 2s ceiling already proves.
        """
        session, _db = _make_context_manager_mock()
        bg_started = threading.Event()
        bg_release = threading.Event()

        def slow_bg(*args, **kwargs):
            bg_started.set()
            # Block as if Uzum was slow to respond to GET /order/{id}.
            bg_release.wait(timeout=5.0)

        with patch.object(fbs_data, "cancel_fbs_order",
                          return_value=({}, "url")), \
             patch.object(fbs_data, "SessionLocal", session), \
             patch.object(fbs_data, "invalidate_fbs_cache"), \
             patch.object(fbs_data, "_apply_action_response_to_db",
                          side_effect=slow_bg):

            t0 = time.perf_counter()
            fbs_data.cancel_order(
                token="t", order_id=42, shop_uzum_id="95673",
                reason="OUT_OF_STOCK",
            )
            elapsed = time.perf_counter() - t0

            assert elapsed < 2.0, (
                f"cancel_order took {elapsed:.2f}s — background thread "
                f"must have blocked the main path (expected sub-2s)"
            )
            assert bg_started.wait(timeout=1.0), \
                "background _apply_action_response_to_db was never invoked"
            # Release the blocked bg thread so the test process can exit cleanly.
            bg_release.set()

    def test_background_thread_invokes_apply_response(self):
        """The background daemon calls ``_apply_action_response_to_db``
        with the same shop+order ids, so the canonical Uzum row eventually
        overwrites our local guess (including the localized reason
        label). The bg thread then runs a SECOND ``invalidate_fbs_cache``
        — so the next /fbs read picks up the canonical row, not the
        stale row our sync-path manual UPDATE left behind."""
        session, _db = _make_context_manager_mock()
        bg_done = threading.Event()
        captured: dict = {}

        def capture(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            bg_done.set()
            return True  # signal "upserted" so the bg log says OK

        with patch.object(fbs_data, "cancel_fbs_order",
                          return_value=({}, "url")), \
             patch.object(fbs_data, "SessionLocal", session), \
             patch.object(fbs_data, "invalidate_fbs_cache") as mock_inv, \
             patch.object(fbs_data, "_apply_action_response_to_db",
                          side_effect=capture):

            fbs_data.cancel_order(
                token="t-zzz", order_id=108130896, shop_uzum_id="95673",
                reason="OUT_OF_STOCK",
            )

            assert bg_done.wait(timeout=2.0), \
                "background refetch did not complete within 2s"
            # First positional arg = shop_uzum_id, second = response dict.
            assert captured["args"][0] == "95673"
            assert captured["args"][1] == {}
            # kwargs carry the token + order_id used for the refetch.
            assert captured["kwargs"]["token"] == "t-zzz"
            assert captured["kwargs"]["order_id"] == 108130896

            # The bg thread calls invalidate_fbs_cache AFTER _apply_*
            # returns — give it a moment to complete that second call.
            # Two calls total: one sync from the main path + one async
            # from the bg thread. Anything less means the bg thread
            # was killed or never reached the second invalidate.
            deadline = time.perf_counter() + 2.0
            while mock_inv.call_count < 2 and time.perf_counter() < deadline:
                time.sleep(0.02)
            assert mock_inv.call_count == 2, (
                f"expected 2 invalidate_fbs_cache calls (sync + bg), "
                f"got {mock_inv.call_count}"
            )
            # Both calls target the same shop.
            for call in mock_inv.call_args_list:
                assert call.args[0] == "95673"

    def test_db_update_failure_is_swallowed(self):
        """If the synchronous DB UPDATE raises (e.g. row doesn't exist
        yet), we LOG and continue — Uzum-side cancel already succeeded,
        and the background refetch will create / repair the row. The
        caller must still receive the (body, url) tuple, not an exception."""
        session, db = _make_context_manager_mock()
        db.execute.side_effect = RuntimeError("simulated DB outage")

        with patch.object(fbs_data, "cancel_fbs_order",
                          return_value=({"x": 1}, "url")), \
             patch.object(fbs_data, "SessionLocal", session), \
             patch.object(fbs_data, "invalidate_fbs_cache"), \
             patch.object(fbs_data, "_apply_action_response_to_db"):

            # No exception should escape.
            body, url = fbs_data.cancel_order(
                token="t", order_id=42, shop_uzum_id="95673",
                reason="OUT_OF_STOCK",
            )
            assert body == {"x": 1}
            assert url == "url"
