"""Per-shop OpenAPI finance fetchers + row normalizers.

Bridges the new ``/v1/finance/orders`` and ``/v1/finance/expenses`` endpoints
to the existing ``sales_lines`` / ``expenses_ledger`` ingest pipeline.

Design choice
-------------
The downstream ingest functions (``_ingest_sales_lines_window``,
``_ingest_expenses_window_for_shop``) accept ROW DICTS keyed by the canonical
field names produced by the RU→canonical CSV header map. To minimise blast
radius this module produces dicts with the *exact same keys* so the ingest
functions don't have to grow a separate code path — they keep parsing the
same canonical schema regardless of source.

Additions on top of the CSV shape
---------------------------------
OpenAPI exposes three fields the CSV never had. We pass them along as extra
keys; the ingest function picks them up and writes them to the corresponding
new columns (see migration 20260520_0004):

  * ``shop_id``  — int. CSV never carried this (one report per shop run).
                   With OpenAPI it's right on the row, so we skip the SKU→
                   shop catalog lookup downstream.
  * ``product_image`` — JSON-serializable dict. The raw ``productImage``
                   object Uzum returns. Saved verbatim in
                   ``sales_lines.product_image``.
  * ``qty_cancelled`` — int. The OpenAPI ``cancelled`` field. CSV encoded
                   cancellations as their own ``Отменен`` rows that we drop
                   at ingest, so this stays 0 in the CSV path.

For expenses, additions: ``date_created``, ``date_updated``, ``seller_id``,
``external_id``, ``code``. ``op_type`` is translated from the OpenAPI enum
``OUTCOME``/``INCOME`` to the Russian strings the downstream notification
code already understands (``"Оплата"``/``"Возврат"``).

Status mapping
--------------
OpenAPI orders return a status enum:
  TO_WITHDRAW, PROCESSING, CANCELED, PARTIALLY_CANCELLED.
The ingest pipeline drops rows where ``status == "Отменен"``. We translate
``CANCELED`` (and only CANCELED — PARTIALLY_CANCELLED still has surviving
items) to ``"Отменен"`` so the existing filter rule applies unchanged.

Date semantics
--------------
Uzum's swagger says ``dateFrom``/``dateTo`` are milliseconds. It's wrong —
the API parses them as **seconds**. Always convert via
``int(dt.timestamp())`` after attaching ``APP_TZ``, NOT ``int(dt.timestamp() * 1000)``.

Pagination
----------
We walk pages until either (a) the API returns fewer rows than requested, or
(b) we hit a safety cap. ``size=100`` is the sweet spot Uzum's rate-limit
(2 burst / 2/sec replenish / 100k/day) tolerates well.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Iterable, Mapping

from config import APP_TZ
from core import uzum_openapi as _api

# Sanity ceiling so a runaway loop can't drain the entire daily budget on
# one shop. ~5M rows max per fetch call which is more than enough for any
# real shop / window.
_MAX_PAGES = int(50_000)
_PAGE_SIZE_DEFAULT = 100

# Between-page sleep. The token bucket is 2 requests/sec replenish, so a
# 0.55s gap keeps a comfortable margin and avoids triggering the burst cap.
_BETWEEN_PAGE_SLEEP_S = 0.55


# ── Time conversion helpers ───────────────────────────────────────────

def _tashkent_naive_to_epoch_sec(dt: datetime) -> int:
    """Attach APP_TZ to a naive Tashkent datetime, return Unix-epoch seconds."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=APP_TZ)
    return int(dt.timestamp())


def _epoch_ms_to_tashkent_naive(ms: int | None) -> datetime | None:
    """Convert an Uzum-returned epoch-millisecond timestamp to naive Tashkent."""
    if ms is None:
        return None
    try:
        ms_int = int(ms)
    except (TypeError, ValueError):
        return None
    if ms_int <= 0:
        return None
    return (
        datetime.fromtimestamp(ms_int / 1000, APP_TZ)
        .replace(tzinfo=None)
    )


