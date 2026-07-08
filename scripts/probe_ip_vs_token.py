"""IP-mi / token-mi? — Uzum portal WAF (403) va token-bucket (429) shifti probe.

MAQSAD (Abdulaziz 2026-07-07): SellerHub hamma do'konga BITTA admin token +
BITTA IP bilan kiradi. Savol: tezlik oshsa bizni nima to'sadi —
  • token-bucket (429, har token o'z budjeti) — unда KO'P TOKEN yordam beradi;
  • WAF/IP-blok (403, IP darajasi) — unда faqat KO'P IP yordam beradi.

Bu probe ikkalasini AJRATADI va aniq javob beradi.

⚠️ Bu probe ATAYLAB tezlikni oshirib blokni QIDIRADI. Shuning uchun uni PROD
serverida EMAS, alohida IP'da (lokal kompyuter yoki bir martalik VPS) ishga
tushiring — 403 chiqsa faqat O'SHA IP bloklanadi, prod tirik qoladi.

READ-ONLY: faqat GET .../v2/invoice/dimensional-groups (hech narsa
yaratmaydi/o'zgartirmaydi).

── Ishga tushirish (lokal mashina = boshqa IP) ──────────────────────────────
  # 1+ token, 1+ do'kon (vergul bilan; token[i] ↔ shop[i]):
  UZUM_PROBE_TOKENS="Bearer AAA,Bearer BBB" \
  UZUM_PROBE_SHOPS="51948,60231" \
  python scripts/probe_ip_vs_token.py

  • Token'da "Bearer " bo'lmasa avtomatik qo'shiladi (xom token ham bo'ladi).
  • Ikki+ token bersangiz — DIFERENSIAL test ishlaydi (IP-mi/token-mi aniq javob).
  • Bitta token bersangiz — faqat shift o'lchanadi (403=WAF signal, 429=bucket signal).

Prod token'ni qayerdan olaman? Admin DB'dan: `users.api_key` (is_admin=true).
Ikkinchi token uchun boshqa userning `api_key`'sini oling (ular sizni menejer
qilib qo'shgan bo'lsa, o'z tokeni ham portalga kiradi).
"""
from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

_BASE = "https://api-seller.uzum.uz/api/seller/shop"

# Spring Cloud Gateway RequestRateLimiter header'lari (token-bucket'ni ko'rsatadi).
_RATE_KEYS = (
    "x-ratelimit-remaining", "x-ratelimit-burst-capacity",
    "x-ratelimit-replenish-rate", "x-ratelimit-requested-tokens",
    "retry-after", "ratelimit-limit", "ratelimit-remaining", "ratelimit-reset",
)

# Xavfsizlik: bitta bosqichда eng ko'pi shuncha so'rov (cheksiz ramp bo'lmasin).
_MAX_TOTAL = 4000


def _norm(tok: str) -> str:
    tok = tok.strip()
    return tok if tok.startswith("Bearer ") else f"Bearer {tok}"


def _headers(tok: str) -> dict:
    return {
        "Authorization": _norm(tok),
        "Accept": "application/json",
        "Origin": "https://seller.uzum.uz",
        "Referer": "https://seller.uzum.uz/",
    }


def _rate(resp: requests.Response) -> dict:
    return {k: v for k, v in resp.headers.items() if k.lower() in _RATE_KEYS}


def _one(shop: str, tok: str) -> dict:
    """Bitta yengil read-only GET. -> {status, ms, rate}. Exception otmaydi."""
    url = f"{_BASE}/{shop}/v2/invoice/dimensional-groups"
    t0 = time.monotonic()
    try:
        r = requests.get(url, headers=_headers(tok), timeout=15)
        return {"status": r.status_code, "ms": (time.monotonic() - t0) * 1000,
                "rate": _rate(r)}
    except Exception as e:
        return {"status": -1, "ms": (time.monotonic() - t0) * 1000, "err": str(e)[:80]}


