"""Time-slot (portal API) PARALLEL-RPS gradient probe — READ-ONLY.

Savol: taymslot-tanlagich (autoslot) fon-so'rovi bilan Uzum portaliga BIR
LAHZADA necha PARALLEL so'rov yuborsa bo'ladi — Uzum qayerda 429 bilan
to'xtatadi? Bu skript 1 → 100 gacha SEKIN gradient bo'yicha har darajada
AYNAN shu sondagi bir vaqtli so'rovni otadi va Uzum'ning Spring Cloud Gateway
token-bucket header'larini (`x-ratelimit-*`: replenish-rate / burst-capacity /
remaining) o'qib chinakam chegarani KO'RSATADI.

Endpoint: POST /api/seller/shop/{id}/v2/invoice/time-slot — AYNAN autoslot
slot-kuzatuvi otadigan so'rov (`postavki.client.get_time_slots`). FAQAT-O'QIYDI:
bo'sh slotlar ro'yxatini qaytaradi, hech narsa yaratmaydi/o'zgartirmaydi va
`time-slot/set` 3-martalik budjetiga TEGMAYDI.

⚠️ JONLI admin brauzer-tokenini ishlatadi — autoslot booking'i ham shu token.
To'liq 100-gacha burst tokenni bir muddat sovutishi mumkin. Autoslot sokin
paytda ishga tushiring.

Muhim: xom `requests.post` ishlatadi (ilova `http_json`'i 429'ni RETRY qilib
YASHIRADI — probe uchun yaramaydi). Har daraja mustaqil burst: to'lqinlar
orasida qisqa pauza → bucket tiklanadi → har daraja «N ni bir lahzada» toza
sinaydi.

Konteyner ichida ishga tushiriladi (prod egress IP + token):
    docker compose exec app python scripts/probe_timeslot_gradient.py
    docker compose exec app python scripts/probe_timeslot_gradient.py --max 60 --repeat 2
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

_BASE = "https://api-seller.uzum.uz/api/seller/shop"

# time-slot qidiruv oynasi — client.get_time_slots bilan AYNAN bir xil konstanta.
_SLOT_WINDOW_MS = 31_532_400_000
_TIMEFROM_LEAD_MS = 5 * 60 * 1000

# Spring Cloud Gateway RequestRateLimiter header'lari (portal shu shaklda beradi).
_RATE_KEYS = (
    "x-ratelimit-remaining", "x-ratelimit-burst-capacity",
    "x-ratelimit-replenish-rate", "x-ratelimit-requested-tokens",
    "retry-after", "ratelimit-limit", "ratelimit-remaining", "ratelimit-reset",
)


def _load_context() -> tuple[str, str, dict, str]:
    """(token, shop_uzum_id, sku_line, pool_source) — autoslot bilan bir manba.

    SKU + ombor + pool `slot_watch._get_sku_context` orqali AYNAN watcher
    tanlaydigan tarzda olinadi (eng ko'p zaxirali real SKU)."""
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
        base, _dim, pool = (None, None, None)
        if shop_id:
            base, _dim, pool = _get_sku_context(shop_id)
    if not base:
        return tok, shop_id, {}, ""
    line = {**base, "quantityToStock": 150}
    return tok, shop_id, line, (pool or "FULLFILMENT")


def _headers(tok: str) -> dict:
    auth = tok if tok.startswith("Bearer ") else f"Bearer {tok}"
    return {
        "Authorization": auth,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://seller.uzum.uz",
        "Referer": "https://seller.uzum.uz/",
    }


def _rate_headers(resp: requests.Response) -> dict:
    return {k.lower(): v for k, v in resp.headers.items() if k.lower() in _RATE_KEYS}


def _one(url: str, headers: dict, body: dict) -> dict:
    """Bitta XOM time-slot POST (retry YO'Q — 429 ko'rinadi)."""
    t0 = time.monotonic()
    try:
        r = requests.post(url, headers=headers, json=body, timeout=20)
        return {"status": r.status_code, "ms": (time.monotonic() - t0) * 1000,
                "rate": _rate_headers(r)}
    except Exception as e:
        return {"status": -1, "ms": (time.monotonic() - t0) * 1000, "err": str(e)}


def _wave(url: str, headers: dict, body: dict, n: int) -> dict:
    """`n` ta so'rovni BIR LAHZADA (n parallel worker) otadi."""
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=n) as pool:
        futs = [pool.submit(_one, url, headers, body) for _ in range(n)]
        res = [f.result() for f in as_completed(futs)]
    span = time.monotonic() - t0
    ok = sum(1 for x in res if x["status"] == 200)
    rl = sum(1 for x in res if x["status"] == 429)
    er = sum(1 for x in res if x["status"] not in (200, 429))
    rate = next((x["rate"] for x in res if x.get("rate")), {})
    errs = [x.get("err") for x in res if x.get("err")]
    return {"n": n, "ok": ok, "rl": rl, "er": er, "span": span,
            "rate": rate, "err_sample": errs[0] if errs else None}


