"""READ-ONLY probe: does the seller-OpenAPI expose the identifier TYPE?

Why: the browser/portal API returns a PER-ITEM `identifierInfo` block —

    "identifierInfo": {"type": "ASL_BELGISI", "required": false, "values": []}

— so Uzum distinguishes IMEI from ASL_BELGISI (O'zbekiston markirovka kodi).
Our sync only reads the ORDER-level `identifierRequired` bool and labels every
such order "IMEI", which is wrong for ASL_BELGISI goods (Abdulaziz, 2026-07-14:
«Магнит браслет» asked for ASL Belgisi, we said IMEI).

Before changing anything we must know whether the OPENAPI (the API our app
actually uses — a DIFFERENT surface from the portal) carries the same field.

Reads only:  GET /v1/fbs/order/{orderId}   — no writes, 1 request per order.

Run inside the container:
    docker compose exec -T -e PYTHONPATH=/app app python tools/probe_fbs_identifier_type.py 116914839
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.uzum_openapi import OPENAPI_BASE, _fbs_orders_request_with_auth  # noqa: E402


def _token_for_order(order_id: str) -> tuple[str, str]:
    """Return (token, shop_id) of the user owning this order."""
    from app import app as flask_app
    from extensions import SessionLocal
    from models import FbsOrder, Shop, User
    from sqlalchemy import select

    with flask_app.app_context():
        with SessionLocal() as db:
            row = db.execute(
                select(FbsOrder.shop_id).where(FbsOrder.order_id == str(order_id))
            ).first()
            if not row:
                raise SystemExit(f"order {order_id} not in fbs_orders")
            shop_id = str(row[0])
            shop = db.execute(
                select(Shop).where(Shop.uzum_id == shop_id)
            ).scalars().first()
            if not shop:
                raise SystemExit(f"shop {shop_id} not registered")
            user = db.get(User, shop.owner_id)
            token = (user.uzum_openapi_token or "").strip() if user else ""
            if not token:
                raise SystemExit("owner has no OpenAPI token")
            return token, shop_id


def main() -> None:
    order_id = sys.argv[1] if len(sys.argv) > 1 else "116914839"
    token, shop_id = _token_for_order(order_id)
    print(f"order={order_id} shop={shop_id}\n")

    url = f"{OPENAPI_BASE}/v1/fbs/order/{int(order_id)}"
    parsed, status, text, _ = _fbs_orders_request_with_auth(
        url, token, method="GET", accept_language=None,
        debug_label=f"probe.order[{order_id}]", fail_fast=True,
    )
    print(f"HTTP {status}")
    if not (200 <= status < 300):
        print("BODY:", text[:400])
        return

    payload = parsed.get("payload") if isinstance(parsed, dict) else parsed
    if not isinstance(payload, dict):
        print("kutilmagan javob shakli:", str(parsed)[:300])
        return

    print("ORDER-LEVEL:")
    print("  identifierRequired =", payload.get("identifierRequired"))
    print("  top-level kalitlar  =", sorted(payload.keys()))

    items = payload.get("orderItems") or []
    print(f"\nORDER ITEMS ({len(items)}):")
    for it in items:
        if not isinstance(it, dict):
            continue
        info = it.get("identifierInfo", "<KALIT YO'Q>")
        print(f"  item id={it.get('id')} sku={it.get('skuTitle')!r}")
        print(f"    identifierInfo = {json.dumps(info, ensure_ascii=False)}")
        print(f"    item kalitlari = {sorted(it.keys())}")

    print("\nXULOSA:")
    has = any(isinstance(it, dict) and "identifierInfo" in it for it in items)
    print("  OpenAPI identifierInfo (tur) beradi" if has
          else "  OpenAPI identifierInfo BERMAYDI → turni portal API'sidan olish kerak")


if __name__ == "__main__":
    main()
