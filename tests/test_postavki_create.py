"""Mocked tests for the Postavki (FBO поставка) create flow.

Pytest-first: ``/postavki/api/create`` is an ACTION endpoint — it opens a
REAL накладной at Uzum's fulfilment centre (penalty/quality risk if the
payload is wrong). So these mocked tests (no real Uzum, no network, no
Postgres) exist BEFORE any prod test. They pin two things:

  1. ``_parse_sku_lines`` rejects junk and normalises good input — the
     route's only guard against shipping a malformed line to Uzum.
  2. The client builds EXACTLY the URL + JSON body shape captured from the
     real portal HAR (sku/stocks → time-slot → create). A drift here would
     silently create wrong/empty поставки.
"""
from __future__ import annotations

from unittest.mock import patch

from postavki import client
from postavki.routes import _build_qr_labels, _flatten_qr_lines, _parse_sku_lines


# ── _parse_sku_lines: the route's input guard ────────────────────────


class TestParseSkuLines:
    def test_good_input_normalised(self):
        lines, err = _parse_sku_lines(
            [{"skuId": "782119", "quantityToStock": "10", "purchasePrice": "3100"}]
        )
        assert err is None
        assert lines == [{"skuId": 782119, "quantityToStock": 10, "purchasePrice": 3100}]

    def test_missing_price_defaults_zero(self):
        lines, err = _parse_sku_lines([{"skuId": 1, "quantityToStock": 5}])
        assert err is None
        assert lines[0]["purchasePrice"] == 0

    def test_empty_rejected(self):
        assert _parse_sku_lines([])[0] is None
        assert _parse_sku_lines(None)[0] is None

    def test_zero_or_negative_qty_rejected(self):
        assert _parse_sku_lines([{"skuId": 1, "quantityToStock": 0}])[0] is None
        assert _parse_sku_lines([{"skuId": 1, "quantityToStock": -3}])[0] is None

    def test_non_numeric_rejected(self):
        assert _parse_sku_lines([{"skuId": "abc", "quantityToStock": 5}])[0] is None
        assert _parse_sku_lines([{"skuId": 1, "quantityToStock": "x"}])[0] is None

    def test_non_dict_row_rejected(self):
        assert _parse_sku_lines([42])[0] is None


# ── client: URL + body shape must match the real portal HAR ──────────


def _capture():
    """Patch client.http_json, return (calls, fake) where calls records
    every (url, method, body)."""
    calls = []

    def fake(url, method="GET", body=None, headers=None, _get_admin_token=None):
        calls.append({"url": url, "method": method, "body": body})
        return fake.ret

    fake.ret = {}
    return calls, fake


class TestResolveStocks:
    def test_url_and_body(self):
        calls, fake = _capture()
        fake.ret = {"payload": {"stocks": [{"id": 34, "poolSource": "FULLFILMENT"}]}}
        with patch.object(client, "http_json", fake):
            stocks = client.resolve_stocks("10945", [782119, 782120])
        c = calls[0]
        assert c["method"] == "POST"
        assert c["url"].endswith("/shop/10945/v2/invoice/sku/stocks")
        assert c["body"] == {"skuList": [{"skuId": 782119}, {"skuId": 782120}]}
        assert stocks[0]["id"] == 34


