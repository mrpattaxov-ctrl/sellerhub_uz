"""Admin routes extracted from app.py as a Flask Blueprint."""
from __future__ import annotations

import threading
import time as _time
from datetime import date, datetime, timedelta

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import delete, desc, func, select
from sqlalchemy.exc import OperationalError
from werkzeug.security import generate_password_hash

from extensions import SessionLocal
from models import (
    ExpensesLedger,
    FinanceHourlySnapshot,
    FinanceOrder,
    PosActionLog,
    ProductGroup,
    SalesLine,
    Shop,
    ShopBackfillChunk,
    ShopSyncState,
    SubscriptionCode,
    SubscriptionCodeActivation,
    User,
    Variant,
    VariantSale,
)
from core.auth_helpers import (
    _current_user_is_admin,
    _json_response,
    _user_shop_ids,
)
from core.redis_client import revoke_user, unrevoke_user
from core.subscriptions import (
    _admin_clear_user_subscription,
    _admin_set_user_subscription,
    _can_user_add_shop,
    _ensure_user_trial_started,
    _generate_subscription_code,
    _get_or_create_subscription_settings,
    _invalidate_settings_cache,
    _invalidate_user_ctx_cache,
    _recalculate_subscription_for_user,
    _subscription_code_duration_label,
    _subscription_code_duration_rows,
    _subscription_plan_rows,
    _subscription_settings_dict,
    _subscription_status_for_user,
)

admin_bp = Blueprint("admin_bp", __name__)

_app = None


def init_admin_routes(app_module):
    global _app
    _app = app_module


def _fire_finance_seed(uzum_id: str, shop_pk: int):
    """Trigger background finance work for a newly added shop.

    Two daemon threads fire:
      1. _sync_finance_for_shop  — fast 30-day seed for Variant
         sales_30d_finance + avg_daily_sales (so Warehouse/POS reorder
         logic works within seconds of attach).
      2. _run_full_backfill_for_shop — slow first-sale-year → today
         backfill into finance_orders, day-by-day. ~30 minutes for an
         active shop. Runs sequentially; the user sees historical sales
         appear progressively in the UI.

    Both threads use the shop owner's per-user OpenAPI token. No queue,
    no chunked machinery — just thread + sleep + fetch.
    """
    def _run_variant_seed(uzum_id=uzum_id, shop_pk=shop_pk):
        try:
            _app._sync_finance_for_shop(uzum_id, shop_pk)
        except Exception as e:
            print(f"[AdminShop] Finance seed (variants) failed for {uzum_id}: {e}")

    def _run_full_backfill(uzum_id=uzum_id, shop_pk=shop_pk):
        try:
            _app._run_full_backfill_for_shop(uzum_id, shop_pk)
        except Exception as e:
            print(f"[AdminShop] Full backfill failed for {uzum_id}: {e}")

    threading.Thread(target=_run_variant_seed, daemon=True).start()
    threading.Thread(target=_run_full_backfill, daemon=True).start()


def _shop_limit_error_response(db, owner_id: int | None, *, existing_owner_id: int | None = None):
    if not owner_id or owner_id == existing_owner_id:
        return None
    owner = db.get(User, int(owner_id))
    if owner is None or owner.is_admin:
        return None
    settings = _get_or_create_subscription_settings(db)
    can_add, current_count, limit = _can_user_add_shop(
        db,
        user_id=owner.id,
        settings=settings,
    )
    if can_add:
        return None
    return _json_response({
        "error": f"Для аккаунта достигнут лимит магазинов: {limit}.",
        "shop_limit": limit,
        "current_count": current_count,
    }, 400)


# ----------------------------
# Shop Management API
# ----------------------------

@admin_bp.get("/api/shops")
@login_required
def get_shops():
    uid = int(current_user.get_id())
    shop_ids = _user_shop_ids(uid)
    with SessionLocal() as db:
        shops = db.execute(select(Shop).where(Shop.id.in_(shop_ids))).scalars().all()
        return _json_response({
            "shops": [{"id": s.id, "uzum_id": s.uzum_id, "name": s.name, "owner_id": s.owner_id} for s in shops]
        })

