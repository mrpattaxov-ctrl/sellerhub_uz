"""Tests for the async bulk-confirm flow (Bosqich 2).

The seller can select many CREATED orders and confirm them in one click;
instead of holding the request open ~60s, the route returns a ``job_id``
immediately and a daemon thread confirms them paced through the 2/s token
bucket, writing progress to Redis for the front-end to poll.

These tests pin the ban-relevant guarantees WITHOUT touching a real Uzum
account (confirm is a real, rate-limited, penalty-bearing action):

  * ``_partition_confirmable`` — only CREATED orders are ever handed to the
    confirm loop; non-CREATED are skipped with NO Uzum call.
  * ``_run_bulk_confirm_job`` — calls ``confirm_order`` once per order with
    the right args, records each outcome, and a per-order error NEVER aborts
    the batch (the rest still confirm).
  * ``core.fbs_bulk_jobs`` — the Redis progress tracker counts ok/failed
    correctly and degrades gracefully when Redis is down.

Everything is mocked — no Postgres, no Uzum HTTP, no real threads, no Redis
server. We call the extracted module-level worker directly instead of
spawning the daemon, so the assertions are deterministic.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from core import fbs_bulk_jobs as jobs
from core.uzum_openapi import UzumAPIError
from fbs import routes


# ── Fake Redis: a tiny dict-backed stand-in for the progress store ──────
class _DictRedis:
    """Implements just the get/set(ex=) surface ``fbs_bulk_jobs`` uses."""

    def __init__(self):
        self.store: dict[str, str] = {}

    def set(self, key, value, ex=None):
        self.store[key] = value

    def get(self, key):
        return self.store.get(key)


class _BoomRedis:
    """Every call raises — simulates Redis being unreachable."""

    def set(self, *a, **k):
        raise RuntimeError("redis down")

    def get(self, *a, **k):
        raise RuntimeError("redis down")


# ════════════════════════════════════════════════════════════════════
class TestPartitionConfirmable:
    """Only CREATED orders reach the confirm loop — the ban-relevant gate."""

    def test_only_created_is_confirmable(self):
        rows = [
            {"order_id": 1, "shop_uzum_id": "10", "status": "CREATED"},
            {"order_id": 2, "shop_uzum_id": "20", "status": "PACKING"},
            {"order_id": 3, "shop_uzum_id": "30", "status": "CREATED"},
        ]
        confirmable, skipped = routes._partition_confirmable(rows)
        assert confirmable == [
            {"order_id": 1, "shop_uzum_id": "10"},
            {"order_id": 3, "shop_uzum_id": "30"},
        ]
        assert len(skipped) == 1
        assert skipped[0]["order_id"] == 2
        assert skipped[0]["status"] == "PACKING"
        assert skipped[0]["error"]  # carries a user-facing reason

    def test_case_insensitive_status(self):
        rows = [{"order_id": 1, "shop_uzum_id": "10", "status": "created"}]
        confirmable, skipped = routes._partition_confirmable(rows)
        assert len(confirmable) == 1 and not skipped

    def test_all_skipped_when_none_created(self):
        rows = [
            {"order_id": 1, "shop_uzum_id": "10", "status": "PACKING"},
            {"order_id": 2, "shop_uzum_id": "20", "status": "CANCELED"},
        ]
        confirmable, skipped = routes._partition_confirmable(rows)
        assert confirmable == []
        assert len(skipped) == 2


# ════════════════════════════════════════════════════════════════════
class TestRunBulkConfirmJob:
    """The daemon worker confirms paced + records every outcome."""

    def _patches(self, recorded, finished, *, confirm_side=None, confirm_return=None):
        """Patch the worker's collaborators; capture record/finish calls."""
        kw = {}
        if confirm_side is not None:
            kw["side_effect"] = confirm_side
        else:
            kw["return_value"] = confirm_return or ({"status": "PACKING"}, "url")
        return (
            patch.object(routes, "confirm_order", **kw),
            patch.object(routes, "record_result",
                         side_effect=lambda jid, oid, **k: recorded.append((oid, k))),
            patch.object(routes, "finish_job",
                         side_effect=lambda jid: finished.append(jid)),
            patch.object(routes, "_warm_labels_async"),
        )

    def test_all_success(self):
        confirmable = [
            {"order_id": 1, "shop_uzum_id": "10"},
            {"order_id": 2, "shop_uzum_id": "20"},
        ]
        recorded, finished = [], []
        p_conf, p_rec, p_fin, p_warm = self._patches(recorded, finished)
        with p_conf as m_conf, p_rec, p_fin, p_warm:
            routes._run_bulk_confirm_job("job1", "tok", confirmable, "uz", 7)

        # confirm_order called exactly once per order, with the right args.
        assert m_conf.call_count == 2
        m_conf.assert_any_call("tok", 1, "10", fail_fast=True)
        m_conf.assert_any_call("tok", 2, "20", fail_fast=True)
        # both recorded as success, job finished once.
        assert recorded == [(1, {"ok": True}), (2, {"ok": True})]
        assert finished == ["job1"]

    def test_uzum_error_recorded_and_batch_continues(self):
        """A per-order UzumAPIError is recorded (localized) — NOT raised — and
        the remaining orders still get confirmed."""
        confirmable = [
            {"order_id": 1, "shop_uzum_id": "10"},
            {"order_id": 2, "shop_uzum_id": "20"},
        ]
        err = UzumAPIError(409, "seller-order-03", None, "body", "url")

        def conf(token, oid, shop, *, fail_fast):
            if oid == 1:
                raise err
            return ({"status": "PACKING"}, "url")

        recorded, finished = [], []
        p_conf, p_rec, p_fin, p_warm = self._patches(
            recorded, finished, confirm_side=conf)
        with p_conf, p_rec, p_fin, p_warm, \
             patch.object(routes, "_format_uzum_payload_detail", return_value=None):
            routes._run_bulk_confirm_job("j", "t", confirmable, "uz", 7)

        # order 1 → failure with the localized message for seller-order-03.
        assert recorded[0][0] == 1
        assert recorded[0][1]["ok"] is False
        assert recorded[0][1]["code"] == "seller-order-03"
        assert "muddat" in recorded[0][1]["error"].lower()  # "...muddati o'tib ketgan"
        # order 2 still confirmed despite order 1 failing.
        assert recorded[1] == (2, {"ok": True})
        assert finished == ["j"]

    def test_generic_exception_recorded(self):
        confirmable = [{"order_id": 1, "shop_uzum_id": "10"}]
        recorded, finished = [], []
        p_conf, p_rec, p_fin, p_warm = self._patches(
            recorded, finished, confirm_side=RuntimeError("boom"))
        with p_conf, p_rec, p_fin, p_warm:
            routes._run_bulk_confirm_job("j", "t", confirmable, "uz", 7)

        assert recorded[0][0] == 1
        assert recorded[0][1]["ok"] is False
        assert "boom" in recorded[0][1]["error"]
        assert finished == ["j"]

    def test_label_warm_failure_never_breaks_job(self):
        """A crash in the post-confirm label warm is swallowed."""
        confirmable = [{"order_id": 1, "shop_uzum_id": "10"}]
        recorded, finished = [], []
        with patch.object(routes, "confirm_order", return_value=({}, "url")), \
             patch.object(routes, "record_result",
                          side_effect=lambda jid, oid, **k: recorded.append((oid, k))), \
             patch.object(routes, "finish_job",
                          side_effect=lambda jid: finished.append(jid)), \
             patch.object(routes, "_warm_labels_async",
                          side_effect=RuntimeError("warm boom")):
            routes._run_bulk_confirm_job("j", "t", confirmable, "uz", 7)
        # job still recorded + finished cleanly.
        assert recorded == [(1, {"ok": True})]
        assert finished == ["j"]


