"""«Новый товар» — feature doirasi va shell izolyatsiyasi testlari.

Bu testlar DB'ga tegmaydi (conftest `extensions`ni stub qiladi) — ular fayl
darajasidagi shartnomalarni tekshiradi. Har biri qayta qurish paytida REAL
uchragan xatoni qulflaydi:

1. `t` makro soyasi — base.html `t` ni tarjima obyekti sifatida ishlatadi
   (t.token_expired ...). noviy_tavar.html ichida `{% macro t %}` e'lon qilinsa,
   Jinja merosi orqali u base.html'dagi `t` ni soya qilib butun shell'ni
   buzardi. Bu xato qurish paytida topilib tuzatilgan (nt_t ga o'zgartirildi).

2. CSS sizib chiqishi — prompt talabi: feature CSS boshqa sahifalarga ta'sir
   qilmasin. Har bir qoida [data-page="noviy-tavar"] ostida bo'lishi shart.

3. Uzum'ga to'g'ridan-to'g'ri murojaat — brauzer JS faqat /noviy-tavar/api/*
   proxy'lari orqali gaplashishi kerak (token hech qachon brauzerga chiqmaydi).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FEATURE = ROOT / "noviy_tavar"
TEMPLATE = FEATURE / "templates" / "noviy_tavar.html"
EDITOR = FEATURE / "templates" / "partials" / "nt_editor.html"
CSS = FEATURE / "static" / "noviy_tavar.css"
JS = FEATURE / "static" / "noviy_tavar.js"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _markup(p: Path) -> str:
    """Jinja/HTML izohlarisiz manba.

    Izohlar tekshiriladigan matnni ifloslantiradi: masalan «umumiy Фото товара
    bo'limi YO'Q» degan izohning o'zi «Фото товара» qidiruviga tushib qoladi.
    """
    src = _read(p)
    src = re.sub(r"\{#.*?#\}", "", src, flags=re.S)   # Jinja izohlari
    src = re.sub(r"<!--.*?-->", "", src, flags=re.S)  # HTML izohlari
    return src


# ── 1. base.html shell'i buzilmasin ──────────────────────────────────


def test_template_does_not_shadow_base_translation_object():
    """`{% macro t(...) %}` base.html'dagi `t` tarjima obyektini soya qiladi.

    base.html `t.token_expired` kabi murojaatlar qiladi; agar bu template
    top-level `t` e'lon qilsa, meros kontekstida shell buziladi.
    """
    src = _read(TEMPLATE)
    assert not re.search(r"\{%-?\s*macro\s+t\s*\(", src), (
        "`t` nomli makro base.html tarjima obyektini soya qiladi — `nt_t` ishlating"
    )
    assert not re.search(r"\{%-?\s*set\s+t\s*=", src), (
        "`t` ga qiymat berish base.html shell'ini buzadi"
    )


def test_template_extends_base_and_does_not_edit_it():
    src = _read(TEMPLATE)
    assert '{% extends "base.html" %}' in src
    # base.html'ning O'ZI o'zgarmaganini alohida test qiladi (pastda).


def test_base_html_untouched_by_feature():
    """base.html shell'i feature kodidan xoli qolishi kerak.

    Eslatma: bu yerda `"nt-" not in base` deb tekshirib bo'lmaydi — base.html'da
    `content-`, `font-`, `align-items` kabi so'zlarda «nt-» tabiiy uchraydi.
    Shuning uchun feature'ning aniq prefikslari tekshiriladi.
    """
    base = _read(ROOT / "templates" / "base.html")
    assert "noviy-tavar" not in base, "base.html feature'ga xos kod olmasligi kerak"
    assert 'class="nt-' not in base
    assert "noviy_tavar" not in base


def test_global_css_untouched_by_feature():
    styles = _read(ROOT / "static" / "styles.css")
    assert "noviy-tavar" not in styles, "global styles.css'ga feature CSS sizib chiqqan"


# ── 2. CSS doirasi ───────────────────────────────────────────────────


def _css_rule_selectors(css: str) -> list[str]:
    """Izohlar/at-qoidalarsiz top-level selektorlarni ajratish."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    # @media/@supports bloklarining ichini tekshirish uchun ularni ochamiz
    css = re.sub(r"@(?:media|supports)[^{]*\{", "", css)
    out: list[str] = []
    for m in re.finditer(r"([^{}]+)\{", css):
        sel = m.group(1).strip()
        if not sel or sel.startswith("@"):
            continue
        for part in sel.split(","):
            part = part.strip()
            if part:
                out.append(part)
    return out


def test_every_css_rule_is_scoped_to_the_feature_root():
    """Har bir selektor [data-page="noviy-tavar"] bilan boshlanishi shart."""
    leaks = [
        s for s in _css_rule_selectors(_read(CSS))
        if not s.startswith('[data-page="noviy-tavar"]')
    ]
    assert leaks == [], f"doiradan chiqqan selektorlar: {leaks}"


def test_css_defines_measured_tokens():
    """Token qatlami referensdan o'lchangan qiymatlarni saqlaydi."""
    css = _read(CSS)
    # Frame 02 (1920x912) dan o'lchangan qiymatlar.
    for token, value in {
        "--nt-col-w": "1064px",     # karta x=530..1593
        "--nt-card-gap": "10px",    # kartalar orasidagi fon chizig'i
        "--nt-header-h": "104px",   # header y=0..103
        "--nt-bg": "#f1f1f1",
        "--nt-card-bg": "#ffffff",
        "--nt-control-h": "40px",   # input/tugma balandligi
    }.items():
        assert f"{token}: {value}" in css, f"{token} o'lchangan qiymatdan chetlashgan"


def test_card_radius_is_zero_as_measured():
    """O'LCHANGAN: referens kartalari o'tkir burchakli (radius 0), yumaloq emas."""
    assert "--nt-radius-card: 0" in _read(CSS)


def test_reduced_motion_supported():
    assert "prefers-reduced-motion" in _read(CSS)


# ── 3. Xavfsizlik chegarasi ──────────────────────────────────────────


def test_browser_js_never_calls_uzum_directly():
    """Brauzer faqat Flask proxy'si bilan gaplashadi — Uzum bilan emas."""
    js = _read(JS)
    for host in ("api-seller.uzum.uz", "images-uploader.uzum.uz", "uzum.uz"):
        assert host not in js, f"brauzer JS to'g'ridan-to'g'ri {host} ga murojaat qilyapti"
    assert "/noviy-tavar/api/" in js, "proxy prefiksi ishlatilmagan"


def test_browser_js_contains_no_credentials():
    js = _read(JS)
    assert not re.search(r"eyJ[A-Za-z0-9_-]{20,}", js), "JS ichida qotirilgan JWT"
    assert not re.search(r"Bearer\s+[A-Za-z0-9._-]{20,}", js), "JS ichida qotirilgan token"


def test_page_route_still_requires_login():
    """Sahifa va proxy'lar @login_required ostida qolishi kerak."""
    src = _read(FEATURE / "routes.py")
    # har bir route dekoratoridan keyin login_required kelishi kerak
    routes = re.findall(
        r"@noviy_tavar_bp\.(?:get|post|route)\([^)]*\)\s*\n\s*@(\w+)", src
    )
    assert routes, "route topilmadi"
    assert all(d == "login_required" for d in routes), (
        f"himoyasiz route bor: {routes}"
    )


def test_ownership_guard_still_present():
    src = _read(FEATURE / "routes.py")
    assert "_can_access" in src, "do'kon-egalik guard'i olib tashlangan"
    assert "_shop_or_403" in src


# ── 4. Referens tuzilishi ────────────────────────────────────────────


def test_sections_follow_measured_frame_order():
    """Bo'lim tartibi kadrlardan o'lchangan haqiqiy tartibga mos.

    DIQQAT: promptdagi «01..08 yuqoridan pastga» da'vosi XATO edi — kadrlarni
    o'lchab, haqiqiy tartib 02→03→04→05→06→07→08 ekani aniqlandi (01 = 07 ning
    takrori). Bo'lim ketma-ketligi shu o'lchovdan olingan.
    """
    src = _markup(TEMPLATE)
    order = [
        "Категория товара",
        "Название товара",
        "Краткое описание товара",
        "Описание товара",
        "Видео",
        "Фото 360",
        "Характеристики товара",
        "Размерная сетка",
        "Состав",
        "Инструкция по уходу",
        "Сертификаты",
    ]
    positions = [src.find(s) for s in order]
    missing = [s for s, p in zip(order, positions) if p == -1]
    assert not missing, f"bo'lim yo'q: {missing}"
    assert positions == sorted(positions), "bo'lim tartibi o'lchangan tartibga mos emas"


def test_blank_form_has_no_general_photo_section():
    """O'LCHANGAN (frame 05): bo'sh formada umumiy «Фото товара» bo'limi YO'Q —
    Описание'dan keyin to'g'ridan-to'g'ri Видео keladi. Rasm yuklash faqat rang
    xususiyati tanlangach «Медиафайлы для каждого цвета» sifatida chiqadi.
    """
    src = _markup(TEMPLATE)
    assert "Фото товара" not in src, (
        "bo'sh formada umumiy foto bo'limi bo'lmasligi kerak (frame 05 dalili)"
    )


def test_save_button_starts_disabled():
    """Referens: bo'sh formada «Сохранить и продолжить» o'chiq."""
    src = _read(TEMPLATE)
    m = re.search(r'id="ntSave"[^>]*', src)
    assert m and "disabled" in m.group(0), "bo'sh formada Saqlash tugmasi o'chiq bo'lishi kerak"


def test_characteristics_disabled_until_category_chosen():
    """Frame 06: kategoriya tanlanmaguncha «Добавить характеристику» o'chiq.

    Bosqich A'da `<select>` combobox tugmasiga almashtirildi — referens
    (012_..._characteristic-dropdown-open.png) native select emas, maxsus
    panel ko'rsatadi.
    """
    src = _read(TEMPLATE)
    m = re.search(r'id="ntCharBtn"[^>]*', src)
    assert m and "disabled" in m.group(0)
    assert "только после выбора категории" in src, "sariq ogohlantirish yo'q"


@pytest.mark.parametrize("path", [TEMPLATE, EDITOR, CSS, JS])
def test_feature_files_are_valid_utf8_without_mojibake(path: Path):
    """Prompt talabi: mojibake artefaktlarini tuzatish."""
    text = path.read_text(encoding="utf-8")
    for bad in ("Ð", "Ñ", "â€", "Ð¾", "ï»¿"):
        assert bad not in text, f"{path.name} ichida mojibake: {bad!r}"


# ── BOSQICH A — kategoriya kaskadi, xususiyat qatorlari, qiymat modali ──
#
# Har bir test REAL uchragan xatoni yoki o'lchangan shartnomani qulflaydi.


def test_category_control_is_cascade_not_search():
    """DALIL: Uzum'da kategoriya QIDIRUV endpointi YO'Q.

    Ikkala HAR dasturiy skanerlandi (har/categories/001 va 2712-yozuvli
    uzum-sacvoyage-product-flow.har) — faqat rootCategories + childCategories
    chiqdi. Ya'ni bu drill-down kaskad; terilgan matn faqat ko'rinib turgan
    darajani filtrlaydi. Agar kimdir «qidiruv» endpointini o'ylab topib
    qo'shsa, shu test uni ushlaydi.
    """
    js = _read(JS)
    assert "/noviy-tavar/api/categories" in js
    for invented in ("/api/category-search", "searchCategories", "/search?"):
        assert invented not in js, (
            f"{invented!r} — Uzum'da bunday kategoriya qidiruvi YO'Q (HAR bilan tekshirilgan)"
        )
    # ⚠️ `&search=` ni butun JS bo'yicha taqiqlab bo'lmaydi — u FILTR
    # qiymatlari uchun QONUNIY (HAR: filters/product/values?...&search=...).
    # Taqiq faqat KATEGORIYA chaqiruviga tegishli, shuning uchun aynan shu
    # URL quriladigan joy tekshiriladi.
    cat_call = js[js.index("'/noviy-tavar/api/categories?shop='"):]
    cat_call = cat_call[:cat_call.index("credentials")]
    assert "search" not in cat_call, (
        "kategoriya chaqiruviga qidiruv parametri qo'shilgan — Uzum'da bunday endpoint yo'q"
    )


