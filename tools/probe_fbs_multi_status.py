"""READ-ONLY probe: does /v2/fbs/orders accept MORE THAN ONE status in one call?

Why this matters (Abdulaziz, 2026-07-13): page entry costs 5 Uzum calls —
1 list drain for the open chip + 3 /count calls for the other active badges
(+1 empty-confirm). At the shared 2 req/s bucket that's ~2.7s.

/v2/fbs/orders/count can NEVER collapse those 3: its payload is a single int
(fbs_docs/ENDPOINTS.md), so a multi-status count would return a merged sum we
cannot split back into per-chip badges.

The LIST endpoint could, though: every order body carries its own ``status``,
so ONE call returning all 4 active statuses gives us the open chip's rows AND
all 4 badge numbers (counted locally) AND a 4-status phantom prune. 5 calls → 1.

Swagger says ``status`` is a single value, but the Uzum PORTAL list call uses
``statuses`` (plural) — so the server side may well support a set. Unverified
either way, hence this probe.

The dangerous outcome we MUST rule out: Uzum silently IGNORING an unknown /
malformed status param and returning orders of EVERY status. Our prune logic
treats a drain as authoritative for one status, so a silently-unfiltered
response would look like "these are all the CREATED orders" and could delete
live rows. Every variant below is therefore judged on WHICH statuses actually
came back, not just on the HTTP code.

SAFETY: GET only. Nothing created/changed/deleted. Every call goes through the
shared per-token bucket, so it cannot burst Uzum. Token is read from our DB and
never printed.

Run (inside the app container):
    docker compose exec app python tools/probe_fbs_multi_status.py
Optional:
    --user 1        (default: first user with a token)
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

# The two statuses we probe with. An order lives in exactly ONE status, so if a
# response carries BOTH, the server honoured a multi-status filter — that cannot
# happen by accident.
#
# The pair MUST be two statuses that actually HAVE orders, or every variant comes
# back empty and proves nothing (first run: CREATED=0, PACKING=0 → inconclusive).
# The param's semantics don't depend on WHICH statuses we pass, so we default to
# the two busiest ones; override with --s1/--s2.
S1 = "CANCELED"
S2 = "COMPLETED"

# The 4 active chips the page-entry load currently pays 4 calls for. If a
# variant works, this is the set we'd fetch in ONE call.
ACTIVE = ("CREATED", "PACKING", "DELIVERING", "PENDING_DELIVERY")

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
    """One paced GET. Returns {http, statuses: Counter, n, elapsed_s, err}."""
    t0 = time.monotonic()
    parsed, http_status, text, _auth = _fbs_orders_request_with_auth(
        url, token, accept_language=None, debug_label=label, fail_fast=True,
    )
    elapsed = round(time.monotonic() - t0, 2)

    seen: Counter = Counter()
    n = None
    payload = parsed.get("payload") if isinstance(parsed, dict) else None
    if isinstance(payload, dict):
        orders = payload.get("orders")
        if isinstance(orders, list):
            n = len(orders)
            for o in orders:
                if isinstance(o, dict):
                    seen[str(o.get("status") or "?")] += 1
    elif isinstance(payload, int):          # the /count shape
        n = payload

    return {
        "http": http_status,
        "n": n,
        "statuses": dict(seen),
        "elapsed_s": elapsed,
        "err": None if 200 <= http_status < 300 else (text or "")[:200],
    }


def orders_url(shops: list[str], status_qs: str) -> str:
    qs = "&".join([f"shopIds={s}" for s in shops] + [status_qs, "page=0", f"size={SIZE}"])
    return f"{OPENAPI_BASE}/v2/fbs/orders?{qs}"


def count_url(shops: list[str], status_qs: str) -> str:
    qs = "&".join([f"shopIds={s}" for s in shops] + [status_qs])
    return f"{OPENAPI_BASE}/v2/fbs/orders/count?{qs}"


def main() -> None:
    global S1, S2
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", type=int, default=None)
    ap.add_argument("--s1", default=S1)
    ap.add_argument("--s2", default=S2)
    args = ap.parse_args()
    S1, S2 = args.s1.upper(), args.s2.upper()

    token, shops, uid = pick_token_and_shops(args.user)
    print(f"user_id={uid}  shops={shops}  (token loaded, not shown)\n")

    # ── Ground truth: one call per status, the way we do it today ────────
    print("BASELINE (bugungi usul — har status alohida):")
    base: dict[str, dict] = {}
    for st in (S1, S2):
        r = _get(token, orders_url(shops, f"status={st}"), f"probe.base[{st}]")
        base[st] = r
        print(f"  status={st:<10} HTTP {r['http']}  orders={r['n']}  {r['statuses']}  {r['elapsed_s']}s")

    b1 = base[S1].get("n") or 0
    b2 = base[S2].get("n") or 0
    print(f"\n  → {S1}={b1}, {S2}={b2}. Multi-status ISHLASA, javobda IKKALASI ham "
          f"chiqadi (jami {b1 + b2}).\n")

    # ── The variants ────────────────────────────────────────────────────
    variants = [
        ("A  /orders  status=X&status=Y  (takroriy param)",
         orders_url(shops, f"status={S1}&status={S2}")),
        ("B  /orders  status=X,Y         (vergul)",
         orders_url(shops, f"status={S1},{S2}")),
        ("C  /orders  statuses=X&statuses=Y (portal nomi, takroriy)",
         orders_url(shops, f"statuses={S1}&statuses={S2}")),
        ("D  /orders  statuses=X,Y       (portal nomi, vergul)",
         orders_url(shops, f"statuses={S1},{S2}")),
        ("E  /count   status=X&status=Y  (yig'indi qaytaradimi?)",
         count_url(shops, f"status={S1}&status={S2}")),
    ]

    print("VARIANTLAR:")
    results = []
    for label, url in variants:
        r = _get(token, url, f"probe.{label[:1]}")
        r["variant"] = label
        r["url"] = url.replace(OPENAPI_BASE, "")
        results.append(r)
        if 200 <= r["http"] < 300:
            print(f"  {label}\n      HTTP {r['http']}  n={r['n']}  statuses={r['statuses']}  {r['elapsed_s']}s")
        else:
            print(f"  {label}\n      HTTP {r['http']}  RAD ETILDI  {r['err']}")

    # ── Verdict ─────────────────────────────────────────────────────────
    print("\n--- XULOSA ---")
    winner = None
    for r in results[:4]:                      # /orders variants only
        if not (200 <= r["http"] < 300):
            print(f"  {r['variant'][:1]}: rad etildi (HTTP {r['http']}) — bu sintaksis emas.")
            continue
        got = set(r["statuses"])
        others = got - {S1, S2}
        if S1 in got and S2 in got and not others:
            print(f"  {r['variant'][:1]}: ✅ ISHLAYDI — javobda IKKALA status bor, "
                  f"begonasi yo'q: {r['statuses']}")
            winner = winner or r
        elif others:
            print(f"  {r['variant'][:1]}: ☠️  XAVFLI — filtr TASHLAB YUBORILGAN, begona "
                  f"statuslar ham keldi: {r['statuses']}. Bunday javobga prune qilib "
                  f"BO'LMAYDI (tirik qatorlarni o'chirardi).")
        elif got and got != {S1, S2}:
            only = ", ".join(sorted(got))
            print(f"  {r['variant'][:1]}: ⚠️  faqat «{only}» keldi — Uzum birinchi/oxirgi "
                  f"qiymatni olib, qolganini e'tiborsiz qoldirdi. Foydasi yo'q.")
        elif b1 or b2:
            # Baseline had orders, this variant returned none → the param name we
            # sent is UNKNOWN to Uzum, so it fell back to the swagger default
            # (status=CREATED, which is empty). Harmless, but useless.
            print(f"  {r['variant'][:1]}: ⚠️  bo'sh javob, lekin baseline'da "
                  f"{b1 + b2} ta order bor edi → Uzum bu parametr NOMINI tanimadi va "
                  f"default'ga (status=CREATED) tushib qoldi. Foydasi yo'q.")
        else:
            print(f"  {r['variant'][:1]}: ❔ bo'sh javob — baseline ham bo'sh, sinov "
                  f"hal qilmaydi. Buyurtmasi bor statuslar bilan qayta uring "
                  f"(--s1/--s2).")

    e = results[4]
    if 200 <= e["http"] < 300:
        print(f"  E (/count): HTTP 200, payload={e['n']} — kutilgan yig'indi {b1 + b2} "
              f"bo'lsa, u faqat bitta SON; badge'larga ajratib bo'lmaydi (baribir foydasiz).")
    else:
        print(f"  E (/count): HTTP {e['http']} — rad etildi.")

    print()
    if winner:
        print(f"NATIJA: ✅ multi-status ISHLAYDI ({winner['variant'][:1]} varianti). "
              f"Sahifaga kirish 5 so'rov → 1 so'rovga tushirilishi mumkin: "
              f"{'+'.join(ACTIVE)} ni bitta chaqiruvda tortib, badge'larni o'zimiz sanaymiz.")
    else:
        print("NATIJA: ❌ multi-status ishlamadi. Kirishdagi 3 ta /count'ni olib tashlash "
              "(badge'lar DB'dan) — yagona yo'l: 5 so'rov → 2.")

    print("\nraw:", json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
