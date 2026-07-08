"""Unit tests for the order label-cache (core.fbs_label_cache).

Pure logic only — no DB, no network. Two surfaces are pinned:

  * ``fetch_label_live`` — Uzum's per-order ``/labels/print`` 429s on a burst
    (recovering in ~3-4s), so it drives a SHORT 429-aware retry (same shape as
    ``fetch_akt_live``). It also collapses Uzum's multi-package PDF list into one
    document (single package → the lone PDF verbatim, no merge).

  * ``prefetch_labels_for_token`` — the background warm: it reads the active FBS
    order set from our own DB, fetches only the MISSING labels (capped per tick),
    and prunes labels that left the active set. We mock the ``SessionLocal``
    boundary + the cache helpers so the loop's policy is asserted DB-free.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from core import fbs_label_cache
from core.uzum_openapi import UzumAPIError


def _err(status):
    return UzumAPIError(status, None, None, "", "http://x/labels/print")


# ── fetch_label_live: 429 retry + multi-package merge ────────────────────

def test_success_first_try_single_pdf_no_merge(monkeypatch):
    calls = {"n": 0, "slept": 0}

    def fake_dl(token, oid, *, size="LARGE", fail_fast=False):
        calls["n"] += 1
        return ([b"%PDF-ONE"], "url")

    monkeypatch.setattr(fbs_label_cache, "download_fbs_label", fake_dl)
    monkeypatch.setattr(fbs_label_cache._time, "sleep",
                        lambda s: calls.__setitem__("slept", calls["slept"] + 1))

    # Single package → the lone PDF is returned verbatim (merge NOT invoked).
    assert fbs_label_cache.fetch_label_live("tok", 1) == b"%PDF-ONE"
    assert calls["n"] == 1
    assert calls["slept"] == 0


def test_multi_package_is_merged(monkeypatch):
    def fake_dl(token, oid, *, size="LARGE", fail_fast=False):
        return ([b"%PDF-A", b"%PDF-B"], "url")

    monkeypatch.setattr(fbs_label_cache, "download_fbs_label", fake_dl)
    # merge is imported lazily from core.fbs_data inside the function.
    monkeypatch.setattr("core.fbs_data.merge_label_pdfs", lambda lst: b"MERGED:" + b",".join(lst))

    assert fbs_label_cache.fetch_label_live("tok", 1) == b"MERGED:%PDF-A,%PDF-B"


def test_retries_then_succeeds(monkeypatch):
    seq = [_err(429), None]  # first 429, then success
    calls = {"slept": 0}

    def fake_dl(token, oid, *, size="LARGE", fail_fast=False):
        item = seq.pop(0)
        if item is not None:
            raise item
        return ([b"%PDF-OK"], "url")

    monkeypatch.setattr(fbs_label_cache, "download_fbs_label", fake_dl)
    monkeypatch.setattr(fbs_label_cache._time, "sleep",
                        lambda s: calls.__setitem__("slept", calls["slept"] + 1))

    assert fbs_label_cache.fetch_label_live("tok", 1) == b"%PDF-OK"
    assert calls["slept"] == 1


def test_429_exhausts_attempts(monkeypatch):
    calls = {"n": 0, "slept": 0}

    def fake_dl(token, oid, *, size="LARGE", fail_fast=False):
        calls["n"] += 1
        raise _err(429)

    monkeypatch.setattr(fbs_label_cache, "download_fbs_label", fake_dl)
    monkeypatch.setattr(fbs_label_cache._time, "sleep",
                        lambda s: calls.__setitem__("slept", calls["slept"] + 1))

    with pytest.raises(UzumAPIError):
        fbs_label_cache.fetch_label_live("tok", 1, attempts=4, wait=0)
    assert calls["n"] == 4       # tried the full budget
    assert calls["slept"] == 3   # waited between tries, not after the last


def test_non_429_raises_immediately(monkeypatch):
    calls = {"n": 0}

    def fake_dl(token, oid, *, size="LARGE", fail_fast=False):
        calls["n"] += 1
        raise _err(500)

    monkeypatch.setattr(fbs_label_cache, "download_fbs_label", fake_dl)
    monkeypatch.setattr(fbs_label_cache._time, "sleep", lambda s: None)

    with pytest.raises(UzumAPIError):
        fbs_label_cache.fetch_label_live("tok", 1)
    assert calls["n"] == 1       # a non-429 error is not retried


# ── prefetch_labels_for_token: warm + cap + skip-cached + prune + throttle ──

@pytest.fixture(autouse=True)
def _reset_warm_throttle():
    """Each test starts with a clean per-user throttle state."""
    fbs_label_cache._LAST_WARM_TS.clear()
    yield
    fbs_label_cache._LAST_WARM_TS.clear()


def _mock_active_orders(order_ids, cached=()):
    """``with SessionLocal() as db:`` whose LEFT-JOIN query returns one row per
    active order — ``cached_id`` mirrors the join: the order's own id when its
    label is already cached, NULL (None) when it's missing."""
    rows = [
        MagicMock(order_id=oid, cached_id=(oid if oid in cached else None))
        for oid in order_ids
    ]
    db = MagicMock(name="db")
    db.execute.return_value.all.return_value = rows
    session = MagicMock(name="SessionLocal")
    session.return_value.__enter__.return_value = db
    session.return_value.__exit__.return_value = False
    return session


