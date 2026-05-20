#!/usr/bin/env python3
"""Probe: does params-based list lookup defuse the cross-wire race?

Hypothesis under test
---------------------
The race could be either of:

  (1) CREATE race — POST /documents/create returns a requestId that is
      already wired to the wrong file on the server.
  (2) POLL race   — GET  /documents/v2/{requestId} resolves requestId→file
      through a broken cache keyed on (token, window) that collides
      between concurrent callers.

If (1) alone: ignoring the create-returned requestId and finding our report
via GET /documents/v2?jobFilters=SELLS_REPORT&shopIds=<our>&size=N, matched
by (shopIds, dateFrom, dateTo), should hand us the correct requestId → the
subsequent GET /{requestId} returns our real file → SHAs are unique per shop.

If (2) or (1)+(2): even the list-matched requestId comes back with a
cross-wired link on GET /{requestId} → SHAs still collide.

Procedure
---------
Fire N concurrent create calls with IDENTICAL window but different shopIds
(the original race trigger). For each:

  1. Record the requestId the CREATE response gave us (`req_from_create`).
  2. Poll LIST /documents/v2?jobFilters=SELLS_REPORT&shopIds=<my_shop>&size=50
     until an entry with matching (shopIds, dateFrom, dateTo) exists AND is
     status=COMPLETED.
  3. Record that entry's requestId (`req_from_list`).
  4. GET /documents/v2/{req_from_list} to resolve the link.
  5. Download bytes and hash.

Diagnostics
-----------
  * `req_from_create != req_from_list` → the create response itself was
    cross-wired at handoff time.
  * cross-shop SHA collisions → the GET /{requestId} path is still poisoned.

Run inside the app container:

    docker exec warehouse_app_uzum_qty_and_row_fix-app-1 \
        python scripts/probe_uzum_list_lookup.py
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from config import APP_TZ  # noqa: E402
from core.auth_helpers import _get_admin_token  # noqa: E402
from core.http_client import http_json  # noqa: E402
from core import uzum_reports as _ur  # noqa: E402


_CREATE_URL = "https://api-seller.uzum.uz/api/seller/documents/create"
_LIST_URL = "https://api-seller.uzum.uz/api/seller/documents/v2"
_DETAIL_URL_T = "https://api-seller.uzum.uz/api/seller/documents/v2/{rid}"
_REPORT_HEADERS = {
    "Origin": "https://seller.uzum.uz",
    "Referer": "https://seller.uzum.uz/",
}

DEFAULT_SHOPS = ["5983", "7138", "10945", "19621"]


def _tashkent_to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=APP_TZ)
    return int(dt.timestamp() * 1000)


def create_one(shop_ids: list[int], date_from_ms: int, date_to_ms: int) -> str:
    body = {
        "idempotencyKey": str(uuid.uuid4()),
        "jobType": "SELLS_REPORT",
        "contentType": "CSV",
        "params": {
            "returns": False,
            "group": False,
            "shopIds": [int(s) for s in shop_ids],
            "dateFrom": int(date_from_ms),
            "dateTo": int(date_to_ms),
        },
    }
    resp = http_json(
        _CREATE_URL,
        method="POST",
        body=body,
        headers=dict(_REPORT_HEADERS),
        _get_admin_token=_get_admin_token,
    )
    payload = resp.get("payload") if isinstance(resp, dict) else None
    req_id = None
    if isinstance(payload, dict):
        req_id = payload.get("requestId") or payload.get("id")
    if not req_id and isinstance(resp, dict):
        req_id = resp.get("requestId")
    if not req_id:
        raise RuntimeError(f"no requestId in create: {resp!r:.200}")
    return str(req_id)


def _list_items(shop_uzum_id: int, size: int = 50) -> list[dict]:
    """GET the user-visible report list, filtered by shop.

    Uzum's seller dashboard sends this exact call; we send it identically.
    Returns the raw `payload` array of items, or [] on any oddness.
    """
    url = (
        f"{_LIST_URL}?jobFilters=SELLS_REPORT&shopIds={int(shop_uzum_id)}"
        f"&page=1&size={int(size)}"
    )
    resp = http_json(
        url,
        method="GET",
        headers=dict(_REPORT_HEADERS),
        _get_admin_token=_get_admin_token,
    )
    # Uzum returns a bare JSON array at the top level for the list endpoint.
    if isinstance(resp, list):
        return resp
    # Defensive: some deployments may wrap it in {payload: [...]} or similar.
    payload = resp.get("payload") if isinstance(resp, dict) else None
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("items", "content", "data", "results"):
            v = payload.get(key)
            if isinstance(v, list):
                return v
    return []


def _match_item(
    items: list[dict], shop_uzum_id: int, date_from_ms: int, date_to_ms: int
) -> dict | None:
    """Return the one list item whose params match our (shopIds, window)."""
    target_shop = int(shop_uzum_id)
    target_from = int(date_from_ms)
    target_to = int(date_to_ms)
    for item in items:
        if not isinstance(item, dict):
            continue
        params = item.get("params")
        if not isinstance(params, dict):
            continue
        shops = params.get("shopIds")
        if not isinstance(shops, list) or [int(s) for s in shops] != [target_shop]:
            continue
        if int(params.get("dateFrom") or 0) != target_from:
            continue
        if int(params.get("dateTo") or 0) != target_to:
            continue
        return item
    return None


def _resolve_link(request_id: str) -> str:
    """GET /documents/v2/{requestId} → extract the direct file link."""
    resp = http_json(
        _DETAIL_URL_T.format(rid=request_id),
        method="GET",
        headers=dict(_REPORT_HEADERS),
        _get_admin_token=_get_admin_token,
    )
    link = _ur._extract_download_link(resp)
    if not link:
        raise RuntimeError(f"detail had no link for req_id={request_id}: {resp!r:.200}")
    return link


def run_via_list_lookup(
    shop_uzum_id: int, date_from_ms: int, date_to_ms: int, *, max_wait_s: int = 300
) -> dict[str, Any]:
    """Create, then find our report via list + params, then download."""
    t_total = time.monotonic()

    t0 = time.monotonic()
    req_from_create = create_one([shop_uzum_id], date_from_ms, date_to_ms)
    t_create = time.monotonic() - t0

    # Poll the LIST endpoint until our (shop, window) entry is COMPLETED.
    deadline = time.monotonic() + max_wait_s
    attempt = 0
    schedule = (1.0, 3.0, 5.0, 10.0, 20.0)
    matched: dict | None = None
    status_seen = "UNKNOWN"

    while time.monotonic() < deadline:
        time.sleep(schedule[min(attempt, len(schedule) - 1)])
        attempt += 1
        try:
            items = _list_items(shop_uzum_id, size=50)
        except Exception:
            continue
        cand = _match_item(items, shop_uzum_id, date_from_ms, date_to_ms)
        if cand is None:
            continue
        status_seen = str(cand.get("status") or "")
        if status_seen == "COMPLETED":
            matched = cand
            break

    t_list = time.monotonic() - t0 - t_create
    if matched is None:
        return {
            "shop": shop_uzum_id,
            "req_from_create": req_from_create,
            "err": f"list never showed COMPLETED match (last status={status_seen})",
        }

    req_from_list = str(matched.get("requestId") or matched.get("id") or "")

    t1 = time.monotonic()
    link = _resolve_link(req_from_list)
    t_detail = time.monotonic() - t1

    t2 = time.monotonic()
    raw = _ur.download_csv(link)
    t_dl = time.monotonic() - t2

    rows = _ur.parse_sells_csv(raw)
    sha = hashlib.sha256(raw).hexdigest()[:16]

    return {
        "shop": shop_uzum_id,
        "req_from_create": req_from_create,
        "req_from_list": req_from_list,
        "ids_match": req_from_create == req_from_list,
        "list_attempts": attempt,
        "link": link,
        "sha16": sha,
        "bytes": len(raw),
        "rows": len(rows),
        "t_create": round(t_create, 2),
        "t_list_wait": round(t_list, 2),
        "t_detail": round(t_detail, 2),
        "t_dl": round(t_dl, 2),
        "t_total": round(time.monotonic() - t_total, 2),
        "err": None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shops", default=",".join(DEFAULT_SHOPS))
    ap.add_argument("--hours-back", type=int, default=24)
    ap.add_argument("--max-wait-s", type=int, default=300)
    ap.add_argument(
        "--rounds", type=int, default=1,
        help="Repeat the concurrent burst N times to strengthen the signal.",
    )
    args = ap.parse_args()

    shops = [int(s.strip()) for s in args.shops.split(",") if s.strip()]
    now = datetime.now(APP_TZ).replace(microsecond=0)
    window_from = now - timedelta(hours=args.hours_back)
    window_to = now
    df_ms = _tashkent_to_ms(window_from)
    dt_ms = _tashkent_to_ms(window_to)

    print("=" * 70)
    print("List-lookup race probe")
    print(f"  shops      = {shops}")
    print(f"  window     = {window_from.isoformat()} → {window_to.isoformat()}")
    print(f"  dateFrom   = {df_ms}  dateTo = {dt_ms}")
    print(f"  rounds     = {args.rounds}")
    print("=" * 70)

    summary: list[dict[str, Any]] = []

    for round_idx in range(1, args.rounds + 1):
        print(f"\n--- Round {round_idx}/{args.rounds} ---")
        t_wall = time.monotonic()
        results: list[dict[str, Any]] = [None] * len(shops)
        with ThreadPoolExecutor(max_workers=len(shops)) as pool:
            futs = {
                pool.submit(
                    run_via_list_lookup, sid, df_ms, dt_ms, max_wait_s=args.max_wait_s
                ): idx
                for idx, sid in enumerate(shops)
            }
            for fut in futs:
                idx = futs[fut]
                try:
                    results[idx] = fut.result()
                except Exception as exc:
                    results[idx] = {"shop": shops[idx], "err": repr(exc)}
        wall = round(time.monotonic() - t_wall, 2)

        # Per-shop lines.
        for r in results:
            if r.get("err"):
                print(
                    f"  shop={r['shop']:<6} ERR {r['err'][:120]}  "
                    f"(req_from_create={r.get('req_from_create','-')})"
                )
                continue
            match_tag = "=" if r["ids_match"] else "≠"
            print(
                f"  shop={r['shop']:<6} sha16={r['sha16']}  rows={r['rows']:<5} "
                f"bytes={r['bytes']:<8}  "
                f"req_create={r['req_from_create']}  {match_tag}  "
                f"req_list={r['req_from_list']}  "
                f"t_list_wait={r['t_list_wait']}s t_total={r['t_total']}s"
            )

        # Race detection via sha collision.
        ok = [r for r in results if not r.get("err")]
        shas: dict[str, list[int]] = {}
        for r in ok:
            shas.setdefault(r["sha16"], []).append(r["shop"])
        non_empty_dups = [
            (sha, lst) for sha, lst in shas.items() if len(lst) > 1 and any(
                r["rows"] > 0 for r in ok if r["sha16"] == sha
            )
        ]
        empty_dups = [
            (sha, lst) for sha, lst in shas.items() if len(lst) > 1 and all(
                r["rows"] == 0 for r in ok if r["sha16"] == sha
            )
        ]
        id_mismatches = [r for r in ok if not r["ids_match"]]

        print(f"  wall = {wall}s")
        if id_mismatches:
            print(
                f"  >>> CREATE-RESPONSE MISMATCH on {len(id_mismatches)} shop(s): "
                "list-matched requestId differed from create-returned requestId"
            )
            for r in id_mismatches:
                print(
                    f"      shop={r['shop']}  create={r['req_from_create']}  "
                    f"list={r['req_from_list']}"
                )
        if non_empty_dups:
            for sha, shops_hit in non_empty_dups:
                print(f"  >>> RACE STILL FIRES: sha={sha} shared by shops {shops_hit}")
        else:
            print("  >>> CLEAN: every non-empty CSV unique per shop")
        if empty_dups:
            for sha, shops_hit in empty_dups:
                print(f"  (empty-CSV collision: sha={sha} shops={shops_hit}) — expected")

        summary.append(
            {
                "round": round_idx,
                "ok": len(ok),
                "errors": len(results) - len(ok),
                "id_mismatches": len(id_mismatches),
                "nonempty_dups": len(non_empty_dups),
                "empty_dups": len(empty_dups),
                "wall": wall,
            }
        )

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"{'round':<6}{'ok':<4}{'err':<5}{'id_mismatch':<14}{'nonempty_dup':<15}{'empty_dup':<12}wall")
    for s in summary:
        print(
            f"{s['round']:<6}{s['ok']:<4}{s['errors']:<5}"
            f"{s['id_mismatches']:<14}{s['nonempty_dups']:<15}"
            f"{s['empty_dups']:<12}{s['wall']}s"
        )

    print()
    total_id = sum(s["id_mismatches"] for s in summary)
    total_neraces = sum(s["nonempty_dups"] for s in summary)
    total_ok = sum(s["ok"] for s in summary)
    total_err = sum(s["errors"] for s in summary)
    if total_ok == 0:
        print("VERDICT: probe inconclusive — no shops completed the list lookup.")
        print(f"         Errors: {total_err}. Check list-endpoint response shape / timeouts.")
        return 0
    if total_id == 0 and total_neraces == 0:
        print("VERDICT: list-lookup path delivered correct file on every shop.")
        print("         Wiring wait_for_report to use list+params lookup is a valid fix.")
    elif total_id > 0 and total_neraces == 0:
        print("VERDICT: create-response cross-wired the requestId, but list-matched")
        print("         requestId still resolved to the CORRECT file. The create")
        print("         response is the broken surface; list-lookup defuses it.")
    elif total_neraces > 0:
        print("VERDICT: the race survives list-lookup — GET /documents/v2/{rid}")
        print("         is itself poisoned under concurrency. Path C remains the")
        print("         only safe fix.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
