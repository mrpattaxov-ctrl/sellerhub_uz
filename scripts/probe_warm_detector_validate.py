"""Yangi ISSIQ detektor yo'lini tasdiqlash (READ-ONLY, ~60s).

Aynan PROD funksiyasini chaqiradi: `slot_volume._measure_parallel` (warm pool +
`client.enable_fast_read` → issiq, retry'siz, 600ms timeout). O'lchaydi:
  - burst davomiyligi (p50/p95/max) — issiq bo'lsa ~300ms kutamiz
  - STRAGGLER-SKIP soni (>600ms → `_StragglerSkip` → sikl jimgina o'tkaziladi)
  - REAL xato soni (403/HTTP — cooldown chiqarardi)
Hech narsa band qilmaydi/yozmaydi (faqat _measure_parallel — snapshot/DB yo'q).

    docker compose exec -T app python scripts/probe_warm_detector_validate.py --seconds 60 --cycle 0.3
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _pctl(xs, p):
    if not xs:
        return 0.0
    ys = sorted(xs)
    return ys[min(len(ys) - 1, int(len(ys) * p))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=60)
    ap.add_argument("--cycle", type=float, default=0.3)
    ap.add_argument("--shop", default="")
    args = ap.parse_args()

    from app import app as flask_app
    from models import Shop
    from extensions import SessionLocal
    from sqlalchemy import select
    from postavki import slot_volume, slot_watch

    with flask_app.app_context():
        shop_id = args.shop.strip()
        if not shop_id:
            with SessionLocal() as db:
                shop_id = db.execute(
                    select(Shop.uzum_id).where(Shop.uzum_id.isnot(None)).limit(1)
                ).scalar_one_or_none()
            shop_id = str(shop_id) if shop_id else ""
        base, dim, pool = slot_watch._get_sku_context(shop_id)
        if not base:
            print(f"[validate] SKU konteksti yo'q (shop={shop_id})"); return

        to = os.environ.get("POSTAVKA_DETECT_TIMEOUT_SEC", "0.6")
        print(f"[validate] YANGI ISSIQ detektor yo'li — shop={shop_id} sku={base.get('skuId')} "
              f"pool={pool} ladder={len(slot_volume.LADDER)} req, cycle={args.cycle}s, "
              f"timeout={to}s, {args.seconds}s\n")

        bursts, skips, errs = [], 0, 0
        n = 0
        end = time.monotonic() + args.seconds
        nxt = time.monotonic() + 15
        while time.monotonic() < end:
            t0 = time.monotonic()
            try:
                snap = slot_volume._measure_parallel(shop_id, base, pool, slot_volume.LADDER)
                bursts.append((time.monotonic() - t0) * 1000.0)
            except slot_volume._StragglerSkip:
                skips += 1
            except Exception as e:
                errs += 1
                print(f"   [{n}] REAL XATO: {type(e).__name__}: {e}"[:160], flush=True)
            n += 1
            if time.monotonic() >= nxt:
                nxt += 15
                print(f"   ...{n} sikl — burst p50 ~{_pctl(bursts,.5):.0f}ms, "
                      f"straggler-skip {skips}, xato {errs}", flush=True)
            time.sleep(max(0.0, args.cycle - (time.monotonic() - t0)))

    print(f"\n{'='*52}")
    print(f"[validate] TUGADI — {n} sikl")
    print(f"   sog'lom burst : {len(bursts)}")
    print(f"   straggler-skip: {skips}  ({100.0*skips/max(1,n):.1f}% sikl — jimgina o'tkazildi)")
    print(f"   REAL xato     : {errs}  (403/HTTP — bo'lsa cooldown chiqarardi)")
    if bursts:
        print(f"\n   BURST davomiyligi (issiq):")
        print(f"     p50 {_pctl(bursts,.5):.0f}ms · p95 {_pctl(bursts,.95):.0f}ms · "
              f"max {max(bursts):.0f}ms · o'rtacha {statistics.mean(bursts):.0f}ms")
    verdict = "✅ TAYYOR" if errs == 0 else "⚠️ REAL XATO BOR — tekshir"
    print(f"\n[validate] {verdict} — read-only, hech narsa yozilmadi.")


if __name__ == "__main__":
    main()
