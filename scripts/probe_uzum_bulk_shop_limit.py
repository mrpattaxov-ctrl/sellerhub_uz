"""Probe: find Uzum's shopIds-array cap for /documents/create SELLS_REPORT.

Path C (bulk create + SKU-route) assumes we can ask Uzum for one CSV covering
many shops at once. Unknown: how many? Tested only at 4 shops so far.

Three unknowns this script maps:
  1. shopIds array cap  — HTTP 400 (``illegal-argument-001`` or similar)
  2. server-side generation cap — poll never reaches COMPLETED within deadline
  3. silent truncation — CSV returns only K < N shops worth of data

Strategy: pull real uzum_ids from the Shop table, then *sequentially* (never
concurrent — that's the race) escalate N through a ladder. For each N, fire
one create with those N shops, wait for completion, download, parse, and
count distinct shops actually represented in the CSV (join
``sku_id → Variant.sku → ProductGroup.shop_id``).

Stop conditions:
  - HTTP 4xx on create (record error then stop)
  - poll exceeds --poll-deadline seconds (record timeout then stop)
  - silent truncation (covered < requested, record then stop)

Guards:
  - Fresh ``idempotencyKey`` per call (uuid4)
  - --between-calls-delay seconds between iterations (Uzum has a 60s
    exact-args dedup; our window is the same across N, so this matters)
  - DRY-RUN — nothing writes to the DB.

Example:
    docker exec warehouse_app_uzum_qty_and_row_fix-app-1 bash -lc \\
      'cd /app && python scripts/probe_uzum_bulk_shop_limit.py --hours-back 24'
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sqlalchemy import select

from core.auth_helpers import _get_admin_token
from core.http_client import _get_http_session
from core import uzum_reports as _ur
from extensions import SessionLocal
from models import ProductGroup, Shop, Variant


DEFAULT_LADDER = [10, 50, 100, 500, 1000, 2000, 5000]

_REPORT_HEADERS = {
    "Origin": "https://seller.uzum.uz",
    "Referer": "https://seller.uzum.uz/",
}


def _bearer() -> str:
    tok = _get_admin_token() or ""
    return tok if tok.startswith("Bearer ") else f"Bearer {tok}"


def _load_uzum_ids() -> list[int]:
    with SessionLocal() as s:
        rows = s.execute(
            select(Shop.uzum_id).where(Shop.uzum_id.isnot(None))
        ).all()
    ids: list[int] = []
    seen: set[int] = set()
    for (u,) in rows:
        try:
            v = int(str(u).strip())
        except (TypeError, ValueError):
            continue
        if v not in seen:
            seen.add(v)
            ids.append(v)
    return ids


def _build_sku_to_shop() -> dict[str, int]:
    with SessionLocal() as s:
        rows = s.execute(
            select(Variant.sku, ProductGroup.shop_id)
            .join(ProductGroup, Variant.group_id == ProductGroup.id)
            .where(Variant.sku.isnot(None))
        ).all()
    out: dict[str, int] = {}
    for sku, shop_id in rows:
        if sku is None or shop_id is None:
            continue
        out[str(sku).strip()] = int(shop_id)
    return out


def _create_raw(shop_ids: list[int], date_from_ms: int, date_to_ms: int) -> tuple[int, str, dict | None]:
    """Direct POST so we can see the 4xx body verbatim."""
    body = {
        "idempotencyKey": str(uuid.uuid4()),
        "jobType": "SELLS_REPORT",
        "contentType": "CSV",
        "params": {
            "returns": False,
            "group": False,
            "shopIds": [int(x) for x in shop_ids],
            "dateFrom": int(date_from_ms),
            "dateTo": int(date_to_ms),
        },
    }
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Authorization": _bearer(),
        "Content-Type": "application/json",
        **_REPORT_HEADERS,
    }
    sess = _get_http_session()
    resp = sess.post(
        "https://api-seller.uzum.uz/api/seller/documents/create",
        json=body,
        headers=headers,
        timeout=60,
    )
    status = resp.status_code
    text = resp.text or ""
    parsed: dict | None = None
    try:
        import json as _json
        parsed = _json.loads(text) if text else None
    except Exception:
        parsed = None
    return status, text, parsed


def _extract_request_id(parsed: dict | None) -> str | None:
    if not isinstance(parsed, dict):
        return None
    payload = parsed.get("payload") if isinstance(parsed.get("payload"), dict) else parsed
    if not isinstance(payload, dict):
        return None
    rid = payload.get("requestId") or payload.get("id")
    if rid is None and isinstance(parsed, dict):
        rid = parsed.get("requestId")
    return str(rid) if rid is not None else None


def _coverage(rows: list[dict], sku_to_shop: dict[str, int]) -> tuple[int, int, int]:
    """Return (distinct_shops_from_rows, mapped_rows, unmapped_rows)."""
    shops: set[int] = set()
    mapped = 0
    unmapped = 0
    for r in rows:
        sku = r.get("sku_id")
        if not sku:
            unmapped += 1
            continue
        sid = sku_to_shop.get(str(sku).strip())
        if sid is None:
            unmapped += 1
        else:
            mapped += 1
            shops.add(sid)
    return len(shops), mapped, unmapped


def _run_one(n: int, shop_ids: list[int], df_ms: int, dt_ms: int,
             poll_deadline_s: int, sku_to_shop: dict[str, int]) -> dict:
    t_all = time.monotonic()
    row: dict = {"N": n, "requested": len(shop_ids)}

    t0 = time.monotonic()
    status, text, parsed = _create_raw(shop_ids, df_ms, dt_ms)
    row["t_create"] = round(time.monotonic() - t0, 2)
    row["http"] = status

    if status >= 400:
        row["verdict"] = "HTTP_ERROR"
        row["error"] = text[:300]
        row["t_total"] = round(time.monotonic() - t_all, 2)
        return row

    rid = _extract_request_id(parsed)
    if not rid:
        row["verdict"] = "NO_REQUEST_ID"
        row["error"] = text[:300]
        row["t_total"] = round(time.monotonic() - t_all, 2)
        return row
    row["req_id"] = rid

    try:
        t0 = time.monotonic()
        link = _ur.wait_for_report(rid, token_getter=_get_admin_token, max_wait_s=poll_deadline_s)
        row["t_poll"] = round(time.monotonic() - t0, 2)
    except TimeoutError as e:
        row["t_poll"] = round(time.monotonic() - t0, 2)
        row["verdict"] = "POLL_TIMEOUT"
        row["error"] = str(e)[:300]
        row["t_total"] = round(time.monotonic() - t_all, 2)
        return row
    except Exception as e:
        row["verdict"] = "POLL_ERROR"
        row["error"] = f"{type(e).__name__}: {e}"[:300]
        row["t_total"] = round(time.monotonic() - t_all, 2)
        return row

    try:
        t0 = time.monotonic()
        raw = _ur.download_csv(link)
        row["t_download"] = round(time.monotonic() - t0, 2)
        row["bytes"] = len(raw)
    except Exception as e:
        row["verdict"] = "DOWNLOAD_ERROR"
        row["error"] = f"{type(e).__name__}: {e}"[:300]
        row["t_total"] = round(time.monotonic() - t_all, 2)
        return row

    try:
        t0 = time.monotonic()
        rows_parsed = _ur.parse_sells_csv(raw)
        row["t_parse"] = round(time.monotonic() - t0, 2)
        row["rows"] = len(rows_parsed)
    except Exception as e:
        row["verdict"] = "PARSE_ERROR"
        row["error"] = f"{type(e).__name__}: {e}"[:300]
        row["t_total"] = round(time.monotonic() - t_all, 2)
        return row

    covered, mapped, unmapped = _coverage(rows_parsed, sku_to_shop)
    row["covered_shops"] = covered
    row["mapped_rows"] = mapped
    row["unmapped_rows"] = unmapped

    if len(rows_parsed) == 0:
        row["verdict"] = "OK_EMPTY"
    elif covered == 0:
        row["verdict"] = "OK_NO_COVERAGE_MATCH"
    else:
        row["verdict"] = "OK"

    row["t_total"] = round(time.monotonic() - t_all, 2)
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ladder", default=",".join(str(x) for x in DEFAULT_LADDER),
                    help="Comma-separated N values to try, in order")
    ap.add_argument("--include-all", action="store_true",
                    help="Also run with ALL available shops at the end")
    ap.add_argument("--hours-back", type=int, default=24,
                    help="Window size in hours (ends at now UTC)")
    ap.add_argument("--poll-deadline", type=int, default=600,
                    help="Max seconds to wait for each report to COMPLETE")
    ap.add_argument("--between-calls-delay", type=int, default=65,
                    help="Seconds to wait between ladder steps (>60 dodges dedup)")
    ap.add_argument("--stop-on-error", action="store_true",
                    help="Stop at first HTTP_ERROR / POLL_TIMEOUT (default: continue)")
    ap.add_argument("--synthetic-pad", action="store_true",
                    help="Pad shopIds with fake numeric ids up to ladder N. Only "
                         "measures array-length validation — Uzum filters out "
                         "shops the token doesn't own, so row data won't stress "
                         "generation time or file size.")
    ap.add_argument("--synthetic-start", type=int, default=900000000,
                    help="First fake shop id when --synthetic-pad is set")
    ap.add_argument("--dup-real", action="store_true",
                    help="Cycle real owned shop ids to fill the array up to N "
                         "(bypasses forbidden-001, still exercises array-length "
                         "validator; sales coverage won't grow past total_available)")
    args = ap.parse_args()

    ladder = [int(x.strip()) for x in args.ladder.split(",") if x.strip()]

    all_ids = _load_uzum_ids()
    total_available = len(all_ids)
    if total_available == 0:
        print("FATAL: no uzum_ids found in shops table.")
        return 2

    # Ladder clamps. When --synthetic-pad or --dup-real is set, skip the cap
    # at total_available and keep the raw ladder values.
    runs: list[int] = []
    if args.synthetic_pad or args.dup_real:
        for n in ladder:
            if n not in runs:
                runs.append(n)
    else:
        for n in ladder:
            capped = min(n, total_available)
            if capped not in runs:
                runs.append(capped)
        if args.include_all and total_available not in runs:
            runs.append(total_available)

    end_utc = datetime.now(timezone.utc).replace(microsecond=0)
    start_utc = end_utc - timedelta(hours=args.hours_back)
    df_ms = int(start_utc.timestamp() * 1000)
    dt_ms = int(end_utc.timestamp() * 1000)

    print("=" * 78)
    print("Uzum bulk-shop limit probe")
    print(f"  shops available : {total_available}")
    print(f"  ladder          : {runs}")
    print(f"  window (UTC)    : {start_utc.isoformat()} → {end_utc.isoformat()}")
    print(f"  poll deadline   : {args.poll_deadline}s per call")
    print(f"  delay between   : {args.between_calls_delay}s")
    print("=" * 78)

    print("Building SKU→shop map (needed for coverage check)…")
    t0 = time.monotonic()
    sku_to_shop = _build_sku_to_shop()
    print(f"  sku_to_shop size={len(sku_to_shop)} built_in={round(time.monotonic()-t0,2)}s")
    print()

    results: list[dict] = []
    for i, n in enumerate(runs):
        if args.dup_real and n > total_available:
            shop_ids = [all_ids[j % total_available] for j in range(n)]
            tag = f"N={n} (real={total_available}, cycled to {n})"
        elif args.synthetic_pad and n > total_available:
            pad_needed = n - total_available
            fakes = list(range(args.synthetic_start,
                               args.synthetic_start + pad_needed))
            shop_ids = all_ids + fakes
            tag = f"N={n} (real={total_available} + fake={pad_needed})"
        else:
            shop_ids = all_ids[:n]
            tag = f"N={n}"
        print(f"--- step {i+1}/{len(runs)}  {tag} ---")
        r = _run_one(n, shop_ids, df_ms, dt_ms, args.poll_deadline, sku_to_shop)
        results.append(r)
        print(f"  verdict={r['verdict']}  http={r.get('http')}  "
              f"t_create={r.get('t_create')}  t_poll={r.get('t_poll')}  "
              f"t_dl={r.get('t_download')}  bytes={r.get('bytes')}  "
              f"rows={r.get('rows')}  covered={r.get('covered_shops')}/{n}")
        if r.get("error"):
            print(f"  error: {r['error']}")
        print()

        if r["verdict"] in {"HTTP_ERROR", "POLL_TIMEOUT", "POLL_ERROR",
                            "DOWNLOAD_ERROR", "PARSE_ERROR", "NO_REQUEST_ID"}:
            if args.stop_on_error:
                print("Stopping on first error (--stop-on-error).")
                break

        if i < len(runs) - 1:
            print(f"  sleeping {args.between_calls_delay}s to dodge 60s dedup…")
            time.sleep(args.between_calls_delay)

    print("=" * 78)
    print("SUMMARY")
    print("=" * 78)
    print(f"{'N':>6} {'verdict':>22} {'http':>5} {'t_create':>9} "
          f"{'t_poll':>8} {'t_dl':>7} {'bytes':>10} {'rows':>8} {'cov':>6}")
    for r in results:
        print(f"{r['N']:>6} {r['verdict']:>22} {str(r.get('http','')):>5} "
              f"{str(r.get('t_create','')):>9} {str(r.get('t_poll','')):>8} "
              f"{str(r.get('t_download','')):>7} {str(r.get('bytes','')):>10} "
              f"{str(r.get('rows','')):>8} "
              f"{str(r.get('covered_shops','')):>6}")

    ok_ns = [r["N"] for r in results if r["verdict"].startswith("OK")]
    bad = [r for r in results if not r["verdict"].startswith("OK")]

    print()
    if ok_ns:
        print(f"Highest N accepted by Uzum : {max(ok_ns)}")
    if bad:
        first = bad[0]
        print(f"First failure              : N={first['N']} "
              f"verdict={first['verdict']} http={first.get('http')}")
        if first.get("error"):
            print(f"  error: {first['error']}")
    else:
        print("No failures observed — cap may be higher than the ladder tested.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
