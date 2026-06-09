"""Unit tests for the akt-cache live-fetch 429 retry (core.fbs_akt_cache).

Pure logic only — no DB, no network. Uzum's ``/print`` endpoint allows only
~4 quick calls before HTTP 429 (recovering in ~3-4s), so ``fetch_akt_live``
drives a SHORT 429-aware retry instead of the shared session's 60s backoff.
We monkeypatch the print call + sleep so that retry is asserted without any
real waiting or network.
"""
import pytest

from core import fbs_akt_cache
from core.uzum_openapi import UzumAPIError


def _err(status):
    return UzumAPIError(status, None, None, "", "http://x/print")


def test_success_first_try_no_retry(monkeypatch):
    calls = {"n": 0, "slept": 0}

    def fake_fetch(token, iid, *, fail_fast):
        calls["n"] += 1
        return (b"%PDF-1", "url")

    monkeypatch.setattr(fbs_akt_cache, "fetch_fbs_invoice_akt_pdf", fake_fetch)
    monkeypatch.setattr(fbs_akt_cache._time, "sleep",
                        lambda s: calls.__setitem__("slept", calls["slept"] + 1))

    assert fbs_akt_cache.fetch_akt_live("tok", 1) == b"%PDF-1"
    assert calls["n"] == 1
    assert calls["slept"] == 0


def test_retries_then_succeeds(monkeypatch):
    seq = [_err(429), None]  # first 429, then success
    calls = {"slept": 0}

    def fake_fetch(token, iid, *, fail_fast):
        item = seq.pop(0)
        if item is not None:
            raise item
        return (b"%PDF-OK", "url")

    monkeypatch.setattr(fbs_akt_cache, "fetch_fbs_invoice_akt_pdf", fake_fetch)
    monkeypatch.setattr(fbs_akt_cache._time, "sleep",
                        lambda s: calls.__setitem__("slept", calls["slept"] + 1))

    assert fbs_akt_cache.fetch_akt_live("tok", 1) == b"%PDF-OK"
    assert calls["slept"] == 1


def test_429_exhausts_attempts(monkeypatch):
    calls = {"n": 0, "slept": 0}

    def fake_fetch(token, iid, *, fail_fast):
        calls["n"] += 1
        raise _err(429)

    monkeypatch.setattr(fbs_akt_cache, "fetch_fbs_invoice_akt_pdf", fake_fetch)
    monkeypatch.setattr(fbs_akt_cache._time, "sleep",
                        lambda s: calls.__setitem__("slept", calls["slept"] + 1))

    with pytest.raises(UzumAPIError):
        fbs_akt_cache.fetch_akt_live("tok", 1, attempts=4, wait=0)
    assert calls["n"] == 4       # tried the full budget
    assert calls["slept"] == 3   # waited between tries, not after the last


def test_non_429_raises_immediately(monkeypatch):
    calls = {"n": 0}

    def fake_fetch(token, iid, *, fail_fast):
        calls["n"] += 1
        raise _err(500)

    monkeypatch.setattr(fbs_akt_cache, "fetch_fbs_invoice_akt_pdf", fake_fetch)
    monkeypatch.setattr(fbs_akt_cache._time, "sleep", lambda s: None)

    with pytest.raises(UzumAPIError):
        fbs_akt_cache.fetch_akt_live("tok", 1)
    assert calls["n"] == 1       # a non-429 error is not retried
