"""Probe WHY GET /v1/fbs/invoice returns bad-request-001.

READ-ONLY: only GET calls against the invoice-list endpoint, paced. Tries
several path/param shapes to find the one Uzum accepts.

Run INSIDE the app container (same egress IP + token as production):
    docker compose exec app python scripts/probe_fbs_invoice_list.py
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

OPENAPI_BASE = "https://api-seller.uzum.uz/api/seller-openapi"
GAP = 2.5


def _load_token() -> str:
    env_tok = os.environ.get("UZUM_PROBE_TOKEN", "").strip()
    if env_tok:
        return env_tok
    from app import app as flask_app
    from models import User
    from extensions import SessionLocal
    from sqlalchemy import select
    with flask_app.app_context():
        with SessionLocal() as db:
            return (db.execute(
                select(User.uzum_openapi_token).where(
                    User.uzum_openapi_token.isnot(None)
                ).limit(1)
            ).scalar_one_or_none() or "").strip()


def _get(token: str, path_and_qs: str, *, label: str) -> None:
    url = f"{OPENAPI_BASE}{path_and_qs}"
    headers = {
        "Accept": "application/json",
        "User-Agent": "uzum-warehouse-app/1.0 (+openapi-client)",
        "Authorization": token,
        "Accept-Language": "uz",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=30)
    except requests.RequestException as e:
        print(f"  [{label}] NETWORK ERROR {e}\n    url={url}")
        return
    body = (resp.text or "")[:180]
    print(f"  [{label}] HTTP {resp.status_code}\n    url={url}\n    body={body!r}")


def main() -> None:
    token = _load_token()
    if not token:
        print("[probe] no token found")
        return
    print(f"[probe] token={token[:8]}..  (read-only GET, paced {GAP}s)\n")

    cases = [
        ("/v1/fbs/invoice?statuses=CREATED&page=0&size=50",        "v1 invoice single CREATED"),
        ("/v1/fbs/invoice?statuses=CREATED",                       "v1 invoice CREATED no-paging"),
        ("/v1/fbs/invoice?statuses=CREATED&statuses=ACCEPTANCE_IN_PROGRESS", "v1 invoice 2 statuses repeated"),
        ("/v1/fbs/invoice?statuses=CREATED,ACCEPTANCE_IN_PROGRESS", "v1 invoice 2 statuses comma"),
        ("/v1/fbs/invoice",                                        "v1 invoice NO statuses"),
        ("/v1/fbs/invoice?page=0&size=20",                         "v1 invoice paging-only no-statuses"),
        ("/v1/fbs/invoices?statuses=CREATED&page=0&size=20",       "v1 invoiceS plural"),
        ("/v2/fbs/invoice?statuses=CREATED&page=0&size=20",        "v2 invoice"),
    ]
    for path, label in cases:
        _get(token, path, label=label)
        time.sleep(GAP)


if __name__ == "__main__":
    main()
