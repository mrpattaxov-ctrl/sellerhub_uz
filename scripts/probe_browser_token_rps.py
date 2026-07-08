"""Brauzer-token (portal API) RPS probe — READ-ONLY.

Savol: 1 sekundda brauzer token bilan Uzum portaliga necha so'rov yuborsa
bo'ladi? Bu skript JONLI portal API'ga yengil, FAQAT-O'QIYDIGAN GET'lar yuboradi
va Uzum'ning token-bucket header'larini (Spring Cloud Gateway `X-RateLimit-*`:
replenish-rate / burst-capacity / remaining) o'qib chinakam limitni KO'RSATADI.

Endpoint: GET /api/seller/shop/{id}/v2/invoice/dimensional-groups — eng yengil
read-only portal chaqiruvi (hech narsa yaratmaydi/o'zgartirmaydi).

Bosqichlar:
  1. Header-o'qish: 1 ta sokin so'rov → Uzum ayni bucket o'lchamini header'da beradi.
  2. Sustained: ~3s davomida ketma-ket (serial) so'rov → barqaror RPS.
  3. Burst: 2→4→6→8 parallel → 429 qayerda chiqadi + tiklanish vaqti.

Konteyner ichida ishga tushiriladi (prod token + egress IP):
    docker compose exec app python scripts/probe_browser_token_rps.py
"""
from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

_BASE = "https://api-seller.uzum.uz/api/seller/shop"

# Spring Cloud Gateway RequestRateLimiter header'lari (portal shu shaklda beradi)
_RATE_KEYS = (
    "x-ratelimit-remaining", "x-ratelimit-burst-capacity",
    "x-ratelimit-replenish-rate", "x-ratelimit-requested-tokens",
    "retry-after", "ratelimit-limit", "ratelimit-remaining", "ratelimit-reset",
)


def _load_token_and_shop() -> tuple[str, str]:
    from app import app as flask_app
    from models import Shop
    from extensions import SessionLocal
    from sqlalchemy import select
    from core.auth_helpers import _get_admin_token
    with flask_app.app_context():
        tok = (_get_admin_token() or "").strip()
        with SessionLocal() as db:
            shop_id = db.execute(
                select(Shop.uzum_id).where(Shop.uzum_id.isnot(None)).limit(1)
            ).scalar_one_or_none()
    return tok, (str(shop_id) if shop_id else "")


def _headers(tok: str) -> dict:
    auth = tok if tok.startswith("Bearer ") else f"Bearer {tok}"
    return {
        "Authorization": auth,
        "Accept": "application/json",
        "Origin": "https://seller.uzum.uz",
        "Referer": "https://seller.uzum.uz/",
    }


def _rate_headers(resp: requests.Response) -> dict:
    return {k: v for k, v in resp.headers.items() if k.lower() in _RATE_KEYS}


def _one(url: str, headers: dict) -> dict:
    t0 = time.monotonic()
    try:
        r = requests.get(url, headers=headers, timeout=15)
        return {"status": r.status_code, "ms": (time.monotonic() - t0) * 1000,
                "rate": _rate_headers(r)}
    except Exception as e:
        return {"status": -1, "ms": (time.monotonic() - t0) * 1000, "err": str(e)}