# ════════════════════════════════════════════════════════════════════
class TestConfirm429Retry:
    """The async worker patiently retries a transient 429 (background only),
    but NEVER retries other errors (penalty risk). Fixes the prod 429 seen
    2026-06-17 where a confirm landed on Uzum's momentarily-drained bucket."""

    def _err(self, status, code=None):
        return UzumAPIError(status, code, None, "body", "url")

    def test_success_first_try_no_retry(self):
        with patch.object(routes, "confirm_order",
                          return_value=({"status": "PACKING"}, "url")) as m, \
             patch.object(routes.time, "sleep") as m_sleep:
            out = routes._confirm_with_429_retry("tok", 1, "10")
        assert out == ({"status": "PACKING"}, "url")
        assert m.call_count == 1
        m_sleep.assert_not_called()

    def test_429_then_success(self):
        calls = {"n": 0}

        def conf(token, oid, shop, *, fail_fast):
            calls["n"] += 1
            if calls["n"] == 1:
                raise self._err(429)
            return ({"status": "PACKING"}, "url")

        with patch.object(routes, "confirm_order", side_effect=conf), \
             patch.object(routes.time, "sleep") as m_sleep:
            out = routes._confirm_with_429_retry("tok", 1, "10")
        assert out == ({"status": "PACKING"}, "url")
        assert calls["n"] == 2            # retried once
        assert m_sleep.call_count == 1    # waited before the retry

    def test_429_exhausts_then_raises(self):
        with patch.object(routes, "confirm_order", side_effect=self._err(429)), \
             patch.object(routes.time, "sleep") as m_sleep:
            with pytest.raises(UzumAPIError) as ei:
                routes._confirm_with_429_retry("tok", 1, "10")
        assert ei.value.http_status == 429
        # Slept between attempts but not after the last one.
        assert m_sleep.call_count == routes._BULK_CONFIRM_429_RETRIES - 1

    def test_non_429_raises_immediately(self):
        """A non-rate error (e.g. deadline passed) must NOT be retried."""
        with patch.object(routes, "confirm_order",
                          side_effect=self._err(409, "seller-order-03")) as m, \
             patch.object(routes.time, "sleep") as m_sleep:
            with pytest.raises(UzumAPIError) as ei:
                routes._confirm_with_429_retry("tok", 1, "10")
        assert ei.value.code == "seller-order-03"
        assert m.call_count == 1          # no retry
        m_sleep.assert_not_called()

    def test_worker_recovers_via_retry(self):
        """End-to-end: a 429 inside the batch is retried and recorded as OK."""
        confirmable = [{"order_id": 1, "shop_uzum_id": "10"}]
        calls = {"n": 0}

        def conf(token, oid, shop, *, fail_fast):
            calls["n"] += 1
            if calls["n"] == 1:
                raise self._err(429)
            return ({"status": "PACKING"}, "url")

        recorded, finished = [], []
        with patch.object(routes, "confirm_order", side_effect=conf), \
             patch.object(routes.time, "sleep"), \
             patch.object(routes, "record_result",
                          side_effect=lambda jid, oid, **k: recorded.append((oid, k))), \
             patch.object(routes, "finish_job",
                          side_effect=lambda jid: finished.append(jid)), \
             patch.object(routes, "_warm_labels_async"):
            routes._run_bulk_confirm_job("j", "t", confirmable, "uz", 7)
        # The 429 was retried away → recorded as success, not failure.
        assert recorded == [(1, {"ok": True})]
        assert finished == ["j"]


