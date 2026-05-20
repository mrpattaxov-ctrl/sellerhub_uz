"""Probe whether the Uzum /documents/create race is avoidable with a trivial fix.

Question being tested: is the cross-shop CSV contamination actually a Uzum-side
*dedup/cache* keyed on ``(token, dateFrom, dateTo)`` — in which case perturbing
any one of those inputs per shop defuses it — or is it a true server worker
race that we can only avoid by collapsing to a single bulk call (Path C)?

Three concurrent-burst tests, all on the same admin token, 4 shops, short window
(last ~2 hours wall-clock). No DB writes. Each test fires 4 ``/documents/create``
calls in parallel via ``ThreadPoolExecutor(4)``, waits for each to COMPLETE,
downloads the CSV, computes ``sha256(raw_bytes)`` and row count. Identical
sha256 between two shops = race fired.

  A. CONTROL       — identical dateFrom/dateTo on all 4 calls. Mirrors the
                     original reproducer. Race expected.
  B. MS OFFSET     — dateFrom offset by shop_index milliseconds
                     (+1, +2, +3, +4 ms). Hypothesis: Uzum keys its dedup
                     cache on the exact ms timestamp pair, so a 1ms nudge
                     gives each call its own slot. If race gone → keep
                     per-shop architecture, add offsets, done.
  C. PADDED LIST   — same dateFrom/dateTo but ``shopIds=[s, s]`` duplicated.
                     Probes whether the dedup key includes the literal
                     shopIds array shape.

Run inside the app container so imports + token-getter + DB session work:

    docker exec -it warehouse_app_uzum_qty_and_row_fix-app-1 \\
        python scripts/probe_uzum_race_cause.py
"""
from __future__ import annotations

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

from sqlalchemy import select

from config import APP_TZ
from core.auth_helpers import _get_admin_token
from core.http_client import http_json
from core import uzum_reports as _ur
from extensions import SessionLocal
from models import Shop, Variant, ProductGroup


_CREATE_URL = "https://api-seller.uzum.uz/api/seller/documents/create"
_REPORT_HEADERS = {
    "Origin": "https://seller.uzum.uz",
    "Referer": "https://seller.uzum.uz/",
}

DEFAULT_SHOPS = ["5983", "7138", "10945", "19621"]


def _tashkent_to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=APP_TZ)
    return int(dt.timestamp() * 1000)


def create_one(
    shop_ids: list[int],
    date_from_ms: int,
    date_to_ms: int,
) -> str:
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
        raise RuntimeError(f"no requestId in response: {resp!r:.200}")
    return str(req_id)


def run_one_call(
    shop_uzum_id: int,
    uses_shop_ids: list[int],
    date_from_ms: int,
    date_to_ms: int,
) -> dict[str, Any]:
    """Fire one create → poll → download. Returns per-call record."""
    t0 = time.monotonic()
    req_id = create_one(uses_shop_ids, date_from_ms, date_to_ms)
    t_create = time.monotonic() - t0

    t1 = time.monotonic()
    file_url = _ur.wait_for_report(req_id, token_getter=_get_admin_token, max_wait_s=300)
    t_poll = time.monotonic() - t1

    t2 = time.monotonic()
    raw = _ur.download_csv(file_url)
    t_dl = time.monotonic() - t2

    rows = _ur.parse_sells_csv(raw)
    sha = hashlib.sha256(raw).hexdigest()[:16]

    return {
        "shop": shop_uzum_id,
        "req_id": req_id,
        "file_url": file_url,
        "bytes": len(raw),
        "rows": len(rows),
        "sha16": sha,
        "t_create": round(t_create, 2),
        "t_poll": round(t_poll, 2),
        "t_dl": round(t_dl, 2),
    }


