"""Finance-related routes extracted from app.py as a Flask Blueprint."""
from __future__ import annotations

import io
import json
from datetime import date, datetime, timedelta

from flask import Blueprint, render_template, request, send_file
from flask_login import current_user, login_required
from sqlalchemy import desc, func, select

from core.auth_helpers import _current_user_is_admin, _json_response, _user_shop_ids
from core.http_client import http_json
from core.time_helpers import _today_app_tz
from extensions import SessionLocal
from models import FinanceOrder, FinanceSyncLog, Shop

try:
    import openpyxl
    from openpyxl.styles import Font
except ImportError:
    openpyxl = None


finance_bp = Blueprint("finance_bp", __name__)

_app = None


def init_finance_routes(app_module):
    global _app
    _app = app_module


@finance_bp.get("/finance")
@login_required
def finance_page():
    uid = int(current_user.get_id())
    allowed = _user_shop_ids(uid)
    with SessionLocal() as db:
        shops = db.execute(select(Shop).where(Shop.id.in_(allowed))).scalars().all() if allowed else []
        sync_info = {}
        for shop in shops:
            last = db.execute(
                select(FinanceSyncLog)
                .where(FinanceSyncLog.shop_id == shop.uzum_id)
                .order_by(desc(FinanceSyncLog.synced_at))
                .limit(1)
            ).scalar_one_or_none()
            total = db.execute(
                select(func.count(FinanceOrder.id))
                .where(FinanceOrder.shop_id == shop.uzum_id)
            ).scalar() or 0
            sync_info[shop.uzum_id] = {
                "last_sync": last.synced_at.strftime("%Y-%m-%d %H:%M") if last else None,
                "last_type": last.sync_type if last else None,
                "total_records": total,
            }
    return render_template(
        "finance_sync.html",
        shops=shops,
        sync_info=sync_info,
        is_admin=_current_user_is_admin(),
    )


@finance_bp.post("/api/finance/sync")
@login_required
def api_finance_sync():
    shop_id = request.json.get("shop_id", "").strip() if request.is_json else ""
    if not shop_id:
        return _json_response({"error": "shop_id required"}, 400)

    with _app._finance_active_lock:
        if shop_id in _app._finance_active_shops:
            return _json_response({"error": "finance sync already running for this shop"}, 409)

    job_id = _app._create_sync_job(shop_id, "auto")
    _app._onboard_executor.submit(_app._run_manual_sync_job, job_id, shop_id, False)
    return _json_response({"job_id": job_id, "status": "queued", "shop_id": shop_id})


@finance_bp.get("/api/finance/debug-fetch")
@login_required
def api_finance_debug_fetch():
    shop_id = request.args.get("shop_id", "5983")
    raw_from = request.args.get("date_from", "").strip()
    raw_to = request.args.get("date_to", "").strip()

    today = _today_app_tz()
    try:
        d_from = date.fromisoformat(raw_from) if raw_from else today.replace(day=1)
    except ValueError:
        d_from = today.replace(day=1)
    try:
        d_to = date.fromisoformat(raw_to) if raw_to else today
    except ValueError:
        d_to = today

    from_ts = _app._app_day_start_ts(d_from)
    to_ts = _app._app_day_end_ts(d_to)
    url = (
        f"https://api-seller.uzum.uz/api/seller/finance/orders"
        f"?shopIds={shop_id}&dateFrom={from_ts}&dateTo={to_ts}"
        f"&group=true&page=0&size=2"
    )

    uz_resp = None
    try:
        uz_resp = http_json(url, headers={"Accept-Language": "uz"})
    except Exception:
        pass

    ru_resp = None
    try:
        ru_resp = http_json(url, headers={"Accept-Language": "ru-RU,ru;q=0.9"})
    except Exception:
        pass

    ru_resp2 = None
    try:
        ru_resp2 = http_json(url, headers={"x-language": "ru", "Accept-Language": "ru"})
    except Exception:
        pass

    def _extract_first_title(resp):
        if not isinstance(resp, dict):
            return None
        order_items = resp.get("orderItems", [])
        if not order_items:
            return None
        first = order_items[0]
        product_title = first.get("productTitle", "")
        sku_items = first.get("items", [])
        first_sku_title = sku_items[0].get("productTitle", "") if sku_items else ""
        return {"productTitle": product_title, "first_sku_productTitle": first_sku_title}

    return _json_response({
        "url": url,
        "date_from": d_from.isoformat(),
        "date_to": d_to.isoformat(),
        "uz_header_Accept-Language_uz": _extract_first_title(uz_resp),
        "ru_header_Accept-Language_ru": _extract_first_title(ru_resp),
        "ru_header_x-language_ru": _extract_first_title(ru_resp2),
    })