def test_category_panel_does_not_filter_by_active_flag():
    """REGRESSIYA QULFI — jonli javob bilan isbotlangan xato.

    /api/categories?shop=10945 → 22 ta ildizning HAMMASI `active:false`
    (`canUse:true`, `hasChildren:true`). `active===false` bo'yicha filtrlash
    butun ro'yxatni bo'sh qoldiradi — bu aynan sodir bo'lgan va tuzatilgan.
    """
    js = _read(JS)
    assert "c.active === false" not in js, (
        "`active` bo'yicha filtrlamang: jonli javobda hamma ildiz active:false"
    )


def test_category_cascade_uses_parent_id_drilldown():
    js = _read(JS)
    assert "parentId=" in js, "bolalar darajasi parentId bilan olinadi"
    assert "hasChildren" in js, "barg va tugun farqlanishi kerak"


def test_accept_button_loads_category_meta():
    """«Принять» → /api/category-meta → forma ochiladi."""
    js = _read(JS)
    assert "/noviy-tavar/api/category-meta" in js
    assert "state.categoryId = leaf.id" in js, (
        "kategoriya faqat «Принять» bosilgach TASDIQLANADI (barg bo'yicha)"
    )


# ── Kategoriya oqimi: USTMA-UST select'lar (Uzum bilan bir xil) ──────
#
# ⚠️ Birinchi urinishda drill-down panel qilingan edi («⟵ Orqaga» bilan bitta
# kontrol ichida pastga tushish) — foydalanuvchi jonli Uzum kadrlarini
# ko'rsatdi va bu XATO ekani aniqlandi. Quyidagi testlar to'g'ri oqimni
# qulflaydi, shunda u yana drill-down'ga qaytib ketmasin.


def test_category_uses_stacked_selects_not_drilldown():
    """Har daraja O'Z select'ini oladi; «⟵ Orqaga» bilan tushish YO'Q."""
    js = _read(JS)
    assert "cat.levels" in js, "darajalar ro'yxati bo'lishi kerak"
    assert "renderLevels" in js
    for gone in ("cat.stack", "nt-panel-back", "openCatPanel", "loadCatLevel"):
        assert gone not in js, f"{gone!r} — eski drill-down qoldig'i (Uzum'da unday emas)"


def test_second_level_placeholder_matches_reference():
    """Referens: 1-select «Название категории или товара», keyingilari
    «Выбрать подкатегорию»."""
    js = _read(JS)
    assert "Выбрать подкатегорию" in js
    assert "Название категории или товара" in js


def test_confirmed_state_shows_breadcrumb_and_change_button():
    """DALIL: sacvoyage-filled-step1-product-card.png — tasdiqlangach
    select'lar o'rniga «A › B › C» breadcrumb; jonli kadrda «Изменить»."""
    src = _markup(TEMPLATE)
    assert 'id="ntCrumbs"' in src
    assert 'id="ntCatChange"' in src
    assert "Изменить" in src
    js = _read(JS)
    assert "'›'" in js, "breadcrumb ajratuvchisi referensda `›`"


def test_cancel_button_only_in_edit_mode():
    """Jonli kadr dalili: «Отмена» faqat «Изменить» bosilgach chiqadi —
    birinchi tanlashda yo'q."""
    src = _markup(TEMPLATE)
    m = re.search(r'id="ntCatCancel"[^>]*', src)
    assert m and "hidden" in m.group(0), "boshida «Отмена» yashirin bo'lishi kerak"
    js = _read(JS)
    assert "cat.editing" in js
    assert "catCancel.hidden = !cat.editing" in js


def test_accept_is_violet_when_enabled():
    """O'LCHANGAN: faol «Принять» = #7000FF (modal «Сохранить» bilan bir xil);
    o'chiq holatda kulrang (blank-step1-category-typed.png: #E6E5EA)."""
    css = _read(CSS)
    assert ".nt-btn--accept:not(:disabled)" in css
    assert "--nt-violet-solid: #7000ff" in css


def test_required_fields_legend_present():
    """Referens kadrlarda kartalar ustida «* - обязательные поля» bor."""
    src = _markup(TEMPLATE)
    assert "обязательные поля" in src


def test_category_list_is_alphabetically_sorted():
    """Jonli kadr: Автотовары, Аксессуары, Бытовая техника... — alifbo bo'yicha.

    API tartibi saralanmagan (Hayvonlar, Mebel, Turizm...), shuning uchun
    client-side saralash kerak.
    """
    js = _read(JS)
    assert "localeCompare" in js, "ro'yxat alifbo bo'yicha saralanishi kerak"


def test_fetch_error_is_not_silently_swallowed():
    """REGRESSIYA QULFI — jonli topilgan nuqson.

    Uzum token o'lganda 401 qaytaradi (probe: «unauthorized-001»), proxy 502
    beradi. Avval JS `{categories: []}` ga tushib panelda «Нет подходящей
    категории» ko'rsatardi — foydalanuvchi «kategoriya yo'q ekan» deb YOLG'ON
    xulosa chiqarardi. Endi xato va bo'sh ro'yxat farqlanadi.
    """
    js = _read(JS)
    assert "{ categories: [] }" not in js, (
        "xato holatida bo'sh ro'yxatga tushmang — bu yolg'on xabar beradi"
    )
    assert "error: 'fetch'" in js, "xato holati belgilanishi kerak"
    assert "Не удалось загрузить категории" in js, "xato xabari yo'q"


def test_failed_fetch_is_not_cached():
    """Xato keshlanmasin — aks holda «Qayta urinish» hech qachon tuzatmaydi."""
    js = _read(JS)
    i = js.index("cat.cache[key] = list;")
    assert "faqat MUVAFFAQIYAT keshlanadi" in js[i:i + 120], (
        "kesh faqat muvaffaqiyatli javobda to'lishi kerak"
    )


def test_error_panel_offers_retry():
    js = _read(JS)
    assert "nt-panel-retry" in js and "Повторить" in js
    assert "nt-panel-retry" in _read(CSS)


def test_read_calls_pass_page_language_to_portal():
    """Kategoriya `title` — TEKIS string; portal uni Accept-Language bo'yicha
    tarjima qiladi. `uz-UZ` qotirilgan bo'lsa RU sahifada ham «Hayvonlar»
    chiqadi (Uzum'da «Животные») — foydalanuvchi shu farqni ko'rsatdi.

    Jonli tasdiq (konteynerdan): lang='uz' → ['Hayvonlar', 'Mebel', ...],
    lang='ru' → ['Животные', 'Мебель', 'Туризм, рыбалка и охота'].
    """
    routes = _read(FEATURE / "routes.py")
    assert "def _lang()" in routes
    for call in ("root_categories(shop, lang=_lang())",
                 "category_meta(shop, cid, lang=_lang())"):
        assert call in routes, f"o'qish chaqiruvi tilni uzatmayapti: {call}"


def test_locale_map_only_contains_probed_values():
    """⚠️ Faqat JONLI tekshirilgan locale'lar bo'lsin.

    Probe (konteynerdan, xom header bilan): 'ru' → 200 «Животные»,
    'ru-RU' → 200, 'ru-RU,ru;q=0.9' → 200, LEKIN 'ru_RU' (pastki chiziq)
    → HTTP 500 internal-server-error-001. Global en-US ham 500 (client.py
    izohi, 2026-07-11 probe). Xaritaga tekshirilmagan qiymat qo'shmang.
    """
    c = _read(FEATURE / "client.py")
    assert '_LOCALE_BY_LANG = {"uz": "uz-UZ", "ru": "ru"}' in c
    assert "ru_RU" not in c, "ru_RU portalda 500 beradi (jonli tekshirilgan)"
    assert "en-US" not in c.split("_LOCALE_BY_LANG")[1][:200]


def test_unknown_language_falls_back_to_uzbek_default():
    """Noma'lum til kelsa uz-UZ (o'zbekcha-birinchi konvensiya saqlanadi)."""
    c = _read(FEATURE / "client.py")
    assert "_LOCALE_BY_LANG.get(" in c and "_PRODUCT_ACCEPT_LANGUAGE)" in c


def test_write_calls_keep_uzbek_default():
    """YOZISH (create) chaqiruvi tilga bog'lanmaydi — uz-UZ default'da qoladi."""
    routes = _read(FEATURE / "routes.py")
    m = re.search(r"def nt_create\(\).*?(?=\n@|\Z)", routes, flags=re.S)
    assert m, "nt_create topilmadi"
    assert "lang=_lang()" not in m.group(0), (
        "mahsulot yaratish chaqiruvi sahifa tiliga bog'lanmasin"
    )


def test_open_level_guards_against_focus_recursion():
    """REGRESSIYA QULFI — real xato «Maximum call stack size exceeded».

    openLevel() → renderLevels() → input.focus() → focus handleri →
    openLevel() ... cheksiz halqa. Ochiq darajani qayta ochmaslik shart.
    """
    js = _read(JS)
    assert "if (!cat.levels[i] || cat.levels[i].open) return;" in js, (
        "openLevel() ochiq darajada darrov qaytishi kerak — aks holda rekursiya"
    )


def test_required_type_rule_matches_evidence():
    """DALIL (HANDOFF §2.3 + jonli meta): REQUIRED → qator avto-render;

    REQUIRED_ONE_OF_SIZE (1453 ta) → render BO'LMAYDI. 235 rankdan 0 ziddiyat.
    """
    js = _read(JS)
    assert "requiredType === 'REQUIRED'" in js, "REQUIRED qoidasi yo'q"
    # DIQQAT: xom matnda qidirmang — izohning O'ZI («REQUIRED_ONE_OF_SIZE →
    # render BO'LMAYDI») tekshiruvga tushadi va soxta yiqiladi. Bu tuzoqqa
    # bir marta tushilgan. Shuning uchun KODdagi solishtirishni tekshiramiz.
    assert "'REQUIRED_ONE_OF_SIZE'" not in js, (
        "REQUIRED_ONE_OF_SIZE qator RENDER QILMAYDI — uni majburiy deb hisoblamang"
    )


def test_char_row_has_add_and_delete_controls():
    """Referens 001_category-baseline-viewport.png: qatorda «+ Добавить» va «Удалить»."""
    css = _read(CSS)
    for cls in (".nt-btn--add", ".nt-btn--del", ".nt-char-row"):
        assert cls in css, f"{cls} yo'q"
    js = _read(JS)
    assert "removeRow" in js and "openValueModal" in js


def test_value_modal_exists_and_is_modal_not_dropdown():
    """DALIL: 005_-1_Цвет_value-dropdown-open.png — fayl nomida «dropdown»,

    lekin kadrda MODAL: sarlavha «Выбрать характеристики», Поиск, checkbox
    ro'yxati, «Отмена»/«Сохранить». Nomga aldanmang.
    """
    src = _markup(TEMPLATE)
    assert 'id="ntValueModal"' in src
    assert 'role="dialog"' in src and 'aria-modal="true"' in src
    assert 'id="ntValueSearch"' in src, "modalda Поиск maydoni bor"
    assert 'id="ntValueSave"' in src and 'id="ntValueCancel"' in src


def test_value_modal_is_hidden_in_blank_state():
    src = _markup(TEMPLATE)
    m = re.search(r'id="ntValueModal"[^>]*', src)
    assert m and "hidden" in m.group(0), "bo'sh holatda modal yopiq"


def test_modal_measured_geometry_is_encoded():
    """O'LCHANGAN qiymatlar (taxmin emas) CSS'da qulflangan.

    Manba: 005_-1_Цвет_value-dropdown-open.png (1920x1080)
      - modal x=640..1279 -> 640px, markazda
      - overlay alfasi IKKI xil fonda: 0.302 (oq karta) va 0.303 (#F1F1F1)
      - rang doirasi 30x30
      - «Сохранить» foni #7000FF (mediana)
    Implementatsiya jonli o'lchandi: modal 640..1279 (aynan), doira 30px,
    tugma #7000FF, overlay karta ustida #B2B2B2 = referens bilan bir xil.
    """
    css = _read(CSS)
    assert "--nt-modal-w: 640px" in css
    assert "rgba(0, 0, 0, .30)" in css
    assert "--nt-swatch: 30px" in css
    assert "--nt-violet-solid: #7000ff" in css


def test_panel_measured_geometry_is_encoded():
    """O'LCHANGAN: 012_..._characteristic-dropdown-open.png — element qadami 44px."""
    css = _read(CSS)
    assert "--nt-panel-item-h: 44px" in css


