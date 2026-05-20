"""Finance-related routes extracted from app.py as a Flask Blueprint."""
from __future__ import annotations

import io
import json
from datetime import date

from flask import Blueprint, render_template, request, send_file
from flask_login import current_user, login_required
from sqlalchemy import func, select

from core.auth_helpers import _current_user_is_admin, _json_response, _user_shop_ids
from core.http_client import http_json
from core.sales_reads import day_bounds_tashkent
from core.time_helpers import _today_app_tz
from extensions import SessionLocal
from models import ProductGroup, SalesLine, Shop, Variant

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
        shops = (
            db.execute(
                select(Shop).where(Shop.id.in_(allowed)).order_by(Shop.uzum_id)
            ).scalars().all()
            if allowed
            else []
        )
    return render_template(
        "finance_sync.html",
        shops=shops,
        is_admin=_current_user_is_admin(),
    )


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

        # target_shops still holds Shop.uzum_id strings; SalesLine.shop_id is int.
        target_shop_ints: list[int] = []
        for sid in target_shops:
            try:
                target_shop_ints.append(int(sid))
            except (TypeError, ValueError):
                continue
        if not target_shop_ints:
            return _json_response({"items": [], "totals": {}})

        # Tashkent window [d_from 00:00, d_to+1 00:00). sales_lines has
        # per-order rows so a strict right-open window is the correct shape.
        start_ts, _ = day_bounds_tashkent(d_from)
        _, end_ts = day_bounds_tashkent(d_to)

        where_clauses = [
            SalesLine.shop_id.in_(target_shop_ints),
            SalesLine.created_at >= start_ts,
            SalesLine.created_at < end_ts,
        ]
        if sku_filter:
            where_clauses.append(SalesLine.sku_title.contains(sku_filter))

        # Server-side GROUP BY — single query. `sales_lines` carries no
        # product_title_ru / product_title / image_url / characteristics
        # columns; those are resolved via a cheap Variant lookup below.
        stmt = (
            select(
                SalesLine.sku_title.label("sku_title"),
                func.max(SalesLine.product_id).label("product_id"),
                func.max(SalesLine.shop_id).label("shop_id"),
                func.max(SalesLine.sku_id).label("sku_id"),
                func.coalesce(func.sum(SalesLine.qty), 0).label("amount"),
                func.coalesce(func.sum(SalesLine.qty_returns), 0).label("amount_returns"),
                func.coalesce(func.sum(SalesLine.revenue), 0).label("sell_price"),
                func.coalesce(func.sum(SalesLine.commission), 0).label("commission"),
                func.coalesce(func.sum(SalesLine.seller_profit), 0).label("seller_profit"),
                func.coalesce(func.sum(SalesLine.purchase_price), 0).label("purchase_price"),
                func.coalesce(func.sum(SalesLine.logistics_fee), 0).label("logistics_fee"),
                func.coalesce(func.sum(SalesLine.promo_amount), 0).label("seller_discount"),
                func.count().label("row_count"),
            )
            .where(*where_clauses)
            .group_by(SalesLine.sku_title)
            .order_by(func.sum(SalesLine.qty).desc())
        )

        rows = db.execute(stmt).all()
        total_row_count = sum(int(r.row_count or 0) for r in rows)

        # Enrichment: resolve product_title_ru / product_title / image_url /
        # characteristics via Variant lookup. Cheap — one IN-query per page.
        sku_titles = [r.sku_title for r in rows if r.sku_title]
        variant_meta: dict[str, dict] = {}
        if sku_titles:
            var_rows = db.execute(
                select(
                    Variant.sku,
                    Variant.image_url,
                    Variant.color,
                    Variant.size,
                    ProductGroup.name,
                )
                .join(ProductGroup, Variant.group_id == ProductGroup.id)
                .where(Variant.sku.in_(sku_titles))
            ).all()
            for vr in var_rows:
                bits: list[str] = []
                if vr.color:
                    bits.append(str(vr.color))
                if vr.size:
                    bits.append(str(vr.size))
                variant_meta[vr.sku] = {
                    "product_title": vr.name or "",
                    "image_url": vr.image_url or "",
                    "characteristics": ", ".join(bits),
                }

        items = []
        for r in rows:
            meta = variant_meta.get(r.sku_title, {})
            product_title = meta.get("product_title", "")
            # sales_lines doesn't split ru/uz titles — use Variant group name
            # for both languages (the finance page shows whichever the user
            # chose; SKU + characteristics carry the variant info).
            items.append({
                "sku": r.sku_title,
                "product_title": product_title,
                "product_id": r.product_id,
                "image_url": meta.get("image_url", ""),
                "shop_id": str(r.shop_id) if r.shop_id is not None else "",
                "characteristics": meta.get("characteristics", ""),
                "amount": int(r.amount or 0),
                "amount_returns": int(r.amount_returns or 0),
                "sell_price": float(r.sell_price or 0),
                "commission": float(r.commission or 0),
                "seller_profit": float(r.seller_profit or 0),
                "purchase_price": float(r.purchase_price or 0),
                "logistics_fee": float(r.logistics_fee or 0),
                "seller_discount": float(r.seller_discount or 0),
            })
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
            "total_orders": total_row_count,
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
