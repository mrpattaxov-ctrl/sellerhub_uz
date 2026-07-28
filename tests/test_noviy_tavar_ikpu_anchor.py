"""«Новый товар» — ИКПУ panelining JOYLASHUVI (bag 2026-07-22).

BAG: 2-qadamdagi ⚡ («Указать для всей колонки») bosilib, popover ichidagi
«Выберите» tugmasi bosilganda IKPU paneli sahifaning chap-yuqori burchagiga
uchib ketardi.

SABABI (jonli o'lchandi, Playwright + localhost:5000, mahsulot 3090201):
    s2OpenIkpu() birinchi qatorda s2CloseBulk() chaqirardi → bulk popover
    `display:none` bo'lardi → anchor SHU popover ICHIDA bo'lgani uchun
    getBoundingClientRect() NOL qaytarardi → top = scrollY+6, left = 8.

    o'lchov «before»: ⚡ (742, 665) → panel (8, 6)     ← bag
    o'lchov «after» : ⚡ (742, 665) → panel (657, 768) ← «Выберите» ostida

Shu fayl xuddi shu tuzoq qaytmasligini qulflaydi.
"""
from __future__ import annotations

import re
from pathlib import Path

JS = Path(__file__).resolve().parent.parent / "noviy_tavar" / "static" / "noviy_tavar.js"


def _body(name: str) -> str:
    """`function <name>(...) { ... }` tanasi (2 bo'shliq chekinishi bilan yozilgan)."""
    src = JS.read_text(encoding="utf-8")
    i = src.index("function " + name + "(")
    j = src.index("\n  }\n", i)
    return src[i:j]


def test_open_ikpu_bulk_popoverni_yopmaydi() -> None:
    """Anchor bulk popover ichida bo'lsa — popover OCHIQ qoladi."""
    body = _body("s2OpenIkpu")
    assert "inBulk" in body
    assert "if (!inBulk) s2CloseBulk();" in body


def test_open_ikpu_shartsiz_close_bulk_qilmaydi() -> None:
    """Shartsiz `s2CloseBulk()` = o'sha bagning o'zi (anchor o'lchovi nolga tushadi)."""
    body = _body("s2OpenIkpu")
    # Eski kod aynan `s2CloseBulk.call(null);` deb yozilgan edi — ikkala
    # shaklni ham ushlaymiz.
    assert re.search(r"^\s*s2CloseBulk(\(\)|\.call\()", body, re.M) is None


def test_anchor_hujjat_koordinatalarida_saqlanadi() -> None:
    """Panel qayta joylashishi uchun anchor o'rni eslab qolinishi shart."""
    body = _body("s2OpenIkpu")
    assert "ikpuAnchorBox" in body
    assert "window.scrollY" in body and "window.scrollX" in body


def test_place_ikpu_ekranga_sig_diradi() -> None:
    """Pastda joy yo'q bo'lsa tepaga ochiladi, o'ngda chiqib ketmaydi."""
    body = _body("s2PlaceIkpu")
    assert "clientHeight" in body      # vertikal joy hisobi (flip)
    assert "clientWidth" in body       # o'ng chekka qisqichi
    assert "Math.min" in body and "Math.max" in body


def test_place_ikpu_render_dan_keyin_qayta_chaqiriladi() -> None:
    """Ro'yxat to'lgach balandlik o'zgaradi — joy qayta hisoblanmasa, tepaga
    ochilgan panel bilan tugma orasida bo'sh joy qoladi."""
    src = JS.read_text(encoding="utf-8")
    assert src.count("s2PlaceIkpu()") >= 3   # ochilish + note + ro'yxat
    assert "s2PlaceIkpu();" in _body("s2IkpuNote")


def test_ikpu_ichidagi_klik_bulkni_yopmaydi() -> None:
    """Panel bulk popover'dan ochilgan — ichidagi klik popoverni yopmasin."""
    src = JS.read_text(encoding="utf-8")
    i = src.index("var inIkpu = ")
    tail = src[i:i + 400]
    assert "!inIkpu && !bulkPop.contains(e.target)" in tail


def test_fokus_sahifani_sakratmaydi() -> None:
    """`focus()` sahifani surib yubormasin — foydalanuvchi «tepaga ketdi» deydi."""
    body = _body("s2OpenIkpu")
    assert "preventScroll: true" in body
