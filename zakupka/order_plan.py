"""Xarid rejasi — «Xitoydan nimani, qancha va qanchaga sotib olish kerak».

MATEMATIKA — `postavki/restock_plan.py` ning davomi, yangi hisob YO'Q.

`build_plan()` har bir SKU uchun allaqachon quyidagilarni beradi:

    need      = max(davr sotuvi − Uzum qoldig'i, 0)   ← umumiy yetishmovchilik
    recommend = min(need, wh_qty)                     ← shuni BUGUN jo'nata olamiz

`recommend` — omborimizdan Uzum'ga jo'natiladigan qism. Sotib olish kerak
bo'lgan qism esa aynan omborimiz YOPA OLMAGANI:

    to_order  = need − recommend = max(need − wh_qty, 0)
              = max(davr sotuvi − Uzum qoldig'i − ombor qoldig'i, 0)

Ya'ni bitta tenglamaning ikkinchi yarmi:

    spros (davr sotuvi) = Uzum qoldig'i + ombor qoldig'i + DEFITSIT

Shuning uchun bu sahifa Uzum'ga BITTA ham yangi so'rov qo'shmaydi va
yarim-avtomat/avto накладнойlar bilan HAR DOIM bir xil raqamni ko'rsatadi.
"""
from __future__ import annotations

from sqlalchemy import select

from core.uzum_skulist import normalize_uzum_image_url
from extensions import SessionLocal
from models import ProductGroup, Shop, Variant
from postavki import restock_plan