def run_burst(
    label: str,
    shop_ids: list[int],
    *,
    window_from: datetime,
    window_to: datetime,
    per_shop_ms_offset: int = 0,
    padded_list: bool = False,
) -> list[dict[str, Any]]:
    """Fire N concurrent per-shop creates in one burst.

    ``per_shop_ms_offset`` — add (i * offset) ms to dateFrom for shop at index i.
    ``padded_list``         — send shopIds=[shop, shop] instead of [shop].
    """
    base_from_ms = _tashkent_to_ms(window_from)
    base_to_ms = _tashkent_to_ms(window_to)

    print(f"\n=== {label} ===")
    print(f"  window={window_from.isoformat()} → {window_to.isoformat()}")
    print(f"  offset={per_shop_ms_offset}ms/shop  padded_list={padded_list}")

    tasks: list[tuple[int, list[int], int, int]] = []
    for i, sid in enumerate(shop_ids):
        from_ms = base_from_ms + i * per_shop_ms_offset
        shop_list = [sid, sid] if padded_list else [sid]
        tasks.append((sid, shop_list, from_ms, base_to_ms))

    results: list[dict[str, Any]] = [None] * len(tasks)
    with ThreadPoolExecutor(max_workers=len(tasks)) as ex:
        futs = {
            ex.submit(run_one_call, sid, shop_list, fm, tm): idx
            for idx, (sid, shop_list, fm, tm) in enumerate(tasks)
        }
        for fut in futs:
            idx = futs[fut]
            try:
                results[idx] = fut.result()
            except Exception as exc:
                results[idx] = {"shop": tasks[idx][0], "error": repr(exc)}

    for r in results:
        if "error" in r:
            print(f"  shop={r['shop']:<6} ERROR {r['error']}")
        else:
            print(
                f"  shop={r['shop']:<6} sha16={r['sha16']}  rows={r['rows']:<5} "
                f"bytes={r['bytes']:<8} req={r['req_id'][:18]}…  "
                f"t_create={r['t_create']}s t_poll={r['t_poll']}s"
            )

    # Cross-wiring detection via sha dupes.
    shas: dict[str, list[int]] = {}
    for r in results:
        if "error" in r:
            continue
        shas.setdefault(r["sha16"], []).append(r["shop"])
    dup_groups = [v for v in shas.values() if len(v) > 1]
    if dup_groups:
        print(f"  >>> RACE FIRED: shops sharing identical CSV: {dup_groups}")
    else:
        print("  >>> CLEAN: all CSVs distinct")

    return results