@admin_bp.post("/api/shops")
@login_required
def add_shop():
    """Add a shop. Admin can assign to any user; regular users auto-own the shop."""
    payload = request.get_json(force=True, silent=True) or {}
    uzum_id = str(payload.get("uzum_id") or "").strip()
    name = str(payload.get("name") or "").strip()

    if not uzum_id:
        return _json_response({"error": "uzum_id required"}, 400)

    uid = int(current_user.get_id())
    is_admin = _current_user_is_admin()

    # Admin can set owner_id explicitly; regular users always own the shop themselves
    if is_admin:
        owner_id = payload.get("owner_id")
        resolved_owner = int(owner_id) if owner_id else None
    else:
        resolved_owner = uid

    with SessionLocal() as db:
        existing = db.execute(select(Shop).where(Shop.uzum_id == uzum_id)).scalar_one_or_none()
        if existing:
            # Regular user can only update shops they own or unowned shops
            if not is_admin and existing.owner_id is not None and existing.owner_id != uid:
                return _json_response({"error": "Shop belongs to another user"}, 403)
            limit_error = _shop_limit_error_response(
                db,
                resolved_owner,
                existing_owner_id=existing.owner_id,
            )
            if limit_error is not None:
                return limit_error
            if name:
                existing.name = name
            if is_admin and payload.get("owner_id") is not None:
                existing.owner_id = resolved_owner
            elif not is_admin and existing.owner_id is None:
                existing.owner_id = uid
            db.commit()
            _fire_finance_seed(uzum_id, existing.id)
            return _json_response({"ok": True, "id": existing.id})

        limit_error = _shop_limit_error_response(db, resolved_owner)
        if limit_error is not None:
            return limit_error
        s = Shop(uzum_id=uzum_id, name=name or f"Shop {uzum_id}", owner_id=resolved_owner)
        db.add(s)
        db.commit()
        db.refresh(s)
        _fire_finance_seed(uzum_id, s.id)
        return _json_response({"ok": True, "id": s.id})


# ----------------------------
# OpenAPI-token shop discovery + bulk attach
# ----------------------------

@admin_bp.post("/api/shops/openapi/discover")
@login_required
def discover_shops_via_openapi():
    """Probe Uzum Seller OpenAPI /v1/shops with the user-supplied token.

    Body: { "token": "<openapi token>" }  (optional — falls back to the
    token saved on the user row)
    Returns: { "shops": [{ "uzum_id", "name", "already_added", "owned_by_other" }] }

    The browser MUST call this (and never Uzum directly) — Uzum's CORS
    policy blocks cross-origin requests from the seller's browser.
    """
    from core.uzum_openapi import list_owned_shops

    payload = request.get_json(force=True, silent=True) or {}
    token = str(payload.get("token") or "").strip()

    uid = int(current_user.get_id())

    with SessionLocal() as db:
        user = db.get(User, uid)
        if user is None:
            return _json_response({"error": "User not found"}, 404)
        if not token:
            token = (user.uzum_openapi_token or "").strip()
        if not token:
            return _json_response({"error": "OpenAPI token required"}, 400)

        try:
            shops = list_owned_shops(token)
        except Exception as exc:
            return _json_response({"error": f"Uzum OpenAPI error: {exc}"}, 400)

        # Persist the token on first successful probe (or refresh it if changed)
        if user.uzum_openapi_token != token:
            user.uzum_openapi_token = token
            db.commit()

        # Annotate each shop with current ownership state
        uzum_ids = [s["uzum_id"] for s in shops if s.get("uzum_id")]
        existing_rows: dict[str, Shop] = {}
        if uzum_ids:
            for row in db.execute(select(Shop).where(Shop.uzum_id.in_(uzum_ids))).scalars():
                existing_rows[row.uzum_id] = row

        result = []
        for s in shops:
            uid_str = s["uzum_id"]
            existing = existing_rows.get(uid_str)
            already_added = existing is not None and existing.owner_id == uid
            owned_by_other = (
                existing is not None
                and existing.owner_id is not None
                and existing.owner_id != uid
            )
            result.append({
                "uzum_id": uid_str,
                "name": s.get("name") or "",
                "already_added": already_added,
                "owned_by_other": owned_by_other,
            })

    return _json_response({"shops": result})


