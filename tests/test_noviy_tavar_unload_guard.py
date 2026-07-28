"""«Новый товар» — SAQLANMAGAN MEHNAT QO'RIQCHISI (`beforeunload`).

MAQSAD (foydalanuvchi talabi 2026-07-22): karta yaratilayotganda bilmasdan
F5 bosilsa, brauzer «orqaga» tugmasi bosilsa yoki oyna yopilsa — brauzerning
o'z tasdiq oynasi chiqsin, ish bir zumda yo'qolib ketmasin. HAR UCH bosqichda.

⚠️ Bu qoralama (draft) mexanizmini ALMASHTIRMAYDI, ustiga qo'shiladi:
   · 1-qadam — localStorage qoralamasi bor, lekin ogohlantirish baribir kerak
     (foydalanuvchi tiklanishiga ishonib turishi shart emas);
   · 2/3-qadam — qoralama YO'Q, narx/SKU/xususiyat faqat Uzumda. Bu yerda
     yangilash tahrirlarni BUTUNLAY yo'q qiladi — ogohlantirish yagona himoya.

Shovqin (bezor qiluvchi oyna) ham xato hisoblanadi: yo'qotadigan narsa
bo'lmaganda `beforeunload` JIM chiqishi kerak.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "noviy_tavar" / "static" / "noviy_tavar.js"
TEMPLATE = ROOT / "noviy_tavar" / "templates" / "noviy_tavar.html"


@pytest.fixture(scope="module")
def js() -> str:
    return JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def tpl() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def _fn(js: str, name: str, size: int = 900) -> str:
    return js[js.index("function " + name) :][:size]


# ── Qo'riqchining o'zi ───────────────────────────────────────────────


def test_guard_functions_exist(js):
    for fn in ("function ntHasUnsaved", "function ntSnapshot", "function ntSig"):
        assert fn in js, f"{fn} yo'q"


def test_beforeunload_prompts_only_when_dirty(js):
    """Ogohlantirish SHARTLI: `ntHasUnsaved()` false bo'lsa oyna chiqmasin.
    (2026-07-23: shartga `ntBypassGuard` qo'shildi — «Chiqish» tasdiqlangach
    nativ oyna qayta chiqmasin.)"""
    i = js.index("window.addEventListener('beforeunload'")
    body = js[i : i + 700]
    assert "ntHasUnsaved()" in body, "shart tekshirilmayapti — oyna DOIM chiqadi"
    assert "if (ntBypassGuard || !ntHasUnsaved()) return" in body


def test_beforeunload_sets_both_signals(js):
    """Chrome `returnValue` ni, Firefox `preventDefault()` ni talab qiladi —
    bittasi yetmaydi, aks holda ba'zi brauzerda oyna umuman chiqmaydi."""
    i = js.index("window.addEventListener('beforeunload'")
    body = js[i : i + 700]
    assert "e.preventDefault()" in body
    assert "e.returnValue" in body


def test_draft_still_flushed_before_unload(js):
    """⚠️ Regressiya qo'riqchisi: qo'riqchi qo'shilganda qoralamani diskka
    tushirish (`saveDraftFlush`) tushib qolmasin — u yo'qolsa oxirgi 250ms
    ichida terilgan matn yo'qolardi."""
    i = js.index("window.addEventListener('beforeunload'")
    assert "saveDraftFlush()" in js[i : i + 400]
    # va u SHARTDAN oldin bo'lsin — «bekor qilish» bosilsa ham yozilgan bo'lsin
    body = js[i : i + 700]
    assert body.index("saveDraftFlush()") < body.index("ntHasUnsaved()")


# ── 1-qadam: imzo taqqoslash ─────────────────────────────────────────


def test_step1_uses_signature_not_event_flag(js):
    """1-qadamdagi ko'p o'zgarish KLIK bilan bo'ladi (rasm, xususiyat,
    kategoriya) — ular `input`/`change` bermaydi. Shuning uchun butun
    holat imzosi (`collectDraft`) taqqoslanadi."""
    body = _fn(js, "ntSig", 300)
    assert "collectDraft()" in body
    assert "d.ts = 0" in body, "vaqt tamg'asi imzodan chiqarilmagan — DOIM iflos bo'ladi"


