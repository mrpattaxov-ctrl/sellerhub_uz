"""«Новый товар» 2-qadam — OMBORDA O'LCHANGAN SKU (ВГХ qulfi), Uzumga 1:1.

MUAMMO (foydalanuvchi talabi 2026-07-28): Uzum omborga bir necha bor borgan
tovarni O'ZI o'lchaydi va shundan keyin sotuvchiga eni/uzunligi/balandligi/
og'irligini o'zgartirishga RUXSAT BERMAYDI — to'rtala katak kulrang-qulf,
o'ng chekkasida ko'k ⓘ, ustiga borilsa qora maslahat chiqadi. Bizda bunday
holat umuman yo'q edi: kataklar tahrirlanadigan ko'rinib turardi.

DALIL 1 — JONLI (read-only probe, 2026-07-28):
    GET /api/seller/shop/5983/product/2907838/description-response -> 200
    11/11 SKU:  updatedFromWms: true,  canEdit: true,  status IN_STOCK/RUN_OUT
  ⚠️ `canEdit` HAMMASIDA true — ya'ni mavjud `canEdit` qulfi bu holatni
  QAMRAB OLMAYDI, alohida bayroq shart.

DALIL 2 — BANDL (mf-products, HAR: .uicheck/uzum-har/products_new.har):
  · `edit-product-sku-table` (@15682803): to'rtala o'lchov inputiga
        disabled: f(n) || n.updatedFromWms
    (`f` = status ARCHIVED / ARCHIVED_BLOCKED / BLOCKED)
  · `size-input` (@15676158): xato/ogohlantirish nishoni bo'lmasa va
    `sku.updatedFromWms` bo'lsa -> ⓘ + `$t("create_sku.updated_from_wms")`
  · i18n (@48970064 ru / @49035417 uz) — matn so'zma-so'z shu yerdan
  · validatsiya `y(e,t)` (@15701585): `... || e.updatedFromWms` bo'lsa
    o'lchov tekshiruvi BUTUNLAY o'tkazib yuboriladi
  · ⚡ bulk `R()` (@15711296): `n.updatedFromWms && l` -> o'lchov ustunlari
    chetlab o'tiladi (narx/MXIK esa baribir to'ldiriladi)
  · joylashuv `v(a)` : oxirgi qator -> "top-center", qolgani "bottom-center"
  · maslahat CSS `.base-tooltip__content`: 8px 12px · #1f2026 · #fff · 8px
  · nishon rangi — dizayn tokeni `IconInfo: "#478eff"` (yorug' mavzu)
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "noviy_tavar" / "static" / "noviy_tavar.js"
CSS = ROOT / "noviy_tavar" / "static" / "noviy_tavar.css"
ROUTES = ROOT / "noviy_tavar" / "routes.py"


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


# ── 1. Bayroq qatorga yetib boradi ──────────────────────────────────


def test_row_carries_the_wms_flag(js):
    """`updatedFromWms` description-response'dan qatorga ko'chiriladi."""
    assert "updatedFromWms: !!sk.updatedFromWms," in js


def test_route_passes_sku_list_through_untouched():
    """Server skuList'ni XOM uzatadi — bayroq yo'lda tushib qolmaydi."""
    routes = ROUTES.read_text(encoding="utf-8")
    assert '"skuList": res.get("skuList") or [],' in routes


# ── 2. To'rtala ВГХ katagi qulflanadi ───────────────────────────────


def test_all_four_dimension_cells_get_the_flag(js):
    """Bandl to'rtala inputga bir xil `disabled` shartini beradi."""
    i = js.index("var w = !!row.updatedFromWms;")
    body = js[i:i + 900]
    for field in ("width", "length", "height", "weight"):
        assert f"s2Cell(row, i, '{field}'" in body, f"{field} ustuni topilmadi"
        assert body.count("wms: w") == 4, "to'rtala katak ham bayroq olishi kerak"


def test_cell_disables_the_input_when_measured(js):
    assert "if (opts.wms) inp.disabled = true;" in js


def test_only_dimension_columns_are_locked(js):
    """Narx / chegirma / MXIK / SKU-kodi QULFLANMAYDI — Uzumda ham shunday."""
    i = js.index("var w = !!row.updatedFromWms;")
    tail = js[i:i + 2600]
    for field in ("fullPrice", "off", "sellerItemCode"):
        m = re.search(r"s2Cell\(row, i, '" + field + r"'[^)]*\)", tail, re.S)
        if m:
            assert "wms:" not in m.group(0), f"{field} qulflanmasligi kerak"


# ── 3. Ko'k ⓘ nishoni ───────────────────────────────────────────────


def test_info_icon_uses_uzum_design_token_colour(js):
    """`IconInfo: "#478eff"` — o'zimdan rang o'ylab topilmagan."""
    i = js.index("function s2WmsIcon(")
    body = js[i:i + 1200]
    assert '#478eff' in body
    assert 'viewBox="0 0 16 16"' in body, "bandl `width:16` beradi"


