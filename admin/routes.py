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
    AdminAlertChat,
    ErrorEvent,
    ExpensesLedger,
    FinanceHourlySnapshot,
    FinanceOrder,
    PosActionLog,
    ProductGroup,
    Shop,
    ShopSyncState,
    SubscriptionCode,
    SubscriptionCodeActivation,
    User,
    Variant,
)
from core.auth_helpers import (
    _current_user_is_admin,
    _json_response,
    _user_shop_ids,
)
from core.redis_client import redis_client, revoke_user, unrevoke_user, mark_user_for_recheck
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


#start the fetching all dada from uzum for the newly added shop in the background

def _fire_finance_seed(uzum_id: str, shop_pk: int):
    """Trigger background finance work for a newly added shop.

    Fires one orchestrator daemon thread that runs three jobs in parallel
    and then sends a post-backfill summary:

      1. ``_run_full_backfill_for_shop`` — quarter-chunked OpenAPI sales
         backfill into ``finance_orders`` (~1-2 min for a 2-year shop).
      2. ``_run_full_expenses_backfill_for_shop`` — year-chunked OpenAPI
         expenses backfill into ``expenses_ledger`` (~30s-2 min depending
         on volume).
      3. ``_sync_products_via_openapi`` — products sync that ALSO fans out
         to the admin-token ``/sku-list`` endpoint to populate per-SKU
         ``Variant.image_url`` (POS/invoice/print/variants page all read
         this). Skipped silently if the shop owner has no OpenAPI token.
      4. After all finish, ``_send_post_backfill_summary`` posts a
         Telegram message to the shop owner with yesterday's sales +
         expenses, so the operator can verify both pipelines populated.

    All three jobs share the same TokenBucket so we don't exceed Uzum's
    rate even when bursting on add-shop. Variant.avg_daily_sales /
    sales_30d_finance stay at 0 until the user clicks "Sync Finance"
    (POST /api/uzum/sync-finance), which reads the populated finance_orders.
    """
    def _safe_call(fn, label, *args):
        try:
            fn(*args)
        except Exception as e:
            print(f"[AdminShop] {label} failed for {args[0] if args else '?'}: {e}")
            # Add-shop backfill failure (sales/expenses/products) — this is
            # where a saved-but-unusable OpenAPI token first shows up.
            try:
                from core.error_monitor import report_background_error
                report_background_error(f"Добавление магазина: {label}",
                                        args[0] if args else None, repr(e))
            except Exception:
                pass

    def _run_products_burst(uzum_id=uzum_id, shop_pk=shop_pk):
        owner_token = ""
        try:
            with SessionLocal() as db:
                shop_row = db.get(Shop, shop_pk)
                if shop_row and shop_row.owner_id:
                    owner = db.get(User, int(shop_row.owner_id))
                    if owner:
                        owner_token = (owner.uzum_openapi_token or "").strip()
        except Exception as e:
            print(f"[AdminShop] products burst owner-token lookup failed for {uzum_id}: {e}")
            return
        if not owner_token:
            print(f"[AdminShop] products burst skipped for shop {uzum_id}: owner has no OpenAPI token")
            return
        # Products sync first (creates the Variant rows). Then /sku-list to
        # populate per-SKU image URLs. The products sync deliberately does
        # NOT write image_url anymore (see app.py::_sync_products_via_openapi),
        # so this is the only path that touches variant images on add-shop.
        _safe_call(_app._sync_products_via_openapi,
                   "products burst", uzum_id, owner_token)
        try:
            from core.uzum_skulist import refresh_sku_images_for_shop
            refresh_sku_images_for_shop(uzum_id, shop_pk)
        except Exception as e:
            print(f"[fetch] sku-list image refresh failed for shop {uzum_id}: {e}")

    def _run_fbs_seed(uzum_id=uzum_id, shop_pk=shop_pk):
        """Initial FBS order backfill for THIS freshly-added shop only.

        The накладные list is scope-filtered by the local set of
        ``fbs_orders.invoice_number`` (see ``fbs.routes._filter_invoices_to_owned``),
        which the order-sync worker populates. Without seeding, a newly added
        shop's EXISTING накладные stay hidden until the worker's next full
        sweep (~10 min). This pulls the new shop's orders across ALL statuses
        NOW — including orders already on an invoice (PENDING_DELIVERY) and
        terminal ones (ACCEPTED/CANCELLED invoices) — so its накладные appear
        on the very next list reload.

        Scoped to ``[uzum_id]`` — never re-syncs the user's other shops. Each
        status is one paced ``/v2/fbs/orders`` call through the shared
        per-token gate, so this never bursts Uzum's per-token rate limit.
        """
        owner_token = ""
        try:
            with SessionLocal() as db:
                shop_row = db.get(Shop, shop_pk)
                if shop_row and shop_row.owner_id:
                    owner = db.get(User, int(shop_row.owner_id))
                    if owner:
                        owner_token = (owner.uzum_openapi_token or "").strip()
        except Exception as e:
            print(f"[AdminShop] FBS seed owner-token lookup failed for {uzum_id}: {e}")
            return
        if not owner_token:
            print(f"[AdminShop] FBS seed skipped for shop {uzum_id}: owner has no OpenAPI token")
            return
        try:
            from core.fbs_data import _refresh_shops_status
            from core.fbs_sync import FBS_ALL_SYNC_STATUSES
        except Exception as e:
            print(f"[AdminShop] FBS seed import failed for shop {uzum_id}: {e}")
            return
        total = 0
        seed_errors: list[str] = []
        for status in FBS_ALL_SYNC_STATUSES:
            try:
                total += _refresh_shops_status(owner_token, [uzum_id], status)
            except Exception as e:
                print(f"[AdminShop] FBS seed status={status} failed for shop {uzum_id}: {e}")
                seed_errors.append(f"{status}: {e!r}")
        if seed_errors:
            # One report per seed run, not one per failed status.
            try:
                from core.error_monitor import report_background_error
                report_background_error("Добавление магазина: FBS seed", uzum_id,
                                        "; ".join(seed_errors)[:500])
            except Exception:
                pass
        print(f"[AdminShop] FBS seed done shop={uzum_id}: {total} order(s) synced")

    def _orchestrate(uzum_id=uzum_id, shop_pk=shop_pk):
        t_sales = threading.Thread(
            target=lambda: _safe_call(_app._run_full_backfill_for_shop,
                                      "sales backfill", uzum_id, shop_pk),
            daemon=True,
            name=f"backfill-sales-{uzum_id}",
        )
        t_expenses = threading.Thread(
            target=lambda: _safe_call(_app._run_full_expenses_backfill_for_shop,
                                      "expenses backfill", uzum_id, shop_pk),
            daemon=True,
            name=f"backfill-expenses-{uzum_id}",
        )
        t_products = threading.Thread(
            target=_run_products_burst,
            daemon=True,
            name=f"backfill-products-{uzum_id}",
        )
        # FBS order seed — scoped to THIS shop, so its накладные show up on the
        # next list reload instead of waiting for the worker's ~10-min sweep.
        t_fbs = threading.Thread(
            target=_run_fbs_seed,
            daemon=True,
            name=f"backfill-fbs-{uzum_id}",
        )
        t_sales.start()
        t_expenses.start()
        t_products.start()
        t_fbs.start()
        t_sales.join()
        t_expenses.join()
        t_products.join()
        t_fbs.join()
        _safe_call(_app._send_post_backfill_summary,
                   "post-backfill summary", uzum_id, shop_pk)

    threading.Thread(target=_orchestrate, daemon=True,
                     name=f"backfill-orchestrate-{uzum_id}").start()