# ════════════════════════════════════════════════════════════════════
class TestBulkJobsTracker:
    """The Redis-backed progress store counts ok/failed and self-heals."""

    def test_new_job_initial_state(self):
        fake = _DictRedis()
        with patch.object(jobs, "redis_client", fake):
            jid = jobs.new_job(7, total=3)
            st = jobs.get_job(jid)
        assert st["user_id"] == 7
        assert st["total"] == 3
        assert st["done"] == 0 and st["ok"] == 0
        assert st["failed"] == []
        assert st["status"] == "running"

    def test_record_success_increments_done_and_ok(self):
        fake = _DictRedis()
        with patch.object(jobs, "redis_client", fake):
            jid = jobs.new_job(7, total=2)
            jobs.record_result(jid, 100, ok=True)
            jobs.record_result(jid, 101, ok=True)
            st = jobs.get_job(jid)
        assert st["done"] == 2 and st["ok"] == 2
        assert st["failed"] == []

    def test_record_failure_lists_reason(self):
        fake = _DictRedis()
        with patch.object(jobs, "redis_client", fake):
            jid = jobs.new_job(7, total=2)
            jobs.record_result(jid, 100, ok=True)
            jobs.record_result(jid, 101, ok=False,
                               error="Tasdiqlash muddati o'tgan", code="seller-order-03")
            st = jobs.get_job(jid)
        assert st["done"] == 2
        assert st["ok"] == 1
        assert len(st["failed"]) == 1
        assert st["failed"][0]["order_id"] == 101
        assert st["failed"][0]["code"] == "seller-order-03"
        assert "muddat" in st["failed"][0]["error"].lower()

    def test_finish_sets_done_status(self):
        fake = _DictRedis()
        with patch.object(jobs, "redis_client", fake):
            jid = jobs.new_job(7, total=1)
            jobs.finish_job(jid)
            st = jobs.get_job(jid)
        assert st["status"] == "done"

    def test_get_unknown_returns_none(self):
        fake = _DictRedis()
        with patch.object(jobs, "redis_client", fake):
            assert jobs.get_job("does-not-exist") is None

    def test_record_and_finish_on_missing_are_noops(self):
        fake = _DictRedis()
        with patch.object(jobs, "redis_client", fake):
            # Must not raise even though the job never existed / expired.
            jobs.record_result("nope", 1, ok=True)
            jobs.finish_job("nope")

    def test_redis_down_degrades_gracefully(self):
        """A dead Redis must never raise into the confirm flow."""
        with patch.object(jobs, "redis_client", _BoomRedis()):
            jid = jobs.new_job(7, total=1)   # _save swallows → no raise
            assert jobs.get_job(jid) is None  # get swallows → None
            jobs.record_result(jid, 1, ok=True)  # no raise
            jobs.finish_job(jid)                  # no raise
