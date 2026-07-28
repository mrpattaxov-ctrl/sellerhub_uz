"""«Новый товар» — 3-QADAM «Свойства» KO'RINISHI Uzumga 1:1 (2026-07-22).

Bu fayl UI qoidalarini qulflaydi. Har bir tasdiqning yonida DALILI turadi —
ikkitasi bor va ikkalasi ham qat'iy:

  · REFERENS KADR — `artifacts/uzum-product-form-reference/screenshots/`
      sacvoyage-filled-step3-properties.png        (SACVOYAGE do'koni, 3-bosqich)
      sacvoyage-step3-operating-system-dropdown.png (ochiq dropdown)
    Kadrlar 2400px kenglikdagi viewport'da, DPR 1 (ustun eng kami 180px bo'lib
    o'lchandi — bandl qiymati bilan bir xil, ya'ni masshtab 1:1).
  · BANDL — Uzum portalining O'Z kodi (mf-products.1.43.36.*.js), HAR ichida.

Piksel bo'yicha o'lchangan (probe skriptlari bilan):
    sahifa foni #f1f1f1 · kartalar oq · kontent x=228..1895
    kirish kartasi 815px keng · karta bilan jadval orasi 24px
    qator balandligi 81px · boshqaruv balandligi 42px · ustki/ostki bo'shliq 20px
    «Tovar» ustuni 188px · ochiq tugma foni #f5f5f5, chegarasi #222226
"""
from __future__ import annotations

import re
from pathlib import Path

FEATURE = Path(__file__).resolve().parent.parent / "noviy_tavar"
JS = FEATURE / "static" / "noviy_tavar.js"
CSS = FEATURE / "static" / "noviy_tavar.css"
TEMPLATE = FEATURE / "templates" / "noviy_tavar.html"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _rule(css: str, selector: str) -> str:
    """Selektorning qoida tanasi. Izohlar OLIB TASHLANADI — ular ichida `}`
    bo'lishi mumkin (masalan `var(--rounding-150, 10px)}`) va sodda kesish
    qoidani yarmidan uzib qo'yardi."""
    clean = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    i = clean.index(selector + " {")
    return clean[i:clean.index("}", i)]


# ── Mantiqiy (boolean) atribut = DROPDOWN, segment tugma EMAS ────────
#
# Bandl `BooleanField` (mf-products @2022149):
#   u-select type:"single", input-variant:"outlined", size:"medium",
#   values: [{label:t("yes"),value:true},{label:t("no"),value:false},
#            {label:"—",value:null}], showClearIcon:false
# Bizda ilgari binafsha «Ha|Yoʻq» segmenti chizilardi — Uzumda bunday
# boshqaruv umuman yo'q.


def test_boolean_attribute_renders_as_a_dropdown():
    js = _read(JS)
    assert "if (s3IsEnum(vt) || vt === 'boolean') {" in js, (
        "mantiqiy atribut enum bilan bir xil combo'ni ishlatishi kerak"
    )


def test_boolean_segmented_control_is_gone():
    js, css = _read(JS), _read(CSS)
    assert "nt-s3-bool-btn" not in js, "eski segment tugma kodi qolib ketdi"
    assert ".nt-s3-bool {" not in css and ".nt-s3-bool-btn" not in css, (
        "eski segment tugma uslublari qolib ketdi"
    )


def test_boolean_dropdown_has_exactly_the_bundle_options():
    """Uchta variant: Ha / Yoʻq / «—» (bo'sh qiymat ham TANLANADIGAN variant)."""
    js = _read(JS)
    assert "[[NT_S3.yes, true], [NT_S3.no, false], [NT_S3.dash, null]]" in js
    assert "dash:        '—'," in js


def test_boolean_empty_shows_dash_not_placeholder():
    js = _read(JS)
    assert "(raw === true) ? NT_S3.yes : (raw === false) ? NT_S3.no : NT_S3.dash" in js


def test_boolean_dropdown_has_no_search_field():
    """Bandl `BooleanField` u-select'ga `filter` propini BERMAYDI."""
    js = _read(JS)
    i = js.index("function s3OpenEnum(")
    body = js[i:i + 1800]
    assert "if (isBool) { s3RenderPop(''); return; }" in body


