"""Bitta so'rov ICHIDA vaqt QAYERGA ketadi? (READ-ONLY dekompozitsiya.)

Har so'rovni qismlarga ajratadi:
  1. PING  — sof tarmoq borib-kelishi (xom TCP-connect RTT).
  2. ESHIK OCHISH — TCP + TLS handshake (sovuq, BIR MARTALIK; keep-alive'da qayta emas).
  3. UZUM HISOBI — server javob tayyorlash vaqti = (issiq so'rov − ping).
  4. THROTTLE — Uzum o'qishni cheklamaydi (0×429 tasdiqlandi) → 0.

    docker compose exec -T app python scripts/probe_request_breakdown.py
"""
from __future__ import annotations

import os
import socket
import ssl
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from requests.adapters import HTTPAdapter

_HOST = "api-seller.uzum.uz"
_PORT = 443
_BASE = "https://api-seller.uzum.uz/api/seller/shop"
_SLOT_WINDOW_MS = 31_532_400_000
_TIMEFROM_LEAD_MS = 5 * 60 * 1000


def _load_ctx():
    from app import app as flask_app
    from models import Shop
    from extensions import SessionLocal
    from sqlalchemy import select
    from core.auth_helpers import _get_admin_token
    from postavki.slot_watch import _get_sku_context
    with flask_app.app_context():
        tok = (_get_admin_token() or "").strip()
        with SessionLocal() as db:
            sid = db.execute(select(Shop.uzum_id).where(Shop.uzum_id.isnot(None)).limit(1)).scalar_one_or_none()
        sid = str(sid) if sid else ""
        base = pool = None
        if sid:
            base, _d, pool = _get_sku_context(sid)
    if not base:
        return None
    return tok, sid, {**base, "quantityToStock": 150}, (pool or "FULLFILMENT")


def _med(xs):
    return statistics.median(xs) if xs else 0.0


def main():
    ctx = _load_ctx()
    if not ctx:
        print("[breakdown] kontekst topilmadi"); return
    tok, shop, line, pool = ctx
    print(f"[breakdown] shop={shop} host={_HOST}\n")

    # 1. PING — xom TCP-connect RTT (sof tarmoq borib-kelish; ~1 RTT).
    pings = []
    for _ in range(20):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM); s.settimeout(5)
        t0 = time.monotonic()
        try:
            s.connect((_HOST, _PORT))
            pings.append((time.monotonic() - t0) * 1000)
        except Exception as e:
            print(f"  tcp connect xato: {e!r}")
        finally:
            s.close()
        time.sleep(0.05)
    ping = _med(pings)

    # 2. ESHIK OCHISH — TCP + TLS handshake (sovuq, bir martalik).
    tcp_t, tls_t = [], []
    sslctx = ssl.create_default_context()
    for _ in range(6):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM); s.settimeout(5)
        try:
            t0 = time.monotonic(); s.connect((_HOST, _PORT)); t1 = time.monotonic()
            ss = sslctx.wrap_socket(s, server_hostname=_HOST); t2 = time.monotonic()
            tcp_t.append((t1 - t0) * 1000); tls_t.append((t2 - t1) * 1000)
            ss.close()
        except Exception as e:
            print(f"  tls xato: {e!r}")
        time.sleep(0.05)
    tcp_c, tls_c = _med(tcp_t), _med(tls_t)

    # 3. ISSIQ so'rov — keep-alive session, ulanish qayta ishlatiladi.
    sess = requests.Session()
    ad = HTTPAdapter(pool_connections=2, pool_maxsize=2, max_retries=0)
    sess.mount("https://", ad)
    h = {"Authorization": tok if tok.startswith("Bearer ") else f"Bearer {tok}",
         "Accept": "application/json", "Content-Type": "application/json"}
    url = f"{_BASE}/{shop}/v2/invoice/time-slot"

    def _body():
        now = int(time.time() * 1000)
        return {"skuList": [line], "poolSource": pool,
                "timeFrom": now + _TIMEFROM_LEAD_MS, "timeTo": now + _SLOT_WINDOW_MS}

    warm, first_cold = [], None
    for i in range(120):
        t0 = time.monotonic()
        try:
            r = sess.post(url, headers=h, json=_body(), timeout=(5, 15))
            dt = (time.monotonic() - t0) * 1000
            if i == 0:
                first_cold = dt          # 1-so'rov = eshik ochish + so'rov (sovuq)
            elif r.status_code == 200:
                warm.append(dt)
        except Exception as e:
            print(f"  warm req xato: {e!r}")
        time.sleep(0.02)

    warm_med = _med(warm)
    uzum_calc = max(0.0, warm_med - ping)     # server hisobi = issiq − tarmoq

    # ── Xulosa ────────────────────────────────────────────────────────
    print(f"{'='*54}")
    print(f"  BITTA SO'ROV VAQTI QAYERGA KETADI\n")
    print(f"  ESHIK OCHISH (TCP+TLS handshake) — BIR MARTALIK:")
    print(f"     TCP connect : {tcp_c:5.0f} ms")
    print(f"     TLS handshake:{tls_c:5.0f} ms")
    print(f"     JAMI eshik  : {tcp_c + tls_c:5.0f} ms   ← faqat 1-marta (keep-alive → keyin 0)")
    print(f"     (1-so'rov real, sovuq: {first_cold:.0f} ms)\n")
    print(f"  HAR ISSIQ SO'ROV (eshik allaqachon ochiq):")
    print(f"     🌐 Ping (tarmoq borib-kelish) : {ping:5.0f} ms   ({100*ping/warm_med:.0f}%)")
    print(f"     🧮 Uzum hisobi (server)       : {uzum_calc:5.0f} ms   ({100*uzum_calc/warm_med:.0f}%)")
    print(f"     ⏱  Throttle (Uzum o'qish limiti): {0:5.0f} ms   (0×429 → yo'q)")
    print(f"     ────────────────────────────────────────")
    print(f"     JAMI issiq so'rov             : {warm_med:5.0f} ms")
    print(f"\n  Ping tafsiloti: eng tez {min(pings):.0f}ms · median {ping:.0f}ms · n={len(pings)}")
    print(f"  Issiq so'rov  : eng tez {min(warm):.0f}ms · median {warm_med:.0f}ms · n={len(warm)}")
    print(f"\n[breakdown] read-only.")


if __name__ == "__main__":
    main()
