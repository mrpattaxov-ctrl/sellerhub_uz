"""READ-ONLY probe: is the DB (what the bg worker wrote) actually TRUE right now?

The open proposal is to stop live-refreshing the 3 chips the seller isn't looking
at on page entry and serve their badges from the DB instead. That is only safe if
the DB is actually correct. Abdulaziz's objection is exactly right: phantoms
("arvox") used to survive in the DB for a long time, so "the worker refreshes it"
is a claim, not a fact.

This probe measures it: for every user token, for each of the 4 active-work
statuses, it compares

    DB count  (what a badge served from the DB would show)
    vs
    Uzum /count  (the truth, right now)

and prints the drift. A non-zero drift on a status = a badge served from the DB
would LIE by that much.

It also prints how long ago the newest row of each status was written, and the
worker's configured interval for that status (core.fbs_sync.FBS_STATUS_SYNC_INTERVAL_MIN)
— note PENDING_DELIVERY is ABSENT from that map, i.e. the worker NEVER syncs it.

SAFETY: GET only (one /count per status per token). Nothing written or deleted.
Paced through the shared per-token bucket. Tokens are never printed.

Run:
    docker compose exec -e PYTHONPATH=/app app python tools/probe_fbs_db_vs_live.py
"""
from __future__ import annotations

import sys
from datetime import datetime

from sqlalchemy import func, select

from extensions import SessionLocal
from models import FbsOrder, Shop, User
from core.uzum_openapi import fetch_fbs_orders_count
from core.fbs_sync import FBS_STATUS_SYNC_INTERVAL_MIN

ACTIVE = ("CREATED", "PACKING", "PENDING_DELIVERY", "DELIVERING")


def main() -> None:
    with SessionLocal() as db:
        users = db.execute(
            select(User).where(func.length(func.coalesce(User.uzum_openapi_token, "")) > 0)
            .order_by(User.id)
        ).scalars().all()
        if not users:
            sys.exit("No user with an OpenAPI token.")
        by_token: dict[str, tuple[list[int], list[str]]] = {}
        for u in users:
            shops = db.execute(
                select(Shop.uzum_id).where(Shop.owner_id == u.id)
            ).scalars().all()
            shops = [str(s) for s in shops if s]
            if not shops:
                continue
            tok = (u.uzum_openapi_token or "").strip()
            uids, sids = by_token.setdefault(tok, ([], []))
            uids.append(int(u.id))
            for s in shops:
                if s not in sids:
                    sids.append(s)

    print("Fon-worker intervallari (core/fbs_sync.py):")
    for st in ACTIVE:
        iv = FBS_STATUS_SYNC_INTERVAL_MIN.get(st)
        print(f"  {st:<18} {str(iv) + ' daq' if iv else 'HECH QACHON sinxronlanmaydi'}")
    print()

    now = datetime.utcnow()
    total_drift = 0

    for tok, (uids, shops) in by_token.items():
        print(f"═══ users={uids}  shops={shops}")
        for st in ACTIVE:
            with SessionLocal() as db:
                db_n = db.execute(
                    select(func.count(FbsOrder.id))
                    .where(FbsOrder.shop_id.in_(shops))
                    .where(FbsOrder.status == st)
                ).scalar() or 0
                newest = db.execute(
                    select(func.max(FbsOrder.synced_at))
                    .where(FbsOrder.shop_id.in_(shops))
                    .where(FbsOrder.status == st)
                ).scalar()
            try:
                live_n, _ = fetch_fbs_orders_count(tok, shops, status=st, fail_fast=True)
            except Exception as e:
                print(f"  {st:<18} DB={db_n:<4} LIVE=XATO ({e!r})")
                continue

            drift = int(db_n) - int(live_n)
            total_drift += abs(drift)
            age = ""
            if newest:
                mins = (now - newest).total_seconds() / 60.0
                age = f"  (eng yangi qator {mins:.0f} daq oldin yozilgan)"
            mark = "✅ mos" if drift == 0 else (
                f"❌ ARVOX: DB'da {drift} ta ORTIQCHA" if drift > 0
                else f"❌ KAMOMAD: DB'da {-drift} ta YETISHMAYDI"
            )
            print(f"  {st:<18} DB={db_n:<4} LIVE={live_n:<4} {mark}{age}")
        print()

    print("--- XULOSA ---")
    if total_drift == 0:
        print("DB hozir Uzum bilan TO'LIQ mos — badge'ni DB'dan ko'rsatish xavfsiz "
              "(lekin intervallarni yodda tuting: PENDING_DELIVERY hech qachon "
              "fonda yangilanmaydi).")
    else:
        print(f"DB Uzumdan {total_drift} ta qator bilan FARQ qiladi → o'sha statusning "
              f"badge'ini DB'dan ko'rsatsak, YOLG'ON raqam chiqadi.")


if __name__ == "__main__":
    main()
