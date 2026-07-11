"""Admin «Авто-слот» panel route'lari — IZOLYATSIYALANGAN blueprint.

Faqat SellerHub operatori (`current_user.is_admin`). Vazifasi:
  - GET  /admin/autoslot            → panel sahifasi
  - GET  /admin/autoslot/api/list   → GLOBAL navbat (barcha do'kon/user) JSON
  - POST /admin/autoslot/api/priority → reja prioritetini o'rnatish

Prioritet DVIGATELI `postavki.autoslot.store` da (candidates_for navbat
tartibi priority↓, enabled_at↑). Bu qatlam faqat KO'RSATISH + `set_priority`
chaqiruvi — boshqa modullarga tegmaydi.
"""
from __future__ import annotations

from flask import (Blueprint, jsonify, redirect, render_template, request,
                   url_for)
from flask_login import current_user, login_required
from sqlalchemy import select

from extensions import SessionLocal
from models import Shop, User
from postavki.autoslot import store
from postavki import autoslot_client

admin_autoslot_bp = Blueprint("admin_autoslot_bp", __name__)


def _is_admin() -> bool:
    """Faqat SellerHub operatori. Global navbatда adolat uchun sellerlar
    o'zi prioritet o'zgartira olmaydi."""
    return bool(getattr(current_user, "is_admin", False))


@admin_autoslot_bp.get("/admin/autoslot")
@login_required
def autoslot_page():
    if not _is_admin():
        return redirect(url_for("products_bp.groups_page"))
    return render_template("admin_autoslot.html")


@admin_autoslot_bp.get("/admin/autoslot/api/list")
@login_required
def autoslot_list():
    """GLOBAL navbat — barcha do'kon/user rejalari, navbat tartibida.
    ?status= (waiting/booking/booked/failed/canceled) ixtiyoriy filtr."""
    if not _is_admin():
        return jsonify({"error": "Admin only"}), 403
    status = (request.args.get("status") or "").strip() or None
    # AUTOSLOT_URL qo'yilgan → navbat autoslot servisidan; enrichment (nom/ega)
    # baribir SellerHub'da (Shop/User jadvallari shu yerда).
    if autoslot_client.enabled():
        try:
            plans = autoslot_client.admin_list(status)
        except Exception as e:
            return jsonify({"error": f"autoslot servisi: {e}"}), 502
    else:
        plans = store.list_for_admin(status=status)
    # Do'kon nomi + egasi (username) — bitta so'rov bilan mapping.
    shop_ids = {p["shop_uzum_id"] for p in plans if p.get("shop_uzum_id")}
    user_ids = {p["user_id"] for p in plans if p.get("user_id")}
    shops, users = {}, {}
    with SessionLocal() as db:
        if shop_ids:
            for s in db.execute(
                    select(Shop).where(Shop.uzum_id.in_(shop_ids))).scalars():
                shops[s.uzum_id] = s.name or s.uzum_id
        if user_ids:
            for u in db.execute(
                    select(User).where(User.id.in_(user_ids))).scalars():
                users[u.id] = u.username
    for p in plans:
        p["shop_name"] = shops.get(p.get("shop_uzum_id"), p.get("shop_uzum_id"))
        p["owner"] = users.get(p.get("user_id"))
    return jsonify({"plans": plans})


@admin_autoslot_bp.post("/admin/autoslot/api/priority")
@login_required
def autoslot_set_priority():
    """Reja prioritetini o'rnatadi (yuqori = navbatда oldin). Faqat admin."""
    if not _is_admin():
        return jsonify({"error": "Admin only"}), 403
    data = request.get_json(silent=True) or {}
    try:
        plan_id = int(data.get("planId"))
        priority = int(data.get("priority"))
    except (TypeError, ValueError):
        return jsonify({"error": "planId/priority butun son bo'lsin"}), 400
    if not (0 <= priority <= 1_000_000):
        return jsonify({"error": "priority 0..1000000 oralig'ida"}), 400
    if autoslot_client.enabled():
        try:
            val = autoslot_client.set_priority(plan_id, priority)
        except Exception as e:
            return jsonify({"error": f"autoslot servisi: {e}"}), 502
    else:
        val = store.set_priority(plan_id, priority)
    if val is None:
        return jsonify({"error": "Reja topilmadi"}), 404
    return jsonify({"ok": True, "planId": plan_id, "priority": val})
