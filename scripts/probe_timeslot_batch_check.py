"""Time-slot BATCH tekshiruvi — 15 qty'ni BITTA so'rovga solsa nima bo'ladi?

Savol (Abdulaziz 2026-07-08): 50,100,150,…,750 ni AYRIM so'rov emas, BITTA
so'rovda (skuList ko'p qatorli) yuborsa — Uzum har qty uchun alohida javob
beradimi, yoki hammasini QO'SHIB (sum) bitta javob beradimi?

Tekshiruv (READ-ONLY, ~6 so'rov):
  A) qty=50 yakka        → slot soni N50
  B) qty=750 yakka       → slot soni N750
  C) qty=6000 yakka      → slot soni Nsum   (6000 = 50+100+…+750)
  D) BATCH: skuList = 15 qator, bitta skuId, qty 50..750  → status + shape + Nbatch
  E) BATCH: skuList = 15 qator, TURLI skuId (agar topilsa) → status + Nbatch

Agar Nbatch == Nsum  →  Uzum QO'SHADI (sum), per-qty javob BERMAYDI.
Javob shakli {payload:{timeSlots:[…]}} — yassi; per-qator kaliti bo'lsa ko'ramiz.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

_BASE = "https://api-seller.uzum.uz/api/seller/shop"
_SLOT_WINDOW_MS = 31_532_400_000
_TIMEFROM_LEAD_MS = 5 * 60 * 1000


def _ctx():
    from app import app as flask_app
    from models import Shop
    from extensions import SessionLocal
    from sqlalchemy import select
    from core.auth_helpers import _get_admin_token
    from postavki.slot_watch import _get_sku_context
    from postavki import client
    with flask_app.app_context():
        tok = (_get_admin_token() or "").strip()
        with SessionLocal() as db:
            shop_id = db.execute(
                select(Shop.uzum_id).where(Shop.uzum_id.isnot(None)).limit(1)
            ).scalar_one_or_none()
        shop_id = str(shop_id) if shop_id else ""
        base, _dim, pool = _get_sku_context(shop_id) if shop_id else (None, None, None)
        # bir nechta TURLI sku topishga urinamiz (batch-E uchun)
        extra_skus = []
        try:
            rows = client.restock_skus(shop_id, page=0, size=20)
            for r in (rows or []):
                sid = r.get("skuId") or r.get("id")
                if sid and sid != (base or {}).get("skuId"):
                    extra_skus.append(int(sid))
                if len(extra_skus) >= 14:
                    break
        except Exception as e:
            print("  (extra sku topilmadi:", type(e).__name__, str(e)[:60], ")")
    return tok, shop_id, dict(base or {}), (pool or "FULLFILMENT"), extra_skus


def _post(sess, url, headers, sku_lines, pool):
    import time
    now = int(time.time() * 1000)
    body = {"skuList": sku_lines, "poolSource": pool,
            "timeFrom": now + _TIMEFROM_LEAD_MS, "timeTo": now + _SLOT_WINDOW_MS}
    r = sess.post(url, headers=headers, json=body, timeout=(5, 15))
    n = -1
    keys = None
    try:
        j = r.json()
        pl = j.get("payload")
        if isinstance(pl, dict):
            keys = list(pl.keys())
            n = len(pl.get("timeSlots") or [])
        elif isinstance(pl, list):
            keys = "LIST len=%d" % len(pl)
    except Exception:
        pass
    return r.status_code, n, keys, (r.text[:400] if r.status_code != 200 else "")


def main():
    tok, shop_id, base, pool, extra = _ctx()
    if not (tok and shop_id and base):
        print("[probe] kontekst topilmadi"); return
    sku = base.get("skuId")
    url = f"{_BASE}/{shop_id}/v2/invoice/time-slot"
    h = {"Authorization": tok if tok.startswith("Bearer ") else f"Bearer {tok}",
         "Accept": "application/json", "Content-Type": "application/json",
         "Origin": "https://seller.uzum.uz", "Referer": "https://seller.uzum.uz/"}
    sess = requests.Session()
    ladder = [50, 100, 150, 200, 250, 300, 350, 400, 450, 500, 550, 600, 650, 700, 750]
    total = sum(ladder)  # 6000

    print(f"[probe] shop={shop_id} sku={sku} pool={pool}  narvon={ladder}  sum={total}")
    print(f"[probe] extra turli sku topildi: {len(extra)} ta\n")

    def line(q, s=None):
        return {**base, "skuId": (s or sku), "quantityToStock": q}

    print("  TEST                                     status  slots  payload-keys")
    print("  " + "-"*70)

    st, n50, k, _ = _post(sess, url, h, [line(50)], pool)
    print(f"  A) yakka qty=50                          {st}    {n50:>5}  {k}")
    st, n750, k, _ = _post(sess, url, h, [line(750)], pool)
    print(f"  B) yakka qty=750                         {st}    {n750:>5}  {k}")
    st, nsum, k, _ = _post(sess, url, h, [line(total)], pool)
    print(f"  C) yakka qty={total} (=sum narvon)       {st}    {nsum:>5}  {k}")

    # D) BATCH — bitta skuId, 15 qator har xil qty
    batch_same = [line(q) for q in ladder]
    st, nbat, k, err = _post(sess, url, h, batch_same, pool)
    print(f"  D) BATCH 15 qator (BIR skuId, qty50..750) {st}    {nbat:>5}  {k}")
    if err:
        print(f"       xato matni: {err}")

    # E) BATCH — turli skuId (agar bor bo'lsa)
    if extra:
        skus = [sku] + extra[:14]
        batch_diff = [line(q, s) for q, s in zip(ladder, skus)]
        st, nbat2, k, err = _post(sess, url, h, batch_diff, pool)
        print(f"  E) BATCH {len(batch_diff)} qator (TURLI skuId)      {st}    {nbat2:>5}  {k}")
        if err:
            print(f"       xato matni: {err}")

    print("\n  " + "="*68)
    print("  XULOSA:")
    if nbat == nsum and nbat >= 0:
        print(f"    D-batch slot soni ({nbat}) == qty={total} yakka ({nsum})")
        print(f"    → Uzum skuList'ni QO'SHADI (sum). BITTA umumiy javob, per-qty EMAS.")
    elif nbat == n50:
        print(f"    D-batch ({nbat}) == qty=50 yakka ({n50}) → faqat 1-qatorni hisobga oladi?")
    elif nbat < 0:
        print(f"    D-batch xato/rad etildi (status yuqorida) — bir xil skuId qo'llanmaydi.")
    else:
        print(f"    D-batch={nbat}, sum-yakka={nsum}, qty50={n50}, qty750={n750} — mos kelmadi, tahlil kerak.")
    print("  Javob shakli barcha holatda {payload:{timeSlots:[…]}} — yassi ro'yxat,")
    print("  per-qator/per-qty ajratma YO'Q → bitta so'rov 15 alohida javob BERa olmaydi.")
    print("\n[probe] tugadi — read-only.")


if __name__ == "__main__":
    main()
