"""Payme Merchant API routes and local order creation."""
from __future__ import annotations

import base64
import hmac
from datetime import datetime, timedelta

from flask import Blueprint, request
from flask_login import current_user, login_required
from sqlalchemy import select

from config import (
    APP_PUBLIC_BASE_URL,
    PAYME_KEY,
    PAYME_MERCHANT_ID,
    PAYME_MERCHANT_LOGIN,
    PAYME_TEST_KEY,
    PAYME_USE_TEST,
)
from core.auth_helpers import _json_response
from core.subscriptions import (
    _apply_paid_subscription_plan,
    _get_or_create_subscription_settings,
    _subscription_plan_by_key,
)
from extensions import SessionLocal
from models import PaymeTransaction, SubscriptionOrder, User

payme_bp = Blueprint("payme_bp", __name__)

_PAYME_STATE_CREATED = 1
_PAYME_STATE_COMPLETED = 2
_PAYME_STATE_CANCELLED = -1
_PAYME_STATE_CANCELLED_AFTER_COMPLETE = -2
_PAYME_CREATE_TIMEOUT_MS = 43_200_000


def _payme_now() -> datetime:
    return datetime.utcnow()


def _payme_now_ms() -> int:
    return int(_payme_now().timestamp() * 1000)


def _payme_message(ru: str, *, uz: str | None = None, en: str | None = None) -> dict:
    return {
        "ru": ru,
        "uz": uz or ru,
        "en": en or ru,
    }


def _payme_result(result: dict, request_id):
    return _json_response({
        "result": result,
        "id": request_id,
    })


def _payme_error(code: int, message: dict, *, request_id=None, data=None):
    payload: dict = {
        "error": {
            "code": code,
            "message": message,
        },
        "id": request_id,
    }
    if data is not None:
        payload["error"]["data"] = data
    return _json_response(payload)


def _payme_is_enabled() -> bool:
    return bool(PAYME_MERCHANT_ID and PAYME_MERCHANT_LOGIN and (PAYME_KEY or PAYME_TEST_KEY))


def _payme_checkout_url() -> str:
    return "https://test.paycom.uz" if PAYME_USE_TEST else "https://checkout.paycom.uz"


def _payme_public_base_url() -> str:
    if APP_PUBLIC_BASE_URL:
        return APP_PUBLIC_BASE_URL
    return request.url_root.rstrip("/")


def _payme_expected_authorizations() -> set[str]:
    expected: set[str] = set()
    for password in (PAYME_KEY, PAYME_TEST_KEY):
        if not password:
            continue
        raw = f"{PAYME_MERCHANT_LOGIN}:{password}".encode("utf-8")
        expected.add(f"Basic {base64.b64encode(raw).decode('ascii')}")
    return expected


def _payme_is_authorized() -> bool:
    header = request.headers.get("Authorization", "")
    if not header:
        return False
    for expected in _payme_expected_authorizations():
        if hmac.compare_digest(header, expected):
            return True
    return False


def _payme_parse_int(value, *, default: int | None = None) -> int | None:
    try:
        return int(value)
    except Exception:
        return default


def _payme_order_from_params(db, params: dict, *, request_id=None):
    account = params.get("account") or {}
    if not isinstance(account, dict):
        account = {}
    order_id = _payme_parse_int(account.get("order_id"))
    if not order_id:
        return None, _payme_error(
            -31050,
            _payme_message("Некорректный номер заказа", uz="Buyurtma raqami noto'g'ri", en="Invalid order id"),
            request_id=request_id,
            data="account.order_id",
        )

    order = db.get(SubscriptionOrder, int(order_id))
    if order is None:
        return None, _payme_error(
            -31050,
            _payme_message("Заказ не найден", uz="Buyurtma topilmadi", en="Order not found"),
            request_id=request_id,
            data="account.order_id",
        )
    return order, None


def _payme_transaction_payload(tx: PaymeTransaction) -> dict:
    return {
        "create_time": int(tx.create_time_ms or 0),
        "perform_time": int(tx.perform_time_ms or 0),
        "cancel_time": int(tx.cancel_time_ms or 0),
        "transaction": str(tx.id),
        "state": int(tx.state),
        "reason": tx.reason,
    }


def _payme_create_result(tx: PaymeTransaction) -> dict:
    return {
        "create_time": int(tx.create_time_ms or 0),
        "transaction": str(tx.id),
        "state": int(tx.state),
    }


def _payme_perform_result(tx: PaymeTransaction) -> dict:
    return {
        "transaction": str(tx.id),
        "perform_time": int(tx.perform_time_ms or 0),
        "state": int(tx.state),
    }


def _payme_cancel_result(tx: PaymeTransaction) -> dict:
    return {
        "transaction": str(tx.id),
        "cancel_time": int(tx.cancel_time_ms or 0),
        "state": int(tx.state),
    }


