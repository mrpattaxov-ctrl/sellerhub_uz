"""SellerHub → Auto-slot HTTP client testlari (tarmoqsiz — `requests` mock).

Tekshiradi: AUTOSLOT_URL bayrog'i, so'rov URL/header/body, javob shakl-
moslashtirishi (snake_case → camelCase), 404 → None.
"""
from __future__ import annotations

import pytest

from postavki import autoslot_client as ac


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("AUTOSLOT_URL", "http://autoslot:8000")
    monkeypatch.setenv("AUTOSLOT_API_KEY", "k123")


def test_enabled_reflects_env(monkeypatch):
    monkeypatch.setenv("AUTOSLOT_URL", "")
    assert ac.enabled() is False
    monkeypatch.setenv("AUTOSLOT_URL", "http://x:8000")
    assert ac.enabled() is True


def test_post_plan_sends_correct_request(on, monkeypatch):
    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, json=json, headers=headers)
        return _Resp(201, {"id": 9, "action": "created", "status": "waiting"})

    monkeypatch.setattr(ac.requests, "post", fake_post)
    out = ac.post_plan({"shop_uzum_id": "51948", "invoice_id": 7})
    assert out["id"] == 9 and out["action"] == "created"
    assert seen["url"] == "http://autoslot:8000/v1/plans"
    assert seen["headers"]["X-Autoslot-Key"] == "k123"
    assert seen["json"]["invoice_id"] == 7


def test_list_for_shop_maps_shape(on, monkeypatch):
    raw = {"id": 5, "shop_uzum_id": "51948", "invoice_id": 7, "invoice_number": "N-1",
           "volume": 30, "max_date": "2026-07-20", "enabled_at": "2026-07-10T00:00:00",
           "status": "waiting", "booked_slot_ms": None, "error": None}
    monkeypatch.setattr(ac.requests, "get", lambda *a, **k: _Resp(200, {"plans": [raw]}))
    plans = ac.list_for_shop("51948", "waiting")
    assert len(plans) == 1
    p = plans[0]
    assert p["invoiceId"] == 7 and p["invoiceNumber"] == "N-1"
    assert p["size"] == 30 and p["maxDate"] == "2026-07-20"
    assert p["targetDay"] == "2026-07-20"   # autoslot'da target_day yo'q → maxDate
    assert p["status"] == "waiting"


def test_list_for_shops_adds_shopname(on, monkeypatch):
    raw = {"id": 1, "shop_uzum_id": "51948", "invoice_id": 7, "max_date": "2026-07-20",
           "status": "waiting"}
    monkeypatch.setattr(ac.requests, "get", lambda *a, **k: _Resp(200, {"plans": [raw]}))
    plans = ac.list_for_shops({"51948": "Mening do'konim"}, None)
    assert plans[0]["shopName"] == "Mening do'konim"
    assert plans[0]["shop"] == "51948"


def test_set_priority_404_returns_none(on, monkeypatch):
    monkeypatch.setattr(ac.requests, "post", lambda *a, **k: _Resp(404, {}))
    assert ac.set_priority(999, 5) is None


def test_set_priority_ok(on, monkeypatch):
    monkeypatch.setattr(ac.requests, "post", lambda *a, **k: _Resp(200, {"priority": 5}))
    assert ac.set_priority(3, 5) == 5


def test_cancel_404_returns_none(on, monkeypatch):
    monkeypatch.setattr(ac.requests, "post", lambda *a, **k: _Resp(404, {}))
    assert ac.cancel(999) is None


def test_push_token_noop_when_disabled(monkeypatch):
    monkeypatch.setenv("AUTOSLOT_URL", "")
    assert ac.push_token("Bearer X") is False


def test_push_token_ok(on, monkeypatch):
    monkeypatch.setattr(ac.requests, "post", lambda *a, **k: _Resp(200, {"status": "ok", "token_len": 8}))
    assert ac.push_token("Bearer X") is True