# Manual "Синхронизация" button on the /fetch page. It must pull ALL data exactly
# like a freshly added shop, and be un-spammable — one full run per shop per window.
_RESYNC_COOLDOWN_SEC = 600  # 10 minutes per shop


def _resync_cooldown_key(uzum_id: str) -> str:
    return f"resync:cooldown:{uzum_id}"


@admin_bp.post("/api/shops/<uzum_id>/resync")
@login_required
def resync_shop(uzum_id: str):
    """Manual full re-sync for one shop — identical background backfill to add-shop.

    Fires the SAME orchestrator used when a shop is first attached
    (``_fire_finance_seed``): full sales backfill + full expenses backfill +
    products sync (+ per-SKU images) + FBS order seed + post-backfill summary.
    Returns immediately; the work runs in background daemon threads.

    Rate-limited to once per ``_RESYNC_COOLDOWN_SEC`` per shop via a Redis NX
    key so a user can't spam it. On cooldown, returns 429 with ``retry_after``
    (seconds remaining). Fails open if Redis is unavailable — a cache outage
    should not block a legitimate sync.
    """
    uzum_id = str(uzum_id or "").strip()
    if not uzum_id:
        return _json_response({"error": "uzum_id required"}, 400)

    uid = int(current_user.get_id())
    is_admin = _current_user_is_admin()

    # Ownership check + resolve the DB primary key the orchestrator needs.
    with SessionLocal() as db:
        shop = db.execute(
            select(Shop).where(Shop.uzum_id == uzum_id)
        ).scalar_one_or_none()
        if shop is None:
            return _json_response({"error": "Shop not found"}, 404)
        if not is_admin and shop.owner_id != uid:
            return _json_response({"error": "Access denied to this shop"}, 403)
        shop_pk = int(shop.id)

    # Atomic per-shop rate limit: SET NX with a TTL. If the key already exists,
    # someone synced this shop within the window — reject with time remaining.
    key = _resync_cooldown_key(uzum_id)
    try:
        acquired = redis_client.set(
            key, str(int(_time.time())), nx=True, ex=_RESYNC_COOLDOWN_SEC
        )
    except Exception as e:
        print(f"[Resync] cooldown check failed (allowing) for {uzum_id}: {e}")
        acquired = True  # fail open

    if not acquired:
        try:
            ttl = int(redis_client.ttl(key))
        except Exception:
            ttl = _RESYNC_COOLDOWN_SEC
        if ttl < 0:
            ttl = _RESYNC_COOLDOWN_SEC
        return _json_response({
            "error": "cooldown",
            "retry_after": ttl,
            "cooldown_sec": _RESYNC_COOLDOWN_SEC,
        }, 429)

    _fire_finance_seed(uzum_id, shop_pk)
    return _json_response({
        "ok": True,
        "started": True,
        "shop_id": uzum_id,
        "cooldown_sec": _RESYNC_COOLDOWN_SEC,
    })


