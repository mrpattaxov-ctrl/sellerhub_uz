"""«Новый товар» — Flask blueprint: sahifa + proxy JSON API'lar.

Brauzer api-seller.uzum.uz'ni to'g'ridan-to'g'ri chaqira olmaydi (CORS) —
hamma portal chaqiruvi shu proxy'lar orqali o'tadi (postavki naqshi).
Har API do'kon-egalik guard'i bilan himoyalangan (_can_access).
"""
from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request, session
from flask_login import current_user, login_required
from sqlalchemy import select

from core.auth_helpers import _user_shop_ids
from extensions import SessionLocal
from models import Shop
from noviy_tavar import client
from noviy_tavar.client import NoviyTavarError

noviy_tavar_bp = Blueprint(
    "noviy_tavar_bp", __name__, template_folder="templates"
)


# ── Do'kon-egalik yordamchilari (postavki bilan bir xil naqsh) ────────


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


def _can_access(shop_uzum_id: str) -> bool:
    uid = int(current_user.get_id())
    allowed = _user_shop_ids(uid)
    if not allowed:
        return False
    with SessionLocal() as db:
        shop = db.execute(
            select(Shop).where(Shop.uzum_id == str(shop_uzum_id))
        ).scalar_one_or_none()
    return bool(shop and shop.id in allowed)


def _shop_or_403():
    """`shop` query/body parametrini o'qib egalikni tekshiradi.

    Returns (shop_id, None) yoki (None, flask javobi).
    """
    shop = str(request.args.get("shop") or "").strip()
    if not shop:
        return None, (jsonify({"error": "shop kerak"}), 400)
    if not _can_access(shop):
        return None, (jsonify({"error": "Do'kon topilmadi yoki ruxsat yo'q"}), 403)
    return shop, None


def _portal_error(e: NoviyTavarError):
    """Portal xatosini brauzerga xavfsiz shaklda uzatish."""
    return jsonify({
        "error": f"Uzum portal xatosi (HTTP {e.http_status})",
        "detail": e.body[:300],
    }), 502


# ── Sahifa ───────────────────────────────────────────────────────────


@noviy_tavar_bp.get("/noviy-tavar")
@login_required
def noviy_tavar_page():
    lang = session.get("lang", "uz")
    shops = _user_shops()
    return render_template(
        "noviy_tavar.html",
        title="Yangi tovar" if lang == "uz" else "Новый товар",
        shops=shops,
    )


# ── Proxy API'lar ────────────────────────────────────────────────────


@noviy_tavar_bp.get("/noviy-tavar/api/categories")
@login_required
def nt_categories():
    """Ildiz yoki bola kategoriyalar (parentId bo'lsa — bolalar)."""
    shop, err = _shop_or_403()
    if err:
        return err
    parent = (request.args.get("parentId") or "").strip()
    try:
        if parent:
            data = client.child_categories(shop, int(parent))
        else:
            data = client.root_categories(shop)
        return jsonify({"categories": data if isinstance(data, list) else []})
    except NoviyTavarError as e:
        return _portal_error(e)
    except ValueError:
        return jsonify({"error": "parentId raqam bo'lishi kerak"}), 400


@noviy_tavar_bp.get("/noviy-tavar/api/category-meta")
@login_required
def nt_category_meta():
    """Kategoriya qoidalari: komissiya, sxemalar, xususiyatlar, filtrlar..."""
    shop, err = _shop_or_403()
    if err:
        return err
    try:
        cid = int(request.args.get("categoryId") or "")
    except ValueError:
        return jsonify({"error": "categoryId raqam bo'lishi kerak"}), 400
    try:
        return jsonify(client.category_meta(shop, cid))
    except NoviyTavarError as e:
        return _portal_error(e)


@noviy_tavar_bp.get("/noviy-tavar/api/filter-values")
@login_required
def nt_filter_values():
    """Brend/Model/Davlat qiymatlari — qidiruvli, sahifalangan."""
    shop, err = _shop_or_403()
    if err:
        return err
    try:
        fid = int(request.args.get("filterId") or "")
        cid = int(request.args.get("categoryId") or "")
        page = int(request.args.get("page") or 0)
    except ValueError:
        return jsonify({"error": "filterId/categoryId/page raqam bo'lishi kerak"}), 400
    search = (request.args.get("search") or "").strip()
    try:
        data = client.filter_values(shop, fid, cid, search=search, page=page)
        return jsonify({"values": data})
    except NoviyTavarError as e:
        return _portal_error(e)


@noviy_tavar_bp.post("/noviy-tavar/api/check-words")
@login_required
def nt_check_words():
    """Taqiqlangan so'z tekshiruvi — [] = toza."""
    body = request.get_json(silent=True) or {}
    shop = str(body.get("shop") or "").strip()
    if not shop:
        return jsonify({"error": "shop kerak"}), 400
    if not _can_access(shop):
        return jsonify({"error": "Do'kon topilmadi yoki ruxsat yo'q"}), 403
    text = str(body.get("text") or "")
    if not text.strip():
        return jsonify({"words": []})
    try:
        return jsonify({"words": client.check_words(shop, text)})
    except NoviyTavarError as e:
        return _portal_error(e)


