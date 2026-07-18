"""Yarim-avtomat поставка rejasi — «qancha kerak» hisobi + guruhlash.

Hisob mantig'i (avval `/invoice/restock` sahifasida edi — u sahifa olib
tashlandi, mantiq shu yerda yashaydi):

    sotildi (oxirgi N kun, finance_orders) − Uzum ombordagi qoldiq  → kerak
    min(kerak, bizning ombordagi qoldiq)                            → tavsiya

Farqi: qatorlar Uzum'ning ``/v2/invoice/sku-list`` javobidan quriladi (faqat
поставка qilinishi MUMKIN bo'lgan SKU'lar, `skuId` bilan), bizning bazamiz esa
faqat ombor qoldig'i / guruh (ProductGroup) / rang uchun qo'shiladi. Shu bois
har bir qator kafolatli tarzda hisob-faktura qatoriga aylana oladi.
"""
from __future__ import annotations

import threading
import time
from datetime import date, timedelta

from sqlalchemy import select

from extensions import SessionLocal
from models import ProductGroup, Shop, Variant
from core.sales_reads import day_bounds_tashkent, read_sales_aggregated
from core.time_helpers import _today_app_tz
from core.uzum_skulist import normalize_uzum_image_url
from postavki import client

PERIOD_DAYS = (7, 10, 15, 30, 60)   # maks — 60 kun
DEFAULT_DAYS = 30

# Uzum sku-list keshi (jarayon ichida, TTL). Sabab: Uzum har sahifani ~1.5s
# beradi va bu butun hisobning ~99% vaqtini yeydi, holbuki SKU ro'yxati
# daqiqalar davomida o'zgarmaydi. Davr chipini (7/30/90 kun) bosganda esa
# FAQAT sotuv oynasi o'zgaradi — sku-list qayta tortilmaydi (0.01s hisob).
_SKU_TTL = 300.0  # 5 daqiqa
_sku_cache: dict[tuple, tuple[float, list]] = {}
_sku_lock = threading.Lock()


def invalidate_sku_cache(shop_uzum_id: str | None = None) -> None:
    """Keshni tozalash (do'kon bo'yicha yoki butunlay)."""
    with _sku_lock:
        if shop_uzum_id is None:
            _sku_cache.clear()
        else:
            for k in [k for k in _sku_cache if k[0] == str(shop_uzum_id)]:
                _sku_cache.pop(k, None)


def resolve_days(raw) -> int:
    """`?days=` chipini [7,10,15,30,60] ichiga qisadi, default 30."""
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_DAYS
    return n if n in PERIOD_DAYS else DEFAULT_DAYS


def resolve_window(days_raw=None, from_raw=None, to_raw=None) -> dict:
    """Sotuv oynasi: `date_from`+`date_to` (kalendar) USTUN, aks holda `days` chipi.

    Ilovadagi boshqa sahifalar bilan bir xil shartnoma (products/routes.py:1993):
    sanalar `YYYY-MM-DD`; teskari oraliq almashtiriladi; 1..365 kun bilan
    chegaralanadi. Qaytadi: {start_ts, end_ts, days, date_from, date_to, custom}.
    """
    today = _today_app_tz()
    d_from = d_to = None

    if from_raw and to_raw:
        try:
            d_from = date.fromisoformat(str(from_raw).strip())
            d_to = date.fromisoformat(str(to_raw).strip())
        except ValueError:
            d_from = d_to = None

    if d_from and d_to:
        if d_to < d_from:                    # teskari tanlov — almashtiramiz
            d_from, d_to = d_to, d_from
        days = max(1, min((d_to - d_from).days + 1, 365))
        custom = True
    else:
        days = resolve_days(days_raw)
        d_to = today
        d_from = today - timedelta(days=days)
        custom = False

    start_ts, _ = day_bounds_tashkent(d_from)
    _, end_ts = day_bounds_tashkent(d_to)
    return {
        "start_ts": start_ts, "end_ts": end_ts, "days": days,
        "date_from": d_from.isoformat(), "date_to": d_to.isoformat(),
        "custom": custom,
    }


