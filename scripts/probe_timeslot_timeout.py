"""Time-slot TIMEOUT-vaqti o'lchovi — «to'yган rejimda» so'rov qancha osiladi?

Gradient probe'da ~60+ parallelda xato chiqqan, lekin span'lar ~20.2s edi —
CHUNKI timeout=20s QO'LDA qo'yilgan edi (tabiiy emas). Bu skript XOM (pool'siz)
yuqori-N burst'ni QAYTA tiklaydi, AMMO saxiy timeout bilan (connect=10s,
read=45s) — shunda har so'rov TABIIY qancha osilishini ko'ramiz:
  • tez xato (~<3s, connection-reset/refused) — server/mijoz darhol rad etadi
  • sekin xato (read-timeout gacha) — ulanish osilib qoladi
  • sekin 200 — javob keldi-yu kechikib

Har so'rovning elapsed vaqtini yig'ib, natija bo'yicha (200 / connect-timeout /
read-timeout / reset) min/median/max ko'rsatadi. READ-ONLY.

    docker compose cp scripts/probe_timeslot_timeout.py app:/app/scripts/probe_timeslot_timeout.py
    docker compose exec app python scripts/probe_timeslot_timeout.py --levels 80,100
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

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


def _classify(exc: Exception) -> str:
    s = repr(exc)
    if "ConnectTimeout" in s:
        return "connect-timeout"
    if "ReadTimeout" in s or "Read timed out" in s:
        return "read-timeout"
    if "Connection refused" in s:
        return "conn-refused"
    if "reset by peer" in s or "RemoteDisconnected" in s or "ConnectionResetError" in s:
        return "conn-reset"
    if "Max retries" in s:
        return "max-retries"
    if "ConnectionError" in s:
        return "conn-error"
    return "other"


def _one(url, headers, body, connect_to, read_to) -> tuple[str, float]:
    """XOM POST (har safar yangi ulanish). -> (outcome, elapsed_s)."""
    t0 = time.monotonic()
    try:
        r = requests.post(url, headers=headers, json=body, timeout=(connect_to, read_to))
        dt = time.monotonic() - t0
        return (f"http-{r.status_code}", dt)
    except Exception as e:
        return (_classify(e), time.monotonic() - t0)


def _summarize(label: str, times: list[float]) -> str:
    if not times:
        return ""
    med = statistics.median(times)
    return (f"      {label:<16} n={len(times):>3}  "
            f"min={min(times):5.1f}s  med={med:5.1f}s  max={max(times):5.1f}s")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--levels", default="80,100")
    ap.add_argument("--connect-timeout", type=float, default=10.0)
    ap.add_argument("--read-timeout", type=float, default=45.0)
    ap.add_argument("--pause", type=float, default=4.0)
    args = ap.parse_args()
    levels = [int(x) for x in args.levels.split(",") if x.strip()]

    tok, shop_id, line, pool = _load_context()
    if not (tok and shop_id and line):
        print("[probe] kontekst topilmadi"); return

    url = f"{_BASE}/{shop_id}/v2/invoice/time-slot"
    now = int(time.time() * 1000)
    body = {"skuList": [line], "poolSource": pool,
            "timeFrom": now + _TIMEFROM_LEAD_MS, "timeTo": now + _SLOT_WINDOW_MS}
    h = {"Authorization": tok if tok.startswith("Bearer ") else f"Bearer {tok}",
         "Accept": "application/json", "Content-Type": "application/json",
         "Origin": "https://seller.uzum.uz", "Referer": "https://seller.uzum.uz/"}

    print(f"[probe] XOM (pool'siz) burst, saxiy timeout: connect={args.connect_timeout}s "
          f"read={args.read_timeout}s")
    print(f"[probe] shop={shop_id} sku={line.get('skuId')} — «to'yган rejim»da tabiiy vaqt\n")

    for n in levels:
        t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=n) as pool_ex:
            futs = [pool_ex.submit(_one, url, h, body, args.connect_timeout, args.read_timeout)
                    for _ in range(n)]
            res = [f.result() for f in as_completed(futs)]
        span = time.monotonic() - t0

        by_outcome: dict[str, list[float]] = {}
        for outcome, dt in res:
            by_outcome.setdefault(outcome, []).append(dt)

        ok = sum(len(v) for k, v in by_outcome.items() if k == "http-200")
        fail = n - ok
        print(f"   ── N={n} parallel  →  200×{ok}  xato×{fail}  (wave {span:.1f}s) ──")
        for outcome in sorted(by_outcome, key=lambda k: -len(by_outcome[k])):
            print(_summarize(outcome, by_outcome[outcome]))
        print()
        time.sleep(args.pause)

    print("[probe] talqin: 'read-timeout med' ≈ ulanish tabiiy qancha osiladi; "
          "'conn-reset min' ≈ darhol rad. Bu XOM yo'l — real ilova pooled Session "
          "ishlatib bu rejimga umuman tushmaydi.")
    print("[probe] tugadi — read-only.")


if __name__ == "__main__":
    main()
