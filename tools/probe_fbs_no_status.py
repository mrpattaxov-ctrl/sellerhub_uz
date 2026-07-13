"""READ-ONLY probe: can /v2/fbs/orders return orders of ALL statuses in ONE call?

Follow-up to tools/probe_fbs_multi_status.py, which proved the OpenAPI list
takes exactly ONE status (repeated param → first wins; comma → 400; the portal's
``statuses`` name → unrecognised). So a multi-status FILTER is out.

The remaining idea (Abdulaziz): don't filter at all. If the list endpoint with
NO status param returns a mixed bag of every status, then one drain gives us the
open chip's rows AND every badge number (count them locally) — 5 calls → 1.

Swagger claims ``status`` defaults to CREATED, which would kill the idea, but
swagger already lied about ``statuses`` semantics once, so we ask the server.

Variants:
  F  no ``status`` param at all      → does it default to CREATED, or return all?
  G  ``status=`` (empty value)       → some Spring binders treat this as "no filter"
  H  ``status=ALL``                  → a magic value some APIs accept

The one thing that would make this WORK: a response whose orders carry MORE THAN
ONE distinct status. Anything else (only CREATED / 400) kills it.

Also reported: sortBy/sortOrder — if the unfiltered list is date-sorted DESC, the
active orders (recent) would sit on page 0 and the thousands of terminal ones
behind them, which is what would make a single drain practical.

SAFETY: GET only. Nothing created/changed/deleted. Paced through the shared
per-token bucket. Token read from our DB, never printed.

Run:
    docker compose exec -e PYTHONPATH=/app app python tools/probe_fbs_no_status.py
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter

from sqlalchemy import func, select

from extensions import SessionLocal
from models import Shop, User
from core.uzum_openapi import OPENAPI_BASE, _fbs_orders_request_with_auth

SIZE = 50


def pick_token_and_shops(user_id: int | None) -> tuple[str, list[str], int]:
    with SessionLocal() as db:
        q = select(User).where(func.length(func.coalesce(User.uzum_openapi_token, "")) > 0)
        if user_id is not None:
            q = q.where(User.id == int(user_id))
        user = db.execute(q.order_by(User.id)).scalars().first()
        if user is None:
            sys.exit("No user with an OpenAPI token found in the DB.")
        shops = db.execute(
            select(Shop.uzum_id).where(Shop.owner_id == user.id)
        ).scalars().all()
        shops = [str(s) for s in shops if s]
        if not shops:
            sys.exit(f"User {user.id} has no shops.")
        return (user.uzum_openapi_token or "").strip(), shops, int(user.id)


def _get(token: str, url: str, label: str) -> dict:
    t0 = time.monotonic()
    parsed, http_status, text, _auth = _fbs_orders_request_with_auth(
        url, token, accept_language=None, debug_label=label, fail_fast=True,
    )
    elapsed = round(time.monotonic() - t0, 2)

    seen: Counter = Counter()
    n = None
    total = None
    payload = parsed.get("payload") if isinstance(parsed, dict) else None
    if isinstance(payload, dict):
        orders = payload.get("orders")
        # Uzum reports the full result-set size alongside the page — useful:
        # it tells us how deep a drain would have to go.
        for k in ("totalElements", "total", "totalCount", "count"):
            if isinstance(payload.get(k), int):
                total = payload[k]
                break
        if isinstance(orders, list):
            n = len(orders)
            for o in orders:
                if isinstance(o, dict):
                    seen[str(o.get("status") or "?")] += 1
    return {
        "http": http_status,
        "n": n,
        "total": total,
        "statuses": dict(seen),
        "elapsed_s": elapsed,
        "err": None if 200 <= http_status < 300 else (text or "")[:200],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", type=int, default=None)
    args = ap.parse_args()

    token, shops, uid = pick_token_and_shops(args.user)
    print(f"user_id={uid}  shops={shops}  (token loaded, not shown)\n")

    shop_qs = "&".join(f"shopIds={s}" for s in shops)
    base = f"{OPENAPI_BASE}/v2/fbs/orders?{shop_qs}&page=0&size={SIZE}"

    variants = [
        ("F  status parametri UMUMAN yo'q", base),
        ("G  status=  (bo'sh qiymat)",      f"{base}&status="),
        ("H  status=ALL  (sehrli qiymat)",  f"{base}&status=ALL"),
    ]

    results = []
    for label, url in variants:
        r = _get(token, url, f"probe.{label[:1]}")
        r["variant"] = label
        r["url"] = url.replace(OPENAPI_BASE, "")
        results.append(r)
        if 200 <= r["http"] < 300:
            print(f"  {label}\n      HTTP {r['http']}  orders={r['n']}  total={r['total']}  "
                  f"statuses={r['statuses']}  {r['elapsed_s']}s")
        else:
            print(f"  {label}\n      HTTP {r['http']}  RAD ETILDI  {r['err']}")

    print("\n--- XULOSA ---")
    win = None
    for r in results:
        if not (200 <= r["http"] < 300):
            print(f"  {r['variant'][:1]}: rad etildi (HTTP {r['http']}).")
            continue
        got = set(r["statuses"])
        if len(got) > 1:
            print(f"  {r['variant'][:1]}: ✅ ARALASH javob — bir nechta status keldi: "
                  f"{r['statuses']}  → bitta so'rovda hammasini olsa BO'LADI.")
            win = win or r
        elif got == {"CREATED"}:
            print(f"  {r['variant'][:1]}: ❌ faqat CREATED — swagger'dagi default. Filtr o'chmadi.")
        elif got:
            print(f"  {r['variant'][:1]}: ❌ faqat «{next(iter(got))}» — bitta status.")
        else:
            print(f"  {r['variant'][:1]}: ❔ bo'sh javob (CREATED default'i bo'sh bo'lsa shunday).")

    print()
    if win:
        tot = win["total"]
        print(f"NATIJA: ✅ filtrsiz ro'yxat ISHLAYDI. Javobda total={tot} — bu qancha "
              f"buyurtmani drenaj qilish kerakligini bildiradi. Agar total katta bo'lsa "
              f"(masalan 1000+), 4 ta aktiv statusni topish uchun o'nlab sahifa kerak "
              f"bo'ladi → 1 so'rov bo'lmaydi. Sana bo'yicha DESC saralangan bo'lsa, "
              f"aktivlar 1-sahifada bo'lishi mumkin — quyidagi raw'dan tekshiring.")
    else:
        print("NATIJA: ❌ filtrsiz ro'yxat yo'q — Uzum doim bitta statusga filtrlaydi "
              "(default CREATED). Bitta so'rovda 4 chipni olish IMKONSIZ.")

    print("\nraw:", json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