def _fetch_all_skus(shop_uzum_id: str, groups: str, refresh: bool = False) -> list[dict]:
    """Uzum sku-list — bitta so'rovda + 5 daqiqalik kesh."""
    key = (str(shop_uzum_id), groups)
    now = time.monotonic()
    if not refresh:
        with _sku_lock:
            hit = _sku_cache.get(key)
        if hit and (now - hit[0]) < _SKU_TTL:
            return hit[1]

    items = client.restock_skus_all(shop_uzum_id, search="", groups=groups)
    with _sku_lock:
        _sku_cache[key] = (time.monotonic(), items)
    return items


def _sales_map(db, shop_uzum_id: str, start_ts, end_ts) -> dict[str, int]:
    """Oyna ichidagi sotuvlar: {kalit(upper) → dona}. Kalitlar: sku_id, sku title."""
    try:
        rows = read_sales_aggregated(
            shop_uzum_id, start_ts, end_ts, group_by="sku", session=db
        )
    except Exception as e:  # finance o'qishi yiqilsa — 0 sotuv, sahifa baribir ishlaydi
        print(f"[postavki/plan] finance read failed shop={shop_uzum_id}: {e!r}", flush=True)
        rows = []
    m: dict[str, int] = {}
    for r in rows:
        qty = int(r.get("qty_sum") or 0)
        sid = r.get("sku_id")
        if sid:
            m[str(sid).upper()] = qty
        title = (r.get("sku_title") or "").strip()
        if title:
            m[title.upper()] = qty
    return m


def _lookup_sales(sales: dict[str, int], *keys) -> int:
    """SKU sotuvi: sku_id → sku kodi → shtrix-kod (barchasi upper bo'yicha)."""
    for k in keys:
        if not k:
            continue
        v = sales.get(str(k).upper())
        if v:
            return int(v)
    return 0