def _load() -> list[tuple[str, str]]:
    """(token, shop) juftliklari. Env'dan yoki (ichkarida) DB'dan.
    token[i] ↔ shop[i]; do'kon yetmasa birinchisi qayta ishlatiladi."""
    # (a) FAYLDAN — eng xavfsiz: token chatда/buyruqда ko'rinmaydi. Har qator:
    #     "<token><bo'sh joy><shop>" (shop ixtiyoriy). # bilan boshlangani izoh.
    fpath = os.environ.get("UZUM_PROBE_TOKENS_FILE", "").strip()
    toks: list[str] = []
    shops: list[str] = []
    if fpath and os.path.exists(fpath):
        for line in open(fpath, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            toks.append(parts[0] if not line.startswith("Bearer ") else " ".join(parts[:2]))
            # "Bearer XXX shop" → token=2 so'z, shop=3-chi; "XXX shop" → token=1, shop=2
            rest = parts[2:] if line.startswith("Bearer ") else parts[1:]
            if rest:
                shops.append(rest[0])
    # (b) ENV'dan (vergul bilan) — fallback.
    if not toks:
        toks = [t for t in os.environ.get("UZUM_PROBE_TOKENS", "").split(",") if t.strip()]
    if not shops:
        shops = [s.strip() for s in os.environ.get("UZUM_PROBE_SHOPS", "").split(",") if s.strip()]
    if not toks:
        # Ixtiyoriy DB fallback (faqat konteyner ichida ishlaydi — alohida
        # IP'da UZUM_PROBE_TOKENS ishlating).
        try:
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            from app import app as flask_app
            from models import User, Shop
            from extensions import SessionLocal
            from sqlalchemy import select
            with flask_app.app_context(), SessionLocal() as db:
                toks = [t for (t,) in db.execute(
                    select(User.api_key).where(User.api_key.isnot(None)).limit(4)).all() if t]
                if not shops:
                    shops = [str(s) for (s,) in db.execute(
                        select(Shop.uzum_id).where(Shop.uzum_id.isnot(None)).limit(4)).all()]
            print("[probe] ⚠️ token'lar DB'dan olindi — bu KONTEYNER IP'si! "
                  "Alohida IP uchun UZUM_PROBE_TOKENS ishlating.")
        except Exception as e:
            print(f"[probe] token topilmadi (UZUM_PROBE_TOKENS bering): {e}")
            return []
    if not shops:
        print("[probe] do'kon yo'q (UZUM_PROBE_SHOPS bering).")
        return []
    return [(toks[i], shops[i] if i < len(shops) else shops[0]) for i in range(len(toks))]


def _burst(pairs: list[tuple[str, str]], n_per_token: int) -> dict:
    """Barcha token'lardan bir vaqtda `n_per_token` parallel so'rov (bitta IP).
    -> per-token status yig'indisi + eng yomon holat."""
    jobs = []
    for tok, shop in pairs:
        jobs += [(tok, shop)] * n_per_token
    out = {i: {"200": 0, "403": 0, "429": 0, "err": 0} for i in range(len(pairs))}
    idx = {id(tok): i for i, (tok, _) in enumerate(pairs)}
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=min(len(jobs), 100)) as pool:
        futs = {pool.submit(_one, shop, tok): tok for tok, shop in jobs}
        for f in as_completed(futs):
            r = f.result()
            i = idx[id(futs[f])]
            k = str(r["status"]) if r["status"] in (200, 403, 429) else "err"
            out[i][k] += 1
    span = max(0.001, time.monotonic() - t0)
    return {"per": out, "span": span, "rps": len(jobs) / span}