@admin_bp.post("/api/shops/openapi/attach")
@login_required
def attach_shops_via_openapi():
    """Bulk-attach shops picked from the OpenAPI discovery list.

    Body: { "shops": [{ "uzum_id": "...", "name": "..." }, ...] }
    Returns: { "added": [...], "skipped": [{uzum_id, reason}], "shop_count": N }

    Mirrors the single-shop add_shop() semantics: upserts the Shop row,
    sets owner_id to the current user (or leaves an admin-owned shop
    alone), respects the per-user shop limit, and fires a background
    finance seed for each newly attached shop.
    """
    payload = request.get_json(force=True, silent=True) or {}
    items = payload.get("shops") or []
    if not isinstance(items, list) or not items:
        return _json_response({"error": "shops[] required"}, 400)

    uid = int(current_user.get_id())
    is_admin = _current_user_is_admin()

    added: list[dict] = []
    skipped: list[dict] = []
    seeds: list[tuple[str, int]] = []

    with SessionLocal() as db:
        settings = _get_or_create_subscription_settings(db)
        # Snapshot current count once; we'll decrement headroom as we add.
        if not is_admin:
            _, current_count, limit = _can_user_add_shop(
                db, user_id=uid, settings=settings,
            )
            headroom = max(0, int(limit) - int(current_count))
        else:
            headroom = 10**9  # effectively unlimited for admins

        for item in items:
            if not isinstance(item, dict):
                continue
            uzum_id = str(item.get("uzum_id") or "").strip()
            if not uzum_id:
                continue
            name = str(item.get("name") or "").strip()

            existing = db.execute(
                select(Shop).where(Shop.uzum_id == uzum_id)
            ).scalar_one_or_none()

            if existing is not None:
                if existing.owner_id == uid:
                    skipped.append({"uzum_id": uzum_id, "reason": "already_added"})
                    continue
                if existing.owner_id is not None and not is_admin:
                    skipped.append({"uzum_id": uzum_id, "reason": "owned_by_other"})
                    continue
                # Claim an unowned shop (regular user) or leave admin-assignable
                if headroom <= 0 and not is_admin:
                    skipped.append({"uzum_id": uzum_id, "reason": "limit_reached"})
                    continue
                if existing.owner_id is None:
                    existing.owner_id = uid
                if name and not existing.name:
                    existing.name = name
                db.flush()
                added.append({"uzum_id": uzum_id, "id": existing.id})
                seeds.append((uzum_id, existing.id))
                headroom -= 1
                continue

            if headroom <= 0 and not is_admin:
                skipped.append({"uzum_id": uzum_id, "reason": "limit_reached"})
                continue

            shop = Shop(
                uzum_id=uzum_id,
                name=name or f"Shop {uzum_id}",
                owner_id=uid if not is_admin else None,
            )
            db.add(shop)
            db.flush()
            added.append({"uzum_id": uzum_id, "id": shop.id})
            seeds.append((uzum_id, shop.id))
            headroom -= 1

        db.commit()

        # Updated shop count for the UI pill (regular users only)
        if not is_admin:
            _, current_count, limit = _can_user_add_shop(
                db, user_id=uid, settings=settings,
            )
        else:
            current_count, limit = 0, 0

    for uzum_id, shop_pk in seeds:
        _fire_finance_seed(uzum_id, shop_pk)

    return _json_response({
        "added": added,
        "skipped": skipped,
        "shop_count": current_count,
        "shop_limit": limit,
    })


@admin_bp.post("/api/shops/<int:shop_id>/assign")
@login_required
def assign_shop(shop_id: int):
    """Admin assigns a shop to a user (or unassigns with owner_id=null)."""
    if not _current_user_is_admin():
        return _json_response({"error": "Admin only"}, 403)
    payload = request.get_json(force=True, silent=True) or {}
    owner_id = payload.get("owner_id")
    with SessionLocal() as db:
        shop = db.get(Shop, shop_id)
        if not shop:
            return _json_response({"error": "Shop not found"}, 404)
        target_owner_id = int(owner_id) if owner_id else None
        limit_error = _shop_limit_error_response(
            db,
            target_owner_id,
            existing_owner_id=shop.owner_id,
        )
        if limit_error is not None:
            return limit_error
        shop.owner_id = target_owner_id
        db.commit()
    return _json_response({"ok": True})

