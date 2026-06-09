"""Last-ditch probe: is the OpenAPI token itself a JWT with sellerId inside?

If it's a JWT (header.payload.signature), the payload is base64-JSON
and may contain {sub, sellerId, accountId, ...} — in which case we can
extract sellerId without asking the user to paste anything.

Read-only — only decodes locally, no network calls.
"""
from __future__ import annotations

import base64
import json
import sys

from sqlalchemy import select

from extensions import SessionLocal
from models import User


def decode_jwt_payload(token: str) -> dict | str:
    parts = token.split(".")
    if len(parts) != 3:
        return f"NOT A JWT (has {len(parts)} dot-segments, JWT has 3)"
    payload_b64 = parts[1]
    payload_b64 += "=" * (-len(payload_b64) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload_b64)
        return json.loads(raw)
    except Exception as e:
        return f"DECODE_ERROR: {e!r}"


def main():
    with SessionLocal() as db:
        users = db.execute(
            select(User)
            .where(User.uzum_openapi_token.is_not(None))
            .order_by(User.id)
        ).scalars().all()
        if not users:
            print("No users with uzum_openapi_token")
            sys.exit(1)
        for u in users:
            tok = u.uzum_openapi_token
            print(f"# user={u.username!r} (id={u.id})")
            print(f"  token length: {len(tok)}")
            print(f"  token prefix: {tok[:30]}...")
            print(f"  dot-segments: {tok.count('.')}")
            decoded = decode_jwt_payload(tok)
            if isinstance(decoded, dict):
                print(f"  ✓ JWT payload: {json.dumps(decoded, ensure_ascii=False, indent=2)}")
            else:
                print(f"  ✗ {decoded}")
            print()


if __name__ == "__main__":
    main()