#shop limit error response for the user if the user has reached the limit of shops that can be added to their account. This is used in the add_shop and assign_shop endpoints to prevent users from exceeding their shop limit.

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


def _safe_invalidate_ctx(*user_ids) -> None:
    """Bust each affected user's subscription-context cache after a shop-count
    change (add / delete / reassign). The card's "N / limit магазинов" number
    is a Redis blob (``sub_ctx:{uid}``) that these routes would otherwise leave
    stale until its TTL. Guarded so a Redis hiccup can't fail a request whose DB
    commit already succeeded — the card self-heals on TTL anyway. None ids
    (e.g. unowned shops) are skipped."""
    for _uid in {u for u in user_ids if u is not None}:
        try:
            _invalidate_user_ctx_cache(int(_uid))
        except Exception as _e:  # best-effort: never break the request
            print(f"[shops] ctx-cache invalidate skipped for {_uid}: {_e}")


#function to get shops from db
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


#we use it only for shop edit for now
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
            _safe_invalidate_ctx(resolved_owner)
            _fire_finance_seed(uzum_id, existing.id)
            return _json_response({"ok": True, "id": existing.id})

        limit_error = _shop_limit_error_response(db, resolved_owner)
        if limit_error is not None:
            return limit_error
        s = Shop(uzum_id=uzum_id, name=name or f"Shop {uzum_id}", owner_id=resolved_owner)
        db.add(s)
        db.commit()
        db.refresh(s)
        _safe_invalidate_ctx(resolved_owner)
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