class TestGetTimeSlots:
    def test_body_shape_and_window(self):
        import time as _t

        calls, fake = _capture()
        fake.ret = {"payload": {"timeSlots": [{"timeFrom": 1, "timeTo": 2}]}}
        lines = [{"skuId": 782119, "quantityToStock": 10, "purchasePrice": 3100}]
        before = int(_t.time() * 1000)
        with patch.object(client, "http_json", fake):
            slots = client.get_time_slots("10945", lines, "FULLFILMENT")
        after = int(_t.time() * 1000)
        c = calls[0]
        assert c["method"] == "POST"
        assert c["url"].endswith("/shop/10945/v2/invoice/time-slot")
        b = c["body"]
        assert b["skuList"] == lines
        assert b["poolSource"] == "FULLFILMENT"
        # timeFrom MUST be in the future (Uzum rejects past/now with
        # validation-failed-001). Auto-filled = now + lead.
        assert b["timeFrom"] >= before + client._TIMEFROM_LEAD_MS
        assert b["timeFrom"] <= after + client._TIMEFROM_LEAD_MS
        # timeTo is anchored to now (not timeFrom) so the search horizon stays
        # inside Uzum's allowed window.
        assert b["timeTo"] <= after + client._SLOT_WINDOW_MS
        assert b["timeFrom"] < b["timeTo"]
        assert slots == [{"timeFrom": 1, "timeTo": 2}]

    def test_explicit_window_passed_through(self):
        calls, fake = _capture()
        fake.ret = {"payload": {"timeSlots": []}}
        with patch.object(client, "http_json", fake):
            client.get_time_slots("10945", [], "FULLFILMENT", time_from=111, time_to=222)
        b = calls[0]["body"]
        assert b["timeFrom"] == 111 and b["timeTo"] == 222


class TestCreateInvoice:
    def test_exact_create_body(self):
        calls, fake = _capture()
        fake.ret = {"id": 3631101, "invoiceNumber": 1100036311015, "status": "Создана"}
        lines = [
            {"skuId": 782119, "quantityToStock": 10, "purchasePrice": 3100},
            {"skuId": 782120, "quantityToStock": 10, "purchasePrice": 3100},
        ]
        with patch.object(client, "http_json", fake):
            inv = client.create_invoice("10945", lines, time_from=1782295200000, stock_id=34)
        c = calls[0]
        assert c["method"] == "POST"
        assert c["url"].endswith("/shop/10945/v2/invoice/create")
        # The body must be EXACTLY what the HAR shows — no extra/missing keys.
        assert c["body"] == {
            "skuList": lines,
            "timeFrom": 1782295200000,
            "stockId": 34,
        }
        assert inv["id"] == 3631101

    def test_create_without_slot_omits_timefrom(self):
        # «Выбрать позже» — timeFrom omitted entirely (HAR Поставки10+).
        calls, fake = _capture()
        fake.ret = {"id": 1, "timeSlotReservation": None}
        lines = [{"skuId": 5, "quantityToStock": 2, "purchasePrice": 100}]
        with patch.object(client, "http_json", fake):
            client.create_invoice("10945", lines, None, 34)
        assert calls[0]["body"] == {"skuList": lines, "stockId": 34}
        assert "timeFrom" not in calls[0]["body"]


# ── Faza 3: change-slot + cancel (action endpoints, HAR-pinned) ──────


class TestInvoiceTimeSlots:
    def test_get_body_shape(self):
        calls, fake = _capture()
        fake.ret = {"payload": {"timeSlots": [{"timeFrom": 1, "timeTo": 2}]}}
        with patch.object(client, "http_json", fake):
            slots = client.invoice_time_slots("51948", 3631245, "FULLFILMENT", time_from=999)
        c = calls[0]
        assert c["method"] == "POST"
        assert c["url"].endswith("/shop/51948/v2/invoice/time-slot/get")
        # HAR: {invoiceIds:[id], poolSource, timeFrom} — no skuList, no timeTo.
        assert c["body"] == {"invoiceIds": [3631245], "poolSource": "FULLFILMENT", "timeFrom": 999}
        assert slots == [{"timeFrom": 1, "timeTo": 2}]

    def test_default_timefrom_is_future(self):
        # No explicit time_from → must auto-fill a FUTURE timestamp (Uzum
        # rejects past/now with validation-failed-001, same as time-slot).
        import time as _t

        calls, fake = _capture()
        fake.ret = {"payload": {"timeSlots": []}}
        before = int(_t.time() * 1000)
        with patch.object(client, "http_json", fake):
            client.invoice_time_slots("51948", 3631245, "FULLFILMENT")
        assert calls[0]["body"]["timeFrom"] >= before + client._TIMEFROM_LEAD_MS