def _parse_isoish_to_tashkent_naive(s: str | None) -> datetime | None:
    """Parse Uzum-returned ISO-ish date-time strings to naive Tashkent.

    Uzum returns either ``"2026-05-20T05:56:29.767541974"`` (no tz, microsecond
    overflow) or ``"2026-05-20T05:56:29Z"``. They appear to be UTC for
    ``dateCreated``/``dateUpdated``/``dateService`` per the swagger
    ``format: date-time``. We parse, treat as UTC, then convert to Tashkent
    naive so it's directly comparable to the rest of the column.
    """
    if not s:
        return None
    raw = str(s).strip()
    if not raw:
        return None
    # Strip trailing Z if present so fromisoformat() can handle it.
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    # Truncate microsecond overflow ("767541974" → "767541").
    if "." in raw:
        head, _, tail = raw.partition(".")
        # Tail may end in tz suffix like "+00:00"; preserve that.
        if "+" in tail:
            frac, _, tz_suf = tail.partition("+")
            tz_suf = "+" + tz_suf
        elif "-" in tail:
            frac, _, tz_suf = tail.partition("-")
            tz_suf = "-" + tz_suf
        else:
            frac, tz_suf = tail, ""
        raw = f"{head}.{frac[:6]}{tz_suf}"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # No tz info — Uzum's behavior here is undocumented; assume UTC.
        from datetime import timezone
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(APP_TZ).replace(tzinfo=None)


# ── Row normalizers ──────────────────────────────────────────────────

def _coerce_int(val) -> int:
    if val is None:
        return 0
    try:
        return int(val)
    except (TypeError, ValueError):
        try:
            return int(float(val))
        except (TypeError, ValueError):
            return 0


def _coerce_str(val) -> str:
    if val is None:
        return ""
    return str(val)


# Map OpenAPI `status` enum → the Russian string the existing ingest
# uses. CANCELED → "Отменен" makes the existing drop-cancelled-at-ingest
# rule (coder rule §2) apply transparently.
_ORDER_STATUS_RU = {
    "TO_WITHDRAW": "К выводу",
    "PROCESSING": "В обработке",
    "CANCELED": "Отменен",
    "PARTIALLY_CANCELLED": "Частично отменен",
}

# Map OpenAPI expense `type` enum → RU op_type the existing read/notify
# code already filters on ("Оплата" outflow, "Возврат" income).
_EXPENSE_TYPE_RU = {
    "OUTCOME": "Оплата",
    "INCOME": "Возврат",
}


def _openapi_order_to_canonical(row: Mapping, shop_id_int: int) -> dict | None:
    """Convert a /v1/finance/orders (group=false) row to canonical dict.

    Returns None for rows that are missing the bare minimum needed by the
    sales_lines pipeline (orderId, skuTitle, date). Status mapping below
    drives the existing drop-at-ingest filter.

    Real-data verified surprises:
      * Unit price key is ``sellPrice`` in the response, not ``sellerPrice``
        as the swagger states.
      * ``productTitle`` is reliably null on PROCESSING rows; we still write
        whatever's there so it lights up on settled rows.
      * ``skuCharTitle``/``skuCharValue`` are absent in practice — the
        characteristic-string lives only inside ``skuTitle``.
    """
    order_id = _coerce_str(row.get("orderId")).strip()
    sku_code = _coerce_str(row.get("skuTitle")).strip()
    created = _epoch_ms_to_tashkent_naive(row.get("date"))
    if not order_id or not sku_code or created is None:
        return None

    status_enum = _coerce_str(row.get("status")).strip().upper()
    status_ru = _ORDER_STATUS_RU.get(status_enum, status_enum or None)

    return {
        # Carries the new shop_id directly — downstream skips the SKU→shop
        # catalog lookup when this is present and trusts it.
        "shop_id": int(shop_id_int),

        # ── Fields matching _SELLS_HEADER_MAP keys ──────────────────
        "status":         status_ru,
        "created_at":     created,
        "received_at":    _epoch_ms_to_tashkent_naive(row.get("dateIssued")),
        "order_id":       order_id,
        "barcode":        None,   # not in OpenAPI orders
        "sku_id":         sku_code,
        "sku_title":      _coerce_str(row.get("productTitle")) or sku_code,
        "category":       None,   # not in OpenAPI orders
        "qty":            _coerce_int(row.get("amount")),
        "qty_returns":    _coerce_int(row.get("amountReturns")),
        # Browser CSV ships pre-computed `revenue`; derive it the same way
        # so downstream queries don't have to special-case the source.
        "revenue":        _coerce_int(row.get("sellPrice")) * _coerce_int(row.get("amount")),
        "seller_profit":  _coerce_int(row.get("sellerProfit")),
        "commission":     _coerce_int(row.get("commission")),
        "unit_price":     _coerce_int(row.get("sellPrice")),
        "promo_amount":   0,      # not in group=false (only in group=true)
        "purchase_price": _coerce_int(row.get("purchasePrice")),
        "logistics_fee": _coerce_int(row.get("logisticDeliveryFee")),

        # ── New OpenAPI-only fields ─────────────────────────────────
        "product_image":  row.get("productImage") if isinstance(row.get("productImage"), dict) else None,
        "qty_cancelled":  _coerce_int(row.get("cancelled")),
        # Surface productId too — downstream ingest already handles it.
        "product_id":     _coerce_int(row.get("productId")) or None,
    }