@finance_bp.post("/api/finance/sync-full")
@login_required
def api_finance_sync_full():
    shop_id = request.json.get("shop_id", "").strip() if request.is_json else ""
    if not shop_id:
        return _json_response({"error": "shop_id required"}, 400)

    with _app._finance_active_lock:
        if shop_id in _app._finance_active_shops:
            return _json_response({"error": "finance sync already running for this shop"}, 409)

    job_id = _app._create_sync_job(shop_id, "full")
    _app._onboard_executor.submit(_app._run_manual_sync_job, job_id, shop_id, True)
    return _json_response({"job_id": job_id, "status": "queued", "shop_id": shop_id})


@finance_bp.get("/api/finance/job-status/<job_id>")
@login_required
def api_finance_job_status(job_id):
    job = _app._get_sync_job(job_id)
    if not job:
        return _json_response({"error": "job not found"}, 404)
    return _json_response(job)


@finance_bp.get("/api/finance/active-jobs")
@login_required
def api_finance_active_jobs():
    return _json_response(_app._list_sync_jobs(statuses=("queued", "running")))


@finance_bp.get("/api/finance/data")
@login_required
def api_finance_data():
    shop_id = request.args.get("shop_id", "").strip()
    raw_from = request.args.get("date_from", "").strip()
    raw_to = request.args.get("date_to", "").strip()
    sku_filter = request.args.get("sku", "").strip()
    lang = request.args.get("lang", "ru").strip()

    today = _today_app_tz()
    try:
        d_from = date.fromisoformat(raw_from) if raw_from else today.replace(day=1)
    except ValueError:
        d_from = today.replace(day=1)
    try:
        d_to = date.fromisoformat(raw_to) if raw_to else today
    except ValueError:
        d_to = today

    uid = int(current_user.get_id())
    allowed = _user_shop_ids(uid)

    with SessionLocal() as db:
        if allowed:
            shops = db.execute(select(Shop).where(Shop.id.in_(allowed))).scalars().all()
            allowed_uzum_ids = [s.uzum_id for s in shops]
        else:
            allowed_uzum_ids = []

        if shop_id and shop_id not in allowed_uzum_ids:
            return _json_response({"error": "Access denied"}, 403)

        target_shops = [shop_id] if shop_id else allowed_uzum_ids
        if not target_shops:
            return _json_response({"items": [], "totals": {}})

        stmt = (
            select(FinanceOrder)
            .where(
                FinanceOrder.shop_id.in_(target_shops),
                FinanceOrder.period_from <= d_to,
                FinanceOrder.period_to >= d_from,
            )
            .order_by(FinanceOrder.period_from.desc())
        )
        if sku_filter:
            stmt = stmt.where(FinanceOrder.sku_title.contains(sku_filter))

        orders = db.execute(stmt).scalars().all()
        sku_map = {}
        for order in orders:
            key = order.sku_title
            if key not in sku_map:
                title = (order.product_title_ru or order.product_title or "") if lang == "ru" else (order.product_title or "")
                sku_map[key] = {
                    "sku": key,
                    "product_title": title,
                    "product_id": order.product_id,
                    "image_url": order.image_url or "",
                    "shop_id": order.shop_id,
                    "characteristics": order.characteristics or "",
                    "amount": 0,
                    "amount_returns": 0,
                    "sell_price": 0,
                    "commission": 0,
                    "seller_profit": 0,
                    "purchase_price": 0,
                    "logistics_fee": 0,
                    "seller_discount": 0,
                }
            item = sku_map[key]
            item["amount"] += order.amount
            item["amount_returns"] += order.amount_returns
            item["sell_price"] += order.sell_price
            item["commission"] += order.commission
            item["seller_profit"] += order.seller_profit
            item["purchase_price"] += order.purchase_price
            item["logistics_fee"] += order.logistics_fee
            item["seller_discount"] += order.seller_discount

        items = sorted(sku_map.values(), key=lambda value: value["amount"], reverse=True)
        labels = {
            "ru": {
                "amount": "Количество", "returns": "Возвраты", "revenue": "Выручка",
                "commission": "Комиссия", "profit": "Прибыль", "cost": "Себестоимость",
                "logistics": "Логистика", "sku": "Артикул", "product": "Товар",
                "orders": "Заказы", "total": "Итого", "sync": "Синхронизация",
                "full_sync": "Полная синхронизация", "refresh": "Обновить (30 дней)",
                "download": "Скачать Excel", "filter": "Фильтр", "from": "С",
                "to": "По", "search": "Поиск по SKU", "apply": "Применить",
            },
            "uz": {
                "amount": "Miqdori", "returns": "Qaytarishlar", "revenue": "Tushum",
                "commission": "Komissiya", "profit": "Foyda", "cost": "Tannarx",
                "logistics": "Logistika", "sku": "Artikul", "product": "Mahsulot",
                "orders": "Buyurtmalar", "total": "Jami", "sync": "Sinxronlash",
                "full_sync": "To'liq sinxronlash", "refresh": f"Yangilash ({_app.FINANCE_REFRESH_DAYS} kun)",
                "download": "Excel yuklab olish", "filter": "Filtr", "from": "Dan",
                "to": "Gacha", "search": "SKU bo'yicha qidirish", "apply": "Qo'llash",
            },
        }
        labels["ru"]["refresh"] = labels["ru"]["refresh"].replace("30", str(_app.FINANCE_REFRESH_DAYS), 1)

        return _json_response({
            "date_from": d_from.isoformat(),
            "date_to": d_to.isoformat(),
            "total_orders": len(orders),
            "total_skus": len(items),
            "labels": labels.get(lang, labels["ru"]),
            "totals": {
                "amount": sum(item["amount"] for item in items),
                "amount_returns": sum(item["amount_returns"] for item in items),
                "revenue": sum(item["sell_price"] for item in items),
                "commission": sum(item["commission"] for item in items),
                "profit": sum(item["seller_profit"] for item in items),
                "cost": sum(item["purchase_price"] for item in items),
                "logistics": sum(item["logistics_fee"] for item in items),
            },
            "items": items,
        })


