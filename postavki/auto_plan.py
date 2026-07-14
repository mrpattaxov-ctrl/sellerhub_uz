"""«Авто» поставка — rejani o'zi to'ldiradi va slotni o'zi hal qiladi.

Hisob YARIM-AVTOMAT bilan bir xil (`restock_plan.build_plan`):

    sotildi (oxirgi N kun) − Uzum qoldig'i          → kerak (need)
    min(kerak, bizning ombor qoldig'i)              → tavsiya (recommend)

Farqi — user qo'ygan TO'RTTA chegara avtomat qo'llanadi:

    min_per_sku  SKU tushsa kamida shuncha dona (sotuv 1 bo'lsa ham 2 ta);
                 omborda yetmasa — bori.
    max_units    bitta накладнойда jami dona shifti.
    max_skus     bitta накладнойда SKU qatorlari shifti (Uzum'da 100).
    slot_from/to slot uchun NISBIY kun oynasi (0 = bugun) — aniq sana emas,
                 shuning uchun bir marta qo'yiladi va har kuni ishlaydi.

Bu modul HECH NARSA yaratmaydi — faqat hisoblaydi. `pack_lines()` sof
funksiya (I/O yo'q), shuning uchun to'g'ridan-to'g'ri test qilinadi.
"""
from __future__ import annotations

from datetime import date, timedelta

from core.time_helpers import _today_app_tz
from postavki.restock_plan import DEFAULT_DAYS, build_plan

# Uzum'ning o'z shifti — user bundan kattasini qo'ya olmaydi.
UZUM_MAX_SKUS = 100

DEFAULTS = {
    "maxUnits": 500,
    "maxSkus": 100,
    "minPerSku": 1,
    "slotFrom": 0,     # bugundan
    "slotTo": 2,       # indingacha
    "salesDays": DEFAULT_DAYS,
}


def clamp_config(raw: dict) -> tuple[dict, str | None]:
    """UI/DB'dan kelgan sozlamani tekshiradi va chegaraga qisadi.

    Qaytadi: (config, error). error bo'lsa — saqlamaymiz.
    """
    def _int(key, lo, hi):
        try:
            v = int(raw.get(key, DEFAULTS[key]))
        except (TypeError, ValueError):
            raise ValueError(f"{key}: butun son bo'lsin")
        return max(lo, min(v, hi))

    try:
        cfg = {
            "maxUnits":  _int("maxUnits", 1, 100_000),
            "maxSkus":   _int("maxSkus", 1, UZUM_MAX_SKUS),
            "minPerSku": _int("minPerSku", 1, 10_000),
            "slotFrom":  _int("slotFrom", 0, 30),
            "slotTo":    _int("slotTo", 0, 30),
            "salesDays": _int("salesDays", 1, 365),
        }
    except ValueError as e:
        return {}, str(e)

    if cfg["slotFrom"] > cfg["slotTo"]:
        return {}, "Слот: день «с» не может быть позже дня «по»."
    if cfg["minPerSku"] > cfg["maxUnits"]:
        return {}, "Минимум на SKU не может превышать максимум единиц в поставке."
    return cfg, None


def slot_window(cfg: dict, today: date | None = None) -> tuple[date, date]:
    """Nisbiy oynani BUGUNGI sanaga bog'laydi. Har накладнойда qayta hisoblanadi:
    bugun yaratilsa [bugun..indinga], ertaga yaratilsa [ertaga..+1] — user
    kalendardan aniq sana tanlamaydi."""
    t = today or _today_app_tz()
    return (t + timedelta(days=int(cfg["slotFrom"])),
            t + timedelta(days=int(cfg["slotTo"])))


MAX_INVOICES = 20   # xavfsizlik shifti — bitta bosishda 20 dan ortiq накладной ochilmasin


def _eligible(variants: list[dict], cfg: dict) -> tuple[list[dict], list[dict]]:
    """Jo'natsa bo'ladigan qatorlar + chetda qolganlar (sababi bilan)."""
    min_per_sku = int(cfg["minPerSku"])
    eligible, skipped = [], []
    for v in variants:
        rec = int(v.get("recommend") or 0)
        wh = int(v.get("wh_qty") or 0)
        price = int(v.get("price") or 0)
        if v.get("blocked"):
            skipped.append({**v, "reason": "blocked"});  continue
        if rec <= 0:
            continue          # ehtiyoj yo'q yoki omborda yo'q — jim o'tkazamiz
        if price <= 0:
            skipped.append({**v, "reason": "no_price"});  continue
        qty = min(max(rec, min_per_sku), wh)   # «sotuv 1 bo'lsa ham min», ombordan oshmaydi
        if qty <= 0:
            continue
        eligible.append({**v, "qty": qty})
    return eligible, skipped


