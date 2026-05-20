"""Path C historical backfill — DRY-RUN with per-phase timing.

Validates the Path C approach for the Uzum /documents/create race bug BEFORE
rewriting `_hourly_sales_reports_loop`. One bulk create with `shopIds=[all]`,
then route each CSV row to its correct shop via `Variant.uzum_sku_id →
ProductGroup.shop_id`. See memory/project_uzum_race_bug_fix.md.

Phases timed independently (wall-clock, time.monotonic):
  1. create_report POST         → requestId
  2. wait_for_report poll       → COMPLETED + fileUrl
  3. download_csv GET           → raw bytes (also reports byte size)
  4. parse_sells_csv            → list of dicts
  5. Build sku_to_shop dict     → Variant ⋈ ProductGroup
  6. Filter + route loop        → drop Отменен, resolve shop_id per row
  7. DB bulk insert             → ON CONFLICT DO NOTHING (composite PK)

Pre-flight checks (always run, before Phase 1):
  a. SKU uniqueness — any `uzum_sku_id` mapped to >1 shop blocks routing
  b. Contaminated-rows count — how many existing sales_lines rows already
     disagree with the catalog (cleanup SQL candidates)

`--dry-run` skips Phase 7 (the INSERT) entirely — nothing writes to the DB.
In that mode the script is read-only on our side, though Phases 1-4 still
hit Uzum's API using the shared admin token, so run off-hour to avoid
colliding with the live hourly loop's concurrent creates.

Example:
    python scripts/backfill_path_c_dryrun.py --dry-run
    python scripts/backfill_path_c_dryrun.py --shops 5983,7138,10945,19621 \
        --from 2022-01-01 --to 2026-04-21 --dry-run
    python scripts/backfill_path_c_dryrun.py --chunk-days 30 --dry-run
        (monthly sequential fallback if Uzum rejects the 4-year single create)
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from datetime import datetime, date, timedelta, time as dt_time
from decimal import Decimal, InvalidOperation

# Project root importable.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sqlalchemy import select, text, func
from sqlalchemy.dialects.postgresql import insert as pg_insert

from config import APP_TZ
from extensions import SessionLocal
from core.auth_helpers import _get_admin_token
from core.http_client import http_json
from core import uzum_reports as _ur
from models import Shop, Variant, ProductGroup, SalesLine


# ── Constants mirrored from core.uzum_reports (kept local so this script
#    stays a single-file tool, and so we can hit `/documents/create` with a
#    list of shopIds without editing production code).
_CREATE_URL = "https://api-seller.uzum.uz/api/seller/documents/create"
_REPORT_HEADERS = {
    "Origin": "https://seller.uzum.uz",
    "Referer": "https://seller.uzum.uz/",
}


# ── Phase 1 helper: bulk create with list of shopIds ──────────────────

def create_report_bulk(
    shop_ids: list[int],
    job_type: str,
    date_from_ms: int,
    date_to_ms: int,
) -> str:
    """POST /documents/create with shopIds=<list>. Returns requestId.

    Intentionally inlined — core.uzum_reports.create_report will grow a
    list-accepting signature in the hourly rewrite; for now we keep the
    production helper untouched and build the body here.
    """
    body = {
        "idempotencyKey": str(uuid.uuid4()),
        "jobType": job_type,
        "contentType": "CSV",
        "params": {
            "returns": False,
            "group": False,
            "shopIds": [int(s) for s in shop_ids],
            "dateFrom": int(date_from_ms),
            "dateTo": int(date_to_ms),
        },
    }
    headers = dict(_REPORT_HEADERS)
    resp = http_json(
        _CREATE_URL,
        method="POST",
        body=body,
        headers=headers,
        _get_admin_token=_get_admin_token,
    )
    payload = resp.get("payload") if isinstance(resp, dict) else None
    request_id = None
    if isinstance(payload, dict):
        request_id = payload.get("requestId") or payload.get("id")
    if not request_id and isinstance(resp, dict):
        request_id = resp.get("requestId")
    if not request_id:
        raise RuntimeError(f"create_report_bulk: no requestId in response ({resp!r:.200})")
    return str(request_id)


# ── CSV row parsers (duplicated from app.py:4733-4772 — those are local
#    closures inside _ingest_sales_lines_window, not importable) ────────

def _parse_dt(val) -> datetime | None:
    if not val:
        return None
    s = str(val).strip()
    if not s:
        return None
    for fmt in (
        "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y",
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _parse_int(val) -> int:
    if val is None:
        return 0
    s = str(val).strip().replace(" ", "").replace("\u00a0", "")
    if not s:
        return 0
    s = s.replace(",", ".")
    try:
        return int(float(s))
    except (ValueError, InvalidOperation):
        return 0


def _parse_dec(val) -> Decimal:
    if val is None:
        return Decimal("0")
    s = str(val).strip().replace(" ", "").replace("\u00a0", "").replace(",", ".")
    if not s:
        return Decimal("0")
    try:
        return Decimal(s)
    except (ValueError, InvalidOperation):
        return Decimal("0")


# ── ms helpers (Tashkent wall-clock → UTC ms, matches app.py:4702) ────

def _tashkent_to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=APP_TZ)
    return int(dt.timestamp() * 1000)


# ── Pre-flight checks ────────────────────────────────────────────────

def preflight_sku_uniqueness() -> tuple[int, list[tuple[str, int]]]:
    """Count Variant.sku values that map to >1 shop via Variant ⋈ ProductGroup.

    Routing uses Variant.sku (seller code) because the CSV's SKU column carries
    the seller code, not the Uzum internal numeric id (uzum_sku_id). Verified
    2026-04-21: sku gives 100% coverage against CSV, uzum_sku_id gives 0%.

    Returns (n_conflicts, sample_rows). Zero conflicts = safe to route by catalog.
    """
    with SessionLocal() as db:
        rows = db.execute(
            select(
                Variant.sku,
                func.count(func.distinct(ProductGroup.shop_id)).label("n_shops"),
            )
            .join(ProductGroup, Variant.group_id == ProductGroup.id)
            .where(Variant.sku.is_not(None))
            .group_by(Variant.sku)
            .having(func.count(func.distinct(ProductGroup.shop_id)) > 1)
        ).all()
    sample = [(str(sku), int(n)) for sku, n in rows[:10]]
    return len(rows), sample


def preflight_contaminated_rows() -> int:
    """Count existing sales_lines rows whose shop_id disagrees with the catalog.

    Join is Variant.sku = sales_lines.sku_id (both hold the seller code).
    """
    with SessionLocal() as db:
        n = db.execute(
            text(
                """
                SELECT COUNT(*) FROM sales_lines sl
                JOIN variants v        ON v.sku = sl.sku_id
                JOIN product_groups pg ON pg.id = v.group_id
                WHERE pg.shop_id <> sl.shop_id
                """
            )
        ).scalar()
    return int(n or 0)


# ── Active shop resolution ───────────────────────────────────────────

def resolve_shops(cli_shops: str | None) -> list[tuple[int, int]]:
    """Return list of (shop_pk_id, uzum_id_int) for shops we're backfilling.

    If `cli_shops` is provided it's a comma-separated list of uzum_ids
    (strings); otherwise we take every Shop row with an owner_id set,
    matching _active_shop_ids_for_sales in app.py:3527.
    """
    with SessionLocal() as db:
        if cli_shops:
            wanted = [s.strip() for s in cli_shops.split(",") if s.strip()]
            rows = db.execute(
                select(Shop.id, Shop.uzum_id).where(Shop.uzum_id.in_(wanted))
            ).all()
        else:
            rows = db.execute(
                select(Shop.id, Shop.uzum_id)
                .where(Shop.uzum_id.is_not(None))
                .where(Shop.owner_id.is_not(None))
            ).all()
    out: list[tuple[int, int]] = []
    for pk, uid in rows:
        try:
            out.append((int(pk), int(str(uid).strip())))
        except (TypeError, ValueError):
            continue
    return out


# ── Row building (mirrors app.py:4804-4832) ──────────────────────────

def build_payload(
    kept_rows: list[dict],
    sku_to_shop: dict[str, int],
    product_by_sku: dict[str, int],
    now_utc: datetime,
) -> tuple[list[dict], dict[str, int]]:
    """Convert parsed CSV rows to SalesLine insert dicts, routing by SKU.

    Returns (payload, counters). Counters:
      unknown_sku: row dropped — sku_id not in catalog
      malformed:   missing order_id / sku_id / created_at
    """
    counters = {"unknown_sku": 0, "malformed": 0}
    payload: list[dict] = []
    for r in kept_rows:
        order_id = (r.get("order_id") or "").strip()
        sku_key = (r.get("sku_id") or "").strip()
        created = _parse_dt(r.get("created_at"))
        if not order_id or not sku_key or created is None:
            counters["malformed"] += 1
            continue
        shop_pk = sku_to_shop.get(sku_key)
        if shop_pk is None:
            counters["unknown_sku"] += 1
            continue
        payload.append({
            "shop_id": shop_pk,
            "order_id": order_id,
            "sku_id": sku_key,
            "sku_title": (r.get("sku_title") or "")[:500] or None,
            "barcode": (r.get("barcode") or "")[:120] or None,
            "category": (r.get("category") or "")[:300] or None,
            "product_id": product_by_sku.get(sku_key),
            "status": (r.get("status") or "")[:40] or None,
            "created_at": created,
            "received_at": _parse_dt(r.get("received_at")),
            "qty": _parse_int(r.get("qty")),
            "qty_returns": _parse_int(r.get("qty_returns")),
            "revenue": _parse_dec(r.get("revenue")),
            "seller_profit": _parse_dec(r.get("seller_profit")),
            "commission": _parse_dec(r.get("commission")),
            "unit_price": _parse_dec(r.get("unit_price")),
            "promo_amount": _parse_dec(r.get("promo_amount")),
            "purchase_price": _parse_dec(r.get("purchase_price")),
            "logistics_fee": _parse_dec(r.get("logistics_fee")),
            "synced_at": now_utc,
        })
    return payload, counters


# ── Chunked window generator (fallback for Uzum window caps) ─────────

def iter_windows(
    date_from: datetime, date_to: datetime, chunk_days: int | None
) -> list[tuple[datetime, datetime]]:
    if not chunk_days or chunk_days <= 0:
        return [(date_from, date_to)]
    out: list[tuple[datetime, datetime]] = []
    cur = date_from
    step = timedelta(days=chunk_days)
    while cur < date_to:
        nxt = min(cur + step, date_to)
        out.append((cur, nxt))
        cur = nxt
    return out


# ── One-window pipeline (phases 1-7) ─────────────────────────────────

def run_window(
    window_from: datetime,
    window_to: datetime,
    uzum_shop_ids: list[int],
    shop_pk_by_uzum_id: dict[int, int],
    sku_to_shop_pk: dict[str, int],
    product_by_sku: dict[str, int],
    *,
    do_insert: bool,
    poll_max_s: int,
) -> dict:
    """Run all 7 phases for a single [from, to) window. Returns per-phase timings."""
    timings: dict[str, float] = {}
    sizes: dict[str, int | str] = {}

    date_from_ms = _tashkent_to_ms(window_from)
    date_to_ms = _tashkent_to_ms(window_to) - 1  # mirror app.py:4711

    # Phase 1 — POST create.
    t0 = time.monotonic()
    request_id = create_report_bulk(uzum_shop_ids, "SELLS_REPORT", date_from_ms, date_to_ms)
    timings["1_create"] = time.monotonic() - t0

    # Phase 2 — poll until COMPLETED or deadline.
    t0 = time.monotonic()
    file_url = _ur.wait_for_report(
        request_id, max_wait_s=poll_max_s, token_getter=_get_admin_token
    )
    timings["2_poll"] = time.monotonic() - t0

    # Phase 3 — download CSV bytes.
    t0 = time.monotonic()
    raw = _ur.download_csv(file_url, token_getter=_get_admin_token)
    timings["3_download"] = time.monotonic() - t0
    sizes["csv_bytes"] = len(raw)

    if len(raw) > 50 * 1024 * 1024:
        print(f"  [!] CSV is {len(raw)/1_048_576:.1f} MB — parse will hold full string in memory")

    # Phase 4 — parse CSV into canonical-keyed dicts.
    t0 = time.monotonic()
    rows = _ur.parse_sells_csv(raw)
    timings["4_parse"] = time.monotonic() - t0
    sizes["parsed_rows"] = len(rows)

    # Phase 5 — sku_to_shop dict is prebuilt once by caller (keeps phase 5
    # out of per-window repetition when chunking); record zero here.
    timings["5_lookup_build"] = 0.0

    # Phase 6 — filter Отменен + route by SKU + shape payload.
    t0 = time.monotonic()
    kept: list[dict] = []
    dropped_cancelled = 0
    for r in rows:
        if (r.get("status") or "").strip() == "Отменен":
            dropped_cancelled += 1
            continue
        kept.append(r)
    payload, counters = build_payload(kept, sku_to_shop_pk, product_by_sku, datetime.utcnow())
    timings["6_filter_route"] = time.monotonic() - t0
    sizes["dropped_cancelled"] = dropped_cancelled
    sizes["unknown_sku"] = counters["unknown_sku"]
    sizes["malformed"] = counters["malformed"]
    sizes["payload_rows"] = len(payload)

    # Phase 7 — DB bulk insert (ON CONFLICT DO NOTHING) unless --dry-run.
    # Batched: one pg_insert.values([...]) with 161k dicts explodes SQLAlchemy
    # compile time + memory. Postgres also caps a single query at 65535 params,
    # and SalesLine has 20 columns → max ~3276 rows/batch. 2500 leaves slack.
    if do_insert and payload:
        batch = 2500
        t0 = time.monotonic()
        with SessionLocal() as db:
            with db.begin():
                for i in range(0, len(payload), batch):
                    chunk = payload[i:i + batch]
                    stmt = pg_insert(SalesLine).values(chunk)
                    stmt = stmt.on_conflict_do_nothing(
                        index_elements=["shop_id", "order_id", "sku_id"]
                    )
                    db.execute(stmt)
        timings["7_insert"] = time.monotonic() - t0
        sizes["inserted_or_skipped"] = len(payload)
    else:
        timings["7_insert"] = 0.0
        sizes["inserted_or_skipped"] = 0 if not payload else -1  # -1 = skipped by --dry-run

    return {"timings": timings, "sizes": sizes}


# ── Reporting ────────────────────────────────────────────────────────

def print_timing_table(label: str, result: dict) -> None:
    print(f"\n── Timings: {label} ──")
    print(f"  {'Phase':<20} {'Seconds':>10}")
    print(f"  {'-'*20} {'-'*10}")
    for k, v in result["timings"].items():
        print(f"  {k:<20} {v:>10.3f}")
    total = sum(result["timings"].values())
    print(f"  {'TOTAL':<20} {total:>10.3f}")
    print(f"\n── Sizes/Counters ──")
    for k, v in result["sizes"].items():
        print(f"  {k:<20} {v}")


# ── Main ─────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(description="Path C bulk backfill dry-run")
    p.add_argument(
        "--shops",
        default=None,
        help="Comma-separated uzum_ids (default: every Shop with owner_id set)",
    )
    p.add_argument("--from", dest="date_from", default="2022-01-01",
                   help="Tashkent-local date, inclusive (YYYY-MM-DD)")
    p.add_argument("--to", dest="date_to", default=None,
                   help="Tashkent-local date, exclusive (YYYY-MM-DD). Default: today Tashkent")
    p.add_argument("--dry-run", action="store_true",
                   help="Skip Phase 7 (DB insert). Uzum API is still called.")
    p.add_argument("--chunk-days", type=int, default=0,
                   help="Split the window into N-day sequential chunks (0 = one call)")
    p.add_argument("--poll-max-s", type=int, default=1800,
                   help="wait_for_report deadline in seconds (default 30 min for big windows)")
    args = p.parse_args()

    # ── Parse CLI dates → naive Tashkent datetimes ─────────────────
    try:
        df = datetime.strptime(args.date_from, "%Y-%m-%d")
    except ValueError as e:
        print(f"[ERR] --from: {e}")
        return 2
    if args.date_to:
        try:
            dt = datetime.strptime(args.date_to, "%Y-%m-%d")
        except ValueError as e:
            print(f"[ERR] --to: {e}")
            return 2
    else:
        today = datetime.now(APP_TZ).date()
        dt = datetime.combine(today, dt_time(0, 0, 0))
    if df >= dt:
        print(f"[ERR] --from must be < --to")
        return 2

    # ── Resolve shops ──────────────────────────────────────────────
    shops = resolve_shops(args.shops)
    if not shops:
        print("[ERR] no shops resolved")
        return 2
    shop_pk_by_uzum_id = {uid: pk for pk, uid in shops}
    uzum_shop_ids = [uid for _, uid in shops]

    print(f"Shops ({len(shops)}): uzum_ids={uzum_shop_ids}")
    print(f"Window: [{df.isoformat()}, {dt.isoformat()}) Tashkent")
    print(f"Mode: {'DRY-RUN (no DB insert)' if args.dry_run else 'LIVE (will INSERT)'}")
    print(f"Chunks: {args.chunk_days or 'single call'}")

    # ── Admin token sanity ─────────────────────────────────────────
    tok = _get_admin_token()
    if not tok:
        print("[ERR] no admin token in DB; cannot call Uzum")
        return 2
    print(f"Admin token: {tok[:20]}... (len={len(tok)})")

    # ── Pre-flight A: SKU uniqueness ───────────────────────────────
    print("\n[preflight] SKU uniqueness check...")
    t0 = time.monotonic()
    n_conflicts, sample = preflight_sku_uniqueness()
    print(f"  took {time.monotonic()-t0:.2f}s, conflicts={n_conflicts}")
    if n_conflicts:
        print(f"  sample (sku_id, n_shops): {sample}")
        print("  [!] Non-zero conflicts — routing by catalog is unsafe until these are resolved.")
        print("      Dry-run will still run so you can see timings, but payload will be skewed.")

    # ── Pre-flight B: contaminated-row count ───────────────────────
    print("\n[preflight] Contaminated-row count (existing sales_lines mismatches)...")
    t0 = time.monotonic()
    n_bad = preflight_contaminated_rows()
    print(f"  took {time.monotonic()-t0:.2f}s, bad_rows={n_bad}")
    if n_bad and not args.dry_run:
        print("  [!] LIVE insert requested but contaminated rows still in DB.")
        print("      Run the cleanup DELETE first (see memory/project_uzum_race_bug_fix.md).")
        print("      Aborting to avoid compounding the problem.")
        return 2

    # ── Phase 5 (done once, amortized across chunks) ───────────────
    print("\n[phase 5] Build sku_to_shop dict...")
    t0 = time.monotonic()
    with SessionLocal() as db:
        rows_v = db.execute(
            select(
                Variant.sku,
                ProductGroup.shop_id,
                ProductGroup.uzum_product_id,
            )
            .join(ProductGroup, Variant.group_id == ProductGroup.id)
            .where(Variant.sku.is_not(None))
        ).all()
    sku_to_shop_pk: dict[str, int] = {}
    product_by_sku: dict[str, int] = {}
    for sku_raw, shop_pk, pid_raw in rows_v:
        sku_key = str(sku_raw or "").strip()
        if not sku_key or shop_pk is None:
            continue
        sku_to_shop_pk[sku_key] = int(shop_pk)
        try:
            product_by_sku[sku_key] = int(str(pid_raw).strip()) if pid_raw is not None else None
        except (TypeError, ValueError):
            pass
    phase5_elapsed = time.monotonic() - t0
    print(f"  took {phase5_elapsed:.2f}s, sku_to_shop entries={len(sku_to_shop_pk)}")

    # ── Run chunks (or single window) ──────────────────────────────
    windows = iter_windows(df, dt, args.chunk_days)
    agg_timings: dict[str, float] = {k: 0.0 for k in (
        "1_create", "2_poll", "3_download", "4_parse",
        "5_lookup_build", "6_filter_route", "7_insert",
    )}
    agg_timings["5_lookup_build"] = phase5_elapsed

    agg_sizes = {
        "csv_bytes": 0, "parsed_rows": 0, "dropped_cancelled": 0,
        "unknown_sku": 0, "malformed": 0, "payload_rows": 0,
        "inserted_or_skipped": 0,
    }

    for i, (wf, wt) in enumerate(windows, start=1):
        print(f"\n[window {i}/{len(windows)}] [{wf.isoformat()}, {wt.isoformat()})")
        try:
            result = run_window(
                wf, wt, uzum_shop_ids, shop_pk_by_uzum_id,
                sku_to_shop_pk, product_by_sku,
                do_insert=not args.dry_run,
                poll_max_s=args.poll_max_s,
            )
        except Exception as e:
            print(f"  [ERR] window failed: {e}")
            print("  If this is a window-cap error from Uzum, rerun with --chunk-days 30")
            return 1
        print_timing_table(f"window {i}", result)
        for k, v in result["timings"].items():
            if k != "5_lookup_build":
                agg_timings[k] += v
        for k in agg_sizes:
            v = result["sizes"].get(k)
            if isinstance(v, int) and v >= 0:
                agg_sizes[k] += v

    # ── Final aggregate report ─────────────────────────────────────
    print("\n" + "=" * 50)
    print("  AGGREGATE (all windows)")
    print("=" * 50)
    print(f"\n  {'Phase':<20} {'Seconds':>10}")
    print(f"  {'-'*20} {'-'*10}")
    for k, v in agg_timings.items():
        print(f"  {k:<20} {v:>10.3f}")
    total = sum(agg_timings.values())
    print(f"  {'TOTAL':<20} {total:>10.3f}")
    print(f"\n  Sizes/Counters:")
    for k, v in agg_sizes.items():
        print(f"    {k:<22} {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
