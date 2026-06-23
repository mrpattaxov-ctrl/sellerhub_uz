"""Per-SKU image fetch via Uzum browser endpoint /api/seller/shop/{id}/sku-list.

The Seller OpenAPI ``/v1/product/shop/{shopId}`` returns ``previewImage`` on each
SKU, but upstream that field is frequently empty or stale (see project memory
``openapi_products_migration``). The browser-only ``/sku-list`` endpoint is the
authoritative source the seller UI itself reads, so we fall back to it after
the OpenAPI products sync to fill in per-variant thumbnails.

Token: this endpoint requires the admin's browser token (User.api_key), NOT a
per-user OpenAPI token — they are not interchangeable. Rate-limiting is handled
by the shared TokenBucket in core/http_client.py.
"""
from __future__ import annotations

import re
from typing import Iterable

from sqlalchemy import select

from core.auth_helpers import _get_admin_token
from core.http_client import http_json
from extensions import SessionLocal
from models import ProductGroup, Variant


_IMG_KEYS_PRIMARY = ("image", "imageUrl", "img", "photo", "picture", "pic")
_IMG_KEYS_PREVIEW = (
    "previewImage", "previewImg", "preview_image", "preview",
    "previewPhoto", "preview_photo",
)
_IMG_KEYS_ARRAY = ("images", "photos", "pictureUrls", "productImages", "previewImages")


def _pick_image_url(row: dict) -> str | None:
    """Extract the best image URL from a /sku-list row, mirroring app.py::_extract_variant_image."""
    if not isinstance(row, dict):
        return None
    for k in _IMG_KEYS_PRIMARY + _IMG_KEYS_PREVIEW:
        v = row.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, list) and v:
            first = v[0]
            if isinstance(first, str) and first.strip():
                return first.strip()
            if isinstance(first, dict):
                for kk in ("url", "src", "image", "photo"):
                    vv = first.get(kk)
                    if isinstance(vv, str) and vv.strip():
                        return vv.strip()
    for k in _IMG_KEYS_ARRAY:
        v = row.get(k)
        if isinstance(v, list) and v:
            first = v[0]
            if isinstance(first, str) and first.strip():
                return first.strip()
            if isinstance(first, dict):
                for kk in ("url", "src", "image", "photo"):
                    vv = first.get(kk)
                    if isinstance(vv, str) and vv.strip():
                        return vv.strip()
    return None


# Matches the trailing /t_product_<filename>.<ext> segment Uzum CDN paths end
# with. Real-world variants observed: ``t_product_540_high.jpg`` (sharp),
# ``t_product_240_high.jpg`` (medium), ``t_product_80_low.jpg`` (tiny). The
# pattern intentionally allows ANY suffix between t_product_ and the
# extension so future renditions like ``_medium``/``_origin`` are also
# rewritten — Uzum serves the same image path at multiple sizes/qualities.
_T_SIZE_RE = re.compile(r"/t_product_[\w-]+\.(jpe?g|png|webp)$", re.IGNORECASE)


def _normalize_uzum_cdn(url: str | None) -> str | None:
    """Force images.uzum.uz URLs to the 540px high-quality rendition.

    Verified empirically: ``.../t_product_80_low.jpg`` (~830 B) and
    ``.../t_product_540_high.jpg`` (~46 KB) point to the same image at
    different qualities. The seller UI itself uses 540 for thumbnails — we
    do the same so the variants/POS/invoice tables don't look pixelated.
    URLs that lack any /t_product_*.* suffix get one appended.
    """
    if not url:
        return None
    if "images.uzum.uz" not in url:
        return url
    if _T_SIZE_RE.search(url):
        return _T_SIZE_RE.sub("/t_product_540_high.jpg", url)
    if "/t_" not in url:
        return url.rstrip("/") + "/t_product_540_high.jpg"
    return url