def _build_plan_from_db(
    shop_uzum_id: str,
    days: int = 30,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    """Xarid rejasi uchun SKU-ro'yxati — Uzum'ga EMAS, o'z bazamizga so'rov.

    `restock_plan.build_plan` bilan AYNAN bir xil shakldagi dict qaytaradi
    ({days, date_from, date_to, custom, groups:[{key,name,image,short_sku,
    variants:[...]}]}), lekin har bir qator jonli Uzum `sku-list`'idan emas,
    `variants` jadvalidan quriladi. Uzum'dan olinadigan ikki qiymat allaqachon
    bazamizда bor va ProductsSync fon-jarayoni ularni har necha daqiqada
    yangilab turadi:

        quantityActive → Variant.uzum_quantity   («На Uzum» qoldig'i)
        purchasePrice  → Variant.purchase_price
        image          → Variant.image_url

    Xarid rejasi «nima sotib olish» tavsiyasi bo'lgani uchun bir necha daqiqa
    eskirgan qoldiq mutlaqo yetarli — evaziga sahifa Uzum'ga BITTA ham so'rov
    yubormaydi (ilgari har bosishда do'kon boshiga 2 ta jonli so'rov ketardi).

    Eslatma: bu yerда o'lcham guruhi bo'yicha FILTR yo'q — do'kon sotayotgan
    HAMMA narsani ko'rsatamiz (xarid uchun cheklov kerak emas). Sotilmagan
    SKU'lar defitsit=0 bo'lib tabiiy ravishda ro'yxatdan tushib qoladi.
    """
    win = restock_plan.resolve_window(days, date_from, date_to)

    groups_by_key: dict[str, dict] = {}
    with SessionLocal() as db:
        shop = db.execute(
            select(Shop).where(Shop.uzum_id == str(shop_uzum_id))
        ).scalar_one_or_none()

        rows = []
        if shop:
            rows = db.execute(
                select(Variant, ProductGroup)
                .join(ProductGroup, Variant.group_id == ProductGroup.id)
                .where(ProductGroup.shop_id == shop.id)
            ).all()

        # Sotuv oynasi — o'z finance bazamizdan (Uzum'ga bog'liq emas).
        sales = restock_plan._sales_map(
            db, str(shop_uzum_id), win["start_ts"], win["end_ts"]
        )

    for v, g in rows:
        # `skuId` bo'lmagan variant Uzum'да sotila olmaydi — xarid rejasiga
        # kirmaydi (jonli sku-list'да ham faqat skuId'li qatorlar bor edi).
        if not v.uzum_sku_id:
            continue
        sku_id_s = str(v.uzum_sku_id)
        barcode = str(v.barcode or "")
        uzum_qty = int(v.uzum_quantity or 0)
        wh_qty = int(v.warehouse_quantity or 0)
        sold = restock_plan._lookup_sales(sales, sku_id_s, v.sku, barcode)
        price = int(v.purchase_price or 0)
        image = normalize_uzum_image_url(v.image_url or "")

        key = f"g:{g.id}"
        grp = groups_by_key.get(key)
        if grp is None:
            grp = groups_by_key[key] = {
                "key": key,
                "name": g.name or "",
                "image": normalize_uzum_image_url(g.image_url or "") or image,
                "short_sku": "",
                "variants": [],
            }
        try:
            sku_id_val: int | str = int(sku_id_s)
        except (TypeError, ValueError):
            sku_id_val = sku_id_s
        grp["variants"].append({
            "skuId": sku_id_val,
            "sku": v.sku or sku_id_s,
            "color": v.color or "",
            "barcode": barcode,
            "image": image,
            "price": price,
            "sales": sold,
            "uzum_qty": uzum_qty,
            "wh_qty": wh_qty,
        })

    def _rank(x: dict) -> tuple:
        # `build_plan` bilan bir xil tartib: eng shoshilinch (recommend) tepada.
        need = max(int(x["sales"]) - int(x["uzum_qty"]), 0)
        rec = min(need, max(int(x["wh_qty"]), 0))
        return (-rec, -need, str(x["sku"]))

    out = []
    for grp in groups_by_key.values():
        grp["variants"].sort(key=_rank)
        first = str(grp["variants"][0]["sku"] or "") if grp["variants"] else ""
        grp["short_sku"] = "-".join(first.split("-")[:2]) if "-" in first else first
        out.append(grp)

    return {
        "days": win["days"],
        "date_from": win["date_from"],
        "date_to": win["date_to"],
        "custom": win["custom"],
        "groups": out,
    }


def to_order_qty(v: dict) -> int:
    """Bitta SKU uchun sotib olish miqdori — sof funksiya (test qilinadi).

    `need − recommend` deb yozish CHIROYLI, lekin XATO: `build_plan` ичida
    `recommend = min(need, wh_qty)` va wh_qty MANFIY bo'lishi mumkin (bizning
    bazada 135 ta SKU shunday — hisob buzilgan, eng yomoni −276). Manfiy
    recommend ayirmani KO'PAYTIRIB yuboradi: need 27 − (−40) = 67, ya'ni
    sotmagan tovarimizni «sotib oling» deb chiqaradi.

    Shuning uchun xom raqamlardan, MANFIYNI NOLGA qisib hisoblaymiz. Manfiy
    ombor qoldig'i «minus 40 dona bor» degani emas — «hisob adashgan» degani,
    javonда esa HECH NARSA yo'q. Nol — yagona to'g'ri o'qish.
    """
    sales = int(v.get("sales") or 0)
    uzum = max(int(v.get("uzum_qty") or 0), 0)
    wh = max(int(v.get("wh_qty") or 0), 0)
    return max(sales - uzum - wh, 0)


def coverage(sales: int, uzum: int, wh: int) -> dict:
    """Sprosning uch bo'lagi. Ular ANIQ sprosga teng bo'ladi:

        min(u, s) + min(w, s−u) + max(s−u−w, 0) ≡ s

    Shu ayniyat tufayli qoplama chizig'i har doim to'la bo'ladi va guruh
    darajasida bo'laklarni QO'SHIB chiqsa ham buzilmaydi.
    """
    s = max(int(sales or 0), 0)
    u = max(int(uzum or 0), 0)
    w = max(int(wh or 0), 0)
    c_u = min(u, s)
    c_w = min(w, max(s - u, 0))
    gap = max(s - u - w, 0)
    return {"demand": s, "uzum": c_u, "wh": c_w, "gap": gap}


def _variant(v: dict) -> dict:
    """build_plan variantidan xarid qatorini yasaydi."""
    qty = to_order_qty(v)
    price = int(v.get("price") or 0)
    return {
        "skuId": v.get("skuId"),
        "sku": v.get("sku") or "",
        "color": v.get("color") or "",
        "barcode": v.get("barcode") or "",
        "image": v.get("image") or "",
        "sales": int(v.get("sales") or 0),
        "uzum_qty": int(v.get("uzum_qty") or 0),
        "wh_qty": int(v.get("wh_qty") or 0),
        "to_order": qty,
        "price": price,
        # Narx nomaʼlum bo'lsa — summani NOL deb ko'rsatmaymiz (bu yolg'on
        # bo'lardi va jami summani kamaytirib yuborardi). `no_price` bilan
        # belgilaymiz, UI «—» chizadi, jami esa buni alohida sanaydi.
        "cost": qty * price if price > 0 else 0,
        "no_price": qty > 0 and price <= 0,
        # Ombor qoldig'i manfiy — hisob buzilgan. Yashirmaymiz: hisobда 0 deb
        # olinadi, lekin foydalanuvchi buni KO'RISHI kerak (aks holda reja
        # to'g'ri ko'rinadi-yu, ombor raqamiga ishonib bo'lmaydi).
        "neg_wh": int(v.get("wh_qty") or 0) < 0,
        "cov": coverage(v.get("sales"), v.get("uzum_qty"), v.get("wh_qty")),
    }


def build_order_plan(
    shop_uzum_id: str,
    shop_name: str = "",
    days: int = 30,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    """Bitta do'kon uchun xarid rejasi.

    SKU qatorlari o'z bazamizdan olinadi (`_build_plan_from_db`) — sahifa
    Uzum'ga hech qanday so'rov yubormaydi. Hisob-kitob (need/recommend/coverage)
    o'zgarmagan: xuddi ilgarigidek xom raqamlardан qayta hisoblanadi.
    """
    plan = _build_plan_from_db(
        str(shop_uzum_id),
        days=days,
        date_from=date_from,
        date_to=date_to,
    )
    groups = []
    for g in plan.get("groups") or []:
        variants = [_variant(v) for v in (g.get("variants") or [])]
        # SKU'lar: eng katta defitsit tepada
        variants.sort(key=lambda v: (-v["to_order"], -v["cost"], v["sku"]))
        to_order = sum(v["to_order"] for v in variants)
        sales = sum(v["sales"] for v in variants)
        uzum_q = sum(v["uzum_qty"] for v in variants)
        wh_q = sum(v["wh_qty"] for v in variants)
        # Guruh chizig'i — SKU bo'laklarining yig'indisi (guruh raqamlaridan
        # QAYTA hisoblamaymiz). Ayniyat SKU darajasida saqlangani uchun
        # yig'indi ham sprosga aniq teng bo'ladi.
        cov = {
            "demand": sum(v["cov"]["demand"] for v in variants),
            "uzum": sum(v["cov"]["uzum"] for v in variants),
            "wh": sum(v["cov"]["wh"] for v in variants),
            "gap": sum(v["cov"]["gap"] for v in variants),
        }
        # Qoldiq BOR, lekin BOSHQA SKU'da. Ya'ni «318 − 315 = 3» degan sodda
        # hisob 145 ni bermaydi: oltin 17-o'lcham kumush 21-o'lchamning
        # o'rnini bosa olmaydi. Buni ATAYMIZ, aks holda jadval buzuq ko'rinadi.
        naive_gap = max(sales - max(uzum_q, 0) - max(wh_q, 0), 0)
        groups.append({
            "key": g.get("key"),
            "name": g.get("name") or "",
            "image": g.get("image") or "",
            "short_sku": g.get("short_sku") or "",
            "shop": shop_name,
            "shop_id": str(shop_uzum_id),
            "vcount": len(variants),
            "sales": sales,
            "uzum_qty": uzum_q,
            "wh_qty": wh_q,
            "to_order": to_order,
            "cost": sum(v["cost"] for v in variants),
            "no_price": any(v["no_price"] for v in variants),
            "neg_wh": any(v["neg_wh"] for v in variants),
            "cov": cov,
            "mismatch": to_order > naive_gap,
            "variants": variants,
        })
    return {
        "days": plan.get("days"),
        "date_from": plan.get("date_from"),
        "date_to": plan.get("date_to"),
        "custom": plan.get("custom", False),
        "groups": groups,
    }


def merge_plans(plans: list[dict]) -> dict:
    """Bir nechta do'kon rejasini BITTA ro'yxatga qo'shadi.

    Xarid Xitoydan bitta bo'lib keladi — shuning uchun «Все магазины» rejimida
    do'konlar bir jadvalda, defitsit bo'yicha kamayish tartibida turadi.
    """
    groups: list[dict] = []
    for p in plans:
        groups.extend(p.get("groups") or [])
    # «Yetishmayotgani tepada» — foydalanuvchi aynan shuni so'radi.
    groups.sort(key=lambda g: (-g["to_order"], -g["cost"], (g["name"] or "").lower()))
    need = [g for g in groups if g["to_order"] > 0]
    base = plans[0] if plans else {}
    return {
        "days": base.get("days"),
        "date_from": base.get("date_from"),
        "date_to": base.get("date_to"),
        "custom": base.get("custom", False),
        "groups": groups,
        "totals": {
            "products": len(need),          # xarid kerak bo'lgan tovarlar
            "covered": len(groups) - len(need),   # hammasi yetarli — tegmaymiz
            "units": sum(g["to_order"] for g in need),
            "cost": sum(g["cost"] for g in need),
            # Narxi yo'q tovarlar summaga KIRMAYDI — jami «to'liq emas»ligini
            # aytib turishimiz kerak, aks holda raqam ishonchli ko'rinadi-yu,
            # aslida kam bo'ladi.
            "no_price": sum(1 for g in need if g["no_price"]),
            # Ombor hisobi adashgan SKU'lar — reja to'g'ri, lekin ombor
            # raqamlariga to'liq ishonib bo'lmaydi. Ogohlantiramiz.
            "neg_wh": sum(
                1 for g in groups for v in g["variants"] if v["neg_wh"]
            ),
        },
    }