#Adds shops to db and start the function for backfill shops 
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

    from core.uzum_openapi import verify_shop_access

    uid = int(current_user.get_id())
    is_admin = _current_user_is_admin()

    added: list[dict] = []
    skipped: list[dict] = []
    seeds: list[tuple[str, int]] = []

    with SessionLocal() as db:
        settings = _get_or_create_subscription_settings(db)
        user = db.get(User, uid)
        token = ((user.uzum_openapi_token or "").strip() if user else "")
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

            if headroom <= 0 and not is_admin:
                skipped.append({"uzum_id": uzum_id, "reason": "limit_reached"})
                continue

            # ── Permission gate (Ulug'bek 2026-07-13) ─────────────────────
            # `/v1/shops` lists the shops the picker offers, but the OpenAPI
            # token only works for the shops the seller TICKED when creating
            # it. Without this probe an unpermitted shop attached
            # "successfully", then every background fetch 403'd forever and
            # the user just saw an empty page (and burned a shop slot).
            # Verify BEFORE we write the row or fire the seed.
            # NOTE: only an explicit `forbidden` blocks the add — an
            # inconclusive result ("error": network/5xx) must not punish a
            # legitimate shop, so we let it through as before.
            if token:
                ok, why = verify_shop_access(token, uzum_id)
                if not ok and why == "forbidden":
                    print(f"[AdminShop] attach REFUSED shop={uzum_id}: token has no permission for it")
                    skipped.append({"uzum_id": uzum_id, "reason": "no_permission"})
                    continue

            if existing is not None:
                # Claim an unowned shop (regular user) or leave admin-assignable
                if existing.owner_id is None:
                    existing.owner_id = uid
                if name and not existing.name:
                    existing.name = name
                db.flush()
                added.append({"uzum_id": uzum_id, "id": existing.id})
                seeds.append((uzum_id, existing.id))
                headroom -= 1
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
            _safe_invalidate_ctx(uid)
            _, current_count, limit = _can_user_add_shop(
                db, user_id=uid, settings=settings,
            )
        else:
            current_count, limit = 0, 0

    for uzum_id, shop_pk in seeds:
        _fire_finance_seed(uzum_id, shop_pk)

    # A skipped shop is a user-facing failure the frontend shows as a popup,
    # but the HTTP response is 200 — the error monitor's after_request hook
    # can't see it. Record each non-benign skip explicitly so the admin gets
    # the same DB row + instant Telegram alert as for real 4xx/5xx errors.
    _skip_reason_text = {
        "no_permission": "Токен OpenAPI не имеет доступа к этому магазину",
        "owned_by_other": "Магазин уже привязан к другому аккаунту",
        "limit_reached": "Достигнут лимит магазинов",
    }
    from flask import request as _rq
    from core.error_monitor import record_error
    for item in skipped:
        reason_text = _skip_reason_text.get(item.get("reason"))
        if not reason_text:
            continue  # "already_added" is informational, not a failure
        record_error(
            user_id=uid,
            username=getattr(current_user, "username", None),
            path=_rq.path,
            method=_rq.method,
            endpoint=_rq.endpoint,
            referer=_rq.headers.get("Referer"),
            status_code=400,
            error_message=f"Добавление магазина {item.get('uzum_id')}: {reason_text}",
        )

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
        prev_owner_id = shop.owner_id
        shop.owner_id = target_owner_id
        db.commit()
    _safe_invalidate_ctx(prev_owner_id, target_owner_id)
    return _json_response({"ok": True})


