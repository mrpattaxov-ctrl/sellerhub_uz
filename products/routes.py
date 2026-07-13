"""Product/group/sync-related routes extracted from app.py as a Flask Blueprint."""
from __future__ import annotations

import io
import zipfile
from datetime import date, datetime, timedelta, time as dt_time

from flask import Blueprint, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required
from sqlalchemy import select, func, delete, update, false as sql_false

from extensions import SessionLocal
from models import ProductGroup, Variant, Shop, User, ExpensesLedger
from core.parsers import _safe_qty
from core.uzum_skulist import normalize_uzum_image_url
from core.sales_reads import (
    day_bounds_tashkent,
    read_sales_aggregated,
)
from core.time_helpers import _today_app_tz
from core.http_client import http_post_multipart
from core.auth_helpers import (
    _json_response,
    _jwt_expires_in_seconds,
    _get_fresh_api_key,
    _get_admin_token,
    _current_user_is_admin,
    admin_required,
    _user_shop_ids,
)

try:
    import openpyxl
    from openpyxl.styles import Font, Alignment, Border, Side
except ImportError:
    openpyxl = None

products_bp = Blueprint("products_bp", __name__)

# ── Late-binding to avoid circular imports ──────────────────────────────
_app = None


def init_products_routes(app_module):
    global _app
    _app = app_module


# ── Routes ──────────────────────────────────────────────────────────────

@products_bp.get("/")
@login_required
def home():
    return redirect(url_for("products_bp.economics_page"))

@products_bp.get("/print/labels")
@login_required
def print_labels():
    ids_str = request.args.get("ids") or ""
    if not ids_str:
        return "No items selected", 400

    try:
        ids = [int(x) for x in ids_str.split(",") if x.strip().isdigit()]
    except ValueError:
        return "Invalid IDs", 400

    LABEL_SIZES = {
        "30x20":  {"w": 30, "h": 20, "tcol": 7,  "qr": 14, "sku_fs": 5.4, "num_fs": 6, "num_last4_fs": 8},
        "43x25":  {"w": 43, "h": 25, "tcol": 8,  "qr": 20, "sku_fs": 6,   "num_fs": 7, "num_last4_fs": 9},
        "40x30":  {"w": 40, "h": 30, "tcol": 9,  "qr": 20, "sku_fs": 6.5, "num_fs": 7, "num_last4_fs": 9},
        "60x60":  {"w": 60, "h": 60, "tcol": 12, "qr": 34, "sku_fs": 8,   "num_fs": 8, "num_last4_fs": 11},
        "70x37":  {"w": 70, "h": 37, "tcol": 12, "qr": 40, "sku_fs": 8,   "num_fs": 8, "num_last4_fs": 11},
    }
    size_key = request.args.get("size", "30x20")
    if size_key not in LABEL_SIZES:
        size_key = "30x20"
    lbl = LABEL_SIZES[size_key]

    with SessionLocal() as db:
        # Fetch unique variants first
        unique_ids = list(set(ids))
        if not unique_ids:
             return "No items", 400
        objs = db.execute(select(Variant).where(Variant.id.in_(unique_ids))).scalars().all()
        obj_map = {o.id: o for o in objs}

        # Rebuild list with duplicates based on input 'ids' order to support quantity
        variants = []
        for i in ids:
            if i in obj_map:
                variants.append(obj_map[i])

    return render_template("print_labels.html", variants=variants, lbl=lbl)

@products_bp.get("/print/queue")
@login_required
def print_queue_page():
    return render_template("print_queue.html")

@products_bp.get("/groups")
@login_required
def groups_page():
    q = (request.args.get("q") or "").strip()
    shop_filter = (request.args.get("shop_id") or "").strip()
    status_filter = (request.args.get("status") or "active").strip().lower()
    display_status = "archived" if status_filter in ("archived", "archive") else "active"
    page = 1  # pagination removed — all products render on a single page

    # Restrict to the user's assigned shops
    uid = int(current_user.get_id())
    allowed_shop_ids = _user_shop_ids(uid)

    # Default shop selection: on first visit (no shop_id param) reuse the shop
    # picked on other pages (sh_shop cookie, holds Shop.uzum_id or the sentinel
    # 'all'), falling back to the first shop. An explicit "Все магазины" pick
    # arrives as shop_id=all and is likewise remembered via the cookie.
    if shop_filter == "" and allowed_shop_ids:
        saved = (request.cookies.get("sh_shop") or "").strip()
        if saved == "all":
            shop_filter = "all"
        elif saved:
            with SessionLocal() as db:
                sid = db.execute(
                    select(Shop.id).where(
                        Shop.uzum_id == saved, Shop.id.in_(allowed_shop_ids)
                    )
                ).scalar_one_or_none()
            if sid is not None:
                shop_filter = str(sid)
        if shop_filter == "":
            shop_filter = str(sorted(allowed_shop_ids)[0])

    # The groups page has no "Все магазины" (all shops) option in its picker, so
    # coerce the 'all' sentinel — which may arrive from the shop cookie shared
    # with other pages, or from an old ?shop_id=all link — to the first shop.
    if shop_filter == "all" and allowed_shop_ids:
        shop_filter = str(sorted(allowed_shop_ids)[0])

    with SessionLocal() as db:
        stmt = select(ProductGroup)

        # Always filter to allowed shops; also include groups with no shop assigned (NULL)
        if allowed_shop_ids:
            stmt = stmt.where(
                ProductGroup.shop_id.in_(allowed_shop_ids) | (ProductGroup.shop_id == None)
            )
        else:
            stmt = stmt.where(ProductGroup.shop_id == None)

        if shop_filter and shop_filter.isdigit() and int(shop_filter) in allowed_shop_ids:
            stmt = stmt.where(ProductGroup.shop_id == int(shop_filter))

        if display_status == "archived":
            stmt = stmt.where(ProductGroup.is_archived == True)
        else:
            stmt = stmt.where((ProductGroup.is_archived == False) | (ProductGroup.is_archived == None))

        if q:
            like = f"%{q}%"
            # Use a subquery to avoid SELECT DISTINCT + ORDER BY expression conflict in PostgreSQL
            subq = stmt.outerjoin(ProductGroup.variants).where(
                ProductGroup.name.ilike(like) |
                Variant.sku.ilike(like) |
                Variant.barcode.ilike(like)
            ).with_only_columns(ProductGroup.id).distinct().subquery()
            stmt = select(ProductGroup).where(ProductGroup.id.in_(select(subq)))

        # Newest products first (mirrors Uzum's "Мои товары" ordering). The Uzum
        # sku-list position grows with recency, so order it DESC. Rows with 0
        # (not in the sku-list) still sort last; id.desc() breaks any ties.
        stmt = stmt.order_by(
            (ProductGroup.uzum_sort_order == 0).asc(),
            ProductGroup.uzum_sort_order.desc(),
            ProductGroup.id.desc(),
        )

        total_count = db.execute(select(func.count()).select_from(stmt.subquery())).scalar() or 0
        total_pages = 1  # single page — no LIMIT/OFFSET, all rows returned

        groups = db.execute(stmt).scalars().all()

        # aggregate counts for the full list
        group_ids = [g.id for g in groups]
        vstmt = (
            select(
                Variant.group_id,
                func.count(Variant.id),
                func.coalesce(func.sum(Variant.uzum_quantity), 0),
                func.coalesce(func.sum(Variant.quantity_fbs), 0),
                func.coalesce(func.sum(Variant.warehouse_quantity), 0),
                func.min(Variant.sku),
                # Extra metrics for the detailed ("stats") card view
                func.coalesce(func.sum(Variant.views_30d), 0),
                func.coalesce(func.sum(Variant.quantity_sold), 0),
                func.coalesce(func.sum(Variant.quantity_returned), 0),
                func.coalesce(func.sum(Variant.quantity_defected), 0),
                func.min(func.nullif(Variant.price_sum, 0)),      # "от" price (cheapest variant)
                func.avg(func.nullif(Variant.sell_price_uzum, 0)),
                func.avg(func.nullif(Variant.purchase_price, 0)),
            )
            .where(Variant.group_id.in_(group_ids) if group_ids else False)
            .group_by(Variant.group_id)
        )
        agg = {}
        for (gid, c, u, fbs, w, s, views, sold, ret, defect,
             price_from, avg_sell, avg_purchase) in db.execute(vstmt).all():
            sold_i = int(sold or 0)
            views_i = int(views or 0)
            # Конверсия = orders ÷ views (real metric from synced data)
            conv = (sold_i / views_i * 100.0) if views_i else 0.0
            # ROI proxy = markup over purchase cost. Prefer the realized sell
            # price; fall back to the listing price when finance data isn't
            # synced. None when there's no purchase cost to divide by.
            roi = None
            sell_basis = float(avg_sell) if avg_sell else (float(price_from) if price_from else None)
            if avg_purchase and float(avg_purchase) > 0 and sell_basis:
                roi = (sell_basis - float(avg_purchase)) / float(avg_purchase) * 100.0
            agg[gid] = {
                "variants": c,
                "fbo": int(u),
                "fbs": int(fbs),
                "wh": int(w),
                "uzum_qty": int(u),  # kept for backward compat with other templates/JS
                "wh_qty": int(w),
                "sku": "-".join(str(s).split("-")[:2]) if s else "",
                "views": views_i,
                "sold": sold_i,
                "returned": int(ret or 0),
                "defected": int(defect or 0),
                "conversion": conv,
                "roi": roi,
                "price_from": int(price_from or 0),
            }

        # Fetch shops for the picker (with name lookup used by the cards).
        # Ordered by id so the first listed shop matches the auto-selected
        # default (sorted(allowed_shop_ids)[0]).
        shops_stmt = (
            select(Shop).where(Shop.id.in_(allowed_shop_ids)) if allowed_shop_ids else select(Shop)
        ).order_by(Shop.id)
        shops = db.execute(shops_stmt).scalars().all()
        shops_by_id = {s.id: s for s in shops}

        # Tab totals (Активные / Архив) over the *visible* set (allowed shops + store filter, ignoring search)
        tab_base = select(func.count(ProductGroup.id))
        if allowed_shop_ids:
            tab_base = tab_base.where(
                ProductGroup.shop_id.in_(allowed_shop_ids) | (ProductGroup.shop_id == None)
            )
        else:
            tab_base = tab_base.where(ProductGroup.shop_id == None)
        if shop_filter and shop_filter.isdigit() and int(shop_filter) in allowed_shop_ids:
            tab_base = tab_base.where(ProductGroup.shop_id == int(shop_filter))
        count_active = db.execute(
            tab_base.where((ProductGroup.is_archived == False) | (ProductGroup.is_archived == None))
        ).scalar() or 0
        count_archived = db.execute(
            tab_base.where(ProductGroup.is_archived == True)
        ).scalar() or 0

    return render_template(
        "groups.html",
        groups=groups, agg=agg, q=q,
        current_status=display_status, shops=shops, shops_by_id=shops_by_id,
        current_shop_id=shop_filter,
        page=page, total_pages=total_pages, total_count=total_count,
        count_active=count_active, count_archived=count_archived,
    )