@admin_bp.delete("/api/shops/<int:shop_id>")
@login_required
def delete_shop(shop_id: int):
    """Delete a shop. Users can only delete their own shops; admin can delete any unowned shop.

    Runs under a deadlock-retry loop: the cascade touches Variant/ProductGroup
    while the background sales-ingest loops hold locks on the same rows
    (sku->shop routing reads + sales_lines DELETE+INSERT). Postgres aborts
    one side as the deadlock victim; we simply retry the whole transaction
    on a fresh session. Also purges the new-pipeline tables
    (sales_lines / expenses_ledger / shop_backfill_chunks / shop_sync_state)
    keyed by int(Shop.uzum_id) so a deleted shop leaves no orphan rows.
    """
    uid = int(current_user.get_id())
    max_attempts = 4

    for attempt in range(1, max_attempts + 1):
        try:
            with SessionLocal() as db:
                shop = db.get(Shop, shop_id)
                if not shop:
                    return _json_response({"error": "Shop not found"}, 404)
                # Permission: must own the shop (or be admin)
                if not _current_user_is_admin() and shop.owner_id != uid:
                    return _json_response({"error": "Access denied"}, 403)

                # New-pipeline tables key on the Uzum shop id (int), not the
                # local PK. Resolve it before the Shop row is deleted.
                uzum_id_int = None
                try:
                    uzum_id_int = int(str(shop.uzum_id).strip())
                except (TypeError, ValueError):
                    uzum_id_int = None

                # Cascade delete: VariantSale -> Variant -> ProductGroup -> Shop
                group_ids = db.execute(
                    select(ProductGroup.id).where(ProductGroup.shop_id == shop_id)
                ).scalars().all()
                if group_ids:
                    variant_ids = db.execute(
                        select(Variant.id).where(Variant.group_id.in_(group_ids))
                    ).scalars().all()
                    if variant_ids:
                        db.execute(delete(VariantSale).where(VariantSale.variant_id.in_(variant_ids)))
                        db.execute(delete(Variant).where(Variant.id.in_(variant_ids)))
                    db.execute(delete(ProductGroup).where(ProductGroup.id.in_(group_ids)))

                # pos_action_log has FK -> shops.id; clear it so the Shop
                # delete doesn't fail with an IntegrityError.
                db.execute(delete(PosActionLog).where(PosActionLog.shop_id == shop_id))

                # Purge new-pipeline data so the deleted shop leaves no
                # orphan rows. shop_id columns mix conventions: SalesLine /
                # ExpensesLedger / ShopBackfillChunk / ShopSyncState use
                # int; FinanceOrder / FinanceHourlySnapshot use string
                # (matches Shop.uzum_id varchar).
                if uzum_id_int is not None:
                    db.execute(delete(SalesLine).where(SalesLine.shop_id == uzum_id_int))
                    db.execute(delete(ExpensesLedger).where(ExpensesLedger.shop_id == uzum_id_int))
                    db.execute(delete(ShopBackfillChunk).where(ShopBackfillChunk.shop_id == uzum_id_int))
                    db.execute(delete(ShopSyncState).where(ShopSyncState.shop_id == uzum_id_int))
                uzum_id_str = (shop.uzum_id or "").strip()
                if uzum_id_str:
                    db.execute(delete(FinanceOrder).where(FinanceOrder.shop_id == uzum_id_str))
                    db.execute(delete(FinanceHourlySnapshot).where(FinanceHourlySnapshot.shop_id == uzum_id_str))

                db.delete(shop)
                db.commit()
            return _json_response({"ok": True})
        except OperationalError as exc:
            is_deadlock = "deadlock" in str(getattr(exc, "orig", exc)).lower()
            if is_deadlock and attempt < max_attempts:
                _time.sleep(0.2 * attempt)
                continue
            raise
    return _json_response({"error": "Could not delete shop, please retry"}, 503)


@admin_bp.get("/my-shops")
@login_required
def my_shops_page():
    return redirect(url_for("products_bp.fetch_page"))


# ----------------------------
# Admin: User Management
# ----------------------------
@admin_bp.get("/admin/users")
@login_required
def admin_users_page():
    if not _current_user_is_admin():
        return redirect(url_for("products_bp.groups_page"))
    with SessionLocal() as db:
        users = db.execute(select(User)).scalars().all()
        shops = db.execute(select(Shop)).scalars().all()
    return render_template("admin_users.html", users=users, shops=shops)

