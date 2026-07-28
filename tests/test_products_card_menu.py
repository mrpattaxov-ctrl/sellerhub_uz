"""Mahsulot kartasi ⋮ amallar menyusi + «Arxivlash» Uzum mutatsiyasi.

MAQSAD (foydalanuvchi talabi 2026-07-25): /groups kartalaridagi ⋮ tugma
Uzumnikidek menyu ochsin (bizning dizaynga moslangan), tugmalar ishlasin —
faqat «Hodisalar tahlili» hozircha o'chiq. Menyu HAR UCH ko'rinishda
(grid/list/stats) ishlaydi.

⚠️ «Arxivlash» — HAQIQIY Uzum mutatsiyasi (PRODUCT_DELETE huquqi). Foydalanuvchi
qoidasi: action endpoint'ni prodda sinashdan oldin MOCKED test. Bu yerda Uzum'ga
bitta ham so'rov ketmaydi — faqat `_req` ga uzatilgan URL/metod tekshiriladi.
Endpoint dalili: Uzum portal bandli (`.uicheck/uzum-har/new.har`,
`archiveProduct`/`restoreFromArchive`).
"""
from __future__ import annotations

from pathlib import Path

import pytest

import noviy_tavar.client as client

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "templates" / "groups.html"
ROUTES = ROOT / "products" / "routes.py"


@pytest.fixture(scope="module")
def tpl() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def routes() -> str:
    return ROUTES.read_text(encoding="utf-8")


# ── client.archive_product — Uzum endpoint shakli (MOCKED) ───────────


def test_archive_product_hits_correct_endpoint(monkeypatch):
    """Bandl: POST /seller/shop/{shopId}/product/{productId}/archive, TANASIZ."""
    calls = []
    monkeypatch.setattr(client, "_req",
                        lambda method, url, **kw: calls.append((method, url, kw)) or {})
    client.archive_product("23059", "3106012")
    assert len(calls) == 1
    method, url, kw = calls[0]
    assert method == "POST"
    assert url == "https://api-seller.uzum.uz/api/seller/shop/23059/product/3106012/archive"
    assert "json_body" not in kw, "arxiv chaqiruvi TANASIZ — IDlar path'da"


def test_restore_product_hits_restore_endpoint(monkeypatch):
    """Arxivdan chiqarish: xuddi shu path + /restore."""
    calls = []
    monkeypatch.setattr(client, "_req",
                        lambda method, url, **kw: calls.append((method, url, kw)) or {})
    client.archive_product("23059", "3106012", restore=True)
    method, url, kw = calls[0]
    assert method == "POST"
    assert url == "https://api-seller.uzum.uz/api/seller/shop/23059/product/3106012/archive/restore"
    assert "json_body" not in kw


# ── Backend route — egalik-himoyasi + Uzum-avval, DB-keyin ───────────


def test_archive_route_is_ownership_guarded(routes):
    i = routes.index('def api_group_archive')
    body = routes[i:i + 2800]
    # Guruh kiruvchining do'konlariga tegishli bo'lishi shart.
    assert "_user_shop_ids(uid)" in body
    assert "ProductGroup.shop_id.in_(allowed)" in body
    # Uzum AVVAL chaqiriladi, faqat muvaffaqiyatda DB flag o'zgaradi.
    assert body.index("archive_product(") < body.index("update(ProductGroup)")
    assert "is_archived=make_archived" in body
    # Sinxronlanmagan (uzum_product_id yo'q) mahsulot — Uzum'ga bormaymiz.
    assert "synchronized" in body or "синхрон" in body or "sinxron" in body


def test_archive_route_maps_archive_flag_to_restore(routes):
    """archive=false → restore=True (arxivdan chiqarish)."""
    i = routes.index('def api_group_archive')
    body = routes[i:i + 2800]
    assert "restore=not make_archived" in body


# ── Uzum xatosini tushunarli xabarga aylantirish (JONLI probe 2026-07-26) ──
#
# mixbox 2683148 (sotuvdagi zirak) → archive → HTTP 400
#   {"errors":[{"code":"product-003","message":"Product either has active
#    invoice or is on sale"}]}
# Foydalanuvchi «Uzum: HTTP 400» xom kodini emas, SABABNI ko'rishi kerak.


def test_uzum_error_maps_product003_on_sale():
    from products.routes import _uzum_archive_error_text
    body = ('{"payload":null,"errors":[{"code":"product-003","message":'
            '"Product either has active invoice or is on sale"}],"error":'
            '"Product either has active invoice or is on sale"}')
    uz = _uzum_archive_error_text(body, 400, "uz")
    ru = _uzum_archive_error_text(body, 400, "ru")
    assert "sotuvda" in uz.lower() and "HTTP" not in uz, uz
    assert "прода" in ru.lower(), ru


