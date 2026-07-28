"""«Новый товар» 1-bosqich QORALAMASI (draft) — manba shartnomalari.

MAQSAD (foydalanuvchi talabi 2026-07-21): karta yaratilayotganda F5 bosilsa,
brauzer «orqaga» tugmasi bosilsa yoki brauzer yopilib qayta ochilsa —
to'ldirilgan forma YO'QOLMASIN.

Naqsh postavki.html:3153 (`saveCreateDraftNow`/`restoreCreateDraft`) dan.

⚠️ Qoralama KESH EMAS: `nt.draft.*` va `nt.cache.*` boshqa-boshqa. Katalog
keshi tozalansa qoralama o'lmasligi kerak — shuning uchun prefikslar ajratilgan.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "noviy_tavar" / "static" / "noviy_tavar.js"


@pytest.fixture(scope="module")
def js() -> str:
    return JS.read_text(encoding="utf-8")


# ── Saqlash ──────────────────────────────────────────────────────────


def test_draft_functions_exist(js):
    for fn in ("function saveDraftNow", "function restoreDraft",
               "function clearDraft", "function collectDraft"):
        assert fn in js, f"{fn} yo'q"


def test_draft_key_is_per_shop(js):
    """Har do'konning O'Z qoralamasi — aks holda do'kon almashganda yot
    forma tirilardi (postavki `createDraftKey()` bilan bir xil qoida)."""
    start = js.index("function draftKey")
    assert "state.shop" in js[start:start + 200]


def test_draft_prefix_is_own(js):
    """Qoralama O'Z prefiksida (`nt.draft.`) — UI bayroqlari (`nt.tipsHidden`)
    bilan aralashmasin va hech kim uni yopiq holda o'chirmasin."""
    assert "'nt.draft.'" in js
    # localStorage.clear() BUTUN saytning holatini (postavki qoralamalarini
    # ham) o'chirardi — bu modulda umuman ishlatilmasligi kerak.
    assert "localStorage.clear()" not in js


def test_draft_is_debounced(js):
    """Har harf terishda diskka yozmaymiz (postavki: 250ms)."""
    start = js.index("function saveDraft(")
    assert "setTimeout" in js[start:start + 300]


def test_draft_flushed_before_unload(js):
    """Debounce kutayotgan yozuv sahifa yopilganda YO'QOLMASIN."""
    assert "pagehide" in js and "beforeunload" in js
    assert "saveDraftFlush" in js


def test_empty_form_is_not_saved(js):
    """Bo'sh forma yozilmasin — aks holda har ochilishda keraksiz yozuv
    va «tiklandi» xabari chiqardi."""
    assert "function draftHasContent" in js
    start = js.index("function saveDraftNow")
    body = js[start:start + 800]
    assert "draftHasContent" in body
    assert "removeItem" in body, "bo'sh bo'lsa eski qoralama o'chirilmayapti"


def test_save_is_locked_until_restore_finishes(js):
    """TARTIB QULFI — jonli uchragan xato (2026-07-21).

    Sahifa ochilganda forma BO'SH, tiklash esa ildizlar yuklangach (~400ms)
    boshlanadi. Qulfsiz: bo'sh formaning 250ms debounce'i tiklashdan OLDIN
    ishga tushib, `draftHasContent` false bo'lgani uchun qoralamani
    O'CHIRIB yuborardi — foydalanuvchi butun mehnatini yo'qotardi.
    """
    assert "draftReady" in js, "tartib qulfi yo'q"
    start = js.index("function saveDraftNow")
    assert "if (!draftReady) return" in js[start:start + 300], (
        "saqlash tiklash tugashini kutmayapti"
    )


def test_restore_unlocks_saving_on_every_exit(js):
    """`restoreDraft()` HAR chiqish yo'lida qulfni ochishi kerak — aks holda
    saqlash butunlay o'lik qoladi (qoralama yo'q / buzuq / meta kelmadi)."""
    start = js.index("function restoreDraft")
    body = js[start:js.index("function repaintMedia")]
    # har `return false` dan oldin qulf ochilgan bo'lsin
    assert body.count("markDraftReady()") >= 5, (
        f"chiqish yo'llarining bir qismi qulfni ochmayapti "
        f"({body.count('markDraftReady()')} ta topildi)"
    )


