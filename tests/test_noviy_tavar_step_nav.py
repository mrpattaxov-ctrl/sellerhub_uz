"""«Новый товар» — qadam nishonlari (bosiladi) + kartani YANGILASH (editProduct).

Nega mocked: `editProduct` HAQIQIY kartani o'zgartiradi. Foydalanuvchi qoidasi —
yozuvchi endpointni prodda sinashdan oldin mocked test yozish. Bu faylda
Uzum'ga BITTA ham so'rov ketmaydi.

DALIL (Uzum portal bandli, t12.har bilan tasdiqlangan):
  · qadam nishonlari — `chunk-2e85dd89` @52129:
        [card /edit, prices /edit/sku/all, property /edit/filters]
        (t === joriy || yuklanmoqda)  -> link = ""      (bosilmaydi)
        !isEdit && t > joriy          -> link = ""      (oldinga sakrash yo'q)
  · saqlash — `chunk-6dbbb9d8` @79125:
        isEdit ? editProduct(shopId, {...v}) : createProduct(shopId, v, {...})
        so'ng ikkalasida ham `push(/{shop}/products/id/{id}/edit/sku/all)`
  · endpoint — `mf-products` @3234674:
        canEditProduct -> POST /product/editable?productId=
        editProduct    -> POST /product/editProduct
"""
from __future__ import annotations

from pathlib import Path

import pytest

from noviy_tavar import client
from noviy_tavar.client import build_create_body, build_edit_body

FEATURE = Path(__file__).resolve().parent.parent / "noviy_tavar"
JS = FEATURE / "static" / "noviy_tavar.js"
CSS = FEATURE / "static" / "noviy_tavar.css"
TEMPLATE = FEATURE / "templates" / "noviy_tavar.html"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _kwargs(**kw):
    base = dict(
        category_id=11937,
        title_uz="G'ilof", title_ru="Чехол",
        short_uz="qisqa", short_ru="кратко",
        desc_uz="<p>tavsif</p>", desc_ru="<p>описание</p>",
        filter_values_sel=[],
        characteristics_sel=[],
        images=[{"key": "abc", "url": "https://images.uzum.uz/abc/original.jpg"}],
        product_fields={},
    )
    base.update(kw)
    return base


# ── build_edit_body ──────────────────────────────────────────────────


def test_edit_body_carries_the_product_id():
    """`id` bo'lmasa Uzum qaysi kartani yangilashni bilmaydi."""
    body = build_edit_body(product_id=2885781, current={}, **_kwargs())
    assert body["id"] == 2885781


def test_edit_body_keeps_existing_skus():
    """⚠️ ENG MUHIM: bo'sh `skuList` yuborilsa 2-qadamda yaratilgan SKU'lar
    yo'qolib ketishi mumkin — ular joriy kartadan ko'chirilishi shart."""
    skus = [{"skuTitle": "ПРОЗР-14", "skuFullTitle": "LUXUZ-CHEHOL-ПРОЗР-14"}]
    body = build_edit_body(product_id=1, current={"skuList": skus}, **_kwargs())
    assert body["skuList"] == skus
    # Yaratish tanasi esa doim bo'sh ro'yxat bilan ketadi (yangi kartada SKU yo'q)
    assert build_create_body(**_kwargs())["skuList"] == []


def test_edit_body_carries_moderation_traces():
    """Moderatsiya/blok izlari formadan kelmaydi — joriy kartadan olinadi."""
    current = {
        "createdByFlowB": True,
        "dateModerated": 1784600421625,
        "blockReason": "sabab",
        "blockReasons": ["x"],
        "hasAnySkuBlocked": True,
        "allFiltersFilled": True,
        "categoryEditable": False,
    }
    body = build_edit_body(product_id=7, current=current, **_kwargs())
    for key, val in current.items():
        assert body[key] == val, f"{key} joriy kartadan ko'chirilmadi"


