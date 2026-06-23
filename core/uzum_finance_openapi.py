"""Per-shop OpenAPI finance fetchers + row normalizers.

Bridges ``/v1/finance/orders`` and ``/v1/finance/expenses`` to the
``finance_orders`` / ``expenses_ledger`` ingest pipeline. Module exports:

- ``fetch_daily_aggregates_for_shop_day`` — group=true; one call per day
  returning per-(shop, day, sku) rollups. Feeds ``_ingest_finance_orders_for_day``.
- ``fetch_orders_ungrouped_for_window`` + ``aggregate_line_items_to_finance_orders``
  — group=false line-item fetch + client-side roll-up, used by the
  quarter-chunked backfill (one call per quarter rather than per day).
- ``fetch_finance_expenses_for_shop_window`` — paginated expense fetch.
  Feeds ``_ingest_expenses_window_for_shop``.
- ``detect_first_sale_year`` — yearly probe used to bound the backfill.

Status mapping
--------------
OpenAPI orders return a status enum: TO_WITHDRAW, PROCESSING, CANCELED,
PARTIALLY_CANCELLED. ``CANCELED`` (and only CANCELED — PARTIALLY_CANCELLED
still has surviving items) is translated to the RU label ``"Отменен"`` so
the downstream drop-at-ingest filter applies unchanged. Expense type enum
``OUTCOME``/``INCOME`` maps to ``"Оплата"``/``"Возврат"``.

Date semantics
--------------
Uzum's swagger says ``dateFrom``/``dateTo`` are milliseconds. It's wrong —
the API parses them as **seconds**. Always convert via
``int(dt.timestamp())`` after attaching ``APP_TZ``, NOT ``int(dt.timestamp() * 1000)``.

Pagination
----------
We walk pages until either (a) the API returns fewer rows than requested, or
(b) we hit a safety cap. ``size=100`` is the sweet spot Uzum's rate-limit
(2 burst / 2/sec replenish / 100k/day) tolerates well. The process-wide
``core.http_client.TokenBucket`` enforces the actual pacing.
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


#converts naive tashkent into epoch-milliseconds
def _tashkent_naive_to_epoch_sec(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=APP_TZ)
    return int(dt.timestamp())

#convert an Uzum-returned epoch-millisecond timestamp to naive Tashkent.
def _epoch_ms_to_tashkent_naive(ms: int | None) -> datetime | None:
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


def _parse_uzum_date_field(val) -> datetime | None:
    """Parse a Uzum date field that may be epoch-ms int OR ISO-ish string.

    The expenses swagger documents ``date-time`` (ISO string) but live
    responses return ``dateService`` / ``dateCreated`` / ``dateUpdated``
    as integer epoch-milliseconds (verified 2026-05-23 against shop 10945:
    ``dateService=1779515968928`` = 2026-05-23 05:59:28 UTC). Accept both
    shapes so the parser is robust to whichever form the API returns —
    pure-digit strings are treated as stringified ms-epoch.
    """
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return _epoch_ms_to_tashkent_naive(val)
    s = str(val).strip()
    if not s:
        return None
    # Stringified integer (e.g. "1779515968928") → ms-epoch path.
    if s.lstrip("-").isdigit():
        try:
            return _epoch_ms_to_tashkent_naive(int(s))
        except (TypeError, ValueError):
            return None
    return _parse_isoish_to_tashkent_naive(s)


def _parse_isoish_to_tashkent_naive(s: str | None) -> datetime | None:
    """Parse Uzum-returned ISO-ish date-time strings to naive Tashkent.

    Uzum may return ``"2026-05-20T05:56:29.767541974"`` (no tz, microsecond
    overflow) or ``"2026-05-20T05:56:29Z"`` for some date-time fields.
    Treat naive parses as UTC, convert to Tashkent naive so it's directly
    comparable to the rest of the column. For fields that may also arrive
    as integer epoch-ms, use ``_parse_uzum_date_field`` instead.
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


# Force-anything-into-an-int, or 0 if it can't. Bouncer that never lets bad data in
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

# Force-anything-into-a-string, or "" if it's nothing." Same bouncer, string version
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

