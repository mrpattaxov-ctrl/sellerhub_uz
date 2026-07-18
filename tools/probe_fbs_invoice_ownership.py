"""READ-ONLY probe: does the накладная ownership filter actually work — and does
a postavka created in the UZUM APP survive it?

Two questions from Abdulaziz (2026-07-14):
  1. "Begona aktlar ko'rinmaydimi?" — user 2 and user 3 SHARE one Uzum token
     (one account, 6 shops). Uzum's invoice list has no shopId, so it returns
     BOTH users' накладные. Does our filter really drop user 3's?
  2. "Uzumdan postavka yaratsam ham ko'rinadimi?" — an invoice created directly
     in the Uzum app has no trace in our DB. Does it still show?

This replays the exact filter the route runs (fbs/routes.py:4466-4470):

    owned = _owned_invoice_numbers(user_shops)            # DB  (history)
    owned |= _live_owned_invoice_numbers(token, shops)    # LIVE (PENDING_DELIVERY drain)
    invoices = _filter_invoices_to_owned(invoices, owned)

...for BOTH users on the shared token, and prints which invoice numbers each one
keeps and drops. If the filter works, the two sets are disjoint and every invoice
lands with exactly one user.

It also reports the COVERAGE GAP that question 2 hinges on: an invoice whose
orders have already left PENDING_DELIVERY (e.g. Uzum accepted it) can only be
recognised via the DB. If the worker hasn't synced those orders yet, the invoice
is in NEITHER set → it would be dropped as "foreign" and become INVISIBLE to its
own owner. This prints exactly how many invoices are in that state right now.

SAFETY: GET only (1 invoice-list call + 1 PENDING_DELIVERY drain per token).

Run:
    docker compose exec -e PYTHONPATH=/app app python tools/probe_fbs_invoice_ownership.py
"""
from __future__ import annotations

import sys

from sqlalchemy import func, select

from extensions import SessionLocal
from models import FbsOrder, Shop, User
from core.uzum_openapi import fetch_fbs_invoices_list, FBS_INVOICE_STATUSES

import fbs.routes as R


def users_on_shared_tokens():
    """{token: [(user_id, [shop_uzum_ids]), ...]} — grouped by token."""
    out: dict[str, list] = {}
    with SessionLocal() as db:
        users = db.execute(
            select(User)
            .where(func.length(func.coalesce(User.uzum_openapi_token, "")) > 0)
            .order_by(User.id)
        ).scalars().all()
        for u in users:
            shops = [
                str(s) for s in db.execute(
                    select(Shop.uzum_id).where(Shop.owner_id == u.id)
                ).scalars().all() if s
            ]
            if shops:
                out.setdefault((u.uzum_openapi_token or "").strip(), []).append(
                    (int(u.id), shops)
                )
    return out


def db_owned(shops: list[str]) -> set[str]:
    """Invoice numbers our DB already links to these shops (any order status)."""
    with SessionLocal() as db:
        rows = db.execute(
            select(FbsOrder.invoice_number)
            .where(FbsOrder.shop_id.in_(shops))
            .where(FbsOrder.invoice_number.is_not(None))
            .distinct()
        ).scalars().all()
    return {str(r) for r in rows if r}


def main() -> None:
    groups = users_on_shared_tokens()
    for token, members in groups.items():
        ids = ", ".join(f"user {u} ({len(s)} do'kon)" for u, s in members)
        print(f"\n{'='*70}\nTOKEN: {ids}")

        # What Uzum hands back for the WHOLE account (no shop filter possible).
        all_invoices: list[dict] = []
        for st in FBS_INVOICE_STATUSES:
            try:
                inv, _ = fetch_fbs_invoices_list(token, statuses=[st], page=0, fail_fast=True)
            except Exception as e:
                print(f"  {st}: XATO {e!r}")
                continue
            for i in inv:
                i["_status"] = st
            all_invoices.extend(inv)
        numbers = [str(i.get("number")) for i in all_invoices]
        print(f"\nUzum akkaunt bo'yicha qaytardi: {len(all_invoices)} ta накладная")
        for i in all_invoices:
            print(f"    #{i.get('number')}  id={i.get('id')}  {i['_status']}"
                  f"  ombor={(i.get('stock') or {}).get('title')}")

        # Replay the route's filter for EACH user on this token.
        claimed: dict[str, list[int]] = {}
        for uid, shops in members:
            db_set = db_owned(shops)
            live_set = R._live_owned_invoice_numbers(token, shops)
            owned = db_set | live_set
            kept = R._filter_invoices_to_owned(list(all_invoices), owned)
            kept_nums = {str(i.get("number")) for i in kept}

            print(f"\n  ── user {uid}  do'konlar={shops}")
            print(f"     DB'dan tanilgan накладная raqamlari : {len(db_set)}")
            print(f"     JONLI drenajdan (PENDING_DELIVERY)  : {len(live_set)}")
            print(f"     → KO'RADI  : {sorted(kept_nums) if kept_nums else 'hech narsa'}")
            print(f"     → YASHIRIN : {sorted(set(numbers) - kept_nums) or 'yo\'q'}")
            for n in kept_nums:
                claimed.setdefault(n, []).append(uid)

        # ── Verdicts ───────────────────────────────────────────────────
        print(f"\n  {'-'*66}\n  XULOSA")
        orphan = [n for n in numbers if n not in claimed]
        shared = [n for n, us in claimed.items() if len(us) > 1]

        if shared:
            print(f"  ❌ SIZIB CHIQDI: {shared} — bir накладнойni bir necha user ko'ryapti!")
        else:
            print("  ✅ Begona накладная sizib chiqmadi (har biri bitta egaga tegishli).")

        if orphan:
            print(f"  ⚠️  HECH KIM KO'RMAYDI: {orphan}")
            print("     Bu — 2-savolning javobi: buyurtmalari PENDING_DELIVERY'dan")
            print("     chiqib ketgan, DB'ga esa hali tushmagan накладная EGASIGA HAM")
            print("     ko'rinmaydi (fon PENDING_DELIVERY'ni sinxronlamaydi).")
        else:
            print("  ✅ Har накладнойning egasi topildi — ko'rinmay qolgani yo'q.")


if __name__ == "__main__":
    main()
