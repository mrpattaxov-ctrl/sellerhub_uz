"""JONLI PROBE — «Yangi tovar» razmer-shkala cheklovini aniqlash (read+create).

Maqsad: Uzum aynan qanday xato berishini o'lchash, KOD YOZISHDAN OLDIN. Bu
skript HAQIQIY qoralamalar yaratadi (moderatsiyaga ketmaydi, kabinetda
«to'ldirilmagan» turadi — keyin qo'lda o'chirsa bo'ladi).

Ishga tushirish (VPS, docker konteyneri ichida — token/DB/LOUIS shu yerda):
    docker compose -f docker-compose.vps.yml exec app python scripts/nt_size_probe.py

Sinaladigan holatlar (do'kon 40571, kategoriya 11894 = Кольца, 2 razmer-shkala):
    S0  rang(1) + shkalaA(1)                 → nazorat, 201 kutiladi
    A   rang(1) + shkalaA(1) + shkalaB(1)    → 2 SHKALA aralash → xato kutiladi
    B   rang(1) + shkalaA(2 qiymat)          → 1 shkala ichida 2 razmer → ??
    C   rang(1) + razmersiz                  → majburiymi? → ??
Har biriga HTTP status + xato tanasi chop etiladi.
"""
from __future__ import annotations

import io
import json
import sys

sys.path.insert(0, ".")

from noviy_tavar import client
from noviy_tavar.client import NoviyTavarError

SHOP = "40571"
CATEGORY = 11894   # Аксессуары / Бижутерные украшения / Кольца (2 razmer-shkala)


def _log(*a):
    print(*a, flush=True)


def _make_image_bytes() -> tuple[bytes, str, str]:
    """1080x1440 oq JPEG (Uzum tavsiya o'lchamiga mos). PIL bo'lmasa — kichik JPEG."""
    try:
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (1080, 1440), (240, 240, 240)).save(buf, format="JPEG", quality=85)
        return buf.getvalue(), "probe.jpg", "image/jpeg"
    except Exception as e:
        _log("  [img] PIL yo'q, kichik JPEG ishlatiladi:", e)
        import base64
        tiny = base64.b64decode(
            "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAP//////////////////////////////"
            "////////////////////////////////////////////////////////wAALCA"
            "AIAAgBAREA/8QAFAABAAAAAAAAAAAAAAAAAAAAA//EABQQAQAAAAAAAAAAAAAAAAA"
            "AAAD/2gAIAQEAAT8Qf//Z"
        )
        return tiny, "probe.jpg", "image/jpeg"


def _find_chars(meta: dict):
    chars = meta.get("characteristics") or []
    if not isinstance(chars, list):
        _log("  [meta] characteristics ro'yxat emas:", type(chars))
        chars = []
    color = None
    scales = []
    for ch in chars:
        rt = ch.get("requiredType")
        vals = ch.get("characteristicValues") or []
        if rt == "REQUIRED" and not color:
            color = ch
        elif rt == "REQUIRED_ONE_OF_SIZE" and vals:
            scales.append(ch)
    return color, scales


def _char_sel(ch: dict, values: list) -> dict:
    return {
        "characteristicId": ch.get("characteristicId", -1),
        "characteristicTitle": ch.get("characteristicTitle") or {},
        "orderingNumber": ch.get("orderingNumber", 0),
        "requiredType": ch.get("requiredType") or "NOT_REQUIRED",
        "flowA": bool(ch.get("flowA", False)),
        "values": values,
    }


def _attempt(name: str, image: dict, chars_sel: list):
    _log(f"\n=== {name} :: xususiyatlar={[ (c['characteristicTitle'].get('ru'), len(c['values'])) for c in chars_sel ]}")
    body = client.build_create_body(
        category_id=CATEGORY,
        title_uz="Probe uzuk", title_ru="Probe кольцо",
        short_uz="", short_ru="",
        desc_uz="Test", desc_ru="Тест",
        filter_values_sel=[],
        characteristics_sel=chars_sel,
        images=[image],
        product_fields={},
    )
    try:
        res = client.create_product(SHOP, body)
        pid = res.get("id") if isinstance(res, dict) else None
        _log(f"  → 201 OK, qoralama id={pid}  (bu qoralama kabinetda qoladi)")
    except NoviyTavarError as e:
        _log(f"  → XATO HTTP {e.http_status}")
        _log(f"     body: {e.body[:400]}")


def main():
    _log(f"PROBE shop={SHOP} category={CATEGORY}")
    meta = client.category_meta(SHOP, CATEGORY)
    color, scales = _find_chars(meta)
    _log("REQUIRED(rang):", (color or {}).get("characteristicTitle"))
    _log("ONE_OF_SIZE shkalalar:", [s.get("characteristicTitle", {}).get("ru") for s in scales])
    if not color or len(scales) < 2:
        _log("!! Kutilgan tuzilma yo'q (rang + 2 shkala). To'xtatildi.")
        _log("   meta.characteristics turlari:",
             [(c.get("characteristicTitle", {}).get("ru"), c.get("requiredType"),
               len(c.get("characteristicValues") or [])) for c in (meta.get("characteristics") or [])])
        return

    cval = (color.get("characteristicValues") or [])[:1]
    scaleA = scales[0]
    scaleB = scales[1]
    aVals = scaleA.get("characteristicValues") or []
    bVals = scaleB.get("characteristicValues") or []
    _log(f"shkalaA='{scaleA['characteristicTitle'].get('ru')}' ({len(aVals)} qiymat), "
         f"shkalaB='{scaleB['characteristicTitle'].get('ru')}' ({len(bVals)} qiymat)")

    _log("\n[img] rasm yuklanmoqda...")
    b, fn, mt = _make_image_bytes()
    up = client.upload_image(b, fn, mt)
    payload = (up.get("payload") or {}) if isinstance(up, dict) else {}
    image = {"key": payload.get("key"), "url": payload.get("originalUrl")}
    _log("  rasm key:", image["key"])
    if not image["key"]:
        _log("!! rasm yuklanmadi, to'xtatildi:", json.dumps(up, ensure_ascii=False)[:300])
        return

    color_sel = _char_sel(color, cval)

    # S0 nazorat: rang + 1 shkala, 1 qiymat
    _attempt("S0 nazorat (rang + shkalaA×1)", image,
             [color_sel, _char_sel(scaleA, aVals[:1])])

    # A: 2 shkala aralash (asosiy test)
    _attempt("A 2-SHKALA (rang + shkalaA×1 + shkalaB×1)", image,
             [color_sel, _char_sel(scaleA, aVals[:1]), _char_sel(scaleB, bVals[:1])])

    # B: 1 shkala ichida 2 qiymat
    if len(aVals) >= 2:
        _attempt("B 1-SHKALA×2-QIYMAT (rang + shkalaA×2)", image,
                 [color_sel, _char_sel(scaleA, aVals[:2])])

    # C: razmersiz (faqat rang)
    _attempt("C RAZMERSIZ (faqat rang)", image, [color_sel])

    _log("\nPROBE tugadi. Yuqoridagi 201/xato natijalarini menga tashlang.")


if __name__ == "__main__":
    main()