#translator for the espenses translates raw JSON variable names into our db variable names
def _openapi_expense_to_canonical(row: Mapping, shop_id_int: int) -> dict | None:
    """Convert a /v1/finance/expenses payment record to canonical dict."""
    op_id = _coerce_str(row.get("id")).strip()
    if not op_id:
        return None

    type_enum = _coerce_str(row.get("type")).strip().upper()
    op_type_ru = _EXPENSE_TYPE_RU.get(type_enum, type_enum or None)

    charged_at = _parse_uzum_date_field(row.get("dateService"))
    # If dateService is missing, fall back to dateCreated so we still get
    # the row stored — the day bucket is computed from charged_at downstream.
    if charged_at is None:
        charged_at = _parse_uzum_date_field(row.get("dateCreated"))
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
        "date_created": _parse_uzum_date_field(row.get("dateCreated")),
        "date_updated": _parse_uzum_date_field(row.get("dateUpdated")),
        "seller_id":    _coerce_int(row.get("sellerId")) or None,
        "external_id":  _coerce_str(row.get("externalId")) or None,
        "code":         _coerce_str(row.get("code")) or None,
    }


# ── Per-shop paginated fetchers ──────────────────────────────────────


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

#translater of the raw json variable names to our tables variable names to code to understand
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

#fetchng wth group=true for daily aggregates, one call per day, returns per-(shop, day, sku) rollups
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




# --------- Ungrouped + client-side aggregation backfill path---------------


_LARGE_PAGE_SIZE_DEFAULT = 10000

#fetching with group=False to get ungrouped finnce data used in burst fetch and in nighly fetch.
def fetch_orders_ungrouped_for_window(
    token: str,
    shop_uzum_id: str | int,
    date_from_tashkent: datetime,
    date_to_tashkent: datetime,
    *,
    size: int = _LARGE_PAGE_SIZE_DEFAULT,
) -> list[dict]:
    """Walk /v1/finance/orders pages with ``group=false`` for the given window.

    Returns RAW line item dicts (one per order), not aggregated. Pair with
    :func:`aggregate_line_items_to_finance_orders` to produce FinanceOrder
    rows for ingest.

    Uzum's ``dateFrom``/``dateTo`` are SECONDS (swagger says ms, swagger
    lies). Window is inclusive on both ends.
    """
    if date_from_tashkent >= date_to_tashkent:
        return []
    if not token:
        raise RuntimeError("fetch_orders_ungrouped_for_window: empty token")

    shop_int = int(shop_uzum_id)
    from_sec = _tashkent_naive_to_epoch_sec(date_from_tashkent)
    to_sec = _tashkent_naive_to_epoch_sec(date_to_tashkent) - 1

    out: list[dict] = []
    page = 0
    while page < _MAX_PAGES:
        try:
            body = _api.fetch_finance_orders_page(
                token, shop_int,
                date_from_sec=from_sec, date_to_sec=to_sec,
                page=page, size=size, group=False,
            )
        except Exception as e:
            print(f"[FinanceOpenAPI] ungrouped fetch failed "
                  f"shop={shop_int} window=[{date_from_tashkent.isoformat()},"
                  f"{date_to_tashkent.isoformat()}) page={page}: {e}")
            raise

        items = body.get("orderItems") if isinstance(body, dict) else None
        items = items if isinstance(items, list) else []
        if page == 0:
            total = body.get("totalElements") if isinstance(body, dict) else None
            print(f"[FinanceOpenAPI] ungrouped shop={shop_int} window=["
                  f"{date_from_tashkent.isoformat()},{date_to_tashkent.isoformat()}) "
                  f"totalElements={total} size={size}")

        out.extend(it for it in items if isinstance(it, Mapping))

        if len(items) < size:
            break
        page += 1
        # No between-page sleep — global TokenBucket in core.http_client
        # throttles cooperatively across all callers.

    return out