def main() -> None:
    pairs = _load()
    if not pairs:
        return
    print(f"[probe] {len(pairs)} token, bitta IP (bu mashina). Endpoint: "
          f"GET .../v2/invoice/dimensional-groups (read-only)\n")
    for i, (tok, shop) in enumerate(pairs):
        print(f"   token[{i}] (yashirin)  shop={shop}")
    print()

    # ── 1. Header o'qish — har token bucket'i ────────────────────────────
    print("── 1. Bitta sokin so'rov / token — Uzum bucket header'lari ──")
    for i, (tok, shop) in enumerate(pairs):
        r = _one(shop, tok)
        rate = r.get("rate") or {}
        print(f"   token[{i}]: HTTP {r['status']} {r['ms']:.0f}ms  "
              + (str(rate) if rate else "(rate-header yo'q — limit yashirin)"))
        time.sleep(0.5)

    # ── 2. Bitta token ramp — 429/403 shifti qayerda ─────────────────────
    tok0, shop0 = pairs[0]
    print("\n── 2. BITTA token ramp (o'sib boradigan burst) — shiftni topamiz ──")
    print("   (403=WAF/IP signal · 429=token-bucket signal)")
    time.sleep(3)
    first_block = None            # (status, rps) — birinchi to'silish
    for n in (4, 8, 12, 16, 24, 32, 48, 64):
        r = _burst([(tok0, shop0)], n)
        p = r["per"][0]
        tag = ""
        if p["403"]:
            tag = "  ← 403 (WAF/IP!)"
        elif p["429"]:
            tag = "  ← 429 (bucket)"
        print(f"   {n:>2} parallel (~{r['rps']:.0f}/s)  →  "
              f"200×{p['200']} 403×{p['403']} 429×{p['429']} err×{p['err']}{tag}")
        if (p["403"] or p["429"]) and first_block is None:
            first_block = ("403" if p["403"] else "429", r["rps"])
            break
        time.sleep(3)

    if first_block is None:
        print("   → shift topilmadi (bu darajada blok yo'q). RPS'ni oshiring "
              "yoki _MAX_TOTAL/to'lqinlarni kattalashtiring.")
    else:
        st, rps = first_block
        print(f"\n   ⇒ BIRINCHI to'siq: {st}  ~{rps:.0f} so'rov/s atrofida.")

    # ── 3. DIFERENSIAL: token[0] to'silganда token[1] SHU IP'da ishlaydimi? ─
    if len(pairs) >= 2 and first_block:
        tok1, shop1 = pairs[1]
        print("\n── 3. DIFERENSIAL test (IP-mi / token-mi) ──")
        print(f"   token[0] hozir to'silgan bo'lishi mumkin. Xuddi shu IP'dan "
              f"token[1] bilan so'rov yuboramiz:")
        # token[1] ni bir necha marta sinaymiz (token[0] hali "issiq" to'siqда).
        res1 = [_one(shop1, tok1) for _ in range(3)]
        st1 = [r["status"] for r in res1]
        ok1 = sum(1 for s in st1 if s == 200)
        blk1 = sum(1 for s in st1 if s in (403, 429))
        print(f"   token[1] natijasi: {st1}")
        # token[0] ni ham qayta tekshiramiz (hali to'siqda turibdimi?)
        r0 = _one(shop0, tok0)
        print(f"   token[0] hozir: HTTP {r0['status']}")
        print("\n   ⇒ XULOSA:")
        if r0["status"] in (403, 429) and blk1 and ok1 == 0:
            print("     token[0] to'silgan VA token[1] ham SHU IP'da to'sildi")
            print("     → 🔴 IP-ASOSLI (WAF). Ko'p token YORDAM BERMAYDI — KO'P IP kerak.")
        elif r0["status"] in (403, 429) and ok1 > 0:
            print("     token[0] to'silgan, LEKIN token[1] shu IP'da ishlayapti")
            print("     → 🟢 TOKEN-ASOSLI (bucket). KO'P TOKEN yordam beradi "
                  "(bitta IP'da ham budjet ko'payadi).")
        else:
            print("     Aniq emas — token[0] tiklanган yoki blok tranzient. "
                  "Bir necha marta qayta ishga tushiring.")
    elif len(pairs) < 2:
        print("\n── 3. DIFERENSIAL test O'TKAZILMADI ──")
        print("   Faqat 1 token berilgan. IP-mi/token-mi'ni ANIQ ajratish uchun "
              "2-token kerak (boshqa userning api_key'i). Hozircha ipuchi: yuqoridagi "
              "birinchi to'siq 403 bo'lsa — WAF/IP; 429 bo'lsa — bucket.")

    # ── 4. Tiklanish vaqti (blok necha soniyada ketadi) ──────────────────
    if first_block:
        print("\n── 4. Tiklanish — token[0] necha soniyada 200 qaytaradi ──")
        t0 = time.monotonic()
        for i in range(1, 31):
            time.sleep(2.0)
            if _one(shop0, tok0)["status"] == 200:
                print(f"   → tiklandi ~{time.monotonic()-t0:.0f}s da "
                      f"(429 bucket → sekundlar; 403 WAF → uzoqroq/IP almashtirish).")
                break
        else:
            print("   → 60s+ tiklanmadi — bu WAF/IP blokiga o'xshaydi (bucket "
                  "bo'lganда tezroq tiklanardi). IP almashtirish kerak bo'lishi mumkin.")

    print("\n[probe] tugadi — read-only, hech narsa o'zgartirilmadi.")


if __name__ == "__main__":
    main()
