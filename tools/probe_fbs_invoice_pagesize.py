"""READ-ONLY: Uzum OpenAPI /v1/fbs/invoice bitta so'rovda nechta накладной beradi?

Savol (Abdulaziz 2026-07-15): «Поставки» ro'yxati har sahifada har xil qator
(14/18/5) ko'rsatadi — begona-do'kon накладнойlari scope-filtr bilan tashlangani
uchun. To'g'ri yechim uchun avval BILISH kerak: Uzum bitta sahifada nechta
qaytaradi, va page oshsa ko'proq keladimi.

Read-only: faqat GET /v1/fbs/invoice. Yozuv yo'q.

Ishga tushirish:
    docker compose exec -T -e PYTHONPATH=/app app python tools/probe_fbs_invoice_pagesize.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.uzum_openapi import (  # noqa: E402
    OPENAPI_BASE, FBS_INVOICE_STATUSES, _fbs_orders_request_with_auth,
)


def _first_token_and_shops() -> tuple[str, set[str]]:
    from app import app as flask_app
    from extensions import SessionLocal
    from models import User, Shop
    from sqlalchemy import select
    with flask_app.app_context():
        with SessionLocal() as db:
            user = db.execute(
                select(User).where(User.uzum_openapi_token.isnot(None))
            ).scalars().first()
            token = (user.uzum_openapi_token or "").strip()
            shops = {
                str(s.uzum_id) for s in db.execute(
                    select(Shop).where(Shop.owner_id == user.id)
                ).scalars().all()
            }
            return token, shops


def _fetch(token: str, statuses: list[str], page: int) -> list[dict]:
    base_qs = "&".join(f"statuses={s}" for s in statuses)
    url = f"{OPENAPI_BASE}/v1/fbs/invoice?{base_qs}&page={page}"
    parsed, status, text, _ = _fbs_orders_request_with_auth(
        url, token, method="GET", accept_language=None,
        debug_label=f"probe.invoice[p={page} n={len(statuses)}st]", fail_fast=True,
    )
    if not (200 <= status < 300):
        print(f"  HTTP {status}: {text[:150]}")
        return []
    payload = parsed.get("payload") if isinstance(parsed, dict) else None
    return [i for i in payload if isinstance(i, dict)] if isinstance(payload, list) else []


def main() -> None:
    token, my_shops = _first_token_and_shops()
    print(f"token=...{token[-6:]}  o'z do'konlar={sorted(my_shops)}\n")

    all_statuses = list(FBS_INVOICE_STATUSES)
    print(f"=== HAMMA STATUS ({','.join(all_statuses)}) — sahifalar ===")
    for page in range(0, 4):
        invs = _fetch(token, all_statuses, page)
        # Har накладнойда shopId bormi? (scope-filtr shunga qarab tashlaydi —
        # aslida invoice payload'ida shopId YO'Q, egalik order orqali).
        shop_keys = set()
        for i in invs:
            for k in ("shopId", "sellerId", "stock"):
                if k in i:
                    shop_keys.add(k)
        print(f"  page={page}: {len(invs)} ta накладной  (payload kalitlari namuna: "
              f"{sorted(invs[0].keys()) if invs else '—'})")
        if not invs:
            print("  → bo'sh sahifa, to'xtadik")
            break

    print("\n=== BITTA STATUS (CANCELED) — sahifalar ===")
    for page in range(0, 3):
        invs = _fetch(token, ["CANCELED"], page)
        print(f"  page={page}: {len(invs)} ta")
        if not invs:
            break


if __name__ == "__main__":
    main()
