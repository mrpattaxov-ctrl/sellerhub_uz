"""SellerHub → Auto-slot HTTP client (yupqa).

Bayroq: `AUTOSLOT_URL`.
  • BO'SH (hozirgi prod holati) → `enabled()` False → SellerHub route'lari
    BUGUNGIDEK mahalliy ishlaydi (`store.*`, lokal DB). NOL o'zgarish.
  • QO'YILGAN → avto-band yozish/o'qish alohida autoslot servisiga o'tadi.

Cutover (VPS): `AUTOSLOT_URL` qo'yiladi + SellerHub'ning o'z `POSTAVKA_*_LOOP`
lari ENV bilan O'CHIRILADI (ikki tomon bir slotni urishtirmasin).

Bu modul SellerHub'ning yagona autoslot-tashqi nuqtasi — boshqa joyда HTTP
takrorlanmaydi. Autoslot javob shakli (snake_case) → seller UI shakli
(camelCase) shu yerда moslashtiriladi.
"""
from __future__ import annotations

import os

import requests


def _base() -> str:
    return (os.environ.get("AUTOSLOT_URL") or "").strip().rstrip("/")


def _key() -> str:
    return (os.environ.get("AUTOSLOT_API_KEY") or "").strip()


def enabled() -> bool:
    return bool(_base())


def _headers() -> dict:
    return {"X-Autoslot-Key": _key(), "Content-Type": "application/json"}


def _timeout() -> float:
    try:
        return max(1.0, float(os.environ.get("AUTOSLOT_HTTP_TIMEOUT_SEC", "10") or 10))
    except ValueError:
        return 10.0


# ── Yozish ────────────────────────────────────────────────────────────

def post_plan(payload: dict) -> dict:
    """Aktni autoslot navbatiga qo'shadi (UPSERT). -> {"id","action","status"}."""
    r = requests.post(f"{_base()}/v1/plans", json=payload, headers=_headers(), timeout=_timeout())
    r.raise_for_status()
    return r.json()


def set_priority(plan_id, priority) -> int | None:
    r = requests.post(f"{_base()}/v1/plans/{int(plan_id)}/priority",
                      json={"priority": int(priority)}, headers=_headers(), timeout=_timeout())
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json().get("priority")


def cancel(plan_id) -> str | None:
    r = requests.post(f"{_base()}/v1/plans/{int(plan_id)}/cancel",
                      headers=_headers(), timeout=_timeout())
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json().get("status")


# ── O'qish ────────────────────────────────────────────────────────────

def get_plan(plan_id) -> dict | None:
    r = requests.get(f"{_base()}/v1/plans/{int(plan_id)}", headers=_headers(), timeout=_timeout())
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def _fetch(status: str | None = None, shop: str | None = None) -> list[dict]:
    """Autoslot GET /v1/plans → xom dict ro'yxati (snake_case)."""
    params = {}
    if status:
        params["status"] = status
    if shop:
        params["shop"] = shop
    r = requests.get(f"{_base()}/v1/plans", params=params, headers=_headers(), timeout=_timeout())
    r.raise_for_status()
    return r.json().get("plans", [])


def admin_list(status: str | None = None) -> list[dict]:
    """ADMIN paneli uchun XOM dict'lar (snake_case) — enrichment SellerHub'da."""
    return _fetch(status=status)


def _map_seller(p: dict, shop_name: str | None = None) -> dict:
    """Autoslot xom dict → seller UI (camelCase) shakli. Autoslot'da target_day
    yo'q — u SellerHub'да doim max_date'ga teng edi, shuning uchun targetDay=maxDate."""
    md = p.get("max_date")
    return {
        "id": p.get("id"),
        "shop": p.get("shop_uzum_id"),
        "shopName": shop_name,
        "invoiceId": p.get("invoice_id"),
        "invoiceNumber": p.get("invoice_number"),
        "size": p.get("volume"),
        "targetDay": md,
        "maxDate": md,
        "enabledAt": p.get("enabled_at"),
        "status": p.get("status"),
        "bookedSlotMs": p.get("booked_slot_ms"),
        "error": p.get("error"),
    }


def list_for_shop(shop: str, status: str | None = None) -> list[dict]:
    """Bitta do'kon rejalari — seller shaklида."""
    return [_map_seller(p) for p in _fetch(status=status, shop=shop)]


def list_for_shops(names: dict, status: str | None = None) -> list[dict]:
    """Bir nechta do'kon (uzum_id→nom) rejalari — seller shaklида, shopName bilan.
    Autoslot ko'p-do'kon filtrini bermaydi → do'kon boshiga bittadan so'rov."""
    out: list[dict] = []
    for shop_id, name in names.items():
        for p in _fetch(status=status, shop=str(shop_id)):
            out.append(_map_seller(p, shop_name=name))
    return out


# ── Token push (rotatsiya) ────────────────────────────────────────────

def push_token(token: str) -> bool:
    if not enabled() or not token:
        return False
    try:
        r = requests.post(f"{_base()}/v1/tokens", json={"token": token},
                          headers=_headers(), timeout=_timeout())
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"[autoslot_client] token push xato: {e!r}")
        return False


def token_push_loop():
    """Fon loop: admin Uzum tokenini davriy autoslot'ga push qiladi (token
    rotatsiya qilinganda autoslot ham yangisini olsin). AUTOSLOT_URL bo'sh →
    darhol chiqadi (no-op). O'chirish: AUTOSLOT_TOKEN_PUSH=0."""
    import time as _t
    if not enabled() or os.environ.get("AUTOSLOT_TOKEN_PUSH", "1").strip().lower() in ("0", "false", "no"):
        print("[autoslot_client] token-push loop o'chiq (AUTOSLOT_URL yo'q yoki _PUSH=0)")
        return
    from core.auth_helpers import _get_admin_token
    try:
        interval = max(10, int(os.environ.get("AUTOSLOT_TOKEN_PUSH_SEC", "60") or 60))
    except ValueError:
        interval = 60
    print(f"[autoslot_client] token-push loop started — every {interval}s → {_base()}")
    last = None
    while True:
        try:
            tok = _get_admin_token()
            if tok and tok != last and push_token(tok):
                last = tok
                print("[autoslot_client] token autoslot'ga push qilindi")
        except Exception as e:
            print(f"[autoslot_client] token-push loop xato: {e!r}")
        _t.sleep(interval)
