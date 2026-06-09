"""Read-only probe: can we discover sellerId from Uzum OpenAPI?

Strategy (all GET, no state mutations):
  A. Walk every dict in /v1/shops response and print keys that look like a
     seller account ID (contains 'seller', 'merchant', 'owner', 'account',
     'sid', 'sId', 'id' but NOT 'shopId').
  B. Try a battery of plausible "current account" endpoints. Print HTTP
     status + first 200 chars of body for each.
  C. Same field walk for /v2/fbs/orders sample order + /v1/fbs/order/{id}.

If ANY endpoint returns 200 with a sellerId-shaped field, the user does
NOT have to paste it manually. Otherwise we have empirical confirmation
that Uzum hides this value.

Usage:
    docker exec sellerhub_uz-app-1 python /app/tools/probe_seller_id.py
"""
from __future__ import annotations

import json
import sys

import requests
from sqlalchemy import select

from extensions import SessionLocal
from models import User
from core.uzum_openapi import (
    OPENAPI_BASE,
    fetch_fbs_orders_page,
    list_owned_shops,
)


# Field-name patterns that smell like a "seller account ID".
# Explicitly EXCLUDES shopId, shop_id, shopName, etc.
SELLER_HINTS = (
    "seller", "merchant", "owner", "account",
    "supplier", "vendor", "company", "principal",
)
SHOP_HINTS = ("shop",)  # exclusion list


def looks_seller_id(key: str) -> bool:
    k = key.lower()
    if any(h in k for h in SHOP_HINTS):
        return False
    return any(h in k for h in SELLER_HINTS)


def walk_for_seller_fields(obj, path="", out=None):
    """Recursive walk — collect every key+value where the key smells
    like a seller-account identifier and the value is a number or
    numeric string."""
    if out is None:
        out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            new_path = f"{path}.{k}" if path else k
            if looks_seller_id(k):
                # Only record scalar values (the ID itself, not a nested obj)
                if isinstance(v, (int, str)) and str(v).strip():
                    out.append((new_path, k, v))
            walk_for_seller_fields(v, new_path, out)
    elif isinstance(obj, list):
        for i, item in enumerate(obj[:3]):  # only first 3 elements
            walk_for_seller_fields(item, f"{path}[{i}]", out)
    return out


def get_with_token(url: str, token: str) -> tuple[int, dict | str]:
    try:
        r = requests.get(
            url,
            headers={"Authorization": token, "Accept": "application/json"},
            timeout=12,
        )
    except Exception as e:
        return 0, repr(e)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, r.text[:300]