def fetch_sku_images_for_shop(
    shop_uzum_id: str,
    admin_token: str | None = None,
    *,
    size: int = 200,
    max_pages: int = 200,
    search: str = "",
) -> dict[str, dict[str, str]]:
    """Paginate /sku-list and return image URLs keyed for variant matching.

    Returns a dict with three lookup tables:

        {
          "by_sku_id":    {"<uzum_sku_id>": "<url>", ...},
          "by_barcode":   {"<barcode>":     "<url>", ...},
          "by_sku_title": {"<sku_title>":   "<url>", ...},
        }

    Variants are matched in that priority order downstream (same priority the
    OpenAPI sync uses when upserting). Empty/None image URLs are dropped so a
    later "no image returned" never wipes an existing one.
    """
    token = (admin_token or _get_admin_token() or "").strip()
    if not token:
        raise RuntimeError("No admin Uzum token available for /sku-list fetch")

    headers = {
        "Authorization": token if token.startswith("Bearer ") else f"Bearer {token}",
        "Origin": "https://seller.uzum.uz",
        "Referer": "https://seller.uzum.uz/",
    }

    by_sku_id: dict[str, str] = {}
    by_barcode: dict[str, str] = {}
    by_sku_title: dict[str, str] = {}
    # Cost price («себестоимость» / purchasePrice) lives on the SAME /sku-list
    # rows, catalog-wide and independent of any sale — so we harvest it in the
    # same pagination pass (no extra API calls). Unlike images, cost is captured
    # for EVERY row, even ones with no image.
    cost_by_sku_id: dict[str, int] = {}
    cost_by_barcode: dict[str, int] = {}
    cost_by_sku_title: dict[str, int] = {}

    page = 0
    while True:
        url = (
            f"https://api-seller.uzum.uz/api/seller/shop/{shop_uzum_id}"
            f"/sku-list?page={page}&size={size}&search={search}"
        )
        try:
            data = http_json(url, headers=headers)
        except Exception as e:
            print(f"[SkuList] shop={shop_uzum_id} page={page} fetch failed: {e}")
            break

        items: Iterable[dict] = data.get("skuList") or []
        items = list(items)
        if not items:
            break

        for row in items:
            sku_id = str(row.get("skuId") or row.get("id") or "").strip()
            barcode = str(row.get("barcode") or "").strip()
            title = str(
                row.get("skuFullTitle") or row.get("skuTitle")
                or row.get("sku") or ""
            ).strip()

            pp = row.get("purchasePrice")
            if isinstance(pp, (int, float)) and pp > 0:
                pp = int(pp)
                if sku_id:
                    cost_by_sku_id[sku_id] = pp
                if barcode:
                    cost_by_barcode[barcode] = pp
                if title:
                    cost_by_sku_title[title] = pp

            img = _normalize_uzum_cdn(_pick_image_url(row))
            if not img:
                continue
            if sku_id:
                by_sku_id[sku_id] = img
            if barcode:
                by_barcode[barcode] = img
            if title:
                by_sku_title[title] = img

        total_pages = data.get("totalPages")
        if isinstance(total_pages, int) and page + 1 >= total_pages:
            break
        if len(items) < size:
            break
        page += 1
        if max_pages and page >= max_pages:
            break

    return {
        "by_sku_id": by_sku_id, "by_barcode": by_barcode, "by_sku_title": by_sku_title,
        "cost_by_sku_id": cost_by_sku_id, "cost_by_barcode": cost_by_barcode,
        "cost_by_sku_title": cost_by_sku_title,
    }


def apply_sku_images_for_shop(shop_pk: int, image_map: dict[str, dict[str, str]]) -> dict[str, int]:
    """Update Variant.image_url for shop_pk using the three lookup tables.

    Match priority: uzum_sku_id → barcode → sku title. Only updates when the
    new URL is non-empty AND differs from the stored one (avoids needless
    write churn). Returns counters for logging.
    """
    by_sku_id = image_map.get("by_sku_id") or {}
    by_barcode = image_map.get("by_barcode") or {}
    by_sku_title = image_map.get("by_sku_title") or {}

    if not (by_sku_id or by_barcode or by_sku_title):
        return {"updated": 0, "unchanged": 0, "no_match": 0, "scanned": 0}

    updated = 0
    unchanged = 0
    no_match = 0
    scanned = 0

    with SessionLocal() as db:
        variants = db.execute(
            select(Variant).join(ProductGroup).where(ProductGroup.shop_id == shop_pk)
        ).scalars().all()

        for v in variants:
            scanned += 1
            new_url = None
            if v.uzum_sku_id:
                new_url = by_sku_id.get(str(v.uzum_sku_id).strip())
            if not new_url and v.barcode:
                new_url = by_barcode.get(v.barcode.strip())
            if not new_url and v.sku:
                new_url = by_sku_title.get(v.sku.strip())

            if not new_url:
                no_match += 1
                continue
            if v.image_url == new_url:
                unchanged += 1
                continue
            v.image_url = new_url
            updated += 1

        db.commit()

    return {"updated": updated, "unchanged": unchanged, "no_match": no_match, "scanned": scanned}


