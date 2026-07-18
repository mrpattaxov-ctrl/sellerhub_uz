"""READ-ONLY probe v2: EXHAUSTIVE hunt for a shop filter on the накладная list.

Round 1 (probe_fbs_invoice_shopids.py) showed shopIds/shopId/csv all return the
SAME 8 invoices as the unfiltered call, and the invoice payload carries no shop
field. Abdulaziz asked to dig deeper before we accept that. So:

1. CONTROL — send a GARBAGE shop id (999999999). If Uzum truly reads a shop
   filter, a nonexistent shop must return 0 (or 400). If it still returns all 8,
   the param is provably NOT read — that settles it, no naming guesswork left.

2. NAME SWEEP — every plausible spelling Uzum uses elsewhere in its API:
   shopIds, shopId, shop_id, shopIdList, shops, shop, sellerId, sellerIds,
   stockId, stockIds. Each judged against the baseline count.

3. SIBLING ENDPOINTS — maybe another route exposes the link:
   /v2/fbs/invoice, /v1/fbs/invoice/list, /v1/fbs/invoice/count.

4. DETAIL payload — does GET /v1/fbs/invoice/{id} carry a shop field the list
   omits? (Even if yes it costs 1 call per invoice, but it would let us verify
   ownership without the orders drain.)

Decisive outcomes:
  * garbage id → 0 invoices  ⇒ a real filter exists; find its name and use it.
  * garbage id → 8 invoices  ⇒ NO filter exists on this endpoint. Full stop.

SAFETY: GET only, ~15 calls, paced through the shared bucket. Token never printed.

Run:
    docker compose exec -e PYTHONPATH=/app app python tools/probe_fbs_invoice_shopids2.py
"""
from __future__ import annotations

import json
import sys

from sqlalchemy import func, select

from extensions import SessionLocal
from models import Shop, User
from core.uzum_openapi import OPENAPI_BASE, _fbs_orders_request_with_auth

GARBAGE_SHOP = "999999999"   # a shop id that cannot exist on this account


def pick():
    with SessionLocal() as db:
        user = db.execute(
            select(User)
            .where(func.length(func.coalesce(User.uzum_openapi_token, "")) > 0)
            .order_by(User.id)
        ).scalars().first()
        if user is None:
            sys.exit("No token.")
        shops = db.execute(
            select(Shop.uzum_id).where(Shop.owner_id == user.id)
        ).scalars().all()
        return (user.uzum_openapi_token or "").strip(), [str(s) for s in shops if s]


def get(token, url, label):
    parsed, http, text, _ = _fbs_orders_request_with_auth(
        url, token, accept_language=None, debug_label=label, fail_fast=True,
    )
    payload = parsed.get("payload") if isinstance(parsed, dict) else None
    n = len(payload) if isinstance(payload, list) else None
    return http, n, payload, (text or "")[:120]


def main() -> None:
    token, shops = pick()
    status = "ACCEPTED"   # the status round 1 found invoices in
    base = f"{OPENAPI_BASE}/v1/fbs/invoice?statuses={status}&page=0"

    http, baseline, payload, _ = get(token, base, "probe2.baseline")
    print(f"shops={shops}")
    print(f"BASELINE (filtrsiz): HTTP {http}, {baseline} ta накладная\n")
    if not baseline:
        sys.exit("Baseline bo'sh — sinov ma'nosiz.")

    # ── 1. CONTROL: garbage shop id ────────────────────────────────────
    print("=== 1. NAZORAT SINOVI: mavjud bo'lmagan do'kon id (999999999) ===")
    print("    Agar Uzum filtr qilsa → 0 (yoki 400) qaytishi SHART.\n")
    for name in ("shopIds", "shopId"):
        h, n, _p, err = get(token, f"{base}&{name}={GARBAGE_SHOP}", f"probe2.garbage.{name}")
        if not (200 <= h < 300):
            verdict = "✅ RAD ETDI — demak parametrni O'QIYDI!"
        elif n == 0:
            verdict = "✅ 0 QAYTARDI — demak FILTR ISHLAYDI!"
        elif n == baseline:
            verdict = "❌ o'zgarmadi → parametr UMUMAN O'QILMAYDI"
        else:
            verdict = f"❓ kutilmagan: {n}"
        print(f"    {name}={GARBAGE_SHOP:<12} HTTP {h}  n={n}   {verdict}")

    # ── 2. NAME SWEEP ──────────────────────────────────────────────────
    print("\n=== 2. PARAMETR NOMLARI (bitta haqiqiy do'kon bilan) ===")
    one = shops[0]
    names = [
        "shopIds", "shopId", "shop_id", "shopIdList", "shops", "shop",
        "sellerId", "sellerIds", "stockId", "stockIds",
    ]
    for name in names:
        h, n, _p, err = get(token, f"{base}&{name}={one}", f"probe2.name.{name}")
        if not (200 <= h < 300):
            mark = f"HTTP {h} (rad)"
        elif n == baseline:
            mark = f"n={n}  = baseline → e'tiborsiz"
        else:
            mark = f"n={n}  ≠ baseline ({baseline})  ← ⚠️ TA'SIR QILDI!"
        print(f"    {name:<12} {mark}")

    # ── 3. SIBLING ENDPOINTS ───────────────────────────────────────────
    print("\n=== 3. QO'SHNI ENDPOINTLAR ===")
    for path in ("/v2/fbs/invoice", "/v1/fbs/invoice/list", "/v1/fbs/invoice/count"):
        url = f"{OPENAPI_BASE}{path}?statuses={status}&page=0"
        h, n, p, err = get(token, url, f"probe2.ep{path}")
        print(f"    {path:<24} HTTP {h}  payload={type(p).__name__}"
              f"{'' if 200 <= h < 300 else '  ' + err}")

    # ── 4. DETAIL payload ──────────────────────────────────────────────
    print("\n=== 4. NAKLADNAYA DETALI — do'kon maydoni bormi? ===")
    inv_id = payload[0].get("id") if isinstance(payload, list) and payload else None
    if inv_id is None:
        print("    namuna yo'q")
    else:
        h, _n, det, err = get(token, f"{OPENAPI_BASE}/v1/fbs/invoice/{inv_id}",
                              "probe2.detail")
        if isinstance(det, dict):
            keys = sorted(det.keys())
            hits = [k for k in keys if "shop" in k.lower() or "seller" in k.lower()]
            print(f"    id={inv_id}  maydonlar: {keys}")
            print("    do'kon/sotuvchi maydoni: %s" % (hits if hits else "YO'Q"))
        else:
            print(f"    HTTP {h}  {err}")


if __name__ == "__main__":
    main()
