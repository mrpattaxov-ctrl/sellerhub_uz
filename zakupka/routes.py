"""План закупки — sahifa + API.

Bitta o'qish endpoint'i. Hech narsa YARATMAYDI va hech narsani o'zgartirmaydi —
sof tavsiya (nima sotib olish kerak). Uzum'ga yangi so'rov turi qo'shilmaydi:
hisob `postavki.restock_plan.build_plan()` ustida quriladi.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from flask import Blueprint, jsonify, render_template, request, session
from flask_login import current_user, login_required
from sqlalchemy import select

from extensions import SessionLocal
from models import Shop
from core.auth_helpers import _user_shop_ids
from postavki import restock_plan
from zakupka import order_plan

zakupka_bp = Blueprint("zakupka_bp", __name__)


def _user_shops() -> list[dict]:
    uid = int(current_user.get_id())
    allowed = _user_shop_ids(uid)
    if not allowed:
        return []
    with SessionLocal() as db:
        rows = db.execute(
            select(Shop.uzum_id, Shop.name)
            .where(Shop.id.in_(allowed))
            .order_by(Shop.name.is_(None), Shop.name, Shop.uzum_id)
        ).all()
    return [{"uzum_id": r.uzum_id, "name": r.name} for r in rows]


@zakupka_bp.get("/zakupka")
@login_required
def zakupka_page():
    lang = session.get("lang", "uz")
    shops = _user_shops()
    return render_template(
        "zakupka.html",
        title="Xarid rejasi" if lang == "uz" else "План закупки",
        shops=shops,
    )


@zakupka_bp.get("/zakupka/api/plan")
@login_required
def zakupka_plan_api():
    """?shop=<uzum_id>|all&days=30|&date_from=&date_to=

    «all» — foydalanuvchining BARCHA do'konlari bitta jadvalda: Xitoydan
    xarid bitta bo'lib keladi, do'konlarga keyin bo'linadi.
    """
    shops = _user_shops()
    if not shops:
        return jsonify({"error": "Do'kon topilmadi"}), 403

    want = (request.args.get("shop") or "all").strip()
    if want and want != "all":
        shops = [s for s in shops if s["uzum_id"] == want]
        if not shops:
            return jsonify({"error": "Do'kon topilmadi yoki ruxsat yo'q"}), 403

    days = restock_plan.resolve_days(request.args.get("days"))
    date_from = (request.args.get("date_from") or "").strip() or None
    date_to = (request.args.get("date_to") or "").strip() or None

    def one(s):
        return order_plan.build_order_plan(
            s["uzum_id"], shop_name=s["name"] or s["uzum_id"],
            days=days, date_from=date_from, date_to=date_to,
        )

    # Do'konlar PARALLEL — har biri ~1.5-5s Uzum sku-list'iga ketadi, ketma-ket
    # qilsak «Все магазины» ochilishi do'kon soniga ko'paytiriladi.
    plans, errors = [], []
    with ThreadPoolExecutor(max_workers=min(4, len(shops))) as ex:
        for s, fut in [(s, ex.submit(one, s)) for s in shops]:
            try:
                plans.append(fut.result())
            except Exception as e:
                # Bitta do'konning tokeni o'lgan bo'lsa — QOLGANLARI baribir
                # ko'rsatiladi, lekin jami summa to'liq emasligini aytamiz.
                errors.append({"shop": s["name"] or s["uzum_id"], "error": str(e)})

    if not plans:
        return jsonify({"error": (errors[0]["error"] if errors else "Reja tuzilmadi"),
                        "errors": errors}), 502

    out = order_plan.merge_plans(plans)
    out["errors"] = errors
    out["multi"] = len(shops) > 1
    return jsonify(out)