def _group_order(eligible: list[dict]) -> list[dict]:
    """Mahsulot (guruh) bo'yicha yonma-yon, guruhlar shoshilinchlik bo'yicha.

    Bir mahsulotning variantlari BIR накладнойга tushishi uchun ular ketma-ket
    keladi — накладной «guruh bo'yicha kesiladi», UI ham shunday ko'rsatadi.
    """
    buckets: dict[str, list[dict]] = {}
    for v in eligible:
        buckets.setdefault(v.get("groupKey") or v.get("group") or "—", []).append(v)
    for items in buckets.values():
        items.sort(key=lambda x: (-int(x.get("need") or 0), -x["qty"], str(x.get("sku") or "")))
    ordered = sorted(
        buckets.values(),
        key=lambda items: (-max(int(i.get("need") or 0) for i in items),
                           -sum(i["qty"] for i in items)),
    )
    return [v for items in ordered for v in items]


def pack_invoices(variants: list[dict], cfg: dict) -> dict:
    """Hammasini bir nechta накладнойga bo'ladi (chegaralarni buzmasdan).

    Ilgari chegaradan oshgani TASHLAB yuborilardi; endi u KEYINGI накладнойга
    o'tadi. SKU o'zi max_units'dan katta bo'lsa — bo'laklanadi, lekin har bo'lak
    `min_per_sku`dan kichik bo'lib qolmaydi (mayda «dum» bo'lmasin).
    """
    max_units = int(cfg["maxUnits"])
    max_skus = min(int(cfg["maxSkus"]), UZUM_MAX_SKUS)
    min_per_sku = int(cfg["minPerSku"])

    eligible, skipped = _eligible(variants, cfg)
    ordered = _group_order(eligible)

    invoices: list[dict] = []
    cur = {"lines": [], "picked": [], "totalUnits": 0}

    def close():
        if cur["lines"]:
            invoices.append({**cur, "skuCount": len(cur["lines"]), "index": len(invoices)})

    for v in ordered:
        remaining = v["qty"]
        while remaining > 0:
            room = max_units - cur["totalUnits"]
            full = len(cur["lines"]) >= max_skus or room < min(min_per_sku, remaining)
            if full:
                close()
                if len(invoices) >= MAX_INVOICES:
                    skipped.append({**v, "reason": "too_many"})
                    remaining = 0
                    cur = {"lines": [], "picked": [], "totalUnits": 0}
                    break
                cur = {"lines": [], "picked": [], "totalUnits": 0}
                room = max_units

            take = min(remaining, room)
            tail = remaining - take
            # Mayda «dum» qoldirmaymiz: keyingi накладнойга min_per_sku'dan
            # kichik qoldiq tushmasin (aks holda u yerda qoida buziladi).
            if 0 < tail < min_per_sku and take - (min_per_sku - tail) >= min_per_sku:
                take -= (min_per_sku - tail)

            cur["lines"].append({
                "skuId": int(v["skuId"]),
                "quantityToStock": int(take),
                "purchasePrice": int(v["price"]),
            })
            cur["picked"].append({**v, "qty": int(take), "partial": take < v["qty"]})
            cur["totalUnits"] += take
            remaining -= take
    close()

    return {
        "invoices": invoices,
        "skipped": skipped,
        "totalUnits": sum(i["totalUnits"] for i in invoices),
        "totalSkus": sum(i["skuCount"] for i in invoices),
    }


