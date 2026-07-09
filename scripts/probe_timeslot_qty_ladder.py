"""Time-slot QTY-NARVON stress-sinovi — 60s, HAR 10ms 1 so'rov (~100/s), narvonli.

Abdulaziz 2026-07-08 talabi: detektsiya endi BITTA qty (50) emas, balki AYLANMA
narvon — har so'rov keyingi miqdorni yuboradi: 50,100,150,200,250,…,750 (15 pog'ona,
step 50). Tempo: HAR 10ms 1 so'rov = ~100/s (isbotlangan xavfsiz tezlik). 60s →
~6000 so'rov, har miqdor ~400 marta. Bursty EMAS — tekis pacing (real detektor
«don't-wait» oqimiga yaqin).

Har so'rov: POST /v2/invoice/time-slot {skuList:[{...,quantityToStock:Q}], ...}.
Yig'iladi: outcome + latency + qaytgan slot soni (Q bo'yicha). Yakunda rasm-uslub
readout (Best/Typical/p95/Worst · success%) + histogram + qty bo'yicha qisqa jadval.

READ-ONLY — hech narsa yaratmaydi/o'zgartirmaydi (faqat /time-slot o'qish).

    docker compose cp scripts/probe_timeslot_qty_ladder.py app:/app/scripts/probe_timeslot_qty_ladder.py
    docker compose exec app python scripts/probe_timeslot_qty_ladder.py --seconds 60 --every-ms 10
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


def _load_context() -> tuple[str, str, dict, str]:
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
        base = pool = None
        if shop_id:
            base, _dim, pool = _get_sku_context(shop_id)
    if not base:
        return tok, shop_id, {}, ""
    return tok, shop_id, dict(base), (pool or "FULLFILMENT")


def _make_session(pool_size: int) -> requests.Session:
    s = requests.Session()
    ad = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size, max_retries=0)
    s.mount("https://", ad); s.mount("http://", ad)
    return s


def _classify(exc: Exception) -> str:
    s = repr(exc)
    if "ConnectTimeout" in s:
        return "connect-timeout"
    if "ReadTimeout" in s or "Read timed out" in s:
        return "read-timeout"
    if "reset by peer" in s or "RemoteDisconnected" in s or "ConnectionResetError" in s:
        return "conn-reset"
    if "Connection refused" in s:
        return "conn-refused"
    if "Max retries" in s:
        return "max-retries"
    return "other-error"


def _one(sess, url, headers, body, qty, connect_to, read_to):
    """-> (outcome, elapsed_s, qty, n_slots)."""
    t0 = time.monotonic()
    try:
        r = sess.post(url, headers=headers, json=body, timeout=(connect_to, read_to))
        n_slots = -1
        if r.status_code == 200:
            try:
                n_slots = len((r.json().get("payload") or {}).get("timeSlots") or [])
            except Exception:
                n_slots = -1
        return (f"http-{r.status_code}", time.monotonic() - t0, qty, n_slots)
    except Exception as e:
        return (_classify(e), time.monotonic() - t0, qty, -1)


def _pctl(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    xs = sorted(vals)
    return xs[min(len(xs) - 1, int(len(xs) * p))]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--every-ms", type=float, default=10.0, help="so'rovlar orasidagi masofa (ms)")
    ap.add_argument("--qty-start", type=int, default=50)
    ap.add_argument("--qty-step", type=int, default=50)
    ap.add_argument("--qty-count", type=int, default=15)
    ap.add_argument("--connect-timeout", type=float, default=5.0)
    ap.add_argument("--read-timeout", type=float, default=15.0)
    args = ap.parse_args()

    tok, shop_id, base, pool = _load_context()
    if not (tok and shop_id and base):
        print("[probe] kontekst topilmadi (token/shop/sku)"); return

    ladder = [args.qty_start + i * args.qty_step for i in range(args.qty_count)]
    url = f"{_BASE}/{shop_id}/v2/invoice/time-slot"
    h = {"Authorization": tok if tok.startswith("Bearer ") else f"Bearer {tok}",
         "Accept": "application/json", "Content-Type": "application/json",
         "Origin": "https://seller.uzum.uz", "Referer": "https://seller.uzum.uz/"}

    interval = args.every_ms / 1000.0
    total = int(round(args.seconds / interval))   # 60s / 0.010 = 6000
    achieved_rate = 1.0 / interval

    # Har so'rov o'z timeFrom oynasini oladi (now'ga bog'liq — probe davomida siljiydi).
    def _body(qty: int) -> dict:
        now = int(time.time() * 1000)
        return {"skuList": [{**base, "quantityToStock": qty}], "poolSource": pool,
                "timeFrom": now + _TIMEFROM_LEAD_MS, "timeTo": now + _SLOT_WINDOW_MS}

    workers = 200
    sess = _make_session(pool_size=220)

    print(f"[probe] QTY-NARVON: har {args.every_ms:.0f}ms 1 so'rov (~{achieved_rate:.0f}/s) "
          f"× {args.seconds}s = ~{total} so'rov")
    print(f"[probe] narvon (qty): {ladder}  (aylanma)")
    print(f"[probe] shop={shop_id} sku={base.get('skuId')} pool={pool} "
          f"timeout connect={args.connect_timeout}s read={args.read_timeout}s")
    print(f"[probe] har ~{max(1,int(1/interval))} so'rovda '.' ; xato bo'lsa harf\n")

    futures = []
    t_start = time.monotonic()
    tick = max(1, int(round(1.0 / interval)))  # ~sekundiga bir '.'
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i in range(total):
            target = t_start + i * interval
            dt = target - time.monotonic()
            if dt > 0:
                time.sleep(dt)
            qty = ladder[i % len(ladder)]
            futures.append(ex.submit(_one, sess, url, h, _body(qty), qty,
                                     args.connect_timeout, args.read_timeout))
            if i % tick == 0:
                sys.stdout.write("."); sys.stdout.flush()
        submit_span = time.monotonic() - t_start
        print(f"\n[probe] barcha {len(futures)} so'rov yuborildi ({submit_span:.1f}s da), "
              f"javoblar kutilmoqda...")
        results = [f.result() for f in futures]
    total_span = time.monotonic() - t_start

    # ── Xulosa ────────────────────────────────────────────────────
    by: dict[str, list[float]] = {}
    per_qty: dict[int, dict] = {}
    for outcome, el, qty, n_slots in results:
        by.setdefault(outcome, []).append(el)
        pq = per_qty.setdefault(qty, {"ok": 0, "fail": 0, "slots": []})
        if outcome == "http-200":
            pq["ok"] += 1
            if n_slots >= 0:
                pq["slots"].append(n_slots)
        else:
            pq["fail"] += 1

    ok = len(by.get("http-200", []))
    n = len(results)
    timeouts = {k: v for k, v in by.items() if "timeout" in k}
    n_timeout = sum(len(v) for v in timeouts.values())
    n_429 = len(by.get("http-429", []))
    n_403 = len(by.get("http-403", []))
    n_other = n - ok - n_timeout - n_429 - n_403

    print(f"\n{'='*64}")
    print(f"  YAKUN — {n} so'rov / {total_span:.1f}s  ({n/total_span:.0f} req/s haqiqiy)")
    print(f"{'='*64}")
    print(f"  200 OK        : {ok:>5}  ({100*ok/n:.1f}%)")
    print(f"  429 rate-limit: {n_429:>5}")
    print(f"  403 forbidden : {n_403:>5}")
    print(f"  TIMEOUT       : {n_timeout:>5}")
    print(f"  boshqa xato   : {n_other:>5}")
    print()
    for outcome in sorted(by, key=lambda k: -len(by[k])):
        vals = by[outcome]
        print(f"    {outcome:<16} n={len(vals):>5}  "
              f"min={min(vals)*1000:6.0f}ms med={statistics.median(vals)*1000:6.0f}ms "
              f"max={max(vals)*1000:6.0f}ms")

    if ok:
        okv = by["http-200"]
        best, p50 = min(okv), _pctl(okv, 0.5)
        p95, worst = _pctl(okv, 0.95), max(okv)
        print(f"\n{'─'*64}")
        print(f"  ⚡ Best {best*1000:.0f}ms   Typical {p50*1000:.0f}ms   "
              f"p95 {p95*1000:.0f}ms   Worst {worst*1000:.0f}ms   ·   "
              f"{100*ok/n:.1f}% success")
        print(f"{'─'*64}")
        edges = [0, .2, .3, .5, 1, 2, 5, 10, 1e9]
        labels = ["<200ms", "200-300ms", "300-500ms", "500ms-1s", "1-2s", "2-5s", "5-10s", "10s+"]
        counts = [0]*(len(edges)-1)
        for v in okv:
            for i in range(len(edges)-1):
                if edges[i] <= v < edges[i+1]:
                    counts[i] += 1; break
        for lab, c in zip(labels, counts):
            if c == 0:
                continue
            pct = 100.0*c/len(okv)
            print(f"     {lab:>10}  {pct:5.1f}%   {'█'*int(pct/2)}")

    # ── Qty-narvon bo'yicha (bonus: qaysi hajm bookable) ──────────
    print(f"\n  QTY-NARVON (har miqdor uchun qaytgan bo'sh slot soni):")
    print(f"    {'qty':>5}  {'ok':>4} {'fail':>4}  {'slots med':>9}  {'slots max':>9}")
    for qty in sorted(per_qty):
        pq = per_qty[qty]
        sl = pq["slots"]
        med = int(statistics.median(sl)) if sl else 0
        mx = max(sl) if sl else 0
        print(f"    {qty:>5}  {pq['ok']:>4} {pq['fail']:>4}  {med:>9}  {mx:>9}")

    if n_timeout:
        allt = [v for vals in timeouts.values() for v in vals]
        print(f"\n  TIMEOUT vaqti: min={min(allt):.1f}s med={statistics.median(allt):.1f}s "
              f"max={max(allt):.1f}s  (turlari: "
              + ", ".join(f'{k}×{len(v)}' for k, v in timeouts.items()) + ")")
    print(f"\n[probe] tugadi — read-only.")


if __name__ == "__main__":
    main()
