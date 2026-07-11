"""Uzum seller-cabinet *marketing / sales* (акции) API wrappers.

These are the browser-cabinet endpoints the seller.uzum.uz frontend itself
calls (base ``https://api-seller.uzum.uz/api/seller/shop/{shopId}/marketing``).
They require the seller's **cabinet** token (User.api_key / admin token) with
``Authorization: Bearer`` + Origin/Referer headers — the per-user OpenAPI token
does NOT work here (same constraint as core/uzum_skulist.py).

Captured from a real session HAR (seller.uzum.uz, shop 5983, 2026-06-13):

    GET /marketing/sales?saleType=ALL&page=&size=
        → {"payload":[{id,title,startDate,finishDate,status,type,
                       imageUrl{uz,ru},suitableProductsCount,involvedProductsCount}]}
    GET /marketing/sales/{saleId}
        → {"payload":{...same... ,"categoryRule":[{categoryId,title,
                       minDiscountPercentage,discountOnCommission}]}}
    GET /marketing/sales/{saleId}/suitable-products?search=&page=&size=&orderBy=&orderDirection=
        → {"payload":{"content":[{productId,title{uz,ru},imageLow,imageHigh,
                       availableCount,purchasePrice,minSellPrice}],totalPages,totalCount}}
    GET /marketing/sales/{saleId}/products?page=&size=
        → {"payload":{"content":[...already-added...],totalPages,totalCount}}

NOTE: adding products to a sale (the write request) was NOT present in the
captured HAR — the seller only browsed. ``add_products_to_sale`` is therefore
a deliberate stub that raises until the exact request is captured, so we never
fire an unverified write against the live marketplace.

``status`` values seen: CREATED (upcoming, open to join), ACTIVE (running),
COMPLETED. ``type``: BIG_SALE.
"""
from __future__ import annotations

import json
import os
import threading
import time

import redis

from core.auth_helpers import _get_admin_token, _get_fresh_api_key
from core.http_client import http_json
from core.redis_client import redis_client

_BASE = "https://api-seller.uzum.uz/api/seller/shop"


class UzumMarketingError(RuntimeError):
    pass


def _resolve_token(token: str | None) -> str:
    """Cabinet token: prefer the current user's, fall back to the admin's."""
    tok = (token or "").strip()
    if not tok:
        try:
            tok = (_get_fresh_api_key() or "").strip()
        except Exception:
            tok = ""
    if not tok:
        tok = (_get_admin_token() or "").strip()
    if not tok:
        raise UzumMarketingError("Нет Uzum-токена кабинета для запроса акций.")
    return tok


def _headers(token: str | None) -> dict:
    tok = _resolve_token(token)
    return {
        "Authorization": tok if tok.startswith("Bearer ") else f"Bearer {tok}",
        "Accept": "application/json",
        "Accept-Language": "ru-RU",
        "Origin": "https://seller.uzum.uz",
        "Referer": "https://seller.uzum.uz/",
    }


def list_sales(
    shop_uzum_id: str | int,
    *,
    token: str | None = None,
    sale_type: str = "ALL",
    page: int = 0,
    size: int = 50,
) -> list[dict]:
    """All sales/campaigns visible to the shop (CREATED / ACTIVE / COMPLETED)."""
    url = (
        f"{_BASE}/{shop_uzum_id}/marketing/sales"
        f"?saleType={sale_type}&page={page}&size={size}"
    )
    data = http_json(url, headers=_headers(token))
    return list(data.get("payload") or [])


def get_sale(shop_uzum_id: str | int, sale_id: int, *, token: str | None = None) -> dict:
    """Single sale detail, including per-category discount rules."""
    url = f"{_BASE}/{shop_uzum_id}/marketing/sales/{sale_id}"
    data = http_json(url, headers=_headers(token))
    return data.get("payload") or {}


