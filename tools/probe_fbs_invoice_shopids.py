"""READ-ONLY probe: can /v1/fbs/invoice filter by shopIds — and does an invoice
carry ANY shop identifier?

Why (Abdulaziz 2026-07-14): the накладные list currently costs 3 Uzum calls,
one of which is a FULL PENDING_DELIVERY drain whose only job is to answer "is
this invoice mine?" (fbs/routes.py:4468 _live_owned_invoice_numbers). Uzum
returns every invoice on the seller ACCOUNT, including shops the user never
registered here, and the invoice payload — per swagger and our reader — has no
shopId, only a `stock` (warehouse). Hence the drain.

If Uzum accepts `shopIds` on the invoice list (like it does on /v2/fbs/orders),
the drain disappears entirely: we just ask for our own shops' invoices.

Swagger says the only params are statuses/page/size. Swagger has been wrong
before (size=400 on invoice, plural `statuses` on the portal), so we ask the
server, not the doc.

Variants:
  A  ?shopIds=<each shop>       repeated param, like /v2/fbs/orders
  B  ?shopId=<one shop>         singular
  C  ?shopIds=<csv>             comma list
  D  baseline (no shop param)   — the count we compare against

Decisive signals:
  * a variant returns FEWER invoices than the baseline → the filter WORKS;
  * same count → the param was IGNORED (useless, and dangerous to trust);
  * HTTP 400 → rejected.

It also dumps the KEYS of one invoice, so we can see whether ANY field
identifies the shop (which would let us filter locally, still killing the drain).

SAFETY: GET only. Paced through the shared bucket. Token never printed.

Run:
    docker compose exec -e PYTHONPATH=/app app python tools/probe_fbs_invoice_shopids.py
"""
from __future__ import annotations

import json
import sys

from sqlalchemy import func, select

from extensions import SessionLocal
from models import Shop, User
from core.uzum_openapi import OPENAPI_BASE, _fbs_orders_request_with_auth

STATUS = "CREATED"   # the накладная status the seller actually watches


def pick_token_and_shops(user_id: int | None = None):
    with SessionLocal() as db:
        q = select(User).where(func.length(func.coalesce(User.uzum_openapi_token, "")) > 0)
        if user_id is not None:
            q = q.where(User.id == int(user_id))
        user = db.execute(q.order_by(User.id)).scalars().first()
        if user is None:
            sys.exit("No user with an OpenAPI token.")
        shops = db.execute(
            select(Shop.uzum_id).where(Shop.owner_id == user.id)
        ).scalars().all()
        return (user.uzum_openapi_token or "").strip(), [str(s) for s in shops if s], int(user.id)


def _get(token: str, url: str, label: str):
    parsed, http, text, _ = _fbs_orders_request_with_auth(
        url, token, accept_language=None, debug_label=label, fail_fast=True,
    )
    invoices = []
    if isinstance(parsed, dict) and isinstance(parsed.get("payload"), list):
        invoices = [i for i in parsed["payload"] if isinstance(i, dict)]
    return http, invoices, (text or "")[:160]


def main() -> None:
    token, shops, uid = pick_token_and_shops()
    print(f"user_id={uid}  shops={shops}\n")

    # The probe is meaningless against an empty status (0 == 0 proves nothing).
    # Walk the enum and pick the first status that actually HAS invoices.
    status = STATUS
    for candidate in ("CREATED", "ACCEPTED", "ACCEPTANCE_IN_PROGRESS", "CANCELLED"):
        http, invs, _e = _get(
            token, f"{OPENAPI_BASE}/v1/fbs/invoice?statuses={candidate}&page=0",
            f"probe.pick[{candidate}]",
        )
        print(f"  {candidate:<24} HTTP {http}  накладная = {len(invs)}")
        if 200 <= http < 300 and invs:
            status = candidate
            break
    else:
        sys.exit("\nHech bir statusda накладная yo'q — sinov hal qilmaydi.")

    print(f"\nSinov statusi: {status}\n")
    base = f"{OPENAPI_BASE}/v1/fbs/invoice?statuses={status}&page=0"

    variants = [
        ("D  baseline (do'kon parametri YO'Q)", base),
        ("A  ?shopIds=X&shopIds=Y (takroriy)",
         base + "".join(f"&shopIds={s}" for s in shops)),
        ("B  ?shopId=X (birlik, 1-do'kon)", base + f"&shopId={shops[0]}"),
        ("C  ?shopIds=X,Y (vergul)", base + "&shopIds=" + ",".join(shops)),
    ]

    results = []
    for label, url in variants:
        http, invs, err = _get(token, url, f"probe.inv[{label[:1]}]")
        results.append((label, http, len(invs), invs, err))
        if 200 <= http < 300:
            print(f"  {label}\n      HTTP {http}  накладная soni = {len(invs)}")
        else:
            print(f"  {label}\n      HTTP {http}  RAD ETILDI  {err}")

    baseline_n = results[0][2] if 200 <= results[0][1] < 300 else None

    # ── Does an invoice carry ANY shop identifier? ──────────────────────
    sample = next((invs[0] for (_l, h, n, invs, _e) in results if n), None)
    print("\n--- NAKLADNAYA MAYDONLARI (do'kon belgisi bormi?) ---")
    if sample is None:
        print("  Namuna yo'q — bu statusda накладная topilmadi.")
    else:
        keys = sorted(sample.keys())
        print(f"  maydonlar: {keys}")
        hits = [k for k in keys if "shop" in k.lower() or "seller" in k.lower()]
        print("  do'kon/sotuvchi bilan bog'liq maydon: %s" % (hits if hits else "YO'Q"))
        print("  stock:", json.dumps(sample.get("stock"), ensure_ascii=False)[:200])

    # ── Verdict ────────────────────────────────────────────────────────
    print("\n--- XULOSA ---")
    if baseline_n is None:
        print("  Baseline ishlamadi — token/status'ni tekshiring.")
        return
    print(f"  Baseline (filtrsiz): {baseline_n} ta накладная")
    for label, http, n, _invs, _e in results[1:]:
        if not (200 <= http < 300):
            print(f"  {label[:1]}: ❌ HTTP {http} — bu parametr qabul qilinmaydi.")
        elif n < baseline_n:
            print(f"  {label[:1]}: ✅ FILTRLADI — {baseline_n} → {n}. Drenajni "
                  f"O'CHIRSA BO'LADI.")
        else:
            print(f"  {label[:1]}: ⚠️  E'TIBORSIZ QOLDIRDI — soni o'zgarmadi "
                  f"({n}). Uzum parametrni tanimadi; ishonib bo'lmaydi.")


if __name__ == "__main__":
    main()
