"""FBS / DBS orders blueprint.

Surfaces Uzum FBS/DBS orders to the seller via the per-user Seller
OpenAPI token (``User.uzum_openapi_token``). Read-only in Stage 1; later
stages add confirm/cancel/label actions and DB caching.

Architecture: this module imports from ``core.fbs_data`` only, never
``core.uzum_openapi`` directly. Stage 4 will swap ``fbs_data`` to read
from a PostgreSQL ``fbs_orders`` table — this file won't change.
"""
from __future__ import annotations

from flask import Blueprint, render_template, request
from flask_login import current_user, login_required
from sqlalchemy import select

from extensions import SessionLocal
from models import Shop, User
from core.auth_helpers import _json_response, _user_shop_ids
from core.fbs_data import (
    get_fbs_orders,
    get_fbs_count,
    get_fbs_order_detail,
    FBS_ORDER_STATUSES,
    FBS_ORDER_SCHEMES,
)


fbs_bp = Blueprint("fbs_bp", __name__)


# Default status when the user lands on /fbs without picking one — the
# new-order queue is what sellers care about most.
DEFAULT_STATUS = "CREATED"


def _current_user_token_and_shops() -> tuple[str | None, list[dict]]:
    """Return ``(token, [{uzum_id, name}, ...])`` for the logged-in user.

    Token is read from the user's row (admin or regular). If the user
    has no token yet, returns ``(None, shops)`` so the UI can render a
    hint pointing to /fetch (My Shops page) where the token is pasted.
    """
    uid = int(current_user.get_id())
    allowed_shop_db_ids = _user_shop_ids(uid)
    with SessionLocal() as db:
        user = db.get(User, uid)
        token = (user.uzum_openapi_token or "").strip() if user else ""
        if not allowed_shop_db_ids:
            return (token or None, [])
        shops = db.execute(
            select(Shop.uzum_id, Shop.name)
            .where(Shop.id.in_(allowed_shop_db_ids))
            .order_by(Shop.name.is_(None), Shop.name, Shop.uzum_id)
        ).all()
    return (token or None, [{"uzum_id": s.uzum_id, "name": s.name} for s in shops])


def _resolve_user_token_for_shop(shop_uzum_id: str) -> tuple[str | None, str | None]:
    """Look up the calling user's token + verify shop scope.

    Returns ``(token_or_None, error_message_or_None)``. ``error_message``
    is non-None when the caller has no access to the shop or has no
    OpenAPI token set; the route turns that into a 400/404 JSON response.
    """
    uid = int(current_user.get_id())
    allowed_shop_db_ids = _user_shop_ids(uid)
    with SessionLocal() as db:
        shop = db.execute(
            select(Shop).where(Shop.uzum_id == shop_uzum_id)
        ).scalar_one_or_none()
        if not shop or shop.id not in allowed_shop_db_ids:
            return (None, "Shop not found or not accessible")
        user = db.get(User, uid)
        token = (user.uzum_openapi_token or "").strip() if user else ""
    if not token:
        return (None, (
            "Uzum OpenAPI token not set. Open «My Shops» (/fetch) "
            "and paste your Seller OpenAPI token first."
        ))
    return (token, None)


# ── Pages ────────────────────────────────────────────────────────────


@fbs_bp.get("/fbs")
@login_required
def fbs_page():
    token, shops = _current_user_token_and_shops()
    return render_template(
        "fbs_orders.html",
        title="FBS / DBS — buyurtmalar",
        shops=shops,
        has_token=bool(token),
        default_status=DEFAULT_STATUS,
        status_enum=list(FBS_ORDER_STATUSES),
        scheme_enum=list(FBS_ORDER_SCHEMES),
    )


@fbs_bp.get("/fbs/<int:order_id>")
@login_required
def fbs_order_detail_page(order_id: int):
    # Detail data is fetched client-side via /fbs/api/order/<id> so the
    # page renders fast even when Uzum is slow. Stage 4 will switch the
    # data source to DB; this template doesn't care.
    return render_template(
        "fbs_order_detail.html",
        title=f"Buyurtma №{order_id}",
        order_id=order_id,
    )


# ── JSON APIs ────────────────────────────────────────────────────────