@admin_bp.post("/api/admin/users")
@login_required
def admin_create_user():
    """Admin creates a new seller account."""
    if not _current_user_is_admin():
        return _json_response({"error": "Admin only"}, 403)
    payload = request.get_json(force=True, silent=True) or {}
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "").strip()
    if not username or not password:
        return _json_response({"error": "username and password required"}, 400)
    with SessionLocal() as db:
        if db.execute(select(User).where(User.username == username)).scalar_one_or_none():
            return _json_response({"error": "Username already exists"}, 409)
        user = User(username=username, password_hash=generate_password_hash(password), is_admin=False)
        _ensure_user_trial_started(db, user)
        db.add(user)
        db.commit()
        return _json_response({"ok": True, "id": user.id, "username": user.username})

@admin_bp.delete("/api/admin/users/<int:user_id>")
@login_required
def admin_delete_user(user_id: int):
    if not _current_user_is_admin():
        return _json_response({"error": "Admin only"}, 403)
    if user_id == int(current_user.get_id()):
        return _json_response({"error": "Cannot delete your own account"}, 400)
    with SessionLocal() as db:
        user = db.get(User, user_id)
        if not user:
            return _json_response({"error": "User not found"}, 404)
        # Unassign their shops instead of deleting them
        db.execute(select(Shop).where(Shop.owner_id == user_id))
        for shop in db.execute(select(Shop).where(Shop.owner_id == user_id)).scalars().all():
            shop.owner_id = None
        db.delete(user)
        db.commit()
    return _json_response({"ok": True})

@admin_bp.get("/api/admin/users")
@login_required
def admin_list_users():
    if not _current_user_is_admin():
        return _json_response({"error": "Admin only"}, 403)
    with SessionLocal() as db:
        users = db.execute(select(User)).scalars().all()
        shops = db.execute(select(Shop)).scalars().all()
        shop_map: dict[int, list] = {}
        for s in shops:
            if s.owner_id:
                shop_map.setdefault(s.owner_id, []).append({"id": s.id, "uzum_id": s.uzum_id, "name": s.name})
        return _json_response({"users": [
            {"id": u.id, "username": u.username, "is_admin": u.is_admin,
             "shops": shop_map.get(u.id, [])}
            for u in users
        ]})


