"""
SNAPSHOT (2026-05-21, refresh): core/uzum_openapi.py'ga qo'shilgan FBS qismi.

Bu fayl FAQAT o'qish uchun — haqiqiy kod core/uzum_openapi.py'da.

O'zgarishlar (oldingi snapshot'dan):
- _fbs_order_url_variants() → _LEGACY_FBS_ORDER_URL_VARIANTS (faqat env flag bilan)
- fetch_fbs_orders_page() endi /v2/fbs/orders aniq URL'ga uradi
- Yangi parametrlar: status (single), scheme (FBS/DBS), date_from_ms, date_to_ms
- size capped 50 ga (Uzum cheklovi)
- shopIds repeated query param sifatida (OpenAPI 3 explode=true)
- extract_fbs_orders_list() endi faqat payload.orders + payload.totalAmount qaraydi
- Yangi: FBS_ORDER_STATUSES (11 ta), FBS_ORDER_SCHEMES (FBS, DBS)

Bog'liqlik (core/uzum_openapi.py'da yuqorida aniqlangan):
- OPENAPI_BASE = 'https://api-seller.uzum.uz/api/seller-openapi'
- _AUTH_VARIANTS — auth header variantlari
- _clean(token), _try_request(url, headers, debug_label), _is_token_not_found(status, parsed)
"""
from __future__ import annotations

import os
# from core.http_client import _get_http_session  # ── yuqorida import qilingan

──
# FBS / DBS orders — confirmed against Uzum seller-openapi swagger
# 2026-05-21. The list endpoint is ``GET /v2/fbs/orders`` (DBS orders
# are returned by the same endpoint with ``scheme=DBS`` filter).
#
# Status enum (single value at a time, not CSV — swagger lists 11 values):
#   CREATED, PACKING, PENDING_DELIVERY, DELIVERING, DELIVERED,
#   ACCEPTED_AT_DP, DELIVERED_TO_CUSTOMER_DELIVERY_POINT, COMPLETED,
#   CANCELED, PENDING_CANCELLATION, RETURNED
#
# Response top-wrap: ``{"payload": {"orders": [...], "totalAmount": N},
# "errors": [...], "timestamp": "...", "trace": "...", "error": "null"}``
#
# Per-order key fields (verbatim Uzum names):
#   id, status, scheme, dateCreated, acceptUntil, deliverUntil, price,
#   shopId, identifierRequired, cancelReason, orderItems[], stock,
#   timeSlot, dropOffPoint, deliveryInfo {deliveryAddress,
#   customerFullname, customerPhone, deliveryComment}
# ─────────────────────────────────────────────────────────────────────


# Legacy URL probe variants used during the swagger-blind discovery phase.
# All five proved to be 404 — kept ONLY so an ops-time env flag can replay
# them if Uzum changes the canonical path. The default code path skips
# this entirely.
_LEGACY_FBS_ORDER_URL_VARIANTS = [
    ("v1.order.shop",   "/v1/order/shop/{sid}"),
    ("v1.orders.shop",  "/v1/orders/shop/{sid}"),
    ("v1.shop.order",   "/v1/shop/{sid}/order"),
    ("v1.order.q",      "/v1/order?shopIds={sid}"),
    ("v1.orders.q",     "/v1/orders?shopIds={sid}"),
]


def _fbs_orders_request_with_auth(
    url: str, token: str, *,
    accept_language: str | None,
    debug_label: str,
) -> tuple[dict | list | None, int, str, str]:
    """Execute a GET against `url`, trying each auth variant.

    Returns ``(parsed_or_None, status, body_text, auth_label_used)``. The
    caller decides whether a non-2xx is fatal — this helper only handles
    the auth-variant rotation when Uzum returns 401/forbidden-001.
    """
    last_status = 0
    last_text = ""
    last_label = ""
    for auth_label, builder in _AUTH_VARIANTS:
        try:
            headers = builder(token)
        except Exception as e:
            print(f"[UzumOpenAPI] header-builder error on {auth_label}: {e}")
            continue
        if accept_language:
            headers = {**headers, "Accept-Language": accept_language}
        status, text, parsed = _try_request(
            url, headers, debug_label=f"{debug_label}/{auth_label}"
        )
        if 200 <= status < 300:
            return (parsed, status, text, auth_label)
        last_status, last_text, last_label = status, text, auth_label
        # Only retry with next auth variant on auth-shaped failures.
        if not _is_token_not_found(status, parsed):
            break
    return (None, last_status, last_text, last_label)


