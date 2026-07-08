"""«GRAB» vaqtini o'lchash — check→(book) ikki-bosqichli zanjir, 100 so'rov/s ostida.

Savol: 100/s ostida bitta «slot ushlash» (check + book) qancha vaqt oladi?
Real grab = 2 ketma-ket round-trip: (1) DETECT — bo'sh slotni ko'rish, (2) BOOK —
o'sha slotni band qilish. BOOK real yozuv (set_time_slot, 3× budjet) — sinov uchun
OTMAYMIZ. O'rniga 2-bosqich sifatida AYNAN o'sha endpointga yana bir READ yuboramiz
(bir xil round-trip) → zanjir vaqti realga teng (faqat serverdagi yozuv-ishi
o'lchanmaydi). Har «reptitsiya» = t0→detect→book-proxy→t1, t1−t0 = grab vaqti.

Ko'p reptitsiyani parallel yuritamiz → ~100 so'rov/s yuk (2 so'rov × 50 rep/s).
READ-ONLY — hech narsa band qilinmaydi/o'zgartirilmaydi.

    docker compose cp scripts/probe_timeslot_grab_rehearsal.py app:/app/scripts/probe_timeslot_grab_rehearsal.py
    docker compose exec -T app python scripts/probe_timeslot_grab_rehearsal.py --seconds 40 --rep-per-sec 50
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import threading
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
        base = pool = None
        if shop_id:
            base, _dim, pool = _get_sku_context(shop_id)
    if not base:
        return tok, shop_id, {}, ""
    return tok, shop_id, {**base, "quantityToStock": 150}, (pool or "FULLFILMENT")


def _make_session(pool_size):
    s = requests.Session()
    ad = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size, max_retries=0)
    s.mount("https://", ad); s.mount("http://", ad)
    return s


def _pctl(xs, p):
    if not xs:
        return 0.0
    ys = sorted(xs)
    return ys[min(len(ys) - 1, int(len(ys) * p))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=40)
    ap.add_argument("--rep-per-sec", type=int, default=50, help="reptitsiya/s (×2 so'rov = req/s)")
    ap.add_argument("--connect-timeout", type=float, default=5.0)
    ap.add_argument("--read-timeout", type=float, default=8.0)
    ap.add_argument("--workers", type=int, default=300)
    args = ap.parse_args()

    tok, shop_id, line, pool = _load_context()
    if not (tok and shop_id and line):
        print("[probe] kontekst topilmadi"); return

    url = f"{_BASE}/{shop_id}/v2/invoice/time-slot"
    h = {"Authorization": tok if tok.startswith("Bearer ") else f"Bearer {tok}",
         "Accept": "application/json", "Content-Type": "application/json",
         "Origin": "https://seller.uzum.uz", "Referer": "https://seller.uzum.uz/"}
    sess = _make_session(pool_size=args.rep_per_sec * 3 + 40)

    def _fresh_body():
        now = int(time.time() * 1000)
        return {"skuList": [line], "poolSource": pool,
                "timeFrom": now + _TIMEFROM_LEAD_MS, "timeTo": now + _SLOT_WINDOW_MS}

    def _one_hop():
        t0 = time.monotonic()
        try:
            r = sess.post(url, headers=h, json=_fresh_body(),
                          timeout=(args.connect_timeout, args.read_timeout))
            return (time.monotonic() - t0, r.status_code == 200)
        except Exception:
            return (time.monotonic() - t0, False)

    lock = threading.Lock()
    hop_times = []     # bitta round-trip (check)
    grab_times = []    # to'liq check→book zanjiri
    ok_grabs = 0
    bad = 0

    def _rehearsal():
        nonlocal ok_grabs, bad
        t0 = time.monotonic()
        d_ms, d_ok = _one_hop()    # 1) DETECT
        b_ms, b_ok = _one_hop()    # 2) BOOK (proxy — bir xil round-trip)
        total = time.monotonic() - t0
        with lock:
            hop_times.append(d_ms); hop_times.append(b_ms)
            grab_times.append(total)
            if d_ok and b_ok:
                ok_grabs += 1
            else:
                bad += 1

    print(f"[probe] GRAB REPTITSIYA: {args.rep_per_sec} rep/s × {args.seconds}s "
          f"= ~{args.rep_per_sec*2} so'rov/s; har rep = check→book (2 round-trip)")
    print(f"[probe] shop={shop_id} sku={line.get('skuId')} pool={pool}")
    print(f"[probe] READ-ONLY (book o'rniga read-proxy — hech narsa band qilinmaydi)\n")

    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for sec in range(args.seconds):
            target = start + sec
            dt = target - time.monotonic()
            if dt > 0:
                time.sleep(dt)
            for _ in range(args.rep_per_sec):
                ex.submit(_rehearsal)
            if sec % 5 == 0:
                with lock:
                    n = len(grab_times)
                    g50 = _pctl(grab_times, 0.5)
                print(f"   {sec:>3}s  reptitsiya={n:>5}  hozirgi grab-median={g50*1000:.0f}ms")
                sys.stdout.flush()
        print("\n[probe] qolgan javoblar yig'ilmoqda...")
        time.sleep(args.read_timeout + 3)

    span = time.monotonic() - start
    with lock:
        n_rep = len(grab_times)
        n_req = len(hop_times)
    print(f"\n{'='*66}")
    print(f"  NATIJA — {span:.0f}s, {n_rep} grab-reptitsiya, {n_req} so'rov ({n_req/span:.0f}/s)")
    print(f"{'='*66}")
    print(f"  muvaffaqiyatli grab (2/2 hop OK): {ok_grabs}   nuqsonli: {bad}\n")

    print(f"  BITTA CHECK (1 round-trip):")
    print(f"     p50={_pctl(hop_times,.5)*1000:.0f}ms  p90={_pctl(hop_times,.9)*1000:.0f}ms  "
          f"p95={_pctl(hop_times,.95)*1000:.0f}ms  p99={_pctl(hop_times,.99)*1000:.0f}ms  "
          f"max={max(hop_times)*1000:.0f}ms" if hop_times else "     (yo'q)")
    print(f"\n  TO'LIQ GRAB (check→book, 2 round-trip):")
    print(f"     p50={_pctl(grab_times,.5)*1000:.0f}ms  p90={_pctl(grab_times,.9)*1000:.0f}ms  "
          f"p95={_pctl(grab_times,.95)*1000:.0f}ms  p99={_pctl(grab_times,.99)*1000:.0f}ms  "
          f"max={max(grab_times)*1000:.0f}ms" if grab_times else "     (yo'q)")
    if grab_times:
        print(f"\n  → 100/s ostida ODATDA bitta slot ushlash ~{_pctl(grab_times,.5)*1000:.0f}ms; "
              f"eng yomon holatda ~{_pctl(grab_times,.95)*1000:.0f}ms (p95).")
        print(f"  ⚠️ Eslatma: 2-bosqich (book) real yozuv EMAS — read-proxy. Haqiqiy "
              f"set_time_slot serverda biroz ko'proq ishlashi mumkin (~+50-150ms).")
    print(f"\n[probe] tugadi — read-only.")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