def list_suitable_products(
    shop_uzum_id: str | int,
    sale_id: int,
    *,
    token: str | None = None,
    search: str = "",
    size: int = 100,
    max_pages: int = 50,
) -> list[dict]:
    """Every product eligible to be added to the sale (paginated → flat list)."""
    hdrs = _headers(token)
    out: list[dict] = []
    page = 0
    while page < max_pages:
        url = (
            f"{_BASE}/{shop_uzum_id}/marketing/sales/{sale_id}/suitable-products"
            f"?search={search}&page={page}&size={size}"
            f"&orderBy=TITLE&orderDirection=DESC"
        )
        data = http_json(url, headers=hdrs)
        payload = data.get("payload") or {}
        content = list(payload.get("content") or [])
        out.extend(content)
        total_pages = payload.get("totalPages")
        if isinstance(total_pages, int):
            if page + 1 >= total_pages:
                break
        elif len(content) < size:
            break
        if not content:
            break
        page += 1
    return out


def list_sale_products(
    shop_uzum_id: str | int,
    sale_id: int,
    *,
    token: str | None = None,
    size: int = 100,
    max_pages: int = 50,
) -> list[dict]:
    """Products already enrolled in the sale (paginated → flat list)."""
    hdrs = _headers(token)
    out: list[dict] = []
    page = 0
    while page < max_pages:
        url = (
            f"{_BASE}/{shop_uzum_id}/marketing/sales/{sale_id}/products"
            f"?page={page}&size={size}"
        )
        data = http_json(url, headers=hdrs)
        payload = data.get("payload") or {}
        content = list(payload.get("content") or [])
        out.extend(content)
        total_pages = payload.get("totalPages")
        if isinstance(total_pages, int):
            if page + 1 >= total_pages:
                break
        elif len(content) < size:
            break
        if not content:
            break
        page += 1
    return out


def list_suitable_skus(
    shop_uzum_id: str | int,
    sale_id: int,
    product_id: int,
    *,
    token: str | None = None,
) -> list[dict]:
    """Per-SKU price limits for a SUITABLE (not-yet-added) product in a sale.

    Captured 2026-06-15:
        GET /marketing/sales/{saleId}/suitable-products/{productId}/sku
        → {"payload":[{skuId, skuTitle, imageLow/High, availableCount,
                       characteristics, currentSellPrice, recommendedPrice,
                       referencePrice}]}

    ``recommendedPrice`` is the «Не больше X» ceiling (max allowed sale price);
    it's the fair-discount limit and differs per SKU (based on each SKU's recent
    price). The sale price we submit must be ≤ recommendedPrice."""
    url = (
        f"{_BASE}/{shop_uzum_id}/marketing/sales/{sale_id}"
        f"/suitable-products/{product_id}/sku"
    )
    data = http_json(url, headers=_headers(token))
    return list(data.get("payload") or [])


# ──────────────────────────────────────────────────────────────────────
# Per-shop sales dataset + short-lived cache.
#
# The expensive part of the "add to sale" UX is that, per click, we listed
# all sales and then pulled suitable-products + enrolled products + detail
# for EACH joinable sale — a dozen+ rate-limited calls. But that data is
# per-SALE (identical for every product in the shop), so we build it ONCE per
# shop and cache it briefly. Every product click then resolves from the cache
# (a set membership test), instead of re-hitting Uzum.
# ──────────────────────────────────────────────────────────────────────

# Shared (Redis) cache so all gunicorn workers reuse one warm copy — an
# in-process dict rebuilt per worker (~5s, 15 sales × 3 calls each), which is
# what made the expenses "in sale" badges lag on every page load.
_SALES_DS_PREFIX = "uzum:sales_ds:"      # + shop_uzum_id          -> JSON dataset
_SKU_LIMITS_PREFIX = "uzum:sku_limits:"  # + shop:sale:product      -> JSON list
_SALES_CACHE_LOCK = threading.Lock()         # in-worker anti-stampede on cold build
try:
    _SALES_CACHE_TTL = float(os.getenv("UZUM_SALES_CACHE_TTL", "180"))