def test_combo_class_does_not_hardcode_width():
    """REGRESSIYA QULFI — real xato.

    `.nt-combo { width: 100% }` `.nt-select-wrap` ning 398px'ini bosib ketdi
    va kategoriya kontroli kartaning butun enini egalladi. Kenglikni
    chaqiruvchi beradi.
    """
    css = _read(CSS)
    m = re.search(r'\.nt-combo \{([^}]*)\}', css)
    assert m, ".nt-combo qoidasi yo'q"
    assert "width" not in m.group(1), (
        ".nt-combo kenglik bermasin — .nt-select-wrap(398px) ni bosib ketadi"
    )


def test_color_swatch_only_for_hex_values():
    """DALIL: 005 (rang) doira BILAN, 019 (o'lcham) doira SIZ — modal bir xil.

    Farq qiymatning o'zida: rangda `value` HEX bo'ladi.
    """
    js = _read(JS)
    assert "isHex" in js, "doira faqat HEX qiymatda chiqishi kerak"
    assert "nt-opt-swatch" in js


def test_value_modal_draft_is_not_applied_until_save():
    """«Сохранить» bosilmaguncha tanlov qatorga o'tmaydi (Отмена — bekor qiladi)."""
    js = _read(JS)
    assert "modalDraft" in js
    assert "modalRow.selected = modalDraft.slice()" in js, (
        "tanlov faqat «Saqlash» bosilganda qo'llanadi"
    )


def test_modal_supports_escape_and_focus_trap():
    """Prompt talabi: Escape, fokus ko'rinishi, tab tartibi."""
    js = _read(JS)
    assert "'Escape'" in js
    assert "e.key !== 'Tab'" in js, "modalda fokus tuzog'i bo'lishi kerak"


def test_max_five_characteristics_matches_reference_heading():
    """Referens sarlavhasi: «Характеристики товара (Максимум 5)»."""
    js = _read(JS)
    assert "state.rows.length >= 5" in js, "5 ta limit yo'q"


def test_stage_a_still_routes_through_proxy_only():
    """Uzum tokeni brauzerga CHIQMAYDI — hamma chaqiruv /noviy-tavar/api/*."""
    js = _read(JS)
    for host in ("api-seller.uzum.uz", "images-uploader.uzum.uz", "api.uzum.uz"):
        assert host not in js, f"brauzer JS {host} ga to'g'ridan-to'g'ri murojaat qilmasin"


# ── BOSQICH B: render qoidasi 235 jonli sxemaga qarshi ───────────────
#
# Fikstura `tests/fixtures/noviy_tavar_category_schemas.json` — Uzum'ning
# 235 ta NOYOB kategoriya sxemasi (getDefinedCharacteristics javoblaridan
# tozalab olingan; artifacts/ gitignore'da bo'lgani uchun nusxa kerak).
#
# Bosqich B'da jonli referens bilan ISBOTLANGANI:
#   • 234/234 kategoriyada Uzum dropdown'i ko'rsatgan har bir xususiyat shu
#     javobda bor — ya'ni variantlar manbai to'liq;
#   • `requiredType === 'REQUIRED'` → qator avto-render (rank 001 baseline
#     kadri bilan ko'z bilan tasdiqlangan: «Rang / Цвет» + Добавить/Удалить);
#   • butun katalogda YAGONA REQUIRED xususiyat — «Цвет», kategoriyaga 1 ta.
#
# Ikkita «nomuvofiqlik» tekshirilib rad etilgan (bizning xato emas):
#   rank 113 — yozib oluvchi skript «Формат» so'zini «Фото 360» kartasidan
#              topib adashgan (baseline kadr: qator YO'Q);
#   rank 229 — redirect drift (13882→2607), Uzum'ning o'zi qoidani bajarmagan.
#
# Uzum shaklni o'zgartirsa — fiksturani qayta generatsiya qiling; quyidagi
# testlar farqni ko'rsatadi.

FIXTURE = ROOT / "tests" / "fixtures" / "noviy_tavar_category_schemas.json"

# JS aynan shu maydon nomlarini o'qiydi (noviy_tavar.js::buildCharOptions).
_JS_CHAR_FIELDS = ("characteristicId", "characteristicTitle", "characteristicValues",
                   "requiredType")
_JS_VALUE_FIELDS = ("skuValue", "title", "value")


@pytest.fixture(scope="module")
def schemas() -> dict:
    import json
    assert FIXTURE.exists(), (
        f"Bosqich B fiksturasi yo'q: {FIXTURE}. artifacts/ gitignore'da — "
        "fikstura repoda saqlanishi SHART."
    )
    return json.loads(_read(FIXTURE))


def test_fixture_carries_no_secrets():
    """HAR'lar sanitizatsiya qilinmagan — fiksturaga sir sizib o'tmasin.

    Faqat kategoriya metama'lumoti ko'chirilgan: token/cookie/header/URL/shopId
    EMAS. Bu test shu chegarani qulflaydi.
    """
    raw = _read(FIXTURE)
    body = raw[raw.index('"contract"'):]        # o'z izohlarimizni hisobga olmaymiz
    for bad in ("authorization", "cookie", "bearer", "set-cookie", "sessionid",
                "http://", "https://", "uzum.uz", "shopId", "sellerId"):
        assert bad.lower() not in body.lower(), f"fiksturada sir bo'lishi mumkin: {bad!r}"


def test_fixture_covers_all_235_unique_schemas(schemas):
    """235 noyob sxema — audit ham, HAR ham shu songa keladi."""
    assert schemas["observed"]["categories"] == 235
    assert len(schemas["categories"]) == 235


def test_required_type_enum_is_exhaustive(schemas):
    """Uzum'da atigi 3 xil requiredType bor — noma'lum 4-chisi paydo bo'lmasin.

    Agar Uzum yangi qiymat qo'shsa, fikstura qayta generatsiya qilinganda shu
    test yiqiladi va biz UI qoidasini ongli ravishda qayta ko'rib chiqamiz.
    """
    assert set(schemas["contract"]["requiredTypes"]) == {
        "REQUIRED", "NOT_REQUIRED", "REQUIRED_ONE_OF_SIZE"
    }
    seen = {c["requiredType"] for cat in schemas["categories"] for c in cat["characteristics"]}
    assert seen <= {"REQUIRED", "NOT_REQUIRED", "REQUIRED_ONE_OF_SIZE"}


def test_js_auto_renders_exactly_the_required_type(schemas):
    """Bizning qoida: `REQUIRED` → avto-qator; qolgan ikkisi → dropdown'da.

    ⚠️ Bu qiyoslash KOD SHAKLIDA qidiriladi (tirnoq bilan) — izohdagi so'z
    testni yolg'ondan o'tkazib yubormasin.
    """
    js = _read(JS)
    assert "c.requiredType === 'REQUIRED'" in js, "avto-render qoidasi o'zgargan"
    # REQUIRED_ONE_OF_SIZE alohida shart sifatida TEKSHIRILMAYDI — u oddiy
    # ixtiyoriy variant (jonli: dropdown'dan qo'shiladi, avto chiqmaydi).
    assert "'REQUIRED_ONE_OF_SIZE'" not in js, (
        "ONE_OF_SIZE uchun maxsus shart yo'q edi — jonli referens buni tasdiqlagan"
    )


def test_auto_rendered_rows_never_hit_the_five_row_cap(schemas):
    """«Максимум 5» chegarasi avto-qator bilan hech qachon to'qnashmasin.

    Jonli: kategoriyada eng ko'pi 1 ta REQUIRED. Agar Uzum buni 6 ga
    ko'tarsa — avto-qo'shish jim rad etilib, majburiy qator TUSHIB QOLARDI.
    """
    js = _read(JS)
    assert "state.rows.length >= 5" in js
    worst = max(
        (sum(1 for c in cat["characteristics"] if c["requiredType"] == "REQUIRED")
         for cat in schemas["categories"]),
        default=0,
    )
    assert worst == schemas["observed"]["maxRequiredPerCategory"] == 1
    assert worst <= 5, "avto-qatorlar 5 ta chegarasidan oshib ketadi"


def test_only_color_is_required_in_the_whole_catalog(schemas):
    """Butun katalogda yagona majburiy xususiyat — «Цвет».

    Referens (rank 001 baseline) aynan «Rang / Цвет» qatorini avto-chiqargan.
    Yangi majburiy xususiyat paydo bo'lsa — UI'ni qayta ko'rish kerak.
    """
    assert schemas["observed"]["requiredTitlesRu"] == ["Цвет"]
    js = _read(JS)
    assert "'Rang / Цвет'" not in js, "nom qotirilmasin — meta'dan kelsin"


def test_js_reads_the_field_names_uzum_actually_sends(schemas):
    """Xom maydon nomlari shartnomasi — 697 xususiyat / 33 441 qiymatda bir xil."""
    js = _read(JS)
    for field in _JS_CHAR_FIELDS:
        assert field in schemas["contract"]["characteristicKeys"], (
            f"Uzum endi {field!r} yubormaydi — JS'ni moslash kerak"
        )
        assert field in js, f"JS {field!r} ni o'qimayapti"
    for field in _JS_VALUE_FIELDS:
        assert field in schemas["contract"]["valueKeys"], (
            f"Uzum qiymat obyektida endi {field!r} yubormaydi"
        )
    assert set(schemas["contract"]["titleKeys"]) == {"ru", "uz"}, (
        "xususiyat nomi {ru,uz} dict bo'lib qoladi (kategoriya title'idan farqli — "
        "u tekis string va Accept-Language bilan tarjima qilinadi)"
    )


def test_every_characteristic_has_the_fields_our_row_needs(schemas):
    """Har xususiyatda id + ikki tilli nom bor — rowLabel «uz / ru» chiqara oladi."""
    for cat in schemas["categories"]:
        for c in cat["characteristics"]:
            cid = cat["categoryId"]
            assert isinstance(c["id"], int), f"{cid}: characteristicId butun son emas"
            assert c["uz"] and c["ru"], f"{cid}: {c['id']} — ikki tilli nom yetishmayapti"
            assert c["valueCount"] > 0, f"{cid}: {c['id']} — qiymatsiz xususiyat"


def test_categories_with_no_characteristics_have_an_empty_state(schemas):
    """Xususiyati umuman yo'q kategoriyalar bor — panel yolg'on gapirmasin."""
    assert schemas["observed"]["categoriesWithZeroCharacteristics"], (
        "fiksturada xususiyatsiz kategoriya yo'q — bu holat sinovdan chiqib ketdi"
    )
    js = _read(JS)
    # ⚠️ `nt-panel-empty` sinfini qidirish YETARLI EMAS — u kategoriya panelida
    # ham ishlatiladi, ya'ni xususiyat paneli bo'sh holatsiz qolsa ham test
    # o'tib ketardi (mutatsiya sinovi shuni ko'rsatdi). Shuning uchun aynan
    # xususiyat panelining matni qulflanadi.
    assert "Других характеристик нет" in js, (
        "xususiyat panelida bo'sh holat matni yo'q"
    )


def test_options_are_built_from_characteristics_not_the_required_endpoint(schemas):
    """Variantlar `getDefinedCharacteristics` dan qurilsin.

    `required-characteristics` endpointi ham xususiyat qaytaradi, lekin
    QIYMATSIZ (1526/1526) — undan qursak qiymat modali bo'm-bo'sh chiqardi.
    REQUIRED to'plami esa ikkisida bir xil (5078/5078 barg), shuning uchun
    o'sha endpointni umuman o'qimaslik xavfsiz.
    """
    js = _read(JS)
    assert "meta.characteristics" in js
    assert "requiredCharacteristics" not in js, (
        "qiymatsiz endpointdan variant qurilmasin"
    )


# ── BOSQICH C1: ixtiyoriy bo'limlar (Добавить -> ikki tilli muharrir -> Удалить) ──
#
# DALIL MANBAI: Uzum portalining O'Z kodi (HAR ichidagi chunk-3ca9d890 /
# chunk-6dbbb9d8 bandllari, `editProductCard/Comments` store). HAR'larda hech
# bir createProduct tanasi bu maydonlarni to'ldirmagan — ya'ni tarmoq yozuvidan
# xarita chiqarib bo'lmasdi. Bandl aniq ko'rsatdi:
#     addComment(tur)         -> {comment:{ru:"",uz:""}, commentType:tur}
#     removeComment(tur, til) -> faqat o'sha tilni o'chiradi; ikkalasi ketsa splice
#     «Добавить» faqat comments[tur] YO'Q bo'lganda ko'rinadi
# Jonli tekshirildi (uicheck): qo'shish -> uz o'chirish -> ru o'chirish ->
# «Добавить» qaytdi.