def test_step1_clean_points_are_snapshotted(js):
    """XAVFSIZ nuqtalar: bo'sh forma va Uzumga saqlangan karta.
    (2026-07-24: tiklangan qoralama endi xavfsiz nuqta EMAS — pastdagi
    `test_restored_draft_is_NOT_snapshotted` ga qara.)"""
    assert js.count("ntSnapshot()") >= 3, "xavfsiz nuqtalarning bir qismi belgilanmagan"
    # Uzumga saqlangandan keyin
    i = js.index("state.cardFilled = true;")
    assert "ntSnapshot()" in js[i : i + 200], "create/update dan keyin toza deb belgilanmagan"
    # Bo'sh forma: tiklanadigan narsa bo'lmasa (`restoreDraft()` false) snapshot.
    i2 = js.index("if (!restoreDraft()) ntSnapshot()")
    assert i2 > 0, "bo'sh forma xavfsiz nuqta sifatida belgilanmagan"


def test_restored_draft_is_NOT_snapshotted(js):
    """⚠️ FOYDALANUVCHI TALABI 2026-07-24: tiklangan qoralama «toza» deb
    BELGILANMASIN. Forma to'la bo'lsa, chiqishda ogohlantirish CHIQSIN —
    localStorage'da nusxa borligiga qaramay («soddagina ogohlantirish
    chiqadigan bo'lsin»). Aks holda F5 dan keyin to'la forma jimgina
    yo'qolib ketardi (jonli isbotlangan: guardPrevented=false)."""
    # Tiklash mantiqi `applyDraftObject` ga ko'chdi (restoreDraft + s1Load
    # ulashadi). restoreDraft uni FAQAT notify bilan chaqiradi — cardFilled YO'Q
    # → markFilled false → snapshot yo'q → tiklangan draft «iflos» (ogohlantiradi).
    r = js.index("function restoreDraft")
    rbody = js[r : js.index("return true;", r)]   # faqat restoreDraft tanasi (applyDraftObject izohisiz)
    assert "applyDraftObject(d, { notify:" in rbody
    assert "cardFilled" not in rbody, "restoreDraft cardFilled bermasligi kerak (draft toza bo'lib qolardi)"
    # applyDraftObject: snapshot FAQAT markFilled (tahrirlash) bo'lsa.
    a = js.index("function applyDraftObject")
    abody = js[a : a + 2600]
    assert "if (markFilled) { state.cardFilled = true; ntSnapshot(); }" in abody


def test_unknown_baseline_errs_towards_warning(js):
    """Xavfsiz nuqta hali belgilanmagan bo'lsa (ildizlar kelmadi/xato) —
    shubhada FOYDALANUVCHI foydasiga: mazmun bo'lsa ogohlantiramiz."""
    body = _fn(js, "ntHasUnsaved", 1200)
    assert "ntCleanSig === null" in body
    assert "draftHasContent" in body


# ── 2/3-qadam: bayroq ────────────────────────────────────────────────


def test_step23_marked_dirty_on_edit(js):
    """2/3-qadamda qoralama yo'q — har tahrir bayroq ko'tarsin."""
    i = js.index("['input', 'change'].forEach")
    body = js[i : i + 400]
    assert "ntDirty[state.step] = true" in body


def test_step3_dropdown_writes_mark_dirty(js):
    """3-qadam qiymatlari `s3Set()` orqali o'tadi (dropdown/chip — `change`
    hodisasi bermaydi), `s3Load` esa `S3.values` ni to'g'ridan-to'g'ri
    to'ldiradi — demak bayroq aynan `s3Set` da bo'lishi kerak."""
    body = _fn(js, "s3Set", 500)
    assert "ntDirty[3] = true" in body


def test_saved_steps_become_clean(js):
    """Uzumga saqlangach ogohlantirish chiqmasin."""
    i2 = js.index("function s2Send")
    assert "ntDirty[2] = false" in js[i2 : i2 + 3000], "SKU saqlangach bayroq tushmayapti"
    i3 = js.index("function s3Send")
    assert "ntDirty[3] = false" in js[i3 : i3 + 3000], "xususiyat saqlangach bayroq tushmayapti"


def test_reloaded_steps_become_clean(js):
    """Qadam serverdan qayta tortilganda ham toza — nishon orqali borib
    kelgan foydalanuvchiga bo'sh joydan ogohlantirish chiqmasin."""
    for fn, flag in (("s2Enter", "ntDirty[2] = false"), ("s3Enter", "ntDirty[3] = false")):
        assert flag in _fn(js, fn, 700), f"{fn} toza holatga qaytarmayapti"


# ── O'z dizaynli «sahifadan chiqish» modali (2026-07-23) ─────────────
#
# ⚠️ HALOL CHEKLOV: nativ `beforeunload` oynasi (F5/tab yopish/manzil satri)
# almashtirib bo'lmaydi — brauzer bloklaydi. Custom modal FAQAT ilova ichidagi
# o'tishlarda (havola klik + «orqaga») ko'rsatiladi. Nativ oyna zaxira qoladi.