def test_edit_body_is_the_create_body_plus_carried_keys():
    """Tana AYNAN create tanasi — bandl ikkala endpointga bitta obyekt yuboradi."""
    created = build_create_body(**_kwargs())
    edited = build_edit_body(product_id=5, current={}, **_kwargs())
    assert set(edited) - set(created) == {"id"}
    for key in created:
        if key in client._EDIT_CARRY_KEYS:
            continue
        assert edited[key] == created[key], f"{key} yaratish tanasidan farq qildi"


def test_edit_body_form_fields_win_over_current():
    """Foydalanuvchi o'zgartirgan maydon joriy kartadagi eskisini bosib o'tsin."""
    body = build_edit_body(
        product_id=5,
        current={"title": {"uz": "eski", "ru": "старое"}, "skuList": []},
        **_kwargs(title_uz="yangi", title_ru="новое"),
    )
    assert body["title"] == {"uz": "yangi", "ru": "новое"}


# ── Endpoint manzillari (tarmoq mock qilingan) ───────────────────────


def test_edit_product_hits_the_bundle_endpoint(monkeypatch):
    seen = {}

    def fake_req(method, url, *, json_body=None, timeout=30, lang=None):
        seen.update(method=method, url=url, body=json_body, timeout=timeout)
        return {"id": 42}

    monkeypatch.setattr(client, "_req", fake_req)
    res = client.edit_product(5983, {"id": 42, "title": {"uz": "x", "ru": "x"}})
    assert res == {"id": 42}
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/5983/product/editProduct")
    # createProduct'ning `?testVariant=` si YANGILASHDA yo'q
    assert "testVariant" not in seen["url"]
    assert seen["body"]["id"] == 42


def test_create_and_edit_are_different_endpoints(monkeypatch):
    """Yangilashda createProduct chaqirilsa — Uzumda DUBLIKAT karta paydo bo'lardi."""
    urls = []
    monkeypatch.setattr(
        client, "_req",
        lambda method, url, **kw: urls.append(url) or {"id": 1},
    )
    client.create_product(5983, {})
    client.edit_product(5983, {})
    assert "createProduct" in urls[0] and "editProduct" not in urls[0]
    assert "editProduct" in urls[1] and "createProduct" not in urls[1]


def test_can_edit_product_is_a_bodyless_post(monkeypatch):
    """Bandl `canEditProduct`: POST, tanasiz, faqat query."""
    seen = {}
    monkeypatch.setattr(
        client, "_req",
        lambda method, url, **kw: seen.update(method=method, url=url, kw=kw) or {},
    )
    client.can_edit_product(5983, 2885781)
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/5983/product/editable?productId=2885781")
    assert seen["kw"].get("json_body") is None


# ── /noviy-tavar/api/update yo'nalishi ───────────────────────────────


def test_update_route_exists_and_is_guarded():
    src = _read(FEATURE / "routes.py")
    i = src.index('@noviy_tavar_bp.post("/noviy-tavar/api/update")')
    head = src[i:i + 1600]
    assert "@login_required" in head
    assert "_can_access(shop)" in head, "do'kon egaligi tekshirilmayapti"
    assert 'return jsonify({"error": "productId kerak"}), 400' in head


def test_update_route_reads_current_card_before_writing():
    """`skuList` ko'chirilishi uchun joriy karta OLDIN o'qilishi shart."""
    src = _read(FEATURE / "routes.py")
    i = src.index('def nt_update():')
    body = src[i:i + 2000]
    assert body.index("client.get_product(") < body.index("client.edit_product(")
    assert "client.build_edit_body(" in body


def test_create_route_never_receives_a_product_id():
    """createProduct doim YANGI karta — unga productId berilmaydi."""
    src = _read(FEATURE / "routes.py")
    i = src.index("def nt_create():")
    assert "productId" not in src[i:src.index("def nt_update():")]


# ── Brauzer: qadam nishonlari ────────────────────────────────────────


def test_steps_are_real_buttons():
    """`<div>` bosilmaydi va klaviaturaga tushmaydi — Uzumda ular havola/tugma."""
    src = _read(TEMPLATE)
    for n in (1, 2, 3):
        assert f'class="nt-step{" is-active" if n == 1 else ""}" data-step="{n}"' in src \
            or f'data-step="{n}"' in src
    assert src.count('<button type="button" class="nt-step') == 3


