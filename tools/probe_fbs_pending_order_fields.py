"""READ-ONLY probe: can the накладные view be built from the ORDERS call alone?

Abdulaziz's point (2026-07-14): /v2/fbs/orders DOES honour shopIds — so use THAT
for postavka too, instead of the shop-blind /v1/fbs/invoice.

We already make that call: the ownership drain
(GET /v2/fbs/orders?shopIds=...&status=PENDING_DELIVERY). The question is whether
its payload carries enough INVOICE metadata to render the накладные cards without
also calling /v1/fbs/invoice. If it does, the view drops from 3 Uzum calls to 1.

The invoice list gives us, per накладная:
    id, number, status{value,text,color}, fullPrice, acceptedPrice,
    numberOrders, numberAcceptedOrders, stock{...}, dropOffPoint{...},
    timeSlot{timeFrom,timeTo}, ettn, dateCreated/Updated, accepted*Date

So this probe dumps EVERY key of a PENDING_DELIVERY order and highlights anything
invoice-shaped, then reports which of the fields above are present vs missing.

Verdict:
  * all card fields present  → drop /v1/fbs/invoice entirely (3 calls → 1).
  * only invoiceNumber       → the invoice list stays; ownership still needs the
                               drain (which is this same call), so the floor is 2.

SAFETY: GET only, 1-2 calls. Token never printed.

Run:
    docker compose exec -e PYTHONPATH=/app app python tools/probe_fbs_pending_order_fields.py
"""
from __future__ import annotations

import json
import sys

from sqlalchemy import func, select

from extensions import SessionLocal
from models import Shop, User
from core.fbs_sync import fetch_all_pages

# What the накладная card needs (from templates/fbs_invoices.html + the list API).
CARD_FIELDS = [
    "id", "number", "status", "fullPrice", "acceptedPrice",
    "numberOrders", "numberAcceptedOrders", "stock", "dropOffPoint",
    "timeSlot", "ettn", "dateCreated",
]


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


def main() -> None:
    token, shops = pick()
    print(f"shops={shops}")
    print("GET /v2/fbs/orders?shopIds=...&status=PENDING_DELIVERY\n")

    orders = fetch_all_pages(token, shops, status="PENDING_DELIVERY", fail_fast=True)
    print(f"PENDING_DELIVERY buyurtmalar: {len(orders)}\n")
    if not orders:
        sys.exit("Buyurtma yo'q — sinov hal qilmaydi (накладная yaratib qayta uring).")

    o = orders[0]
    keys = sorted(o.keys())
    print("=== BUYURTMA MAYDONLARI ===")
    print(f"  {keys}\n")

    inv_like = {k: o[k] for k in keys if "invoice" in k.lower() or "shop" in k.lower()}
    print("=== NAKLADNAYA / DO'KON bilan bog'liq maydonlar ===")
    print("  " + json.dumps(inv_like, ensure_ascii=False, default=str)[:400] + "\n")

    print("=== NAKLADNAYA KARTASI uchun kerakli maydonlar ===")
    have, miss = [], []
    for f in CARD_FIELDS:
        # match case-insensitively, and also inside a nested `invoice` object
        nested = o.get("invoice") if isinstance(o.get("invoice"), dict) else {}
        found = f in o or f in nested or any(k.lower() == f.lower() for k in keys)
        (have if found else miss).append(f)
        print(f"  {'✅' if found else '❌'} {f}")

    print("\n--- XULOSA ---")
    if not miss:
        print("✅ HAMMASI BOR → /v1/fbs/invoice KERAK EMAS. «Поставка» 3 so'rov → 1.")
    else:
        print(f"❌ YETISHMAYDI: {miss}")
        print("   → накладная kartasini faqat buyurtmalardan chizib bo'lmaydi;")
        print("     /v1/fbs/invoice qoladi. Eng past chegara: 2 so'rov (kesh bilan 1).")


if __name__ == "__main__":
    main()
