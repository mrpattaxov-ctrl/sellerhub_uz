"""«Новый товар» — 3-QADAM: ommaviy tahrirlash + «Yakunlash» validatsiyasi.

Uzumga 1:1 keltirildi (2026-07-22). Uch narsa o'zgardi:

  1. Ustun sarlavhasidagi nishon SARALASH emas — «Ommaviy tahrirlash»
     modalini ochadi.
  2. «Yakunlash» DOIM faol; bosilganda bo'sh majburiy kataklar qizaradi va
     ostida «Обязательное поле» chiqadi (eski hint qatori olib tashlandi).
  3. Modal sarlavhasi/matni bandl i18n dan olindi.

DALIL — Uzum portalining O'Z bandli (HAR ichidan ochilgan,
`mf-products.1.43.36.fe55a55a.js`):

  · komponent `data-v-12137e6d`:
        .attribute-header -> tugma (size:small, variant:secondary,
        shape:square) -> `attribute-bulk-modal`
        __header-text{flex-direction:column;gap:4px}
        __close-button{margin-left:auto}
        __actions{display:flex;justify-content:flex-end;gap:8px}
        .attribute-header__required{color:red}
  · i18n (ru @2019442 / uz @2021501):
        bulkEditTitle          «Массовое редактирование» / «Ommaviy tahrirlash»
        applyValueToAllSku     «Применить значение «{name}» ко всем SKU»
                               / ««{name}» qiymatini barcha SKUlarga qoʻllash»
        requiredField          «Обязательное поле» / «Maydon toʻldirish majburiy»
  · i18n `create_filters` (ru @365968 / uz @429024):
        validation_errors      «Данные заполнены некорректно. Проверьте
                               введённые значения.» / «Maʼlumotlar notoʻgʻri
                               toʻldirilgan. Kiriting qiymatlarni tekshiring.»
  · `.u-select-input--error{border:1px solid var(--border-negative,#E50000)}`

JONLI TEKSHIRUV (Playwright, localhost:5000, stub step3 javobi bilan):
  sarlavha `*` rgb(255,0,0) · «maks. qiymatlar: 5» · «Yakunlash» faol →
  6 ta qizil katak (3 SKU × 2 majburiy), ramka rgb(229,0,0), matn
  «Maydon toʻldirish majburiy», bildirishnoma `validation_errors` ·
  modal 600px, «Qoʻllash» → uchala qatorga qiymat yozildi, xato kataklar 6→3.
"""
from __future__ import annotations

from pathlib import Path

FEATURE = Path(__file__).resolve().parent.parent / "noviy_tavar"
JS = FEATURE / "static" / "noviy_tavar.js"
CSS = FEATURE / "static" / "noviy_tavar.css"
TEMPLATE = FEATURE / "templates" / "noviy_tavar.html"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _fn(name: str) -> str:
    src = _read(JS)
    i = src.index("function " + name + "(")
    return src[i:src.index("\n  }", i)]


# ── 1. Nishon = ommaviy tahrirlash, saralash EMAS ──────────────────


def test_header_icon_opens_bulk_modal_not_sort():
    js = _read(JS)
    assert "s3OpenBulk(Number(btn.getAttribute('data-ai')))" in js
    assert "nt-s3-bulk-btn" in js
    # Saralash butunlay olib tashlangan (Uzumda ustunni saralash YO'Q).
    assert "function s3SortBy" not in js, "saralash qaytib kelgan"
    assert "S3.sort" not in js
    assert "NT_S3.sort" not in js


def test_bulk_button_labelled_with_bundle_title():
    head = _fn("s3Render")
    assert "esc(NT_S3.bulkTitle)" in head


# ── 2. Modal: matnlar bandldan ────────────────────────────────────


def test_bulk_modal_markup_exists():
    tpl = _read(TEMPLATE)
    for el in ("ntS3BulkModal", "ntS3BulkTitle", "ntS3BulkSub", "ntS3BulkLimits",
               "ntS3BulkField", "ntS3BulkApply", "ntS3BulkCancel", "ntS3BulkClose"):
        assert 'id="%s"' % el in tpl, el + " yo'q"
    assert "Массовое редактирование" in tpl and "Ommaviy tahrirlash" in tpl


