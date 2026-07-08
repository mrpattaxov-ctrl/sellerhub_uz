"""Time-slot parallel-limit TASDIQLOVCHI probe — ~60 devor kim tomondan?

Birinchi gradient (probe_timeslot_gradient.py) shuni topdi: Uzum HECH 429
qaytarmaydi; ~60 parallelda devor CHIQADI, lekin u connection-timeout
(HTTPSConnectionPool 20s), 429 EMAS. Savol: bu devor UZUM tomonidami (per-IP
ulanish cheki) yoki BIZNING konteyner o'zi to'yib qolganmi (har so'rovga yangi
TLS handshake + ephemeral port bosimi)?

Ikki farqlovchi o'zgartirish:
  1. YAGONA `requests.Session` + katta ulanish pool (keep-alive) → mijoz
     tomonidagi handshake/port bosimi yo'qoladi. Agar ~60 devor QOLSA →
     UZUM-tomon (per-IP cap). Agar YO'QOLSA → bizning mijoz to'ygan edi.
  2. connect-timeout va read-timeout AJRATILADI (5s, 20s):
       - connect-timeout → Uzum SYN'ni tashlayapti (server per-IP ulanish cheki).
       - read-timeout    → Uzum ulanishni QABUL qildi-yu, javob sekin
         (server yuk/latency throttle) — bu ham server-tomon.

READ-ONLY. Konteyner ichida:
    docker compose cp scripts/probe_timeslot_confirm.py app:/app/scripts/probe_timeslot_confirm.py
    docker compose exec app python scripts/probe_timeslot_confirm.py --levels 40,50,60,70,80,100
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from requests.adapters import HTTPAdapter
from urllib3.exceptions import ConnectTimeoutError, ReadTimeoutError

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
    s.mount("https://", ad)
    s.mount("http://", ad)
    return s


def _classify(exc: Exception) -> str:
    """Xato turini ajratadi: connect / read / conn-pool / other."""
    s = repr(exc)
    # requests o'raydigan urllib3 sabablarini tekshiramiz.
    cause = getattr(exc, "args", [None])
    for c in cause:
        if isinstance(c, (ConnectTimeoutError,)):
            return "connect-timeout"
        if isinstance(c, (ReadTimeoutError,)):
            return "read-timeout"
    if "ConnectTimeout" in s or "Connection to" in s and "timed out" in s:
        return "connect-timeout"
    if "ReadTimeout" in s or "Read timed out" in s:
        return "read-timeout"
    if "Max retries" in s or "Connection refused" in s or "RemoteDisconnected" in s:
        return "conn-refused/reset"
    if "ConnectionError" in s:
        return "conn-error"
    return "other"


def _one(sess, url, headers, body, connect_to, read_to) -> dict:
    t0 = time.monotonic()
    try:
        r = sess.post(url, headers=headers, json=body, timeout=(connect_to, read_to))
        return {"status": r.status_code, "ms": (time.monotonic() - t0) * 1000}
    except Exception as e:
        return {"status": -1, "ms": (time.monotonic() - t0) * 1000,
                "kind": _classify(e)}


def _wave(sess, url, headers, body, n, connect_to, read_to) -> dict:
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=n) as pool:
        futs = [pool.submit(_one, sess, url, headers, body, connect_to, read_to)
                for _ in range(n)]
        res = [f.result() for f in as_completed(futs)]
    span = time.monotonic() - t0
    ok = sum(1 for x in res if x["status"] == 200)
    rl = sum(1 for x in res if x["status"] == 429)
    kinds: dict[str, int] = {}
    for x in res:
        if x.get("kind"):
            kinds[x["kind"]] = kinds.get(x["kind"], 0) + 1
    return {"n": n, "ok": ok, "rl": rl, "span": span, "kinds": kinds}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--levels", default="30,40,50,55,60,65,70,80,100")
    ap.add_argument("--pause", type=float, default=3.0)
    ap.add_argument("--connect-timeout", type=float, default=5.0)
    ap.add_argument("--read-timeout", type=float, default=20.0)
    args = ap.parse_args()
    levels = [int(x) for x in args.levels.split(",") if x.strip()]

    tok, shop_id, line, pool = _load_context()
    if not (tok and shop_id and line):
        print("[probe] kontekst topilmadi (token/shop/sku)"); return

    sess = _make_session(pool_size=max(levels) + 10)
    url = f"{_BASE}/{shop_id}/v2/invoice/time-slot"
    now = int(time.time() * 1000)
    body = {"skuList": [line], "poolSource": pool,
            "timeFrom": now + _TIMEFROM_LEAD_MS, "timeTo": now + _SLOT_WINDOW_MS}
    h = {"Authorization": tok if tok.startswith("Bearer ") else f"Bearer {tok}",
         "Accept": "application/json", "Content-Type": "application/json",
         "Origin": "https://seller.uzum.uz", "Referer": "https://seller.uzum.uz/"}

    print(f"[probe] TASDIQ: yagona Session + pool={max(levels)+10} keep-alive, "
          f"connect_to={args.connect_timeout}s read_to={args.read_timeout}s")
    print(f"[probe] shop={shop_id} sku={line.get('skuId')} pool={pool}\n")
    print(f"   {'N':>4}  {'200':>4}  {'429':>4}  {'span':>7}  xato-turlari")
    print("   " + "-" * 58)

    for n in levels:
        w = _wave(sess, url, h, body, n, args.connect_timeout, args.read_timeout)
        kinds = "  ".join(f"{k}×{v}" for k, v in w["kinds"].items()) or "—"
        print(f"   {n:>4}  {w['ok']:>4}  {w['rl']:>4}  {w['span']*1000:>6.0f}ms  {kinds}")
        time.sleep(args.pause)

    print("\n[probe] talqin:")
    print("  • pool bilan ham ~60 da timeout → UZUM per-IP ulanish cheki (server-tomon)")
    print("  • connect-timeout ustun → Uzum SYN tashlayapti (aniq ulanish cheki)")
    print("  • read-timeout ustun    → Uzum qabul qildi-yu sekin javob (yuk/latency)")
    print("  • pool bilan devor YO'QOLDI → oldingi ~60 mijoz-tomon handshake bosimi edi")
    print("\n[probe] tugadi — read-only.")


if __name__ == "__main__":
    main()