def fetch_fbs_orders_page(
    token: str,
    shop_uzum_id: str | int | list,
    *,
    status: str = "CREATED",
    scheme: str | None = None,
    page: int = 0,
    size: int = 20,
    date_from_ms: int | None = None,
    date_to_ms: int | None = None,
    accept_language: str | None = None,
) -> tuple[dict, str]:
    """GET /v2/fbs/orders — FBS/DBS seller orders by status.

    Returns ``(parsed_body, used_url)``. Body shape (verbatim swagger,
    confirmed 2026-05-21):

        {"payload": {"orders": [...], "totalAmount": N},
         "errors": [...], "timestamp": "...", "trace": "...",
         "error": "null"}

    Parameters
    ----------
    shop_uzum_id : single id or list of ids. Always serialized as the
        ``shopIds`` repeated query parameter (OpenAPI 3 style=form,
        explode=true). REQUIRED by Uzum.
    status : one of the 11 enum values (CREATED, PACKING, ...). Single
        value only — the endpoint does NOT accept CSV here.
    scheme : ``"FBS"`` / ``"DBS"`` / ``None`` (returns both).
    page, size : 0-based pagination. ``size`` is capped at 50 by Uzum.
    date_from_ms, date_to_ms : epoch *milliseconds* per swagger
        (``integer($int64)``). NOTE: the related ``/v1/finance/orders``
        endpoint documented ms but actually expects seconds — empirically
        verify against /v2/fbs/orders the first time these are used.

    Legacy fallback
    ---------------
    If ``UZUM_FBS_PROBE_LEGACY=1`` is set in the environment, the
    function additionally tries the five guessed URL paths from the
    pre-swagger discovery phase. Production should never need this.
    """
    token = _clean(token)
    if size > 50:
        size = 50  # Uzum caps at 50; pass-through would 400.
    elif size < 1:
        size = 1

    # Serialize shopIds as repeated form params per OpenAPI 3 default.
    if isinstance(shop_uzum_id, (list, tuple)):
        ids = [str(s).strip() for s in shop_uzum_id if str(s).strip()]
    else:
        ids = [str(shop_uzum_id).strip()]
    if not ids or not any(ids):
        raise RuntimeError("fetch_fbs_orders_page: shop_uzum_id is required")

    qs_parts = [f"shopIds={sid}" for sid in ids]
    qs_parts.append(f"status={status}")
    if scheme:
        qs_parts.append(f"scheme={scheme}")
    if date_from_ms is not None:
        qs_parts.append(f"dateFrom={int(date_from_ms)}")
    if date_to_ms is not None:
        qs_parts.append(f"dateTo={int(date_to_ms)}")
    qs_parts.append(f"page={int(page)}")
    qs_parts.append(f"size={int(size)}")
    qs = "&".join(qs_parts)

    primary_url = f"{OPENAPI_BASE}/v2/fbs/orders?{qs}"
    debug_label = f"fbs.orders[shops={','.join(ids)} status={status} scheme={scheme or '-'} p={page}]"

    parsed, status_code, text, _ = _fbs_orders_request_with_auth(
        primary_url, token,
        accept_language=accept_language,
        debug_label=debug_label,
    )
    if 200 <= status_code < 300 and isinstance(parsed, dict):
        return (parsed, primary_url)

    # Legacy probe — only when explicitly opted in. Useful if Uzum ever
    # renames the canonical path; lets us bring the page back without a
    # code deploy by flipping an env var.
    if os.getenv("UZUM_FBS_PROBE_LEGACY") == "1":
        for variant_label, path_tpl in _LEGACY_FBS_ORDER_URL_VARIANTS:
            # Old shapes used per-shop path templates; only retry for a
            # single-id call to keep the legacy semantics intact.
            if len(ids) != 1:
                continue
            path = path_tpl.format(sid=ids[0])
            sep = "&" if "?" in path else "?"
            url = (
                f"{OPENAPI_BASE}{path}{sep}page={int(page)}&size={int(size)}"
                f"&statuses={status}"
            )
            if scheme:
                url += f"&types={scheme}"
            legacy_parsed, legacy_status, _, _ = _fbs_orders_request_with_auth(
                url, token,
                accept_language=accept_language,
                debug_label=f"fbs.orders.legacy/{variant_label}",
            )
            if 200 <= legacy_status < 300 and isinstance(legacy_parsed, (dict, list)):
                body = legacy_parsed if isinstance(legacy_parsed, dict) else {"orders": legacy_parsed}
                return (body, url)

    raise RuntimeError(
        f"Uzum OpenAPI /v2/fbs/orders failed for shops={','.join(ids)} status={status} "
        f"(HTTP {status_code}). Response: {text[:300]}"
    )


def extract_fbs_orders_list(body: dict) -> tuple[list[dict], int]:
    """Pull ``payload.orders`` + ``payload.totalAmount`` out of a
    /v2/fbs/orders response.

    Returns ``([], 0)`` for any unexpected shape. Confirmed against the
    swagger response template (2026-05-21):

        {"payload": {"orders": [{...}, ...], "totalAmount": N}, ...}

    Falls back to a bare-list response only if Uzum ever changes the
    wrap — that's the only divergence we tolerate without a code change.
    """
    if isinstance(body, list):
        return (body, len(body))
    if not isinstance(body, dict):
        return ([], 0)

    payload = body.get("payload")
    if isinstance(payload, dict):
        orders = payload.get("orders")
        if isinstance(orders, list):
            try:
                total = int(payload.get("totalAmount") or len(orders))
            except (TypeError, ValueError):
                total = len(orders)
            return (orders, total)
    return ([], 0)


# ── Order statuses (canonical enum from /v2/fbs/orders swagger) ────────
# Single source of truth so routes/templates can import instead of
# duplicating the list. Order matters: this is the lifecycle.
FBS_ORDER_STATUSES = (
    "CREATED",
    "PACKING",
    "PENDING_DELIVERY",
    "DELIVERING",
    "DELIVERED",
    "ACCEPTED_AT_DP",
    "DELIVERED_TO_CUSTOMER_DELIVERY_POINT",
    "COMPLETED",
    "CANCELED",
    "PENDING_CANCELLATION",
    "RETURNED",
)
FBS_ORDER_SCHEMES = ("FBS", "DBS")
