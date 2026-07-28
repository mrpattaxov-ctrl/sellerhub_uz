"""«Новый товар» 2-qadam — MAHSULOT SKU'si saqlangandan keyin O'ZGARMAYDI.

MUAMMO (foydalanuvchi talabi 2026-07-28): Uzumda mavjud kartani ochsangiz
«SKU для названия товара» maydoni tahrirlanmaydi (foydalanuvchi kadri:
KOMPLEKT8). Bizda esa uni yozib bo'lardi.

DALIL — BANDL (mf-products, HAR: .uicheck/uzum-har/products_new.har @15691746):
    readonly: O.isEditing && !!O.original.productSkuTitle
              && !O.isProductInActiveInvoice

  · Uchinchi shart o'sha chunk'da `Z=ref(!1)` bo'lib e'lon qilingan va HECH
    QAYERDA o'zlashtirilmagan (`Z.value=` bitta ham yo'q) => amalda DOIM
    false. Ya'ni qoida `isEditing && saqlangan productSkuTitle bor`.
  · Bizga alohida «isEditing» bayrog'i kerak emas: yangi qoralamada
    `productSkuTitle` BO'SH keladi (routes.py qaydi: qoralama 3068623 ->
    productSkuTitle=""), mavjud kartada esa to'la.
    JONLI DALIL (2026-07-28): mahsulot 2907838 -> productSkuTitle="BRELOK1".

NEGA qulf: prefiks har bir SKU'ning `skuTitle` ichiga pishirilgan
("LUXUZ-BRELOK1-КОРИЧН") — keyin o'zgartirilsa mavjud SKU'lar uziladi.

⚠️ QAMROV: Uzumda bu QULF FAQAT UI'da (`readonly` — HTML atributi; Uzumning
o'z API'si o'zgargan prefiksni qabul qiladi). Shuning uchun biz ham server
tomonga qo'shimcha qulf QO'YMAYMIZ — aks holda Uzumdan QATTIQROQ bo'lardi va
har saqlashga qo'shimcha portal so'rovi kerak bo'lardi.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "noviy_tavar" / "static" / "noviy_tavar.js"
CSS = ROOT / "noviy_tavar" / "static" / "noviy_tavar.css"


@pytest.fixture(scope="module")
def js() -> str:
    return JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def css() -> str:
    return CSS.read_text(encoding="utf-8")


def _rule(css: str, selector: str) -> str:
    clean = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    i = clean.index(selector + " {")
    return clean[i:clean.index("}", i)]


# ── 1. Qulf saqlangan qiymat bo'yicha qo'yiladi ─────────────────────


def test_lock_is_applied_from_the_loaded_product_sku(js):
    """`productSkuTitle` bo'sh emas -> qulf. Bo'sh (yangi qoralama) -> yo'q."""
    i = js.index("var pf = document.getElementById('ntSkuPrefix');")
    body = js[i:i + 400]
    assert "s2LockPrefix(pf, !!d.productSkuTitle);" in body


def test_lock_uses_readonly_not_disabled(js):
    """Uzum `readonly` beradi — `disabled` EMAS: matn o'qiladi va nusxa olinadi."""
    i = js.index("function s2LockPrefix(")
    body = js[i:i + 500]
    assert "pf.readOnly = !!locked;" in body
    assert "disabled" not in body, "disabled qilinmasin — Uzum kulrang qilmaydi"


def test_lock_resets_on_reload(js):
    """Bir xil DOM elementi qayta ishlatiladi — qoralamaga o'tilganda qulf
    TUSHISHI kerak, aks holda yangi tovarga SKU yozib bo'lmaydi."""
    i = js.index("function s2LockPrefix(")
    body = js[i:i + 500]
    # Shartsiz o'zlashtirish (`if (locked)` emas) — tiklanish shu bilan ta'minlanadi.
    assert "pf.readOnly = !!locked;" in body
    assert "if (locked) pf.readOnly" not in body


def test_lock_state_is_tracked_on_the_step_state(js):
    assert "skuLocked: false" in js
    i = js.index("function s2LockPrefix(")
    assert "S2.skuLocked = !!locked;" in js[i:i + 300]


# ── 2. Ko'rinish: kulrang EMAS, faqat interaktivlik o'chadi ─────────


def test_readonly_input_has_no_style_of_its_own(css):
    """⚠️ Bandlda yangi `u-input` ning `readonly` holati uchun BITTA ham
    uslub qoidasi YO'Q — ko'rinish o'zgarmaydi. Topilgan
    `cursor:not-allowed` qoidalari ESKI komponentlarniki
    (`data-v-65bf44cc`, `data-v-7029fe4f`) va bu maydonga tegishli emas.

    Shuning uchun bizda ham `[readonly]` selektori BO'LMASLIGI kerak:
    ilgari shu yerda dalilsiz `cursor:default` + fokus ramkasini o'chirish
    turgandi, olib tashlandi."""
    clean = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    assert ".nt-input[readonly]" not in clean, (
        "readonly uchun uslub qo'shilgan — bandldan dalil keltiring"
    )


# ── 3. Tuzatilmas xato tuzog'i ─────────────────────────────────────


def test_locked_prefix_is_not_validated(js):
    """Qulflangan qiymat Uzumdan keldi va foydalanuvchi uni tuzata olmaydi —
    format tekshiruvi «Saqlash» ni butunlay to'sib qo'ymasligi kerak."""
    for fn in ("function s2Validate(", "function s2Send("):
        i = js.index(fn)
        body = js[i:i + 900]
        assert "S2.skuLocked ? null : s2PrefixError(" in body, f"{fn} da qo'riqchi yo'q"


# ── 4. Doira: server tomonga qulf QO'SHILMAGAN (ataylab) ───────────


def test_no_server_side_prefix_lock(js):
    """Uzumning O'ZIDA bu faqat UI qulfi. Server tomonda takrorlamaymiz —
    aks holda Uzumdan qattiqroq bo'lardi + har saqlashga ortiqcha so'rov."""
    routes = (ROOT / "noviy_tavar" / "routes.py").read_text(encoding="utf-8")
    i = routes.index("def nt_send_sku(")
    body = routes[i:i + 2500]
    assert "product_description_response" not in body, (
        "saqlash yo'liga qo'shimcha portal so'rovi qo'shilgan"
    )