@fbs_bp.get("/fbs/api/orders")
@login_required
def fbs_orders_api():
    """JSON: list FBS/DBS orders for a shop.

    Query params:
      shop_id  — required, Uzum shop id (string of digits)
      status   — one of FBS_ORDER_STATUSES (default CREATED)
      scheme   — "FBS" / "DBS" / empty (both)
      page     — default 0
      size     — default 20, max 50 (Uzum caps)
    """
    shop_id = (request.args.get("shop_id") or "").strip()
    if not shop_id:
        return _json_response({"error": "shop_id is required"}, 400)

    status_val = (request.args.get("status") or DEFAULT_STATUS).strip().upper()
    if status_val not in FBS_ORDER_STATUSES:
        return _json_response(
            {"error": f"status must be one of {list(FBS_ORDER_STATUSES)}"}, 400
        )

    scheme_val = (request.args.get("scheme") or "").strip().upper() or None
    if scheme_val and scheme_val not in FBS_ORDER_SCHEMES:
        return _json_response(
            {"error": f"scheme must be one of {list(FBS_ORDER_SCHEMES)} or empty"}, 400
        )

    try:
        page = max(0, int(request.args.get("page") or 0))
    except ValueError:
        page = 0
    try:
        size = max(1, min(50, int(request.args.get("size") or 20)))
    except ValueError:
        size = 20

    token, err = _resolve_user_token_for_shop(shop_id)
    if err:
        # 404 for ownership failure, 400 for missing token — caller
        # checks the message to distinguish.
        return _json_response({"error": err}, 404 if "not accessible" in err else 400)

    try:
        orders, total, used_url = get_fbs_orders(
            token, shop_id,
            status=status_val, scheme=scheme_val,
            page=page, size=size,
        )
    except Exception as e:
        return _json_response({"error": str(e)}, 502)

    return _json_response({
        "shop_id": shop_id,
        "status": status_val,
        "scheme": scheme_val,
        "page": page,
        "size": size,
        "used_url": used_url,
        "total": total,
        "orders": orders,
    })


@fbs_bp.get("/fbs/api/count")
@login_required
def fbs_count_api():
    """JSON: single-status count for one of the status chips.

    Uzum's ``GET /v2/fbs/orders/count`` returns a count for ONE status
    at a time — the response payload is a bare integer. Frontend fans
    out one call per status to fill all chip badges.

    Query params:
      shop_id — required
      status  — required, one of FBS_ORDER_STATUSES
    """
    shop_id = (request.args.get("shop_id") or "").strip()
    if not shop_id:
        return _json_response({"error": "shop_id is required"}, 400)

    status_val = (request.args.get("status") or "").strip().upper()
    if status_val not in FBS_ORDER_STATUSES:
        return _json_response(
            {"error": f"status must be one of {list(FBS_ORDER_STATUSES)}"}, 400
        )

    token, err = _resolve_user_token_for_shop(shop_id)
    if err:
        return _json_response({"error": err}, 404 if "not accessible" in err else 400)

    try:
        count, used_url = get_fbs_count(token, shop_id, status=status_val)
    except Exception as e:
        return _json_response({"error": str(e)}, 502)

    return _json_response({
        "shop_id": shop_id,
        "status": status_val,
        "used_url": used_url,
        "count": count,
    })


@fbs_bp.get("/fbs/api/order/<int:order_id>")
@login_required
def fbs_order_detail_api(order_id: int):
    """JSON: single-order detail via ``GET /v1/fbs/order/{orderId}``.

    Currently 501 — swagger details for this endpoint not yet captured.
    Routing scaffold is here so the detail page can wire up the fetch
    call already; once ``get_fbs_order_detail`` is implemented, this
    flips to returning the real body.

    Scope check: we don't know the order's shop_id until we call Uzum,
    so the scope check happens against the response's ``shopId`` field
    after the call (rejecting orders from shops the user doesn't own).
    """
    uid = int(current_user.get_id())
    allowed_shop_db_ids = _user_shop_ids(uid)
    with SessionLocal() as db:
        user = db.get(User, uid)
        token = (user.uzum_openapi_token or "").strip() if user else ""
    if not token:
        return _json_response({
            "error": "Uzum OpenAPI token not set. Open «My Shops» (/fetch) first."
        }, 400)

    try:
        order, used_url = get_fbs_order_detail(token, order_id)
    except NotImplementedError as e:
        return _json_response({"error": str(e), "pending": True}, 501)
    except Exception as e:
        return _json_response({"error": str(e)}, 502)

    # Post-fetch scope check (we only know the shop after Uzum responds).
    order_shop_uzum_id = str(order.get("shopId") or "").strip()
    with SessionLocal() as db:
        shop = db.execute(
            select(Shop).where(Shop.uzum_id == order_shop_uzum_id)
        ).scalar_one_or_none()
        if not shop or shop.id not in allowed_shop_db_ids:
            return _json_response({"error": "Order not accessible"}, 404)

    return _json_response({
        "order_id": order_id,
        "used_url": used_url,
        "order": order,
    })
