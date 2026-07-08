"""Bitta so'rov qancha tez — YUKSIZ round-trip (contention yo'q).

Ketma-ket (bittalab) so'rov yuboradi: har biri tugagach keyingisi. Shunda
yuk/navbat ta'siri YO'Q → Uzum'gacha borib-kelishning CHINAKAM eng tez vaqti
o'lchanadi. Birinchi so'rov (sovuq — TLS handshake) alohida ko'rsatiladi.

    docker compose cp scripts/probe_timeslot_single.py app:/app/scripts/probe_timeslot_single.py
    docker compose exec -T app python scripts/probe_timeslot_single.py --n 60
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import time

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


def _pctl(xs, p):
    ys = sorted(xs)
    return ys[min(len(ys) - 1, int(len(ys) * p))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=60, help="ketma-ket so'rov soni (minutes=0 bo'lsa)")
    ap.add_argument("--minutes", type=float, default=0, help=">0 bo'lsa shu daqiqa davomida ishlaydi")
    ap.add_argument("--gap", type=float, default=0.05, help="so'rovlar orasi kutish (s)")
    args = ap.parse_args()

    tok, shop_id, line, pool = _load_context()
    if not (tok and shop_id and line):
        print("[probe] kontekst topilmadi"); return

    url = f"{_BASE}/{shop_id}/v2/invoice/time-slot"
    h = {"Authorization": tok if tok.startswith("Bearer ") else f"Bearer {tok}",
         "Accept": "application/json", "Content-Type": "application/json",
         "Origin": "https://seller.uzum.uz", "Referer": "https://seller.uzum.uz/"}

    # Keep-alive session — 1-so'rovdan keyin ulanish qayta ishlatiladi (issiq).
    sess = requests.Session()
    ad = HTTPAdapter(pool_connections=2, pool_maxsize=2, max_retries=0)
    sess.mount("https://", ad); sess.mount("http://", ad)

    def _fresh_body():
        now = int(time.time() * 1000)
        return {"skuList": [line], "poolSource": pool,
                "timeFrom": now + _TIMEFROM_LEAD_MS, "timeTo": now + _SLOT_WINDOW_MS}

    def _one():
        t0 = time.monotonic()
        try:
            r = sess.post(url, headers=h, json=_fresh_body(), timeout=(5, 15))
            return (time.monotonic() - t0) * 1000, r.status_code
        except Exception as e:
            return (time.monotonic() - t0) * 1000, f"ERR:{type(e).__name__}"

    print(f"[probe] BITTA SO'ROV (ketma-ket, yuksiz) — {args.n} ta o'lchov")
    print(f"[probe] shop={shop_id} sku={line.get('skuId')} pool={pool}\n")

    # 1-so'rov = sovuq (TLS handshake kiradi).
    cold_ms, cold_st = _one()
    print(f"   1-so'rov (SOVUQ, TLS handshake bilan): {cold_ms:.0f}ms  [HTTP {cold_st}]")

    warm = []
    bad = 0
    if args.minutes and args.minutes > 0:
        end = time.monotonic() + args.minutes * 60
        next_report = time.monotonic() + 60
        while time.monotonic() < end:
            time.sleep(args.gap)
            ms, st = _one()
            if st == 200:
                warm.append(ms)
            else:
                bad += 1
            if time.monotonic() >= next_report:
                next_report += 60
                recent = warm[-200:] if warm else [0]
                print(f"   ...{len(warm)} ok, {bad} xato — hozirgi median "
                      f"~{_pctl(recent,.5):.0f}ms", flush=True)
        print(f"\n   {args.minutes} daqiqa tugadi: {len(warm)} ok, {bad} xato\n")
    else:
        for i in range(args.n):
            time.sleep(args.gap)
            ms, st = _one()
            if st == 200:
                warm.append(ms)
            else:
                bad += 1
        print(f"   keyingi {args.n} so'rov (ISSIQ, ulanish qayta ishlatilgan): "
              f"{len(warm)} ok, {bad} xato\n")

    if warm:
        print(f"   ISSIQ bitta-so'rov vaqti (yuksiz, chinakam eng tez):")
        print(f"     eng tez (min) : {min(warm):.0f}ms")
        print(f"     odatda  (p50) : {_pctl(warm,.5):.0f}ms")
        print(f"     p90           : {_pctl(warm,.9):.0f}ms")
        print(f"     sekin 5% (p95): {_pctl(warm,.95):.0f}ms")
        print(f"     eng sekin(max): {max(warm):.0f}ms")
        print(f"     o'rtacha      : {statistics.mean(warm):.0f}ms")
        print(f"\n   → Bu Uzum'gacha borib-kelishning eng tez vaqti (~{_pctl(warm,.5):.0f}ms).")
        print(f"     Yuk ostidagi (~210ms) shundan sekinroq bo'lsa — farqi bizning navbat.")
    print(f"\n[probe] tugadi — read-only.")


if __name__ == "__main__":
    main()
