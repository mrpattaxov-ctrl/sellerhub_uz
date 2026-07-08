"""403 sababini USHLASH — barqaror yuk ostida non-200 javob tanasi + header'lari.

Sustained sinovda 6000 dan 3 tasi HTTP 403 qaytardi (0.05%, tez 0.1s — timeout
EMAS). Sababi noma'lum edi (probe tanani tashlagan). Bu skript aynan shu yukni
qaytaradi (100/sek × Ns) va HAR non-200 javobning: status, tana (~300 belgi),
va muhim header'larini (www-authenticate, retry-after, x-ratelimit-*, x-request-id,
date) YOZADI — shunda 403 nega chiqqani ANIQ ko'rinadi (token/auth vs throttle vs
gateway blip). READ-ONLY.

    docker compose cp scripts/probe_timeslot_403capture.py app:/app/scripts/probe_timeslot_403capture.py
    docker compose exec app python scripts/probe_timeslot_403capture.py --seconds 60 --per-sec 100
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

_INTERESTING_HEADERS = (
    "www-authenticate", "retry-after", "date", "x-request-id", "x-trace-id",
    "cf-ray", "server", "content-type",
    "x-ratelimit-remaining", "x-ratelimit-burst-capacity", "x-ratelimit-replenish-rate",
)

_lock = threading.Lock()
_captured: list[dict] = []
_MAX_CAPTURE = 15


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


def _one(sess, url, headers, body, sec) -> str:
    try:
        r = sess.post(url, headers=headers, json=body, timeout=(5, 15))
        if r.status_code != 200:
            with _lock:
                if len(_captured) < _MAX_CAPTURE:
                    hdrs = {k: v for k, v in r.headers.items()
                            if k.lower() in _INTERESTING_HEADERS}
                    _captured.append({
                        "sec": sec, "status": r.status_code,
                        "body": (r.text or "")[:300], "headers": hdrs,
                    })
        return f"http-{r.status_code}"
    except Exception as e:
        return type(e).__name__


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--per-sec", type=int, default=100)
    args = ap.parse_args()

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

    print(f"[probe] 403-USHLASH: {args.per_sec}/sek × {args.seconds}s, "
          f"token={tok[:12]}.. shop={shop_id}\n")

    sess = _make_session(pool_size=args.per_sec + 20)
    futures = []
    t_start = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.per_sec * 3) as ex:
        for sec in range(args.seconds):
            dt = (t_start + sec) - time.monotonic()
            if dt > 0:
                time.sleep(dt)
            for _ in range(args.per_sec):
                futures.append(ex.submit(_one, sess, url, h, body, sec))
            sys.stdout.write("."); sys.stdout.flush()
        print("\n[probe] javoblar yig'ilmoqda...")
        counts: dict[str, int] = {}
        for f in futures:
            k = f.result()
            counts[k] = counts.get(k, 0) + 1

    n = len(futures)
    print(f"\n── Natija ({n} so'rov) ──")
    for k in sorted(counts, key=lambda x: -counts[x]):
        print(f"   {k:<18} {counts[k]}")

    print(f"\n── Ushlangan non-200 javoblar ({len(_captured)}) ──")
    if not _captured:
        print("   (bu safar bironta non-200 chiqmadi)")
    for c in _captured:
        print(f"\n   • sek {c['sec']}  HTTP {c['status']}")
        print(f"     headers: {c['headers']}")
        print(f"     body:    {c['body']!r}")
    print(f"\n[probe] tugadi — read-only.")


if __name__ == "__main__":
    main()
