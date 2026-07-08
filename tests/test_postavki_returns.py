"""Mocked tests for the Postavki «Возвраты» (returns) flow.

Pytest-first: ``/postavki/api/return/create`` and ``…/cancel`` are ACTION
endpoints — they open / cancel a REAL return накладной at Uzum (penalty/quality
risk if the payload is wrong). These mocked tests (no real Uzum, no network, no
Postgres) pin the URL + JSON body shapes captured from the real portal HAR
(«Поставки20+»), and the route's filter/input guards. A drift here would
silently create wrong returns or leak cross-shop data.
"""
from __future__ import annotations

from unittest.mock import patch

from postavki import client
from postavki.routes import _clean_csv, _RETURN_STATUSES, _RETURN_TYPES


def _capture():
    """Patch client.http_json; record every (url, method, body)."""
    calls = []

    def fake(url, method="GET", body=None, headers=None, _get_admin_token=None):
        calls.append({"url": url, "method": method, "body": body})
        return fake.ret

    fake.ret = {}
    return calls, fake


# ── _clean_csv: the route's filter guard (no junk reaches Uzum) ──────


class TestCleanCsv:
    def test_keeps_only_allowed_statuses(self):
        assert _clean_csv("CREATED,bogus,CANCELED", _RETURN_STATUSES) == "CREATED,CANCELED"

    def test_uppercases_and_dedups(self):
        assert _clean_csv("created,created,sent", _RETURN_STATUSES) == "CREATED,SENT"

    def test_types_filter(self):
        assert _clean_csv("return,fbs,junk", _RETURN_TYPES) == "RETURN,FBS"

    def test_empty_and_all_junk(self):
        assert _clean_csv("", _RETURN_STATUSES) == ""
        assert _clean_csv("x,y,z", _RETURN_STATUSES) == ""
        assert _clean_csv(None, _RETURN_TYPES) == ""

    def test_order_preserved(self):
        assert _clean_csv("SENT,CREATED", _RETURN_STATUSES) == "SENT,CREATED"


# ── list_returns: URL + filter query (read-only) ─────────────────────


class TestListReturns:
    def test_plain_list_url(self):
        calls, fake = _capture()
        fake.ret = {"payload": [{"id": 1}]}
        with patch.object(client, "http_json", fake):
            out = client.list_returns("5983", page=1, size=20)
        assert calls[0]["method"] == "GET"
        assert calls[0]["url"].endswith("/shop/5983/return?page=1&size=20")
        assert out == [{"id": 1}]

    def test_filters_appended(self):
        calls, fake = _capture()
        fake.ret = {"payload": []}
        with patch.object(client, "http_json", fake):
            client.list_returns(
                "5983", statuses="CREATED,SENT", types="RETURN", number_filter="90580"
            )
        u = calls[0]["url"]
        assert "&statuses=CREATED,SENT" in u
        assert "&types=RETURN" in u
        assert "&returnNumberFilter=90580" in u

    def test_payload_unwrapped_else_empty(self):
        calls, fake = _capture()
        fake.ret = {"nope": 1}
        with patch.object(client, "http_json", fake):
            assert client.list_returns("5983") == []


# ── return_detail: GET /return/{id}, unwrap payload ──────────────────


class TestReturnDetail:
    def test_url_and_unwrap(self):
        calls, fake = _capture()
        fake.ret = {"payload": {"id": 185363, "returnItems": [{"skuId": 10264228, "amount": 1}]}}
        with patch.object(client, "http_json", fake):
            d = client.return_detail("5983", 185363)
        assert calls[0]["method"] == "GET"
        assert calls[0]["url"].endswith("/shop/5983/return/185363")
        assert d["id"] == 185363 and d["returnItems"][0]["skuId"] == 10264228


# ── return_sku_source: GET /product/stock-sku, Spring-Page or list ───