def test_leave_modal_markup_exists(tpl):
    for el in ("ntLeaveModal", "ntLeaveTitle", "ntLeaveDesc", "ntLeaveOk", "ntLeaveCancel"):
        assert 'id="%s"' % el in tpl, el + " yo'q"
    # Nativ oynadan olingan ohang (Изменения могут не сохраниться).
    assert "Изменения могут не сохраниться." in tpl
    assert "Kiritilgan maʼlumotlar saqlanmasligi mumkin." in tpl
    # Chiqish/Qolish tugmalari.
    assert "Выйти" in tpl and "Chiqish" in tpl
    assert "Остаться" in tpl and "Qolish" in tpl


def test_internal_link_clicks_are_intercepted(js):
    """Chap menyu/logotip/breadcrumb havolasi bosilsa — o'z modalimiz, nativ
    oyna EMAS. Faqat haqiqiy o'tish + saqlanmagan mehnat bo'lganda."""
    i = js.index("// ── 1. Ichki navigatsiya")
    body = js[i : i + 2200]
    # Oddiy <a href> HAM, yon-panelning [data-href] elementlari HAM ushlansin.
    assert "closest('a[href], [data-href]')" in body
    assert "getAttribute('data-href')" in body
    assert "e.preventDefault()" in body
    assert "showLeaveModal({ type: 'href'" in body
    # Faqat iflos holatda.
    assert "if (!ntHasUnsaved()) return" in body
    # capture-fazada (o'z havola ishlovchilaridan oldin).
    assert "}, true);" in body


def test_datahref_nav_stops_propagation(js):
    """⚠️ JONLI ISBOTLANGAN XATO 2026-07-24: yon-panel (`uzum-nav-item`)
    href'siz — `uzum_ui.js` uni `[data-href]` bilan almashtirib, BUBBLE
    fazasida `location.assign()` qiladi. Biz capture'da to'xtatmasak (faqat
    `preventDefault` yetmaydi), u baribir navigatsiya qilib NATIV oynani
    chiqarardi. `stopPropagation()` SHART."""
    i = js.index("// ── 1. Ichki navigatsiya")
    body = js[i : i + 2200]
    assert "e.stopPropagation()" in body, "uzum_ui.js data-href navigatsiyasi to'xtatilmayapti"


def test_link_guard_skips_non_navigations(js):
    """Hash (#), yangi tab (target=_blank), download, Ctrl/Cmd+klik, ayni
    sahifa — ushlanmasligi SHART (aks holda modal bekorga chiqadi)."""
    i = js.index("// ── 1. Ichki navigatsiya")
    body = js[i : i + 2200]
    assert "raw.charAt(0) === '#'" in body
    assert "a.target !== '_self'" in body
    assert "a.hasAttribute('download')" in body
    assert "e.metaKey || e.ctrlKey" in body
    # Ayni sahifa (faqat hash) — o'tish emas.
    assert "dest.pathname === location.pathname" in body


def test_back_button_intercepted_via_history_sentinel(js):
    """«Orqaga» tugmasi — history sentinel bilan ushlanadi; toza bo'lsa
    o'tishga ruxsat, iflos bo'lsa modal."""
    i = js.index("// ── 2. «Orqaga»")
    body = js[i : i + 900]
    assert "history.pushState(null, '', location.href)" in body
    assert "'popstate'" in body
    assert "if (!ntHasUnsaved()) { history.back(); return; }" in body
    assert "showLeaveModal({ type: 'back' })" in body


def test_confirm_leave_bypasses_native_guard(js):
    """«Chiqish» bosilgach nativ oyna qayta chiqmasin (ntBypassGuard) va
    kutilgan manzilga o'tsin."""
    body = _fn(js, "doLeave", 400)
    assert "ntBypassGuard = true" in body
    assert "window.location.href = p.url" in body
    assert "history.go(-2)" in body, "back-holatda sentinel + joriy sahifadan o'tilmayapti"


def test_native_guard_is_kept_as_fallback(js):
    """Nativ `beforeunload` OLIB TASHLANMAGAN — F5/tab yopish uchun zaxira."""
    assert "window.addEventListener('beforeunload'" in js
    # Izohda cheklov aniq yozilgan bo'lsin (kelajakda «nega custom emas» savoli).
    assert "NATIV" in js and "bloklaydi" in js