@products_bp.get("/expenses")
@login_required
def expenses_page():
    """Warehouse (paid-storage) expenses, grouped by product.

    One row per ProductGroup showing the SHORT sku + the group's TOTAL
    paid-storage expense (Variant.paid_storage_amount — "Платное хранение"),
    summed across its variants, plus total Uzum warehouse qty and average
    sell/cost price. Groups are sorted by total expense (highest first); each
    row expands to reveal its variant SKUs, also sorted by expense (highest
    first). Mirrors the groups page UX (search bar + per-shop picker +
    pagination).
    """
    q = (request.args.get("q") or "").strip()
    shop_filter = (request.args.get("shop_id") or "").strip()
    page = max(1, int(request.args.get("page") or 1))
    per_page = 50

    uid = int(current_user.get_id())
    allowed_shop_ids = _user_shop_ids(uid)

    with SessionLocal() as db:
        # Base WHERE — shop scope only. (Search is applied as a group-id
        # restriction below so a SKU match still surfaces the WHOLE group, not
        # just the matching variant.)
        #
        # NOTE: archived products are deliberately NOT excluded here. An
        # archived Uzum listing can still have stock physically sitting in the
        # warehouse racking up paid-storage ("Платное хранение") charges — a
        # real cash cost. We keep every non-archived group plus any archived
        # group that STILL has storage or warehouse qty (enforced by the HAVING
        # on the aggregate below). Archived-but-empty groups contribute 0 to the
        # storage total, so `total_storage` needs no archived filter either.
        conds = []
        if allowed_shop_ids:
            conds.append(ProductGroup.shop_id.in_(allowed_shop_ids))
        else:
            conds.append(sql_false())
        if shop_filter and shop_filter.isdigit() and int(shop_filter) in allowed_shop_ids:
            conds.append(ProductGroup.shop_id == int(shop_filter))

        if q:
            like = f"%{q}%"
            match_ids = (
                select(ProductGroup.id)
                .outerjoin(Variant, Variant.group_id == ProductGroup.id)
                .where(*conds)
                .where(
                    ProductGroup.name.ilike(like)
                    | Variant.sku.ilike(like)
                    | Variant.barcode.ilike(like)
                )
                .distinct()
            )
            conds.append(ProductGroup.id.in_(match_ids))

        storage_col = func.coalesce(func.sum(func.coalesce(Variant.paid_storage_amount, 0)), 0)
        qty_col = func.coalesce(func.sum(func.coalesce(Variant.uzum_quantity, 0)), 0)

        # Keep non-archived groups always; keep archived groups only while they
        # still cost money to store (storage > 0) or still hold warehouse stock
        # (qty > 0). Archived + empty groups are dropped so the list isn't
        # cluttered with dead listings that no longer incur storage.
        archived_ok = (
            (ProductGroup.is_archived == False)
            | (ProductGroup.is_archived == None)
            | (storage_col > 0)
            | (qty_col > 0)
        )

        # Per-group aggregate (inner join → only groups that have variants).
        agg = (
            select(
                ProductGroup.id.label("gid"),
                ProductGroup.name.label("name"),
                ProductGroup.image_url.label("image_url"),
                ProductGroup.shop_id.label("shop_id"),
                ProductGroup.uzum_product_id.label("uzum_product_id"),
                ProductGroup.is_archived.label("is_archived"),
                storage_col.label("storage"),
                qty_col.label("qty"),
                func.count(Variant.id).label("vcount"),
                func.min(Variant.sku).label("min_sku"),
            )
            .join(Variant, Variant.group_id == ProductGroup.id)
            .where(*conds)
            .group_by(
                ProductGroup.id, ProductGroup.name,
                ProductGroup.image_url, ProductGroup.shop_id,
                ProductGroup.uzum_product_id, ProductGroup.is_archived,
            )
            .having(archived_ok)
        )

        total_count = db.execute(
            select(func.count()).select_from(agg.subquery())
        ).scalar() or 0
        # Single-page view: all matching groups are rendered at once (no
        # pagination). `page`/`total_pages` are kept only so the template's
        # existing bindings stay valid.
        total_pages = 1
        page = 1

        # Grand total storage across all matching groups (every page).
        total_storage = db.execute(
            select(func.coalesce(func.sum(func.coalesce(Variant.paid_storage_amount, 0)), 0))
            .select_from(Variant).join(
                ProductGroup, Variant.group_id == ProductGroup.id
            ).where(*conds)
        ).scalar() or 0

        agg = agg.order_by(storage_col.desc(), ProductGroup.id.asc())
        rows = db.execute(agg).all()
        group_ids = [r.gid for r in rows]

        # Fetch all variants for the page's groups (for the expand dropdown),
        # ordered by expense (highest first).
        variants_by_group: dict[int, list] = {}
        if group_ids:
            vrows = db.execute(
                select(Variant)
                .where(Variant.group_id.in_(group_ids))
                .order_by(
                    func.coalesce(Variant.paid_storage_amount, 0).desc(),
                    func.lower(Variant.sku).asc(),
                )
            ).scalars().all()
            for v in vrows:
                variants_by_group.setdefault(v.group_id, []).append(v)

        items = []
        for r in rows:
            gvars = variants_by_group.get(r.gid, [])
            sells = [int(v.price_sum or 0) for v in gvars if (v.price_sum or 0) > 0]
            costs = [int(v.purchase_price or 0) for v in gvars if (v.purchase_price or 0) > 0]
            avg_sell = (sum(sells) // len(sells)) if sells else 0
            avg_cost = (sum(costs) // len(costs)) if costs else 0
            # Short SKU like the groups page (first two dash-segments).
            short_sku = "-".join(str(r.min_sku).split("-")[:2]) if r.min_sku else ""
            items.append({
                "id": r.gid,
                "name": r.name or "",
                "image_url": r.image_url or "",
                "shop_id": r.shop_id,
                "uzum_product_id": r.uzum_product_id or "",
                "short_sku": short_sku,
                "is_archived": bool(r.is_archived),
                "vcount": int(r.vcount or 0),
                "storage": int(r.storage or 0),
                "qty": int(r.qty or 0),
                "avg_sell": avg_sell,
                "avg_cost": avg_cost,
                "variants": [{
                    "id": v.id,
                    "sku": v.sku or "",
                    "color": v.color or "",
                    "image_url": (v.image_url or r.image_url) or "",
                    "storage": int(v.paid_storage_amount or 0),
                    "qty": int(v.uzum_quantity or 0),
                    "sell_price": int(v.price_sum or 0),
                    "cost_price": int(v.purchase_price or 0),
                } for v in gvars],
            })

        # Shops for the picker.
        shops = db.execute(
            select(Shop).where(Shop.id.in_(allowed_shop_ids)) if allowed_shop_ids else select(Shop)
        ).scalars().all()
        shops_by_id = {s.id: s for s in shops}

        # Enrolled-in-sale state, rendered server-side so already-in-sale
        # buttons are correct on first paint (no 5s flip). Read-only PEEK at the
        # shared (Redis) sales cache — never triggers a cold Uzum build here; if
        # the cache is cold the client-side refresh fills it in.
        from core.uzum_marketing import peek_shop_sales_dataset
        enrolled_pids: dict[str, list] = {}
        cost_by_pid: dict[str, int] = {}   # uzum_product_id -> Uzum cost price
        page_uzids = {
            str(shops_by_id[it["shop_id"]].uzum_id)
            for it in items
            if shops_by_id.get(it["shop_id"]) and shops_by_id[it["shop_id"]].uzum_id
        }
        for uzid in page_uzids:
            ds = peek_shop_sales_dataset(uzid)
            if not ds:
                continue
            for s in ds["sales"]:
                title = _split_sale_title(s.get("title"))
                for pid in s["involved_ids"]:
                    enrolled_pids.setdefault(str(pid), []).append(title)
            for pid, c in (ds.get("purchase_prices") or {}).items():
                if c and str(pid) not in cost_by_pid:
                    cost_by_pid[str(pid)] = int(c)

        # Cost price: Uzum's product-level «себестоимость» is often missing from
        # the DB (the product sync doesn't carry it). Backfill blank costs from
        # the sales dataset's purchasePrice and surface it on this page even
        # before the DB write lands.
        if cost_by_pid:
            _persist_uzum_costs(allowed_shop_ids, cost_by_pid)
            for it in items:
                c = cost_by_pid.get(str(it.get("uzum_product_id") or ""))
                if not c:
                    continue
                if not it["avg_cost"]:
                    it["avg_cost"] = int(c)
                for v in it["variants"]:
                    if not v["cost_price"]:
                        v["cost_price"] = int(c)

    return render_template(
        "expenses.html",
        items=items, q=q, shops=shops, shops_by_id=shops_by_id,
        current_shop_id=shop_filter,
        page=page, total_pages=total_pages, total_count=total_count, per_page=per_page,
        total_storage=int(total_storage),
        enrolled_pids=enrolled_pids,
    )


# ──────────────────────────────────────────────────────────────────────
# Marketing / Sales (акции) — enroll products into Uzum sale campaigns.
# Read side is live; the "add" write is gated until its request is captured
# (see core/uzum_marketing.py).
# ──────────────────────────────────────────────────────────────────────

def _owned_shop_by_uzum_id(db, uid: int, raw_shop: str):
    """Return the Shop (owned by uid) matching raw_shop uzum_id, else None."""
    allowed = _user_shop_ids(uid)
    if not allowed:
        return None
    shop = db.execute(
        select(Shop).where(Shop.uzum_id == str(raw_shop), Shop.id.in_(allowed))
    ).scalar_one_or_none()
    return shop


def _split_sale_title(t: str) -> dict:
    """Sale titles arrive as 'RU text/UZ text' — split once on the last '/'."""
    t = (t or "").strip()
    if "/" in t:
        ru, uz = t.rsplit("/", 1)
        return {"ru": ru.strip(), "uz": uz.strip()}
    return {"ru": t, "uz": t}


def _persist_uzum_costs(shop_ids, cost_by_pid: dict) -> None:
    """Backfill blank Variant.purchase_price from Uzum's product-level cost.

    ``cost_by_pid`` is ``{uzum_product_id(str): cost(int)}`` harvested from the
    marketing suitable-products payload. We only fill variants whose cost is
    currently 0/NULL — a user-entered cost is never overwritten.

    Runs in its OWN session (not the caller's) so its commit never expires the
    caller's loaded ORM objects — committing the caller's session would expire
    e.g. the expenses page's Shop list and blow up at render time
    (DetachedInstanceError). Best-effort: any error is swallowed."""
    cost_by_pid = {str(k): int(v) for k, v in (cost_by_pid or {}).items() if v and int(v) > 0}
    if not cost_by_pid or not shop_ids:
        return
    try:
        with SessionLocal() as db:
            groups = db.execute(
                select(ProductGroup.id, ProductGroup.uzum_product_id)
                .where(
                    ProductGroup.shop_id.in_(list(shop_ids)),
                    ProductGroup.uzum_product_id.in_(list(cost_by_pid.keys())),
                )
            ).all()
            changed = False
            for gid, upid in groups:
                cost = cost_by_pid.get(str(upid))
                if not cost:
                    continue
                res = db.execute(
                    update(Variant)
                    .where(
                        Variant.group_id == gid,
                        (Variant.purchase_price == None) | (Variant.purchase_price == 0),  # noqa: E711
                    )
                    .values(purchase_price=cost)
                )
                if res.rowcount:
                    changed = True
            if changed:
                db.commit()
    except Exception:
        pass


def _sku_payout_rates(db, shop_uzum_id: str, days: int = 120) -> dict[str, dict]:
    """Per-SKU commission rate + per-unit logistics from real finance history.

    Uzum doesn't expose commission/logistics in the marketing SKU payload, so we
    derive each SKU's deductions from our own finance/orders (which DO store them
    per sale). «К выводу» = new_price × (1 − commission_rate) − logistics_per_unit.
    Keyed by sku_title (= Variant.sku code), which the marketing payload also
    returns. SKUs with no recent sales simply won't have a rate (UI shows '—')."""
    today = _today_app_tz()
    start_ts, _ = day_bounds_tashkent(today - timedelta(days=days))
    _, end_ts = day_bounds_tashkent(today)
    rates: dict[str, dict] = {}
    try:
        rows = read_sales_aggregated(
            str(shop_uzum_id), start_ts, end_ts, group_by="sku", session=db
        )
    except Exception:
        return rates
    for r in rows:
        title = r.get("sku_title")
        rev = float(r.get("revenue_sum") or 0)
        qty = int(r.get("qty_sum") or 0)
        comm = float(r.get("commission_sum") or 0)
        logi = float(r.get("logistics_sum") or 0)
        if not title or rev <= 0 or qty <= 0:
            continue
        rates[str(title)] = {
            "commission_rate": max(0.0, min(0.9, comm / rev)),
            "logistics_per_unit": max(0.0, round(logi / qty)),
        }
    return rates


@products_bp.get("/sales")
@login_required
def sales_page():
    uid = int(current_user.get_id())
    allowed_shop_ids = _user_shop_ids(uid)
    with SessionLocal() as db:
        shops = db.execute(
            select(Shop).where(Shop.id.in_(allowed_shop_ids)) if allowed_shop_ids else select(Shop).where(sql_false())
        ).scalars().all()
        shops_list = [{"uzum_id": s.uzum_id, "name": s.name or s.uzum_id} for s in shops]
    return render_template("sales.html", shops=shops_list)


@products_bp.get("/api/sales")
@login_required
def api_sales_list():
    """List sale campaigns for a shop (uzum_id via ?shop_id=)."""
    raw_shop = (request.args.get("shop_id") or "").strip()
    uid = int(current_user.get_id())
    with SessionLocal() as db:
        shop = _owned_shop_by_uzum_id(db, uid, raw_shop)
    if not shop:
        return _json_response({"error": "Магазин не найден или недоступен."}, 403)
    try:
        from core.uzum_marketing import list_sales, UzumMarketingError
        sales = list_sales(raw_shop)
    except UzumMarketingError as e:
        return _json_response({"error": str(e)}, 502)
    except Exception as e:
        return _json_response({"error": f"Ошибка Uzum: {e!s}"}, 502)

    def split_title(t: str) -> dict:
        # Titles arrive as "RU text/UZ text"; split once on the last "/".
        t = (t or "").strip()
        if "/" in t:
            ru, uz = t.rsplit("/", 1)
            return {"ru": ru.strip(), "uz": uz.strip()}
        return {"ru": t, "uz": t}

    out = []
    for s in sales:
        out.append({
            "id": s.get("id"),
            "title": split_title(s.get("title")),
            "start_date": s.get("startDate"),
            "finish_date": s.get("finishDate"),
            "status": s.get("status"),
            "type": s.get("type"),
            "image_url": (s.get("imageUrl") or {}),
            "suitable_count": s.get("suitableProductsCount"),
            "involved_count": s.get("involvedProductsCount"),
        })
    return _json_response({"sales": out})


@products_bp.get("/api/sales/<int:sale_id>/suitable")
@login_required
def api_sale_suitable(sale_id: int):
    """Products eligible for a sale, mapped to our local product groups +
    variants (so the UI can show every variant that would be enrolled)."""
    raw_shop = (request.args.get("shop_id") or "").strip()
    search = (request.args.get("q") or "").strip()
    uid = int(current_user.get_id())
    with SessionLocal() as db:
        shop = _owned_shop_by_uzum_id(db, uid, raw_shop)
        if not shop:
            return _json_response({"error": "Магазин не найден или недоступен."}, 403)

        try:
            from core.uzum_marketing import (
                get_sale, list_suitable_products, list_sale_products, UzumMarketingError,
            )
            detail = get_sale(raw_shop, sale_id)
            suitable = list_suitable_products(raw_shop, sale_id, search=search)
            involved = list_sale_products(raw_shop, sale_id)
        except UzumMarketingError as e:
            return _json_response({"error": str(e)}, 502)
        except Exception as e:
            return _json_response({"error": f"Ошибка Uzum: {e!s}"}, 502)

        involved_ids = {str(p.get("productId")) for p in involved if p.get("productId") is not None}

        # Product-level cost («purchasePrice») from suitable-products — Uzum's
        # only cost source. Backfill blank Variant.purchase_price from it.
        uzum_cost_by_pid = {
            str(p.get("productId")): int(p.get("purchasePrice"))
            for p in suitable
            if p.get("productId") is not None
            and isinstance(p.get("purchasePrice"), (int, float))
            and p.get("purchasePrice") > 0
        }
        _persist_uzum_costs([shop.id], uzum_cost_by_pid)

        # Already-enrolled products drop off suitable-products, so their cost
        # isn't in the payload — read it back from the DB (filled above / on a
        # prior pass) for the right-panel cost column.
        cost_by_involved: dict[str, int] = {}
        if involved_ids:
            crows = db.execute(
                select(ProductGroup.uzum_product_id, func.max(Variant.purchase_price))
                .join(Variant, Variant.group_id == ProductGroup.id)
                .where(
                    ProductGroup.shop_id == shop.id,
                    ProductGroup.uzum_product_id.in_(list(involved_ids)),
                )
                .group_by(ProductGroup.uzum_product_id)
            ).all()
            for upid, c in crows:
                if c and int(c) > 0:
                    cost_by_involved[str(upid)] = int(c)

        # Map Uzum productId → our ProductGroup (+ variants) for this shop.
        pid_strs = [str(p.get("productId")) for p in suitable if p.get("productId") is not None]
        variants_by_pid: dict[str, list] = {}
        if pid_strs:
            grows = db.execute(
                select(ProductGroup, Variant)
                .join(Variant, Variant.group_id == ProductGroup.id)
                .where(
                    ProductGroup.shop_id == shop.id,
                    ProductGroup.uzum_product_id.in_(pid_strs),
                )
                .order_by(func.lower(Variant.sku))
            ).all()
            for g, v in grows:
                variants_by_pid.setdefault(str(g.uzum_product_id), []).append({
                    "sku": v.sku or "",
                    "color": v.color or "",
                    "barcode": v.barcode or "",
                    "image_url": (v.image_url or g.image_url) or "",
                    "qty": int(v.uzum_quantity or 0),
                    "sell_price": int(v.price_sum or 0),
                    "cost_price": int(v.purchase_price or 0),
                })

        # DB name/image for the ALREADY-ADDED products (right panel) — the Uzum
        # enrolled payload doesn't reliably carry title/image, so fall back to
        # our own ProductGroup.
        meta_by_pid: dict[str, dict] = {}
        if involved_ids:
            mrows = db.execute(
                select(ProductGroup.uzum_product_id, ProductGroup.name, ProductGroup.image_url)
                .where(
                    ProductGroup.shop_id == shop.id,
                    ProductGroup.uzum_product_id.in_(list(involved_ids)),
                )
            ).all()
            for upid, name, img in mrows:
                meta_by_pid[str(upid)] = {"name": name or "", "image_url": img or ""}

        cat_rules = []
        for c in (detail.get("categoryRule") or []):
            cat_rules.append({
                "category_id": c.get("categoryId"),
                "title": c.get("title"),
                "min_discount": c.get("minDiscountPercentage"),
            })
        # A single representative min-discount (max across category rules so any
        # product clears its category floor). Falls back to 1%.
        min_discounts = [c["min_discount"] for c in cat_rules if isinstance(c.get("min_discount"), (int, float))]
        default_min = int(max(min_discounts)) if min_discounts else 1

        products = []
        for p in suitable:
            pid = str(p.get("productId"))
            title = p.get("title") or {}
            # Skip products already enrolled — they belong in the right panel.
            if pid in involved_ids:
                continue
            products.append({
                "product_id": p.get("productId"),
                "title": {"ru": title.get("ru") or "", "uz": title.get("uz") or ""},
                "image": p.get("imageHigh") or p.get("imageLow") or "",
                "available_count": p.get("availableCount"),
                "purchase_price": p.get("purchasePrice"),
                "min_sell_price": p.get("minSellPrice"),
                "already_in": False,
                "variants": variants_by_pid.get(pid, []),
            })

        # Already-enrolled products (right panel) with their per-SKU sale prices.
        payout_rates = _sku_payout_rates(db, raw_shop)
        added_products = []
        for p in involved:
            pid = str(p.get("productId"))
            meta = meta_by_pid.get(pid, {})
            ptitle = p.get("title") if isinstance(p.get("title"), dict) else {}
            skus_out = []
            for sk in (p.get("skuList") or []):
                cur0 = int(sk.get("currentSellPrice") or 0)
                sp = int(sk.get("salePrice") or 0)
                disc = round((cur0 - sp) / cur0 * 100) if (cur0 > 0 and sp > 0) else 0
                row = {
                    "sku_id": sk.get("skuId"),
                    "sku_title": sk.get("skuTitle") or "",
                    "characteristics": sk.get("characteristics") or "",
                    "image": sk.get("imageHigh") or sk.get("imageLow") or "",
                    "current_price": cur0,
                    "max_price": int(sk.get("maxSuitablePrice") or 0),
                    "sale_price": sp,
                    "discount_pct": disc,
                }
                rt = payout_rates.get(sk.get("skuTitle") or "")
                if rt:
                    row["commission_rate"] = rt["commission_rate"]
                    row["logistics_per_unit"] = rt["logistics_per_unit"]
                skus_out.append(row)
            added_products.append({
                "product_id": p.get("productId"),
                "title": {
                    "ru": (ptitle.get("ru") if ptitle else "") or meta.get("name", ""),
                    "uz": (ptitle.get("uz") if ptitle else "") or meta.get("name", ""),
                },
                "image": p.get("imageHigh") or p.get("imageLow") or meta.get("image_url", ""),
                "purchase_price": cost_by_involved.get(pid) or uzum_cost_by_pid.get(pid),
                "skus": skus_out,
            })

    return _json_response({
        "sale": {
            "id": detail.get("id"),
            "status": detail.get("status"),
            "start_date": detail.get("startDate"),
            "finish_date": detail.get("finishDate"),
        },
        "category_rules": cat_rules,
        "default_min_discount": default_min,
        "products": products,
        "added_products": added_products,
        "involved_count": len(involved_ids),
    })


@products_bp.post("/api/sales/<int:sale_id>/add")
@login_required
def api_sale_add(sale_id: int):
    """Enroll selected products into a sale.

    Request (two accepted shapes per product):
      • explicit per-SKU prices (from the expenses modal):
        {"product_id": int, "skus": [{"sku_id": int, "new_price": int}, ...]}
      • a single discount % applied to every variant (from the Sales page):
        {"product_id": int, "discount_percent": number}

    Enrollment is product-level, so when only a discount is given we expand it
    across ALL the product's variants. Provided skuIds are validated against
    the product's own variants (no arbitrary skuIds reach Uzum)."""
    payload = request.get_json(force=True, silent=True) or {}
    raw_shop = str(payload.get("shop_id") or "").strip()
    req_products = payload.get("products") or []
    uid = int(current_user.get_id())

    from core.uzum_marketing import (
        add_products_to_sale, discounted_price, invalidate_shop_sales_cache,
        invalidate_product_sku_limits, UzumMarketingError,
    )

    with SessionLocal() as db:
        shop = _owned_shop_by_uzum_id(db, uid, raw_shop)
        if not shop:
            return _json_response({"error": "Магазин не найден или недоступен."}, 403)

        uzum_products = []
        skipped = []
        for entry in req_products:
            try:
                pid = int(entry.get("product_id"))
            except (TypeError, ValueError):
                continue

            group = db.execute(
                select(ProductGroup).where(
                    ProductGroup.shop_id == shop.id,
                    ProductGroup.uzum_product_id == str(pid),
                )
            ).scalar_one_or_none()
            if not group:
                skipped.append({"product_id": pid, "reason": "not_found"})
                continue

            variants = db.execute(
                select(Variant).where(Variant.group_id == group.id)
            ).scalars().all()
            # Valid skuId → current price, for validation + discount expansion.
            valid_skus = {}
            for v in variants:
                try:
                    sid = int(str(v.uzum_sku_id).strip())
                except (TypeError, ValueError):
                    continue
                valid_skus[sid] = int(v.price_sum or 0)

            sku_list = []
            explicit = entry.get("skus")
            if explicit:
                # Per-SKU prices set by the user — validate skuId + price.
                for s in explicit:
                    try:
                        sid = int(s.get("sku_id"))
                        np = int(round(float(s.get("new_price"))))
                    except (TypeError, ValueError):
                        continue
                    if sid in valid_skus and np > 0:
                        sku_list.append({"skuId": sid, "newSalePrice": np})
            else:
                # Single discount % expanded across all variants.
                try:
                    disc = float(entry.get("discount_percent") or 0)
                except (TypeError, ValueError):
                    disc = 0.0
                disc = max(0.0, min(99.0, disc))
                for sid, base in valid_skus.items():
                    if base <= 0:
                        continue
                    np = discounted_price(base, disc)
                    if np > 0:
                        sku_list.append({"skuId": sid, "newSalePrice": np})

            if not sku_list:
                skipped.append({"product_id": pid, "reason": "no_priced_skus"})
                continue
            uzum_products.append({"productId": pid, "skuList": sku_list})

    if not uzum_products:
        return _json_response({
            "error": "Не удалось собрать данные SKU (нет цен/skuId). "
                     "Синхронизируйте товары и попробуйте снова.",
            "skipped": skipped,
        }, 422)

    try:
        result = add_products_to_sale(raw_shop, sale_id, uzum_products)
    except UzumMarketingError as e:
        return _json_response({"error": str(e)}, 502)
    except Exception as e:
        msg = str(e)
        # Translate known Uzum business errors into actionable messages.
        if "DISCOUNT_NOT_ENOUGH" in msg:
            return _json_response({
                "error": "Uzum: скидки недостаточно. Uzum сравнивает цену акции "
                         "с действующей (уже сниженной) ценой товара, а не с полной — "
                         "поэтому цену нужно поставить ещё ниже. Точный максимум для "
                         "этого товара Uzum показывает только в кабинете.",
                "code": "DISCOUNT_NOT_ENOUGH",
            }, 422)
        if "PRICE" in msg.upper() and "LOW" in msg.upper():
            return _json_response({
                "error": "Uzum: цена слишком низкая (ниже допустимого минимума).",
                "code": "PRICE_TOO_LOW",
            }, 422)
        return _json_response({"error": f"Ошибка Uzum: {msg}"}, 502)

    # The shop's cached sales dataset is now stale (product moved suitable →
    # enrolled) — drop it so the next load rebuilds with the new state. Also
    # drop the per-SKU limits cache (prices/already_in just changed).
    invalidate_shop_sales_cache(raw_shop)
    invalidate_product_sku_limits(raw_shop)

    # Reflect the new sale price in our DB immediately, instead of waiting for
    # the next products sync (~15 min). The products sync would otherwise be
    # the only thing that updates Variant.price_sum, which is why a freshly
    # changed product kept showing the old price until then.
    try:
        with SessionLocal() as db2:
            for prod in uzum_products:
                for sk in prod["skuList"]:
                    db2.execute(
                        update(Variant)
                        .where(Variant.uzum_sku_id == str(sk["skuId"]))
                        .values(price_sum=int(sk["newSalePrice"]))
                    )
            db2.commit()
    except Exception:
        # Non-fatal: the sale was set on Uzum; the sync will reconcile prices.
        import traceback; traceback.print_exc()

    return _json_response({
        "ok": True,
        "added": len(uzum_products),
        "skipped": skipped,
        **(result or {}),
    })


@products_bp.post("/api/sales/<int:sale_id>/remove")
@login_required
def api_sale_remove(sale_id: int):
    """Remove a product from a sale (all its SKUs — removal is product-level).

    Request: {"shop_id": str, "product_id": int}. Mirrors api_sale_add: same
    ownership guard, same cache invalidation after the Uzum write."""
    payload = request.get_json(force=True, silent=True) or {}
    raw_shop = str(payload.get("shop_id") or "").strip()
    try:
        product_id = int(payload.get("product_id"))
    except (TypeError, ValueError):
        return _json_response({"error": "Некорректный товар."}, 400)
    uid = int(current_user.get_id())

    from core.uzum_marketing import (
        remove_product_from_sale, invalidate_shop_sales_cache,
        invalidate_product_sku_limits, UzumMarketingError,
    )

    with SessionLocal() as db:
        shop = _owned_shop_by_uzum_id(db, uid, raw_shop)
        if not shop:
            return _json_response({"error": "Магазин не найден или недоступен."}, 403)

    try:
        remove_product_from_sale(raw_shop, sale_id, product_id)
    except UzumMarketingError as e:
        return _json_response({"error": str(e)}, 502)
    except Exception as e:
        return _json_response({"error": f"Ошибка Uzum: {e}"}, 502)

    # Product moved enrolled → suitable; drop the stale caches (same as add).
    invalidate_shop_sales_cache(raw_shop)
    invalidate_product_sku_limits(raw_shop, product_id)

    return _json_response({"ok": True})


@products_bp.get("/api/product/<int:product_id>/eligible-sales")
@login_required
def api_product_eligible_sales(product_id: int):
    """Which joinable sales a single product can be enrolled into.

    Product-centric counterpart of /api/sales/<id>/suitable: used by the
    "Add to sale" modal launched from the expenses (or product) page. Scans
    only joinable sales (CREATED/ACTIVE) and checks this productId against each
    one's suitable-products list. Returns the product's variants too so the
    modal can show exactly what will be enrolled (whole product = all SKUs)."""
    raw_shop = (request.args.get("shop_id") or "").strip()
    uid = int(current_user.get_id())
    with SessionLocal() as db:
        shop = _owned_shop_by_uzum_id(db, uid, raw_shop)
        if not shop:
            return _json_response({"error": "Магазин не найден или недоступен."}, 403)

        # Local product + variants (what gets enrolled).
        group = db.execute(
            select(ProductGroup).where(
                ProductGroup.shop_id == shop.id,
                ProductGroup.uzum_product_id == str(product_id),
            )
        ).scalar_one_or_none()
        product_name = group.name if group else ""
        variants = []
        if group:
            vrows = db.execute(
                select(Variant).where(Variant.group_id == group.id)
                .order_by(func.lower(Variant.sku))
            ).scalars().all()
            for v in vrows:
                try:
                    sku_id = int(str(v.uzum_sku_id).strip()) if v.uzum_sku_id else None
                except (TypeError, ValueError):
                    sku_id = None
                variants.append({
                    "sku": v.sku or "",
                    "sku_id": sku_id,
                    "color": v.color or "",
                    "image_url": (v.image_url or group.image_url) or "",
                    "qty": int(v.uzum_quantity or 0),
                    "current_price": int(v.price_sum or 0),
                    "sell_price": int(v.price_sum or 0),
                    "cost_price": int(v.purchase_price or 0),
                    "storage": int(v.paid_storage_amount or 0),
                })
            # High-expense SKUs first — they're the ones worth discounting.
            variants.sort(key=lambda x: x["storage"], reverse=True)

    try:
        from core.uzum_marketing import get_shop_sales_dataset, UzumMarketingError
        # Cached per-shop dataset (built once, reused across products/clicks).
        dataset = get_shop_sales_dataset(raw_shop)
        eligible = []
        for s in dataset["sales"]:
            # A product is relevant to a sale if it can still be ADDED
            # (in suitable-products) OR is ALREADY enrolled (added products
            # drop off "suitable").
            in_suitable = str(product_id) in s["suitable_ids"]
            already_in = str(product_id) in s["involved_ids"]
            if not (in_suitable or already_in):
                continue
            eligible.append({
                "id": s["id"],
                "title": _split_sale_title(s.get("title")),
                "image_url": s.get("image_url") or {},
                "start_date": s.get("start_date"),
                "finish_date": s.get("finish_date"),
                "status": s.get("status"),
                "already_in": already_in,
                "min_discount": s.get("min_discount", 1),
            })
    except UzumMarketingError as e:
        return _json_response({"error": str(e)}, 502)
    except Exception as e:
        return _json_response({"error": f"Ошибка Uzum: {e!s}"}, 502)

    return _json_response({
        "product": {
            "product_id": product_id,
            "name": product_name,
            "variants": variants,
        },
        "eligible_sales": eligible,
    })


@products_bp.get("/api/sales/<int:sale_id>/product/<int:product_id>/sku-limits")
@login_required
def api_sale_product_sku_limits(sale_id: int, product_id: int):
    """Per-SKU price limits for a product in a specific sale.

    Returns each SKU's current price + max allowed sale price (Uzum's
    «Не больше X» / fair-discount ceiling). Uses the suitable-skus endpoint for
    not-yet-added products; falls back to the enrolled products' skuList (which
    carries maxSuitablePrice) for products already in the sale."""
    raw_shop = (request.args.get("shop_id") or "").strip()
    uid = int(current_user.get_id())
    with SessionLocal() as db:
        shop = _owned_shop_by_uzum_id(db, uid, raw_shop)
    if not shop:
        return _json_response({"error": "Магазин не найден или недоступен."}, 403)
    try:
        from core.uzum_marketing import get_product_sku_limits, UzumMarketingError
        # Cached per (shop, sale, product) — the suitable-skus call is the slow
        # part of opening the modal; the expenses page pre-warms it on hover/load.
        out = get_product_sku_limits(raw_shop, sale_id, product_id)
    except UzumMarketingError as e:
        return _json_response({"error": str(e)}, 502)
    except Exception as e:
        return _json_response({"error": f"Ошибка Uzum: {e!s}"}, 502)
    # Attach per-SKU payout rates (commission + logistics) from finance history
    # so the UI can show «К выводу» live as the price changes.
    try:
        with SessionLocal() as db:
            rates = _sku_payout_rates(db, raw_shop)
        for sk in out:
            rt = rates.get(str(sk.get("sku_title") or ""))
            if rt:
                sk["commission_rate"] = rt["commission_rate"]
                sk["logistics_per_unit"] = rt["logistics_per_unit"]
    except Exception:
        pass
    return _json_response({"skus": out})


@products_bp.get("/api/sales/enrolled-products")
@login_required
def api_enrolled_products():
    """Map of productId → [sale titles] for products currently enrolled in any
    joinable (CREATED/ACTIVE) sale. Used by the expenses page to badge product
    rows as "В акции" without a per-product API call."""
    raw_shop = (request.args.get("shop_id") or "").strip()
    uid = int(current_user.get_id())
    with SessionLocal() as db:
        shop = _owned_shop_by_uzum_id(db, uid, raw_shop)
    if not shop:
        return _json_response({"error": "Магазин не найден или недоступен."}, 403)
    try:
        from core.uzum_marketing import get_shop_sales_dataset, UzumMarketingError
        # Same cached dataset the modal uses → this page-load call also warms
        # the cache, so the first modal click is already fast.
        dataset = get_shop_sales_dataset(raw_shop)
        by_product: dict[str, list] = {}
        for s in dataset["sales"]:
            title = _split_sale_title(s.get("title"))
            for pid in s["involved_ids"]:
                by_product.setdefault(str(pid), []).append(title)
    except UzumMarketingError as e:
        return _json_response({"error": str(e)}, 502)
    except Exception as e:
        return _json_response({"error": f"Ошибка Uzum: {e!s}"}, 502)
    return _json_response({"enrolled": by_product})


def _mask_openapi_token(token: str | None) -> str:
    """Render a saved OpenAPI token as `abcd••••••••wxyz` for display.

    Only the first and last 4 characters ever reach the browser — the middle is
    replaced by a FIXED-width run of dots, so the mask neither leaks the token's
    true length nor overflows the input on very long tokens. Tokens too short to
    mask safely (< 12 chars) are dotted out entirely rather than half-revealed.
    """
    tok = (token or "").strip()
    if not tok:
        return ""
    if len(tok) < 12:
        return "•" * len(tok)
    return f"{tok[:4]}{'•' * 12}{tok[-4:]}"


@products_bp.get("/fetch")
@login_required
def fetch_page():
    return render_template(
        "fetch.html",
        openapi_token_masked=_mask_openapi_token(
            getattr(current_user, "uzum_openapi_token", None)
        ),
    )

@products_bp.get("/groups/<int:group_id>")
@login_required
def group_detail(group_id: int):
    uid = int(current_user.get_id())
    allowed_shop_ids = _user_shop_ids(uid)
    with SessionLocal() as db:
        group = db.get(ProductGroup, group_id)
        if not group:
            return render_template("not_found.html", message="Product not found"), 404
        if not _current_user_is_admin() and group.shop_id not in allowed_shop_ids:
            return render_template("not_found.html", message="Product not found"), 404

        # Sort variants by color first (groups same color together), then SKU.
        variants = db.execute(
            select(Variant).where(Variant.group_id == group_id)
            .order_by(func.lower(func.coalesce(Variant.color, "")), func.lower(Variant.sku))
        ).scalars().all()

        # 30d sales from sales_lines (Tashkent window [today-30, today+1)).
        # Old path filtered `period_from >= d_from AND period_to <= today` — the new
        # equivalent is `created_at >= today-30d 00:00 AND created_at < today+1d 00:00`.
        shop = db.get(Shop, group.shop_id)
        sales_30d_map: dict[int, int] = {}
        cost_map: dict[int, int] = {}
        if shop:
            today = _today_app_tz()
            start_ts, _ = day_bounds_tashkent(today - timedelta(days=30))
            _, end_ts = day_bounds_tashkent(today)
            agg_rows = read_sales_aggregated(
                shop.uzum_id,
                start_ts,
                end_ts,
                group_by="sku",
                session=db,
            )
            # Build lookups (assign, don't accumulate — avoids double-counting).
            # `sales_lines.sku_id` carries the seller SKU code (e.g. "LUXUZ-RING-СИНИЙ-17"),
            # which matches `Variant.sku` — NOT the numeric `Variant.uzum_sku_id`.
            by_title: dict[str, int] = {}
            by_sku_id: dict[str, int] = {}
            cost_by_title: dict[str, int] = {}
            cost_by_sku_id: dict[str, int] = {}
            for row in agg_rows:
                title = (row.get("sku_title") or "").strip()
                qty = int(row.get("qty_sum") or 0)
                cost_sum = int(row.get("purchase_price_sum") or 0)
                cost_unit = cost_sum // qty if qty > 0 and cost_sum > 0 else 0
                if title:
                    by_title[title] = qty
                    by_title[title.upper()] = qty
                    if cost_unit > 0:
                        cost_by_title[title] = cost_unit
                        cost_by_title[title.upper()] = cost_unit
                sid = row.get("sku_id")
                if sid:
                    sid_s = str(sid)
                    by_sku_id[sid_s] = qty
                    by_sku_id[sid_s.upper()] = qty
                    if cost_unit > 0:
                        cost_by_sku_id[sid_s] = cost_unit
                        cost_by_sku_id[sid_s.upper()] = cost_unit
            for v in variants:
                vsku = v.sku or ""
                matched = by_sku_id.get(vsku) or by_sku_id.get(vsku.upper()) or 0
                if matched == 0 and v.uzum_sku_id:
                    matched = by_sku_id.get(v.uzum_sku_id, 0)
                if matched == 0:
                    matched = by_title.get(vsku) or by_title.get(vsku.upper()) or 0
                sales_30d_map[v.id] = matched

                cost = cost_by_sku_id.get(vsku) or cost_by_sku_id.get(vsku.upper()) or 0
                if cost == 0 and v.uzum_sku_id:
                    cost = cost_by_sku_id.get(v.uzum_sku_id, 0)
                if cost == 0:
                    cost = cost_by_title.get(vsku) or cost_by_title.get(vsku.upper()) or 0
                if cost > 0:
                    cost_map[v.id] = cost

    return render_template("group_detail.html", group=group, variants=variants,
                           sales_30d_map=sales_30d_map, cost_map=cost_map)


def _categorize_expense_ledger(exp_rows):
    """Bucket `expenses_ledger` rows into warehouse / marketing / misc plus
    netted logistics refunds. Mirrors the owner's categorization decisions
    (see the block comment in ``economics_data_api``). Returns grand totals
    and per-shop (str uzum_id keyed) breakdowns. Shared by the main period
    block and the previous-period delta snapshot so the two never diverge.
    """
    t_warehouse = t_marketing = t_misc = 0
    t_log_refunds = 0  # ≤ 0 — Возврат credits, net against Логистика
    per_shop_exp: dict[str, dict] = {}
    per_shop_log_refund: dict[str, int] = {}
    for r in exp_rows:
        svc_raw = (r.service or "").lower()
        # Normalize Uzbek apostrophe variants (U+02BB, U+2019, U+02BC)
        # to ASCII so substring matches work consistently.
        svc = svc_raw
        for _ap in ("ʻ", "’", "ʼ"):
            svc = svc.replace(_ap, "'")
        op = (r.op_type or "").strip()
        sign = 1 if op == "Оплата" else -1
        amt = int(float(r.amount or 0)) * sign
        # Ledger logistics: skip Оплата charges, net Возврат credits.
        if "logistika" in svc or "logistic" in svc:
            if op == "Оплата":
                continue
            t_log_refunds += amt  # amt < 0 (Возврат)
            sid_lr = str(r.shop_id)
            per_shop_log_refund[sid_lr] = per_shop_log_refund.get(sid_lr, 0) + amt
            continue
        # Skip inter-shop balance redistribution — internal transfer, not a cost.
        if "balansni qayta taqsimlash" in svc:
            continue
        if ("saqlash" in svc) or ("ombor" in svc) or ("qaytarish" in svc):
            cat = "warehouse"
        elif ("pulli targ" in svc) or ("paytirish" in svc):
            cat = "marketing"
        else:
            cat = "misc"
        if cat == "warehouse":   t_warehouse += amt
        elif cat == "marketing": t_marketing += amt
        else:                    t_misc      += amt
        sid_str = str(r.shop_id)
        bucket = per_shop_exp.setdefault(sid_str, {"warehouse": 0, "marketing": 0, "misc": 0})
        bucket[cat] += amt
    return {
        "warehouse": t_warehouse, "marketing": t_marketing, "misc": t_misc,
        "log_refunds": t_log_refunds,
        "per_shop_exp": per_shop_exp,
        "per_shop_log_refund": per_shop_log_refund,
    }


def _payout_snapshot(db, shop_uzum_ids, date_from, date_to):
    """Lightweight grand-total snapshot (revenue + the fields `payoutOf` needs)
    for one window. Feeds the previous-period delta pills without paying for a
    second full economics payload."""
    rev = comm = logi = sp = 0
    if shop_uzum_ids:
        start_ts, _ = day_bounds_tashkent(date_from)
        _, end_ts = day_bounds_tashkent(date_to)
        for row in read_sales_aggregated(shop_uzum_ids, start_ts, end_ts, group_by="sku", session=db):
            rev  += int(row.get("revenue_sum") or 0)
            comm += int(row.get("commission_sum") or 0)
            logi += int(row.get("logistics_sum") or 0)
            sp   += int(row.get("seller_profit_sum") or 0)
        int_shop_ids = []
        for sid in shop_uzum_ids:
            try: int_shop_ids.append(int(sid))
            except (TypeError, ValueError): pass
        if int_shop_ids:
            exp_rows = db.execute(
                select(ExpensesLedger).where(
                    ExpensesLedger.shop_id.in_(int_shop_ids),
                    ExpensesLedger.day >= date_from,
                    ExpensesLedger.day <= date_to,
                )
            ).scalars().all()
            cat = _categorize_expense_ledger(exp_rows)
            # Логистика gross-only — Возврат credits not netted (see main block).
            return {
                "sales_revenue": rev, "sales_commission": comm,
                "sales_logistics": logi, "sales_seller_profit": sp,
                "expenses_marketing": cat["marketing"],
                "expenses_warehouse": cat["warehouse"],
                "expenses_misc": cat["misc"],
            }
    return {
        "sales_revenue": rev, "sales_commission": comm, "sales_logistics": logi,
        "sales_seller_profit": sp,
        "expenses_marketing": 0, "expenses_warehouse": 0, "expenses_misc": 0,
    }


@products_bp.get("/economics")
@login_required
def economics_page():
    return render_template("economics.html")


@products_bp.get("/api/economics/data")
@login_required
def economics_data_api():
    """Returns economics data for the selected date range from local finance_orders DB."""
    today = _today_app_tz()
    raw_from = request.args.get("date_from", "").strip()
    raw_to   = request.args.get("date_to",   "").strip()
    raw_shop = request.args.get("shop_id",   "").strip()  # "" or "all" → all owned shops; else uzum_id
    try:
        date_from = date.fromisoformat(raw_from) if raw_from else today.replace(day=1)
    except ValueError:
        date_from = today.replace(day=1)
    try:
        date_to = date.fromisoformat(raw_to) if raw_to else today
    except ValueError:
        date_to = today

    uid = int(current_user.get_id())
    allowed_shop_ids = _user_shop_ids(uid)

    with SessionLocal() as db:
        # Get uzum_ids for allowed shops + mapping from internal shop_id to uzum_id
        owned_shops = db.execute(select(Shop).where(Shop.id.in_(allowed_shop_ids))).scalars().all() if allowed_shop_ids else []
        # Optional ?shop_id= filter, validated against ownership
        active_shops = owned_shops
        if raw_shop and raw_shop.lower() != "all":
            active_shops = [s for s in owned_shops if str(s.uzum_id) == raw_shop]
        shop_uzum_ids   = [s.uzum_id for s in active_shops]
        active_shop_ids = [s.id for s in active_shops]
        shop_id_to_uzum = {s.id: s.uzum_id for s in active_shops}
        uzum_to_name    = {s.uzum_id: (s.name or s.uzum_id) for s in active_shops}

        # Shops list for the picker (always returned regardless of filter)
        shops_list = [{"uzum_id": s.uzum_id, "name": s.name or s.uzum_id} for s in owned_shops]

        # Aggregate finance_orders per (shop, sku) and per (shop) for the window
        per_shop_sales: dict[tuple[str, str], dict] = {}
        per_shop_totals: dict[str, dict] = {}  # uzum_id → {revenue, commission, logistics, qty, purchase_price}
        daily_revenue: dict[str, int] = {}
        daily_profit_proxy: dict[str, int] = {}  # revenue - commission - logistics (no per-day cost in finance_orders)
        daily_qty: dict[str, int] = {}
        # Period-independent per-SKU unit cost (purchase_price/qty over a wide
        # window). Used for "Вложено в товар" / stock_cost so the displayed
        # inventory value doesn't change when the user toggles date ranges.
        # Stock_qty already comes from Variant.uzum_quantity/warehouse_quantity
        # — those are also period-independent — so cost must be too.
        cost_map: dict[tuple[str, str], int] = {}
        if shop_uzum_ids:
            start_ts, _ = day_bounds_tashkent(date_from)
            _, end_ts = day_bounds_tashkent(date_to)

            # All-time cost lookup, always anchored to TODAY so the per-SKU
            # average cost is identical no matter which period chip the user
            # picks. Floor at 2020-01-01 (older than any data we have).
            cost_floor = date(2020, 1, 1)
            cost_start_ts, _ = day_bounds_tashkent(cost_floor)
            _, cost_end_ts   = day_bounds_tashkent(today)
            cost_rows = read_sales_aggregated(
                shop_uzum_ids,
                cost_start_ts,
                cost_end_ts,
                group_by="sku",
                session=db,
            )
            for row in cost_rows:
                sid = str(row.get("shop_id"))
                title = (row.get("sku_title") or "").strip()
                qty = int(row.get("qty_sum") or 0)
                pp  = int(row.get("purchase_price_sum") or 0)
                if qty <= 0 or pp <= 0:
                    continue
                uc = pp // qty
                if title:
                    cost_map[(sid, title)] = uc
                    cost_map[(sid, title.upper())] = uc
                if row.get("sku_id"):
                    cost_map[(sid, str(row["sku_id"]))] = uc

            agg_rows = read_sales_aggregated(
                shop_uzum_ids,
                start_ts,
                end_ts,
                group_by="sku",
                session=db,
            )
            for row in agg_rows:
                sid = str(row.get("shop_id"))
                title = (row.get("sku_title") or "").strip()
                qty = int(row.get("qty_sum") or 0)
                sell = int(row.get("revenue_sum") or 0)
                comm = int(row.get("commission_sum") or 0)
                logi = int(row.get("logistics_sum") or 0)
                pp   = int(row.get("purchase_price_sum") or 0)
                # seller_profit == Uzum's per-line "К выводу" (withdrawable):
                # revenue − commission − logistics, computed by Uzum itself.
                # Summed directly so the KPI card doesn't reconstruct it from
                # the (currently sellPrice-based) revenue figure.
                sp   = int(row.get("seller_profit_sum") or 0)
                entry = {"qty": qty, "sell_price": sell,
                         "commission": comm, "logistics": logi,
                         "purchase_price": pp, "seller_profit": sp}
                if title:
                    per_shop_sales[(sid, title)] = entry
                    per_shop_sales[(sid, title.upper())] = entry
                if row.get("sku_id"):
                    per_shop_sales[(sid, str(row["sku_id"]))] = entry
                # Per-shop totals (sum across all SKUs, no double-counting since
                # each finance_orders SKU row appears once)
                pst = per_shop_totals.setdefault(sid, {
                    "revenue": 0, "commission": 0, "logistics": 0,
                    "qty": 0, "purchase_price": 0, "seller_profit": 0,
                })
                pst["revenue"]        += sell
                pst["commission"]     += comm
                pst["logistics"]      += logi
                pst["qty"]            += qty
                pst["purchase_price"] += pp
                pst["seller_profit"]  += sp

            # Daily breakdown for "Продажи по дням" chart
            day_rows = read_sales_aggregated(
                shop_uzum_ids,
                start_ts,
                end_ts,
                group_by="day",
                session=db,
            )
            for row in day_rows:
                bucket = row.get("bucket")
                if bucket is None:
                    continue
                bkey = bucket.date().isoformat() if hasattr(bucket, "date") else str(bucket)
                rev = int(row.get("revenue_sum") or 0)
                comm = int(row.get("commission_sum") or 0)
                logi = int(row.get("logistics_sum") or 0)
                qty = int(row.get("qty_sum") or 0)
                daily_revenue[bkey] = daily_revenue.get(bkey, 0) + rev
                daily_profit_proxy[bkey] = daily_profit_proxy.get(bkey, 0) + (rev - comm - logi)
                daily_qty[bkey] = daily_qty.get(bkey, 0) + qty

        # ── Categorized expenses from expenses_ledger ────────────────────
        # Ledger "Logistika" handling:
        #   • Оплата rows (per-order return-leg charges) — EXCLUDED: money
        #     movement, not an expense.
        #   • Возврат rows (credits Uzum pays back) — also EXCLUDED from
        #     Логистика. Their charge date (`day`) rarely matches the order's
        #     `period_from`, so netting them distorted bounded periods.
        # Логистика = gross finance_orders forward leg only
        # (seller_profit == sell - commission - logistics_fee row-by-row).
        t_exp_warehouse = t_exp_marketing = t_exp_misc = 0
        per_shop_exp: dict[str, dict] = {}  # uzum_id (str) → {warehouse, marketing, misc}
        if shop_uzum_ids:
            # ExpensesLedger.shop_id is int; cast our str uzum_ids.
            int_shop_ids = []
            for sid in shop_uzum_ids:
                try: int_shop_ids.append(int(sid))
                except (TypeError, ValueError): pass
            if int_shop_ids:
                exp_rows = db.execute(
                    select(ExpensesLedger).where(
                        ExpensesLedger.shop_id.in_(int_shop_ids),
                        ExpensesLedger.day >= date_from,
                        ExpensesLedger.day <= date_to,
                    )
                ).scalars().all()
                cat = _categorize_expense_ledger(exp_rows)
                t_exp_warehouse     = cat["warehouse"]
                t_exp_marketing     = cat["marketing"]
                t_exp_misc          = cat["misc"]
                per_shop_exp        = cat["per_shop_exp"]

        stmt = select(ProductGroup).where(ProductGroup.is_archived == False)
        if active_shop_ids:
            stmt = stmt.where(ProductGroup.shop_id.in_(active_shop_ids))
        else:
            stmt = stmt.where(False)
        groups = db.execute(stmt).scalars().all()

        items = []
        t_stock_cost = t_stock_qty = t_sales_rev = 0
        t_sales_qty  = t_sales_profit = t_commission = t_logistics = 0
        t_sales_cost = 0
        t_stock_qty_uzum = t_stock_qty_wh = 0
        t_stock_cost_uzum = t_stock_cost_wh = 0
        active_skus = 0

        for g in groups:
            g_stock_qty = g_stock_cost = g_sales_qty = 0
            g_stock_qty_uzum = g_stock_qty_wh = 0
            g_stock_cost_uzum = g_stock_cost_wh = 0
            g_sales_rev = g_sales_cost = g_commission = g_logistics = 0
            g_uzum_id = shop_id_to_uzum.get(g.shop_id, "")
            g_skus = []

            for v in g.variants:
                v_cost_db = v.purchase_price or 0  # cost from local Variant.purchase_price (may be 0)
                qty_uzum  = v.uzum_quantity or 0
                qty_wh    = v.warehouse_quantity or 0
                stock_qty = qty_uzum + qty_wh
                if v.sku:
                    g_skus.append(v.sku)

                # "Active SKU" = currently on sale on Uzum (has stock available to buy).
                # Counts unique variants with Uzum stock > 0; not affected by period.
                if qty_uzum > 0:
                    active_skus += 1

                fin = None
                for key in [v.sku, (v.sku or "").upper(), v.barcode,
                             (v.barcode or "").upper(), v.uzum_sku_id]:
                    if key and (g_uzum_id, key) in per_shop_sales:
                        fin = per_shop_sales[(g_uzum_id, key)]
                        break

                sq         = fin["qty"]              if fin else 0
                total_sell = fin.get("sell_price", 0) if fin else 0
                total_comm = fin.get("commission",  0) if fin else 0
                total_logi = fin.get("logistics",   0) if fin else 0
                # finance.purchase_price_sum is the actual cost-of-goods Uzum reports
                # for the sold units in this window — use it directly. If absent
                # (variant had no sales), fall back to qty * Variant.purchase_price.
                fin_cogs   = int(fin.get("purchase_price", 0)) if fin else 0
                cogs       = fin_cogs if fin_cogs > 0 else (sq * v_cost_db)

                # Per-unit cost for stock_cost computation. Period-INDEPENDENT
                # so "Вложено в товар" stays stable when the user toggles
                # date chips (qty itself is also period-independent).
                #   1) Variant.purchase_price if set (user-entered cost)
                #   2) cost_map: wide-window finance avg (2-year lookback)
                #   3) Fallback: period-scoped finance (rarely needed)
                #   4) 0 (nothing known about this SKU)
                if v_cost_db > 0:
                    unit_cost = v_cost_db
                else:
                    unit_cost = 0
                    for key in [v.sku, (v.sku or "").upper(), v.barcode,
                                 (v.barcode or "").upper(), v.uzum_sku_id]:
                        if key and (g_uzum_id, key) in cost_map:
                            unit_cost = cost_map[(g_uzum_id, key)]
                            break
                    if unit_cost == 0 and fin and fin.get("qty", 0) > 0 and fin.get("purchase_price", 0) > 0:
                        unit_cost = fin["purchase_price"] // fin["qty"]

                g_stock_qty       += stock_qty
                g_stock_qty_uzum  += qty_uzum
                g_stock_qty_wh    += qty_wh
                g_stock_cost      += stock_qty * unit_cost
                g_stock_cost_uzum += qty_uzum  * unit_cost
                g_stock_cost_wh   += qty_wh    * unit_cost

                g_sales_qty  += sq
                g_sales_rev  += total_sell
                g_sales_cost += cogs
                g_commission += total_comm
                g_logistics  += total_logi

            g_sales_profit = g_sales_rev - g_sales_cost - g_commission - g_logistics
            roi = round(g_sales_profit / g_sales_cost * 100, 1) if g_sales_cost > 0 else 0

            items.append({
                "id": g.id, "name": g.name, "image_url": normalize_uzum_image_url(g.image_url) or "",
                "sku": " ".join(g_skus),
                "stock_qty": g_stock_qty, "stock_cost": g_stock_cost,
                "stock_qty_uzum": g_stock_qty_uzum, "stock_qty_warehouse": g_stock_qty_wh,
                "stock_cost_uzum": g_stock_cost_uzum, "stock_cost_warehouse": g_stock_cost_wh,
                "sales_qty": g_sales_qty, "sales_revenue": g_sales_rev,
                "sales_cost": g_sales_cost, "sales_commission": g_commission,
                "sales_logistics": g_logistics, "sales_profit": g_sales_profit,
                "roi": roi,
            })
            t_stock_cost      += g_stock_cost;       t_stock_qty       += g_stock_qty
            t_stock_qty_uzum  += g_stock_qty_uzum;   t_stock_qty_wh    += g_stock_qty_wh
            t_stock_cost_uzum += g_stock_cost_uzum;  t_stock_cost_wh   += g_stock_cost_wh
            t_sales_rev       += g_sales_rev;        t_sales_qty       += g_sales_qty
            t_sales_profit    += g_sales_profit;     t_commission      += g_commission
            t_logistics       += g_logistics;        t_sales_cost      += g_sales_cost

        items.sort(key=lambda x: x["sales_profit"], reverse=True)

        # Логистика is the gross forward-leg fee from finance_orders only.
        # Ledger Возврат credits are NOT netted in: their charge date (`day`)
        # rarely lines up with the order's `period_from`, so netting distorted
        # bounded periods (could even go negative). Gross-only keeps the figure
        # consistent with finance_orders for any window.

        # Per-shop breakdown for hero cards (Revenue, Profit). Uses finance totals
        # for revenue + commission/logistics; profit here is the same proxy used
        # in the daily series (revenue - commission - logistics, no COGS) because
        # COGS is only available at SKU resolution and accumulating it per shop
        # would double-count variant data we already aggregated above.
        per_shop = []
        for s in active_shops:
            pst = per_shop_totals.get(s.uzum_id, {})
            exp = per_shop_exp.get(s.uzum_id, {})
            rev  = int(pst.get("revenue", 0))
            comm = int(pst.get("commission", 0))
            logi = int(pst.get("logistics", 0))
            cogs = int(pst.get("purchase_price", 0))
            wh   = int(exp.get("warehouse", 0))
            mkt  = int(exp.get("marketing", 0))
            misc = int(exp.get("misc", 0))
            shop_expenses = comm + logi + mkt + wh + misc
            per_shop.append({
                "uzum_id": s.uzum_id,
                "name":    s.name or s.uzum_id,
                "revenue":  rev,
                "expenses": shop_expenses,
                "profit":   rev - shop_expenses - cogs,
            })
        per_shop.sort(key=lambda x: x["revenue"], reverse=True)

        # Build daily series (one bucket per day in range, zero-fill gaps)
        daily_series = []
        d = date_from
        while d <= date_to:
            k = d.isoformat()
            daily_series.append({
                "date": k,
                "revenue": daily_revenue.get(k, 0),
                "profit": daily_profit_proxy.get(k, 0),
                "qty": daily_qty.get(k, 0),
            })
            d += timedelta(days=1)

        # 12-month dynamics (revenue + profit-proxy) ending at date_to's month
        monthly_dynamics = []
        if shop_uzum_ids:
            # Build a 12-month window ending at the month containing date_to
            anchor = date(date_to.year, date_to.month, 1)
            # Start 11 months before anchor
            y, m = anchor.year, anchor.month
            for _ in range(11):
                m -= 1
                if m == 0:
                    m = 12; y -= 1
            m_start = date(y, m, 1)
            # End is first day of month AFTER anchor
            ay, am = anchor.year, anchor.month + 1
            if am == 13:
                am = 1; ay += 1
            m_end_excl = date(ay, am, 1)
            start_ts_m, _ = day_bounds_tashkent(m_start)
            end_ts_m, _   = day_bounds_tashkent(m_end_excl)
            month_rows = read_sales_aggregated(
                shop_uzum_ids,
                start_ts_m,
                end_ts_m,
                group_by="month",
                session=db,
            )
            monthly_map: dict[str, dict] = {}
            for row in month_rows:
                bucket = row.get("bucket")
                if bucket is None:
                    continue
                mkey = f"{bucket.year:04d}-{bucket.month:02d}"
                rev = int(row.get("revenue_sum") or 0)
                comm = int(row.get("commission_sum") or 0)
                logi = int(row.get("logistics_sum") or 0)
                prev = monthly_map.setdefault(mkey, {"revenue": 0, "profit": 0})
                prev["revenue"] += rev
                prev["profit"]  += rev - comm - logi
            # Walk months chronologically and zero-fill
            cy, cm = m_start.year, m_start.month
            for _ in range(12):
                mkey = f"{cy:04d}-{cm:02d}"
                entry = monthly_map.get(mkey, {"revenue": 0, "profit": 0})
                monthly_dynamics.append({
                    "month": mkey,
                    "revenue": entry["revenue"],
                    "profit":  entry["profit"],
                })
                cm += 1
                if cm == 13:
                    cm = 1; cy += 1

        # Final profit subtracts the extra expense categories on top of the
        # per-item profit (which only subtracted commission/logistics/cogs).
        # Logistics is gross-only (no Возврат credit), so profit does not add
        # any refund back either — both stay purely finance_orders-derived.
        t_sales_profit_full = t_sales_profit - (t_exp_warehouse + t_exp_marketing + t_exp_misc)

        # Year-to-date revenue across ALL owned shops — feeds the annual tax
        # limit bar. Always all-shops and Jan-1→today, independent of the
        # active shop filter / selected period, so it's folded into every
        # response instead of a separate front-end request.
        ytd_revenue = 0
        owned_uzum_ids = [s.uzum_id for s in owned_shops]
        if owned_uzum_ids:
            yr_start, _ = day_bounds_tashkent(date(today.year, 1, 1))
            _, yr_end = day_bounds_tashkent(today)
            for row in read_sales_aggregated(owned_uzum_ids, yr_start, yr_end, group_by="month", session=db):
                ytd_revenue += int(row.get("revenue_sum") or 0)

        # Previous period of equal length, immediately preceding date_from —
        # feeds the ↑/↓ delta pills. Computed server-side (lightweight totals
        # only) so the page no longer makes a second full request for it.
        period_days = (date_to - date_from).days + 1
        prev_to   = date_from - timedelta(days=1)
        prev_from = prev_to - timedelta(days=period_days - 1)
        prev_totals = _payout_snapshot(db, shop_uzum_ids, prev_from, prev_to)

        return _json_response({
            "items": items,
            "year_to_date_revenue": ytd_revenue,
            "prev_totals": prev_totals,
            "totals": {
                "stock_cost": t_stock_cost, "stock_qty": t_stock_qty,
                "stock_qty_uzum": t_stock_qty_uzum, "stock_qty_warehouse": t_stock_qty_wh,
                "stock_cost_uzum": t_stock_cost_uzum, "stock_cost_warehouse": t_stock_cost_wh,
                "sales_revenue": t_sales_rev, "sales_qty": t_sales_qty,
                "sales_profit": t_sales_profit_full, "sales_commission": t_commission,
                "sales_logistics": t_logistics, "sales_cost": t_sales_cost,
                # "К выводу" = Uzum-reported withdrawable (sum of seller_profit).
                "sales_seller_profit": sum(p.get("seller_profit", 0) for p in per_shop_totals.values()),
                "expenses_warehouse": t_exp_warehouse,
                "expenses_marketing": t_exp_marketing,
                "expenses_misc":      t_exp_misc,
                "active_skus": active_skus,
            },
            "shops":            shops_list,
            "active_shop_id":   (raw_shop or "all"),
            "per_shop":         per_shop,
            "daily_series":     daily_series,
            "monthly_dynamics": monthly_dynamics,
        })

@products_bp.get("/calculator")
@login_required
def calculator_page():
    return render_template("calculator.html")


# ----------------------------
# Product pages (requested)
# ----------------------------
@products_bp.get("/products")
@login_required
def products_redirect():
    # alias to match "main products page"
    return redirect(url_for("products_bp.groups_page"))


# ----------------------------
# Uzum sync API (new)
# ----------------------------
@products_bp.post("/api/uzum/sync")
@login_required
def uzum_sync():
    try:
        return _uzum_sync_inner()
    except Exception as e:
        import traceback; traceback.print_exc()
        return _json_response({"error": str(e)}, 500)


@products_bp.post("/api/uzum/sync-all")
@login_required
@admin_required
def uzum_sync_all():
    """Disabled — bulk admin shop sync was retired with the legacy browser pipeline."""
    return _json_response({
        "error": "Массовая синхронизация всех магазинов отключена. "
                 "Синхронизируйте магазины по отдельности через OpenAPI токен."
    }, 410)


def _uzum_sync_inner():
    payload = request.get_json(force=True, silent=True) or {}
    shop_id = str(payload.get("shop_id") or "").strip()

    if not shop_id:
        return _json_response({"error": "shop_id missing"}, 400)

    if not _current_user_is_admin():
        uid = int(current_user.get_id())
        with SessionLocal() as _db:
            existing = _db.execute(select(Shop).where(Shop.uzum_id == shop_id)).scalar_one_or_none()
        if not existing or existing.owner_id != uid:
            return _json_response({"error": "Access denied to this shop"}, 403)

    size = int(payload.get("size") or 100)
    sync_all = bool(payload.get("sync_all", True))
    max_pages = int(payload.get("max_pages") or 500)

    # OpenAPI-only path. The legacy browser/admin-token products sync has been
    # retired; users must connect a personal Uzum OpenAPI token to sync.
    openapi_token = None
    try:
        uid = int(current_user.get_id())
        with SessionLocal() as _db:
            u = _db.execute(select(User).where(User.id == uid)).scalar_one_or_none()
            openapi_token = (u.uzum_openapi_token if u else None) or None
    except Exception:
        openapi_token = None

    if not openapi_token:
        return _json_response({"error": "Uzum OpenAPI \u0442\u043e\u043a\u0435\u043d \u043d\u0435 \u043d\u0430\u0441\u0442\u0440\u043e\u0435\u043d \u0432 \u043f\u0440\u043e\u0444\u0438\u043b\u0435."}, 401)

    try:
        result = _app._sync_products_via_openapi(
            shop_id, openapi_token,
            size=size, max_pages=max_pages,
        )
        return _json_response({"ok": True, "shop_id": shop_id, **result})
    except Exception as e:
        import traceback; traceback.print_exc()
        return _json_response({"error": f"\u0421\u0438\u043d\u0445\u0440\u043e\u043d\u0438\u0437\u0430\u0446\u0438\u044f \u043d\u0435 \u0443\u0434\u0430\u043b\u0430\u0441\u044c: {e!s}"}, 500)


@products_bp.post("/api/uzum/sync-finance")
@login_required
def uzum_sync_finance():
    """Refresh per-variant 30-day averages from the local finance_orders cache.

    Reads aggregated SKU stats from finance_orders (kept fresh by the hourly +
    nightly finance loops \u2014 no Uzum API call here). Updates each variant's
    sales_30d_finance / avg_daily_sales / purchase_price / sell_price_uzum /
    """
    try:
        payload = request.get_json(force=True, silent=True) or {}
        shop_id = str(payload.get("shop_id") or "").strip()
        if not shop_id:
            return _json_response({"error": "shop_id missing"}, 400)

        with SessionLocal() as db:
            shop_obj = db.execute(select(Shop).where(Shop.uzum_id == shop_id)).scalar_one_or_none()
            if not shop_obj:
                return _json_response({"error": "\u041c\u0430\u0433\u0430\u0437\u0438\u043d \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d. \u0421\u043d\u0430\u0447\u0430\u043b\u0430 \u0432\u044b\u043f\u043e\u043b\u043d\u0438\u0442\u0435 \u0441\u0438\u043d\u0445\u0440\u043e\u043d\u0438\u0437\u0430\u0446\u0438\u044e \u0442\u043e\u0432\u0430\u0440\u043e\u0432."}, 404)

            today = _today_app_tz()
            start_ts, _ = day_bounds_tashkent(today - timedelta(days=30))
            _, end_ts = day_bounds_tashkent(today)

            try:
                agg_rows = read_sales_aggregated(
                    shop_obj.uzum_id,
                    start_ts,
                    end_ts,
                    group_by="sku",
                    session=db,
                )
            except Exception as e:
                return _json_response({"error": f"\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043f\u0440\u043e\u0447\u0438\u0442\u0430\u0442\u044c \u0434\u0430\u043d\u043d\u044b\u0435: {e!s}"}, 500)

            # Build {sku_key: {qty, price, sell_price, commission, logistics}} from
            # the aggregated FinanceOrder rows. read_sales_aggregated already sums
            # across the 30-day window per sku_title \u2014 we just convert totals to
            # per-unit averages here.
            sales_map: dict[str, dict] = {}
            unique_skus = 0
            for row in agg_rows:
                title = (row.get("sku_title") or "").strip()
                qty = int(row.get("qty_sum") or 0)
                if not title or qty <= 0:
                    continue
                rev  = int(row.get("revenue_sum") or 0)
                cost = int(row.get("purchase_price_sum") or 0)
                comm = int(row.get("commission_sum") or 0)
                logi = int(row.get("logistics_sum") or 0)
                entry = {
                    "qty":        qty,
                    "price":      cost // qty if cost > 0 else 0,
                    "sell_price": rev  // qty if rev  > 0 else 0,
                    "commission": comm // qty if comm > 0 else 0,
                    "logistics":  logi // qty if logi > 0 else 0,
                }
                sales_map[title] = entry
                sales_map[title.upper()] = entry
                sid = row.get("sku_id")
                if sid:
                    sales_map[str(sid)] = entry
                unique_skus += 1

            # Update ALL variants in DB for this shop
            variants = db.execute(
                select(Variant).join(ProductGroup).where(ProductGroup.shop_id == shop_obj.id)
            ).scalars().all()
            updated_count = 0

            for v in variants:
                sku_key = (v.sku or "").strip()
                data = sales_map.get(sku_key) or sales_map.get(sku_key.upper())
                if data is None and v.uzum_sku_id:
                    data = sales_map.get(str(v.uzum_sku_id))
                if data is None and v.barcode:
                    bc_key = v.barcode.strip()
                    data = sales_map.get(bc_key) or sales_map.get(bc_key.upper())

                qty_val = data["qty"] if data else 0
                v.sales_30d_finance = qty_val
                v.avg_daily_sales = qty_val / 30.0
                if data and data.get("price", 0) > 0:
                    v.purchase_price = data["price"]
                if data and data.get("sell_price", 0) > 0:
                    v.sell_price_uzum = int(data["sell_price"])
                if data and data.get("commission", 0) > 0:
                    v.commission_per_unit = int(data["commission"])
                if data and data.get("logistics", 0) > 0:
                    v.logistics_per_unit = int(data["logistics"])

                updated_count += 1

            db.commit()

        return _json_response({"ok": True, "updated": updated_count, "sales_records": unique_skus})
    except Exception as e:
        return _json_response({"error": f"\u041e\u0448\u0438\u0431\u043a\u0430 \u0441\u0435\u0440\u0432\u0435\u0440\u0430: {str(e)}"}, 500)

@products_bp.get("/api/groups/<int:group_id>/sales-range")
@login_required
def group_sales_range(group_id: int):
    """Return per-variant sales for a custom date range from local finance_orders DB."""
    days_param = request.args.get("days")
    date_from_str = request.args.get("date_from")
    date_to_str = request.args.get("date_to")

    today = date.today()
    if date_from_str and date_to_str:
        try:
            d_from = date.fromisoformat(date_from_str)
            d_to = date.fromisoformat(date_to_str)
        except ValueError:
            return _json_response({"error": "Invalid date format. Use YYYY-MM-DD."}, 400)
        days_label = (d_to - d_from).days + 1
    else:
        days_label = int(days_param) if days_param else 30
        d_to = today
        d_from = today - timedelta(days=days_label)

    with SessionLocal() as db:
        group = db.get(ProductGroup, group_id)
        if not group:
            return _json_response({"error": "Group not found"}, 404)

        uid = int(current_user.get_id())
        allowed = _user_shop_ids(uid)
        if group.shop_id not in allowed:
            return _json_response({"error": "Access denied"}, 403)

        shop = db.get(Shop, group.shop_id)
        if not shop:
            return _json_response({"error": "Shop not found"}, 404)

        variants = db.execute(
            select(Variant).where(Variant.group_id == group_id)
        ).scalars().all()

        # Aggregate sales_lines by SKU for this shop + date range. Old path
        # `period_from >= d_from AND period_to <= d_to` → right-open
        # `created_at >= d_from 00:00 AND created_at < d_to+1d 00:00`
        # (Tashkent).
        start_ts, _ = day_bounds_tashkent(d_from)
        _, end_ts = day_bounds_tashkent(d_to)
        agg_rows = read_sales_aggregated(
            shop.uzum_id,
            start_ts,
            end_ts,
            group_by="sku",
            session=db,
        )
        # Build lookups. `sales_lines.sku_id` is the seller SKU code that
        # matches `Variant.sku` directly — title / uzum_sku_id are fallbacks.
        sales_by_title: dict[str, int] = {}
        sales_by_sku_id: dict[str, int] = {}
        for row in agg_rows:
            title = (row.get("sku_title") or "").strip()
            qty = int(row.get("qty_sum") or 0)
            if title:
                sales_by_title[title] = qty
                sales_by_title[title.upper()] = qty
            sid = row.get("sku_id")
            if sid:
                sid_s = str(sid)
                sales_by_sku_id[sid_s] = qty
                sales_by_sku_id[sid_s.upper()] = qty

        result = []
        for v in variants:
            vsku = v.sku or ""
            qty = sales_by_sku_id.get(vsku) or sales_by_sku_id.get(vsku.upper()) or 0
            if qty == 0 and v.uzum_sku_id and v.uzum_sku_id in sales_by_sku_id:
                qty = sales_by_sku_id[v.uzum_sku_id]
            if qty == 0:
                for key in [vsku, vsku.upper(), v.barcode,
                            (v.barcode or "").upper()]:
                    if key and key in sales_by_title:
                        qty = sales_by_title[key]
                        break
            result.append({"variant_id": v.id, "qty": qty})

    return _json_response({"sales": result, "days": days_label,
                           "date_from": date_from_str, "date_to": date_to_str})


@products_bp.get("/api/groups/<int:group_id>/daily-stats")
@login_required
def group_daily_stats(group_id: int):
    """Per-day sales + revenue + delta-vs-prev-period + per-variant daily series.

    Used by the redesigned group detail page (hero bar chart + per-row sparklines).
    """
    from models import FinanceOrder

    date_from_raw = (request.args.get("date_from") or "").strip()
    date_to_raw   = (request.args.get("date_to")   or "").strip()
    today = _today_app_tz()

    if date_from_raw and date_to_raw:
        try:
            d_from = date.fromisoformat(date_from_raw)
            d_to   = date.fromisoformat(date_to_raw)
        except ValueError:
            return _json_response({"error": "Invalid date format. Use YYYY-MM-DD."}, 400)
        if d_to < d_from:
            d_from, d_to = d_to, d_from
        days = (d_to - d_from).days + 1
        days = max(1, min(days, 365))
    else:
        try:
            days = int(request.args.get("days", "30"))
        except ValueError:
            days = 30
        days = max(1, min(days, 365))
        d_to = today
        d_from = today - timedelta(days=days - 1)

    with SessionLocal() as db:
        group = db.get(ProductGroup, group_id)
        if not group:
            return _json_response({"error": "Group not found"}, 404)

        uid = int(current_user.get_id())
        if not _current_user_is_admin() and group.shop_id not in _user_shop_ids(uid):
            return _json_response({"error": "Access denied"}, 403)

        shop = db.get(Shop, group.shop_id)
        if not shop:
            return _json_response({"error": "Shop not found"}, 404)

        variants = db.execute(
            select(Variant).where(Variant.group_id == group_id)
        ).scalars().all()
        # Lookup keyed by UPPER(sku) and by uzum_sku_id — match the lenient
        # matching the page-load 30d code uses (different casing/whitespace
        # in FinanceOrder.sku_title vs Variant.sku is common).
        sku_to_vid: dict[str, int] = {}
        for v in variants:
            if v.sku:
                sku_to_vid[v.sku.strip().upper()] = v.id
            if v.uzum_sku_id:
                sku_to_vid[str(v.uzum_sku_id).strip().upper()] = v.id

        # Previous-period window = same length immediately before d_from
        d_from_prev = d_from - timedelta(days=days)

        # Daily aggregate for the whole shop, filtered in Python (lenient match
        # by sku_title or sku_id). Bounded by one shop × N days.
        if variants:
            rows = db.execute(
                select(
                    FinanceOrder.period_from.label("d"),
                    FinanceOrder.sku_title.label("sku"),
                    FinanceOrder.sku_id.label("sid"),
                    func.coalesce(func.sum(FinanceOrder.amount), 0).label("qty"),
                    # Revenue = seller_profit + commission + logistics (Uzum's own
                    # identity). NOT sum(sell_price): the group=false backfill stored
                    # a bogus ~6x-low sellPrice. REVERT to sum(sell_price) once Uzum
                    # fixes the API and the backfill is re-run. See core/sales_reads.py.
                    func.coalesce(func.sum(
                        FinanceOrder.seller_profit
                        + FinanceOrder.commission
                        + FinanceOrder.logistics_fee
                    ), 0).label("rev"),
                    func.coalesce(func.sum(FinanceOrder.commission), 0).label("comm"),
                    func.coalesce(func.sum(FinanceOrder.logistics_fee), 0).label("log"),
                    func.coalesce(func.sum(FinanceOrder.purchase_price), 0).label("cost"),
                )
                .where(
                    FinanceOrder.shop_id == str(shop.uzum_id),
                    FinanceOrder.period_from >= d_from,
                    FinanceOrder.period_from <= d_to,
                )
                .group_by(FinanceOrder.period_from, FinanceOrder.sku_title, FinanceOrder.sku_id)
            ).all()
        else:
            rows = []

        # Build day list (chronological, oldest → today)
        day_list = [d_from + timedelta(days=i) for i in range(days)]
        day_index = {d: i for i, d in enumerate(day_list)}

        group_daily = [0] * days
        group_revenue_daily = [0] * days
        group_revenue = 0
        group_commission = 0
        group_logistics = 0
        group_cost = 0
        per_variant_daily: dict[int, list[int]] = {v.id: [0] * days for v in variants}
        per_variant_qty:  dict[int, int] = {v.id: 0 for v in variants}
        per_variant_comm: dict[int, int] = {v.id: 0 for v in variants}
        per_variant_rev:  dict[int, int] = {v.id: 0 for v in variants}
        per_variant_log:  dict[int, int] = {v.id: 0 for v in variants}
        per_variant_cost: dict[int, int] = {v.id: 0 for v in variants}

        for r in rows:
            idx = day_index.get(r.d)
            if idx is None:
                continue
            vid = sku_to_vid.get((r.sku or "").strip().upper())
            if vid is None and r.sid is not None:
                vid = sku_to_vid.get(str(r.sid).strip().upper())
            if vid is None:
                continue  # row belongs to a different group, skip
            qty = int(r.qty or 0)
            rev = int(r.rev or 0)
            comm = int(r.comm or 0)
            logf = int(r.log or 0)
            cost = int(r.cost or 0)
            group_daily[idx] += qty
            group_revenue_daily[idx] += rev
            group_revenue += rev
            group_commission += comm
            group_logistics += logf
            group_cost += cost
            per_variant_daily[vid][idx] += qty
            per_variant_qty[vid] += qty
            per_variant_comm[vid] += comm
            per_variant_rev[vid] += rev
            per_variant_log[vid] += logf
            per_variant_cost[vid] += cost

        total_sales = sum(group_daily)
        avg_check = (group_revenue // total_sales) if total_sales > 0 else 0

        # Previous period total (for delta %): same lenient match
        if variants:
            prev_rows = db.execute(
                select(
                    FinanceOrder.sku_title.label("sku"),
                    FinanceOrder.sku_id.label("sid"),
                    func.coalesce(func.sum(FinanceOrder.amount), 0).label("qty"),
                )
                .where(
                    FinanceOrder.shop_id == str(shop.uzum_id),
                    FinanceOrder.period_from >= d_from_prev,
                    FinanceOrder.period_from < d_from,
                )
                .group_by(FinanceOrder.sku_title, FinanceOrder.sku_id)
            ).all()
            prev_total = 0
            for r in prev_rows:
                vid = sku_to_vid.get((r.sku or "").strip().upper())
                if vid is None and r.sid is not None:
                    vid = sku_to_vid.get(str(r.sid).strip().upper())
                if vid is not None:
                    prev_total += int(r.qty or 0)
        else:
            prev_total = 0

        if prev_total > 0:
            delta_pct = round((total_sales - prev_total) * 100.0 / prev_total)
        elif total_sales > 0:
            delta_pct = 100
        else:
            delta_pct = 0

    # Per-variant averages for the period:
    #   - commission_avg = commission ÷ units sold (avg сум per unit)
    #   - commission_pct = commission ÷ sell_price × 100 (effective rate %,
    #     normalized for price variation across days)
    per_variant_comm_avg: dict[int, int] = {
        vid: (per_variant_comm[vid] // per_variant_qty[vid]) if per_variant_qty[vid] > 0 else 0
        for vid in per_variant_comm
    }
    per_variant_comm_pct: dict[int, float] = {
        vid: round(per_variant_comm[vid] * 100.0 / per_variant_rev[vid], 1) if per_variant_rev[vid] > 0 else 0.0
        for vid in per_variant_comm
    }
    # Profit = revenue − commission − logistics − cost-of-goods (purchase_price
    # as reported by Uzum's finance API). Storage cost is not subtracted here
    # since it's not allocated per-order in finance_orders.
    group_profit = group_revenue - group_commission - group_logistics - group_cost
    profit_pct = round(group_profit * 100.0 / group_revenue, 1) if group_revenue > 0 else 0.0
    per_variant_profit: dict[int, int] = {
        vid: per_variant_rev[vid] - per_variant_comm[vid] - per_variant_log[vid] - per_variant_cost[vid]
        for vid in per_variant_rev
    }
    per_variant_profit_pct: dict[int, float] = {
        vid: round(per_variant_profit[vid] * 100.0 / per_variant_rev[vid], 1) if per_variant_rev[vid] > 0 else 0.0
        for vid in per_variant_rev
    }

    return _json_response({
        "days": days,
        "date_from": d_from.isoformat(),
        "date_to":   d_to.isoformat(),
        "total_sales": total_sales,
        "total_revenue": group_revenue,
        "total_commission": group_commission,
        "total_logistics": group_logistics,
        "total_cost":      group_cost,
        "total_profit":    group_profit,
        "profit_pct":      profit_pct,
        "avg_check": avg_check,
        "delta_pct": delta_pct,
        "group_daily": group_daily,
        "group_daily_revenue": group_revenue_daily,
        "day_labels": [d.isoformat() for d in day_list],
        "per_variant_daily": per_variant_daily,
        "per_variant_commission": per_variant_comm,        # total сум in period
        "per_variant_commission_avg": per_variant_comm_avg,  # avg сум/шт
        "per_variant_commission_pct": per_variant_comm_pct,  # effective rate %
        "per_variant_revenue": per_variant_rev,
        "per_variant_logistics": per_variant_log,
        "per_variant_cost":      per_variant_cost,
        "per_variant_profit":    per_variant_profit,
        "per_variant_profit_pct": per_variant_profit_pct,
        "per_variant_qty": per_variant_qty,
    })


@products_bp.get("/api/groups/<int:group_id>/variants")
@login_required
def get_group_variants_api(group_id: int):
    with SessionLocal() as db:
        group = db.get(ProductGroup, group_id)
        group_img = group.image_url if group else None
        variants = db.execute(
            select(Variant).where(Variant.group_id == group_id).order_by(func.lower(Variant.sku))
        ).scalars().all()

        items = []
        for v in variants:
            s30 = v.sales_30d_finance or 0
            stock = (v.uzum_quantity or 0) + (v.warehouse_quantity or 0)
            need = (s30 * 2) - stock
            items.append({
                "id": v.id,
                "sku": v.sku,
                "image_url": normalize_uzum_image_url(v.image_url or group_img),
                "sales_30d": s30,
                "need_60d": need
            })

        return _json_response({
            "variants": items
        })

# ----------------------------
# Invoice / Restock Logic
# ----------------------------
_RESTOCK_PERIOD_DAYS = (7, 10, 15, 30, 60, 90)
_RESTOCK_CHUNK_DEFAULT = 35
_RESTOCK_CHUNK_MIN = 1
_RESTOCK_CHUNK_MAX = 100


def _resolve_restock_limit(raw):
    """Clamp the per-invoice SKU limit to [1, 100], default 35."""
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return _RESTOCK_CHUNK_DEFAULT
    return max(_RESTOCK_CHUNK_MIN, min(_RESTOCK_CHUNK_MAX, n))


def _chunk_by_shop(items, chunk_size, shop_key):
    """Split ``items`` into invoice chunks of at most ``chunk_size`` that never
    span two shops. ``items`` must already be sorted so each shop's rows are
    contiguous. A shop with more rows than ``chunk_size`` simply produces
    several chunks. Returns ``[[]]`` when there is nothing to chunk so the
    template can render its empty state.
    """
    chunks = []
    cur_shop = object()  # sentinel that never equals a real shop key
    for it in items:
        sk = shop_key(it)
        if not chunks or sk != cur_shop or len(chunks[-1]) >= chunk_size:
            chunks.append([])
            cur_shop = sk
        chunks[-1].append(it)
    return chunks or [[]]


def _resolve_restock_window():
    """Resolve the restock sales window from request args.

    Mirrors group_sales_range: an explicit ``date_from``/``date_to`` pair wins,
    otherwise a ``days`` chip (one of 7/10/15/30/60/90, default 30). Returns
    ``(start_ts, end_ts, days_label, date_from_str, date_to_str)`` where the
    timestamps are Tashkent day bounds ready for read_sales_aggregated.
    """
    days_param = request.args.get("days")
    date_from_str = (request.args.get("date_from") or "").strip()
    date_to_str = (request.args.get("date_to") or "").strip()
    today = _today_app_tz()

    if date_from_str and date_to_str:
        try:
            d_from = date.fromisoformat(date_from_str)
            d_to = date.fromisoformat(date_to_str)
            days_label = (d_to - d_from).days + 1
        except ValueError:
            d_from, d_to, days_label = today - timedelta(days=30), today, 30
            date_from_str = date_to_str = ""
    else:
        try:
            days_label = int(days_param) if days_param else 30
        except (ValueError, TypeError):
            days_label = 30
        if days_label not in _RESTOCK_PERIOD_DAYS:
            days_label = 30
        d_to = today
        d_from = today - timedelta(days=days_label)

    start_ts, _ = day_bounds_tashkent(d_from)
    _, end_ts = day_bounds_tashkent(d_to)
    return start_ts, end_ts, days_label, date_from_str, date_to_str


def _restock_sales_maps(db, shop_uzum_by_pk, start_ts, end_ts):
    """Build per-shop live sales lookups from finance_orders for the window.

    Returns ``{shop_pk: (by_sku_id, by_title)}``. Same matching keys as
    group_sales_range so the restock page reads sales straight from finance
    rather than the stale Variant.sales_30d_finance snapshot.
    """
    sales_maps: dict[int, tuple[dict, dict]] = {}
    for shop_pk, uzum_id in shop_uzum_by_pk.items():
        try:
            agg_rows = read_sales_aggregated(
                uzum_id, start_ts, end_ts, group_by="sku", session=db,
            )
        except Exception as e:
            print(f"[Restock] finance read failed for shop={uzum_id}: {e!r}")
            agg_rows = []
        by_sku_id: dict[str, int] = {}
        by_title: dict[str, int] = {}
        for row in agg_rows:
            qty = int(row.get("qty_sum") or 0)
            title = (row.get("sku_title") or "").strip()
            if title:
                by_title[title] = qty
                by_title[title.upper()] = qty
            sid = row.get("sku_id")
            if sid:
                sid_s = str(sid)
                by_sku_id[sid_s] = qty
                by_sku_id[sid_s.upper()] = qty
        sales_maps[shop_pk] = (by_sku_id, by_title)
    return sales_maps


def _restock_match(v, by_sku_id, by_title):
    """Look a variant up in (by_sku_id, by_title) maps using the same lenient
    matching everywhere on the restock page: sku → uzum_sku_id → title/barcode.
    Returns 0 when nothing matches.
    """
    vsku = v.sku or ""
    val = by_sku_id.get(vsku) or by_sku_id.get(vsku.upper()) or 0
    if val == 0 and v.uzum_sku_id and str(v.uzum_sku_id) in by_sku_id:
        val = by_sku_id[str(v.uzum_sku_id)]
    if val == 0:
        for key in [vsku, vsku.upper(), v.barcode, (v.barcode or "").upper()]:
            if key and key in by_title:
                val = by_title[key]
                break
    return val


def _restock_period_sales(v, by_sku_id, by_title):
    """Per-variant sales for the window (sku_id → uzum_sku_id → title/barcode)."""
    return _restock_match(v, by_sku_id, by_title)


def _restock_cost_maps(db, shop_uzum_by_pk, end_ts):
    """Per-shop average unit-cost lookups from *all-time* finance_orders.

    Returns ``{shop_pk: (by_sku_id, by_title)}`` where each value is the
    average себестоимость per unit (purchase_price_sum // qty_sum) across all
    finance data up to ``end_ts``. Used as a fallback when a variant's stored
    ``purchase_price`` is 0 — Uzum's product API often omits the cost, but the
    finance/orders feed reports it on every sold unit. Mirrors the unit-cost
    derivation on the economics page.
    """
    cost_floor = date(2020, 1, 1)
    start_ts, _ = day_bounds_tashkent(cost_floor)
    cost_maps: dict[int, tuple[dict, dict]] = {}
    for shop_pk, uzum_id in shop_uzum_by_pk.items():
        try:
            rows = read_sales_aggregated(
                uzum_id, start_ts, end_ts, group_by="sku", session=db,
            )
        except Exception as e:
            print(f"[Restock] cost read failed for shop={uzum_id}: {e!r}")
            rows = []
        by_sku_id: dict[str, int] = {}
        by_title: dict[str, int] = {}
        for row in rows:
            qty = int(row.get("qty_sum") or 0)
            pp = int(row.get("purchase_price_sum") or 0)
            if qty <= 0 or pp <= 0:
                continue
            uc = pp // qty
            title = (row.get("sku_title") or "").strip()
            if title:
                by_title[title] = uc
                by_title[title.upper()] = uc
            sid = row.get("sku_id")
            if sid:
                sid_s = str(sid)
                by_sku_id[sid_s] = uc
                by_sku_id[sid_s.upper()] = uc
        cost_maps[shop_pk] = (by_sku_id, by_title)
    return cost_maps


@products_bp.get("/invoice/restock")
@login_required
def invoice_restock_page():
    shop_filter = (request.args.get("shop_id") or "").strip()
    uid = int(current_user.get_id())
    allowed_shop_ids = _user_shop_ids(uid)

    start_ts, end_ts, days_label, date_from_str, date_to_str = _resolve_restock_window()

    with SessionLocal() as db:
        if _current_user_is_admin():
            shops = db.execute(select(Shop)).scalars().all()
        else:
            shops = db.execute(select(Shop).where(Shop.id.in_(allowed_shop_ids))).scalars().all()

        # Shops in scope for the table (filter chip narrows to one).
        scope_shop_ids = list(allowed_shop_ids)
        if shop_filter and shop_filter.isdigit() and int(shop_filter) in allowed_shop_ids:
            scope_shop_ids = [int(shop_filter)]

        # Live per-shop sales for the chosen window, straight from finance_orders.
        shop_uzum_by_pk = {}
        shop_name_by_pk = {}
        if scope_shop_ids:
            for s in db.execute(select(Shop).where(Shop.id.in_(scope_shop_ids))).scalars().all():
                shop_uzum_by_pk[s.id] = s.uzum_id
                shop_name_by_pk[s.id] = s.name
        sales_maps = _restock_sales_maps(db, shop_uzum_by_pk, start_ts, end_ts)
        # All-time per-unit cost fallback for variants with no stored purchase_price.
        _, cost_end_ts = day_bounds_tashkent(_today_app_tz())
        cost_maps = _restock_cost_maps(db, shop_uzum_by_pk, cost_end_ts)

        stmt = select(Variant, ProductGroup).join(ProductGroup, Variant.group_id == ProductGroup.id)
        if scope_shop_ids:
            stmt = stmt.where(ProductGroup.shop_id.in_(scope_shop_ids))
        else:
            stmt = stmt.where(False)

        rows = db.execute(stmt).all()

        items = []
        for v, g in rows:
            by_sku_id, by_title = sales_maps.get(g.shop_id, ({}, {}))
            sales = _restock_period_sales(v, by_sku_id, by_title)
            u_qty = v.uzum_quantity or 0
            wh_qty = v.warehouse_quantity or 0

            # Logic: needed = sales in window. If u_qty < needed, restock = needed - u_qty
            if u_qty < sales:
                needed = sales - u_qty
                if wh_qty > 0:
                    restock = min(needed, wh_qty)
                    price = v.purchase_price or 0
                    if price <= 0:
                        c_by_sku_id, c_by_title = cost_maps.get(g.shop_id, ({}, {}))
                        price = _restock_match(v, c_by_sku_id, c_by_title) or 0
                    items.append({
                        "id": v.id,
                        "name": g.name,
                        "sku": v.sku,
                        "barcode": v.barcode,
                        "shop_id": g.shop_id,
                        "shop_name": shop_name_by_pk.get(g.shop_id, ""),
                        "sales_30d": sales,
                        "uzum_qty": u_qty,
                        "wh_qty": wh_qty,
                        "restock_qty": restock,
                        "price": price,
                        "total_price": restock * price,
                        "image_url": normalize_uzum_image_url(v.image_url or g.image_url)
                    })

        # Group invoices by shop: sort by shop name, then SKU within each shop.
        items.sort(key=lambda x: (
            str(x.get("shop_name") or "").strip().lower(),
            x.get("shop_id") or 0,
            str(x.get("sku") or "").strip().lower(),
        ))

        # Chunk into max `chunk_size` items per file/invoice (user-configurable
        # 1–100), never mixing two shops in one invoice.
        chunk_size = _resolve_restock_limit(request.args.get("limit"))
        chunks = _chunk_by_shop(items, chunk_size, lambda x: x.get("shop_id"))

    return render_template(
        "invoice_restock.html",
        chunks=chunks,
        shops=shops,
        current_shop=shop_filter,
        days_label=days_label,
        date_from=date_from_str,
        date_to=date_to_str,
        period_days=_RESTOCK_PERIOD_DAYS,
        chunk_limit=chunk_size,
        chunk_limit_min=_RESTOCK_CHUNK_MIN,
        chunk_limit_max=_RESTOCK_CHUNK_MAX,
    )

@products_bp.route("/invoice/restock/download", methods=["GET", "POST"])
@login_required
def invoice_restock_download():
    if not openpyxl:
        return _json_response({"error": "openpyxl library not installed. Please run: pip install openpyxl"}, 500)

    try:
        data_rows = []
        chunk_limit_raw = None

        if request.method == "POST":
            # Use data provided by the client (edited quantities)
            payload = request.get_json(force=True, silent=True) or {}
            items = payload.get("items") or []
            chunk_limit_raw = payload.get("limit")

            # Group by shop, then SKU, so files never mix shops and variants stay together.
            items.sort(key=lambda x: (
                x.get("shop_id") or 0,
                str(x.get("sku") or "").strip().lower(),
            ))

            for item in items:
                bc = str(item.get("barcode") or "").strip()
                try:
                    price = float(item.get("price") or 0)
                    qty = int(item.get("qty") or 0)
                except (ValueError, TypeError):
                    continue
                if qty > 0:
                    data_rows.append([bc, price, qty, item.get("shop_id")])
        else:
            # GET request: Auto-calculate based on live finance for the window.
            shop_filter = (request.args.get("shop_id") or "").strip()
            uid = int(current_user.get_id())
            allowed_shop_ids = _user_shop_ids(uid)
            start_ts, end_ts, _dl, _df, _dt = _resolve_restock_window()
            with SessionLocal() as db:
                scope_shop_ids = list(allowed_shop_ids)
                if shop_filter and shop_filter.isdigit() and int(shop_filter) in allowed_shop_ids:
                    scope_shop_ids = [int(shop_filter)]

                shop_uzum_by_pk = {}
                if scope_shop_ids:
                    for s in db.execute(select(Shop).where(Shop.id.in_(scope_shop_ids))).scalars().all():
                        shop_uzum_by_pk[s.id] = s.uzum_id
                sales_maps = _restock_sales_maps(db, shop_uzum_by_pk, start_ts, end_ts)
                _, cost_end_ts = day_bounds_tashkent(_today_app_tz())
                cost_maps = _restock_cost_maps(db, shop_uzum_by_pk, cost_end_ts)

                stmt = select(Variant, ProductGroup).join(ProductGroup, Variant.group_id == ProductGroup.id)
                if scope_shop_ids:
                    stmt = stmt.where(ProductGroup.shop_id.in_(scope_shop_ids))
                else:
                    stmt = stmt.where(False)
                stmt = stmt.order_by(ProductGroup.shop_id, Variant.sku)
                rows = db.execute(stmt).all()

                for v, g in rows:
                    by_sku_id, by_title = sales_maps.get(g.shop_id, ({}, {}))
                    sales = _restock_period_sales(v, by_sku_id, by_title)
                    u_qty = v.uzum_quantity or 0
                    wh_qty = v.warehouse_quantity or 0

                    if u_qty < sales:
                        needed = sales - u_qty
                        if wh_qty > 0:
                            restock = min(needed, wh_qty)
                            price = v.purchase_price or 0
                            if price <= 0:
                                c_by_sku_id, c_by_title = cost_maps.get(g.shop_id, ({}, {}))
                                price = _restock_match(v, c_by_sku_id, c_by_title) or 0
                            data_rows.append([v.barcode or "", price, restock, g.shop_id])

        # Chunk into max `chunk_size` items per file (user-configurable 1–100),
        # never mixing two shops in one file. Each row carries its shop id as a
        # trailing element used only for grouping — stripped before writing.
        chunk_size = _resolve_restock_limit(
            chunk_limit_raw if chunk_limit_raw is not None else request.args.get("limit")
        )
        _tagged = _chunk_by_shop(
            data_rows, chunk_size, lambda r: r[3] if len(r) > 3 else None
        )
        chunks = [[r[:3] for r in c] for c in _tagged]

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

        def create_wb(rows_subset):
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "\u0422\u043e\u0432\u0430\u0440\u044b \u043d\u0430 \u043e\u0442\u043f\u0440\u0430\u0432\u043a\u0443"
            ws.append(["\u0428\u0442\u0440\u0438\u0445\u043a\u043e\u0434 \u0442\u043e\u0432\u0430\u0440\u0430*", "\u0421\u0435\u0431\u0435\u0441\u0442\u043e\u0438\u043c\u043e\u0441\u0442\u044c (\u0441\u0443\u043c)*", "\u041a\u043e\u043b\u0438\u0447\u0435\u0441\u0442\u0432\u043e (\u0448\u0442)*"])
            for cell in ws[1]: cell.font = Font(bold=True)
            for r in rows_subset:
                ws.append(r)
            out = io.BytesIO()
            wb.save(out)
            out.seek(0)
            return out

        if len(chunks) == 1:
            out = create_wb(chunks[0])
            filename = f"invoice_restock_{timestamp}.xlsx"
            mimetype = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            try:
                return send_file(out, download_name=filename, as_attachment=True, mimetype=mimetype)
            except TypeError:
                # Fallback for older Flask versions
                return send_file(out, attachment_filename=filename, as_attachment=True, mimetype=mimetype)
        else:
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                for i, chunk in enumerate(chunks):
                    xlsx_io = create_wb(chunk)
                    zf.writestr(f"invoice_restock_{timestamp}_part{i+1}.xlsx", xlsx_io.getvalue())

            zip_buffer.seek(0)
            filename = f"invoice_restock_{timestamp}_multi.zip"
            mimetype = "application/zip"
            try:
                return send_file(zip_buffer, download_name=filename, as_attachment=True, mimetype=mimetype)
            except TypeError:
                return send_file(zip_buffer, attachment_filename=filename, as_attachment=True, mimetype=mimetype)

    except Exception as e:
        return _json_response({"error": f"Server Error: {str(e)}"}, 500)


@products_bp.route("/invoice/restock/upload-uzum", methods=["POST"])
@login_required
def invoice_restock_upload_uzum():
    if not openpyxl:
        return _json_response({"error": "openpyxl library not installed. Please run: pip install openpyxl"}, 500)

    payload = request.get_json(force=True, silent=True) or {}
    items = payload.get("items") or []
    shop_db_id = payload.get("shop_id")

    if not shop_db_id:
        return _json_response({"error": "Shop ID is required"}, 400)

    with SessionLocal() as db:
        shop = db.get(Shop, int(shop_db_id))
        if not shop:
            return _json_response({"error": "Shop not found in DB"}, 404)
        uzum_shop_id = shop.uzum_id

    data_rows = []
    zero_price_barcodes = []
    items.sort(key=lambda x: str(x.get("sku") or "").strip().lower())

    for item in items:
        bc = str(item.get("barcode") or "").strip()
        try:
            price = float(item.get("price") or 0)
            qty = int(item.get("qty") or 0)
        except (ValueError, TypeError):
            continue
        if qty > 0:
            # Uzum rejects the whole file if any cost is 0 — catch it here too.
            if price <= 0:
                zero_price_barcodes.append(bc or "—")
                continue
            data_rows.append([bc, price, qty])

    if zero_price_barcodes:
        return _json_response({
            "error": "Укажите себестоимость (больше 0) для штрихкодов: "
                     + ", ".join(zero_price_barcodes)
        }, 400)

    if not data_rows:
        return _json_response({"error": "No valid items to upload"}, 400)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "\u0422\u043e\u0432\u0430\u0440\u044b \u043d\u0430 \u043e\u0442\u043f\u0440\u0430\u0432\u043a\u0443"
    ws.append(["\u0428\u0442\u0440\u0438\u0445\u043a\u043e\u0434 \u0442\u043e\u0432\u0430\u0440\u0430*", "\u0421\u0435\u0431\u0435\u0441\u0442\u043e\u0438\u043c\u043e\u0441\u0442\u044c (\u0441\u0443\u043c)*", "\u041a\u043e\u043b\u0438\u0447\u0435\u0441\u0442\u0432\u043e (\u0448\u0442)*"])
    for cell in ws[1]: cell.font = Font(bold=True)
    for r in data_rows:
        ws.append(r)

    out = io.BytesIO()
    wb.save(out)
    file_bytes = out.getvalue()

    url = f"https://api-seller.uzum.uz/api/seller/shop/{uzum_shop_id}/v2/invoice/create-from-file"

    # Use current user's key if set, otherwise fall back to admin token.
    api_key = _get_fresh_api_key() or _get_admin_token()
    if not api_key:
        return _json_response({
            "error": "Uzum \u0442\u043e\u043a\u0435\u043d \u043e\u0442\u0441\u0443\u0442\u0441\u0442\u0432\u0443\u0435\u0442. \u0423\u0441\u0442\u0430\u043d\u043e\u0432\u0438\u0442\u0435 Chrome-\u0440\u0430\u0441\u0448\u0438\u0440\u0435\u043d\u0438\u0435 \u00abUzum Token Sync\u00bb "
                     "\u0438\u043b\u0438 \u0432\u0441\u0442\u0430\u0432\u044c\u0442\u0435 \u0442\u043e\u043a\u0435\u043d \u0432\u0440\u0443\u0447\u043d\u0443\u044e \u0432 \u041d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0430\u0445."
        }, 401)

    # Warn if already expired before even trying
    exp = _jwt_expires_in_seconds(api_key)
    if exp is not None and exp <= 0:
        return _json_response({
            "error": "Uzum \u0442\u043e\u043a\u0435\u043d \u0438\u0441\u0442\u0451\u043a. \u041e\u0442\u043a\u0440\u043e\u0439\u0442\u0435 \u043a\u0430\u0431\u0438\u043d\u0435\u0442 \u043f\u0440\u043e\u0434\u0430\u0432\u0446\u0430 Uzum \u2014 "
                     "\u0440\u0430\u0441\u0448\u0438\u0440\u0435\u043d\u0438\u0435 \u043e\u0431\u043d\u043e\u0432\u0438\u0442 \u0442\u043e\u043a\u0435\u043d \u0430\u0432\u0442\u043e\u043c\u0430\u0442\u0438\u0447\u0435\u0441\u043a\u0438."
        }, 401)

    headers = {"Authorization": f"Bearer {api_key}" if not api_key.startswith("Bearer ") else api_key}

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    file_name = f"invoice_restock_{timestamp}.xlsx"

    try:
        res = http_post_multipart(url, file_name, file_bytes, headers)
        return _json_response({"ok": True, "uzum_response": res, "rows_sent": len(data_rows)})
    except Exception as e:
        error_msg = str(e)
        if "401" in error_msg:
            return _json_response({"error": "\u0422\u043e\u043a\u0435\u043d Uzum \u0438\u0441\u0442\u0451\u043a \u0438\u043b\u0438 \u043d\u0435\u0434\u0435\u0439\u0441\u0442\u0432\u0438\u0442\u0435\u043b\u0435\u043d. "
                                   "\u041e\u0442\u043a\u0440\u043e\u0439\u0442\u0435 \u043a\u0430\u0431\u0438\u043d\u0435\u0442 \u043f\u0440\u043e\u0434\u0430\u0432\u0446\u0430 Uzum \u2014 \u0440\u0430\u0441\u0448\u0438\u0440\u0435\u043d\u0438\u0435 \u00abUzum Token Sync\u00bb "
                                   "\u043e\u0431\u043d\u043e\u0432\u0438\u0442 \u0435\u0433\u043e \u0430\u0432\u0442\u043e\u043c\u0430\u0442\u0438\u0447\u0435\u0441\u043a\u0438. \u0415\u0441\u043b\u0438 \u0440\u0430\u0441\u0448\u0438\u0440\u0435\u043d\u0438\u0435 \u043d\u0435 \u0443\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u043e, "
                                   "\u0441\u043a\u043e\u043f\u0438\u0440\u0443\u0439\u0442\u0435 \u0441\u0432\u0435\u0436\u0438\u0439 Authorization-\u0442\u043e\u043a\u0435\u043d \u0447\u0435\u0440\u0435\u0437 F12 \u2192 Network \u0438 "
                                   "\u0432\u0441\u0442\u0430\u0432\u044c\u0442\u0435 \u0435\u0433\u043e \u0432 \u041d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0430\u0445."}, 401)
        if "403" in error_msg:
            return _json_response({"error": "403 Forbidden: Token is valid but doesn't have permission for this Shop ID."}, 403)
        return _json_response({"error": f"Failed to upload: {error_msg}"}, 500)