@noviy_tavar_bp.post("/noviy-tavar/api/upload-image")
@login_required
def nt_upload_image():
    """Rasm proxy — brauzer → Flask → images-uploader.uzum.uz."""
    shop = (request.form.get("shop") or "").strip()
    if not shop:
        return jsonify({"error": "shop kerak"}), 400
    if not _can_access(shop):
        return jsonify({"error": "Do'kon topilmadi yoki ruxsat yo'q"}), 403
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "Rasm fayli kerak"}), 400
    data = f.read()
    if len(data) > client.MAX_IMAGE_BYTES:
        return jsonify({"error": "Rasm 15MB dan katta"}), 413
    try:
        res = client.upload_image(data, f.filename, f.mimetype or "image/jpeg")
        payload = res.get("payload") if isinstance(res, dict) else None
        if not isinstance(payload, dict) or not payload.get("key"):
            return jsonify({"error": "Uzum kutilmagan javob qaytardi",
                            "detail": str(res)[:300]}), 502
        return jsonify({
            "key": payload.get("key"),
            "url": payload.get("originalUrl"),
            "recommendations": [
                r.get("text") for r in (payload.get("recommendations") or [])
                if isinstance(r, dict) and r.get("text")
            ],
        })
    except NoviyTavarError as e:
        return _portal_error(e)


@noviy_tavar_bp.post("/noviy-tavar/api/create")
@login_required
def nt_create():
    """QORALAMA yaratish — createProduct (moderatsiyaga O'ZI KETMAYDI)."""
    body = request.get_json(silent=True) or {}
    shop = str(body.get("shop") or "").strip()
    if not shop:
        return jsonify({"error": "shop kerak"}), 400
    if not _can_access(shop):
        return jsonify({"error": "Do'kon topilmadi yoki ruxsat yo'q"}), 403

    # Server-side minimal validatsiya (brauzer validatsiyasiga ishonmaymiz).
    try:
        cid = int(body.get("categoryId") or 0)
    except (TypeError, ValueError):
        cid = 0
    title_uz = str(body.get("titleUz") or "").strip()
    title_ru = str(body.get("titleRu") or "").strip()
    images = body.get("images") or []
    if not cid:
        return jsonify({"error": "Kategoriya tanlanmagan"}), 400
    if not (title_uz or title_ru):
        return jsonify({"error": "Nomi kiritilmagan"}), 400
    if not isinstance(images, list) or not any(
            isinstance(im, dict) and im.get("key") for im in images):
        return jsonify({"error": "Kamida bitta rasm yuklang"}), 400
    # Bir til bo'sh bo'lsa ikkinchisidan nusxa (portal ikkalasini kutadi).
    title_uz = title_uz or title_ru
    title_ru = title_ru or title_uz
    desc_uz = str(body.get("descUz") or "").strip()
    desc_ru = str(body.get("descRu") or "").strip()
    desc_uz = desc_uz or desc_ru
    desc_ru = desc_ru or desc_uz
    short_uz = str(body.get("shortUz") or "").strip()
    short_ru = str(body.get("shortRu") or "").strip()
    short_uz = short_uz or short_ru
    short_ru = short_ru or short_uz

    create_body = client.build_create_body(
        category_id=cid,
        title_uz=title_uz, title_ru=title_ru,
        short_uz=short_uz, short_ru=short_ru,
        desc_uz=desc_uz, desc_ru=desc_ru,
        filter_values_sel=body.get("filterValues") or [],
        characteristics_sel=body.get("characteristics") or [],
        images=images,
        product_fields=body.get("productFields") or {},
    )
    try:
        res = client.create_product(shop, create_body)
    except NoviyTavarError as e:
        return _portal_error(e)

    pid = res.get("id") if isinstance(res, dict) else None
    print(f"[NoviyTavar] CREATED draft product id={pid} shop={shop} title={title_uz!r}")
    return jsonify({
        "id": pid,
        "title": res.get("title") if isinstance(res, dict) else None,
        "skuTitlePrefix": res.get("shopSkuTitle") if isinstance(res, dict) else None,
    }), 201


@noviy_tavar_bp.get("/noviy-tavar/api/check-sku")
@login_required
def nt_check_sku():
    """SKU artikul bandligini tekshirish (kelajak SKU bosqichi uchun)."""
    shop, err = _shop_or_403()
    if err:
        return err
    sku = (request.args.get("sku") or "").strip()
    if not sku:
        return jsonify({"error": "sku kerak"}), 400
    try:
        return jsonify(client.check_sku(shop, sku))
    except NoviyTavarError as e:
        return _portal_error(e)