@finance_bp.get("/api/finance/sync-status")
@login_required
def api_finance_sync_status():
    uid = int(current_user.get_id())
    allowed = _user_shop_ids(uid)
    with SessionLocal() as db:
        shops = db.execute(select(Shop).where(Shop.id.in_(allowed))).scalars().all() if allowed else []
        result = {}
        for shop in shops:
            last = db.execute(
                select(FinanceSyncLog)
                .where(FinanceSyncLog.shop_id == shop.uzum_id)
                .order_by(desc(FinanceSyncLog.synced_at))
                .limit(1)
            ).scalar_one_or_none()
            total = db.execute(
                select(func.count(FinanceOrder.id))
                .where(FinanceOrder.shop_id == shop.uzum_id)
            ).scalar() or 0
            result[shop.uzum_id] = {
                "name": shop.name or shop.uzum_id,
                "last_sync": last.synced_at.strftime("%Y-%m-%d %H:%M") if last else None,
                "last_type": last.sync_type if last else None,
                "records_fetched": last.records_fetched if last else 0,
                "total_records": total,
            }
    return _json_response(result)


@finance_bp.get("/api/admin/finance/sync-dashboard")
@login_required
def api_finance_sync_dashboard():
    if not _current_user_is_admin():
        return _json_response({"error": "Admin only"}, 403)

    with _app._finance_active_lock:
        active = sorted(_app._finance_active_shops)

    with _app._finance_stats_lock:
        stats = dict(_app._finance_stats)
    hourly_burst = _app._hourly_burst_stats_snapshot()

    queue_size = 0
    onboard_queue_size = 0
    try:
        queue_size = _app._finance_executor._work_queue.qsize()
        onboard_queue_size = _app._onboard_executor._work_queue.qsize()
    except Exception:
        pass

    with _app._finance_queue_lock:
        pending_queue_size = len(_app._finance_pending_shop_ids)

    active_manual_jobs = [
        {key: value for key, value in job.items() if key != "created_at"}
        for job in _app._list_sync_jobs(statuses=("queued", "running"))
    ]

    with SessionLocal() as db:
        total_shops = db.execute(
            select(func.count(func.distinct(Shop.uzum_id))).where(Shop.uzum_id.isnot(None))
        ).scalar() or 0
        one_hour_ago = datetime.utcnow() - timedelta(hours=1)
        recent_syncs = db.execute(
            select(
                FinanceSyncLog.sync_type,
                func.count(FinanceSyncLog.id),
                func.sum(FinanceSyncLog.records_fetched),
            )
            .where(FinanceSyncLog.synced_at >= one_hour_ago)
            .group_by(FinanceSyncLog.sync_type)
        ).all()
        synced_shops = db.execute(
            select(func.count(func.distinct(FinanceSyncLog.shop_id)))
        ).scalar() or 0
        stragglers = db.execute(
            select(
                FinanceSyncLog.shop_id,
                func.max(FinanceSyncLog.synced_at).label("last_sync"),
            )
            .group_by(FinanceSyncLog.shop_id)
            .order_by(func.max(FinanceSyncLog.synced_at).asc())
            .limit(20)
        ).all()

    return _json_response({
        "total_shops": total_shops,
        "synced_shops": synced_shops,
        "never_synced": total_shops - synced_shops,
        "currently_syncing": active,
        "currently_syncing_count": len(active),
        "executor_queue_size": queue_size + pending_queue_size,
        "pending_queue_size": pending_queue_size,
        "executor_workers": _app.FINANCE_AUTO_REFRESH_WORKERS,
        "onboard_queue_size": onboard_queue_size,
        "onboard_max_workers": _app.ONBOARD_MAX_CONCURRENT_SYNCS,
        "active_manual_jobs": active_manual_jobs,
        "stats": stats,
        "hourly_burst": hourly_burst,
        "last_hour": [
            {"type": sync_type, "count": count, "records": records or 0}
            for sync_type, count, records in recent_syncs
        ],
        "stragglers": [
            {"shop_id": shop_id, "last_sync": last_sync.isoformat() if last_sync else None}
            for shop_id, last_sync in stragglers
        ],
    })


