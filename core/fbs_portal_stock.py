"""«Ombor» READ path — Uzum PORTAL (ichki) API: server-side qidiruv bilan.

Nega portal, OpenAPI emas: OpenAPI ``GET /v3/fbs/sku/stocks`` da QIDIRUV YO'Q
(probe 2026-07) — qidirish uchun butun katalogni (23 sahifa, ~11s) aylanishga
majbur edik. Uzum'ning o'z «Склад» sahifasi esa ichki endpoint ishlatadi
(HAR «Поставки22+», 2026-07-03)::

    GET https://api-seller.uzum.uz/api/v1/seller/stock/sku
        ?sellerId=&shopIds=a,b,c&searchText=&amountFilter=&sortBy=SKU_ID
        &page=0&size=20

Probe bilan tasdiqlangan qoidalar (2026-07-03):
  * ``size`` QAT'IY 20 — 21 ham HTTP 400 (invoice-list'dagi kabi qotirilgan).
  * ``amountFilter`` enum HTTP 200 beradiganlari: ``ALL`` / ``IN_STOCK`` /
    ``SOLD_OUT``. LEKIN:
      - ``IN_STOCK`` = amount>0 (TO'G'RI — «Mavjud» segmenti shu).
      - ``SOLD_OUT`` ≠ «amount=0»! U «qo'lda nolga tushirilgan» TOR ro'yxat
        (``zeroReason=SELLER_MANUAL``, ~15 ta) — 1200 ta amount=0 EMAS.
        Shuning uchun ``SOLD_OUT`` ISHLATILMAYDI; «Tugagan» (amount==0) ni
        chaqiruvchi ``ALL``dan client-side ajratadi.
  * ``searchText`` nom / SKU-kod / shtrix-kod bo'yicha ishlaydi (server-side).
  * ``linked`` param ixtiyoriy — tashlab yuborilsa HAMMA SKU keladi.
  * javob ``payload.hasMore`` DOIM False (Uzum UI ham ishlatmaydi) —
    ISHONMANG; «to'liq sahifa keldi (20) = yana bor» qoidasi ishlatiladi.
  * javob qatorida per-SKU rasm (``photo``) tayyor keladi — lokal Variant
    join'iga hojat yo'q.

Auth — postavki moduli bilan bir xil: admin browser-token (``_get_admin_token``,
AutoLogin har 90 daqiqada yangilaydi). OpenAPI token EMAS. Do'kon-scope
chaqiruvchida hal qilinadi: ``shop_uzum_ids`` ga FAQAT foydalanuvchining o'z
do'konlari beriladi — server shu ro'yxat bo'yicha filtrlaydi.

YOZISH bu yerda YO'Q — stok saqlash OpenAPI ``POST /v2/fbs/sku/stocks`` da
qoladi (core.uzum_openapi.update_fbs_sku_stocks).
"""
from __future__ import annotations

from urllib.parse import quote

PORTAL_STOCK_URL = "https://api-seller.uzum.uz/api/v1/seller/stock/sku"

# Server sahifa o'lchami — qotirilgan (probe: 21 → 400).
PORTAL_STOCK_PAGE_SIZE = 20

# Faqat shu ikkitasi ishlatiladi: IN_STOCK (amount>0, «Mavjud») va ALL.
# «Tugagan» (amount==0) uchun SOLD_OUT YARAMAYDI (yuqoridagi izohga qarang) —
# u ALL'dan chaqiruvchi tomonda ajratiladi.


def _thumb_from_photo(photo) -> str | None:
    """Portal ``photo`` obyektidan grid uchun kichik rasm URL'i.

    Shakli: ``{"photo": {"240": {"high":…, "low":…}, "800": {…}, …}}`` —
    o'lcham kalitlari katalogga qarab farq qiladi, shuning uchun kichigidan
    kattasiga qarab birinchi borini olamiz.
    """
    try:
        sizes = (photo or {}).get("photo") or {}
        for key in ("240", "480", "540", "720", "800"):
            v = sizes.get(key)
            if isinstance(v, dict) and (v.get("high") or v.get("low")):
                return v.get("high") or v.get("low")
        for v in sizes.values():
            if isinstance(v, dict) and (v.get("high") or v.get("low")):
                return v.get("high") or v.get("low")
    except Exception:
        pass
    return None


def normalize_portal_sku(s: dict) -> dict:
    """Portal qatorini frontend kutadigan (OpenAPI'nikiga mos) shaklga keltiradi.

    Frontend va POST-saqlash oqimi OpenAPI dala nomlarida ishlaydi
    (skuId/fbsAllowed/…) — portal esa id/availableFbs/… qaytaradi.
    """
    return {
        "skuId": s.get("id"),
        "skuTitle": s.get("skuTitle"),
        "productTitle": s.get("productTitle"),
        "barcode": s.get("barcode"),
        "amount": int(s.get("amount") or 0),
        "fbsLinked": bool(s.get("fbsLinked")),
        "dbsLinked": bool(s.get("dbsLinked")),
        "fbsAllowed": bool(s.get("availableFbs")),
        "dbsAllowed": bool(s.get("availableDbs")),
        "image": _thumb_from_photo(s.get("photo")),
    }


def fetch_portal_sku_stocks_page(
    *,
    seller_id: int,
    shop_uzum_ids: list[str],
    page: int = 0,
    search: str = "",
    in_stock_only: bool = False,
) -> tuple[list[dict], bool]:
    """BITTA portal sahifa (≤20 qator, normalize qilingan) + has_more.

    ``in_stock_only`` → ``amountFilter=IN_STOCK`` (faqat amount>0), aks holda
    ``ALL``. «Tugagan» (amount==0) chaqiruvchi tomonda ALL'dan ajratiladi —
    portalning ``SOLD_OUT``i noto'g'ri (modul izohiga qarang).

    ``shop_uzum_ids`` bo'sh bo'lsa hech narsa so'ramaymiz (scope bo'sh) —
    server butun seller bo'yicha qaytarib yuborishining oldini oladi.
    Xatolar RuntimeError bo'lib ko'tariladi (http_json shunday) — chaqiruvchi
    ushlaydi.
    """
    from core.http_client import http_json
    from core.auth_helpers import _get_admin_token

    if not shop_uzum_ids:
        return ([], False)
    amount_filter = "IN_STOCK" if in_stock_only else "ALL"
    url = (
        f"{PORTAL_STOCK_URL}?sellerId={int(seller_id)}"
        f"&shopIds={','.join(str(s) for s in shop_uzum_ids)}"
        f"&amountFilter={amount_filter}&sortBy=SKU_ID"
        f"&page={max(0, int(page))}&size={PORTAL_STOCK_PAGE_SIZE}"
    )
    search = (search or "").strip()
    if search:
        url += f"&searchText={quote(search, safe='')}"
    data = http_json(url, method="GET", _get_admin_token=_get_admin_token)
    raw = ((data.get("payload") or {}).get("skus")) if isinstance(data, dict) else []
    raw = [s for s in (raw or []) if isinstance(s, dict) and s.get("id") is not None]
    rows = [normalize_portal_sku(s) for s in raw]
    # hasMore'ga ishonmaymiz (doim False) — to'liq sahifa = davomi bor.
    has_more = len(raw) >= PORTAL_STOCK_PAGE_SIZE
    return (rows, has_more)