@admin_bp.route("/admin/subscriptions", methods=["GET", "POST"])
@login_required
def admin_subscriptions_page():
    if not _current_user_is_admin():
        return redirect(url_for("products_bp.groups_page"))

    duration_options = _subscription_code_duration_rows()

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()
        with SessionLocal() as db:
            settings = _get_or_create_subscription_settings(db)
            if action == "settings":
                try:
                    trial_days = max(0, int(request.form.get("trial_days") or settings.trial_days))
                    monthly_price_sum = max(0, int(request.form.get("monthly_price_sum") or settings.monthly_price_sum))
                    max_shops_per_user = max(1, int(request.form.get("max_shops_per_user") or settings.max_shops_per_user))
                except ValueError:
                    flash("Некорректные настройки подписки.")
                    return redirect(url_for("admin_bp.admin_subscriptions_page"))
                settings.trial_days = trial_days
                settings.monthly_price_sum = monthly_price_sum
                settings.max_shops_per_user = max_shops_per_user
                db.add(settings)
                db.commit()
                _invalidate_settings_cache()
                flash("Настройки подписки сохранены.")
                return redirect(url_for("admin_bp.admin_subscriptions_page"))

            if action == "create_code":
                duration_key = (request.form.get("duration_key") or "").strip()
                duration_cfg = next(
                    (item for item in duration_options if item["key"] == duration_key),
                    None,
                )
                if duration_cfg is None:
                    flash("Выберите срок действия кода.")
                    return redirect(url_for("admin_bp.admin_subscriptions_page"))
                try:
                    max_activations = max(1, int(request.form.get("max_activations") or 1))
                except ValueError:
                    flash("Некорректный лимит активаций.")
                    return redirect(url_for("admin_bp.admin_subscriptions_page"))

                code_value = _generate_subscription_code(db)
                db.add(SubscriptionCode(
                    code=code_value,
                    duration_days=duration_cfg["duration_days"],
                    is_unlimited=bool(duration_cfg["is_unlimited"]),
                    max_activations=max_activations,
                    created_by_user_id=int(current_user.get_id()),
                ))
                db.commit()
                flash(f"Код создан: {code_value}")
                return redirect(url_for("admin_bp.admin_subscriptions_page"))

            if action == "update_code":
                try:
                    code_id = int(request.form.get("code_id") or 0)
                except ValueError:
                    flash("Некорректный код.")
                    return redirect(url_for("admin_bp.admin_subscriptions_page"))
                code = db.get(SubscriptionCode, code_id)
                if code is None:
                    flash("Код не найден.")
                    return redirect(url_for("admin_bp.admin_subscriptions_page"))
                duration_key = (request.form.get("duration_key") or "").strip()
                duration_cfg = next(
                    (item for item in duration_options if item["key"] == duration_key),
                    None,
                )
                if duration_cfg is None:
                    flash("Выберите новый срок действия кода.")
                    return redirect(url_for("admin_bp.admin_subscriptions_page"))
                affected_user_ids = {
                    int(user_id)
                    for user_id in db.execute(
                        select(SubscriptionCodeActivation.user_id)
                        .where(SubscriptionCodeActivation.code_id == code.id)
                    ).scalars().all()
                }
                code.duration_days = duration_cfg["duration_days"]
                code.is_unlimited = bool(duration_cfg["is_unlimited"])
                db.add(code)
                for user_id in affected_user_ids:
                    user = db.get(User, user_id)
                    if user is not None:
                        _recalculate_subscription_for_user(db, user=user, settings=settings)
                db.commit()
                flash(f"Срок кода {code.code} обновлён.")
                return redirect(url_for("admin_bp.admin_subscriptions_page"))

            if action == "delete_code":
                try:
                    code_id = int(request.form.get("code_id") or 0)
                except ValueError:
                    flash("Некорректный код.")
                    return redirect(url_for("admin_bp.admin_subscriptions_page"))
                code = db.get(SubscriptionCode, code_id)
                if code is None:
                    flash("Код не найден.")
                    return redirect(url_for("admin_bp.admin_subscriptions_page"))
                activations = db.execute(
                    select(SubscriptionCodeActivation).where(SubscriptionCodeActivation.code_id == code.id)
                ).scalars().all()
                affected_user_ids = {int(activation.user_id) for activation in activations}
                for activation in activations:
                    db.delete(activation)
                code_value = code.code
                db.delete(code)
                for user_id in affected_user_ids:
                    user = db.get(User, user_id)
                    if user is not None:
                        _recalculate_subscription_for_user(db, user=user, settings=settings)
                db.commit()
                for uid in affected_user_ids:
                    _invalidate_user_ctx_cache(uid)
                flash(f"Код {code_value} удалён.")
                return redirect(url_for("admin_bp.admin_subscriptions_page"))

            if action == "update_user_subscription":
                try:
                    user_id = int(request.form.get("user_id") or 0)
                except ValueError:
                    flash("Некорректный пользователь.")
                    return redirect(url_for("admin_bp.admin_subscriptions_page"))
                user = db.get(User, user_id)
                if user is None or user.is_admin:
                    flash("Пользователь не найден.")
                    return redirect(url_for("admin_bp.admin_subscriptions_page"))
                duration_key = (request.form.get("duration_key") or "").strip()
                duration_cfg = next(
                    (item for item in duration_options if item["key"] == duration_key),
                    None,
                )
                if duration_cfg is None:
                    flash("Выберите срок подписки.")
                    return redirect(url_for("admin_bp.admin_subscriptions_page"))
                _admin_set_user_subscription(
                    db,
                    user=user,
                    duration_days=duration_cfg["duration_days"],
                    is_unlimited=bool(duration_cfg["is_unlimited"]),
                    settings=settings,
                )
                db.commit()
                _invalidate_user_ctx_cache(user.id)
                # A fresh subscription clears any stale revoke on this user.
                # Their session keys remain stale until the gate's slow path
                # refreshes them on their next request — which is fine since
                # they're now authorized.
                unrevoke_user(user.id)
                flash(f"Подписка пользователя {user.username} обновлена.")
                return redirect(url_for("admin_bp.admin_subscriptions_page"))

            if action == "delete_user_subscription":
                try:
                    user_id = int(request.form.get("user_id") or 0)
                except ValueError:
                    flash("Некорректный пользователь.")
                    return redirect(url_for("admin_bp.admin_subscriptions_page"))
                user = db.get(User, user_id)
                if user is None or user.is_admin:
                    flash("Пользователь не найден.")
                    return redirect(url_for("admin_bp.admin_subscriptions_page"))
                _admin_clear_user_subscription(db, user=user)
                db.commit()
                _invalidate_user_ctx_cache(user.id)
                # Force the target user out on their next request — their
                # signed session might still say "active" until they log in
                # again, and the blocklist is how we close that window.
                revoke_user(user.id)
                flash(f"Подписка пользователя {user.username} удалена.")
                return redirect(url_for("admin_bp.admin_subscriptions_page"))

    with SessionLocal() as db:
        settings = _get_or_create_subscription_settings(db)
        users = db.execute(select(User).order_by(User.is_admin.desc(), User.username.asc())).scalars().all()
        changed = False
        for user in users:
            if _ensure_user_trial_started(db, user):
                changed = True
        if changed:
            db.commit()

        shop_counts: dict[int, int] = {}
        for shop in db.execute(select(Shop)).scalars().all():
            if shop.owner_id:
                shop_counts[int(shop.owner_id)] = shop_counts.get(int(shop.owner_id), 0) + 1

        user_rows = []
        for user in users:
            status = _subscription_status_for_user(user, settings=settings)
            user_rows.append({
                "id": user.id,
                "username": user.username,
                "is_admin": bool(user.is_admin),
                "shop_count": shop_counts.get(user.id, 0),
                "status": status,
                "duration_key": "unlimited" if bool(user.subscription_is_unlimited) else "1m",
            })

        code_rows = []
        for code in db.execute(
            select(SubscriptionCode).order_by(desc(SubscriptionCode.created_at)).limit(100)
        ).scalars().all():
            duration_key = next(
                (
                    item["key"]
                    for item in duration_options
                    if bool(item["is_unlimited"]) == bool(code.is_unlimited)
                    and item["duration_days"] == code.duration_days
                ),
                "",
            )
            code_rows.append({
                "id": code.id,
                "code": code.code,
                "duration_key": duration_key,
                "duration_label": _subscription_code_duration_label(
                    duration_days=code.duration_days,
                    is_unlimited=bool(code.is_unlimited),
                ),
                "max_activations": int(code.max_activations or 0),
                "used_count": int(code.used_count or 0),
                "is_active": bool(code.is_active),
                "created_at": code.created_at,
            })

        activation_rows = []
        activation_query = db.execute(
            select(SubscriptionCodeActivation, User, SubscriptionCode)
            .join(User, SubscriptionCodeActivation.user_id == User.id)
            .outerjoin(SubscriptionCode, SubscriptionCodeActivation.code_id == SubscriptionCode.id)
            .order_by(desc(SubscriptionCodeActivation.activated_at))
            .limit(30)
        ).all()
        for activation, user, code in activation_query:
            activation_rows.append({
                "username": user.username,
                "code": code.code if code else "—",
                "activated_at": activation.activated_at,
                "applied_until": activation.applied_until,
                "was_unlimited": bool(activation.was_unlimited),
            })

        subscription_overview = {
            "total_users": sum(1 for row in user_rows if not row["is_admin"]),
            "paid_users": sum(
                1 for row in user_rows
                if not row["is_admin"] and row["status"]["active"] and row["status"]["state"] == "paid"
            ),
            "trial_users": sum(
                1 for row in user_rows
                if not row["is_admin"] and row["status"]["active"] and row["status"]["state"] == "trial"
            ),
            "expired_users": sum(
                1 for row in user_rows
                if not row["is_admin"] and not row["status"]["active"]
            ),
            "unlimited_users": sum(
                1 for row in user_rows
                if not row["is_admin"] and row["status"]["is_unlimited"]
            ),
            "active_codes": sum(
                1 for row in code_rows
                if row["is_active"] and row["used_count"] < row["max_activations"]
            ),
            "total_codes": len(code_rows),
        }

        return render_template(
            "admin_subscriptions.html",
            subscription_settings=_subscription_settings_dict(settings),
            subscription_plans=_subscription_plan_rows(settings=settings),
            code_duration_options=duration_options,
            subscription_overview=subscription_overview,
            user_rows=user_rows,
            code_rows=code_rows,
            activation_rows=activation_rows,
        )