def build_plan(
    shop_uzum_id: str,
    days: int = DEFAULT_DAYS,
    groups: str = "SMALL,MEDIUM",
    refresh: bool = False,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    """Guruhlangan restock rejasi.

    Qaytadi: ``{"days": N, "groups": [...]}`` — har guruh (asosiy mahsulot):
    ``{key, name, image, short_sku, vcount, sales, uzum_qty, wh_qty, need,
    recommend, variants: [...]}``; har variant qatorida поставка uchun zarur
    bo'lgan hamma narsa bor (skuId, barcode, price, image, ...).
    """
    sku_items = _fetch_all_skus(shop_uzum_id, groups, refresh=refresh)

    with SessionLocal() as db:
        shop = db.execute(
            select(Shop).where(Shop.uzum_id == str(shop_uzum_id))
        ).scalar_one_or_none()

        by_sku_id: dict[str, tuple[Variant, ProductGroup]] = {}
        by_barcode: dict[str, tuple[Variant, ProductGroup]] = {}
        blocked: dict[str, str] = {}
        wh_known = False  # do'kon bo'yicha ombor qoldig'i umuman to'ldirilganmi
        if shop:
            rows = db.execute(
                select(Variant, ProductGroup)
                .join(ProductGroup, Variant.group_id == ProductGroup.id)
                .where(ProductGroup.shop_id == shop.id)
            ).all()
            for v, g in rows:
                if (v.warehouse_quantity or 0) > 0:
                    wh_known = True
                if v.uzum_sku_id:
                    by_sku_id[str(v.uzum_sku_id)] = (v, g)
                if v.barcode:
                    by_barcode[str(v.barcode).upper()] = (v, g)
                if v.blocked and v.uzum_sku_id:
                    blocked[str(v.uzum_sku_id)] = (
                        (v.blocking_reason or v.sku_block_reason or "").strip()
                    )

        win = resolve_window(days, date_from, date_to)
        sales = _sales_map(db, str(shop_uzum_id), win["start_ts"], win["end_ts"])

    groups_by_key: dict[str, dict] = {}

    for it in sku_items:
        sku_id = it.get("skuId")
        if sku_id is None:
            continue
        sku_id_s = str(sku_id)
        barcode = str(it.get("barcode") or "")
        pair = by_sku_id.get(sku_id_s) or by_barcode.get(barcode.upper())
        v, g = pair if pair else (None, None)

        uzum_qty = int(it.get("quantityActive") or 0)
        wh_qty = int(getattr(v, "warehouse_quantity", 0) or 0) if v else 0
        sold = _lookup_sales(
            sales, sku_id_s, getattr(v, "sku", None), it.get("skuTitle"), barcode
        )
        need = max(sold - uzum_qty, 0)
        # Tavsiya HAR DOIM o'z omborimizdagi qoldiq bilan cheklanadi: faqat
        # jismonan bor tovarni jo'nata olamiz. Ombor 0 bo'lsa — tavsiya 0
        # («Добавить» o'chiq), garchi ehtiyoj (need) katta bo'lsa ham.
        recommend = min(need, wh_qty)

        price = int(it.get("purchasePrice") or 0)
        if price <= 0 and v is not None:
            price = int(v.purchase_price or 0)

        image = normalize_uzum_image_url(it.get("image") or it.get("imageHigh") or "")

        if g is not None:
            key = f"g:{g.id}"
            g_name = g.name
            g_image = normalize_uzum_image_url(g.image_url or "") or image
        else:
            key = "t:" + (it.get("productTitle") or "—")
            g_name = it.get("productTitle") or "—"
            g_image = image

        grp = groups_by_key.get(key)
        if grp is None:
            grp = groups_by_key[key] = {
                "key": key,
                "name": g_name,
                "image": g_image,
                "short_sku": "",
                "sales": 0,
                "uzum_qty": 0,
                "wh_qty": 0,
                "need": 0,
                "recommend": 0,
                "variants": [],
            }

        dim = it.get("dimensionalGroup") or {}
        grp["variants"].append({
            "skuId": int(sku_id),
            "sku": (getattr(v, "sku", None) or it.get("skuTitle") or sku_id_s),
            "color": (getattr(v, "color", None) or it.get("skuTitle") or ""),
            "barcode": barcode,
            "image": image,
            "price": price,
            "dim": (dim.get("title") if isinstance(dim, dict) else "") or "",
            "sales": sold,
            "uzum_qty": uzum_qty,
            "wh_qty": wh_qty,
            "need": need,
            "recommend": recommend,
            "blocked": sku_id_s in blocked,
            "blockReason": blocked.get(sku_id_s, ""),
        })
        grp["sales"] += sold
        grp["uzum_qty"] += uzum_qty
        grp["wh_qty"] += wh_qty
        grp["need"] += need
        grp["recommend"] += recommend

    out = []
    for grp in groups_by_key.values():
        grp["variants"].sort(key=lambda x: (-x["recommend"], -x["need"], str(x["sku"])))
        grp["vcount"] = len(grp["variants"])
        # «LRINGS-STRES33» — SKU kodining dastlabki ikki bo'lagi (expenses uslubi)
        first = str(grp["variants"][0]["sku"] or "")
        grp["short_sku"] = "-".join(first.split("-")[:2]) if "-" in first else first
        out.append(grp)

    # Shoshilinchlik bo'yicha: avval yuborsa bo'ladiganlar (recommend), keyin kerak
    out.sort(key=lambda g: (-g["recommend"], -g["need"], (g["name"] or "").lower()))
    return {
        "days": win["days"],
        "date_from": win["date_from"],
        "date_to": win["date_to"],
        "custom": win["custom"],       # kalendar orqali tanlanganmi (chip emas)
        "wh_source": "warehouse" if wh_known else "none",
        "groups": out,
    }
