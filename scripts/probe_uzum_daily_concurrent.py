#!/usr/bin/env python3
"""Probe: do concurrent creates with UNIQUE (shop, dateFrom, dateTo) still race?

Strategy: for each shop x each day in the window, fire ONE create_report as a
worker task. Each task has a unique (shopIds, dateFrom, dateTo) signature.
Each worker waits for its own CSV (poll -> download). Many workers run in
parallel via ThreadPoolExecutor.

Detection: if the same non-empty CSV (sha256) ends up tied to more than one
distinct (shop, day), the race fired despite unique args. If every non-empty
CSV is unique per (shop, day), unique args defuse the race.

Empty CSVs all hash the same (0-row "no data" files); we report them separately
so they don't masquerade as collisions.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, "/app")

from core.auth_helpers import _get_admin_token  # noqa: E402
from core.uzum_reports import (  # noqa: E402
    create_report,
    download_csv,
    parse_sells_csv,
    wait_for_report,
)

TASHKENT = ZoneInfo("Asia/Tashkent")
DEFAULT_SHOPS = ["5983", "7138", "10945", "19621"]


def _tashkent_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TASHKENT)
    return int(dt.timestamp() * 1000)


def run_task(shop_id: str, day: date) -> dict:
    day_start = datetime.combine(day, datetime.min.time()).replace(tzinfo=TASHKENT)
    day_end = day_start + timedelta(days=1)
    df_ms = _tashkent_ms(day_start)
    dt_ms = _tashkent_ms(day_end)
    t_total = time.monotonic()
    try:
        t0 = time.monotonic()
        req_id = create_report(
            [shop_id], "SELLS_REPORT", df_ms, dt_ms, token_getter=_get_admin_token
        )
        t_create = time.monotonic() - t0

        t0 = time.monotonic()
        file_url = wait_for_report(
            req_id, token_getter=_get_admin_token, max_wait_s=300
        )
        t_poll = time.monotonic() - t0

        t0 = time.monotonic()
        raw = download_csv(file_url)
        t_dl = time.monotonic() - t0

        rows = parse_sells_csv(raw)
        sha = hashlib.sha256(raw).hexdigest()[:16]

        return {
            "shop": shop_id,
            "day": day.isoformat(),
            "req_id": str(req_id)[-6:],
            "sha16": sha,
            "bytes": len(raw),
            "rows": len(rows),
            "t_create": round(t_create, 2),
            "t_poll": round(t_poll, 2),
            "t_dl": round(t_dl, 2),
            "t_total": round(time.monotonic() - t_total, 2),
            "err": None,
        }
    except Exception as e:
        return {
            "shop": shop_id,
            "day": day.isoformat(),
            "err": f"{type(e).__name__}: {e}"[:200],
        }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shops", default=",".join(DEFAULT_SHOPS))
    ap.add_argument("--from", dest="from_date", default="2026-01-01")
    ap.add_argument("--to", dest="to_date", default=None,
                    help="Exclusive upper bound (default: today in Tashkent)")
    ap.add_argument("--workers", type=int, default=128)
    ap.add_argument("--progress-every", type=int, default=20)
    args = ap.parse_args()

    shops = [s.strip() for s in args.shops.split(",") if s.strip()]
    from_day = date.fromisoformat(args.from_date)
    to_day = (
        date.fromisoformat(args.to_date)
        if args.to_date
        else datetime.now(TASHKENT).date()
    )

    days: list[date] = []
    d = from_day
    while d < to_day:
        days.append(d)
        d += timedelta(days=1)

    tasks = [(s, d) for s in shops for d in days]
    total = len(tasks)

    print("=" * 70)
    print(f"Daily concurrent probe")
    print(f"  shops   = {shops}")
    print(f"  days    = [{from_day} .. {to_day}) = {len(days)} days")
    print(f"  tasks   = {total} (each has a unique (shop, day) signature)")
    print(f"  workers = {args.workers}")
    print("=" * 70)

    t_wall = time.monotonic()
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(run_task, s, d): (s, d) for s, d in tasks}
        done = 0
        for fut in as_completed(futs):
            r = fut.result()
            results.append(r)
            done += 1
            if done % max(1, args.progress_every) == 0 or done == total:
                print(f"  progress: {done}/{total}")

    wall = round(time.monotonic() - t_wall, 2)

    ok = [r for r in results if not r.get("err")]
    errors = [r for r in results if r.get("err")]

    by_sha: dict[str, list[dict]] = {}
    for r in ok:
        by_sha.setdefault(r["sha16"], []).append(r)

    collisions = {sha: lst for sha, lst in by_sha.items() if len(lst) > 1}
    nonempty_coll = {
        sha: lst for sha, lst in collisions.items() if any(r["rows"] > 0 for r in lst)
    }
    empty_coll = {
        sha: lst for sha, lst in collisions.items() if all(r["rows"] == 0 for r in lst)
    }

    cross_shop_coll = {
        sha: lst
        for sha, lst in nonempty_coll.items()
        if len({r["shop"] for r in lst}) > 1
    }
    same_shop_diff_day_coll = {
        sha: lst
        for sha, lst in nonempty_coll.items()
        if len({r["shop"] for r in lst}) == 1 and len({r["day"] for r in lst}) > 1
    }

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"  Total tasks:                 {total}")
    print(f"  OK:                          {len(ok)}")
    print(f"  Errors:                      {len(errors)}")
    print(f"  Unique SHAs:                 {len(by_sha)}")
    print(f"  Shared SHAs (any):           {len(collisions)}")
    print(f"    - empty-only (0 rows):     {len(empty_coll)}")
    print(f"    - non-empty (has data):    {len(nonempty_coll)}")
    print(f"        cross-shop:            {len(cross_shop_coll)}  <-- data corruption")
    print(f"        same-shop, diff day:   {len(same_shop_diff_day_coll)}  <-- date corruption")
    print(f"  Wall time:                   {wall}s")

    if cross_shop_coll:
        print("\n!!!  RACE FIRED — non-empty CSV shared across DIFFERENT shops  !!!")
        for sha, lst in sorted(cross_shop_coll.items(), key=lambda x: -len(x[1]))[:15]:
            shops_involved = sorted({r["shop"] for r in lst})
            days_involved = sorted({r["day"] for r in lst})
            print(
                f"  sha={sha}  rows={lst[0]['rows']}  bytes={lst[0]['bytes']}  "
                f"shops={shops_involved}  days={days_involved[:3]}..."
            )
            for r in lst[:6]:
                print(f"    shop={r['shop']:>6}  day={r['day']}  req=...{r['req_id']}")
    elif nonempty_coll:
        print("\n??? Non-empty CSV shared but ONLY within one shop across different days:")
        for sha, lst in list(same_shop_diff_day_coll.items())[:10]:
            print(f"  sha={sha}  rows={lst[0]['rows']}  shop={lst[0]['shop']}")
            for r in lst[:5]:
                print(f"    day={r['day']}  req=...{r['req_id']}")
    else:
        print("\n>>> CLEAN — every non-empty CSV was unique to one (shop, day) pair.")
        print("    Unique args per concurrent create DEFUSED the race.")

    if empty_coll:
        tot_empty = sum(len(lst) for lst in empty_coll.values())
        print(
            f"\nEmpty-CSV collisions: {len(empty_coll)} sha buckets, "
            f"{tot_empty} tasks (normal — days with zero sales all return the same empty file)"
        )

    if errors:
        print(f"\nErrors ({len(errors)}):")
        for r in errors[:15]:
            print(f"  shop={r['shop']}  day={r['day']}  err={r['err']}")


if __name__ == "__main__":
    main()