#takes raw lines fetched with group=false with fetch_orders_ungrouped_for_window function 
#and turns it into grouped
def aggregate_line_items_to_finance_orders(
    line_items: Iterable[Mapping],
    shop_uzum_id: str | int,
) -> list[dict]:
    """Group raw line items by (sku_title, day_tashkent) → FinanceOrder dicts.

    Output shape matches :func:`_openapi_grouped_sku_to_finance_order` so it
    feeds directly into ``_ingest_finance_orders_for_day``.

    Verified field-by-field against the group=true aggregated response for
    shop=5983 day=2026-05-20: 216/216 SKUs match across all numeric fields
    AND across cancelled/return-only edge cases.

    Aggregation rules (from the verification):
      * amount / amountReturns / sellerProfit / commission /
        logisticDeliveryFee / sellerDiscountAmount / withdrawnProfit
            → direct sum across line items.
      * sellPrice (per-unit) → sum(sellPrice × amount) gives REVENUE.
      * purchasePrice (per-unit cost basis) → sum(purchasePrice × amount).
        Return-only rows (amount=0) naturally contribute 0.

    All statuses (PROCESSING / TO_WITHDRAW / CANCELED /
    PARTIALLY_CANCELLED) are INCLUDED — Uzum's group=true aggregates them
    all, so we must too to match exactly.
    """
    shop_str = str(shop_uzum_id)

    # Per-(sku_title, day) accumulator
    groups: dict[tuple[str, "date"], dict] = {}

    for item in line_items:
        if not isinstance(item, Mapping):
            continue
        sku_title = _coerce_str(item.get("skuTitle")).strip()
        if not sku_title:
            continue
        day_dt = _epoch_ms_to_tashkent_naive(item.get("date"))
        if day_dt is None:
            continue
        day = day_dt.date()

        key = (sku_title, day)
        g = groups.get(key)
        if g is None:
            g = {
                "shop_id":          shop_str,
                "period_from":      day,
                "period_to":        day,
                "sku_title":        sku_title[:300],
                "sku_id":           None,  # group=false omits skuId
                "product_id":       _coerce_int(item.get("productId")) or None,
                "product_title":    None,
                "product_title_ru": None,
                "image_url":        None,
                "characteristics":  None,  # group=false has no chars array
                "amount":           0,
                "amount_returns":   0,
                "sell_price":       0,
                "purchase_price":   0,
                "seller_discount":  0,
                "seller_profit":    0,
                "commission":       0,
                "withdrawn_profit": 0,
                "logistics_fee":    0,
            }
            groups[key] = g

        # Lazily fill descriptive fields from the first item that has them;
        # PROCESSING rows sometimes carry null productTitle.
        if not g["product_title"]:
            pt = _coerce_str(item.get("productTitle"))
            if pt:
                g["product_title"] = pt[:500]
        if not g["image_url"]:
            url = _pick_image_url(item.get("productImage"))
            if url:
                g["image_url"] = url[:800]
        if not g["product_id"]:
            pid = _coerce_int(item.get("productId"))
            if pid:
                g["product_id"] = pid

        qty          = _coerce_int(item.get("amount"))
        sp_per_unit  = _coerce_int(item.get("sellPrice"))
        pp_per_unit  = _coerce_int(item.get("purchasePrice"))

        g["amount"]           += qty
        g["amount_returns"]   += _coerce_int(item.get("amountReturns"))
        g["sell_price"]       += sp_per_unit * qty
        g["purchase_price"]   += pp_per_unit * qty
        g["seller_discount"]  += _coerce_int(item.get("sellerDiscountAmount"))
        g["seller_profit"]    += _coerce_int(item.get("sellerProfit"))
        g["commission"]       += _coerce_int(item.get("commission"))
        g["withdrawn_profit"] += _coerce_int(item.get("withdrawnProfit"))
        g["logistics_fee"]    += _coerce_int(item.get("logisticDeliveryFee"))

    return list(groups.values())

#fetching the data for expenses 
def fetch_finance_expenses_for_shop_window(
    token: str,
    shop_uzum_id: str | int,
    date_from_tashkent: datetime,
    date_to_tashkent: datetime,
    *,
    size: int = 10000,
) -> list[dict]:
    """Walk /v1/finance/expenses pages for the window, return canonical rows.

    Page size: ``size=10000`` (re-probed live 2026-05-25; sizes up to 15000
    returned full pages in ~1-4s with no 400). Matches the orders endpoint
    so both finance pipes paginate at the same cap. For a 53K-row backfill
    this is ~6 pages vs ~27 at size=2000. The daily loop's single-day
    windows stay well under 10000 rows so the bigger default has no cost.
    """
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