def main() -> None:
    tok, shop_id = _load_token_and_shop()
    if not tok:
        print("[probe] brauzer token topilmadi (admin api_key)"); return
    if not shop_id:
        print("[probe] do'kon uzum_id topilmadi"); return
    url = f"{_BASE}/{shop_id}/v2/invoice/dimensional-groups"
    h = _headers(tok)
    print(f"[probe] token={tok[:10]}..  shop={shop_id}")
    print(f"[probe] endpoint=GET .../{shop_id}/v2/invoice/dimensional-groups (read-only)\n")

    # ── 1. Header o'qish ─────────────────────────────────────────────
    print("── 1. Bitta sokin so'rov — Uzum bucket header'lari ──")
    r = _one(url, h)
    print(f"   HTTP {r['status']}  {r['ms']:.0f}ms")
    if r.get("rate"):
        for k, v in r["rate"].items():
            print(f"     {k}: {v}")
        print("   → replenish-rate = barqaror tok/sek, burst-capacity = bir lahzalik maks.")
    else:
        print("   (rate-limit header yo'q — limit yashirin; empirik o'lchaymiz)")
    time.sleep(2)

    # ── 2. Sustained serial RPS ──────────────────────────────────────
    print("\n── 2. Barqaror RPS — 3s ketma-ket (serial), to'xtovsiz ──")
    end = None
    n_ok = n_429 = n_err = 0
    t_start = time.monotonic()
    first_429_at = None
    cnt = 0
    while time.monotonic() - t_start < 3.0:
        r = _one(url, h)
        cnt += 1
        if r["status"] == 200:
            n_ok += 1
        elif r["status"] == 429:
            n_429 += 1
            if first_429_at is None:
                first_429_at = time.monotonic() - t_start
        else:
            n_err += 1
    elapsed = time.monotonic() - t_start
    print(f"   {cnt} so'rov / {elapsed:.1f}s  →  200×{n_ok}  429×{n_429}  err×{n_err}")
    print(f"   ≈ {n_ok/elapsed:.1f} muvaffaqiyatli so'rov/sek (serial)")
    if first_429_at:
        print(f"   birinchi 429: {first_429_at:.2f}s da ({n_ok} ta 200 dan keyin)")
    time.sleep(4)  # bucket tiklansin

    # ── 3. Burst (parallel) threshold ────────────────────────────────
    print("\n── 3. Burst — bir lahzada N parallel so'rov ──")
    for workers in (8, 16, 24, 32, 48, 64):
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(_one, url, h) for _ in range(workers)]
            res = [f.result() for f in as_completed(futs)]
        ok = sum(1 for x in res if x["status"] == 200)
        rl = sum(1 for x in res if x["status"] == 429)
        er = sum(1 for x in res if x["status"] not in (200, 429))
        rate = next((x["rate"] for x in res if x.get("rate")), {})
        tag = "  ← 429!" if rl else ""
        print(f"   {workers:>2} parallel  →  200×{ok}  429×{rl}  err×{er}{tag}"
              + (f"   {rate}" if rate else ""))
        if rl:
            # tiklanish vaqtini o'lchaymiz
            t0 = time.monotonic()
            for _ in range(20):
                time.sleep(1.0)
                if _one(url, h)["status"] == 200:
                    print(f"      tiklandi ~{time.monotonic()-t0:.0f}s da")
                    break
            break
        time.sleep(3)

    # ── 4. 10 000 gacha SEKIN ramp — 429 chiqsa to'xtaydi ────────────
    time.sleep(4)
    print("\n── 4. 10 000 so'rovgacha sekin ramp (429 chiqsa STOP) ──")
    TARGET = 10_000
    # to'lqin o'lchamlari sekin oshadi, eng kattasi takrorlanadi
    waves = [25, 50, 75, 100, 150, 200]
    total = ok_all = rl_all = er_all = 0
    wave_i = 0
    hit_429 = False
    while total < TARGET and not hit_429:
        w = waves[min(wave_i, len(waves) - 1)]
        w = min(w, TARGET - total)
        with ThreadPoolExecutor(max_workers=min(w, 100)) as pool:
            t0 = time.monotonic()
            futs = [pool.submit(_one, url, h) for _ in range(w)]
            res = [f.result() for f in as_completed(futs)]
            span = time.monotonic() - t0
        ok = sum(1 for x in res if x["status"] == 200)
        rl = sum(1 for x in res if x["status"] == 429)
        er = sum(1 for x in res if x["status"] not in (200, 429))
        total += w; ok_all += ok; rl_all += rl; er_all += er
        rate = next((x["rate"] for x in res if x.get("rate")), {})
        tag = "  ← 429!" if rl else ""
        print(f"   to'lqin {w:>3} ({span:.1f}s, {w/span:.0f}/s)  jami={total:>5}  "
              f"200×{ok} 429×{rl} err×{er}{tag}"
              + (f"  {rate}" if rate else ""))
        if rl:
            hit_429 = True
            t0 = time.monotonic()
            for _ in range(30):
                time.sleep(1.0)
                if _one(url, h)["status"] == 200:
                    print(f"      → tiklandi ~{time.monotonic()-t0:.0f}s da")
                    break
            break
        wave_i += 1
        time.sleep(1.5)  # «sekin» — to'lqinlar orasida nafas

    print(f"\n   YAKUN: jami={total}  200×{ok_all}  429×{rl_all}  err×{er_all}")
    if not hit_429:
        print(f"   → 10 000 so'rov, 0× 429. O'qish endpointida amaliy chegara YO'Q.")

    print("\n[probe] tugadi — read-only, hech narsa o'zgartirilmadi.")


if __name__ == "__main__":
    main()