@finance_bp.post("/api/finance/export")
@login_required
def api_finance_export():
    if not openpyxl:
        return "openpyxl not installed", 500

    raw = request.form.get("data")
    if not raw:
        return "No data", 400

    data = json.loads(raw)
    items = data.get("items", [])
    totals = data.get("totals", {})
    labels = data.get("labels", {})

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = labels.get("total", "Finance")

    headers = [
        labels.get("sku", "SKU"),
        labels.get("product", "Product"),
        labels.get("amount", "Amount"),
        labels.get("returns", "Returns"),
        labels.get("revenue", "Revenue"),
        labels.get("commission", "Commission"),
        labels.get("logistics", "Logistics"),
        labels.get("profit", "Profit"),
        labels.get("cost", "Cost"),
    ]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)

    for item in items:
        ws.append([
            item.get("sku", ""),
            item.get("product_title", ""),
            item.get("amount", 0),
            item.get("amount_returns", 0),
            item.get("sell_price", 0),
            item.get("commission", 0),
            item.get("logistics_fee", 0),
            item.get("seller_profit", 0),
            item.get("purchase_price", 0),
        ])

    ws.append([])
    ws.append([
        labels.get("total", "TOTAL"), "",
        totals.get("amount", 0),
        totals.get("amount_returns", 0),
        totals.get("revenue", 0),
        totals.get("commission", 0),
        totals.get("logistics", 0),
        totals.get("profit", 0),
        totals.get("cost", 0),
    ])
    for cell in ws[ws.max_row]:
        cell.font = Font(bold=True)

    for col in ws.columns:
        max_len = 0
        col_letter = col[0].column_letter
        for cell in col:
            try:
                max_len = max(max_len, len(str(cell.value or "")))
            except Exception:
                pass
        ws.column_dimensions[col_letter].width = max(max_len + 2, 12)

    out = io.BytesIO()
    wb.save(out)
    out.seek(0)

    filename = f"finance_{date.today().isoformat()}.xlsx"
    return send_file(
        out,
        download_name=filename,
        as_attachment=True,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