def test_bulk_strings_match_bundle():
    js = _read(JS)
    assert "bulkTitle:   tr('Ommaviy tahrirlash', 'Массовое редактирование')" in js
    # applyValueToAllSku — {name} interpolatsiyasi bilan.
    assert "'«' + nm + '» qiymatini barcha SKUlarga qoʻllash'" in js
    assert "'Применить значение «' + nm + '» ко всем SKU'" in js
    # Nomsiz holat uchun ...Generic.
    assert "NT_S3.bulkAllGen" in js


def test_bulk_modal_shows_max_values_limit():
    body = _fn("s3OpenBulk")
    assert "s3MaxLabel(mv)" in body and "s3IsMulti(a.valueType)" in body


def test_bulk_apply_writes_every_sku_with_array_copy():
    body = _fn("s3ApplyBulk")
    assert "S3.sku.forEach" in body
    # Massiv HAR qatorga NUSXA bo'lib tushishi shart — aks holda bitta chip
    # o'chirilganda hamma qatordan yo'qolardi.
    assert "Array.isArray(val) ? val.slice() : val" in body


def test_bulk_modal_reuses_the_cell_control():
    """Bandl: modaldagi maydon katakdagi boshqaruvning AYNAN o'zi."""
    body = _fn("s3OpenBulk")
    assert "s3FillCell(field, S3_BULK_SKU, a)" in body


# ── 3. «Yakunlash» validatsiyasi ──────────────────────────────────


def test_finish_is_never_disabled_by_empty_required():
    body = _fn("s3SyncSave")
    assert "save.disabled = !(S3.sku.length && S3.attrs.length);" in body
    assert "missing" not in body, "eski majburiy-gate tugmani yana o'chiryapti"


def test_finish_marks_each_empty_required_cell():
    body = _fn("s3MarkRequiredErrors")
    assert "if (!a.required) return;" in body
    assert "S3.sku.forEach" in body
    assert "s3IsEmpty(a.valueType, s3Get(sk.skuId, a.attributeCode))" in body


def test_cell_error_message_is_bundle_required_field():
    js = _read(JS)
    assert ("requiredField: tr('Maydon toʻldirish majburiy', 'Обязательное поле')"
            in js)
    assert "m.textContent = NT_S3.requiredField;" in _fn("s3PaintCellError")


def test_finish_notifies_with_bundle_validation_errors():
    js = _read(JS)
    assert "validationErrors: tr('Maʼlumotlar notoʻgʻri toʻldirilgan." in js
    assert "'Данные заполнены некорректно. Проверьте введённые значения.')" in js
    send = _fn("s3Send")
    assert "notify(NT_S3.validationErrors);" in send


def test_filling_a_cell_clears_its_error():
    body = _fn("s3Set")
    assert "s3ClearCellError(skuId, code)" in body
    # Katak QAYTA QURILMAYDI — ochiq dropdown uzilib qolmasin.
    clear = _fn("s3ClearCellError")
    assert "classList.remove('has-error')" in clear
    assert "s3FillCell" not in clear


# ── 4. CSS ────────────────────────────────────────────────────────


def test_error_styles_use_bundle_negative_color():
    css = _read(CSS)
    assert "--nt-negative: #e50000;" in css
    assert ".nt-s3-td.has-error .nt-s3-combo" in css
    assert ".nt-s3-cell-err" in css


def test_bulk_modal_css_matches_bundle_geometry():
    css = _read(CSS)
    assert ".nt-modal--bulk { width: 600px; }" in css
    i = css.index(".nt-modal--bulk .nt-modal-foot")
    foot = css[i:css.index("}", i)]
    assert "justify-content: flex-end" in foot and "gap: 8px" in foot
    j = css.index(".nt-s3-bulk-head {")
    head = css[j:css.index("}", j)]
    assert "flex-direction: column" in head and "gap: 4px" in head


def test_enum_dropdown_renders_above_the_modal():
    """Xuddi shu dropdown modal ichida ham ochiladi — overlay z-index 1000."""
    css = _read(CSS)
    i = css.index(".nt-s3-pop {")
    rule = css[i:css.index("}", i)]
    assert "z-index: 1100" in rule