def test_prefetch_fetches_missing_and_prunes(monkeypatch):
    monkeypatch.setattr(fbs_label_cache, "SessionLocal",
                        _mock_active_orders(["A", "B", "C"]))  # none cached
    fetched = []
    monkeypatch.setattr(fbs_label_cache, "fetch_label_live",
                        lambda tok, oid, **k: b"%PDF")
    monkeypatch.setattr(fbs_label_cache, "store_label",
                        lambda uid, oid, size, pdf: fetched.append(oid))
    pruned_with = {}

    def fake_prune(uid, ids):
        pruned_with["ids"] = list(ids)
        return 2

    monkeypatch.setattr(fbs_label_cache, "prune_labels_not_in", fake_prune)

    f, p = fbs_label_cache.prefetch_labels_for_token("tok", 7, ["shop1"])
    assert f == 3                                  # all three warmed
    assert fetched == ["A", "B", "C"]
    assert p == 2
    assert sorted(pruned_with["ids"]) == ["A", "B", "C"]  # prune keeps the active set


def test_prefetch_skips_already_cached(monkeypatch):
    # "A" already cached (join produced its id), "B" missing (NULL).
    monkeypatch.setattr(fbs_label_cache, "SessionLocal",
                        _mock_active_orders(["A", "B"], cached={"A"}))
    fetched = []
    monkeypatch.setattr(fbs_label_cache, "fetch_label_live", lambda tok, oid, **k: b"%PDF")
    monkeypatch.setattr(fbs_label_cache, "store_label",
                        lambda uid, oid, size, pdf: fetched.append(oid))
    monkeypatch.setattr(fbs_label_cache, "prune_labels_not_in", lambda uid, ids: 0)

    f, _ = fbs_label_cache.prefetch_labels_for_token("tok", 7, ["shop1"])
    assert f == 1            # only the missing "B" was fetched
    assert fetched == ["B"]


def test_prefetch_caps_per_tick(monkeypatch):
    monkeypatch.setattr(fbs_label_cache, "SessionLocal",
                        _mock_active_orders(["A", "B", "C", "D", "E"]))
    fetched = []
    monkeypatch.setattr(fbs_label_cache, "fetch_label_live", lambda tok, oid, **k: b"%PDF")
    monkeypatch.setattr(fbs_label_cache, "store_label",
                        lambda uid, oid, size, pdf: fetched.append(oid))
    monkeypatch.setattr(fbs_label_cache, "prune_labels_not_in", lambda uid, ids: 0)

    f, _ = fbs_label_cache.prefetch_labels_for_token("tok", 7, ["shop1"], max_labels=2)
    assert f == 2            # capped — warming never hogs the print bucket
    assert fetched == ["A", "B"]


def test_prefetch_no_shops_is_noop(monkeypatch):
    # No shop ids → return early, never touch the DB or Uzum.
    called = {"db": False}
    monkeypatch.setattr(fbs_label_cache, "SessionLocal",
                        lambda *a, **k: called.__setitem__("db", True))
    assert fbs_label_cache.prefetch_labels_for_token("tok", 7, []) == (0, 0)
    assert called["db"] is False


def test_prefetch_throttled_within_interval(monkeypatch):
    """A second run within the ~25-min window is a no-op (the on-confirm warm
    owns freshness; the sync net only needs to fire every ~3rd tick)."""
    monkeypatch.setattr(fbs_label_cache, "SessionLocal",
                        _mock_active_orders(["A"]))
    monkeypatch.setattr(fbs_label_cache, "fetch_label_live", lambda tok, oid, **k: b"%PDF")
    monkeypatch.setattr(fbs_label_cache, "store_label", lambda *a, **k: None)
    monkeypatch.setattr(fbs_label_cache, "prune_labels_not_in", lambda uid, ids: 1)

    assert fbs_label_cache.prefetch_labels_for_token("tok", 7, ["shop1"]) == (1, 1)
    # Immediately again → throttled: zero work (no DB query, no fetch, no prune).
    assert fbs_label_cache.prefetch_labels_for_token("tok", 7, ["shop1"]) == (0, 0)


def test_prefetch_force_bypasses_throttle(monkeypatch):
    monkeypatch.setattr(fbs_label_cache, "SessionLocal",
                        _mock_active_orders(["A"]))
    monkeypatch.setattr(fbs_label_cache, "fetch_label_live", lambda tok, oid, **k: b"%PDF")
    monkeypatch.setattr(fbs_label_cache, "store_label", lambda *a, **k: None)
    monkeypatch.setattr(fbs_label_cache, "prune_labels_not_in", lambda uid, ids: 0)

    assert fbs_label_cache.prefetch_labels_for_token("tok", 7, ["shop1"]) == (1, 0)
    assert fbs_label_cache.prefetch_labels_for_token("tok", 7, ["shop1"], force=True) == (1, 0)


def test_prefetch_throttle_is_per_user(monkeypatch):
    """User 7 being throttled must not block user 8's warm."""
    monkeypatch.setattr(fbs_label_cache, "SessionLocal",
                        _mock_active_orders(["A"]))
    monkeypatch.setattr(fbs_label_cache, "fetch_label_live", lambda tok, oid, **k: b"%PDF")
    monkeypatch.setattr(fbs_label_cache, "store_label", lambda *a, **k: None)
    monkeypatch.setattr(fbs_label_cache, "prune_labels_not_in", lambda uid, ids: 0)

    assert fbs_label_cache.prefetch_labels_for_token("tok", 7, ["shop1"]) == (1, 0)
    assert fbs_label_cache.prefetch_labels_for_token("tok2", 8, ["shop2"]) == (1, 0)
