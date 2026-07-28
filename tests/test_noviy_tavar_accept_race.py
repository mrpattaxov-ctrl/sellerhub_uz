"""«Новый товар» — «Qabul qilish» poygasi va localStorage himoyasi.

JONLI TOPILGAN XATO (2026-07-21): `pickLevel()` da quyi daraja ASINXRON
qo'shilardi, lekin `syncAccept()` sinxron chaqirilardi. Shu ~400ms oynada
`leafSelected()` tanlovni BARG deb hisoblab «Qabul qilish»ni yoqib yuborardi.

Oqibati: foydalanuvchi ildiz kategoriyani («Aksessuarlar», id 10003)
tasdiqlab, mahsulotni noto'g'ri joyga qo'yishi mumkin edi.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "noviy_tavar" / "static" / "noviy_tavar.js"


@pytest.fixture(scope="module")
def js() -> str:
    # ⚠️ encoding aniq berilgan: fayl katta va grep uni «binary» deb
    # belgilaydi (Windows quirk) — matn sifatida o'zimiz o'qiymiz.
    return JS.read_text(encoding="utf-8")


def test_accept_is_blocked_while_children_load(js):
    assert "pendingChildren" in js, "poyga qulfi yo'q"
    start = js.index("function leafSelected")
    body = js[start:start + 400]
    assert "if (cat.pendingChildren) return null" in body, (
        "leafSelected() yuklanish holatini tekshirmayapti"
    )


def test_pick_uses_generation_guard(js):
    """Kech kelgan javob yot daraja qo'shmasin (postavki `S.rstGen` naqshi).

    Usiz: tortish paytida matn terilsa, `pendingChildren` abadiy yoqiq
    qolib «Qabul qilish» butunlay o'lardi."""
    start = js.index("function pickLevel")
    body = js[start:start + 1400]
    assert "++cat.pickGen" in body, "avlod raqami olinmayapti"
    assert "gen !== cat.pickGen" in body, "eskirgan javob tashlanmayapti"


def test_typing_cancels_pending_child_fetch(js):
    """Matn terish kutilayotgan tortishni bekor qilsin (qulfni ochsin)."""
    # Faylda bir nechta `input` ishlovchisi bor — KATEGORIYA darajasidagisi
    # `renderLevels()` ichida.
    start = js.index("function renderLevels")
    i = js.index("input.addEventListener('input'", start)
    body = js[i:i + 700]
    assert "cat.pickGen++" in body and "cat.pendingChildren = false" in body, (
        "terish kutilayotgan tortishni bekor qilmayapti — tugma o'lik qolishi mumkin"
    )


def test_empty_array_is_not_treated_as_cache_hit(js):
    """`if ([])` JS'da TRUE — bo'sh ro'yxat xotira keshida hit deb sanalib,
    tarmoqqa qayta chiqmasdik (o'tkinchi bo'sh javob qotib qolardi)."""
    start = js.index("function fetchCategories")
    body = js[start:start + 900]
    assert "cat.cache[key] && cat.cache[key].length" in body


def test_every_localstorage_call_is_guarded(js):
    """localStorage private rejimda otadi — himoyasiz chaqiruv butun
    wizardni o'ldiradi (postavki ham har joyda try/catch qiladi)."""
    for m in re.finditer(r"localStorage\.", js):
        window = js[max(0, m.start() - 200):m.start()]
        assert "try {" in window or "try{" in window, (
            f"himoyalanmagan localStorage chaqiruvi, offset {m.start()}"
        )
