"""FBS / DBS data source seam.

Stage 1 (current): every call hits Uzum Seller OpenAPI directly via
``core.uzum_openapi``. Stage 4 will swap the bodies of these functions
to read from a PostgreSQL ``fbs_orders`` table populated by a worker
loop, with optional ``refresh=1`` triggering a live Uzum call.

The routes layer (``fbs/routes.py``) imports ONLY from this module —
never ``core.uzum_openapi`` directly. That isolation makes Stage 4 a
no-op for the route layer: only this file changes.

Contract notes
--------------
- All return tuples include ``used_url`` (or a placeholder like ``"db://"``)
  so the UI can keep showing where the data came from.
- Functions raise ``RuntimeError`` for Uzum failures; the routes layer
  surfaces a 502 with the message.
- A ``NotImplementedError`` from this module means we're still waiting
  on the corresponding swagger details — the route should return 501.
"""
from __future__ import annotations

from core.uzum_openapi import (
    fetch_fbs_orders_page,
    fetch_fbs_orders_count,
    fetch_fbs_order_detail,
    extract_fbs_orders_list,
    FBS_ORDER_STATUSES,
    FBS_ORDER_SCHEMES,
)


def get_fbs_orders(
    token: str,
    shop_uzum_id: str | int,
    *,
    status: str = "CREATED",
    scheme: str | None = None,
    page: int = 0,
    size: int = 20,
    date_from_ms: int | None = None,
    date_to_ms: int | None = None,
) -> tuple[list[dict], int, str]:
    """List FBS/DBS orders. Returns ``(orders, total_amount, used_url)``.

    Stage 1: hits ``GET /v2/fbs/orders`` on Uzum directly.
    Stage 4: will read from ``fbs_orders`` table; ``refresh=True`` arg
    will be added then to trigger a live Uzum call.
    """
    body, used_url = fetch_fbs_orders_page(
        token, shop_uzum_id,
        status=status, scheme=scheme,
        page=page, size=size,
        date_from_ms=date_from_ms, date_to_ms=date_to_ms,
    )
    orders, total = extract_fbs_orders_list(body)
    return (orders, total, used_url)


def get_fbs_count(
    token: str,
    shop_uzum_id: str | int,
    *,
    status: str,
    date_from_ms: int | None = None,
    date_to_ms: int | None = None,
) -> tuple[int, str]:
    """Single-status count via ``GET /v2/fbs/orders/count``. Returns
    ``(count, used_url)``.

    Per Uzum swagger this endpoint returns the count for ONE status at a
    time (payload is a bare integer). Frontends that want a full status
    breakdown fan out 11 parallel calls — one per ``FBS_ORDER_STATUSES``
    value. The endpoint has NO ``scheme`` parameter, so the count covers
    FBS only (per swagger description).
    """
    return fetch_fbs_orders_count(
        token, shop_uzum_id,
        status=status,
        date_from_ms=date_from_ms,
        date_to_ms=date_to_ms,
    )


def get_fbs_order_detail(
    token: str,
    order_id: str | int,
) -> tuple[dict, str]:
    """Single-order detail via ``GET /v1/fbs/order/{orderId}``. Returns
    ``(order_dict, used_url)``.

    The returned ``order_dict`` has the same shape as one element of
    ``/v2/fbs/orders`` ``payload.orders[]`` — same field names for
    customer (deliveryInfo), items (orderItems), drop-off (dropOffPoint),
    stock, status, scheme, all date columns, etc. Templates can reuse
    the list-row mapping unchanged.

    The route layer (fbs/routes.py) is responsible for enforcing the
    shop-scope check against ``order["shopId"]`` after this returns —
    Uzum's endpoint accepts only an orderId and has no shop filter.
    """
    return fetch_fbs_order_detail(token, order_id)


__all__ = [
    "get_fbs_orders",
    "get_fbs_count",
    "get_fbs_order_detail",
    "FBS_ORDER_STATUSES",
    "FBS_ORDER_SCHEMES",
]