def test_icon_sits_outside_the_input_like_the_bundle(css):
    """`.validation-icon{position:absolute;right:-20px}` — 1:1."""
    rule = _rule(css, '[data-page="noviy-tavar"] .nt-wms')
    assert "position: absolute" in rule
    assert "right: -20px" in rule
    assert "transform: translateY(-50%)" in rule


def test_icon_is_inside_the_relative_input_wrapper(js):
    """Nishon `wrap` (`.nt-td-input`, position:relative) ichida — `td` da emas,
    aks holda `right:-20px` boshqa joyga tushardi."""
    i = js.index("if (opts.wms) wrap.appendChild(s2WmsIcon(")
    assert i > 0
    assert "td.appendChild(wrap);" in js[i:i + 200]


# ── 4. Qora maslahat — matni Uzumniki, ko'rinishi Uzumniki ──────────


def test_tooltip_text_is_verbatim_from_the_bundle_in_both_languages(js):
    i = js.index("function s2WmsText(")
    body = js[i:i + 700]
    assert "Данный SKU был замерен на складе. Для изменения ВГХ " in body
    assert "пожалуйста обратитесь в бизнес-поддержку" in body
    assert "Ushbu SKU omborda o‘lchab bo‘lingan. VGT o‘zgartirish uchun " in body
    assert "biznes qo‘llab-quvvatlash xizmatiga murojaat qiling" in body


def test_tooltip_style_matches_the_bundle(css):
    """`.base-tooltip__content`: padding 8px 12px, #1f2026, #fff, radius 8px."""
    rule = _rule(css, ".nt-wms-tip")
    assert "padding: 8px 12px" in rule
    assert "background: #1f2026" in rule
    assert "color: #fff" in rule
    assert "border-radius: 8px" in rule


def test_tooltip_is_teleported_out_of_the_table(js, css):
    """Jadval `overflow-x: auto` — katak ichidagi maslahat KESILARDI.
    Uzum ham shu sababdan `teleport` qiladi.

    ⚠️ `document.body` ga EMAS, sahifa ildiziga: modul doirasi qoidasi
    (test_noviy_tavar_scope) har bir selektor `[data-page=…]` dan
    boshlanishini talab qiladi."""
    i = js.index("function s2WmsTipEl(")
    assert "(root || document.body).appendChild(wmsTipEl);" in js[i:i + 700]
    rule = _rule(css, '[data-page="noviy-tavar"] .nt-wms-tip')
    assert "position: fixed" in rule


def test_last_row_opens_the_tooltip_upwards(js):
    """Bandl `v(a)`: oxirgi qator -> "top-center" (jadvaldan chiqib ketmasin)."""
    assert "var tipTop = vi === vis.length - 1;" in js
    i = js.index("function s2WmsTipShow(")
    body = js[i:i + 900]
    assert "top ? (a.top - b.height - 8) : (a.bottom + 8)" in body


def test_tooltip_hides_on_rerender(js):
    """Qatorlar qayta qurilganda ochiq maslahat «yetim» qolmasin."""
    i = js.index("function s2Render(")
    assert "s2WmsTipHide();" in js[i:i + 400]


# ── 5. Validatsiya va ⚡ bulk qulfni HURMAT QILADI ──────────────────


def test_measured_row_skips_dimension_validation(js):
    """Aks holda foydalanuvchi to'ldira olmaydigan katak «majburiy» deb
    qizarardi va «Saqlash» hech qachon o'tmasdi."""
    i = js.index("function s2EmptyRequired(")
    body = js[i:i + 800]
    assert "if (!row.updatedFromWms && s2DimsRequired()) {" in body


def test_bulk_fill_skips_dimensions_of_measured_rows(js):
    """⚡ o'lchovni chetlab o'tadi, LEKIN narx/MXIK ni baribir to'ldiradi."""
    i = js.index("var isDim = S2_DIMS.indexOf(bulkOpenFor) >= 0;")
    body = js[i:i + 600]
    assert "if (isDim && r.updatedFromWms) return;" in body
    assert "var S2_DIMS = ['width', 'length', 'height', 'weight'];" in js


def test_can_edit_is_not_reused_as_the_measured_flag(js):
    """JONLI DALIL: o'lchangan 11 SKU'da ham `canEdit: true`. Kim bo'lsa ham
    `canEdit` ni bu holat uchun qayta ishlatmasin."""
    i = js.index("updatedFromWms: !!sk.updatedFromWms,")
    ctx = js[max(0, i - 700):i]
    assert "canEdit: sk.canEdit !== false," in ctx
    assert "canEdit" not in js[js.index("function s2WmsIcon("):
                                js.index("function s2WmsIcon(") + 1200]