#shops delete functionality
@admin_bp.delete("/api/shops/<int:shop_id>")
@login_required
def delete_shop(shop_id: int):
    """Delete a shop. Users can only delete their own shops; admin can delete any unowned shop.

    Runs under a deadlock-retry loop: the cascade touches Variant/ProductGroup
    while the background sales-ingest loops hold locks on the same rows
    (sku->shop routing reads). Postgres may abort one side as the deadlock
    victim under contention; we simply retry the whole transaction on a
    fresh session. Also purges the cached finance tables (finance_orders /
    finance_hourly_snapshots / expenses_ledger / shop_sync_state) so a
    deleted shop leaves no orphan rows.
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
                # Capture the owner before the row is deleted so we can bust
                # their subscription-context cache after the commit succeeds.
                deleted_owner_id = shop.owner_id

                # New-pipeline tables key on the Uzum shop id (int), not the
                # local PK. Resolve it before the Shop row is deleted.
                uzum_id_int = None
                try:
                    uzum_id_int = int(str(shop.uzum_id).strip())
                except (TypeError, ValueError):
                    uzum_id_int = None

                # Cascade delete:  -> Variant -> ProductGroup -> Shop
                group_ids = db.execute(
                    select(ProductGroup.id).where(ProductGroup.shop_id == shop_id)
                ).scalars().all()
                if group_ids:
                    variant_ids = db.execute(
                        select(Variant.id).where(Variant.group_id.in_(group_ids))
                    ).scalars().all()
                    if variant_ids:
                        db.execute(delete(Variant).where(Variant.id.in_(variant_ids)))
                    db.execute(delete(ProductGroup).where(ProductGroup.id.in_(group_ids)))

                # pos_action_log has FK -> shops.id; clear it so the Shop
                # delete doesn't fail with an IntegrityError.
                db.execute(delete(PosActionLog).where(PosActionLog.shop_id == shop_id))

                # Purge cached finance data so the deleted shop leaves no
                # orphan rows. shop_id columns mix conventions: ExpensesLedger
                # / ShopSyncState use int; FinanceOrder / FinanceHourlySnapshot
                # use string (matches Shop.uzum_id varchar).
                if uzum_id_int is not None:
                    db.execute(delete(ExpensesLedger).where(ExpensesLedger.shop_id == uzum_id_int))
                    db.execute(delete(ShopSyncState).where(ShopSyncState.shop_id == uzum_id_int))
                uzum_id_str = (shop.uzum_id or "").strip()
                if uzum_id_str:
                    db.execute(delete(FinanceOrder).where(FinanceOrder.shop_id == uzum_id_str))
                    db.execute(delete(FinanceHourlySnapshot).where(FinanceHourlySnapshot.shop_id == uzum_id_str))

                db.delete(shop)
                db.commit()
            _safe_invalidate_ctx(deleted_owner_id, uid)
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
        return redirect(url_for("products_bp.economics_page"))
    with SessionLocal() as db:
        users = db.execute(select(User)).scalars().all()
        shops = db.execute(select(Shop)).scalars().all()
    return render_template("admin_users.html", users=users, shops=shops)

#admin creates user (no email or other metadata, just username/password). Returns the new user's ID so the admin can assign shops to them in a follow-up step. No notification is sent to the user; the admin must communicate credentials out-of-band.
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
#admin deletes user (soft delete by revoking access; shops remain but are unmanageable until reassigned)
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
#admin revokes user access (soft delete by revoking access; shops remain but are unmanageable until unrevoke)
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

# ----------------------------
# Admin: Error monitoring
# ----------------------------
@admin_bp.get("/admin/errors")
@login_required
def admin_errors_page():
    """Error-monitoring dashboard: who hit errors, where, and their contacts.

    Fed by ``core.error_monitor`` (error_events table). The «подключённые
    чаты» card reflects ``admin_alert_chats`` — chats that entered the admin
    password in the alert bot and receive each error instantly.
    """
    if not _current_user_is_admin():
        return redirect(url_for("products_bp.economics_page"))

    now = datetime.utcnow()
    day_ago = now - timedelta(hours=24)
    week_ago = now - timedelta(days=7)

    with SessionLocal() as db:
        users = db.execute(select(User).order_by(User.id)).scalars().all()

        def _agg(since):
            rows = db.execute(
                select(
                    ErrorEvent.user_id,
                    func.count(ErrorEvent.id),
                    func.coalesce(func.sum(ErrorEvent.count), 0),
                )
                .where(ErrorEvent.last_seen_at > since)
                .group_by(ErrorEvent.user_id)
            ).all()
            return {uid: (int(n), int(hits)) for uid, n, hits in rows}

        agg_24h = _agg(day_ago)
        agg_7d = _agg(week_ago)

        unresolved_by_user = {
            uid: int(n)
            for uid, n in db.execute(
                select(ErrorEvent.user_id, func.count(ErrorEvent.id))
                .where(ErrorEvent.resolved == False)  # noqa: E712
                .group_by(ErrorEvent.user_id)
            ).all()
        }

        # Latest error per user (for the users table) — newest 1000 rows are
        # plenty; older history stays reachable through the errors feed.
        latest_by_user: dict[int | None, ErrorEvent] = {}
        recent_errors = db.execute(
            select(ErrorEvent).order_by(desc(ErrorEvent.last_seen_at)).limit(1000)
        ).scalars().all()
        for ev in recent_errors:
            if ev.user_id not in latest_by_user:
                latest_by_user[ev.user_id] = ev

        error_rows = recent_errors[:200]

        chats = db.execute(select(AdminAlertChat)).scalars().all()

        user_map = {u.id: u for u in users}
        user_rows = []
        for u in users:
            n24 = agg_24h.get(u.id, (0, 0))
            n7 = agg_7d.get(u.id, (0, 0))
            last = latest_by_user.get(u.id)
            user_rows.append({
                "id": u.id,
                "username": u.username,
                "is_admin": bool(u.is_admin),
                "phone": (u.phone or "").strip(),
                "telegram_id": (u.telegram_id or "").strip(),
                "language": (u.language or "").strip(),
                "errors_24h": n24[0],
                "hits_24h": n24[1],
                "errors_7d": n7[0],
                "unresolved": unresolved_by_user.get(u.id, 0),
                "last_error": last,
            })
        # Users with the freshest errors first, error-free users after.
        user_rows.sort(
            key=lambda r: (
                r["last_error"].last_seen_at if r["last_error"] else datetime.min,
                r["id"],
            ),
            reverse=True,
        )

        overview = {
            "errors_24h": sum(r["errors_24h"] for r in user_rows),
            "hits_24h": sum(r["hits_24h"] for r in user_rows),
            "affected_24h": sum(1 for r in user_rows if r["errors_24h"]),
            "unresolved": db.execute(
                select(func.count(ErrorEvent.id)).where(ErrorEvent.resolved == False)  # noqa: E712
            ).scalar_one(),
            "chats_active": sum(1 for c in chats if c.is_active),
        }

        return render_template(
            "admin_errors.html",
            tz=timedelta(hours=5),  # UTC -> Tashkent for display
            overview=overview,
            user_rows=user_rows,
            error_rows=error_rows,
            user_map=user_map,
            alert_chats=chats,
            bot_configured=bool((__import__("os").environ.get("ADMIN_TELEGRAM_TOKEN") or "").strip()),
        )


@admin_bp.post("/api/admin/errors/<int:event_id>/resolve")
@login_required
def admin_resolve_error(event_id: int):
    if not _current_user_is_admin():
        return _json_response({"error": "Admin only"}, 403)
    with SessionLocal() as db:
        ev = db.get(ErrorEvent, event_id)
        if ev is None:
            return _json_response({"error": "Not found"}, 404)
        ev.resolved = not bool(ev.resolved)
        db.commit()
        return _json_response({"ok": True, "resolved": bool(ev.resolved)})


@admin_bp.post("/api/admin/errors/resolve-all")
@login_required
def admin_resolve_all_errors():
    if not _current_user_is_admin():
        return _json_response({"error": "Admin only"}, 403)
    from sqlalchemy import update
    with SessionLocal() as db:
        res = db.execute(
            update(ErrorEvent).where(ErrorEvent.resolved == False).values(resolved=True)  # noqa: E712
        )
        db.commit()
        return _json_response({"ok": True, "updated": int(res.rowcount or 0)})


#admin page for subscription management (codes, settings, user overrides)
@admin_bp.route("/admin/subscriptions", methods=["GET", "POST"])
@login_required
def admin_subscriptions_page():
    if not _current_user_is_admin():
        return redirect(url_for("products_bp.economics_page"))

    duration_options = _subscription_code_duration_rows()

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()
        with SessionLocal() as db:
            settings = _get_or_create_subscription_settings(db)
            #change subscription settings (trial days, monthly price, max shops per user)
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
            #code_creation, create the  code with the selected duration and max activations. The code is generated randomly and stored in the database. The admin can then distribute this code to users who can redeem it for a subscription.
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
            #code_deletion, delete the code and all its activations. This revokes any subscriptions granted by this code, but does not affect other active subscriptions the users may have (e.g. from trial or their own payment). The admin can use this to invalidate a code that was leaked or distributed too widely.
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
                # Cancel removes the PAID subscription only. If the user still
                # has an active free trial, keep their access (don't block) —
                # only revoke when no active access remains. Either way force a
                # one-time session recheck so the stale "paid" signed session is
                # recomputed from the DB on their next request.
                status = _subscription_status_for_user(user, settings=settings)
                if status["active"]:
                    unrevoke_user(user.id)
                    mark_user_for_recheck(user.id)
                else:
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