class TestReturnSkuSource:
    def test_url_page_size(self):
        calls, fake = _capture()
        fake.ret = {"quantitySku": 0, "skuList": []}
        with patch.object(client, "http_json", fake):
            client.return_sku_source("5983", page=2, size=50)
        assert calls[0]["method"] == "GET"
        assert calls[0]["url"].endswith("/shop/5983/product/stock-sku?page=2&size=50")

    def test_filter_query_appended(self):
        # HAR «Поставки21+»: server-side search via &filter=<q>.
        calls, fake = _capture()
        fake.ret = {"quantitySku": 0, "skuList": []}
        with patch.object(client, "http_json", fake):
            client.return_sku_source("5983", filter_q="кольцо")
        assert "&filter=" in calls[0]["url"]

    def test_no_filter_when_empty(self):
        calls, fake = _capture()
        fake.ret = {"quantitySku": 0, "skuList": []}
        with patch.object(client, "http_json", fake):
            client.return_sku_source("5983", filter_q="")
        assert "filter=" not in calls[0]["url"]

    def test_top_level_skulist(self):
        # Real portal shape (live probe 2026-06-24): {quantitySku, skuList} at top.
        calls, fake = _capture()
        fake.ret = {"quantitySku": 761, "skuList": [{"skuId": 10540972}, {"skuId": 2}]}
        with patch.object(client, "http_json", fake):
            res = client.return_sku_source("5983")
        assert res["items"] == [{"skuId": 10540972}, {"skuId": 2}]
        assert res["total"] == 761

    def test_spring_page_content(self):
        calls, fake = _capture()
        fake.ret = {"payload": {"content": [{"skuId": 1}, {"skuId": 2}], "totalElements": 7}}
        with patch.object(client, "http_json", fake):
            res = client.return_sku_source("5983")
        assert res["items"] == [{"skuId": 1}, {"skuId": 2}]
        assert res["total"] == 7

    def test_flat_list_payload(self):
        calls, fake = _capture()
        fake.ret = {"payload": [{"skuId": 9}]}
        with patch.object(client, "http_json", fake):
            res = client.return_sku_source("5983")
        assert res["items"] == [{"skuId": 9}] and res["total"] == 1

    def test_empty_payload(self):
        calls, fake = _capture()
        fake.ret = {"payload": {}}
        with patch.object(client, "http_json", fake):
            res = client.return_sku_source("5983")
        assert res == {"items": [], "total": 0}


# ── create_return: POST /return, body echoes full sku DTO + amount ───


class TestCreateReturn:
    def test_exact_body_and_unwrap(self):
        calls, fake = _capture()
        fake.ret = {"payload": {"id": 185363, "status": "CREATED"}}
        # Full sku DTO (as stock-sku returns) + the added `amount`.
        items = [
            {"skuId": 10264233, "skuTitle": "X", "quantityActive": 29, "amount": 2},
            {"skuId": 10264226, "skuTitle": "Y", "quantityActive": 21, "amount": 1},
        ]
        with patch.object(client, "http_json", fake):
            res = client.create_return("5983", items)
        c = calls[0]
        assert c["method"] == "POST"
        assert c["url"].endswith("/shop/5983/return")
        # HAR: body is EXACTLY {"returnItems": [...]} — full objects passed through.
        assert c["body"] == {"returnItems": items}
        assert res["id"] == 185363 and res["status"] == "CREATED"


# ── cancel_return: POST /return/{id}/cancel, empty body ──────────────


class TestCancelReturn:
    def test_url_empty_body_unwrap(self):
        calls, fake = _capture()
        fake.ret = {"payload": {"id": 185363, "status": "CANCELED"}}
        with patch.object(client, "http_json", fake):
            res = client.cancel_return("5983", 185363)
        c = calls[0]
        assert c["method"] == "POST"
        assert c["url"].endswith("/shop/5983/return/185363/cancel")
        assert c["body"] == {}
        assert res["status"] == "CANCELED"