def main():
    with SessionLocal() as db:
        user = db.execute(
            select(User)
            .where(User.uzum_openapi_token.is_not(None))
            .order_by(User.is_admin.desc(), User.id)
            .limit(1)
        ).scalar_one_or_none()
        if user is None:
            print("FATAL: no user with uzum_openapi_token")
            sys.exit(1)
        token = user.uzum_openapi_token
        print(f"# Using token of user='{user.username}' (id={user.id})")
        print(f"# Token prefix: {token[:18]}...")
        print()

    # ---------------------------------------------------------------
    # A. Walk /v1/shops response (raw, before our normalization)
    # ---------------------------------------------------------------
    print("=" * 72)
    print("A. /v1/shops — raw response, scanning every key for seller-shaped ID")
    print("=" * 72)
    code, body = get_with_token(f"{OPENAPI_BASE}/v1/shops", token)
    print(f"HTTP {code}")
    if isinstance(body, dict):
        # Show top-level keys + walk
        print(f"Top-level keys: {list(body.keys())}")
        hits = walk_for_seller_fields(body)
        if hits:
            print("\n✓ POSSIBLE seller-id fields found in /v1/shops response:")
            for path, key, val in hits:
                print(f"    {path}  =  {val!r}")
        else:
            print("\n✗ No seller-shaped fields anywhere in /v1/shops.")
        # Also dump first shop verbatim so we can eyeball
        items = body.get("payload") or body.get("shops") or body.get("data") or []
        if isinstance(items, dict):
            items = items.get("shops") or items.get("data") or []
        if items:
            print("\nFirst shop object (verbatim):")
            print(json.dumps(items[0], ensure_ascii=False, indent=2)[:1200])
    else:
        print(f"Body (truncated): {str(body)[:400]}")

    # ---------------------------------------------------------------
    # B. Try a battery of "current account / me" endpoints
    # ---------------------------------------------------------------
    print()
    print("=" * 72)
    print("B. Probing plausible 'who am I' / 'current seller' endpoints")
    print("=" * 72)
    candidates = [
        "/v1/me",
        "/v1/account",
        "/v1/account/me",
        "/v1/account/info",
        "/v1/profile",
        "/v1/seller",
        "/v1/seller/me",
        "/v1/seller/info",
        "/v1/seller/profile",
        "/v1/seller/current",
        "/v1/sellers/me",
        "/v1/sellers/current",
        "/v1/user",
        "/v1/user/me",
        "/v1/merchant",
        "/v1/merchant/me",
        "/v1/auth/me",
        "/v1/auth/whoami",
        "/v1/info",
        "/v1/whoami",
    ]
    for path in candidates:
        url = f"{OPENAPI_BASE}{path}"
        code, body = get_with_token(url, token)
        snippet = ""
        if isinstance(body, dict):
            # If 200, scan for seller-shaped field
            if code == 200:
                hits = walk_for_seller_fields(body)
                marker = "✓ HIT" if hits else "200 OK (no seller field)"
                snippet = f"  {marker}  keys={list(body.keys())[:6]}"
                if hits:
                    for h in hits:
                        snippet += f"\n         {h[0]} = {h[2]!r}"
            else:
                snippet = "  " + json.dumps(body, ensure_ascii=False)[:160]
        else:
            snippet = "  " + str(body)[:160]
        print(f"  {code:>3}  GET {path}{snippet}")

    # ---------------------------------------------------------------
    # C. Walk a /v2/fbs/orders sample order for seller-shaped fields
    # ---------------------------------------------------------------
    print()
    print("=" * 72)
    print("C. /v2/fbs/orders sample order — any seller-shaped field?")
    print("=" * 72)
    try:
        shops = list_owned_shops(token)
        shop_ids = [s["uzum_id"] for s in shops[:3]]
        if shop_ids:
            for status in ("CREATED", "PACKING", "DELIVERING", "COMPLETED"):
                try:
                    body, _ = fetch_fbs_orders_page(
                        token, shop_ids, status=status, page=0, size=5,
                    )
                except Exception as e:
                    print(f"  status={status}: ERROR {e!r}")
                    continue
                payload = body.get("payload") or {}
                orders = payload.get("orders") or []
                if not orders:
                    continue
                sample = orders[0]
                hits = walk_for_seller_fields(sample)
                if hits:
                    print(f"  ✓ status={status} order has seller-shaped fields:")
                    for h in hits:
                        print(f"      {h[0]} = {h[2]!r}")
                else:
                    print(f"  ✗ status={status} sample order — no seller field. "
                          f"Top keys: {list(sample.keys())[:10]}")
                break  # one sample is enough
        else:
            print("  (no shops to probe)")
    except Exception as e:
        print(f"  ERROR: {e!r}")

    print()
    print("=" * 72)
    print("VERDICT:")
    print("  - If section A or C printed '✓ POSSIBLE seller-id fields' → we")
    print("    can extract sellerId from existing endpoints. Manual paste")
    print("    is NOT required.")
    print("  - If section B printed any '✓ HIT' → that endpoint gives us")
    print("    the sellerId for free.")
    print("  - If everything is ✗ / 4xx → empirical confirmation that the")
    print("    user MUST paste sId=<N> from cabinet URL.")
    print("=" * 72)


if __name__ == "__main__":
    main()
