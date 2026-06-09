# -*- coding: utf-8 -*-
"""One-shot: populate per-SKU cabinet images NOW (same logic as
app._sync_cabinet_sku_images) so we don't wait for the next 10-min tick."""
import json
from urllib.request import Request, urlopen
from urllib.parse import urlencode
from sqlalchemy import select
from extensions import SessionLocal
from models import Variant, Shop
from core.auth_helpers import _get_admin_token


def photo_key(url):
    marker = "images.uzum.uz/"
    i = (url or "").find(marker)
    if i < 0:
        return None
    rest = url[i + len(marker):]
    for sep in ("/", "?"):
        j = rest.find(sep)
        if j >= 0:
            rest = rest[:j]
    return rest or None


def overlay(shop):
    tok = (_get_admin_token() or "").strip()
    if not tok:
        print("no token"); return 0
    auth = tok if tok.startswith("Bearer ") else "Bearer " + tok
    hdrs = {"Authorization": auth, "Origin": "https://seller.uzum.uz",
            "Referer": "https://seller.uzum.uz/"}
    img = {}
    page = 0
    while page < 60:
        url = "https://api-seller.uzum.uz/api/seller/shop/%s/sku-list?%s" % (
            shop, urlencode({"page": page, "size": 100, "search": ""}))
        try:
            with urlopen(Request(url, headers=hdrs), timeout=25) as r:
                raw = json.loads(r.read().decode())
        except Exception as e:
            print("shop", shop, "p", page, "err", e); break
        lst = (raw.get("payload") or {}).get("skuList") or raw.get("skuList") or []
        if not lst:
            break
        for s in lst:
            sid = s.get("skuId")
            k = photo_key(s.get("imageHigh") or s.get("image") or "")
            if sid is not None and k:
                img[str(sid)] = "https://images.uzum.uz/%s/t_product_540_high.jpg" % k
        if len(lst) < 100:
            break
        page += 1
    if not img:
        print("shop", shop, "no images"); return 0
    updated = 0
    with SessionLocal() as db:
        rows = db.execute(select(Variant).where(Variant.uzum_sku_id.in_(list(img.keys())))).scalars().all()
        for v in rows:
            nu = img.get(v.uzum_sku_id)
            if nu and v.image_url != nu:
                v.image_url = nu; updated += 1
        if updated:
            db.commit()
    print("shop", shop, "cabinet_skus", len(img), "variants_updated", updated)
    return updated


with SessionLocal() as db:
    shops = db.execute(select(Shop.uzum_id)).scalars().all()
total = 0
for sh in shops:
    total += overlay(str(sh))
print("TOTAL variants updated:", total)