except (TypeError, ValueError):
    _SALES_CACHE_TTL = 180.0


def _ds_dump(data: dict) -> str:
    """JSON-encode a dataset (sets aren't JSON-serialisable → store as lists)."""
    sales = []
    for s in data.get("sales", []):
        s2 = dict(s)
        s2["suitable_ids"] = sorted(s.get("suitable_ids") or [])
        s2["involved_ids"] = sorted(s.get("involved_ids") or [])
        sales.append(s2)
    return json.dumps({"sales": sales, "purchase_prices": data.get("purchase_prices") or {}})


def _ds_load(raw: str) -> dict:
    data = json.loads(raw)
    for s in data.get("sales", []):
        s["suitable_ids"] = set(s.get("suitable_ids") or [])
        s["involved_ids"] = set(s.get("involved_ids") or [])
    data.setdefault("purchase_prices", {})
    return data

_JOINABLE_STATUSES = ("CREATED", "ACTIVE")
_MAX_JOINABLE_SALES = 15


def build_shop_sales_dataset(shop_uzum_id: str | int, *, token: str | None = None) -> dict:
    """Assemble joinable-sales data for a shop in one pass.

    For each joinable (CREATED/ACTIVE) sale: its meta, min discount, and the
    sets of suitable + enrolled productIds. This is what both the eligible-sales
    and enrolled-products endpoints read from."""
    sales = list_sales(shop_uzum_id, token=token)
    joinable = [s for s in sales if (s.get("status") or "") in _JOINABLE_STATUSES][:_MAX_JOINABLE_SALES]
    out_sales = []
    # Product-level cost price (Uzum's «purchasePrice» — the seller's stated
    # cost), harvested from suitable-products. It's the only place Uzum exposes
    # cost, and it's sale-independent, so we merge it across all joinable sales.
    purchase_prices: dict[str, int] = {}
    for s in joinable:
        sid = s.get("id")
        if sid is None:
            continue
        suitable = list_suitable_products(shop_uzum_id, sid, token=token)
        involved = list_sale_products(shop_uzum_id, sid, token=token)
        for p in suitable:
            pid = p.get("productId")
            pp = p.get("purchasePrice")
            if pid is not None and isinstance(pp, (int, float)) and pp > 0:
                purchase_prices.setdefault(str(pid), int(pp))
        detail = get_sale(shop_uzum_id, sid, token=token)
        mins = [
            c.get("minDiscountPercentage")
            for c in (detail.get("categoryRule") or [])
            if isinstance(c.get("minDiscountPercentage"), (int, float))
        ]
        min_discount = int(max(mins)) if mins else 1
        out_sales.append({
            "id": sid,
            "title": s.get("title"),
            "image_url": s.get("imageUrl") or {},
            "start_date": s.get("startDate"),
            "finish_date": s.get("finishDate"),
            "status": s.get("status"),
            "min_discount": min_discount,
            "suitable_ids": {str(p.get("productId")) for p in suitable if p.get("productId") is not None},
            "involved_ids": {str(p.get("productId")) for p in involved if p.get("productId") is not None},
        })
    return {"sales": out_sales, "purchase_prices": purchase_prices}


def get_shop_sales_dataset(
    shop_uzum_id: str | int,
    *,
    token: str | None = None,
    force: bool = False,
    ttl: float | None = None,
) -> dict:
    """Cached (Redis, shared across workers) wrapper around
    build_shop_sales_dataset. TTL default 180s."""
    key = _SALES_DS_PREFIX + str(shop_uzum_id)
    ttl = int(_SALES_CACHE_TTL if ttl is None else ttl)
    if not force:
        try:
            raw = redis_client.get(key)
            if raw:
                return _ds_load(raw)
        except redis.RedisError:
            pass
    with _SALES_CACHE_LOCK:
        if not force:
            try:
                raw = redis_client.get(key)
                if raw:
                    return _ds_load(raw)
            except redis.RedisError:
                pass
        data = build_shop_sales_dataset(shop_uzum_id, token=token)
        try:
            redis_client.set(key, _ds_dump(data), ex=ttl)
        except redis.RedisError:
            pass
        return data