def apply_sku_costs_for_shop(shop_pk: int, image_map: dict) -> dict[str, int]:
    """Backfill Variant.purchase_price for shop_pk from /sku-list purchasePrice.

    Match priority mirrors images: uzum_sku_id → barcode → sku title. Only fills
    variants whose cost is currently 0/NULL — a user-entered cost is never
    overwritten. Returns counters for logging.
    """
    by_sku_id = image_map.get("cost_by_sku_id") or {}
    by_barcode = image_map.get("cost_by_barcode") or {}
    by_sku_title = image_map.get("cost_by_sku_title") or {}

    if not (by_sku_id or by_barcode or by_sku_title):
        return {"updated": 0, "unchanged": 0, "no_match": 0, "scanned": 0}

    updated = unchanged = no_match = scanned = 0

    with SessionLocal() as db:
        variants = db.execute(
            select(Variant).join(ProductGroup).where(ProductGroup.shop_id == shop_pk)
        ).scalars().all()

        for v in variants:
            scanned += 1
            cost = None
            if v.uzum_sku_id:
                cost = by_sku_id.get(str(v.uzum_sku_id).strip())
            if not cost and v.barcode:
                cost = by_barcode.get(v.barcode.strip())
            if not cost and v.sku:
                cost = by_sku_title.get(v.sku.strip())

            if not cost:
                no_match += 1
                continue
            # Never overwrite a user-entered cost; only fill blanks.
            if (v.purchase_price or 0) > 0:
                unchanged += 1
                continue
            v.purchase_price = int(cost)
            updated += 1

        db.commit()

    return {"updated": updated, "unchanged": unchanged, "no_match": no_match, "scanned": scanned}


def refresh_sku_costs_for_shop(
    shop_uzum_id: str,
    shop_pk: int,
    *,
    admin_token: str | None = None,
    size: int = 200,
    max_pages: int = 200,
) -> dict:
    """Cost-only counterpart of refresh_sku_images_for_shop.

    Fetches /sku-list and backfills blank Variant.purchase_price, WITHOUT
    touching images (images stay on their add-shop-only cadence). Best-effort:
    logs and returns a dict, never raises.
    """
    try:
        sku_map = fetch_sku_images_for_shop(
            shop_uzum_id, admin_token, size=size, max_pages=max_pages,
        )
    except Exception as e:
        print(f"[SkuCost] fetch error for shop {shop_uzum_id}: {e}")
        return {"ok": False, "error": str(e), "source": "fetch"}
    try:
        stats = apply_sku_costs_for_shop(shop_pk, sku_map)
    except Exception as e:
        print(f"[SkuCost] apply error for shop {shop_uzum_id}: {e}")
        return {"ok": False, "error": str(e), "source": "apply"}
    print(f"[SkuCost] shop={shop_uzum_id} pk={shop_pk}: cost_filled={stats['updated']}, scanned={stats['scanned']}")
    return {"ok": True, **stats}


# Public alias: callers in routes/templates use this name. Keeps the private
# helper's signature free to evolve while giving render sites a stable import.
normalize_uzum_image_url = _normalize_uzum_cdn


def refresh_sku_images_for_shop(
    shop_uzum_id: str,
    shop_pk: int,
    *,
    admin_token: str | None = None,
    size: int = 200,
    max_pages: int = 200,
) -> dict:
    """Convenience: fetch from /sku-list and apply to Variants in one call.

    Failures are logged and returned as a dict; never raises. Callers (post
    product sync, post add-shop) treat this as best-effort enrichment.
    """
    try:
        image_map = fetch_sku_images_for_shop(
            shop_uzum_id, admin_token, size=size, max_pages=max_pages,
        )
    except Exception as e:
        print(f"[SkuList] fetch error for shop {shop_uzum_id}: {e}")
        return {"ok": False, "error": str(e), "source": "fetch"}

    fetched = (
        len(image_map.get("by_sku_id", {}))
        + len(image_map.get("by_barcode", {}))
        + len(image_map.get("by_sku_title", {}))
    )
    try:
        stats = apply_sku_images_for_shop(shop_pk, image_map)
    except Exception as e:
        print(f"[SkuList] apply error for shop {shop_uzum_id}: {e}")
        return {"ok": False, "error": str(e), "source": "apply", "fetched_keys": fetched}

    # Backfill blank cost prices from the same payload (best-effort).
    cost_stats = {"updated": 0}
    try:
        cost_stats = apply_sku_costs_for_shop(shop_pk, image_map)
    except Exception as e:
        print(f"[SkuList] cost apply error for shop {shop_uzum_id}: {e}")

    print(
        f"[SkuList] shop={shop_uzum_id} pk={shop_pk}: "
        f"updated={stats['updated']}, unchanged={stats['unchanged']}, "
        f"no_match={stats['no_match']}, scanned={stats['scanned']}, "
        f"cost_filled={cost_stats.get('updated', 0)}"
    )
    return {"ok": True, "fetched_keys": fetched, **stats, "cost_filled": cost_stats.get("updated", 0)}
