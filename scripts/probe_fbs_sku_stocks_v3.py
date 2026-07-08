"""Probe whether GET /v3/fbs/sku/stocks supports a server-side search param.

READ-ONLY GET only, paced. If any param filters the result server-side, the
«Ombor» search can query Uzum directly (like «Новая поставка») instead of
only filtering the loaded pool.

Run INSIDE the app container:
    docker compose exec app python scripts/probe_fbs_sku_stocks_v3.py
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
        print(f"  [{label}] NETWORK ERROR {e}")
        return
    body = (resp.text or "")
    summary = body[:120]
    titles = ""
    try:
        j = resp.json()
        payload = j.get("payload") if isinstance(j, dict) else None
        if isinstance(payload, dict):
            lst = payload.get("skuAmountList")
            if isinstance(lst, list):
                summary = f"len={len(lst)}"
                titles = " | ".join(
                    str(s.get("skuTitle") or s.get("productTitle") or "")[:22]
                    for s in lst[:3])
    except Exception:
        pass
    print(f"  [{label}] HTTP {resp.status_code}  {summary}\n      first3: {titles}")


def main() -> None:
    token = _load_token()
    if not token:
        print("[probe] no token found")
        return
    print(f"[probe] token={token[:8]}..  (read-only GET, paced {GAP}s)\n")

    # Baseline (no filter) vs a candidate search term, across common param names.
    TERM = os.environ.get("PROBE_TERM", "stres")
    cases = [
        ("/v3/fbs/sku/stocks?page=0&size=5",                         "baseline size=5"),
        (f"/v3/fbs/sku/stocks?page=0&size=5&search={TERM}",          f"search={TERM}"),
        (f"/v3/fbs/sku/stocks?page=0&size=5&query={TERM}",          f"query={TERM}"),
        (f"/v3/fbs/sku/stocks?page=0&size=5&q={TERM}",              f"q={TERM}"),
        (f"/v3/fbs/sku/stocks?page=0&size=5&filter={TERM}",        f"filter={TERM}"),
        (f"/v3/fbs/sku/stocks?page=0&size=5&text={TERM}",          f"text={TERM}"),
        (f"/v3/fbs/sku/stocks?page=0&size=5&skuTitle={TERM}",      f"skuTitle={TERM}"),
        (f"/v3/fbs/sku/stocks?page=0&size=5&productTitle={TERM}",  f"productTitle={TERM}"),
        (f"/v3/fbs/sku/stocks?page=0&size=5&name={TERM}",          f"name={TERM}"),
    ]
    for path, label in cases:
        _get(token, path, label=label)
        time.sleep(GAP)


if __name__ == "__main__":
    main()
