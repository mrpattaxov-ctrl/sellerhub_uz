"""READ-ONLY probe: can OpenAPI /count return ALL statuses in ONE request?

Why (Abdulaziz 2026-07-14): the Uzum PORTAL's own count endpoint
(`api-seller.uzum.uz/api/seller/fbs/orders/count`, HAR t2.har) answers with the
counter for EVERY status in a single reply — even though the request only asked
for `statuses=CREATED`:

    {"payload":{"ordersCount":[{"status":"CREATED","counter":0},
                               {"status":"PACKING","counter":1}, ... 11 rows]}}

Our OpenAPI reader (core/uzum_openapi.py:876 fetch_fbs_orders_count) instead
sends `status=<one>` and expects a scalar `{"payload": <int>}` (swagger
2026-05-21). So the badge refresh costs 4 calls where the portal spends 1.

The two endpoints have different paths, so this is NOT a given. Ask the server.

Variants (all GET, all read-only):
  A  status=CREATED                 — baseline, what we send today
  B  (status omitted)               — does it fall back to "all"?
  C  statuses=CREATED               — plural, like the portal
  D  status=CREATED&status=PACKING  — repeated param
  E  statuses=CREATED,PACKING       — comma list
  F  statuses=CREATED + places=STOCK,DROP_OFF   — full portal-shaped query

Decisive signal: payload stops being a bare int and becomes an object/list with
per-status counters → we can collapse 4 calls into 1. HTTP 400 → rejected.

SAFETY: GET only, paced through the shared token bucket, token never printed.

Run:
    docker compose exec -T -e PYTHONPATH=/app app python tools/probe_fbs_count_multistatus.py
"""
from __future__ import annotations

import json
import sys

from sqlalchemy import func, select

from extensions import SessionLocal
from models import Shop, User
from core.uzum_openapi import OPENAPI_BASE, _fbs_orders_request_with_auth


def pick_token_and_shops():
    with SessionLocal() as db:
        user = db.execute(
            select(User)
            .where(func.length(func.coalesce(User.uzum_openapi_token, "")) > 0)
            .order_by(User.id)
        ).scalars().first()
        if user is None:
            sys.exit("OpenAPI tokenli foydalanuvchi yo'q.")
        shops = db.execute(
            select(Shop.uzum_id).where(Shop.owner_id == user.id)
        ).scalars().all()
        return (user.uzum_openapi_token or "").strip(), [str(s) for s in shops if s], int(user.id)


def _get(token: str, url: str, label: str):
    parsed, http, text, _ = _fbs_orders_request_with_auth(
        url, token, accept_language=None, debug_label=label, fail_fast=True,
    )
    return http, parsed, (text or "")[:300]


def _shape(payload) -> str:
    if isinstance(payload, bool):
        return "bool"
    if isinstance(payload, int):
        return f"INT ({payload})  <- bitta son (bugungi holat)"
    if isinstance(payload, dict):
        return f"DICT keys={list(payload.keys())[:8]}"
    if isinstance(payload, list):
        return f"LIST len={len(payload)} first={json.dumps(payload[:1], ensure_ascii=False)[:160]}"
    return f"{type(payload).__name__} = {payload!r}"


def main() -> None:
    token, shops, uid = pick_token_and_shops()
    print(f"user_id={uid}  shops={shops}\n")

    shop_qs = "&".join(f"shopIds={s}" for s in shops)
    base = f"{OPENAPI_BASE}/v2/fbs/orders/count?{shop_qs}"

    variants = [
        ("A  status=CREATED (bugungi)",            base + "&status=CREATED"),
        ("B  status YO'Q",                         base),
        ("C  statuses=CREATED (ko'plik)",          base + "&statuses=CREATED"),
        ("D  status=CREATED&status=PACKING",       base + "&status=CREATED&status=PACKING"),
        ("E  statuses=CREATED,PACKING (vergul)",   base + "&statuses=CREATED,PACKING"),
        ("F  statuses + places (portal shakli)",
         base + "&statuses=CREATED&places=STOCK,DROP_OFF"),
    ]

    print("=" * 78)
    for name, url in variants:
        try:
            http, parsed, err = _get(token, url, f"probe.count[{name[0]}]")
        except Exception as e:
            print(f"{name:<40} XATO: {e!r}")
            print("-" * 78)
            continue
        if isinstance(parsed, dict) and "payload" in parsed:
            print(f"{name:<40} HTTP {http}")
            print(f"     payload: {_shape(parsed['payload'])}")
            if isinstance(parsed["payload"], (dict, list)):
                print("     XOM:", json.dumps(parsed["payload"], ensure_ascii=False)[:600])
        else:
            print(f"{name:<40} HTTP {http}  javob: {err}")
        print("-" * 78)

    print("\nXULOSA:")
    print("  Agar biror variantda payload DICT/LIST bo'lib per-status counter bersa")
    print("  -> loadCounts(true) 4 so'rovdan 1 ga tushadi.")
    print("  Hammasi INT bo'lsa -> OpenAPI'da bunday imkoniyat YO'Q, portal-only.")


if __name__ == "__main__":
    main()
