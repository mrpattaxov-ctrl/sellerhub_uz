"""15-so'rovlik BURST kadensi — burst qancha davom etadi va bursts ORASIDA
qancha kutadi? (READ-ONLY, deep-parallel rejimini AYNAN takrorlaydi.)

Maqsad (Abdulaziz): har tsikl = 15 parallel time-slot so'rovi (deep-parallel).
Prod `slot_volume._measure_parallel`ни AYNAN chaqiradi (o'sha sovuq-ulanish
naqshи — har burst yangi ThreadPoolExecutor). So'ng prod fixed-rate pauzasini
qo'llaydi: nap = max(0, cycle - burst). Har tsikl uchun yozadi:
  - burst_ms   : 15 parallel so'rov qancha davom etdi (eng sekin so'rov)
  - nap_ms     : shu tsikldan keyin qancha KUTDI (0 bo'lsa = orqama-orqa, seamless)

Oxirida: burst p50/p95/max, nap p50/p95, %seamless (nap==0), erishilgan
burst/s va so'rov/s, 403/xato soni.

⚠️ ~15 so'rov × (300/cycle) ≈ ~11k so'rov/5daq (~37.5/s). Read-only —
3x budjetga TEGMAYDI. 403 kelsa DARHOL to'xtaydi (July-3 hodisasini takroramaslik).

    docker compose cp scripts/probe_timeslot_burst_cadence.py app:/app/scripts/probe_timeslot_burst_cadence.py
    docker compose exec -T app python scripts/probe_timeslot_burst_cadence.py --minutes 5 --cycle 0.4
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
    ap.add_argument("--minutes", type=float, default=5.0, help="necha daqiqa ishlaydi")
    ap.add_argument("--cycle", type=float, default=0.4, help="fixed-rate tsikl nishoni (s) — prod=0.4")
    ap.add_argument("--shop", default="", help="do'kon uzum_id (bo'sh → birinchisi)")
    ap.add_argument("--stop-on-403", type=int, default=1, help="1 → birinchi 403/xatoda to'xta")
    ap.add_argument("--warm", action="store_true",
                    help="ISSIQ ulanish: DOIMIY thread-pool (har burst yangi emas) → "
                         "ulanishlar bursts aro qayta ishlatiladi (keep-alive).")
    ap.add_argument("--burst", type=int, default=0,
                    help="burst hajmi (parallel so'rov soni). 0 → ladder uzunligi (15). "
                         ">15 bo'lsa ladder miqdorlari takrorlanadi. FAQAT --warm bilan.")
    args = ap.parse_args()

    from app import app as flask_app
    from models import Shop
    from extensions import SessionLocal
    from sqlalchemy import select
    from postavki import slot_volume, slot_watch, client
    from concurrent.futures import ThreadPoolExecutor

    # ISSIQ o'lchov: DOIMIY pool (bir marta yaratiladi) → worker thread'lar
    # yashaydi → thread-local Session'lar (ulanishlar) bursts aro QAYTA
    # ISHLATILADI (keep-alive). Prod `_measure_parallel` har burst yangi pool
    # yaratadi (SOVUQ) — bu esa aynan shu bilan farq qiladi (yagona o'zgaruvchi).
    _warm_pool = {"ex": None}
    req_ms = []  # HAR bitta so'rov latency (ms) — best/worst uchun

    def _measure_warm(shop_id, base, pool, quantities):
        ex = _warm_pool["ex"]

        def _probe(q):
            t0 = time.monotonic()
            slots = client.get_time_slots(
                shop_id, [{**base, "quantityToStock": int(q)}], pool)
            req_ms.append((time.monotonic() - t0) * 1000.0)
            return int(q), (slots or [])

        results = list(ex.map(_probe, quantities))
        snap = {}
        for q, slots in results:
            for s in slots:
                sf = s.get("timeFrom") if isinstance(s, dict) else None
                if not sf:
                    continue
                sf = int(sf)
                cur = snap.get(sf)
                if cur is None or q > cur["vol"]:
                    snap[sf] = {"vol": int(q), "to": int(s.get("timeTo") or 0)}
        return snap

    with flask_app.app_context():
        shop_id = args.shop.strip()
        if not shop_id:
            with SessionLocal() as db:
                shop_id = db.execute(
                    select(Shop.uzum_id).where(Shop.uzum_id.isnot(None)).limit(1)
                ).scalar_one_or_none()
            shop_id = str(shop_id) if shop_id else ""
        if not shop_id:
            print("[probe] do'kon topilmadi"); return
        base, dim, pool = slot_watch._get_sku_context(shop_id)
        if not base:
            print(f"[probe] SKU konteksti yo'q (shop={shop_id}) — token/ombor?"); return
        ladder = slot_volume.LADDER
        burst = args.burst if args.burst > 0 else len(ladder)
        # burst hajmigacha ladder miqdorlarini takrorlaymiz (realistik varied qty).
        probe_qs = [ladder[i % len(ladder)] for i in range(burst)]
        mode = "ISSIQ (warm, doimiy pool)" if args.warm else "SOVUQ (cold, prod xulqi)"
        if args.warm:
            _warm_pool["ex"] = ThreadPoolExecutor(max_workers=min(burst, 128))
        elif burst != len(ladder):
            print("[probe] ⚠️ --burst faqat --warm bilan; sovuq yo'l 15-ladder'da qoladi.")
            burst = len(ladder); probe_qs = sorted(ladder)

        print(f"[probe] BURST KADENSI — {mode} (READ-ONLY)")
        print(f"[probe] shop={shop_id} sku={base.get('skuId')} pool={pool} "
              f"burst={burst} req/burst, cycle={args.cycle}s, {args.minutes} daqiqa\n")

        bursts = []   # burst davomiyligi (ms)
        naps = []     # bursts orasi kutish (ms)
        slot_counts = []
        n_cycles = 0
        n_err = 0
        end = time.monotonic() + args.minutes * 60
        next_report = time.monotonic() + 30

        while time.monotonic() < end:
            cycle_start = time.monotonic()
            err = None
            snap = {}
            try:
                if args.warm:
                    snap = _measure_warm(shop_id, base, pool, probe_qs)
                else:
                    snap = slot_volume._measure_parallel(shop_id, base, pool, ladder)
            except Exception as e:
                err = f"{type(e).__name__}: {e}"[:120]
            burst_ms = (time.monotonic() - cycle_start) * 1000.0
            nap = max(0.0, args.cycle - (time.monotonic() - cycle_start))
            n_cycles += 1

            if err:
                n_err += 1
                print(f"   [{n_cycles}] burst={burst_ms:.0f}ms XATO: {err}", flush=True)
                if args.stop_on_403 and ("403" in err or "429" in err):
                    print(f"\n[probe] ⛔ {('403' if '403' in err else '429')} — DARHOL to'xtatilyapti (himoya).")
                    break
            else:
                bursts.append(burst_ms)
                naps.append(nap * 1000.0)
                slot_counts.append(len(snap))

            if time.monotonic() >= next_report:
                next_report += 30
                seamless = sum(1 for x in naps if x < 1.0)
                print(f"   ...{n_cycles} tsikl, {n_err} xato — "
                      f"burst p50 ~{_pctl(bursts,.5):.0f}ms, "
                      f"kutish p50 ~{_pctl(naps,.5):.0f}ms, "
                      f"seamless {seamless}/{len(naps)}", flush=True)

            time.sleep(nap)

    # ── Xulosa ───────────────────────────────────────────────────────
    print(f"\n{'='*56}")
    print(f"[probe] TUGADI — {n_cycles} tsikl, {n_err} xato, {len(bursts)} sog'lom o'lchov")
    if not bursts:
        print("[probe] sog'lom o'lchov yo'q."); return

    elapsed = args.minutes * 60
    total_req = n_cycles * burst
    seamless = sum(1 for x in naps if x < 1.0)

    print(f"\n  15-SO'ROVLIK BURST qancha DAVOM ETADI:")
    print(f"     eng tez (min) : {min(bursts):.0f}ms")
    print(f"     odatda  (p50) : {_pctl(bursts,.5):.0f}ms")
    print(f"     p90           : {_pctl(bursts,.9):.0f}ms")
    print(f"     sekin 5% (p95): {_pctl(bursts,.95):.0f}ms")
    print(f"     eng sekin(max): {max(bursts):.0f}ms")
    print(f"     o'rtacha      : {statistics.mean(bursts):.0f}ms")

    print(f"\n  BURSTS ORASIDA qancha KUTADI (cycle={args.cycle}s da):")
    print(f"     odatda  (p50) : {_pctl(naps,.5):.0f}ms")
    print(f"     p90           : {_pctl(naps,.9):.0f}ms")
    print(f"     max           : {max(naps):.0f}ms")
    print(f"     seamless (kutish~0): {seamless}/{len(naps)} tsikl "
          f"({100.0*seamless/max(1,len(naps)):.0f}%)")

    achieved_bps = n_cycles / elapsed
    achieved_rps = total_req / elapsed
    print(f"\n  ERISHILGAN TEZLIK ({args.minutes} daqiqada):")
    print(f"     {n_cycles} burst / {elapsed:.0f}s = {achieved_bps:.2f} burst/s")
    print(f"     {total_req} so'rov / {elapsed:.0f}s = {achieved_rps:.1f} so'rov/s")
    print(f"     har slot ~{_pctl(slot_counts,.5):.0f} ta (p50)")

    # SEAMLESS bo'lsa nima bo'lardi (kutishni olib tashlab, orqama-orqa):
    if bursts:
        seamless_bps = 1000.0 / statistics.mean(bursts)
        print(f"\n  → AGAR SEAMLESS (kutishsiz, orqama-orqa) bo'lsa: "
              f"~{seamless_bps:.2f} burst/s (~{seamless_bps*burst:.0f} so'rov/s)")
        gap_p50 = _pctl(naps,.5)
        if gap_p50 < 1.0:
            print(f"     ⚠️ ALLAQACHON ~seamless: burst ({_pctl(bursts,.5):.0f}ms) ≥ "
                  f"cycle ({args.cycle*1000:.0f}ms) → kutish deyarli yo'q.")
        else:
            print(f"     Hozir p50 ~{gap_p50:.0f}ms kutadi → uni 0 qilsa "
                  f"~{(gap_p50/_pctl(bursts,.5) if _pctl(bursts,.5) else 0)*100:.0f}% tezroq tsikl.")
    if req_ms:
        print(f"\n  HAR BITTA SO'ROV (individual, {len(req_ms)} ta o'lchov):")
        print(f"     ⚡ ENG TEZ  (best) : {min(req_ms):.0f}ms")
        print(f"     odatda     (p50)  : {_pctl(req_ms,.5):.0f}ms")
        print(f"     p95               : {_pctl(req_ms,.95):.0f}ms")
        print(f"     p99               : {_pctl(req_ms,.99):.0f}ms")
        print(f"     🐌 ENG SEKIN(worst): {max(req_ms):.0f}ms")
        # Taqsimot (histogram): har vaqt-oralig'ida nechta so'rov tushdi.
        edges = [0, 200, 250, 300, 400, 500, 750, 1000, 1500, 2000, 10**9]
        labels = ["<200", "200-250", "250-300", "300-400", "400-500",
                  "500-750", "750-1k", "1k-1.5k", "1.5k-2k", "2k+"]
        counts = [0] * (len(edges) - 1)
        for v in req_ms:
            for i in range(len(edges) - 1):
                if edges[i] <= v < edges[i + 1]:
                    counts[i] += 1
                    break
        total = len(req_ms)
        print(f"\n  TAQSIMOT (har vaqt-oraliqda nechta so'rov tushdi):")
        for lab, c in zip(labels, counts):
            pct = 100.0 * c / total
            bar = "█" * int(pct / 2)
            print(f"     {lab:>8}ms  {c:>6}  {pct:5.1f}%  {bar}")
        slow = sorted(req_ms, reverse=True)[:10]
        print(f"\n  ENG SEKIN 10 so'rov (ms): " + ", ".join(f"{v:.0f}" for v in slow))
        print(f"     → burst = shu so'rovlarning ENG SEKINi bilan tugaydi (slowest-of-N).")

    print(f"\n[probe] read-only — hech narsa band qilinmadi.")


if __name__ == "__main__":
    main()
