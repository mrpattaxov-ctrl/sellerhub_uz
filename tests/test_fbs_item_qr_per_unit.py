"""Product-QR stickers must come out PER UNIT, not per orderItem line.

Bug (2026-06-12): an order with items amount=3,1,2,3 (9 pieces) printed only
4 QR stickers — one per SKU line. Warehouse needs one sticker per physical
piece. ``fbs.routes._item_qr_pdfs`` renders the sticker once per item and
repeats the bytes ``amount`` times; all four print paths (single-order qr,
bulk qr / label_with_qr / qr_with_label) go through it.
"""
from __future__ import annotations

import fbs.routes as routes


def _items(*amounts):
    return [{"amount": a, "barcode": f"BC{i}", "skuTitle": f"SKU{i}"}
            for i, a in enumerate(amounts)]


def test_repeats_sticker_per_unit(monkeypatch):
    monkeypatch.setattr(routes, "render_product_qr_pdf",
                        lambda item, size=None, match_uzum_label=False: item["barcode"].encode())
    out = routes._item_qr_pdfs(_items(3, 1, 2, 3))
    assert len(out) == 9                       # 3+1+2+3 dona = 9 stiker
    assert out[:3] == [b"BC0", b"BC0", b"BC0"]  # bir SKU'ning donalari ketma-ket
    assert out[3] == b"BC1"


def test_bad_or_missing_amount_defaults_to_one(monkeypatch):
    monkeypatch.setattr(routes, "render_product_qr_pdf",
                        lambda item, size=None, match_uzum_label=False: b"QR")
    # amount yo'q / None / 0 / matn — hammasi 1 dona deb olinadi.
    items = [{"barcode": "x"}, {"amount": None, "barcode": "x"},
             {"amount": 0, "barcode": "x"}, {"amount": "abc", "barcode": "x"}]
    assert len(routes._item_qr_pdfs(items)) == 4


def test_failed_render_is_skipped_without_repeating(monkeypatch):
    # Render b"" qaytarsa (xato) — o'sha item butunlay tashlanadi, amount
    # bo'yicha bo'sh baytlar ko'paytirilmaydi.
    monkeypatch.setattr(routes, "render_product_qr_pdf",
                        lambda item, size=None, match_uzum_label=False: b"" if item["barcode"] == "bad" else b"OK")
    items = [{"amount": 5, "barcode": "bad"}, {"amount": 2, "barcode": "good"}]
    assert routes._item_qr_pdfs(items) == [b"OK", b"OK"]


def test_size_is_forwarded(monkeypatch):
    seen = []
    def fake_render(item, size=None, match_uzum_label=False):
        seen.append(size)
        return b"QR"
    monkeypatch.setattr(routes, "render_product_qr_pdf", fake_render)
    routes._item_qr_pdfs(_items(1), product_qr_size="uzum")
    routes._item_qr_pdfs(_items(1))            # size'siz → default chaqiruv
    assert seen == ["uzum", None]


def test_match_uzum_label_is_forwarded(monkeypatch):
    # «Yorliq + QR» / «QR + yorliq» rejimida QR sahifasi Uzum yorlig'ining
    # portret+/Rotate-90 geometriyasiga moslanishi kerak (match_uzum_label=True),
    # yolg'iz «QR» chopida esa False (Abdulaziz 2026-06-17).
    seen = []
    def fake_render(item, size=None, match_uzum_label=False):
        seen.append(match_uzum_label)
        return b"QR"
    monkeypatch.setattr(routes, "render_product_qr_pdf", fake_render)
    routes._item_qr_pdfs(_items(1), match_uzum_label=True)
    routes._item_qr_pdfs(_items(1))            # default → False
    assert seen == [True, False]