# ⚠️ API kalitlari UI yorlig'idan FARQ QILADI — eng qimmat topilma:
_COMMENT_TYPES = {
    "Состав": "Состав",                      # yorliq = kalit
    "Размеры": "Размерная сетка",            # yorliq «Размерная сетка», kalit «Размеры»
    "Инструкция": "Инструкция по уходу и эксплуатации",
}
# ⚠️ «Сертификация» ATAYLAB bu ro'yxatda YO'Q. U client.py da qo'llab-quvvatlanadi
# (mavjud tovarni tahrirlash yo'li uchun), lekin YARATISH formasida muharrir
# sifatida KO'RSATILMAYDI — kadr 08 da Сертификаты kartasida faqat «＋ Добавить»
# (productCertificates formasi) bor, muharrir toggle'i yo'q. Bandl ham muharrirni
# faqat `comments.Сертификация` MAVJUD bo'lsa render qiladi.
# Qarang: test_certificates_card_is_a_form_not_a_comment_editor


def test_optional_sections_use_the_comment_types_uzum_actually_sends():
    """`data-nt-opt` da API kaliti tursin, UI yorlig'i EMAS.

    Bu ikkisini adashtirish jim xatoga olib boradi: forma to'g'ri ko'rinadi,
    lekin Uzum matnni qabul qilmaydi.
    """
    html = _markup(TEMPLATE)
    # Kalit `nt_optional(...)` CHAQIRUVIDA turadi — shablonda literal
    # `data-nt-opt="Состав"` yo'q, u makro ichida `{{ ctype }}` bo'lib chiqadi.
    assert 'data-nt-opt="{{ ctype }}"' in html, "makro kalitni chiqarmayapti"
    for ctype in _COMMENT_TYPES:
        assert f"nt_optional('{ctype}'," in html, f"«{ctype}» bo'limi yo'q"
    # UI yorliqlari kalit sifatida ISHLATILMASIN
    for wrong in ("nt_optional('Размерная сетка'", "nt_optional('Сертификаты'"):
        assert wrong not in html, f"UI yorlig'i API kaliti sifatida ishlatilgan: {wrong}"


def test_optional_add_button_hides_once_the_comment_exists(  ):
    """Uzum: «Добавить» faqat comments[tur] yo'q bo'lganda ko'rinadi."""
    js = _read(JS)
    assert "addWrap.hidden = !!st" in js, "«Qo'shish» tugmasi yashirilmayapti"
    assert "if (comments[ctype]) return;" in js, "mavjud comment qayta qo'shilmasin"


def test_optional_delete_is_per_language():
    """Har til alohida o'chiriladi — Uzum'da `removeComment(tur, UZ|RU)`.

    Bitta umumiy «Удалить» qilib qo'ysak, foydalanuvchi bitta tilni o'chira
    olmasdi (referens: `button__delete-composition-uz` / `-ru`).
    """
    html = _markup(TEMPLATE)
    assert 'data-opt-del="{{ code }}"' in html, "til bo'yicha o'chirish tugmasi yo'q"
    assert "{% for code in ['uz', 'ru'] %}" in html, "ikkala til bloki qurilmayapti"
    js = _read(JS)
    assert "function removeComment(ctype, code)" in js


def test_optional_comment_disappears_when_both_languages_are_deleted():
    """Uzum: ikkala til o'chsa comment splice qilinadi -> «Добавить» qaytadi.

    Bu bo'lmasa «o'lik holat» qolardi: muharrir ham yo'q, «Qo'shish» ham yo'q.
    Jonli tasdiqlangan.
    """
    js = _read(JS)
    assert "if (!st.uz && !st.ru) delete comments[ctype];" in js


def test_optional_section_class_does_not_collide_with_the_value_modal():
    """⚠️ REGRESSIYA QULFI: `.nt-opt` — qiymat modalining variant qatori (flex).

    Ixtiyoriy bo'lim uchun ham `.nt-opt` ishlatilganda modaldan `display:flex`
    meros bo'lib, ikki muharrir yonma-yon chiqib ketdi; bundan tashqari
    JS'dagi `querySelectorAll('.nt-opt')` modal qatorlariga ham urilardi.
    """
    css = _read(CSS)
    js = _read(JS)
    html = _markup(TEMPLATE)
    assert 'class="nt-optsec"' in html
    assert 'class="nt-opt"' not in html, "modal sinfi ixtiyoriy bo'limda ishlatilgan"
    assert "root.querySelectorAll('.nt-opt')" not in js, (
        "bu selektor modal variantlariga ham uriladi"
    )
    assert ".nt-opt {" in css and ".nt-optsec-block {" in css, (
        "ikkala sinf ham mavjud bo'lishi kerak — ular BOSHQA narsalar"
    )


def test_editor_is_a_macro_because_include_cannot_see_macro_locals():
    """⚠️ Jinja tuzog'i: `{% include %}` makro lokallarini KO'RMAYDI.

    Ixtiyoriy bo'limlar muharrirni makro ICHIDAN chaqiradi — include bo'lganda
    `editor_id`/`editor_placeholder` bo'sh ketardi (id'siz muharrir = jim buzilish).
    """
    ed = _read(EDITOR)
    html = _markup(TEMPLATE)
    assert "{% macro nt_editor(" in ed, "muharrir makro bo'lishi shart"
    assert "import nt_editor with context" in html
    assert "include 'partials/nt_editor.html'" not in html, (
        "makro ichidan include ishlamaydi — chaqiruvlar makroga o'tkazilgan"
    )


def test_optional_placeholders_and_labels_are_uzums_exact_wording(  ):
    """Matnlar Uzum bandlisining i18n lug'atidan — o'zimizdan tarjima emas."""
    html = _markup(TEMPLATE)
    for exact in (
        "Сок листьев алоэ, экстракт лотоса, экстракт центеллы...",   # composition_example
        "Aloe barglari sharbati, lotus ekstrakti, sentella ekstrakti...",
        "Разместите здесь информацию о размерной сетке или размерах и габаритах товара",
        "(НЕОБЯЗАТЕЛЬНО)",
        "(Majburiy emas)",
    ):
        assert exact in html, f"Uzum matni yo'q: {exact!r}"


def test_certificate_form_texts_are_uzums_exact_wording():
    """Sertifikat formasi matnlari bandl i18n lug'atidan (uz+ru) — tarjima EMAS.

    Manba: mf-products `editProductCard.certificates` (ru @382334, uz @445399).
    """
    html = _markup(TEMPLATE)
    for exact in (
        # required_hint (sariq banner)
        "Товары этой категории не могут продаваться без сертификата",
        "Bu toifadagi tovarlar sertifikatsiz sotila olmaydi",
        # delete_dialog
        "Удалить сертификат?",
        "Восстановить информацию по сертификату не получится — только добавить заново",
    ):
        assert exact in html, f"Uzum matni yo'q: {exact!r}"
    js = _read(JS)
    for exact in (
        "Номер сертификата",        # certificates.form.number
        "Sertifikat raqami",
        "Окончание действия",       # certificates.form.expiration_date
        "Amal qilishining tugashi",
    ):
        assert exact in js, f"Uzum matni yo'q: {exact!r}"


# ── BOSQICH C2: «Общие фотографии товара» + rasm yuklash ─────────────
#
# ENG QIMMAT TOPILMA — rasm yuklagich BOSHQA kalit ishlatadi:
#   Uzum bandli (chunk-6dbbb9d8):
#       fetch(env.LOUIS_API_HOST + "/upload",
#             {headers:{Authorization: env.LOUIS_KEY}})   // XOM, Bearer'siz
#   Jonli probe 2026-07-17 (4 variant, bitta test PNG):
#       sotuvchi tokeni -> 401 · LOUIS_KEY xom -> 200 ✅
#       LOUIS_KEY+Bearer -> 401 · Authorization'siz -> 401
#   Ya'ni eski kod (`_headers()` -> Bearer sotuvchi tokeni) rasmni HECH QACHON
#   yuklay olmasdi — 401 bilan jim yiqilardi.
#
# Karta ko'rinish sharti (bandl ota-render):
#       requiredMediaType !== NOT_DEFINED ? <edit-product-card-photo/> : null
# Bosqich B fiksturasi: 5078 bargning HAMMASIDA "ANY" -> kategoriya tanlangach
# karta doim chiqadi; kategoriyasiz NOT_DEFINED -> yashirin (frame 05 shuni
# ko'rsatgan: Описание -> Видео).

CLIENT = FEATURE / "client.py"


def test_photo_upload_does_not_use_the_seller_token():
    """⚠️ `_headers()` ISHLATILMASIN — u Bearer sotuvchi tokenini qo'yadi (401).

    Jonli isbot: sotuvchi tokeni -> 401, LOUIS_KEY xom -> 200.
    """
    src = _read(CLIENT)
    # ⚠️ Kesim chegarasi `upload_image` dan KEYINGI funksiyagacha bo'lsin.
    # Ilgari u `def create_product` edi; oradagi media funksiyalari (video —
    # u ATAYLAB `_headers()` ishlatadi) kesimga tushib, test YOLG'ONDAN
    # yiqilgan. Kesim torayishi tekshiruv kuchini kamaytirmaydi — aksincha,
    # endi u faqat `upload_image` ni ko'radi.
    up = src[src.index("def upload_image"):src.index("def _parse_media_json")]
    # ⚠️ `"_headers(" not in up` deb tekshirib bo'lmaydi — bu ibora o'z
    # izohimizda ham uchraydi va test yolg'ondan yiqilardi (HANDOFF tuzog'i).
    # Shuning uchun KOD shakli tekshiriladi: tayinlash.
    assert not re.search(r"headers\s*=\s*_headers\(", up), (
        "upload_image sotuvchi tokenini yuboryapti — Uzum rasm serveri uni rad etadi"
    )
    assert '"Authorization": _LOUIS_KEY' in up, "xom LOUIS_KEY yuborilmayapti"
    assert not re.search(r'f?"Bearer \{?_LOUIS_KEY', up), (
        "LOUIS_KEY Bearer bilan yuborilsa 401 qaytadi (jonli tekshirilgan)"
    )


def test_louis_key_is_not_committed_to_the_repo():
    """Kalit kodda EMAS, muhit o'zgaruvchisida — repoda ikkita remote bor."""
    src = _read(CLIENT)
    assert 'os.getenv("UZUM_LOUIS_KEY")' in src, "kalit env'dan o'qilmayapti"
    # base64'ga o'xshash 40+ belgili qotirilgan qiymat bo'lmasin
    assert not re.search(r'_LOUIS_KEY\s*=\s*["\'][A-Za-z0-9+/=]{30,}["\']', src), (
        "kalit kodga qotirilgan — commitga tushadi"
    )


def test_missing_louis_key_fails_loudly_not_with_a_bare_401():
    """Kalit sozlanmasa sabab aniq aytilsin — jim 401 emas."""
    src = _read(CLIENT)
    assert "UZUM_LOUIS_KEY sozlanmagan" in src


def test_photo_card_visibility_reads_required_media_type_from_meta():
    """Shart META'dan o'qilsin, qotirilmasin.

    `/category/{id}/fields` javobi {fields:{...}} shaklida — shuning uchun
    ikki qavat: meta.fields.fields.requiredMediaType.
    """
    js = _read(JS)
    assert "state.meta.fields && state.meta.fields.fields" in js, (
        "fields ichma-ich o'qilmagan — requiredMediaType topilmaydi"
    )
    assert "requiredMediaType() === 'NOT_DEFINED'" in js
    html = _markup(TEMPLATE)
    assert 'id="ntCardPhoto" hidden' in html, "karta boshlang'ich holatda yashirin bo'lsin"


def test_photo_upload_goes_through_our_proxy_only():
    """Token brauzerga chiqmasin — fayl Flask proxy'siga ketadi."""
    js = _read(JS)
    assert "'/noviy-tavar/api/upload-image'" in js
    assert "images-uploader" not in js, "brauzer Uzum rasm serveriga to'g'ridan-to'g'ri urinyapti"
    assert "LOUIS" not in js, "kalit brauzer JS'iga sizib chiqqan"


def test_client_side_size_limit_matches_the_bundle():
    """Bandl: MediaUploader `maxSize: 5` (MB) — biz ham 5 da kesamiz."""
    js = _read(JS)
    assert "MAX_PHOTO_BYTES = 5 * 1024 * 1024" in js