def check_routing_sanity(results: list[dict[str, Any]]) -> None:
    """For each CSV, look at the returned sku_ids and see how many
    map to the shop we THINK we asked for. Requires the catalog join,
    so we only run it when `--deep` is passed to avoid slow DB hits.
    """
    with SessionLocal() as db:
        pg_rows = db.execute(
            select(Variant.sku, ProductGroup.shop_id)
            .join(ProductGroup, Variant.group_id == ProductGroup.id)
            .where(Variant.sku.is_not(None))
        ).all()
        shop_pk_by_uzum = {
            int(str(uid).strip()): int(pk)
            for pk, uid in db.execute(
                select(Shop.id, Shop.uzum_id).where(Shop.uzum_id.is_not(None))
            ).all()
            if str(uid).strip().isdigit()
        }

    sku_to_shop_pk = {str(sku): int(pk) for sku, pk in pg_rows}
    for r in results:
        if "error" in r:
            continue
        expected_pk = shop_pk_by_uzum.get(int(r["shop"]))
        raw = _ur.download_csv(r["file_url"])
        rows = _ur.parse_sells_csv(raw)
        in_shop = out_shop = unknown = 0
        for row in rows:
            sku = (row.get("sku_id") or "").strip()
            pk = sku_to_shop_pk.get(sku)
            if pk is None:
                unknown += 1
            elif pk == expected_pk:
                in_shop += 1
            else:
                out_shop += 1
        verdict = "OK" if out_shop == 0 else "CROSS-SHOP ROWS"
        print(
            f"    shop={r['shop']:<6} in_shop={in_shop} "
            f"cross={out_shop} unknown={unknown} [{verdict}]"
        )


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--shops", default=",".join(DEFAULT_SHOPS))
    ap.add_argument(
        "--hours-back", type=int, default=24,
        help="Window size (hours). Short = small CSVs = fast probe. Default 24.",
    )
    ap.add_argument(
        "--offset-ms", type=int, default=1,
        help="ms offset per shop in Test B. Default 1.",
    )
    ap.add_argument(
        "--deep", action="store_true",
        help="Also print per-CSV in-shop/cross-shop sku mapping (slower).",
    )
    ap.add_argument(
        "--only", choices=["A", "B", "C", "D"],
        help="Run only one test instead of all three.",
    )
    args = ap.parse_args()

    shops = [int(s.strip()) for s in args.shops.split(",") if s.strip()]
    now = datetime.now(APP_TZ).replace(microsecond=0)
    window_from = now - timedelta(hours=args.hours_back)
    window_to = now

    print("=" * 64)
    print(f"Uzum race-cause probe — shops={shops}")
    print(f"window = last {args.hours_back}h: {window_from} → {window_to}")
    print(f"offset = {args.offset_ms} ms/shop in Test B")
    print("=" * 64)

    all_results: dict[str, list[dict]] = {}

    if args.only in (None, "A"):
        all_results["A"] = run_burst(
            "TEST A — CONTROL: identical [dateFrom, dateTo], shopIds=[s]",
            shops, window_from=window_from, window_to=window_to,
        )
        if args.deep:
            check_routing_sanity(all_results["A"])

    if args.only in (None, "B"):
        all_results["B"] = run_burst(
            f"TEST B — OFFSET: dateFrom += i*{args.offset_ms} ms, shopIds=[s]",
            shops,
            window_from=window_from, window_to=window_to,
            per_shop_ms_offset=args.offset_ms,
        )
        if args.deep:
            check_routing_sanity(all_results["B"])

    if args.only in (None, "C"):
        all_results["C"] = run_burst(
            "TEST C — PADDED: identical window, shopIds=[s, s]",
            shops, window_from=window_from, window_to=window_to,
            padded_list=True,
        )
        if args.deep:
            check_routing_sanity(all_results["C"])

    if args.only == "D":
        print("\n=== TEST D — FULLY SERIAL: one shop at a time ===")
        print(f"  window={window_from.isoformat()} → {window_to.isoformat()}")
        base_from_ms = _tashkent_to_ms(window_from)
        base_to_ms = _tashkent_to_ms(window_to)
        results: list[dict[str, Any]] = []
        t_total = time.monotonic()
        for sid in shops:
            r = run_one_call(sid, [sid], base_from_ms, base_to_ms)
            results.append(r)
            print(
                f"  shop={r['shop']:<6} sha16={r['sha16']}  rows={r['rows']:<5} "
                f"bytes={r['bytes']:<8} req={r['req_id'][:18]}…  "
                f"t_create={r['t_create']}s t_poll={r['t_poll']}s t_dl={r['t_dl']}s"
            )
        wall = round(time.monotonic() - t_total, 2)
        shas: dict[str, list[int]] = {}
        for r in results:
            shas.setdefault(r["sha16"], []).append(r["shop"])
        dup_groups = [v for v in shas.values() if len(v) > 1]
        if dup_groups:
            print(f"  >>> UNEXPECTED: serial burst still shares CSVs: {dup_groups}")
        else:
            print("  >>> CLEAN: all 4 CSVs distinct")
        print(f"  TOTAL wall-clock for serial 4-shop pass: {wall}s")
        all_results["D"] = results

    print("\n" + "=" * 64)
    print("SUMMARY")
    print("=" * 64)
    for label, results in all_results.items():
        shas = {}
        for r in results:
            if "error" in r:
                continue
            shas.setdefault(r["sha16"], []).append(r["shop"])
        dup = [v for v in shas.values() if len(v) > 1]
        verdict = f"RACE ({dup})" if dup else "CLEAN"
        print(f"  Test {label}: {verdict}")

    # Decision hint.
    if "A" in all_results and "B" in all_results:
        a_dup = any(
            len([r for r in all_results["A"] if r.get("sha16") == s]) > 1
            for s in {r.get("sha16") for r in all_results["A"] if "sha16" in r}
        )
        b_dup = any(
            len([r for r in all_results["B"] if r.get("sha16") == s]) > 1
            for s in {r.get("sha16") for r in all_results["B"] if "sha16" in r}
        )
        print()
        if a_dup and not b_dup:
            print("VERDICT: H1 confirmed — ms offset defuses the race.")
            print("         → Keep per-shop architecture. Add i*{ms} offset to dateFrom.")
        elif a_dup and b_dup:
            print("VERDICT: H3 — race fires even with ms offset.")
            print("         → Path C (bulk create + SKU routing) is the only fix.")
        elif not a_dup:
            print("VERDICT: Control did NOT reproduce race. Re-run at peak minute")
            print("         or widen shop count before drawing conclusions.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
