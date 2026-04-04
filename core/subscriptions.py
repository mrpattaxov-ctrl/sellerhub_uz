"""Subscription helpers: settings, status, code activation, and shop limits."""
from __future__ import annotations

import secrets
import string
from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import func, select

from extensions import SessionLocal
from models import (
    Shop,
    SubscriptionCode,
    SubscriptionCodeActivation,
    SubscriptionSettings,
    User,
)

SUBSCRIPTION_PLAN_OPTIONS = (
    {"key": "1m", "label": "1 месяц", "months": 1, "duration_days": 30, "discount_percent": 0},
    {"key": "3m", "label": "3 месяца", "months": 3, "duration_days": 90, "discount_percent": 10},
    {"key": "6m", "label": "6 месяцев", "months": 6, "duration_days": 180, "discount_percent": 20},
    {"key": "12m", "label": "1 год", "months": 12, "duration_days": 365, "discount_percent": 30},
)

SUBSCRIPTION_CODE_DURATION_OPTIONS = (
    {"key": "1m", "label": "1 месяц", "duration_days": 30, "is_unlimited": False},
    {"key": "2m", "label": "2 месяца", "duration_days": 60, "is_unlimited": False},
    {"key": "6m", "label": "6 месяцев", "duration_days": 180, "is_unlimited": False},
    {"key": "unlimited", "label": "Безлимит", "duration_days": None, "is_unlimited": True},
)


def _utcnow() -> datetime:
    return datetime.utcnow()


def _format_sum(value: int) -> str:
    return f"{int(value):,}".replace(",", " ")


def _get_or_create_subscription_settings(db) -> SubscriptionSettings:
    row = db.get(SubscriptionSettings, 1)
    if row is not None:
        return row
    row = SubscriptionSettings(id=1)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _subscription_settings_dict(row: SubscriptionSettings) -> dict:
    return {
        "trial_days": int(row.trial_days or 0),
        "monthly_price_sum": int(row.monthly_price_sum or 0),
        "max_shops_per_user": int(row.max_shops_per_user or 0),
    }


def _subscription_plan_rows(*, settings: SubscriptionSettings | dict) -> list[dict]:
    monthly_price = int(
        settings.monthly_price_sum if isinstance(settings, SubscriptionSettings)
        else settings.get("monthly_price_sum", 0)
    )
    rows: list[dict] = []
    for item in SUBSCRIPTION_PLAN_OPTIONS:
        full_price = monthly_price * item["months"]
        final_price = int(round(full_price * (100 - item["discount_percent"]) / 100))
        rows.append({
            **item,
            "full_price_sum": full_price,
            "price_sum": final_price,
            "price_sum_formatted": _format_sum(final_price),
            "full_price_sum_formatted": _format_sum(full_price),
        })
    return rows


def _subscription_plan_by_key(
    plan_key: str,
    *,
    settings: SubscriptionSettings | dict,
) -> dict | None:
    normalized = str(plan_key or "").strip().lower()
    for row in _subscription_plan_rows(settings=settings):
        if str(row.get("key", "")).strip().lower() == normalized:
            return row
    return None


def _subscription_code_duration_rows() -> list[dict]:
    return [dict(item) for item in SUBSCRIPTION_CODE_DURATION_OPTIONS]


def _subscription_code_duration_label(*, duration_days: int | None, is_unlimited: bool) -> str:
    if is_unlimited:
        return "Безлимит"
    for item in SUBSCRIPTION_CODE_DURATION_OPTIONS:
        if bool(item["is_unlimited"]) == bool(is_unlimited) and item["duration_days"] == duration_days:
            return str(item["label"])
    return f"{int(duration_days or 0)} дн."


def _ensure_user_trial_started(db, user: User, *, now: datetime | None = None) -> bool:
    if user.is_admin or user.trial_started_at is not None:
        return False
    user.trial_started_at = now or _utcnow()
    db.add(user)
    return True