def test_upload_errors_are_not_swallowed():
    """Xato jim yutilsa foydalanuvchi rasm yuklandi deb o'ylaydi."""
    js = _read(JS)
    assert "if (!res.ok || !res.d || !res.d.key)" in js
    assert "Не удалось загрузить фото" in js


def test_uzum_quality_recommendations_are_shown():
    """Uzum «1080×1440 dan kichik» kabi ogohlantirish qaytaradi — KO'RSATAMIZ.

    Jonli probe aynan shu matnni qaytardi; ko'rsatmasak karta moderatsiyada
    rad etiladi va sabab foydalanuvchiga noma'lum qoladi.
    """
    js = _read(JS)
    assert "res.d.recommendations" in js, "tavsiyalar o'qilmayapti"
    routes = _read(FEATURE / "routes.py")
    assert '"recommendations"' in routes, "proxy tavsiyalarni qaytarmayapti"


def test_photo_tiles_match_the_reference_frame():
    """Foydalanuvchi bergan jonli kadr: tartib raqami + ⋮ menyu + sudrash matni.

    ⋮ ichi bandldan: [Удалить, Скрыть, Вернуть видимость] — oxirgi ikkitasi
    `can-hide` (hasMainUploadedStudio) ga bog'liq, yangi tovarda u false,
    shuning uchun yaratish oqimida faqat «O'chirish».
    """
    js = _read(JS)
    assert "nt-photo-num" in js, "tartib raqami yo'q"
    assert "nt-photo-menu" in js, "⋮ menyusi yo'q"
    assert "fig.draggable = true" in js, "sudrab tartiblash yo'q"
    html = _markup(TEMPLATE)
    assert 'id="ntPhotosHint" hidden' in html, (
        "sudrash matni boshida yashirin bo'lsin — rasm bo'lgandagina chiqadi"
    )


def test_photo_is_required_for_save():
    """Rasmsiz «Saqlash» yonmasin — server ham rasmsiz qoralama yaratmaydi."""
    js = _read(JS)
    assert "(!needPhoto || state.images.length > 0)" in js


# ── Filtrlar (Бренд / Модель / Страна) + Saqlash ─────────────────────
#
# REJADAGI TESHIK: HANDOFF filtrlarni HECH QAYERDA sanamagan, lekin Bosqich B
# fiksturasi ko'rsatdi — har bargda AYNAN 3 filtr va «Бренд» 5078/5078 da
# MAJBURIY. Ya'ni ularsiz forma haqiqiy tovar bera olmaydi.
#
# Bandl dalili (edit-product-card-filters.ce): har filtr O'Z KARTASI, `v-for`
# API TARTIBIDA; enum BRAND=6, MODEL=7, COUNTRY=8; katakcha yorlig'i
# `filter_missing + " " + title.toLowerCase()`; brend sarlavhasi i18n'dan
# QAYTA YOZILADI va katakcha ham shu nomni oladi.


def test_filters_are_built_from_meta_not_hardcoded():
    """Filtrlar kategoriyaga bog'liq — meta.filters dan qurilsin."""
    js = _read(JS)
    assert "function buildFilters(meta)" in js
    assert "(meta && meta.filters) || []" in js
    html = _markup(TEMPLATE)
    assert 'id="ntFilters"' in html, "filtrlar uchun konteyner yo'q"


def test_filter_ids_match_the_bundle_enum():
    """Bandl: MODEL=7, COUNTRY=8, BRAND=6."""
    js = _read(JS)
    assert "FILTER_BRAND = 6" in js
    assert "FILTER_MODEL = 7" in js
    assert "FILTER_COUNTRY = 8" in js


def test_brand_title_override_happens_before_the_checkbox_label():
    """⚠️ JONLI USHLANGAN XATO: karta «Brend», katakcha «tovar belgisi mavjud emas».

    Bandl brend sarlavhasini i18n'dan qayta yozadi va buni katakcha yorlig'i
    hisoblanishidan OLDIN qiladi. Faqat sarlavhani almashtirsak, katakchaga
    API'ning uz tarjimasi tushib qolardi.
    """
    js = _read(JS)
    assert "f = Object.assign({}, f, { title: tr('Brend', 'Бренд') });" in js, (
        "brend nomi katakcha yorlig'idan oldin almashtirilmayapti"
    )


def test_required_filters_gate_the_save_button():
    """«Бренд» majburiy — usiz «Saqlash» yonmasin. Majburiylik meta'dan."""
    js = _read(JS)
    # ⚠️ `"requiredFiltersFilled()" in js` YETARLI EMAS — bu ibora funksiya
    # TA'RIFIDA ham bor, ya'ni chaqiruv olib tashlansa ham test o'tib ketardi
    # (mutatsiya sinovi shuni ko'rsatdi). Shuning uchun aynan CHAQIRUV joyi.
    assert "&& requiredFiltersFilled()" in js, (
        "validate() majburiy filtrlarni tekshirmayapti"
    )
    assert "return !f.required || state.filterValues[f.id] != null;" in js, (
        "majburiylik meta'dan o'qilmayapti (qotirilgan bo'lishi mumkin)"
    )


def test_filter_value_errors_are_not_swallowed():
    """Bo'sh ro'yxat «brend yo'q» degan yolg'on xabar bo'lardi."""
    js = _read(JS)
    assert "Не удалось загрузить список" in js


def test_filter_list_is_paginated_like_the_har():
    """HAR: size=24/sahifa; bandl `loadMore` — pastga yetganda keyingi sahifa."""
    js = _read(JS)
    assert "res.items.length < 24" in js, "sahifa hajmi HAR'dagi 24 bilan mos emas"
    assert "panel.scrollTop + panel.clientHeight" in js, "sahifalash (loadMore) yo'q"


def test_empty_value_checkbox_sends_the_filters_empty_value_id():
    """«Отсутствует бренд» belgilansa — qiymat sifatida emptyValue.id ketadi."""
    js = _read(JS)
    assert "state.filterValues[f.id] = f.emptyValue.id;" in js
    assert "input.disabled = cb.checked;" in js, "katakcha tanlagichni o'chirmayapti"


def test_save_actually_calls_the_create_proxy():
    """Ilgari brauzer /api/create ni UMUMAN chaqirmasdi — forma hech nima yaratmasdi."""
    js = _read(JS)
    assert "'/noviy-tavar/api/create'" in js
    assert "method: 'POST'" in js


def test_save_errors_are_not_swallowed():
    js = _read(JS)
    assert "if (!res.ok || !res.d || !res.d.id)" in js
    assert "Не удалось создать черновик" in js


def test_save_sends_comments_and_filters():
    """Ixtiyoriy bo'limlar va filtrlar yaratish tanasiga qo'shilsin."""
    js = _read(JS)
    assert "comments: collectComments()" in js
    assert "filterValues: fvals" in js


def test_create_route_passes_comments_through():
    src = _read(FEATURE / "routes.py")
    assert "comments_sel=body.get(\"comments\")" in src


def test_client_rejects_unknown_comment_types():
    """Noma'lum tur Uzum'ga o'tmasin — kalitlar ro'yxati bitta joyda."""
    src = _read(CLIENT)
    assert '_COMMENT_TYPES = frozenset({"Состав", "Размеры", "Инструкция", "Сертификация"})' in src
    assert "if ctype not in _COMMENT_TYPES:" in src


# ── Sertifikatlar formasi (C qoldig'i) ───────────────────────────────
#
# Bu bo'lim REAL topilgan xatoni qulflaydi: karta oldin faqat ikki tilli MATN
# muharriri (nt_optional 'Сертификация') sifatida qurilgan edi. Uchta mustaqil
# dalil buni rad etdi:
#   1) kadr 08 — Состав/Инструкция da «Добавить», Сертификаты da «＋ Добавить»
#   2) bandl `edit-product-card-certificates.ce` — forma + shartli muharrir
#   3) 235 HAR — productCertificates 24/235 (= fillType REQUIRED bo'lgan 24 ta)


def test_certificates_card_is_a_form_not_a_comment_editor():
    """Sertifikat kartasi `nt_optional` muharririni ISHLATMASIN.

    ⚠️ Yalang'och 'Сертификация' ni qidirish YOLG'ON MUSBAT beradi — u
    izohlarda ham, `nt_optional` ta'rifida ham uchraydi. Shuning uchun
    aynan MAKRO CHAQIRUVI tekshiriladi.
    """
    src = _markup(TEMPLATE)
    assert not re.search(r"nt_optional\(\s*'Сертификация'", src), (
        "Сертификаты kartasi matn muharriri EMAS — productCertificates formasi "
        "(dalil: kadr 08 «＋ Добавить» + bandl edit-product-card-certificates.ce)"
    )
    # Boshqa uchta ixtiyoriy bo'lim TEGILMAGAN qolsin (doira chegarasi).
    for ctype in ("Размеры", "Состав", "Инструкция"):
        assert re.search(r"nt_optional\(\s*'%s'" % ctype, src), (
            f"«{ctype}» ixtiyoriy bo'limi tasodifan olib tashlangan"
        )


def test_certificates_form_has_required_controls():
    """Bandl elementi: [× yopish] + «Сертификат N» + rasm + number + expirationDate."""
    src = _markup(TEMPLATE)
    assert 'id="ntCertList"' in src
    assert 'id="ntCertAdd"' in src
    assert 'id="ntCertInput"' in src
    js = _read(JS)
    # Uzum store'ining AYNAN boshlang'ich obyekti
    assert "{ expirationDate: '', number: '', certificateImages: [] }" in js
    assert "tr('Sertifikat', 'Сертификат')" in js


def test_certificate_add_button_has_plus_icon_unlike_other_sections():
    """O'LCHANGAN (kadr 08): Состав/Инструкция «Добавить» BELGISIZ,
    Сертификаты «＋ Добавить» PLYUS bilan — bandl `h["dd"]` plus ikonkasi."""
    src = _markup(TEMPLATE)
    m = re.search(r'<button[^>]*id="ntCertAdd".*?</button>', src, re.S)
    assert m, "«Добавить» tugmasi topilmadi"
    assert 'class="nt-plus"' in m.group(0), "sertifikat tugmasida plyus belgisi yo'q"
    # nt_optional dagi «Добавить» esa belgisiz qolsin
    macro = re.search(r'<button[^>]*data-nt-optional=.*?</button>', src, re.S)
    assert macro and 'nt-plus' not in macro.group(0), (
        "ixtiyoriy bo'lim tugmasiga plyus qo'shilib qolgan (kadr 08 da yo'q)"
    )


def test_certificate_validation_matches_bundle_rule():
    """Bandl `validate()` AYNAN ko'chirilgan:
         OPTIONAL && 0 -> valid · REQUIRED && 0 -> INVALID
         har birida number && rasm && expirationDate
    """
    js = _read(JS)
    assert "if (ft === 'OPTIONAL' && !list.length) return true;" in js
    assert "if (ft === 'REQUIRED' && !list.length) return false;" in js
    assert "return !!c.number && c.certificateImages.length > 0 && !!c.expirationDate;" in js
    # Saqlash tugmasi darvozasiga ULANGAN bo'lsin (aks holda qoida o'lik kod)
    assert "&& certificatesValid()" in js


def test_certificate_fill_type_is_read_from_meta_not_hardcoded():
    """`fillType` meta.certification dan keladi (GET product-certification-filltype,
    javob O'RAMSIZ: {"fillType":"OPTIONAL"}). 235 HAR: OPTIONAL 211, REQUIRED 24."""
    js = _read(JS)
    assert "state.meta && state.meta.certification" in js
    src = _read(FEATURE / "client.py")
    assert "product-certification-filltype" in src


def test_certificate_required_star_and_banner_are_conditional():
    """Bandl: `required: fillType === REQUIRED` -> `*` va sariq banner shartli."""
    js = _read(JS)
    assert "certReq.hidden = ft !== 'REQUIRED'" in js
    assert "ft === 'REQUIRED' && !certBannerDismissed()" in js
    src = _markup(TEMPLATE)
    # Boshlang'ich holatda ikkalasi ham YASHIRIN (kadr 08 da ular yo'q)
    assert re.search(r'id="ntCertReq"[^>]*\shidden', src)
    assert re.search(r'id="ntCertBanner"[^>]*\shidden', src)


def test_certificates_are_sent_in_create_body():
    js = _read(JS)
    assert "certificates: state.certificates" in js
    src = _read(FEATURE / "routes.py")
    assert 'certificates=body.get("certificates")' in src