def peek_shop_sales_dataset(shop_uzum_id: str | int) -> dict | None:
    """Return the cached dataset if present, else None — NEVER builds.

    Used to render enrolled-in-sale state server-side without blocking the page
    on a cold Uzum build; when the cache is cold the client-side refresh fills
    it in."""
    try:
        raw = redis_client.get(_SALES_DS_PREFIX + str(shop_uzum_id))
        return _ds_load(raw) if raw else None
    except redis.RedisError:
        return None


def invalidate_shop_sales_cache(shop_uzum_id: str | int) -> None:
    """Drop a shop's cached dataset (call after a successful add/update)."""
    try:
        redis_client.delete(_SALES_DS_PREFIX + str(shop_uzum_id))
    except redis.RedisError:
        pass


# ──────────────────────────────────────────────────────────────────────
# Per-(shop, sale, product) SKU price-limits cache.
#
# Unlike the per-shop dataset above, the «Не больше X» per-SKU limits are
# product-specific (one live suitable-skus call per product). That call is the
# slow part of opening the "add to sale" modal, so we cache its normalised
# result briefly. The expenses page pre-warms this for the visible products so
# the first modal click pops instantly.
# ──────────────────────────────────────────────────────────────────────

_SKU_LIMITS_CACHE_LOCK = threading.Lock()    # in-worker anti-stampede


def _sku_limits_key(shop_uzum_id, sale_id, product_id) -> str:
    return f"{shop_uzum_id}:{sale_id}:{product_id}"


def build_product_sku_limits(
    shop_uzum_id: str | int,
    sale_id: int,
    product_id: int,
    *,
    token: str | None = None,
) -> list[dict]:
    """Normalised per-SKU price limits for a product in a sale.

    Tries suitable-skus (not-yet-added products); falls back to the enrolled
    products' skuList (already-added products → 404 on suitable-skus). Each row:
    {sku_id, sku_title, characteristics, current_price, max_price,
     reference_price, sale_price, already_in}."""
    out: list[dict] = []
    skus: list[dict] = []
    try:
        skus = list_suitable_skus(shop_uzum_id, sale_id, product_id, token=token)
    except Exception:
        skus = []  # 404 when the product is already enrolled — fall back below
    if skus:
        for sk in skus:
            out.append({
                "sku_id": sk.get("skuId"),
                "sku_title": sk.get("skuTitle"),
                "characteristics": sk.get("characteristics"),
                "image": sk.get("imageHigh") or sk.get("imageLow") or "",
                "current_price": int(sk.get("currentSellPrice") or 0),
                "max_price": int(sk.get("recommendedPrice") or 0),
                "reference_price": int(sk.get("referencePrice") or 0),
                "sale_price": 0,
                "already_in": False,
            })
    else:
        for p in list_sale_products(shop_uzum_id, sale_id, token=token):
            if str(p.get("productId")) != str(product_id):
                continue
            for sk in (p.get("skuList") or []):
                out.append({
                    "sku_id": sk.get("skuId"),
                    "sku_title": sk.get("skuTitle"),
                    "characteristics": sk.get("characteristics"),
                    "image": sk.get("imageHigh") or sk.get("imageLow") or "",
                    "current_price": int(sk.get("currentSellPrice") or 0),
                    "max_price": int(sk.get("maxSuitablePrice") or 0),
                    "reference_price": int(sk.get("currentSellPrice") or 0),
                    "sale_price": int(sk.get("salePrice") or 0),
                    "already_in": True,
                })
            break
    return out


