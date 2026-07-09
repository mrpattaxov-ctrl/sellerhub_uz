"""Time-slot 15-PARALLEL sinovi — har sekund 15 parallel so'rov, 60s, narvonli.

Abdulaziz 2026-07-09: har sekundda 15 ta PARALLEL so'rov (har biri boshqa qty:
50,100,150,…,750), 60 sekund → 900 so'rov. Real ishlayotgan DETEKTORNI TO'XTATMAYDI
— bu alohida jarayon (docker compose exec), ilova worker'i o'z threadlarini ushlab
turadi ('Another worker owns background threads, skipping' — detektor davom etadi).

Rasm-uslub readout: Best/Typical/p95/Worst · success% + histogram + qty jadval.
READ-ONLY — faqat /time-slot o'qish, hech narsa band qilinmaydi/o'zgartirilmaydi.

    docker compose cp scripts/probe_timeslot_15parallel.py app:/app/scripts/probe_timeslot_15parallel.py
    docker compose exec app python scripts/probe_timeslot_15parallel.py --seconds 60
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from requests.adapters import HTTPAdapter

_BASE = "https://api-seller.uzum.uz/api/seller/shop"
_SLOT_WINDOW_MS = 31_532_400_000
_TIMEFROM_LEAD_MS = 5 * 60 * 1000


def _load_context():
    from app import app as flask_app
    from models import Shop
    from extensions import SessionLocal
    from sqlalchemy import select
    from core.auth_helpers import _get_admin_token
    from postavki.slot_watch import _get_sku_context
    with flask_app.app_context():
        tok = (_get_admin_token() or "").strip()
        with SessionLocal() as db:
            shop_id = db.execute(
                select(Shop.uzum_id).where(Shop.uzum_id.isnot(None)).limit(1)
            ).scalar_one_or_none()
        shop_id = str(shop_id) if shop_id else ""
        base, _dim, pool = _get_sku_context(shop_id) if shop_id else (None, None, None)
    return tok, shop_id, dict(base or {}), (pool or "FULLFILMENT")


def _make_session(pool_size: int) -> requests.Session:
    s = requests.Session()
    ad = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size, max_retries=0)
    s.mount("https://", ad); s.mount("http://", ad)
    return s


def _classify(exc: Exception) -> str:
    s = repr(exc)
    if "ConnectTimeout" in s: return "connect-timeout"
    if "ReadTimeout" in s or "Read timed out" in s: return "read-timeout"
    if "reset by peer" in s or "RemoteDisconnected" in s: return "conn-reset"
    return "other-error"


def _one(sess, url, headers, base, pool, qty, connect_to, read_to):
    now = int(time.time() * 1000)
    body = {"skuList": [{**base, "quantityToStock": qty}], "poolSource": pool,
            "timeFrom": now + _TIMEFROM_LEAD_MS, "timeTo": now + _SLOT_WINDOW_MS}
    t0 = time.monotonic()
    try:
        r = sess.post(url, headers=headers, json=body, timeout=(connect_to, read_to))
        n = -1
        if r.status_code == 200:
            try: n = len((r.json().get("payload") or {}).get("timeSlots") or [])
            except Exception: n = -1
        return (f"http-{r.status_code}", time.monotonic() - t0, qty, n)
    except Exception as e:
        return (_classify(e), time.monotonic() - t0, qty, -1)


def _pctl(vals, p):
    if not vals: return 0.0
    xs = sorted(vals); return xs[min(len(xs) - 1, int(len(xs) * p))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--connect-timeout", type=float, default=5.0)
    ap.add_argument("--read-timeout", type=float, default=15.0)
    args = ap.parse_args()

    tok, shop_id, base, pool = _load_context()
    if not (tok and shop_id and base):
        print("[probe] kontekst topilmadi"); return

    ladder = [50, 100, 150, 200, 250, 300, 350, 400, 450, 500, 550, 600, 650, 700, 750]
    url = f"{_BASE}/{shop_id}/v2/invoice/time-slot"
    h = {"Authorization": tok if tok.startswith("Bearer ") else f"Bearer {tok}",
         "Accept": "application/json", "Content-Type": "application/json",
         "Origin": "https://seller.uzum.uz", "Referer": "https://seller.uzum.uz/"}
    sess = _make_session(pool_size=60)

    print(f"[probe] 15-PARALLEL: har sekund {len(ladder)} parallel so'rov × {args.seconds}s "
          f"= ~{len(ladder)*args.seconds} so'rov")
    print(f"[probe] qty (har biri parallel): {ladder}")
    print(f"[probe] shop={shop_id} sku={base.get('skuId')} pool={pool}")
    print(f"[probe] DETEKTOR TO'XTATILMAYDI (alohida jarayon). Har sekund '.'\n")

    futures = []
    t_start = time.monotonic()
    with ThreadPoolExecutor(max_workers=len(ladder) * 2) as ex:
        for sec in range(args.seconds):
            target = t_start + sec
            dt = target - time.monotonic()
            if dt > 0: time.sleep(dt)
            for qty in ladder:
                futures.append(ex.submit(_one, sess, url, h, base, pool, qty,
                                         args.connect_timeout, args.read_timeout))
            sys.stdout.write("."); sys.stdout.flush()
        print(f"\n[probe] {len(futures)} so'rov yuborildi, javoblar kutilmoqda...")
        results = [f.result() for f in futures]
    total_span = time.monotonic() - t_start

    by = {}; per_qty = {}
    for outcome, el, qty, n in results:
        by.setdefault(outcome, []).append(el)
        pq = per_qty.setdefault(qty, {"ok": 0, "fail": 0, "slots": []})
        if outcome == "http-200":
            pq["ok"] += 1
            if n >= 0: pq["slots"].append(n)
        else: pq["fail"] += 1

    ok = len(by.get("http-200", [])); n = len(results)
    n_429 = len(by.get("http-429", [])); n_403 = len(by.get("http-403", []))
    n_to = sum(len(v) for k, v in by.items() if "timeout" in k)
    n_other = n - ok - n_429 - n_403 - n_to

    print(f"\n{'='*64}")
    print(f"  YAKUN — {n} so'rov / {total_span:.1f}s  ({n/total_span:.0f} req/s)")
    print(f"{'='*64}")
    print(f"  200 OK: {ok} ({100*ok/n:.1f}%)   429: {n_429}   403: {n_403}   "
          f"timeout: {n_to}   boshqa: {n_other}")

    if ok:
        okv = by["http-200"]
        best, p50, p95, worst = min(okv), _pctl(okv, .5), _pctl(okv, .95), max(okv)
        print(f"\n{'─'*64}")
        print(f"  ⚡ Best {best*1000:.0f}ms   Typical {p50*1000:.0f}ms   "
              f"p95 {p95*1000:.0f}ms   Worst {worst*1000:.0f}ms   ·   {100*ok/n:.1f}% success")
        print(f"{'─'*64}")
        edges = [0, .2, .3, .5, 1, 2, 5, 1e9]
        labels = ["<200ms", "200-300ms", "300-500ms", "500ms-1s", "1-2s", "2-5s", "5s+"]
        counts = [0]*(len(edges)-1)
        for v in okv:
            for i in range(len(edges)-1):
                if edges[i] <= v < edges[i+1]: counts[i] += 1; break
        for lab, c in zip(labels, counts):
            if c == 0: continue
            pct = 100.0*c/len(okv)
            print(f"     {lab:>10}  {pct:5.1f}%   {'█'*int(pct/2)}")

    print(f"\n  QTY-NARVON (har miqdor: nechta ok, o'rtacha bo'sh slot):")
    print(f"    {'qty':>5}  {'ok':>4} {'fail':>4}  {'slots med':>9}")
    for qty in sorted(per_qty):
        pq = per_qty[qty]; sl = pq["slots"]
        med = int(statistics.median(sl)) if sl else 0
        print(f"    {qty:>5}  {pq['ok']:>4} {pq['fail']:>4}  {med:>9}")
    print(f"\n[probe] tugadi — read-only, detektor ishlashda davom etdi.")


if __name__ == "__main__":
    main()