def test_hidden_buttons_actually_hide():
    """`.nt-btn{display:inline-flex}` muallif uslubi UA'ning `[hidden]{display:none}`
    idan USTUN keladi -> aniq qoida bo'lmasa `btn.hidden = true` ISHLAMAYDI."""
    css = _read(CSS)
    assert '[data-page="noviy-tavar"] .nt-btn[hidden] { display: none; }' in css


# ── Har bir rang uchun mediafayllar (colorImages) ────────────────────
#
# DALIL:
#   darvoza  — bandl ota-render @498913: productColors && requiredMediaType===ANY
#   colors   — store @43526: Object.values(characteristicTitle).includes("Цвет")
#   joylashuv— kadr 009 (category-interactions/001_.../_value-selected-popup-closed)
#   tana     — client.py col_imgs: {key,url,color:{uz,ru},ordering} -> Uzum shakli


def test_color_media_gate_matches_bundle_rule():
    """Darvoza: «Цвет» sarlavhali qatorda qiymat tanlangan BO'LSA va ANY bo'lsa."""
    js = _read(JS)
    # ⚠️ Bandl so'zma-so'z «Цвет» ni solishtiradi (id EMAS) — biz ham.
    assert "o.uz === 'Цвет' || o.ru === 'Цвет'" in js
    assert "requiredMediaType() === 'ANY'" in js
    # Darvoza HAQIQATAN kartani yashirsin (aks holda qoida o'lik kod)
    assert "colorMediaCard.hidden = !on;" in js


def test_color_media_card_is_hidden_until_a_colour_is_picked():
    src = _markup(TEMPLATE)
    assert re.search(r'id="ntCardColorMedia"[^>]*\shidden', src), (
        "rang-media kartasi boshlang'ich holatda ko'rinib turibdi — kadr 05/08 da u YO'Q"
    )


def test_color_media_card_sits_between_characteristics_and_size_grid():
    """Kadr 009: «Характеристики» -> «Медиафайлы для каждого цвета» -> «Размерная сетка».

    Tartib buzilsa forma Uzumdan farq qiladi.
    """
    src = _markup(TEMPLATE)
    i_chars = src.index('id="ntCharRows"')
    i_media = src.index('id="ntCardColorMedia"')
    i_grid = src.index('Размерная сетка')
    assert i_chars < i_media < i_grid, "rang-media kartasi noto'g'ri joyda"


def test_color_media_tips_are_uzums_exact_wording():
    """`editProductCard.hints.color_media` — UCHTA ustun, aynan matn."""
    src = _markup(TEMPLATE)
    for exact in (
        "Формат для фотографий", "Формат для видео", "Формат для фотографий 360",
        "Fotosuratlar uchun format", "Video uchun format", "360 fotosuratlar uchun format",
    ):
        assert exact in src, f"Uzum matni yo'q: {exact!r}"


def test_color_images_are_sent_in_create_body():
    js = _read(JS)
    assert "colorImages: state.colorImages" in js
    src = _read(FEATURE / "routes.py")
    assert 'color_images=body.get("colorImages")' in src


def test_color_image_limit_matches_bundle_max_count():
    """Bandl `maxColorImages` prop default = 10, `max-size: 5` (MB)."""
    js = _read(JS)
    assert "var MAX_COLOR_IMAGES = 10;" in js


def test_color_photos_are_actually_draggable():
    """Sudrash MATNI ko'rsatilsa, sudrash HAQIQATAN ishlashi shart.

    Aks holda UI yolg'on va'da beradi (bandl uploader'ida `onSortImage` bor).
    """
    js = _read(JS)
    assert "moveColorImage(v, colorDragFrom, i);" in js
    assert "fig.draggable = true;" in js


def test_removing_a_colour_drops_its_images():
    """Bandl `resetColorImages()`: tanlovda yo'q rangning rasmlari tushadi.

    Aks holda Uzum'ga tanlanmagan rang uchun rasm ketardi.
    """
    js = _read(JS)
    assert "return keys.indexOf(colorKey(im.color)) >= 0;" in js


# ── Video / Foto 360 ─────────────────────────────────────────────────
#
# ⚠️ AUTH RASMNIKIDAN TESKARI — bu bo'limning asosiy tuzog'i:
#     rasm / 360 -> images-uploader.uzum.uz  + XOM LOUIS_KEY
#     video      -> api-seller.uzum.uz/api   + Bearer sotuvchi tokeni
# Jonli probe (2026-07-18) uchala nomzod hostni sinab isbotladi. Handoff
# «seller.uzum.uz» degan edi — XATO (u yerda 200 + HTML qaytadi).

ROUTES = FEATURE / "routes.py"


def test_video_upload_uses_the_api_seller_host_not_the_portal_host():
    """⚠️ `seller.uzum.uz/api` — SPA qobig'i, API EMAS (200 + text/html).

    Jonli probe: api-seller.uzum.uz/api/public/video/upload -> 200 JSON ✅
                 seller.uzum.uz/api/public/video/upload     -> 200 HTML ❌
    Tarmoq dalili: 707 HAR / 322 717 yozuvda `seller.uzum.uz/api/*` -> 0 yo'l.
    """
    src = _read(CLIENT)
    assert re.search(
        r'_VIDEO_UPLOAD_URL\s*=\s*"https://api-seller\.uzum\.uz/api/public/video/upload"',
        src,
    ), "video hosti noto'g'ri — portal hosti HTML qaytaradi, JSON emas"


def test_video_upload_uses_the_seller_token_not_the_louis_key():
    """Rasmning TESKARISI: bu yerda `_headers()` (Bearer) TO'G'RI.

    Jonli probe 2026-07-18: Bearer sotuvchi tokeni -> 200 ✅
    """
    src = _read(CLIENT)
    up = src[src.index("def upload_video"):src.index("def upload_collection")]
    assert re.search(r"headers\s*=\s*_headers\(\)", up), (
        "video Bearer tokensiz ketyapti — jonli probe aynan shu sxemani tasdiqlagan"
    )
    assert "_LOUIS_KEY" not in up, (
        "videoga LOUIS_KEY qo'yilgan — u rasm/360 uchun, video uchun EMAS"
    )


def test_collection_upload_uses_the_raw_louis_key_like_images():
    """360 — rasm bilan BIR XIL: xom LOUIS_KEY, Bearer prefiksisiz."""
    src = _read(CLIENT)
    up = src[src.index("def upload_collection"):src.index("def collection_status")]
    assert '"Authorization": _LOUIS_KEY' in up
    assert not re.search(r"headers\s*=\s*_headers\(", up), (
        "360 sotuvchi tokenini yuboryapti — LOUIS uni rad etadi"
    )


def test_media_response_rejects_html_even_with_http_200():
    """⚠️ Uzum'da 200 != muvaffaqiyat.

    User-Agent yoqmasa anti-bot sahifasi HTTP 200 + text/html bilan keladi
    («Siz robot emasmisiz?»); noto'g'ri hostda SPA qobig'i — u ham 200.
    Ikkalasi ham jonli probe'da uchradi va auth haqida yolg'on xulosa berdi.
    """
    src = _read(CLIENT)
    fn = src[src.index("def _parse_media_json"):src.index("def upload_video")]
    assert 'if "json" not in ctype' in fn, (
        "Content-Type tekshirilmayapti — HTML javob muvaffaqiyat deb o'qiladi"
    )


def test_video_response_reads_snake_case_original_url():
    """⚠️ Rasm `originalUrl` (camel), video `original_url` (snake) — jonli
    probe tasdiqlagan. camelCase o'qisak key/url bo'sh chiqadi."""
    src = _read(CLIENT)
    up = src[src.index("def upload_video"):src.index("def upload_collection")]
    assert 'payload.get("original_url")' in up, (
        "video javobi snake_case — `originalUrl` o'qish bo'sh natija beradi"
    )


def test_collection_upload_and_status_are_two_separate_calls():
    """360 ASINXRON: yuklash faqat `collection_id` beradi.

    ⚠️ Bandl komponenti `payload.collections[0]` ni yuklash javobidan
    kutgandek ko'rinadi — CHALG'ITADI. Jonli probe:
        POST /upload/collection -> {"payload":{"collection_id":9144}}
        GET  /status/collection/9144 -> {"payload":{"collections":[...]}}
    """
    src = _read(CLIENT)
    up = src[src.index("def upload_collection"):src.index("def collection_status")]
    assert 'payload.get("collection_id")' in up, (
        "yuklash javobidan collection_id o'qilmayapti"
    )
    st = src[src.index("def collection_status"):]
    st = st[:st.index("def build_create_body")]
    assert 'payload.get("collections")' in st, (
        "collections status javobidan o'qilmayapti"
    )


def test_media_size_limits_match_the_bundle_check_not_the_hint_text():
    """⚠️ Ekrandagi maslahat «3 Mb» deydi, bandl KODI `K = 10` ni tekshiradi.

    Uzum'ning O'ZIDA ham shunday (matn 3, tekshiruv 10) — referensga sodiqmiz.
    """
    src = _read(CLIENT)
    assert "MAX_VIDEO_BYTES = 10 * 1024 * 1024" in src
    assert "MAX_COLLECTION_BYTES = 10 * 1024 * 1024" in src
    js = _read(JS)
    assert "MAX_VIDEO_BYTES = 10 * 1024 * 1024" in js
    assert "MAX_360_BYTES = 10 * 1024 * 1024" in js


def test_media_uploads_go_through_our_proxy_only():
    """Token/kalit brauzerga chiqmasin — hamma narsa /noviy-tavar/api/* orqali."""
    js = _read(JS)
    assert "'/noviy-tavar/api/upload-video'" in js
    assert "'/noviy-tavar/api/upload-collection'" in js
    assert "/noviy-tavar/api/collection-status?shop=" in js
    # ⚠️ Yalang'och satr emas — host nomi izohda ham uchraydi, shuning uchun
    # CHAQIRUV shakli tekshiriladi (fetch ichida).
    assert not re.search(r"fetch\(\s*['\"]https://", js), (
        "brauzer Uzum'ga to'g'ridan-to'g'ri urinyapti"
    )


def test_media_proxies_are_guarded_by_login_and_shop_ownership():
    """Proxy'lar auth/egalik chegarasini zaiflashtirmasin."""
    src = _read(ROUTES)
    for fn in ("nt_upload_video", "nt_upload_collection", "nt_collection_status"):
        blk = src[src.index("def " + fn):]
        nxt = blk.find("\n@noviy_tavar_bp")
        blk = blk[:nxt] if nxt != -1 else blk
        assert "_can_access(shop)" in blk, f"{fn} do'kon egaligini tekshirmayapti"
    for route in ("upload-video", "upload-collection", "collection-status"):
        assert '"/noviy-tavar/api/' + route + '"' in src


def test_collection_poll_gives_up_instead_of_hanging_forever():
    """Jim osilib qolish xatodan battar — poll chegarali bo'lsin."""
    js = _read(JS)
    assert "POLL_TRIES" in js
    assert "if (left <= 1)" in js, "poll cheksiz — foydalanuvchi abadiy kutadi"
    assert "POLL_MS = 5000" in js, "bandl 5000ms interval bilan poll qiladi"


def test_media_upload_errors_are_not_swallowed():
    """Xato jim yutilsa foydalanuvchi yuklandi deb o'ylaydi."""
    js = _read(JS)
    assert "Не удалось загрузить видео" in js
    assert "Не удалось загрузить фото 360" in js


def test_color_media_card_actually_implements_what_its_hints_promise():
    """⚠️ DARS (HANDOFF_2026_07_18 §2.5): implement qilmagan narsani va'da
    qiladigan matn ko'rsatma.

    Rang-media kartasining maslahatlari «Формат для видео» va «Формат для
    фотографий 360» deydi — demak per-rang video va 360 ISHLASHI shart.
    """
    html = _markup(TEMPLATE)
    assert "Формат для видео" in html and "Формат для фотографий 360" in html
    assert 'id="ntColorVideoInput"' in html
    assert 'id="ntColor360Input"' in html
    js = _read(JS)
    assert "state.colorVideos.push(" in js, "per-rang video implement qilinmagan"
    assert "state.colorCollections.push(" in js, "per-rang 360 implement qilinmagan"