def test_step_reachability_matches_the_bundle_rule():
    """Bandl @52129: joriy qadam bosilmaydi; `isEdit` (= karta mavjud) bo'lsa
    qolgan qadamlar ochiq; karta yo'q ekan 2/3-qadam qulf."""
    js = _read(JS)
    assert "function stepReachable(n)" in js
    assert "if (n === state.step) return false;" in js
    assert "return !!state.productId;" in js


def test_step_click_is_wired_and_respects_disabled():
    js = _read(JS)
    assert "goStep(Number(b.dataset.step || 0));" in js
    assert "if (!b || b.disabled) return;" in js


def test_step_one_reachable_when_editing_but_loads_before_showing():
    """⚠️ 2026-07-26 YANGILANDI. Ilgari ?productId= bilan ochilgan sahifada
    1-qadam QULF edi (forma bo'sh — saqlash kartani o'chirardi). Endi tahrirda
    (editingProduct) 1-qadam OCHIQ, lekin `goStep(1)` uni ko'rsatishdan OLDIN
    `s1Load` bilan serverdan YUKLAYDI — bo'sh forma umuman ko'rsatilmaydi,
    demak «ustidan saqlab o'chirish» xavfi ham yo'q. (Foydalanuvchi 3-qadamdan
    1-ga qayta olmayotgani shikoyatining tuzatilishi.)"""
    js = _read(JS)
    assert "!!state.editingProduct" in js, "tahrirda 1-qadam hali qulf"
    i = js.index("function goStep")
    body = js[i:i + 700]
    assert "if (state.productId && !state.cardFilled)" in body
    assert "s1Load(state.productId)" in body


def test_save_switches_to_update_when_a_draft_exists():
    """Bandl: `isEdit ? editProduct : createProduct` — bizda ham shu ayri."""
    js = _read(JS)
    assert "var isEdit = !!(state.productId && state.cardFilled);" in js
    assert "fetch(isEdit ? '/noviy-tavar/api/update' : '/noviy-tavar/api/create'" in js
    assert "productId: isEdit ? state.productId : undefined," in js


def test_step_dot_colours_match_the_three_reference_frames():
    """Uch referens kadr (piksel bilan o'lchangan):
        blank-step1-baseline     -> KO'K «1», kulrang «2», kulrang «3»
        sacvoyage-filled-step2   -> yashil ✓, yashil ✓, KO'K «3»
        sacvoyage-filled-step3   -> yashil ✓, yashil ✓, yashil ✓
    ya'ni yashil = bajarilgan, ko'k = ochiq, kulrang = qulflangan."""
    css = _read(CSS)
    assert "--nt-step-done: #52cf5f;" in css, "o'lchangan yashil (82,207,95)"
    assert '.nt-step:not(.is-locked) .nt-step-dot {\n  background: var(--nt-step-active);' in css
    assert ".nt-step.is-done .nt-step-dot {\n  background: var(--nt-step-done);" in css
    # eski «bajarilgan = ko'k» bekor qiluvchi qoida qaytib kelmasin
    assert ".nt-step.is-done .nt-step-dot {\n  background: var(--nt-step-active);" not in css
    js = _read(JS)
    assert "n < state.step || (n === state.step && !!state.productId)" in js


def test_disabled_steps_are_not_clickable_in_css():
    """Qulflangan qadamda qo'l kursori chiqmasin (yolg'on affordans)."""
    css = _read(CSS)
    assert '[data-page="noviy-tavar"] .nt-step:not([disabled]) { cursor: pointer; }' in css
    assert '.nt-step.is-locked { opacity: .5; }' in css


@pytest.mark.parametrize("fn", ["paintSteps", "showStep1", "goStep"])
def test_step_painting_lives_in_one_place(fn):
    """Nishonlarni har joyda qo'lda bo'yash sinxronsizlikka olib kelgan edi."""
    js = _read(JS)
    assert f"function {fn}(" in js
    # eski qo'lda bo'yash qoldiqlari qolmasin
    assert "steps[0].classList.toggle('is-active'" not in js