def _openapi_expense_to_canonical(row: Mapping, shop_id_int: int) -> dict | None:
    """Convert a /v1/finance/expenses payment record to canonical dict."""
    op_id = _coerce_str(row.get("id")).strip()
    if not op_id:
        return None

    type_enum = _coerce_str(row.get("type")).strip().upper()
    op_type_ru = _EXPENSE_TYPE_RU.get(type_enum, type_enum or None)

    charged_at = _parse_isoish_to_tashkent_naive(row.get("dateService"))
    # If dateService is missing, fall back to dateCreated so we still get
    # the row stored — the day bucket is computed from charged_at downstream.
    if charged_at is None:
        charged_at = _parse_isoish_to_tashkent_naive(row.get("dateCreated"))
    if charged_at is None:
        return None

    return {
        "shop_id":      int(shop_id_int),

        # ── Fields matching _EXPENSES_HEADER_MAP keys ───────────────
        "source":       _coerce_str(row.get("source")) or None,
        "service":      _coerce_str(row.get("name")) or None,
        "status":       _coerce_str(row.get("status")) or None,
        "operation_id": op_id,
        "charged_at":   charged_at,
        # Unit cost: paymentPrice / amount when amount > 0 (otherwise 0).
        "unit_cost":    (_coerce_int(row.get("paymentPrice")) // max(_coerce_int(row.get("amount")), 1))
                        if _coerce_int(row.get("amount")) else _coerce_int(row.get("paymentPrice")),
        "qty":          _coerce_int(row.get("amount")),
        "amount":       _coerce_int(row.get("paymentPrice")),
        "op_type":      op_type_ru,

        # ── New OpenAPI-only fields ─────────────────────────────────
        "date_created": _parse_isoish_to_tashkent_naive(row.get("dateCreated")),
        "date_updated": _parse_isoish_to_tashkent_naive(row.get("dateUpdated")),
        "seller_id":    _coerce_int(row.get("sellerId")) or None,
        "external_id":  _coerce_str(row.get("externalId")) or None,
        "code":         _coerce_str(row.get("code")) or None,
    }


# ── Per-shop paginated fetchers ──────────────────────────────────────

def fetch_finance_orders_for_shop_window(
    token: str,
    shop_uzum_id: str | int,
    date_from_tashkent: datetime,
    date_to_tashkent: datetime,
    *,
    size: int = _PAGE_SIZE_DEFAULT,
) -> list[dict]:
    """Walk /v1/finance/orders pages for the given window, return canonical rows.

    Window is **inclusive** on dateFrom and exclusive-ish on dateTo (we shave
    1 second off dateTo because Uzum's filter is inclusive on both sides;
    matches the convention the CSV path uses).
    """
    if date_from_tashkent >= date_to_tashkent:
        return []
    if not token:
        raise RuntimeError("fetch_finance_orders_for_shop_window: empty token")

    shop_int = int(shop_uzum_id)
    date_from_sec = _tashkent_naive_to_epoch_sec(date_from_tashkent)
    date_to_sec = _tashkent_naive_to_epoch_sec(date_to_tashkent) - 1

    out: list[dict] = []
    page = 0
    while page < _MAX_PAGES:
        try:
            body = _api.fetch_finance_orders_page(
                token, shop_int,
                date_from_sec=date_from_sec, date_to_sec=date_to_sec,
                page=page, size=size, group=False,
            )
        except Exception as e:
            print(f"[FinanceOpenAPI] orders fetch failed shop={shop_int} page={page}: {e}")
            raise
        items = body.get("orderItems") if isinstance(body, dict) else None
        items = items if isinstance(items, list) else []
        if page == 0:
            total = body.get("totalElements") if isinstance(body, dict) else None
            print(f"[FinanceOpenAPI] orders shop={shop_int} window=["
                  f"{date_from_tashkent.isoformat()},{date_to_tashkent.isoformat()}) "
                  f"totalElements={total}")

        for raw in items:
            if not isinstance(raw, Mapping):
                continue
            canon = _openapi_order_to_canonical(raw, shop_int)
            if canon is not None:
                out.append(canon)

        if len(items) < size:
            break
        page += 1
        time.sleep(_BETWEEN_PAGE_SLEEP_S)

    return out


# ── Aggregated path (group=true) — feeds FinanceOrder ───────────────
#
# Each call to /v1/finance/orders?group=true returns
#   { "orderItems": [ { productId, shopId, productTitle,
#                       items: [ SkuGroupedSellerItemDto[] ] },
#                     ... ],
#     "totalElements": N (product count) }
# i.e. ONE row per product with an `items` array of per-SKU rollups.
# We flatten to one record per (product, sku) and the daily-aggregate
# ingest in app.py writes one FinanceOrder row per (shop, day, sku_title).
#
# Why group=true and per-day calls (one per day):
#   - Matches the legacy FinanceOrder shape exactly. No code-side
#     aggregation needed; Uzum already returns sums per-SKU for the
#     queried window.
#   - One API call per day × ~1600 days = the cost of the initial
#     backfill. ~25-40 minutes per shop, sequential.

def _pick_image_url(image_obj) -> str | None:
    """Pick a single representative URL from the OpenAPI ``image`` object.

    Each image has ``photo`` keyed by pixel-width (e.g. "800", "240", …)
    → {"high", "low"}. We prefer the 800px "high" variant; fall back to
    other sizes if missing.
    """
    if not isinstance(image_obj, dict):
        return None
    photo = image_obj.get("photo")
    if not isinstance(photo, dict):
        return None
    # Preference order — Uzum normally has all of these for active products.
    for size in ("800", "720", "540", "480", "240", "120", "80"):
        bucket = photo.get(size)
        if isinstance(bucket, dict):
            url = bucket.get("high") or bucket.get("low")
            if isinstance(url, str) and url:
                return url
    return None


def _openapi_grouped_sku_to_finance_order(
    grouped_product: Mapping,
    sku_item: Mapping,
    *,
    shop_uzum_id: str,
    period_day: "date",
) -> dict | None:
    """Convert one product-grouped SKU entry to a FinanceOrder-shaped dict.

    Returns None if the row is missing the bare minimum (sku_title).
    """
    sku_title = _coerce_str(sku_item.get("skuTitle")).strip()
    if not sku_title:
        return None

    # characteristics: array like ["18", "Kumush rang"] → "18, Kumush rang"
    chars = sku_item.get("characteristics")
    chars_str = None
    if isinstance(chars, list) and chars:
        chars_str = ", ".join(_coerce_str(c).strip() for c in chars if c is not None)
        chars_str = chars_str[:300] or None

    sku_id_raw = sku_item.get("skuId") or sku_item.get("sku_id")
    product_id_raw = (
        sku_item.get("productId")
        or grouped_product.get("productId")
        or sku_item.get("product_id")
    )

    product_title = (
        _coerce_str(sku_item.get("productTitle"))
        or _coerce_str(grouped_product.get("productTitle"))
        or ""
    )
    image_url = _pick_image_url(sku_item.get("image"))

    return {
        "shop_id":         str(shop_uzum_id),
        "period_from":     period_day,
        "period_to":       period_day,
        "sku_title":       sku_title[:300],
        "sku_id":          _coerce_int(sku_id_raw) or None,
        "product_id":      _coerce_int(product_id_raw) or None,
        "product_title":   product_title[:500] or None,
        # OpenAPI returns one language at a time (Accept-Language header).
        # The aggregated fetcher calls with no language by default; callers
        # who want a separate RU pass can hit the endpoint a second time.
        "product_title_ru": None,
        "image_url":       (image_url or "")[:800] or None,
        "characteristics": chars_str,
        "amount":          _coerce_int(sku_item.get("amount")),
        "amount_returns":  _coerce_int(sku_item.get("amountReturns")),
        "sell_price":      _coerce_int(sku_item.get("sellPrice")),
        "purchase_price":  _coerce_int(sku_item.get("purchasePrice")),
        "seller_discount": _coerce_int(sku_item.get("sellerDiscountAmount")),
        "seller_profit":   _coerce_int(sku_item.get("sellerProfit")),
        "commission":      _coerce_int(sku_item.get("commission")),
        "withdrawn_profit": _coerce_int(sku_item.get("withdrawnProfit")),
        "logistics_fee":   _coerce_int(sku_item.get("logisticDeliveryFee")),
    }


def fetch_daily_aggregates_for_shop_day(
    token: str,
    shop_uzum_id: str | int,
    day_tashkent: "date",
    *,
    size: int = _PAGE_SIZE_DEFAULT,
) -> list[dict]:
    """Fetch ONE day of group=true aggregates, return FinanceOrder-shaped rows.

    Calls /v1/finance/orders?group=true&dateFrom=<day 00:00>&dateTo=<day 23:59:59>
    (in seconds — Uzum's documented ms is a lie, confirmed live 2026-05-20).
    Walks pages until exhausted.

    Returns a list ready to UPSERT into ``finance_orders`` (one entry per
    SKU sold that day; ``period_from == period_to == day_tashkent``).
    """
    from datetime import datetime as _dt
    from datetime import time as _t

    if not token:
        raise RuntimeError("fetch_daily_aggregates_for_shop_day: empty token")

    shop_int = int(shop_uzum_id)
    day_start = _dt.combine(day_tashkent, _t(0, 0, 0))
    day_end = _dt.combine(day_tashkent, _t(23, 59, 59))
    from_sec = _tashkent_naive_to_epoch_sec(day_start)
    to_sec = _tashkent_naive_to_epoch_sec(day_end)

    out: list[dict] = []
    page = 0
    while page < _MAX_PAGES:
        try:
            body = _api.fetch_finance_orders_page(
                token, shop_int,
                date_from_sec=from_sec, date_to_sec=to_sec,
                page=page, size=size, group=True,
            )
        except Exception as e:
            print(f"[FinanceOpenAPI] daily-aggregate fetch failed "
                  f"shop={shop_int} day={day_tashkent} page={page}: {e}")
            raise

        items = body.get("orderItems") if isinstance(body, dict) else None
        items = items if isinstance(items, list) else []

        for grouped in items:
            if not isinstance(grouped, Mapping):
                continue
            sku_arr = grouped.get("items")
            if not isinstance(sku_arr, list):
                continue
            for sku in sku_arr:
                if not isinstance(sku, Mapping):
                    continue
                row = _openapi_grouped_sku_to_finance_order(
                    grouped, sku,
                    shop_uzum_id=str(shop_uzum_id),
                    period_day=day_tashkent,
                )
                if row is not None:
                    out.append(row)

        if len(items) < size:
            break
        page += 1
        time.sleep(_BETWEEN_PAGE_SLEEP_S)

    return out


def detect_first_sale_year(
    token: str,
    shop_uzum_id: str | int,
    *,
    fallback_year: int = 2022,
) -> int:
    """Probe yearly windows to find the earliest year with any sales.

    Returns the year (int). Used to scope the initial backfill — saves
    walking through years a new shop never operated in. At most one API
    call per year between ``fallback_year`` and the current year.
    """
    from datetime import date as _date, datetime as _dt, time as _t

    if not token:
        return fallback_year

    shop_int = int(shop_uzum_id)
    current_year = _date.today().year

    for year in range(fallback_year, current_year + 1):
        # Window covers the full calendar year in Tashkent.
        ws = _dt.combine(_date(year, 1, 1), _t(0, 0, 0))
        we = _dt.combine(_date(year, 12, 31), _t(23, 59, 59))
        from_sec = _tashkent_naive_to_epoch_sec(ws)
        to_sec = _tashkent_naive_to_epoch_sec(we)
        try:
            body = _api.fetch_finance_orders_page(
                token, shop_int,
                date_from_sec=from_sec, date_to_sec=to_sec,
                page=0, size=1, group=False,
            )
        except Exception as e:
            print(f"[FinanceOpenAPI] detect_first_sale_year shop={shop_int} "
                  f"year={year} probe failed: {e!r} — assuming has sales")
            return year
        total = (body or {}).get("totalElements") if isinstance(body, dict) else None
        if isinstance(total, int) and total > 0:
            return year
        time.sleep(_BETWEEN_PAGE_SLEEP_S)

    return current_year


def fetch_finance_expenses_for_shop_window(
    token: str,
    shop_uzum_id: str | int,
    date_from_tashkent: datetime,
    date_to_tashkent: datetime,
    *,
    size: int = _PAGE_SIZE_DEFAULT,
) -> list[dict]:
    """Walk /v1/finance/expenses pages for the window, return canonical rows."""
    if date_from_tashkent >= date_to_tashkent:
        return []
    if not token:
        raise RuntimeError("fetch_finance_expenses_for_shop_window: empty token")

    shop_int = int(shop_uzum_id)
    date_from_sec = _tashkent_naive_to_epoch_sec(date_from_tashkent)
    date_to_sec = _tashkent_naive_to_epoch_sec(date_to_tashkent) - 1

    out: list[dict] = []
    page = 0
    while page < _MAX_PAGES:
        try:
            body = _api.fetch_finance_expenses_page(
                token, shop_int,
                date_from_sec=date_from_sec, date_to_sec=date_to_sec,
                page=page, size=size,
            )
        except Exception as e:
            print(f"[FinanceOpenAPI] expenses fetch failed shop={shop_int} page={page}: {e}")
            raise

        # Response IS wrapped in `payload` (unlike /v1/finance/orders).
        payload = body.get("payload") if isinstance(body, dict) else None
        items = payload.get("payments") if isinstance(payload, dict) else None
        items = items if isinstance(items, list) else []
        if page == 0:
            total = payload.get("totalElements") if isinstance(payload, dict) else None
            print(f"[FinanceOpenAPI] expenses shop={shop_int} window=["
                  f"{date_from_tashkent.isoformat()},{date_to_tashkent.isoformat()}) "
                  f"totalElements={total}")

        for raw in items:
            if not isinstance(raw, Mapping):
                continue
            canon = _openapi_expense_to_canonical(raw, shop_int)
            if canon is not None:
                out.append(canon)

        if len(items) < size:
            break
        page += 1
        time.sleep(_BETWEEN_PAGE_SLEEP_S)

    return out