def test_uzum_error_falls_back_to_raw_message():
    """Notanish kod — Uzum'ning O'Z matni ko'rsatilsin (xom HTTP emas)."""
    from products.routes import _uzum_archive_error_text
    body = '{"errors":[{"code":"product-999","message":"Some other reason"}]}'
    assert _uzum_archive_error_text(body, 400, "uz") == "Some other reason"


def test_uzum_error_falls_back_to_http_status_when_unparseable():
    from products.routes import _uzum_archive_error_text
    assert _uzum_archive_error_text("<html>502</html>", 502, "uz") == "Uzum: HTTP 502"
    assert _uzum_archive_error_text("", 400, "uz") == "Uzum: HTTP 400"


def test_archive_route_surfaces_parsed_uzum_error(routes):
    """Route xom `HTTP {status}` emas, parse qilingan matnni qaytaradi."""
    i = routes.index('def api_group_archive')
    body = routes[i:i + 2800]
    assert "_uzum_archive_error_text(e.body" in body
    assert '{"error": f"Uzum: HTTP {e.http_status}"}' not in body, "eski xom xabar qoldi"


# ── Template — menyu tarkibi (Uzum 1:1) + 3 ko'rinishda ⋮ ────────────


def test_card_menu_has_all_six_uzum_items(tpl):
    i = tpl.index('id="ppCardMenu"')
    menu = tpl[i:i + 1600]
    for act in ("edit1", "edit2", "edit3", "market", "events", "archive"):
        assert 'data-act="%s"' % act in menu, act + " menyu bandi yo'q"
    # Uzum matnlari (o'zbekcha).
    assert "Umumiy ta'rifni o'zgartirish" in menu
    assert "SKU-ni o'zgartirish" in menu
    assert "Tovar xususiyatlarini o'zgartirish" in menu
    assert "Uzum Marketda ochish" in menu
    assert "Hodisalar tahlili" in menu
    assert "Arxivlash" in menu and "Arxivdan chiqarish" in menu


def test_events_analysis_is_the_only_disabled_item(tpl):
    """Faqat «Hodisalar tahlili» o'chiq bo'lsin (foydalanuvchi talabi)."""
    i = tpl.index('id="ppCardMenu"')
    menu = tpl[i:i + 1600]
    # events bandi disabled + is-disabled.
    j = menu.index('data-act="events"')
    ev = menu[menu.rindex("<button", 0, j):menu.index("</button>", j)]
    assert "is-disabled" in ev and "disabled" in ev
    # boshqa bandlarda disabled bo'lmasin.
    for act in ("edit1", "edit2", "edit3", "market", "archive"):
        k = menu.index('data-act="%s"' % act)
        btn = menu[menu.rindex("<button", 0, k):menu.index("</button>", k)]
        assert "disabled" not in btn, act + " noto'g'ri o'chiq"


def test_kebab_button_carries_context_in_all_views(tpl):
    """⋮ tugma pid/shop/gid/archived olib yuradi — 3 ko'rinish uchun bitta
    macro'dan render bo'ladi, shuning uchun data attributlari SHART."""
    assert tpl.count("ppOpenCardMenu(this)") >= 1
    assert 'data-pid="{{ g.uzum_product_id or \'\' }}"' in tpl
    assert 'data-shop="{{ shop_uid }}"' in tpl
    assert 'data-gid="{{ g.id }}"' in tpl
    assert "data-archived=" in tpl
    # macro shop_uid ni qabul qiladi + 3 loop uni uzatadi.
    assert "render_card(g, a, store_name, store_key, view, shop_uid='')" in tpl
    assert tpl.count("suid) }}") >= 3, "3 ko'rinish loop'i shop_uid uzatmayapti"


# ── Template JS — amal simlari ───────────────────────────────────────


def test_edit_actions_open_editor_at_right_step(tpl):
    i = tpl.index("ppOpenCardMenu = function")
    js = tpl[i:i + 3000]
    assert "/noviy-tavar?productId=" in js
    assert "'&step=' + step" in js
    # edit1→1, edit2→2, edit3→3.
    assert "act === 'edit1' ? 1 : (act === 'edit2' ? 2 : 3)" in js


def test_market_action_opens_uzum_public_url(tpl):
    i = tpl.index("ppOpenCardMenu = function")
    js = tpl[i:i + 3000]
    assert "https://uzum.uz/' + lg + '/product/-'" in js
    assert "_blank" in js


def test_archive_action_posts_to_backend(tpl):
    i = tpl.index("ppOpenCardMenu = function")
    js = tpl[i:i + 3000]
    assert "/groups/api/archive" in js
    assert "archive: makeArchived" in js
    # arxiv holatini teskari qiladi (toggle).
    assert "!c.archived" in js
