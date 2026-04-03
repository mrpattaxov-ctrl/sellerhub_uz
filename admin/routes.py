"""Admin routes extracted from app.py as a Flask Blueprint."""
from __future__ import annotations

from flask import Blueprint, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import select, delete
from werkzeug.security import generate_password_hash

from extensions import SessionLocal
from models import ProductGroup, Shop, User, Variant, VariantSale
from core.auth_helpers import (
    _current_user_is_admin,
    _json_response,
    _user_shop_ids,
)

admin_bp = Blueprint("admin_bp", __name__)


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
            if name:
                existing.name = name
            if is_admin and payload.get("owner_id") is not None:
                existing.owner_id = resolved_owner
            elif not is_admin and existing.owner_id is None:
                existing.owner_id = uid
            db.commit()
            return _json_response({"ok": True, "id": existing.id})

        s = Shop(uzum_id=uzum_id, name=name or f"Shop {uzum_id}", owner_id=resolved_owner)
        db.add(s)
        db.commit()
        return _json_response({"ok": True, "id": s.id})

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
        shop.owner_id = int(owner_id) if owner_id else None
        db.commit()
    return _json_response({"ok": True})

@admin_bp.delete("/api/shops/<int:shop_id>")
@login_required
def delete_shop(shop_id: int):
    """Delete a shop. Users can only delete their own shops; admin can delete any unowned shop."""
    uid = int(current_user.get_id())
    with SessionLocal() as db:
        shop = db.get(Shop, shop_id)
        if not shop:
            return _json_response({"error": "Shop not found"}, 404)
        # Permission: must own the shop (or be admin)
        if not _current_user_is_admin() and shop.owner_id != uid:
            return _json_response({"error": "Access denied"}, 403)
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
        db.delete(shop)
        db.commit()
    return _json_response({"ok": True})


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