def test_color_media_is_one_per_color_not_a_list():
    """Bandl `colorVideosChange`/`colorCollectionsChange` `findIndex` bilan
    ALMASHTIRADI — rang boshiga bitta video/360.

    ⚠️ `... .filter(function (m) {` ga assert qilib bo'lmaydi — u rang
    yechilganda tozalash mantiqida ham uchraydi va test YOLG'ONDAN o'tardi.
    Shuning uchun ALMASHTIRISH predikati tekshiriladi (`!== k`), tozalashniki
    esa (`indexOf(...) >= 0`) — ikkalasi boshqa-boshqa shakl.
    """
    js = _read(JS)
    assert js.count("colorKey(m.color) !== k;") == 2, (
        "eski video/360 almashtirilmayapti — rangga ikkinchisi qo'shilib qoladi"
    )


def test_removing_a_color_also_drops_its_video_and_collection():
    """Rang yechilsa uning MEDIASI ham ketsin (bandl `resetColorImages`)."""
    js = _read(JS)
    sync = js[js.index("function syncColorMedia()"):js.index("function renderColorMedia()")]
    assert "state.colorVideos = state.colorVideos.filter(" in sync
    assert "state.colorCollections = state.colorCollections.filter(" in sync
    # rasm bilan BIR XIL predikat — tanlangan ranglar ro'yxatiga tegishlilik
    assert sync.count("return keys.indexOf(colorKey(m.color)) >= 0;") == 2
    # rang qatori butunlay olib tashlansa — hammasi bo'shaydi
    assert "state.colorVideos = [];" in sync and "state.colorCollections = [];" in sync


def test_collection_preview_url_is_not_sent_in_the_create_body():
    """`previewUrl` faqat UI uchun — Uzum uni tanada saqlamaydi."""
    js = _read(JS)
    assert "imageCollection: state.imageCollection" in js
    assert "{ collectionId: state.imageCollection.collectionId }" in js, (
        "previewUrl tanaga sizib chiqyapti"
    )


# ══════════════════════════════════════════════════════════════════════
#  2-QADAM «Цены и SKU» — UI shartnomasi
# ══════════════════════════════════════════════════════════════════════


def test_template_actually_compiles_as_jinja():
    """⚠️ BU TEST REAL BO'SHLIQNI YOPADI (2026-07-17 da urilgan).

    Men `{% set L = {...} %}` IFODASI ichiga `{# izoh #}` yozdim. Natija:
    butun sahifa HTTP 500 («unexpected char '#'»). Shu paytda testlarning
    HAMMASI (774 ta) O'TAYOTGAN edi — chunki ular faylni MATN sifatida
    o'qiydi, Jinja sifatida kompilyatsiya qilmaydi.

    Ya'ni «testlar yashil» degani «sahifa ochiladi» degani EMAS edi.
    """
    import jinja2

    for path in (TEMPLATE, EDITOR):
        try:
            jinja2.Environment().parse(_read(path))
        except jinja2.TemplateSyntaxError as e:
            pytest.fail(f"{path.name}:{e.lineno} Jinja sintaksis xatosi: {e.message}")


def test_step2_has_every_column_uzum_shows():
    """Ustunlar Uzum bandlining O'Z i18n blokidan (mf-products @359393..359742).

    ⚠️ Referens skrinshot «Цена, сум» da KESILGAN — undan keyingi 5 ta ustun
    unda ko'rinmaydi. Skrinshotga qarab qurgan odam ularni tushirib qoldirardi.
    """
    src = _markup(TEMPLATE)
    for key in ("c_article", "c_status", "c_seller_code", "c_barcode", "c_ikpu",
                "c_width", "c_length", "c_height", "c_weight", "c_market",
                "c_price", "c_off", "c_sell", "c_logistics", "c_comm", "c_withdraw"):
        assert f"nt_t('{key}')" in src, f"{key} ustuni jadvalda yo'q"


def test_column_labels_match_uzums_exact_russian_wording():
    """Ruscha yorliqlar bandl bilan SO'ZMA-SO'Z bir xil bo'lsin.

    ⚠️ `_markup()` SHART, `_read()` EMAS — bu bo'shliqni MUTATSION TEST ochdi
    (2026-07-17): yorliqni «Цена, сум» -> «Цена» ga buzdim va test JIM O'TDI,
    chunki o'sha satr shu faylning IZOHIDA ham bor (bandl i18n dalili yozib
    qo'yilgan). Xom matnga assert qilish YOLG'ON MUSBAT beradi — handoff
    §11 №2 aynan shundan ogohlantirgan, men baribir tushib qoldim.
    """
    src = _markup(TEMPLATE)
    for exact in ("Артикул", "Статус", "Идентификатор SKU", "Штрихкод", "ИКПУ",
                  "Ширина, мм", "Длина, мм", "Высота, мм", "Вес, г",
                  "Рекомендуемая цена", "Цена, сум", "Скидка, сум",
                  "Цена продажи, сум", "Логистический сбор (за шт.)",
                  "Комиссия (за шт.)", "К выводу (за шт.)",
                  "Пока не нашли цену"):
        assert f'"{exact}"' in src, f"Uzum yorlig'i so'zma-so'z emas: {exact}"


def test_bulk_fill_is_exactly_the_seven_fields_the_bundle_defines():
    """Bandl `forAllValues` ANIQ 7 maydon beradi:
        {fullPrice, off, ikpu, width, length, height, weight}
    Ko'proq qo'ysak — Uzumda yo'q tugma; kamroq qo'ysak — funksiya yo'qoladi.
    """
    src = _markup(TEMPLATE)
    got = set(re.findall(r"nt_bulk\('([A-Za-z]+)'\)", src))
    assert got == {"fullPrice", "off", "ikpu", "width", "length", "height", "weight"}


def test_sell_price_is_computed_never_an_input():
    """⚠️ Bandl `lt()` assimetriyasi HAL QILUVCHI:
           fullPrice: Number(t.fullPrice.value)   <- .value BOR  = input
           sellPrice: Number(t.sellPrice)         <- .value YO'Q = hisoblangan
    Ya'ni «Цена, сум» -> fullPrice, sellPrice esa `at()` da fullPrice-off
    dan chiqadi. sellPrice'ni inputga aylantirsak Uzum bilan farq qilamiz.
    """
    js = _read(JS)
    assert "function s2SellPrice(" in js
    # sellPrice uchun katak REDAKTSIYA QILINMAYDIGAN bo'lishi kerak
    assert "nt-td--sell" in js
    assert "s2Cell(row, i, 'sellPrice'" not in js


def test_step2_never_talks_to_uzum_directly():
    """Token brauzerga chiqmaydi — hamma chaqiruv /noviy-tavar/api/* orqali."""
    js = _read(JS)
    for path in ("/noviy-tavar/api/sku-step", "/noviy-tavar/api/send-sku",
                 "/noviy-tavar/api/commission", "/noviy-tavar/api/ikpu-search"):
        assert path in js, f"{path} chaqirilmaydi"


def test_defined_characteristics_are_passed_through_to_send_sku():
    """⚠️ Usiz SKU RANGSIZ ketadi (201 keladi, lekin rang yo'q) —
    `build_sku_body._sku_chars_for` aynan shunga tayanadi."""
    js = _read(JS)
    assert "definedCharacteristicList: S2.defined" in js


def test_step2_css_stays_scoped_to_the_feature():
    """2-qadam CSS'i boshqa sahifalarga sizib chiqmasin."""
    css = _read(CSS)
    for sel in (".nt-tbl", ".nt-sku-badge", ".nt-bulk-pop", ".nt-ikpu-pop",
                ".nt-sku-tab", ".nt-td-unit"):
        for m in re.finditer(re.escape(sel) + r"[\s,{:]", css):
            line_start = css.rfind("\n", 0, m.start()) + 1
            line = css[line_start:m.end()]
            assert '[data-page="noviy-tavar"]' in line or line.strip().startswith(sel[1:]), \
                f"{sel} qoidasi [data-page] ostida qulflanmagan: {line!r}"


def test_commission_columns_are_honest_about_being_empty():
    """Komissiya `skuId` talab qiladi -> yangi qoralamada BO'SH (jonli 2 marta
    tasdiqlangan). Kod buni izohda ochiq aytsin — keyingi odam «buzuq» deb
    o'ylab, qaytadan tekshirib vaqt yo'qotmasin."""
    js = _read(JS)
    assert "skuId" in js and "s2Commission" in js
    routes = _read(FEATURE / "routes.py")
    assert "skuPrices" in routes


def test_uzbek_labels_are_uzums_own_not_invented():
    """⚠️ Uzum bandlida O'ZBEKCHA i18n bloki BOR (mf-products @419100..425900).

    Men avval o'zim tarjima qildim va JIDDIY xato qildim:
        Артикул  -> «Artikul»  (Uzumniki: «Sotuvchi kodi»)
        ИКПУ     -> «IKPU»     (Uzumniki: «MXIK»)
        Вес, г   -> «Ogʻirligi, g» (Uzumniki: «Vazn, g»)
    Bu test o'sha xatoni qaytadan kiritishdan saqlaydi.
    """
    src = _markup(TEMPLATE)
    for uz in ("Sotuvchi kodi", "MXIK", "Kenglik, mm", "Uzunlik, mm",
               "Balandlik, mm", "Vazn, g", "Sotish narxi, so’m",
               "Dona uchun logistika to’lovi", "Dona uchun komissiya",
               "Barchasi", "Arxivdagilar", "Hozircha narxni topmadik",
               "Tavsiya qilinadigan narx"):
        assert f'"{uz}"' in src, f"Uzum'ning o'z o'zbekchasi emas: {uz}"

    # Men o'ylab topgan variantlar QAYTIB KELMASIN
    for invented in ("Artikul", "Eni, mm", "Ogʻirligi, g", "Sotuv narxi",
                     "Narx hali topilmadi", "Для всех строк"):
        assert f'"{invented}"' not in src, f"o'ylab topilgan matn qaytib kelibdi: {invented}"


def test_tooltip_class_does_not_collide_with_the_yellow_tips_card():
    """⚠️ `.nt-tip` BAND — u sariq maslahat kartasi (noviy_tavar.css).

    Men ⓘ uchun aynan shu nomni tanlab, kartani buzishimga oz qoldi
    (handoff §11 №13 dagi `.nt-opt` tuzog'ining aynan takrori).
    ⓘ sinfi `nt-hint` bo'lishi SHART.
    """
    src = _markup(TEMPLATE)
    # ⓘ MAKROSI aynan nt-hint ishlatsin. (`class="nt-tip"` sahifada BOR va
    # BO'LISHI KERAK — u sariq karta; shuning uchun butun faylga assert
    # qilib bo'lmaydi, faqat makroga.)
    m = re.search(r"\{%\s*macro\s+nt_tip\(.*?%\}(.*?)\{%\s*endmacro\s*%\}", src, re.S)
    assert m, "nt_tip makrosi topilmadi"
    assert 'class="nt-hint"' in m.group(1)
    assert 'class="nt-tip"' not in m.group(1), "`.nt-tip` band — sariq maslahat kartasi"

    css = _read(CSS)
    # Sariq karta qoidalari joyida turibdimi (ular boshqa narsa — buzilmasin)
    assert '.nt-tip-h' in css and '.nt-tips' in css
    # ⚠️ CHEGARA SHART: `'.nt-hint' in css` YETARLI EMAS — `.nt-hint-DELETED`
    # ham o'sha satrni o'z ichiga oladi va test JIM O'TADI. Buni mutatsion
    # test ochdi (bugun UCHINCHI marta shu tuzoq: yalang'och satrga assert).
    assert re.search(r'\.nt-hint\s*[,{:]', css), "ⓘ uchun CSS qoidasi yo'q"


def test_tooltip_texts_are_uzums_own_wording():
    """ⓘ matnlari bandl `tooltip_messages` blokidan (ru @358439 / uz @420860)."""
    src = _markup(TEMPLATE)
    assert "Идентификационный код продукции и услуг" in src
    assert "Mahsulot va xizmatlarning identifikatsiya kodi" in src
    # «Рекомендуемая цена» ⓘ — uzun matn, boshi yetarli
    assert "Анализируем похожие товары на рынке" in src
    assert "Bozordagi o‘xshash tovarlarni tahlil qilamiz" in src


def test_bulk_popup_uses_uzums_label_not_an_invented_one():
    """Bandl `specify_for_entire_column` (ru @357034 / uz @419328).
    Men «Для всех строк» deb o'ylab topgandim — Uzumda unday matn YO'Q."""
    src = _markup(TEMPLATE)
    assert '"Указать для всей колонки"' in src
    assert '"Barcha ustunlar uchun koʻrsating"' in src


# ── 3-QADAM «Свойства» (D-B) ─────────────────────────────────────────