def test_save_lock_has_safety_timeout(js):
    """Ildizlar tortilishi qotib qolsa ham saqlash abadiy o'chmasin."""
    assert "setTimeout(markDraftReady" in js, "zaxira timeout yo'q"


def test_draft_not_saved_on_step2(js):
    """2-qadamda qoralama Uzumda yaratilgan — lokal nusxa endi zararli."""
    start = js.index("function saveDraftNow")
    assert "state.step !== 1" in js[start:start + 600]


# ── Tiklash ──────────────────────────────────────────────────────────


def test_draft_has_version_and_ttl(js):
    assert "NT_DRAFT_VER" in js
    assert "NT_DRAFT_TTL" in js
    start = js.index("function restoreDraft")
    body = js[start:start + 600]
    assert "d.v !== NT_DRAFT_VER" in body, "versiya tekshirilmayapti"
    assert "NT_DRAFT_TTL" in body, "eskirish tekshirilmayapti"


def test_restore_rebuilds_category_context(js):
    """Kategoriyasiz xususiyat/filtr qura olmaymiz — meta qayta olinishi kerak."""
    start = js.index("function restoreDraft")
    body = js[start:start + 4500]
    for fn in ("fetchCategoryMeta", "buildCharOptions", "buildFilters",
               "showDoneMode", "renderRows"):
        assert fn in body, f"tiklashda {fn} chaqirilmayapti"


def test_filters_restored_after_buildFilters(js):
    """⚠️ `buildFilters()` `state.filterValues` ni TOZALAYDI — filtrlarni
    undan KEYIN tiklash shart, aks holda tanlov yo'qoladi."""
    start = js.index("function applyDraftObject")
    body = js[start:start + 3000]
    i_build = body.index("buildFilters(meta)")
    i_restore = body.index("state.filterValues[f.fid]")
    assert i_restore > i_build, "filtrlar buildFilters'dan OLDIN tiklanyapti"


def test_restore_skipped_when_already_on_step2(js):
    """URL'da productId bo'lsa foydalanuvchi 2-qadamda — 1-qadam
    qoralamasini tiklash noto'g'ri."""
    # ⚠️ Birinchi uchrash — funksiyaning O'ZI (`function restoreDraft()`).
    # Bizga CHAQIRUV joyi kerak — u ildizlar yuklangач ishlaydi.
    i = js.index("restoreDraft()", js.index("fetchCategories(null).then"))
    window = js[max(0, i - 600):i + 200]
    assert "productId" in window


def test_custom_characteristic_survives_restore(js):
    """Maxsus xususiyat kategoriya meta'sida YO'Q — nomi bilan saqlanib,
    tiklashda qayta yaratilishi kerak."""
    start = js.index("function collectDraft")
    assert "custom" in js[start:start + 1800]
    start2 = js.index("function applyDraftObject")
    assert "sr.custom" in js[start2:start2 + 3000]


# ── Tozalash ─────────────────────────────────────────────────────────


def test_selected_shop_is_remembered(js):
    """⚠️ JONLI UCHRAGAN XATO (2026-07-22, mixbox).

    Qoralama kaliti do'kon bo'yicha (`nt.draft.<shop>`), sahifa esa
    yangilanganda `state.shop` ni jimgina `shops[0]` ga qaytarardi — natijada
    BIRINCHIDAN BOSHQA do'konda F5 bosilsa, tiklash YO'Q do'konning kalitiga
    qarab bo'sh forma ko'rsatardi va mehnat «yo'qolgan»dek tuyulardi.
    """
    i = js.index("var sel = document.getElementById('ntShopSelect')")
    body = js[i:i + 2200]
    assert "localStorage.getItem('nt.shop')" in body, "tanlangan do'kon eslanmayapti"
    assert "localStorage.setItem('nt.shop'" in body, "tanlov yozilmayapti"
    # URL ustunroq — `?shop=` / qoralama URL'i eslab qolinganini bosib o'tsin
    assert "fromUrl" in body, "?shop= ustunligi yo'q"


def test_draft_cleared_after_successful_create(js):
    """Uzumda yaratilgach lokal qoralama o'chsin — aks holda keyingi
    «Yangi tovar» ochilishida eski forma tirilib chiqardi."""
    i = js.index("s2Enter(res.d.id)")
    window = js[max(0, i - 500):i]
    assert "ntClearDraft" in window, "create muvaffaqiyatidan keyin tozalanmayapti"