class TestSetTimeSlot:
    def test_set_body_and_unwrap(self):
        calls, fake = _capture()
        fake.ret = {"payload": [{"id": 3631245, "invoiceNumber": 1100036312456}]}
        with patch.object(client, "http_json", fake):
            inv = client.set_time_slot("51948", 3631245, 1782149400000, 34, "FULLFILMENT")
        c = calls[0]
        assert c["method"] == "POST"
        assert c["url"].endswith("/shop/51948/v2/invoice/time-slot/set")
        assert c["body"] == {
            "timeFrom": 1782149400000,
            "invoiceIds": [3631245],
            "stockId": 34,
            "poolSource": "FULLFILMENT",
        }
        # response payload is a LIST → unwrap to the first invoice.
        assert inv["id"] == 3631245


class TestPrintInvoiceAct:
    def test_decodes_base64_pdf(self):
        import base64

        calls, fake = _capture()
        fake.ret = {"pdf": base64.b64encode(b"%PDF-1.5 act").decode()}
        with patch.object(client, "http_json", fake):
            pdf = client.print_invoice_act("5983", 3385789)
        c = calls[0]
        assert c["method"] == "GET"
        assert c["url"].endswith("/shop/5983/invoice/printInvoice?invoiceId=3385789")
        assert pdf == b"%PDF-1.5 act"

    def test_empty_when_no_pdf(self):
        calls, fake = _capture()
        fake.ret = {}
        with patch.object(client, "http_json", fake):
            assert client.print_invoice_act("5983", 1) == b""


class TestPrintBarcodes:
    def test_body_shape_and_raw_pdf(self):
        # barcodes/print returns RAW application/pdf, not JSON → uses the
        # session directly. Patch _get_http_session to capture the POST.
        captured = {}

        class FakeResp:
            status_code = 200
            content = b"%PDF-1.5 qr"
            text = ""

        class FakeSess:
            def request(self, method, url, json=None, headers=None, timeout=None):
                captured.update(method=method, url=url, body=json)
                return FakeResp()

        with patch.object(client, "_get_http_session", lambda: FakeSess()), patch.object(
            client, "_get_admin_token", lambda: "tok"
        ):
            lines = [{"skuId": 8597104, "amount": 4}, {"skuId": 8597105, "amount": 4}]
            pdf = client.print_barcodes("5983", lines, barcode_type=5)
        assert captured["method"] == "POST"
        assert captured["url"].endswith("/shop/5983/products/v3/barcodes/print")
        # HAR shape: data[] of {barcodeTypeId, skuId, amount}; zero-amount dropped.
        assert captured["body"] == {
            "data": [
                {"barcodeTypeId": 5, "skuId": 8597104, "amount": 4},
                {"barcodeTypeId": 5, "skuId": 8597105, "amount": 4},
            ]
        }
        assert pdf == b"%PDF-1.5 qr"

    def test_drops_zero_amount_rows(self):
        captured = {}

        class FakeResp:
            status_code = 200
            content = b"x"
            text = ""

        class FakeSess:
            def request(self, method, url, json=None, headers=None, timeout=None):
                captured.update(body=json)
                return FakeResp()

        with patch.object(client, "_get_http_session", lambda: FakeSess()), patch.object(
            client, "_get_admin_token", lambda: "tok"
        ):
            client.print_barcodes(
                "5983", [{"skuId": 1, "amount": 0}, {"skuId": 2, "amount": 3}], 1
            )
        assert captured["body"] == {"data": [{"barcodeTypeId": 1, "skuId": 2, "amount": 3}]}


# ── «QR-коды» — bizning HTML chop sahifasi (har birlik 1 yorliq) ─────
#
# QR endi Uzum PDF emas, BIZNING fbs_qr_print.html'da chiqadi. Yorliqlar
# soni/mazmuni noto'g'ri bo'lsa ombor skanerlay olmaydi — shu yer pin qilinadi.


