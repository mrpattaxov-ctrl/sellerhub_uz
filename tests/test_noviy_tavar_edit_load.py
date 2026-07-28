"""«Новый товар» — MAVJUD kartani 1-qadamга yuklab tahrirlash.

MAQSAD (foydalanuvchi talabi 2026-07-25): /groups karta ⋮-menyusidagi 3 tahrir
tugmasi HAR BIRI o'z qadamiga aniq borsin:
  · «Umumiy ta'rifni o'zgartirish» → 1-qadam (nom/tavsif/kategoriya YUKLANGAN)
  · «SKU-ni o'zgartirish»            → 2-qadam
  · «Tovar xususiyatlarini o'zgartirish» → 3-qadam

⚠️ Ilgari step=1 URL'i `else` orqali 2-qadamга tushib ketardi (jonli xato).
Va 1-qadam uchun mavjud kartani yuklaydigan yo'l umuman yo'q edi.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "noviy_tavar" / "static" / "noviy_tavar.js"
ROUTES = ROOT / "noviy_tavar" / "routes.py"


@pytest.fixture(scope="module")
def js() -> str:
    return JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def routes() -> str:
    return ROUTES.read_text(encoding="utf-8")


# ── URL routing: har step O'Z qadamiga ───────────────────────────────


def test_url_step_maps_each_step_distinctly(js):
    """?step=1→s1Enter, 2→s2Enter, 3→s3Enter. Ilgari step=1 s2Enter'ga
    tushardi (buggi `if (st===3) s3Enter; else s2Enter`)."""
    i = js.index("?productId=&shop=[&step=3]")
    body = js[i:i + 1400]
    assert "if (st === 1) {" in body and "s1Enter(pid);" in body, "step=1 → s1Enter emas (bug qaytdi)"
    assert "if (st === 3) s3Enter(pid);" in body
    assert "else s2Enter(pid);" in body


# ── Flash fix: 2/3-qadam tahriri 1-qadamni «yondirmasin» ──────────────


def test_deep_edit_pre_sets_step_before_async_load(js):
    """⚠️ FOYDALANUVCHI SHIKOYATI 2026-07-26: step=3 bosilganda 1-qadam «yonib»
    keyin 3-ga sakrardi (async load orasidagi bo'shliq). Endi routing nav
    nishonini load'dan OLDIN kerakli qadamga qo'yadi."""
    i = js.index("?productId=&shop=[&step=3]")
    body = js[i:i + 1400]
    assert "state.step = (st === 3) ? 3 : 2;" in body
    assert "paintSteps()" in body


def test_deep_edit_hidden_server_side(js):
    """Route + shablon: 2/3-qadam URL'ida 1-qadam SERVER-TOMON `hidden`
    render bo'ladi (flash umuman bo'lmasin — JS timing'iga tayanmaymiz)."""
    routes = (ROOT / "noviy_tavar" / "routes.py").read_text(encoding="utf-8")
    assert "deep_edit" in routes
    assert "_step in (2, 3)" in routes
    tpl = (ROOT / "noviy_tavar" / "templates" / "noviy_tavar.html").read_text(encoding="utf-8")
    assert '<div id="ntStep1"{% if deep_edit %} hidden{% endif %}>' in tpl
    assert 'id="ntEditLoader"' in tpl
    assert "{% if not deep_edit %} hidden{% endif %}" in tpl


def test_loader_hidden_when_any_step_renders(js):
    """Yuklagich kerakli qadam ochilishi bilan yo'qoladi — s2Show/s3Show/
    showStep1 uchtasi ham `ntHideEditLoader()` chaqiradi."""
    assert "function ntHideEditLoader" in js
    for fn in ("s2Show", "s3Show", "showStep1"):
        i = js.index("function " + fn)
        assert "ntHideEditLoader()" in js[i:i + 400], fn + " yuklagichni yashirmayapti"


def test_deep_edit_failure_falls_back_to_step1(js):
    """Yuklab bo'lmasa spinner'da qotib qolmasin — s2Enter/s3Enter xatoда
    showStep1() ga tushadi va promise (ok) qaytaradi."""
    for fn in ("s2Enter", "s3Enter"):
        i = js.index("function " + fn)
        body = js[i:i + 900]
        assert "return s" in body, fn + " promise qaytarmayapti"
        assert "showStep1();" in body, fn + " xato zaxirasi yo'q"
        assert "return ok;" in body


# ── 3→1 qaytish: tahrirda 1-qadam ochiq + s1Load bilan yuklanadi ──────


def test_step1_reachable_while_editing_existing_product(js):
    """⚠️ SHIKOYAT 2026-07-26: 3-qadamda turib 1-qadamga qayta olmasdim.
    Sabab: `stepReachable(1)` productId bor + cardFilled yo'q bo'lsa QULF
    qilardi. Endi `editingProduct` bo'lsa ham ochiq (bosilganda yuklanadi)."""
    i = js.index("function stepReachable")
    body = js[i:i + 1100]
    assert "!!state.editingProduct" in body, "tahrirda 1-qadam hali qulf"
    assert "return !state.productId || !!state.cardFilled || !!state.editingProduct;" in body


def test_deep_edit_sets_editing_flag_in_routing(js):
    """Routing (⋮→2/3-qadam) `editingProduct` ni o'rnatadi — aks holda 1-qadam
    nishoni qulf qolardi va `saveDraftNow` yangi-tovar qoralamasini ifloslardi."""
    i = js.index("?productId=&shop=[&step=3]")
    body = js[i:i + 1400]
    assert "state.editingProduct = pid;" in body


def test_gostep1_loads_existing_card_instead_of_blank(js):
    """`goStep(1)` — karta bu sahifada yuklanmagan bo'lsa (cardFilled yo'q)
    BO'SH forma emas, `s1Load` bilan SERVERDAN yuklaydi + spinner ko'rsatadi.
    Aks holda 1-qadam bo'sh chiqib, ustidan saqlash kartani buzardi."""
    i = js.index("function goStep")
    body = js[i:i + 700]
    assert "if (state.productId && !state.cardFilled)" in body
    assert "s1Load(state.productId)" in body
    assert "ntShowEditLoader()" in body
    # Aks hol(cardFilled bor / yangi yaratish) — saqlangan formani ko'rsatamiz.
    assert "showStep1();" in body


# ── s1Load — mavjud kartani 1-qadamга yuklaydi ───────────────────────


def test_s1_load_fetches_and_applies(js):
    assert "function s1Enter" in js and "function s1Load" in js
    i = js.index("function s1Load")
    body = js[i:i + 900]
    assert "/noviy-tavar/api/load-product" in body
    # cardFilled:true — yuklangan holat TOZA baza (editProduct save uchun ham).
    assert "cardFilled: true" in body
    assert "showStep1()" in body
    # Tahrirlash sessiyasi bayrog'i (draft ifloslanmasin).
    assert "state.editingProduct" in body


def test_apply_draft_object_is_shared(js):
    """restoreDraft VA s1Load bitta `applyDraftObject` mashinasini ishlatadi —
    kategoriya/xususiyat qayta qurish takrorlanmaydi."""
    assert "function applyDraftObject" in js
    r = js.index("function restoreDraft")
    rbody = js[r:js.index("function applyDraftObject")]
    assert "applyDraftObject(d, {" in rbody, "restoreDraft applyDraftObject'ni chaqirmayapti"
    # applyDraftObject markFilled bo'lsagina snapshot (tahrir=toza baza; draft=warns).
    a = js.index("function applyDraftObject")
    abody = js[a:a + 2600]
    assert "if (markFilled) { state.cardFilled = true; ntSnapshot(); }" in abody


def test_edit_session_does_not_pollute_new_product_draft(js):
    """s1Load tahririda localStorage «yangi tovar» qoralamasi yozilmasin."""
    i = js.index("function saveDraftNow")
    body = js[i:i + 500]
    assert "if (state.editingProduct) return" in body


# ── Backend: get_product → draft shakli ──────────────────────────────


def test_load_product_route_maps_get_product(routes):
    i = routes.index("def nt_load_product")
    body = routes[i:i + 2600]
    # Kategoriya yo'li parent zanjiridan (bargдан ildizга → reverse).
    assert 'node.get("parent")' in body and "path.reverse()" in body
    # Xususiyatlar: definedCharacteristics → rows[{id,selected}], selected shakli
    # {uz,ru,value,skuValue} (buildCharOptions/create body bilan bir xil).
    assert "definedCharacteristics" in body
    assert '"skuValue": v.get("skuValue")' in body
    assert 'dc.get("characteristicId")' in body, "row id = characteristicId (meta bilan mos)"
    # Kafolat productFields.WARRANTY dan.
    assert 'WARRANTY' in body
    # Rasm productImages → {key,url}.
    assert "productImages" in body


def test_load_product_route_is_read_only(routes):
    """FAQAT O'QISH — get_product (GET), hech qanday yozuvchi chaqiruv yo'q."""
    i = routes.index("def nt_load_product")
    body = routes[i:i + 2600]
    assert "client.get_product(" in body
    for writer in ("edit_product", "create_product", "save_filters", "send_sku"):
        assert writer not in body, "load-product yozuvchi chaqiruvni ishlatmasligi kerak: " + writer
