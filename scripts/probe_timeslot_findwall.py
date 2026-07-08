"""Time-slot DEVOR-USHLAGICH — sustained 100/s bilan devorni chiqarib, o'sha
«other» xatoning ANIQ status-kod + tana + header'ini USHLAB, DARHOL to'xtaydi.

Maqsad: devor (~5-6 daqiqa sustained 100/s'da chiqadi) chiqishi bilan birinchi
N ta non-200 javobni to'liq yozib olib (nima ekanini bilish uchun), zudlik bilan
to'xtash — prod blok oynasini MINIMUM ushlab turish uchun. Devor chiqmasa,
xavfsizlik uchun max vaqtда o'zi to'xtaydi.

READ-ONLY (band qilmaydi). ⚠️ Devorni ATAYIN chiqaradi → prod Uzum trafigiga
qisqa vaqt tegishi mumkin; devor yuk to'xtagach o'zi tiklanadi (2026-07-07 sinov).

    docker compose cp scripts/probe_timeslot_findwall.py app:/app/scripts/probe_timeslot_findwall.py
    docker compose exec -T app python scripts/probe_timeslot_findwall.py
"""
from __future__ import annotations

import argparse
import os
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

# Non-200'да to'liq yozib olamiz (nima ekanini bilish uchun barcha header'lar).
_ALL_HEADERS = True


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-sec", type=int, default=100)
    ap.add_argument("--max-secs", type=int, default=720, help="devor chiqmasa xavfsiz to'xtash (default 12 daq)")
    ap.add_argument("--capture", type=int, default=15, help="necha non-200 ushlagach to'xtaydi")
    ap.add_argument("--connect-timeout", type=float, default=5.0)
    ap.add_argument("--read-timeout", type=float, default=8.0)
    ap.add_argument("--max-inflight", type=int, default=1000)
    ap.add_argument("--workers", type=int, default=400)
    args = ap.parse_args()

    tok, shop_id, line, pool = _load_context()
    if not (tok and shop_id and line):
        print("[probe] kontekst topilmadi"); return

    url = f"{_BASE}/{shop_id}/v2/invoice/time-slot"
    # ⚠️ timeFrom KELAJAKDA bo'lishi shart — HAR so'rovда QAYTA hisoblanadi (real
    # get_time_slots shunday qiladi). Bir marta muzlatib qo'ysang, 5 daqiqадан keyin
    # o'tmishga tushib validation-failed-001 (400) beradi — bu Uzum bloki EMAS.
    def _fresh_body():
        now = int(time.time() * 1000)
        return {"skuList": [line], "poolSource": pool,
                "timeFrom": now + _TIMEFROM_LEAD_MS, "timeTo": now + _SLOT_WINDOW_MS}
    h = {"Authorization": tok if tok.startswith("Bearer ") else f"Bearer {tok}",
         "Accept": "application/json", "Content-Type": "application/json",
         "Origin": "https://seller.uzum.uz", "Referer": "https://seller.uzum.uz/"}

    sess = _make_session(pool_size=args.per_sec + 40)
    stop = threading.Event()
    lock = threading.Lock()
    counts = {"200": 0, "non200": 0}
    captured = []
    first_bad_sec = {"v": None}
    inflight = {"v": 0}
    infl_lock = threading.Lock()
    start = time.monotonic()

    def _task(sec):
        try:
            r = sess.post(url, headers=h, json=_fresh_body(),
                          timeout=(args.connect_timeout, args.read_timeout))
            status = r.status_code
            if status == 200:
                with lock:
                    counts["200"] += 1
            else:
                bodytext = (r.text or "")[:500]
                hdrs = dict(r.headers) if _ALL_HEADERS else {}
                with lock:
                    counts["non200"] += 1
                    if first_bad_sec["v"] is None:
                        first_bad_sec["v"] = sec
                    if len(captured) < args.capture:
                        captured.append({"sec": sec, "kind": "http",
                                         "status": status, "body": bodytext, "headers": hdrs})
                        if len(captured) >= args.capture:
                            stop.set()
        except Exception as e:
            with lock:
                counts["non200"] += 1
                if first_bad_sec["v"] is None:
                    first_bad_sec["v"] = sec
                if len(captured) < args.capture:
                    captured.append({"sec": sec, "kind": "exception",
                                     "status": None, "body": repr(e)[:300], "headers": {}})
                    if len(captured) >= args.capture:
                        stop.set()
        finally:
            with infl_lock:
                inflight["v"] -= 1

    print(f"[probe] DEVOR-USHLAGICH: {args.per_sec}/s sustained; "
          f"{args.capture} ta non-200 ushlagach STOP; xavfsiz-max {args.max_secs}s")
    print(f"[probe] shop={shop_id} sku={line.get('skuId')} pool={pool} token={tok[:10]}..\n")
    print(f"   {'s':>4} {'200(cum)':>9} {'non200':>7}  holat")
    print("   " + "-" * 40)
    sys.stdout.flush()

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        last_report = -1
        for sec in range(args.max_secs):
            if stop.is_set():
                break
            target = start + sec
            dt = target - time.monotonic()
            if dt > 0:
                time.sleep(dt)
            for _ in range(args.per_sec):
                if stop.is_set():
                    break
                with infl_lock:
                    if inflight["v"] >= args.max_inflight:
                        continue
                    inflight["v"] += 1
                ex.submit(_task, sec)
            # Har 15s hisobot (yoki devor chiqishi bilan).
            if sec % 15 == 0 and sec != last_report:
                last_report = sec
                with lock:
                    c2, cbad = counts["200"], counts["non200"]
                tag = "toza" if cbad == 0 else f"⚠️ DEVOR (birinchi bad: {first_bad_sec['v']}s)"
                print(f"   {sec:>4} {c2:>9} {cbad:>7}  {tag}")
                sys.stdout.flush()
        # Devor chiqib captura to'lgach — qolganini kutamiz.
        print("\n[probe] to'xtatilmoqda, qolgan javoblar yig'ilmoqda...")
        sys.stdout.flush()
        deadline = time.monotonic() + args.read_timeout + 5
        while time.monotonic() < deadline:
            with infl_lock:
                if inflight["v"] <= 0:
                    break
            time.sleep(0.3)

    span = time.monotonic() - start
    with lock:
        c2, cbad = counts["200"], counts["non200"]
    print(f"\n{'='*70}")
    print(f"  NATIJA — {span:.0f}s ishladi ({span/60:.1f} daq)")
    print(f"{'='*70}")
    print(f"  200 OK        : {c2}")
    print(f"  non-200 (bad) : {cbad}")
    if first_bad_sec["v"] is not None:
        print(f"  DEVOR chiqdi  : {first_bad_sec['v']}s da (daqiqa {first_bad_sec['v']//60}) — "
              f"~{first_bad_sec['v']*args.per_sec} so'rovdan keyin")
    else:
        print(f"  DEVOR         : chiqmadi ({args.max_secs}s ichida) — hammasi 200")

    if captured:
        print(f"\n  ══ USHLANGAN XATO JAVOBLAR ({len(captured)}) — «other» nima ekan ══")
        # Status kodlar taqsimoti.
        by_status = {}
        for c in captured:
            key = c["status"] if c["status"] is not None else f"EXC:{c['body'][:40]}"
            by_status[key] = by_status.get(key, 0) + 1
        print(f"  Turlari: {by_status}\n")
        # Birinchi 3 tasini TO'LIQ ko'rsatamiz (status + tana + muhim header).
        for c in captured[:3]:
            print(f"   • sek {c['sec']}  [{c['kind']}]  STATUS = {c['status']}")
            if c["headers"]:
                interesting = {k: v for k, v in c["headers"].items()
                               if k.lower() in ("www-authenticate", "retry-after", "date",
                                                "server", "content-type", "x-request-id",
                                                "cf-ray", "connection", "x-ratelimit-remaining",
                                                "x-ratelimit-burst-capacity")}
                print(f"     header (muhim): {interesting}")
                print(f"     header (to'liq): {c['headers']}")
            print(f"     TANA: {c['body']!r}\n")
    print(f"[probe] tugadi — read-only, hech narsa band qilinmadi.")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