# ── Ochiq dropdown: filtr TUGMA ichida, panelda qidiruv qatori yo'q ──


def test_filter_input_lives_inside_the_trigger():
    js = _read(JS)
    assert "function s3MountFilter(" in js
    assert "inp.className = 'nt-s3-combo-filter';" in js
    assert "inp.placeholder = NT_S3.valueWord;" in js, (
        "ochiq holat placeholder'i «Qiymat»/«Значение» bo'lishi kerak (bandl `value`)"
    )


def test_panel_has_no_separate_search_row():
    src = _read(TEMPLATE)
    assert 'id="ntS3Search"' not in src, "paneldagi eski qidiruv qatori qolib ketdi"
    assert 'id="ntS3PopList"' in src


def test_open_state_visuals_match_the_reference():
    css = _read(CSS)
    block = css[css.index(".nt-s3-combo.is-open {"):]
    block = block[:block.index("}")]
    assert "background: #f5f5f5" in block      # o'lchangan (470,400)
    assert "border-color: #222226" in block    # o'lchangan (468,395)
    assert "rgba(112, 181, 255" in block       # bandl --border-focus: #70b5ff
    assert ".nt-s3-combo.is-open .nt-s3-combo-caret {" in css
    assert "transform: rotate(180deg)" in css


# ── Jadval o'lchovlari (referens kadrdan) ────────────────────────────


def test_header_row_is_white_and_dark_not_the_grey_band():
    """3-qadam sarlavhasi 2-qadamnikidan boshqa: OQ fon, qora matn, 500."""
    css = _read(CSS)
    i = css.index(".nt-s3-tbl .nt-th {")
    block = css[i:css.index("}", i)]
    assert "background: #fff" in block
    assert "color: var(--nt-ink)" in block
    assert "font-weight: 500" in block
    assert "white-space: normal" in block, "bandl `white-space: wrap` — sarlavha o'raladi"
    assert "min-width: 180px" in block, "bandl `.attribute-header{min-width:180px}`"


def test_required_star_is_red():
    """Bandl `.attribute-header__required{color:red;font-weight:500}`."""
    css = _read(CSS)
    assert '.nt-s3-th .nt-req { color: red; font-weight: 500; }' in css


def test_row_and_control_metrics_match_the_measurements():
    css = _read(CSS)
    assert "[data-page=\"noviy-tavar\"] .nt-s3-td { padding: 20px 12px; }" in css
    combo = _rule(css, ".nt-s3-combo")
    assert "min-height: 42px" in combo       # o'lchangan 394..435
    assert "border-radius: 10px" in combo    # bandl --rounding-150


def test_product_column_is_188px_and_wraps():
    css = _read(CSS)
    assert "width: 188px" in css, "«Tovar» ustuni referensda 230..418 = 188px"
    i = css.index(".nt-s3-tbl .nt-s3-prod-top")
    assert "white-space: normal" in css[i:i + 400], "SKU ikki qatorga o'ralsin"


def test_intro_card_is_narrower_than_the_table():
    css = _read(CSS)
    i = css.index("#ntStep3 > .nt-card {")
    block = css[i:css.index("}", i)]
    assert "max-width: 815px" in block     # o'lchangan 230..1044
    assert "margin-bottom: 24px" in block  # o'lchangan 264..287


def test_options_list_height_matches_the_bundle():
    css = _read(CSS)
    assert "max-height: 244px" in css, "bandl `.u-select__options-list{max-height:244px}`"


# ── Qatordagi xato nishoni (sku-errors-hint) ─────────────────────────


def test_row_error_badge_exists_with_pulse():
    """Bandl `.sku-errors-hint__icon{width:16px;height:16px;color:#e53e3e;
    animation: … 2s ease-in-out infinite}` — referens kadrda har qatorda."""
    css, js = _read(CSS), _read(JS)
    i = css.index(".nt-s3-err {")
    block = css[i:css.index("}", i)]
    assert "width: 16px" in block and "height: 16px" in block
    assert "#e53e3e" in block
    assert "animation: nt-s3-err-pulse" in block
    assert "@keyframes nt-s3-err-pulse" in css
    assert "function s3RowMissing(" in js
    assert "nt-s3-err" in js