class TestFlattenQrLines:
    def test_grouped_skus_flattened(self):
        products = [
            {
                "productTitle": "Uzuk",
                "skuForInvoiceDtoList": [
                    {"id": 718364, "skuTitle": "СЕРЫЙ-16", "quantityToStock": 8, "barcode": "BC16"},
                    {"id": 718365, "skuTitle": "СЕРЫЙ-17", "quantityToStock": 3, "barcode": "BC17"},
                ],
            }
        ]
        flat = _flatten_qr_lines(products)
        assert flat == [
            {"skuId": 718364, "skuTitle": "СЕРЫЙ-16", "barcode": "BC16", "qty": 8},
            {"skuId": 718365, "skuTitle": "СЕРЫЙ-17", "barcode": "BC17", "qty": 3},
        ]

    def test_group_without_sublist_uses_group_fields(self):
        # Ba'zi javoblarda skuForInvoiceDtoList yo'q — guruh o'zi bitta SKU.
        products = [
            {"id": 5, "productTitle": "Tovar", "skuTitle": "RED", "quantityToStock": 2}
        ]
        flat = _flatten_qr_lines(products)
        assert flat == [{"skuId": 5, "skuTitle": "RED", "barcode": "", "qty": 2}]

    def test_missing_barcode_left_empty_for_db_fill(self):
        products = [{"id": 9, "skuTitle": "X", "quantityToStock": 1}]
        assert _flatten_qr_lines(products)[0]["barcode"] == ""

    def test_skutitle_falls_back_to_product_title(self):
        products = [{"id": 9, "productTitle": "Mahsulot", "quantityToStock": 1}]
        assert _flatten_qr_lines(products)[0]["skuTitle"] == "Mahsulot"


class TestBuildQrLabels:
    def test_one_label_per_unit(self):
        flat = [{"skuId": 1, "skuTitle": "A", "barcode": "BC", "qty": 3}]
        labels = _build_qr_labels(flat)
        assert len(labels) == 3
        assert all(l == {"sku": "A", "barcode": "BC"} for l in labels)

    def test_qr_content_is_barcode_when_present(self):
        flat = [{"skuId": 1, "skuTitle": "A", "barcode": "BC", "qty": 1}]
        assert _build_qr_labels(flat)[0]["barcode"] == "BC"

    def test_falls_back_to_skutitle_when_no_barcode(self):
        # Shtrix-kod topilmasa, FBS bilan bir xil zaxira: skuTitle QR mazmuni bo'ladi.
        flat = [{"skuId": 1, "skuTitle": "СЕРЫЙ-16", "barcode": "", "qty": 2}]
        labels = _build_qr_labels(flat)
        assert len(labels) == 2
        assert labels[0] == {"sku": "СЕРЫЙ-16", "barcode": "СЕРЫЙ-16"}

    def test_zero_qty_skipped(self):
        flat = [
            {"skuId": 1, "skuTitle": "A", "barcode": "BC", "qty": 0},
            {"skuId": 2, "skuTitle": "B", "barcode": "BD", "qty": 2},
        ]
        labels = _build_qr_labels(flat)
        assert len(labels) == 2 and all(l["barcode"] == "BD" for l in labels)

    def test_empty_content_skipped(self):
        # Na barcode na skuTitle → yorliq chiqmaydi (bo'sh QR oldini olish).
        flat = [{"skuId": 1, "skuTitle": "", "barcode": "", "qty": 5}]
        assert _build_qr_labels(flat) == []


class TestCancelInvoice:
    def test_cancel_body_minimal(self):
        calls, fake = _capture()
        fake.ret = {}  # cancel returns empty 200
        with patch.object(client, "http_json", fake):
            ok = client.cancel_invoice("51948", 3631245)
        c = calls[0]
        assert c["method"] == "POST"
        assert c["url"].endswith("/shop/51948/invoice/cancelInvoice")
        assert c["body"] == {"id": 3631245}
        assert ok is True