def test_step3_block_and_containers_exist():
    """3-qadam bloki + «taqiq» ekrani + jadval konteynerlari joyida."""
    src = _markup(TEMPLATE)
    for anchor in ('id="ntStep3"', 'id="ntS3Forbidden"', 'id="ntS3TblWrap"',
                   'id="ntS3Head"', 'id="ntS3Rows"', 'id="ntS3Pop"',
                   'id="ntS3PopList"'):
        assert anchor in src, f"3-qadam ankeri yo'q: {anchor}"


def test_step3_forbidden_screen_uses_uzums_own_wording():
    """SKU yo'q ekrani matnlari bandl `filters_forbidden` blokidan
    (ru @366064 / uz @429161) — o'zim tarjima qilmadim.

    ⚠️ `_markup()` SHART: bu matnlar izohda ham uchraydi, xom matnga assert
    qilsak yolg'on musbat bo'lardi (handoff §11 №2 tuzog'i)."""
    src = _markup(TEMPLATE)
    # ru — bandl bilan so'zma-so'z
    assert '"Вы не можете добавить свойства товаров без SKU"' in src
    assert '"Чтобы добавить свойства товара, вернитесь на предыдущий шаг и создайте SKU товара"' in src
    assert '"Назад к «Цены и SKU»"' in src
    # uz — Uzumning O'Z o'zbekchasi (men «SKU yo'q...» deb o'ylab topmadim)
    assert '"SKU kodsiz mahsulot xususiyatlarini qoʻsha olmaysiz"' in src


def test_step3_intro_is_uzums_own_wording():
    """Kirish matni bandl `usage_of_product_properties` + `add_properties_info`
    (ru @363208 / uz @426280) — so'zma-so'z."""
    src = _markup(TEMPLATE)
    assert '"Свойства товара используются для фильтрации товаров при поиске, в каталоге и подборках."' in src
    assert '"Добавьте свойства и укажите их значения. Благодаря этому покупателям будет проще найти именно ваш товар."' in src
    assert '"Mahsulot xususiyatlari"' in src   # product_properties (uz)


def test_step3_cell_placeholders_are_uzums_own_not_invented():
    """Katak matnlari bandl `QE` obyektidan (@2017666, ru+uz bitta joyda).

    ⚠️ Bu JS'da (NT_S3) — `_read(JS)`, chunki katak DOM'da quriladi."""
    js = _read(JS)
    for pair in (('Qiymatni tanlang', 'Выберите значение'),
                 ('Qiymatlarni tanlang', 'Выберите значения'),
                 ('Raqamni kiriting', 'Введите число'),
                 ('maks. qiymatlar:', 'макс. значений:')):
        assert f"'{pair[0]}'" in js and f"'{pair[1]}'" in js, \
            f"Uzum katak matni so'zma-so'z emas: {pair}"
    # O'ylab topgan variantlar QAYTIB KELMASIN
    for invented in ("Qiymat tanlang", "Qiymatlarni kiriting", "maks qiymat"):
        assert f"'{invented}'" not in js, f"o'ylab topilgan matn: {invented}"


def test_step3_never_talks_to_uzum_directly():
    """Token brauzerga chiqmaydi — 3-qadam ham faqat /noviy-tavar/api/* orqali.

    ⚠️ Uzum HOSTLARINI bu yerda tekshirmaymiz — `test_browser_js_never_calls_
    uzum_directly` allaqachon «uzum.uz» ni butun JS bo'yicha qamragan. Bu yerda
    yalang'och «assortment» so'ziga assert qilsam YOLG'ON MUSBAT bo'lardi: u
    modul izohida bor (handoff §11 №2 tuzog'i)."""
    js = _read(JS)
    # ⚠️ CHEGARA SHART: yalang'och `path in js` YETARLI EMAS — «/step3X» ham
    # «/step3» ni o'z ichiga oladi va typo test'dan o'tib ketardi (buni
    # MUTATSION test ochdi). Yo'ldan keyin ?/quote kelishini talab qilamiz.
    for path in ("/noviy-tavar/api/step3", "/noviy-tavar/api/attr-enums",
                 "/noviy-tavar/api/save-filters"):
        assert re.search(re.escape(path) + r"['\"?]", js), f"{path} chaqirilmaydi"


def test_step3_css_stays_scoped_to_the_feature():
    """3-qadam CSS'i boshqa sahifalarga sizib chiqmasin."""
    css = _read(CSS)
    for sel in (".nt-s3-forbidden", ".nt-s3-combo", ".nt-s3-pop", ".nt-s3-bool",
                ".nt-s3-num", ".nt-s3-opt", ".nt-s3-th-sub"):
        for m in re.finditer(re.escape(sel) + r"[\s,{:]", css):
            line_start = css.rfind("\n", 0, m.start()) + 1
            line = css[line_start:m.end()]
            assert '[data-page="noviy-tavar"]' in line, \
                f"{sel} qoidasi [data-page] ostida qulflanmagan: {line!r}"


def test_assortment_gateway_prefix_trap_is_documented():
    """⚠️ Gateway `/public/api/v1` ni O'ZI qo'shadi — client.py yo'llari
    PREFIKSSIZ bo'lishi SHART. Bandldagi to'liq yo'lni yuborsang 404
    («/public/api/v1/public/api/v1/...» ikkilangan — jonli o'lchangan)."""
    src = _read(FEATURE / "client.py")
    assert '_ASSORT_BASE = "https://api.uzum.uz/api/assortment/v1"' in src
    # attr_enums yo'li /public/api/v1 PREFIKSISIZ
    m = re.search(r'def attr_enums\(.*?\n(.*?)\n\ndef ', src, re.S)
    assert m, "attr_enums topilmadi"
    assert "/attributes/enums?attrCode=" in m.group(1)
    assert "/public/api/v1" not in m.group(1), "gateway prefiksi ikkilanadi -> 404"


def test_assortment_reuses_bearer_admin_token_not_raw():
    """Assortment `public` mijoz brauzer Bearer tokenini ishlatadi (FBS
    seller-openapi XOM tokeni EMAS) — bandl `hX()` @3421139 tasdiqladi.

    Amalda: attr_enums `_req()` orqali ketadi, u `_headers()` da
    `Bearer <token>` qo'yadi. Ya'ni alohida auth sxemasi yaratilmagan."""
    src = _read(FEATURE / "client.py")
    m = re.search(r'def attr_enums\(.*?\n(.*?)\n\ndef ', src, re.S)
    assert m and '_req(' in m.group(1), "attr_enums _req() orqali ketmaydi"


def test_step3_forbidden_shown_only_when_no_sku():
    """«taqiq» ekrani FAQAT SKU yo'q bo'lganda — Uzumdek. SKU bo'lsa jadval."""
    js = _read(JS)
    m = re.search(r"function s3Render\(\)\s*\{(.*?)\n  \}", js, re.S)
    assert m, "s3Render topilmadi"
    body = m.group(1)
    # SKU bor bo'lsa taqiq YASHIRILADI, jadval KO'RSATILADI
    assert "S3.sku.length" in body and "S3.attrs.length" in body
    assert "forb.hidden = hasSku" in body


def test_step3_empty_value_serializes_to_null():
    """⚠️ Bandl `$T()` (@605568): bo'sh qiymat -> `null` (yuborilmaydi).
    Bo'sh enum'ni `{value:null}` emas, butun atributni null qilib
    yuborish — Uzumning O'Z semantikasi. `RE()` (@2011646) buni AYNAN
    shunday qiladi."""
    js = _read(JS)
    assert "function s3IsEmpty(" in js and "function s3Serialize(" in js
    m = re.search(r"function s3Serialize\(.*?\n(.*?)\n  \}", js, re.S)
    assert m and "return null" in m.group(1), \
        "bo'sh qiymat null'ga serializatsiya qilinmaydi"


# ═══════════════════════════════════════════════════════════════════
#  DO'KON TANLAGICH (SellerHub'ga xos: bir user KO'P do'kon)
#  Ildiz sabab: kartochka `state.shop` do'koniga yaratiladi; ilgari do'kon
#  jimgina `shops[0]` ga o'rnatilardi va almashtirish UI'si yo'q edi ->
#  ko'p do'konli userda noto'g'ri do'konga ketishi mumkin edi.
# ═══════════════════════════════════════════════════════════════════


def test_shop_picker_is_guarded_by_multi_shop():
    """Do'kon kartasi FAQAT >1 do'konda render qilinadi (bitta do'konda
    tanlov shart emas). Select shart-guardining ICHIDA bo'lishi kerak."""
    m = _markup(TEMPLATE)
    guard = re.search(r"\{%\s*if\s+shops\s*\|\s*length\s*>\s*1\s*%\}", m)
    assert guard, "shops>1 shart-guardi yo'q"
    sel = m.find('id="ntShopSelect"')
    assert sel != -1, "ntShopSelect topilmadi"
    assert guard.end() < sel, "select shart-guardidan tashqarida"
    assert m.find("{% endif %}", sel) != -1, "guard yopilmagan"


def test_shop_picker_iterates_all_user_shops():
    """Har do'kon <option> bo'lib chiqadi (qat'iy ro'yxat emas)."""
    m = _markup(TEMPLATE)
    assert re.search(r"\{%\s*for\s+s\s+in\s+shops\s*%\}", m), "shops sikli yo'q"
    assert re.search(r'value="\{\{\s*s\.uzum_id\s*\}\}"', m), "option value yo'q"
    assert re.search(r"\{\{\s*s\.name\s+or\s+s\.uzum_id\s*\}\}", m), "option label yo'q"


def test_shop_picker_i18n_is_verbatim_uz_ru():
    """Sarlavha + maslahat matni ikkala tilda (invented emas)."""
    m = _markup(TEMPLATE)
    assert "Do'kon" in m and "Магазин" in m
    assert "Kartochka tanlangan do'konda yaratiladi" in m
    assert "Карточка создаётся в выбранном магазине" in m


def test_shop_picker_reuses_category_combo_styles():
    """Yangi CSS emas — kategoriya combo'sining .nt-select-wrap/.nt-select
    uslublarini QAYTA ishlatadi (CSS scope testiga yangi selektor qo'shmaydi)."""
    m = _markup(TEMPLATE)
    start = m.find('id="ntCardShop"')
    assert start != -1, "ntCardShop topilmadi"
    block = m[start:start + 700]
    assert 'class="nt-select-wrap"' in block and 'class="nt-select"' in block
    # `.nt-shop*` uslubi CSS'ga qo'shilmagan bo'lsin (reuse tasdig'i).
    assert ".nt-shop" not in _read(CSS), "kutilmagan .nt-shop CSS selektori"


def test_shop_change_updates_state_shop():
    """Do'kon almashtirilsa state.shop yangilanadi (create shu do'konga ketadi).

    ⚠️ Tekshiruv AYNAN change-handler ICHIDA bo'lishi kerak: init blokida
    `if (sel.value) state.shop = sel.value;` ham bor -> butun blokka assert
    qilsak yolg'on-musbat chiqadi (mutatsion test buni ochdi)."""
    js = _read(JS)
    # `sel` o'zgaruvchisi faylda ko'p joyda bor (RTE ham) -> avval ntShopSelect
    # init blokini NOYOB anchor bilan ajratamiz.
    init = re.search(
        r"var sel = document\.getElementById\('ntShopSelect'\);(.*?)\n  \}\)\(\);",
        js, re.S)
    assert init, "ntShopSelect init bloki topilmadi"
    body = init.group(1)
    assert "addEventListener('change'" in body, "change tinglovchisi yo'q"
    # AYNAN change-handler ICHIDA (addEventListener'DAN KEYIN) — init-sync
    # qatori `if (sel.value) state.shop = sel.value;` avvalroq keladi.
    assert re.search(r"addEventListener\('change'.*?state\.shop\s*=\s*sel\.value",
                     body, re.S), "change-handler state.shop'ni yangilamaydi"


def test_shop_locked_after_draft_created():
    """Qoralama yaratilgach do'kon o'zgarmaydi — draft do'konga qat'iy
    bog'langan. s2Show(true) select'ni o'chiradi."""
    js = _read(JS)
    m = re.search(r"function s2Show\(on\)\s*\{(.*?)\n  \}", js, re.S)
    assert m, "s2Show topilmadi"
    body = m.group(1)
    assert re.search(r"getElementById\('ntShopSelect'\)", body) and \
        re.search(r"disabled\s*=\s*true", body), \
        "draft yaratilgach do'kon qulflanmaydi"