def _subscription_status_for_user(
    user: User | None,
    *,
    settings: SubscriptionSettings | dict,
    now: datetime | None = None,
) -> dict:
    now = now or _utcnow()
    if user is None:
        return {
            "active": False,
            "expired": True,
            "state": "missing",
            "label": "Нет пользователя",
            "remaining_text": "Истекло",
            "is_unlimited": False,
            "trial_end_at": None,
            "subscription_end_at": None,
            "effective_end_at": None,
            "remaining_days": 0,
            "remaining_hours": 0,
        }

    if user.is_admin:
        return {
            "active": True,
            "expired": False,
            "state": "admin",
            "label": "Администратор",
            "remaining_text": "Без ограничений",
            "is_unlimited": True,
            "trial_end_at": None,
            "subscription_end_at": None,
            "effective_end_at": None,
            "remaining_days": None,
            "remaining_hours": None,
        }

    trial_days = int(
        settings.trial_days if isinstance(settings, SubscriptionSettings)
        else settings.get("trial_days", 0)
    )
    trial_started_at = user.trial_started_at or now
    trial_end_at = trial_started_at + timedelta(days=max(0, trial_days))
    subscription_end_at = user.subscription_expires_at

    if user.subscription_is_unlimited:
        return {
            "active": True,
            "expired": False,
            "state": "unlimited",
            "label": "Безлимит",
            "remaining_text": "Безлимит",
            "is_unlimited": True,
            "trial_end_at": trial_end_at,
            "subscription_end_at": None,
            "effective_end_at": None,
            "remaining_days": None,
            "remaining_hours": None,
        }

    effective_end_at = max(
        [dt for dt in (trial_end_at, subscription_end_at) if dt is not None],
        default=None,
    )
    remaining_seconds = int(max(0, (effective_end_at - now).total_seconds())) if effective_end_at else 0
    remaining_days = ((remaining_seconds + 86399) // 86400) if remaining_seconds > 0 else 0
    remaining_hours = ((remaining_seconds + 3599) // 3600) if remaining_seconds > 0 else 0
    active = bool(effective_end_at and effective_end_at > now)

    if active and subscription_end_at and subscription_end_at >= trial_end_at:
        state = "paid"
        label = "Подписка активна"
    elif active:
        state = "trial"
        label = "Пробный период"
    else:
        state = "expired"
        label = "Подписка истекла"

    if not active:
        remaining_text = "Истекло"
    elif remaining_days >= 1:
        remaining_text = f"{remaining_days} дн."
    else:
        remaining_text = f"{max(1, remaining_hours)} ч."

    return {
        "active": active,
        "expired": not active,
        "state": state,
        "label": label,
        "remaining_text": remaining_text,
        "is_unlimited": False,
        "trial_end_at": trial_end_at,
        "subscription_end_at": subscription_end_at,
        "effective_end_at": effective_end_at,
        "remaining_days": remaining_days,
        "remaining_hours": remaining_hours,
    }


def _subscription_status_from_values(
    *,
    trial_started_at: datetime | None,
    subscription_expires_at: datetime | None,
    subscription_is_unlimited: bool,
    settings: SubscriptionSettings | dict,
    now: datetime,
) -> dict:
    return _subscription_status_for_user(
        SimpleNamespace(
            is_admin=False,
            trial_started_at=trial_started_at,
            subscription_expires_at=subscription_expires_at,
            subscription_is_unlimited=subscription_is_unlimited,
        ),
        settings=settings,
        now=now,
    )


def _apply_subscription_code_to_state(
    *,
    trial_started_at: datetime | None,
    subscription_expires_at: datetime | None,
    subscription_is_unlimited: bool,
    code_duration_days: int | None,
    code_is_unlimited: bool,
    settings: SubscriptionSettings | dict,
    now: datetime,
) -> tuple[datetime | None, bool, datetime | None, bool]:
    status = _subscription_status_from_values(
        trial_started_at=trial_started_at,
        subscription_expires_at=subscription_expires_at,
        subscription_is_unlimited=subscription_is_unlimited,
        settings=settings,
        now=now,
    )

    if bool(code_is_unlimited):
        return None, True, None, True

    duration_days = int(code_duration_days or 0)
    if duration_days <= 0:
        raise ValueError("Code duration is missing.")

    base_point = now
    if status["active"] and not status["is_unlimited"] and status["effective_end_at"] is not None:
        base_point = max(now, status["effective_end_at"])
    applied_until = base_point + timedelta(days=duration_days)
    return applied_until, False, applied_until, False


def _recalculate_subscription_for_user(
    db,
    *,
    user: User,
    settings: SubscriptionSettings | dict,
) -> None:
    _ensure_user_trial_started(db, user)

    subscription_expires_at = None
    subscription_is_unlimited = False
    activations = db.execute(
        select(SubscriptionCodeActivation)
        .where(SubscriptionCodeActivation.user_id == user.id)
        .order_by(SubscriptionCodeActivation.activated_at.asc(), SubscriptionCodeActivation.id.asc())
    ).scalars().all()

    code_ids = sorted({int(activation.code_id) for activation in activations if activation.code_id is not None})
    code_map: dict[int, SubscriptionCode] = {}
    if code_ids:
        code_map = {
            code.id: code
            for code in db.execute(
                select(SubscriptionCode).where(SubscriptionCode.id.in_(code_ids))
            ).scalars().all()
        }

    for activation in activations:
        code = code_map.get(int(activation.code_id)) if activation.code_id is not None else None
        if code is None:
            if activation.was_unlimited or activation.applied_until is not None:
                subscription_expires_at = activation.applied_until
                subscription_is_unlimited = bool(activation.was_unlimited)
            else:
                activation.applied_until = None
                activation.was_unlimited = False
            db.add(activation)
            continue

        (
            subscription_expires_at,
            subscription_is_unlimited,
            activation.applied_until,
            activation.was_unlimited,
        ) = _apply_subscription_code_to_state(
            trial_started_at=user.trial_started_at,
            subscription_expires_at=subscription_expires_at,
            subscription_is_unlimited=subscription_is_unlimited,
            code_duration_days=code.duration_days,
            code_is_unlimited=bool(code.is_unlimited),
            settings=settings,
            now=activation.activated_at,
        )
        db.add(activation)

    user.subscription_expires_at = subscription_expires_at
    user.subscription_is_unlimited = subscription_is_unlimited
    db.add(user)


def _clear_user_subscription_activations(
    db,
    *,
    user: User,
) -> None:
    activations = db.execute(
        select(SubscriptionCodeActivation).where(SubscriptionCodeActivation.user_id == user.id)
    ).scalars().all()
    code_usage_by_id: dict[int, int] = {}
    for activation in activations:
        if activation.code_id is not None:
            code_usage_by_id[int(activation.code_id)] = code_usage_by_id.get(int(activation.code_id), 0) + 1
        db.delete(activation)

    if not code_usage_by_id:
        return

    for code in db.execute(
        select(SubscriptionCode).where(SubscriptionCode.id.in_(list(code_usage_by_id)))
    ).scalars().all():
        code.used_count = max(0, int(code.used_count or 0) - int(code_usage_by_id.get(int(code.id), 0)))
        db.add(code)


def _admin_set_user_subscription(
    db,
    *,
    user: User,
    duration_days: int | None,
    is_unlimited: bool,
    settings: SubscriptionSettings | dict,
    now: datetime | None = None,
) -> datetime | None:
    now = now or _utcnow()
    _ensure_user_trial_started(db, user, now=now)
    _clear_user_subscription_activations(db, user=user)

    if bool(is_unlimited):
        applied_until = None
        user.subscription_expires_at = None
        user.subscription_is_unlimited = True
    else:
        duration_days = int(duration_days or 0)
        if duration_days <= 0:
            raise ValueError("Subscription duration is missing.")
        applied_until = now + timedelta(days=duration_days)
        user.subscription_expires_at = applied_until
        user.subscription_is_unlimited = False

    db.add(user)
    db.add(SubscriptionCodeActivation(
        code_id=None,
        user_id=user.id,
        activated_at=now,
        applied_until=applied_until,
        was_unlimited=bool(is_unlimited),
    ))
    return applied_until


def _admin_clear_user_subscription(
    db,
    *,
    user: User,
) -> None:
    _clear_user_subscription_activations(db, user=user)
    user.subscription_expires_at = None
    user.subscription_is_unlimited = False
    db.add(user)


def _generate_subscription_code(db, *, length: int = 10) -> str:
    alphabet = string.ascii_uppercase + string.digits
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(length))
        exists = db.execute(select(SubscriptionCode.id).where(SubscriptionCode.code == candidate)).first()
        if not exists:
            return candidate


def _activate_subscription_code(
    db,
    *,
    user: User,
    raw_code: str,
    settings: SubscriptionSettings | dict,
    now: datetime | None = None,
) -> tuple[bool, str, dict | None]:
    now = now or _utcnow()
    code_text = str(raw_code or "").strip().upper()
    if not code_text:
        return False, "Введите код активации.", None

    code = db.execute(
        select(SubscriptionCode).where(
            SubscriptionCode.code == code_text,
            SubscriptionCode.is_active == True,
        )
    ).scalar_one_or_none()
    if code is None:
        return False, "Код не найден.", None
    if int(code.used_count or 0) >= int(code.max_activations or 0):
        return False, "Лимит активаций для этого кода уже исчерпан.", None

    _ensure_user_trial_started(db, user, now=now)
    status = _subscription_status_for_user(user, settings=settings, now=now)

    applied_until = None
    if bool(code.is_unlimited):
        user.subscription_is_unlimited = True
        user.subscription_expires_at = None
    else:
        duration_days = int(code.duration_days or 0)
        if duration_days <= 0:
            return False, "У кода не задан срок действия.", None
        base_point = now
        if status["active"] and not status["is_unlimited"] and status["effective_end_at"] is not None:
            base_point = max(now, status["effective_end_at"])
        applied_until = base_point + timedelta(days=duration_days)
        user.subscription_is_unlimited = False
        user.subscription_expires_at = applied_until

    code.used_count = int(code.used_count or 0) + 1
    activation = SubscriptionCodeActivation(
        code_id=code.id,
        user_id=user.id,
        applied_until=applied_until,
        was_unlimited=bool(code.is_unlimited),
    )
    db.add(user)
    db.add(code)
    db.add(activation)

    return True, "Код активирован.", {
        "code": code.code,
        "duration_label": _subscription_code_duration_label(
            duration_days=code.duration_days,
            is_unlimited=bool(code.is_unlimited),
        ),
        "applied_until": applied_until,
        "is_unlimited": bool(code.is_unlimited),
    }


def _apply_paid_subscription_plan(
    db,
    *,
    user: User,
    plan_key: str,
    settings: SubscriptionSettings | dict,
    now: datetime | None = None,
) -> tuple[datetime, dict]:
    now = now or _utcnow()
    plan = _subscription_plan_by_key(plan_key, settings=settings)
    if plan is None:
        raise ValueError("Unknown subscription plan")

    _ensure_user_trial_started(db, user, now=now)
    status = _subscription_status_for_user(user, settings=settings, now=now)

    duration_days = int(plan["duration_days"] or 0)
    if duration_days <= 0:
        raise ValueError("Subscription plan duration is missing")

    base_point = now
    if status["active"] and not status["is_unlimited"] and status["effective_end_at"] is not None:
        base_point = max(now, status["effective_end_at"])

    applied_until = base_point + timedelta(days=duration_days)
    user.subscription_is_unlimited = False
    user.subscription_expires_at = applied_until
    db.add(user)
    return applied_until, plan


def _user_shop_count(db, user_id: int) -> int:
    return int(
        db.execute(select(func.count(Shop.id)).where(Shop.owner_id == int(user_id))).scalar() or 0
    )


def _can_user_add_shop(
    db,
    *,
    user_id: int,
    settings: SubscriptionSettings | dict,
) -> tuple[bool, int, int]:
    limit = int(
        settings.max_shops_per_user if isinstance(settings, SubscriptionSettings)
        else settings.get("max_shops_per_user", 0)
    )
    current_count = _user_shop_count(db, user_id)
    if limit <= 0:
        return True, current_count, limit
    return current_count < limit, current_count, limit


def _get_subscription_context_for_user(user_id: int) -> dict:
    with SessionLocal() as db:
        user = db.get(User, int(user_id))
        settings = _get_or_create_subscription_settings(db)
        if user is not None and _ensure_user_trial_started(db, user):
            db.commit()
            db.refresh(user)
        status = _subscription_status_for_user(user, settings=settings)
        settings_dict = _subscription_settings_dict(settings)
        shop_count = _user_shop_count(db, int(user_id))
        max_shops = int(settings_dict.get("max_shops_per_user", 0) or 0)
        return {
            "settings": settings_dict,
            "status": status,
            "plans": _subscription_plan_rows(settings=settings),
            "shop_count": shop_count,
            "shops_left": max(0, max_shops - shop_count) if max_shops > 0 else None,
        }
