"""Background yield gate — the worker gives way to the seller (2026-07-13).

Uzum's own headers put the budget at 2 req/s per token, and the shared bucket
spends exactly that. The sync worker's sweep therefore ran ON the ceiling (calls
~0.5s apart), so a seller's click landed as the third request in that second and
took a 429 — absorbed by a retry, but the press stretched to ~2.5s.

The worker has no deadline (its statuses run on 23-63 minute cadences), so it
yields: background calls pace themselves to 1/sec, leaving the other half of the
budget permanently free for whoever is waiting on a screen. Abdulaziz: "worker
1/s da qilsin, bizga tezlik muhim emas."

These tests pin the two halves of that contract:
  * a background drain paces every page,
  * an interactive drain paces NOTHING (it is what the headroom is for), and the
    two never queue behind each other (separate slot keys).
"""
from __future__ import annotations

from unittest.mock import patch

import core.fbs_locks as locks
import core.fbs_sync as fbs_sync
from core.fbs_locks import pace_background_call, _BG_MIN_UZUM_CALL_INTERVAL_SEC


ONE_SHORT_PAGE = {"payload": {"orders": [{"id": "1"}]}}


def _drain(*, bg):
    with patch.object(fbs_sync, "fetch_fbs_orders_page",
                      return_value=(ONE_SHORT_PAGE, "url")), \
         patch.object(fbs_sync, "extract_fbs_orders_list",
                      return_value=([{"id": "1"}], None)), \
         patch("core.fbs_locks.pace_background_call") as pace:
        fbs_sync.fetch_all_pages("tok", ["shopA"], status="CREATED", bg=bg)
    return pace


class TestBackgroundDrainYields:
    def test_worker_drain_paces_each_page(self):
        pace = _drain(bg=True)
        pace.assert_called_once_with("tok")

    def test_interactive_drain_does_not_pace(self):
        # The seller's press must never sit behind the worker's slot — the
        # headroom exists precisely so it doesn't.
        pace = _drain(bg=False)
        pace.assert_not_called()

    def test_default_is_interactive(self):
        # bg is opt-in: a caller that forgets it stays fast, it does not silently
        # become a background citizen.
        with patch.object(fbs_sync, "fetch_fbs_orders_page",
                          return_value=(ONE_SHORT_PAGE, "url")), \
             patch.object(fbs_sync, "extract_fbs_orders_list",
                          return_value=([{"id": "1"}], None)), \
             patch("core.fbs_locks.pace_background_call") as pace:
            fbs_sync.fetch_all_pages("tok", ["shopA"], status="CREATED")
        pace.assert_not_called()


class TestGateSeparation:
    def test_background_slots_are_keyed_apart_from_interactive(self):
        # Same token, different sequences: a worker call reserving its 1s slot
        # must not push an interactive call into a wait.
        with patch("core.fbs_locks.pace_uzum_call") as paced, \
             patch("core.fbs_locks._interactive_active", return_value=False):
            pace_background_call("tok")
        paced.assert_called_once_with(
            "bg:tok", min_interval=_BG_MIN_UZUM_CALL_INTERVAL_SEC,
        )

    def test_background_interval_is_one_second(self):
        # Half of Uzum's advertised 2/s — the other half is the seller's.
        assert _BG_MIN_UZUM_CALL_INTERVAL_SEC == 1.0


class TestInteractivePriority:
    """While a seller is on an FBS screen, the worker steps fully aside so the
    press gets Uzum's whole 2/s — no 429, no retry, no 2-second press."""

    def test_background_waits_while_a_screen_is_active(self):
        # Active for the first two polls, then the seller's burst ends.
        active = iter([True, True, False])
        slept = []
        with patch("core.fbs_locks._interactive_active",
                   side_effect=lambda _t: next(active)), \
             patch("core.fbs_locks.pace_uzum_call"):
            pace_background_call("tok", _sleep=slept.append)
        assert slept == [locks._BG_YIELD_POLL_SEC] * 2

    def test_background_does_not_wait_when_nobody_is_on_screen(self):
        slept = []
        with patch("core.fbs_locks._interactive_active", return_value=False), \
             patch("core.fbs_locks.pace_uzum_call"):
            pace_background_call("tok", _sleep=slept.append)
        assert slept == []

    def test_background_is_never_starved(self):
        # A seller who never stops clicking must not stall the sync forever:
        # past the cap the worker proceeds anyway.
        slept = []
        with patch("core.fbs_locks._interactive_active", return_value=True), \
             patch("core.fbs_locks.pace_uzum_call") as paced:
            pace_background_call("tok", _sleep=slept.append)
        assert sum(slept) == locks._BG_MAX_YIELD_SEC
        paced.assert_called_once()          # it still ran

    def test_redis_down_means_no_yield_not_an_error(self):
        # Best-effort: without the flag the worker keeps its 1/s pacing and the
        # request path is unaffected.
        with patch("core.redis_client.redis_client") as r:
            r.exists.side_effect = RuntimeError("redis down")
            assert locks._interactive_active("tok") is False
            r.setex.side_effect = RuntimeError("redis down")
            locks.mark_interactive_activity("tok")   # must not raise

    def test_activity_key_never_contains_the_raw_token(self):
        key = locks._active_key("super-secret-token")
        assert "super-secret-token" not in key
        assert key.startswith("fbs:interactive:")