@payme_bp.post("/api/payme/orders")
@login_required
def create_payme_order():
    if not _payme_is_enabled():
        return _json_response({"error": "Payme is not configured"}, 503)
    if getattr(current_user, "is_admin", False):
        return _json_response({"error": "Admin account does not require subscriptions"}, 400)

    payload = request.get_json(force=True, silent=True) or {}
    plan_key = str(payload.get("plan_key") or "").strip().lower()
    if not plan_key:
        return _json_response({"error": "plan_key required"}, 400)

    with SessionLocal() as db:
        user = db.get(User, int(current_user.get_id()))
        settings = _get_or_create_subscription_settings(db)
        plan = _subscription_plan_by_key(plan_key, settings=settings)
        if plan is None:
            return _json_response({"error": "Unknown plan"}, 404)

        order = SubscriptionOrder(
            user_id=user.id,
            plan_key=plan["key"],
            plan_label=plan["label"],
            duration_days=int(plan["duration_days"] or 0),
            amount_sum=int(plan["price_sum"] or 0),
            amount_tiyin=int(plan["price_sum"] or 0) * 100,
            status="pending",
        )
        db.add(order)
        db.commit()
        db.refresh(order)

    callback = f"{_payme_public_base_url()}/subscription?payme_order={order.id}&payme_tx=:transaction"
    description = f"{order.plan_label} SellerHub subscription"
    return _json_response({
        "ok": True,
        "order_id": order.id,
        "checkout_url": _payme_checkout_url(),
        "form": {
            "merchant": PAYME_MERCHANT_ID,
            "amount": order.amount_tiyin,
            "account[order_id]": str(order.id),
            "callback": callback,
            "callback_timeout": "1500",
            "description": description,
            "lang": "ru",
        },
    })


@payme_bp.post("/api/payme")
def payme_rpc():
    payload = request.get_json(force=True, silent=True)
    request_id = payload.get("id") if isinstance(payload, dict) else None

    if not isinstance(payload, dict):
        return _payme_error(
            -32600,
            _payme_message("Некорректный RPC-запрос", uz="Noto'g'ri RPC so'rov", en="Invalid RPC request"),
            request_id=request_id,
        )

    if not _payme_is_enabled():
        return _payme_error(
            -32400,
            _payme_message("Payme не настроен", uz="Payme sozlanmagan", en="Payme is not configured"),
            request_id=request_id,
        )

    if not _payme_is_authorized():
        return _payme_error(
            -32504,
            _payme_message("Недостаточно прав", uz="Huquqlar yetarli emas", en="Insufficient privileges"),
            request_id=request_id,
        )

    method = str(payload.get("method") or "").strip()
    params = payload.get("params") or {}
    if not isinstance(params, dict):
        params = {}

    try:
        if method == "CheckPerformTransaction":
            return _payme_check_perform(params, request_id=request_id)
        if method == "CreateTransaction":
            return _payme_create_transaction(params, request_id=request_id)
        if method == "PerformTransaction":
            return _payme_perform_transaction(params, request_id=request_id)
        if method == "CancelTransaction":
            return _payme_cancel_transaction(params, request_id=request_id)
        if method == "CheckTransaction":
            return _payme_check_transaction(params, request_id=request_id)
        if method == "GetStatement":
            return _payme_get_statement(params, request_id=request_id)
        return _payme_error(
            -32601,
            _payme_message("Метод не найден", uz="Metod topilmadi", en="Method not found"),
            request_id=request_id,
        )
    except Exception as exc:
        return _payme_error(
            -32400,
            _payme_message("Системная ошибка", uz="Tizim xatosi", en="System error"),
            request_id=request_id,
            data=str(exc),
        )


def _payme_check_perform(params: dict, *, request_id):
    amount = _payme_parse_int(params.get("amount"), default=0) or 0
    with SessionLocal() as db:
        order, error = _payme_order_from_params(db, params, request_id=request_id)
        if error is not None:
            return error
        if amount != int(order.amount_tiyin or 0):
            return _payme_error(
                -31001,
                _payme_message("Неверная сумма", uz="Noto'g'ri summa", en="Invalid amount"),
                request_id=request_id,
            )
        if order.status != "pending":
            return _payme_error(
                -31008,
                _payme_message("Невозможно выполнить операцию", uz="Amalni bajarib bo'lmaydi", en="Cannot perform operation"),
                request_id=request_id,
            )
        return _payme_result({"allow": True}, request_id)