def get_product_sku_limits(
    shop_uzum_id: str | int,
    sale_id: int,
    product_id: int,
    *,
    token: str | None = None,
    force: bool = False,
    ttl: float | None = None,
) -> list[dict]:
    """Cached (Redis) wrapper around build_product_sku_limits (TTL default =
    sales TTL). Empty results are NOT cached, so a transient Uzum error doesn't
    stick."""
    key = _SKU_LIMITS_PREFIX + _sku_limits_key(shop_uzum_id, sale_id, product_id)
    ttl = int(_SALES_CACHE_TTL if ttl is None else ttl)
    if not force:
        try:
            raw = redis_client.get(key)
            if raw:
                return json.loads(raw)
        except redis.RedisError:
            pass
    with _SKU_LIMITS_CACHE_LOCK:
        if not force:
            try:
                raw = redis_client.get(key)
                if raw:
                    return json.loads(raw)
            except redis.RedisError:
                pass
        data = build_product_sku_limits(shop_uzum_id, sale_id, product_id, token=token)
        if data:
            try:
                redis_client.set(key, json.dumps(data), ex=ttl)
            except redis.RedisError:
                pass
        return data


def invalidate_product_sku_limits(shop_uzum_id: str | int, product_id=None) -> None:
    """Drop cached SKU-limits for a product (all its sales), or for the whole
    shop when product_id is None. Call after a successful add/update."""
    try:
        if product_id is None:
            pattern = _SKU_LIMITS_PREFIX + f"{shop_uzum_id}:*"
        else:
            pattern = _SKU_LIMITS_PREFIX + f"{shop_uzum_id}:*:{product_id}"
        keys = list(redis_client.scan_iter(match=pattern, count=300))
        if keys:
            redis_client.delete(*keys)
    except redis.RedisError:
        pass


def add_products_to_sale(
    shop_uzum_id: str | int,
    sale_id: int,
    products: list[dict],
    *,
    token: str | None = None,
) -> dict:
    """Enroll products into a sale (WRITE — sets live discounts).

    Captured contract (seller.uzum.uz HAR, 2026-06-13):

        POST /marketing/sales/{saleId}/products
        {"products":[{"productId":637136,
                      "skuList":[{"skuId":1336571,"newSalePrice":12770}]}]}
        → 200 {"payload":null}

    ``products`` must already be in that exact Uzum shape — a list of
    ``{"productId": int, "skuList": [{"skuId": int, "newSalePrice": int}, ...]}``.
    The discount→price math (and per-SKU price lookup) is done by the caller
    (products/routes.py) which has DB access; this stays a thin API wrapper.
    """
    if not products:
        raise UzumMarketingError("Нет товаров для добавления в акцию.")
    url = f"{_BASE}/{shop_uzum_id}/marketing/sales/{sale_id}/products"
    resp = http_json(url, method="POST", body={"products": products}, headers=_headers(token))
    return {"ok": True, "payload": (resp or {}).get("payload")}


def remove_product_from_sale(
    shop_uzum_id: str | int,
    sale_id: int,
    product_id: int,
    *,
    token: str | None = None,
) -> dict:
    """Remove a product from a sale (WRITE — drops its live discount).

    Captured contract (seller.uzum.uz HAR, 2026-07-07):

        DELETE /marketing/sales/{saleId}/products/{productId}
        (no body — the product is identified entirely by the URL path)
        → 200 {"payload":null}

    Removal is product-level: all of the product's SKUs leave the sale at once,
    mirroring how enrollment works.
    """
    url = f"{_BASE}/{shop_uzum_id}/marketing/sales/{sale_id}/products/{product_id}"
    resp = http_json(url, method="DELETE", headers=_headers(token))
    return {"ok": True, "payload": (resp or {}).get("payload")}


def discounted_price(current_price: int, discount_percent: float) -> int:
    """Sale price = current price minus discount, floored to the nearest 10.

    Mirrors the Uzum cabinet (captured: 12900 @ 1% → 12770). Flooring (vs
    rounding) guarantees the realised discount is never *less* than requested,
    so it always clears the sale's minimum-discount rule.
    """
    if current_price <= 0:
        return 0
    raw = current_price * (1.0 - (discount_percent or 0) / 100.0)
    return max(0, (int(raw) // 10) * 10)