def test_error_badge_respects_reduced_motion():
    css = _read(CSS)
    assert re.search(r"prefers-reduced-motion: reduce[^}]*\}\s*[^{]*\.nt-s3-err \{ animation: none",
                     css, re.S) or "animation: none" in css


def test_row_badges_repaint_when_a_value_changes():
    js = _read(JS)
    assert "function s3PaintRowErrors(" in js
    i = js.index("function s3SyncSave(")
    # Funksiya tanasi (keyingi `\n  }` gacha) — izoh uzunligiga bog'lanmaymiz.
    body = js[i:js.index("\n  }", i)]
    assert "s3PaintRowErrors();" in body


# ── Sarlavhadagi tugma 3-qadamda «Yakunlash» ────────────────────────


def test_save_button_says_finish_on_step_three():
    """Bandl umumiy i18n: `finish: "Завершить" / "Yakunlash"`; referens kadrda
    3-qadam tugmasi aynan shunday (1-2 qadamda `save_and_continue` qoladi)."""
    js = _read(JS)
    assert "finish:      tr('Yakunlash', 'Завершить')," in js
    assert "b.textContent = (state.step === 3) ? NT_S3.finish : ntSaveLabelDefault;" in js
    i = js.index("function paintSteps(")
    assert "syncSaveLabel();" in js[i:i + 300], "qadam almashganda yorliq yangilanishi kerak"


# ── Enum yorliqlari xom kod bo'lib qolmasin ──────────────────────────


def test_enum_values_are_fetched_beyond_the_first_page():
    """JONLI XATO (2026-07-22): «Qurilma modeli» 1000 dan ko'p qiymatli —
    2-sahifadagi kodlar yorliqsiz qolib, jadvalda «filter_value_672150»
    ko'rinardi."""
    src = _read(FEATURE / "client.py")
    i = src.index("def attr_enums(")
    body = src[i:i + 2200]
    assert "def _page(" in body
    assert "while len(rows) and len(rows) % int(size) == 0" in body
    assert "if not search:" in body, "qidiruvda Uzum o'zi filtrlaydi — sahifalash shart emas"


# ── «Yakunlash» → do'kon tanlab «Mahsulotlar»ni ochish (2026-07-25) ──


def test_finish_redirects_to_groups_with_shop_selected():
    """FOYDALANUVCHI TALABI 2026-07-25: 3-bosqich «Yakunlash» muvaffaqiyatidan
    keyin sahifada QOLIB KETMASIN — karta yaratilgan do'kon tanlangan holda
    «Mahsulotlar» (`/groups`) ochilsin.

    Do'kon tanlovi barcha sahifalar o'rtasida `sh_shop` cookie orqali
    ulashiladi (uzum_id saqlaydi; `products/routes.py::groups_page` shundan
    o'qiydi). s3Send muvaffaqiyat bloki cookie'ni `state.shop` (uzum_id) ga
    yozib, `/groups`ga o'tishi shart. JONLI TASDIQ: cookie=23059(mixbox),
    final_path=/groups, mixbox tanlangan.
    """
    js = _read(JS)
    i = js.index("function s3Send")
    # Muvaffaqiyat bloki: notify(NT_S3.saved) dan keyingi qism.
    body = js[i:i + 3600]
    j = body.index("notify(NT_S3.saved)")
    tail = body[j:]
    # 1) sh_shop cookie state.shop (uzum_id) ga yoziladi — sales.html bilan bir xil shakl.
    assert "'sh_shop=' + encodeURIComponent(state.shop" in tail, "sh_shop cookie state.shop'ga yozilmayapti"
    assert "path=/" in tail and "SameSite=Lax" in tail
    # 2) /groups'ga o'tadi.
    assert "window.location.assign('/groups')" in tail, "/groups'ga redirect yo'q"
    # 3) faqat MUVAFFAQIYATDA (res.ok tekshiruvidan keyin, ntDirty[3]=false bilan birga).
    assert tail.index("window.location.assign('/groups')") > 0
    assert "ntDirty[3] = false" in body, "toza holat belgilanmagan — beforeunload xalaqit berardi"