def _payme_create_transaction(params: dict, *, request_id):
    payme_id = str(params.get("id") or "").strip()
    amount = _payme_parse_int(params.get("amount"), default=0) or 0
    payme_time_ms = _payme_parse_int(params.get("time"), default=0) or 0
    now_ms = _payme_now_ms()

    if not payme_id:
        return _payme_error(
            -32600,
            _payme_message("Не передан id транзакции", uz="Tranzaksiya id yuborilmadi", en="Transaction id is missing"),
            request_id=request_id,
        )

    with SessionLocal() as db:
        order, error = _payme_order_from_params(db, params, request_id=request_id)
        if error is not None:
            return error

        if amount != int(order.amount_tiyin or 0):
            return _payme_error(
                -31001,
                _payme_message("Неверная сумма", uz="Noto'g'ri summa", en="Invalid amount"),
                request_id=request_id,
            )
        if order.status != "pending":
            return _payme_error(
                -31008,
                _payme_message("Невозможно выполнить операцию", uz="Amalni bajarib bo'lmaydi", en="Cannot perform operation"),
                request_id=request_id,
            )

        existing_by_id = db.execute(
            select(PaymeTransaction).where(PaymeTransaction.payme_transaction_id == payme_id)
        ).scalar_one_or_none()
        if existing_by_id is not None:
            return _payme_result(_payme_create_result(existing_by_id), request_id)

        active_for_order = db.execute(
            select(PaymeTransaction)
            .where(PaymeTransaction.order_id == order.id)
            .where(PaymeTransaction.state.in_((_PAYME_STATE_CREATED, _PAYME_STATE_COMPLETED)))
            .order_by(PaymeTransaction.id.desc())
        ).scalar_one_or_none()
        if active_for_order is not None:
            if (
                active_for_order.state == _PAYME_STATE_CREATED
                and now_ms - int(active_for_order.create_time_ms or 0) >= _PAYME_CREATE_TIMEOUT_MS
            ):
                active_for_order.state = _PAYME_STATE_CANCELLED
                active_for_order.reason = 4
                active_for_order.cancel_time_ms = now_ms
                order.status = "cancelled"
                order.cancelled_at = _payme_now()
                db.add(active_for_order)
                db.add(order)
                db.commit()
                return _payme_error(
                    -31008,
                    _payme_message("Невозможно выполнить операцию", uz="Amalni bajarib bo'lmaydi", en="Cannot perform operation"),
                    request_id=request_id,
                )
            else:
                return _payme_error(
                    -31008,
                    _payme_message("Невозможно выполнить операцию", uz="Amalni bajarib bo'lmaydi", en="Cannot perform operation"),
                    request_id=request_id,
                )

        tx = PaymeTransaction(
            payme_transaction_id=payme_id,
            order_id=order.id,
            amount_tiyin=amount,
            account_order_id=order.id,
            payme_time_ms=payme_time_ms,
            create_time_ms=now_ms,
            state=_PAYME_STATE_CREATED,
        )
        db.add(tx)
        db.commit()
        db.refresh(tx)
        return _payme_result(_payme_create_result(tx), request_id)


def _payme_perform_transaction(params: dict, *, request_id):
    payme_id = str(params.get("id") or "").strip()
    if not payme_id:
        return _payme_error(
            -31003,
            _payme_message("Транзакция не найдена", uz="Tranzaksiya topilmadi", en="Transaction not found"),
            request_id=request_id,
        )

    with SessionLocal() as db:
        tx = db.execute(
            select(PaymeTransaction).where(PaymeTransaction.payme_transaction_id == payme_id)
        ).scalar_one_or_none()
        if tx is None:
            return _payme_error(
                -31003,
                _payme_message("Транзакция не найдена", uz="Tranzaksiya topilmadi", en="Transaction not found"),
                request_id=request_id,
            )

        if tx.state == _PAYME_STATE_COMPLETED:
            return _payme_result(_payme_perform_result(tx), request_id)

        if tx.state not in (_PAYME_STATE_CREATED,):
            return _payme_error(
                -31008,
                _payme_message("Невозможно выполнить операцию", uz="Amalni bajarib bo'lmaydi", en="Cannot perform operation"),
                request_id=request_id,
            )

        order = db.get(SubscriptionOrder, int(tx.order_id))
        if order is None or order.status == "cancelled":
            return _payme_error(
                -31008,
                _payme_message("Невозможно выполнить операцию", uz="Amalni bajarib bo'lmaydi", en="Cannot perform operation"),
                request_id=request_id,
            )

        user = db.get(User, int(order.user_id))
        settings = _get_or_create_subscription_settings(db)
        now = _payme_now()
        if order.status != "paid":
            order.previous_subscription_expires_at = user.subscription_expires_at
            order.previous_subscription_is_unlimited = bool(user.subscription_is_unlimited)
            applied_until, _ = _apply_paid_subscription_plan(
                db,
                user=user,
                plan_key=order.plan_key,
                settings=settings,
                now=now,
            )
            order.applied_until = applied_until
            order.status = "paid"
            order.paid_at = now

        tx.state = _PAYME_STATE_COMPLETED
        tx.perform_time_ms = _payme_now_ms()
        db.add(tx)
        db.add(order)
        db.commit()
        return _payme_result(_payme_perform_result(tx), request_id)


