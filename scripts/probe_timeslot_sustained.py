"""Time-slot BARQAROR yuk sinovi — 60s davomida HAR SEKUND 100 parallel.

Real autoslot ehtiyoji: «har sekund 100 so'rov». Bu skript AYNAN shuni sinaydi —
pooled Session (real ilova ishlatadigan yo'l) bilan, 60 sekund davomida har
sekund boshida 100 ta parallel time-slot o'qish so'rovini otadi (~6000 so'rov).
Savol: timeout bo'ladimi, nechta va qancha?

Har so'rov natijasi + elapsed yig'iladi. Yakunda: 200 / 429 / timeout (turi
bo'yicha) / boshqa xato soni, timeout'lar vaqti (min/median/max), muvaffaqiyatli
javob latency (p50/p95/max), va haqiqiy erishilgan so'rov/sekund.

READ-ONLY — hech narsa yaratmaydi/o'zgartirmaydi.

    docker compose cp scripts/probe_timeslot_sustained.py app:/app/scripts/probe_timeslot_sustained.py
    docker compose exec app python scripts/probe_timeslot_sustained.py --seconds 60 --per-sec 100
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
    return tok, shop_id, {**base, "quantityToStock": 150}, (pool or "FULLFILMENT")


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


def _one(sess, url, headers, body, connect_to, read_to) -> tuple[str, float, int]:
    """-> (outcome, elapsed_s, sec_tick placeholder set by caller)."""
    t0 = time.monotonic()
    try:
        r = sess.post(url, headers=headers, json=body, timeout=(connect_to, read_to))
        return (f"http-{r.status_code}", time.monotonic() - t0, 0)
    except Exception as e:
        return (_classify(e), time.monotonic() - t0, 0)


def _pctl(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    xs = sorted(vals)
    return xs[min(len(xs) - 1, int(len(xs) * p))]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--per-sec", type=int, default=100)
    ap.add_argument("--connect-timeout", type=float, default=5.0)
    ap.add_argument("--read-timeout", type=float, default=15.0)
    args = ap.parse_args()

    tok, shop_id, line, pool = _load_context()
    if not (tok and shop_id and line):
        print("[probe] kontekst topilmadi (token/shop/sku)"); return

    url = f"{_BASE}/{shop_id}/v2/invoice/time-slot"
    now = int(time.time() * 1000)
    body = {"skuList": [line], "poolSource": pool,
            "timeFrom": now + _TIMEFROM_LEAD_MS, "timeTo": now + _SLOT_WINDOW_MS}
    h = {"Authorization": tok if tok.startswith("Bearer ") else f"Bearer {tok}",
         "Accept": "application/json", "Content-Type": "application/json",
         "Origin": "https://seller.uzum.uz", "Referer": "https://seller.uzum.uz/"}

    # Ishchilar zaxira bilan — sekin so'rov bo'lsa ham pacing buzilmasin. LEKIN
    # cheklaymiz (OOM/thread-portlash): juda baland per-sec'da ham xavfsiz qoladi.
    workers = min(args.per_sec * 3, 400)
    sess = _make_session(pool_size=min(args.per_sec + 20, 420))

    print(f"[probe] BARQAROR: {args.per_sec}/sek × {args.seconds}s "
          f"(~{args.per_sec * args.seconds} so'rov), pooled Session, "
          f"timeout connect={args.connect_timeout}s read={args.read_timeout}s")
    print(f"[probe] shop={shop_id} sku={line.get('skuId')} pool={pool}")
    print(f"[probe] har sekundda bir '.' ; 429/timeout bo'lsa harf chiqadi\n")

    futures = []  # (future, submit_sec)
    t_start = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for sec in range(args.seconds):
            # Shu sekund boshigacha kutamiz (aniq 1/sek tempo).
            target = t_start + sec
            dt = target - time.monotonic()
            if dt > 0:
                time.sleep(dt)
            for _ in range(args.per_sec):
                futures.append((ex.submit(_one, sess, url, h, body,
                                          args.connect_timeout, args.read_timeout), sec))
            sys.stdout.write("."); sys.stdout.flush()
        submit_span = time.monotonic() - t_start
        print(f"\n[probe] barcha {len(futures)} so'rov yuborildi ({submit_span:.1f}s da), "
              f"javoblar kutilmoqda...")
        # Barcha javoblarni yig'amiz.
        results = []
        per_sec_fail = {}
        for fut, sec in futures:
            outcome, elapsed, _ = fut.result()
            results.append((outcome, elapsed))
            if not outcome.startswith("http-2"):
                per_sec_fail[sec] = per_sec_fail.get(sec, 0) + 1
    total_span = time.monotonic() - t_start

    # ── Xulosa ────────────────────────────────────────────────────
    by: dict[str, list[float]] = {}
    for outcome, el in results:
        by.setdefault(outcome, []).append(el)

    ok = len(by.get("http-200", []))
    n = len(results)
    timeouts = {k: v for k, v in by.items() if "timeout" in k}
    n_timeout = sum(len(v) for v in timeouts.values())
    n_429 = len(by.get("http-429", []))
    n_other = n - ok - n_timeout - n_429

    print(f"\n{'='*60}")
    print(f"  YAKUN — {n} so'rov / {total_span:.1f}s  ({n/total_span:.0f} req/s haqiqiy)")
    print(f"{'='*60}")
    print(f"  200 OK        : {ok:>5}  ({100*ok/n:.1f}%)")
    print(f"  429 rate-limit: {n_429:>5}")
    print(f"  TIMEOUT       : {n_timeout:>5}")
    print(f"  boshqa xato   : {n_other:>5}")
    print()
    for outcome in sorted(by, key=lambda k: -len(by[k])):
        vals = by[outcome]
        print(f"    {outcome:<16} n={len(vals):>5}  "
              f"min={min(vals):5.1f}s med={statistics.median(vals):5.1f}s "
              f"max={max(vals):5.1f}s")
    if ok:
        okv = by["http-200"]
        print(f"\n  200 latency: p50={_pctl(okv,0.5):.2f}s  p95={_pctl(okv,0.95):.2f}s  "
              f"p99={_pctl(okv,0.99):.2f}s  max={max(okv):.2f}s")
        print(f"\n  HAR BITTA 200-OK SO'ROV (ms):")
        print(f"     ⚡ ENG TEZ  (best) : {min(okv)*1000:.0f}ms")
        print(f"     odatda     (p50)  : {_pctl(okv,0.5)*1000:.0f}ms")
        print(f"     p95               : {_pctl(okv,0.95)*1000:.0f}ms")
        print(f"     p99               : {_pctl(okv,0.99)*1000:.0f}ms")
        print(f"     🐌 ENG SEKIN(worst): {max(okv)*1000:.0f}ms")
        edges = [0, .2, .3, .5, 1, 2, 5, 10, 1e9]
        labels = ["<200", "200-300", "300-500", "500ms-1s", "1-2s", "2-5s", "5-10s", "10s+"]
        counts = [0]*(len(edges)-1)
        for v in okv:
            for i in range(len(edges)-1):
                if edges[i] <= v < edges[i+1]:
                    counts[i] += 1; break
        print(f"\n  TAQSIMOT (200-OK so'rovlar; {n_timeout} timeout ALOHIDA — pastda):")
        for lab, c in zip(labels, counts):
            pct = 100.0*c/len(okv)
            print(f"     {lab:>9}  {c:>6}  {pct:5.1f}%  {'█'*int(pct/2)}")
    if n_timeout:
        allt = [v for vals in timeouts.values() for v in vals]
        print(f"  TIMEOUT vaqti: min={min(allt):.1f}s med={statistics.median(allt):.1f}s "
              f"max={max(allt):.1f}s  (turlari: "
              + ", ".join(f'{k}×{len(v)}' for k, v in timeouts.items()) + ")")
        worst = sorted(per_sec_fail.items(), key=lambda x: -x[1])[:5]
        print(f"  eng ko'p xato sekundlar: "
              + ", ".join(f's{s}×{c}' for s, c in worst))
    print(f"\n[probe] tugadi — read-only.")


if __name__ == "__main__":
    main()
