#!/usr/bin/env python3
"""Probe: can we mint scoped sub-tokens off the admin account?

POSTs to https://api.uzum.uz/api/seller/open-api/token using the admin's
bearer JWT (same one we already use for /api-seller.uzum.uz calls).

Usage (inside the app container):

    docker exec warehouse_app_uzum_qty_and_row_fix-app-1 \
        python scripts/probe_uzum_create_subtoken.py --name subtoken1

    # restrict to a subset of shops:
    python scripts/probe_uzum_create_subtoken.py \
        --name subtoken2 --shops 5983,10945

Prints the raw response so we can see:
  * whether the endpoint accepts our bearer at all (401/403 vs 200)
  * the returned token string (if any) — needed if we later test whether
    sub-tokens dodge the cross-shop race
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.auth_helpers import _get_admin_token  # noqa: E402
from core.http_client import http_json  # noqa: E402


_TOKEN_URL = "https://api.uzum.uz/api/seller/open-api/token"

DEFAULT_SHOPS = [5983, 7138, 10920, 10945, 13505, 19621, 23059, 40571, 51948, 81374]


def create_subtoken(name: str, shop_ids: list[int], readonly: bool, expiration: str) -> dict:
    body = {
        "name": name,
        "readonly": readonly,
        "expirationTime": expiration,
        "shopIds": [int(s) for s in shop_ids],
        "metadata": {"shopIds": [int(s) for s in shop_ids]},
    }
    headers = {
        "Origin": "https://seller.uzum.uz",
        "Referer": "https://seller.uzum.uz/",
    }
    resp = http_json(
        _TOKEN_URL,
        method="POST",
        body=body,
        headers=headers,
        _get_admin_token=_get_admin_token,
    )
    return resp


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True, help="Sub-token name, e.g. subtoken1")
    ap.add_argument(
        "--shops",
        default=",".join(str(s) for s in DEFAULT_SHOPS),
        help="Comma-separated Uzum shopIds (defaults to all 10 admin shops)",
    )
    ap.add_argument("--readonly", action="store_true", help="Create a read-only token")
    ap.add_argument("--expiration", default="ONE_MONTH", help="Uzum expirationTime enum")
    args = ap.parse_args()

    shop_ids = [int(s.strip()) for s in args.shops.split(",") if s.strip()]
    bearer = _get_admin_token()
    if not bearer:
        print("[ERROR] no admin bearer token in DB — run auto-login first", file=sys.stderr)
        return 2

    print(f"[probe] POST {_TOKEN_URL}")
    print(f"[probe] name={args.name!r} shops={shop_ids} readonly={args.readonly} exp={args.expiration}")
    print(f"[probe] bearer prefix={bearer[:24]}... len={len(bearer)}")

    try:
        resp = create_subtoken(args.name, shop_ids, args.readonly, args.expiration)
    except Exception as e:
        print(f"[ERROR] request failed: {e!r}")
        return 1

    print("[probe] response:")
    print(json.dumps(resp, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