def _payme_cancel_transaction(params: dict, *, request_id):
    payme_id = str(params.get("id") or "").strip()
    reason = _payme_parse_int(params.get("reason"), default=0)
    if not payme_id:
        return _payme_error(
            -31003,
            _payme_message("Транзакция не найдена", uz="Tranzaksiya topilmadi", en="Transaction not found"),
            request_id=request_id,
        )

    with SessionLocal() as db:
        tx = db.execute(
            select(PaymeTransaction).where(PaymeTransaction.payme_transaction_id == payme_id)
        ).scalar_one_or_none()
        if tx is None:
            return _payme_error(
                -31003,
                _payme_message("Транзакция не найдена", uz="Tranzaksiya topilmadi", en="Transaction not found"),
                request_id=request_id,
            )

        if tx.state in (_PAYME_STATE_CANCELLED, _PAYME_STATE_CANCELLED_AFTER_COMPLETE):
            return _payme_result(_payme_cancel_result(tx), request_id)

        order = db.get(SubscriptionOrder, int(tx.order_id))
        now = _payme_now()
        now_ms = _payme_now_ms()

        if tx.state == _PAYME_STATE_CREATED:
            tx.state = _PAYME_STATE_CANCELLED
            tx.reason = reason
            tx.cancel_time_ms = now_ms
            if order is not None:
                order.status = "cancelled"
                order.cancelled_at = now
                db.add(order)
            db.add(tx)
            db.commit()
            return _payme_result(_payme_cancel_result(tx), request_id)

        if tx.state != _PAYME_STATE_COMPLETED or order is None:
            return _payme_error(
                -31008,
                _payme_message("Невозможно выполнить операцию", uz="Amalni bajarib bo'lmaydi", en="Cannot perform operation"),
                request_id=request_id,
            )

        user = db.get(User, int(order.user_id))
        if user is not None:
            if order.applied_until is not None and user.subscription_expires_at == order.applied_until:
                user.subscription_expires_at = order.previous_subscription_expires_at
                user.subscription_is_unlimited = bool(order.previous_subscription_is_unlimited)
            elif user.subscription_expires_at is not None and not user.subscription_is_unlimited and int(order.duration_days or 0) > 0:
                user.subscription_expires_at = user.subscription_expires_at - timedelta(days=int(order.duration_days))
            else:
                user.subscription_expires_at = order.previous_subscription_expires_at
                user.subscription_is_unlimited = bool(order.previous_subscription_is_unlimited)
            db.add(user)

        tx.state = _PAYME_STATE_CANCELLED_AFTER_COMPLETE
        tx.reason = reason
        tx.cancel_time_ms = now_ms
        order.status = "cancelled"
        order.cancelled_at = now
        db.add(tx)
        db.add(order)
        db.commit()
        return _payme_result(_payme_cancel_result(tx), request_id)


def _payme_check_transaction(params: dict, *, request_id):
    payme_id = str(params.get("id") or "").strip()
    if not payme_id:
        return _payme_error(
            -31003,
            _payme_message("Транзакция не найдена", uz="Tranzaksiya topilmadi", en="Transaction not found"),
            request_id=request_id,
        )

    with SessionLocal() as db:
        tx = db.execute(
            select(PaymeTransaction).where(PaymeTransaction.payme_transaction_id == payme_id)
        ).scalar_one_or_none()
        if tx is None:
            return _payme_error(
                -31003,
                _payme_message("Транзакция не найдена", uz="Tranzaksiya topilmadi", en="Transaction not found"),
                request_id=request_id,
            )
        return _payme_result(_payme_transaction_payload(tx), request_id)


def _payme_get_statement(params: dict, *, request_id):
    from_ms = _payme_parse_int(params.get("from"), default=0) or 0
    to_ms = _payme_parse_int(params.get("to"), default=0) or 0

    with SessionLocal() as db:
        transactions = db.execute(
            select(PaymeTransaction)
            .where(PaymeTransaction.create_time_ms >= from_ms)
            .where(PaymeTransaction.create_time_ms <= to_ms)
            .order_by(PaymeTransaction.create_time_ms.asc(), PaymeTransaction.id.asc())
        ).scalars().all()

        rows = []
        for tx in transactions:
            rows.append({
                "id": tx.payme_transaction_id,
                "time": int(tx.payme_time_ms or 0),
                "amount": int(tx.amount_tiyin or 0),
                "account": {
                    "order_id": str(tx.account_order_id),
                },
                **_payme_transaction_payload(tx),
            })

    return _payme_result({"transactions": rows}, request_id)