def _fmt_rate(rate: dict) -> str:
    if not rate:
        return ""
    rem = rate.get("x-ratelimit-remaining")
    burst = rate.get("x-ratelimit-burst-capacity")
    repl = rate.get("x-ratelimit-replenish-rate")
    ra = rate.get("retry-after")
    parts = []
    if rem is not None:
        parts.append(f"rem={rem}")
    if burst is not None:
        parts.append(f"burst={burst}")
    if repl is not None:
        parts.append(f"repl={repl}/s")
    if ra is not None:
        parts.append(f"retry-after={ra}")
    return "  " + " ".join(parts) if parts else ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=100, help="eng katta parallel daraja (default 100)")
    ap.add_argument("--step", type=int, default=1, help="daraja qadami (default 1 → 1,2,3,...)")
    ap.add_argument("--pause", type=float, default=1.5, help="to'lqinlar orasi pauza s (bucket tiklansin)")
    ap.add_argument("--repeat", type=int, default=1, help="429 chiqqach har darajani necha marta takror o'lchash")
    ap.add_argument("--stop-on-429", action="store_true", help="birinchi 429'da to'xta (default: 100 gacha to'liq)")
    args = ap.parse_args()

    tok, shop_id, line, pool = _load_context()
    if not tok:
        print("[probe] brauzer token topilmadi (admin api_key)"); return
    if not shop_id:
        print("[probe] do'kon uzum_id topilmadi"); return
    if not line:
        print("[probe] real SKU topilmadi (restock_skus bo'sh?) — probe qila olmaydi"); return

    url = f"{_BASE}/{shop_id}/v2/invoice/time-slot"
    now = int(time.time() * 1000)
    body = {
        "skuList": [line],
        "poolSource": pool,
        "timeFrom": now + _TIMEFROM_LEAD_MS,
        "timeTo": now + _SLOT_WINDOW_MS,
    }
    h = _headers(tok)

    print(f"[probe] token={tok[:10]}..  shop={shop_id}  sku={line.get('skuId')}  pool={pool}")
    print(f"[probe] endpoint=POST .../{shop_id}/v2/invoice/time-slot  (autoslot AYNAN shuni otadi, read-only)")
    print(f"[probe] gradient 1→{args.max} (qadam {args.step}), har daraja = N parallel; "
          f"pauza {args.pause}s; {'birinchi 429da STOP' if args.stop_on_429 else '100 gacha TO`LIQ sweep'}\n")

    # ── 1. Bitta sokin so'rov — Uzum bucket header'lari ──────────────
    print("── 1. Bitta sokin so'rov — Uzum bucket header'lari ──")
    r0 = _one(url, h, body)
    print(f"   HTTP {r0['status']}  {r0['ms']:.0f}ms{_fmt_rate(r0.get('rate') or {})}")
    if r0.get("rate"):
        print("   → burst-capacity = bir lahzalik maks parallel; replenish-rate = barqaror tok/sek.")
    else:
        print("   (rate-limit header yo'q — chegara yashirin; empirik gradient ko'rsatadi)")
    if r0.get("err"):
        print(f"   ⚠️ birinchi so'rov xato: {r0['err']}\n   token/SKU/pool ni tekshiring, to'xtadim."); return
    time.sleep(3)

    # ── 2. Gradient sweep ────────────────────────────────────────────
    print("\n── 2. Parallel gradient — har daraja N ni bir lahzada otadi ──")
    print(f"   {'N':>4}  {'200':>4}  {'429':>4}  {'err':>4}  {'wave':>7}  {'req/s':>6}  bucket")
    print("   " + "-" * 62)

    first_429 = None
    last_all_ok = 0
    levels = list(range(args.step, args.max + 1, args.step))
    if levels and levels[0] != 1:
        levels = [1] + levels  # 1 dan boshla

    for n in levels:
        reps = args.repeat if (first_429 is not None) else 1
        for _rep in range(reps):
            w = _wave(url, h, body, n)
            rps = w["n"] / w["span"] if w["span"] > 0 else 0
            tag = ""
            if w["rl"]:
                tag = "  ← 429!"
                if first_429 is None:
                    first_429 = n
            elif w["er"]:
                tag = f"  ← err ({w['err_sample'] or '?'})"[:40]
            if w["rl"] == 0 and w["er"] == 0:
                last_all_ok = n
            print(f"   {n:>4}  {w['ok']:>4}  {w['rl']:>4}  {w['er']:>4}  "
                  f"{w['span']*1000:>6.0f}ms  {rps:>6.0f}{_fmt_rate(w['rate'])}{tag}")
            time.sleep(args.pause)

        if first_429 is not None and args.stop_on_429:
            break

    # ── 3. Tiklanish vaqti (agar 429 bo'lgan bo'lsa) ─────────────────
    if first_429 is not None:
        print(f"\n── 3. Tiklanish — 429 dan keyin qachon yana 200? ──")
        t0 = time.monotonic()
        recovered = None
        for _ in range(30):
            time.sleep(1.0)
            if _one(url, h, body)["status"] == 200:
                recovered = time.monotonic() - t0
                break
        if recovered is not None:
            print(f"   tiklandi ~{recovered:.0f}s da")
        else:
            print("   30s ichida tiklanmadi — kunlik kvota yoki uzoqroq oyna bo'lishi mumkin")

    # ── 4. Xulosa ────────────────────────────────────────────────────
    print("\n── YAKUN ──")
    print(f"   eng katta TOZA burst (0×429): {last_all_ok} parallel")
    if first_429 is not None:
        print(f"   birinchi 429 chiqqan daraja:   {first_429} parallel")
        print(f"   → xavfsiz parallel chegara ≈ {last_all_ok} (undan yuqorida Uzum to'xtatadi)")
    else:
        print(f"   {args.max} parallelgacha 0×429 — bu endpointda amaliy parallel chegara topilmadi")
    if r0.get("rate"):
        print(f"   Uzum header'i aytgan bucket: {_fmt_rate(r0['rate']).strip()}")
        print("   (empirik chegara ≈ burst-capacity bo'lsa, header'ga ishoning — taxminга emas)")
    print("\n[probe] tugadi — read-only, hech narsa yaratilmadi/o'zgartirilmadi.")


if __name__ == "__main__":
    main()