def pack_lines(variants: list[dict], cfg: dict) -> dict:
    """Sof funksiya: variantlar + sozlama → накладной qatorlari.

    ``variants`` — build_plan() bergan variant dict'lari (recommend/need/wh_qty/
    price/blocked/skuId...). Qaytadi: {lines, picked, totalUnits, skipped}.
    """
    max_units = int(cfg["maxUnits"])
    max_skus = min(int(cfg["maxSkus"]), UZUM_MAX_SKUS)
    min_per_sku = int(cfg["minPerSku"])

    eligible, skipped = [], []
    for v in variants:
        rec = int(v.get("recommend") or 0)
        wh = int(v.get("wh_qty") or 0)
        price = int(v.get("price") or 0)
        if v.get("blocked"):
            skipped.append({**v, "reason": "blocked"});  continue
        if rec <= 0:
            # Kerak yo'q, yoki omborimizda yo'q — jo'natadigan narsa yo'q.
            continue
        if price <= 0:
            # Sebestoimostsiz qator Uzum'da validation-failed-001 beradi.
            skipped.append({**v, "reason": "no_price"});  continue
        # «Sotuv 1 bo'lsa ham kamida 2 ta» — lekin omborda borigacha.
        qty = min(max(rec, min_per_sku), wh)
        if qty <= 0:
            continue
        eligible.append({**v, "qty": qty})

    # Shoshilinchlik: ehtiyoji katta → oldin. Teng bo'lsa ko'proq jo'natiladigani.
    eligible.sort(key=lambda x: (-int(x.get("need") or 0), -x["qty"], str(x.get("sku") or "")))

    lines, picked, total = [], [], 0
    for v in eligible:
        if len(lines) >= max_skus:
            skipped.append({**v, "reason": "max_skus"});  continue
        room = max_units - total
        if room < min_per_sku:
            # Qolgan joy eng kichik qatorga ham yetmaydi — qidirishni to'xtatamiz.
            skipped.append({**v, "reason": "max_units"});  continue
        qty = v["qty"]
        if qty > room:
            qty = room                       # oxirgi qator qisman tushadi
        lines.append({
            "skuId": int(v["skuId"]),
            "quantityToStock": int(qty),
            "purchasePrice": int(v["price"]),
        })
        picked.append({**v, "qty": int(qty), "partial": qty < v["qty"]})
        total += qty

    return {"lines": lines, "picked": picked, "totalUnits": total, "skipped": skipped}


def _regroup(picked: list[dict]) -> list[dict]:
    """Bitta накладной qatorlarini MAHSULOT bo'yicha yig'adi (yarim-avtomatdagidek:
    rasm + nom + variantlar)."""
    out: dict[str, dict] = {}
    for v in picked:
        k = v.get("groupKey") or v.get("group") or "—"
        g = out.get(k)
        if g is None:
            g = out[k] = {
                "key": k, "name": v.get("group") or "—",
                "image": v.get("groupImage") or v.get("image") or "",
                "shortSku": v.get("shortSku") or "",
                "sales": 0, "uzum_qty": 0, "wh_qty": 0, "need": 0, "qty": 0,
                "variants": [],
            }
        g["variants"].append(v)
        for f in ("sales", "uzum_qty", "wh_qty", "need", "qty"):
            g[f] += int(v.get(f) or 0)
    for g in out.values():
        g["vcount"] = len(g["variants"])
    return list(out.values())


def build_auto_draft(shop_uzum_id: str, cfg: dict,
                     date_from: str | None = None,
                     date_to: str | None = None) -> dict:
    """build_plan() + pack_invoices() — «nima jo'natiladi» to'liq ko'rinishi.

    Sotuv oynasi: `cfg["salesDays"]` chipi, yoki `date_from`+`date_to` (kalendar,
    ustun turadi) — yarim-avtomatdagi bilan bir xil shartnoma.

    Hech narsa yaratmaydi; UI oldindan ko'rish va /auto/create shuni ishlatadi.
    """
    plan = build_plan(shop_uzum_id, days=int(cfg.get("salesDays") or DEFAULT_DAYS),
                      date_from=date_from, date_to=date_to)
    variants: list[dict] = []
    for g in plan.get("groups") or []:
        for v in g.get("variants") or []:
            variants.append({**v,
                             "group": g.get("name") or "",
                             "groupKey": g.get("key") or "",
                             "groupImage": g.get("image") or "",
                             "shortSku": g.get("short_sku") or ""})

    packed = pack_invoices(variants, cfg)
    for inv in packed["invoices"]:
        inv["groups"] = _regroup(inv["picked"])
        inv.pop("picked", None)

    d_from, d_to = slot_window(cfg)
    packed.update({
        # Sotuv oynasi — SERVER hisoblaganidek (teskari oraliq/chegaralar u yerda).
        "salesDays": plan.get("days"),
        "salesFrom": plan.get("date_from"),
        "salesTo": plan.get("date_to"),
        "salesCustom": bool(plan.get("custom")),
        "whSource": plan.get("wh_source"),
        "slotFromDate": d_from.isoformat(),
        "slotToDate": d_to.isoformat(),
        "candidates": len(variants),
    })
    return packed
