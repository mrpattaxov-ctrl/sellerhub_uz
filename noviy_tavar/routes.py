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
    "noviy_tavar_bp",
    __name__,
    template_folder="templates",
    # Feature-local CSS/JS — global static/ ga TEGILMAYDI, sizib chiqmaydi.
    static_folder="static",
    static_url_path="/noviy-tavar/static",
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



def _lang() -> str:
    """Sahifa tili — portal O'QISH chaqiruvlariga uzatiladi.

    Kategoriya `title` tekis string bo'lib, portal uni Accept-Language bo'yicha
    tarjima qiladi. Bu bo'lmasa RU sahifada ham «Hayvonlar» chiqadi (Uzum'da
    «Животные»). YOZISH (create) chaqiruvlari default uz-UZ'da qoladi.
    """
    return session.get("lang", "uz")

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
            data = client.child_categories(shop, int(parent), lang=_lang())
        else:
            data = client.root_categories(shop, lang=_lang())
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
        return jsonify(client.category_meta(shop, cid, lang=_lang()))
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
        data = client.filter_values(shop, fid, cid, search=search, page=page, lang=_lang())
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


@noviy_tavar_bp.post("/noviy-tavar/api/upload-video")
@login_required
def nt_upload_video():
    """Video proxy — brauzer → Flask → api-seller.uzum.uz.

    ⚠️ Rasmdan BOSHQA host va BOSHQA auth (Bearer, LOUIS_KEY emas) —
    client.upload_video() izohiga qarang. Token brauzerga chiqmaydi.
    """
    shop = (request.form.get("shop") or "").strip()
    if not shop:
        return jsonify({"error": "shop kerak"}), 400
    if not _can_access(shop):
        return jsonify({"error": "Do'kon topilmadi yoki ruxsat yo'q"}), 403
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "Video fayli kerak"}), 400
    data = f.read()
    if len(data) > client.MAX_VIDEO_BYTES:
        return jsonify({"error": "Video 10MB dan katta"}), 413
    try:
        res = client.upload_video(data, f.filename, f.mimetype or "video/mp4")
        # client allaqachon {key, originalUrl} ga normallashtirgan
        # (Uzum `original_url` snake_case beradi).
        return jsonify({"key": res["key"], "url": res["originalUrl"]})
    except NoviyTavarError as e:
        return _portal_error(e)


@noviy_tavar_bp.post("/noviy-tavar/api/upload-collection")
@login_required
def nt_upload_collection():
    """360-arxiv proxy — brauzer → Flask → images-uploader.uzum.uz.

    ASINXRON: faqat ``collectionId`` qaytadi; kadrlar uchun brauzer
    /collection-status ni poll qiladi (bandl: 5000ms).
    """
    shop = (request.form.get("shop") or "").strip()
    if not shop:
        return jsonify({"error": "shop kerak"}), 400
    if not _can_access(shop):
        return jsonify({"error": "Do'kon topilmadi yoki ruxsat yo'q"}), 403
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "Arxiv fayli kerak"}), 400
    data = f.read()
    if len(data) > client.MAX_COLLECTION_BYTES:
        return jsonify({"error": "Arxiv 10MB dan katta"}), 413
    try:
        return jsonify({"collectionId": client.upload_collection(data, f.filename)})
    except NoviyTavarError as e:
        return _portal_error(e)


@noviy_tavar_bp.get("/noviy-tavar/api/collection-status")
@login_required
def nt_collection_status():
    """360-arxiv holati — brauzer buni tayyor bo'lguncha poll qiladi."""
    shop = (request.args.get("shop") or "").strip()
    if not shop:
        return jsonify({"error": "shop kerak"}), 400
    if not _can_access(shop):
        return jsonify({"error": "Do'kon topilmadi yoki ruxsat yo'q"}), 403
    try:
        cid = int(request.args.get("id") or 0)
    except (TypeError, ValueError):
        cid = 0
    if cid <= 0:
        return jsonify({"error": "id kerak"}), 400
    try:
        col = client.collection_status(cid)
    except NoviyTavarError as e:
        return _portal_error(e)
    return jsonify({
        "status": col.get("process_job_status") or "",
        "uploaded": col.get("uploaded") or 0,
        "total": col.get("total") or 0,
        "images": [
            {"key": im.get("key"), "url": im.get("url")}
            for im in (col.get("images") or [])
            if isinstance(im, dict) and im.get("key")
        ],
    })


# ── 2-QADAM «Цены и SKU» proxy'lari ──────────────────────────────────
#
# ⚠️ Do'kon-egalik guard'i SHU YERDA hal qiluvchi: `productId` brauzerdan
# keladi. Guard'siz foydalanuvchi boshqa do'konning mahsulot id'sini yuborib,
# uning kartasini o'qiy olardi. Uzum ham do'kon bo'yicha chegaralaydi, lekin
# biz unga TAYANMAYMIZ — o'z chegaramizni qo'yamiz.


@noviy_tavar_bp.get("/noviy-tavar/api/sku-step")
@login_required
def nt_sku_step():
    """2-qadam ma'lumoti — description-response.

    ⚠️ `/api/product` (get_product) BU EMAS: unda `shopSkuTitle` YO'Q.
    Jonli o'lchangan (qoralama 3068623): shopSkuTitle="BLUMMY",
    productSkuTitle="", skuList Uzum tomonidan rang boshiga OLDINDAN qurilgan.
    """
    shop, err = _shop_or_403()
    if err:
        return err
    try:
        pid = int(request.args.get("productId") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid <= 0:
        return jsonify({"error": "productId kerak"}), 400
    try:
        res = client.product_description_response(shop, pid, lang=_lang())
    except NoviyTavarError as e:
        return _portal_error(e)
    return jsonify({
        "id": res.get("id"),
        "title": res.get("title") or {},
        # Do'kon prefiksi — Uzum O'ZI beradi, biz sozlamaymiz.
        "shopSkuTitle": res.get("shopSkuTitle") or "",
        "productSkuTitle": res.get("productSkuTitle") or "",
        "commission": res.get("commission"),
        "commissionDto": res.get("commissionDto") or {},
        "definedCharacteristicList": res.get("definedCharacteristicList") or [],
        "customCharacteristicList": res.get("customCharacteristicList") or [],
        "skuList": res.get("skuList") or [],
    })


@noviy_tavar_bp.get("/noviy-tavar/api/product")
@login_required
def nt_get_product():
    """Mahsulot kartasi (o'qish) — holat/skuList tekshirish uchun."""
    shop, err = _shop_or_403()
    if err:
        return err
    try:
        pid = int(request.args.get("productId") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid <= 0:
        return jsonify({"error": "productId kerak"}), 400
    try:
        res = client.get_product(shop, pid, lang=_lang())
    except NoviyTavarError as e:
        return _portal_error(e)
    return jsonify({
        "id": pid,
        "status": res.get("status") or {},
        "skuList": res.get("skuList") or [],
        "category": res.get("category") or {},
    })


@noviy_tavar_bp.get("/noviy-tavar/api/ikpu-search")
@login_required
def nt_ikpu_search():
    """IKPU (soliq kodi) qidiruvi — 2-qadamdagi majburiy maydon."""
    shop, err = _shop_or_403()
    if err:
        return err
    try:
        cid = int(request.args.get("categoryId") or 0)
    except (TypeError, ValueError):
        cid = 0
    if cid <= 0:
        return jsonify({"error": "categoryId kerak"}), 400
    q = (request.args.get("q") or "").strip()
    try:
        page = max(0, int(request.args.get("page") or 0))
    except (TypeError, ValueError):
        page = 0
    try:
        rows = client.ikpu_search(cid, q, page=page, lang=_lang())
    except NoviyTavarError as e:
        return _portal_error(e)
    return jsonify({"items": [
        {
            "ikpu": r.get("ikpu"),
            "name": r.get("ikpuName"),
            "position": r.get("positionName"),
            # Uzum aytadi: bu kod shu kategoriyaga mos keladimi. KO'RSATAMIZ —
            # aks holda foydalanuvchi noto'g'ri kod tanlab, moderatsiyada qoladi.
            "validForCategory": r.get("isValidForCategory"),
            "vatRelief": r.get("isVatRelief"),
        }
        for r in rows if isinstance(r, dict) and r.get("ikpu")
    ]})


@noviy_tavar_bp.get("/noviy-tavar/api/ikpu-check")
@login_required
def nt_ikpu_check():
    """IKPU tekshiruvi → {"privileged": bool} (jonli tasdiqlangan shakl)."""
    shop, err = _shop_or_403()
    if err:
        return err
    try:
        cid = int(request.args.get("categoryId") or 0)
    except (TypeError, ValueError):
        cid = 0
    ikpu = (request.args.get("ikpu") or "").strip()
    if cid <= 0 or not ikpu:
        return jsonify({"error": "categoryId va ikpu kerak"}), 400
    try:
        res = client.ikpu_check(cid, ikpu, lang=_lang())
    except NoviyTavarError as e:
        return _portal_error(e)
    return jsonify({"privileged": bool(res.get("privileged"))})


@noviy_tavar_bp.post("/noviy-tavar/api/commission")
@login_required
def nt_commission():
    """Komissiya hisoblagichi — O'QISH (POST bo'lsa ham hech narsa yozmaydi).

    ⚠️ JONLI O'LCHANGAN (2026-07-17) — bandl da'vosi noto'liq edi:
        element = {skuId, commission, sellPrice, marketplaceCommission,
                   toWithdraw, logisticDeliveryFee}
    `recommendedTotalPrice` bu javobda YO'Q (bandl uni boshqa store'dan oladi).
    Tekshirilgan matematika (sku 718371):
        marketplaceCommission = sellPrice * commission/100   (19000*25% = 4750)
        toWithdraw = sellPrice - marketplaceCommission - logisticDeliveryFee
                                                        (19000-4750-50000 = -35750)

    ⚠️ `skuId` MAJBURIY: usiz Uzum `skuPrices: []` qaytaradi — jonli 2 marta
    tasdiqlangan (yangi qoralamada HAM, SKU'si bor mahsulotda HAM). Ya'ni
    YARATISH oqimida (NO_SKU) bu ro'yxat har doim bo'sh bo'ladi, chunki SKU
    hali yaratilmagan — Uzumning O'Z portali ham aynan shu bo'sh javobni oladi.
    Bu XATO EMAS: ustunlar SKU saqlangach (tahrirlash yo'li) to'ladi.
    """
    body = request.get_json(silent=True) or {}
    shop = str(body.get("shop") or "").strip()
    if not shop:
        return jsonify({"error": "shop kerak"}), 400
    if not _can_access(shop):
        return jsonify({"error": "Do'kon topilmadi yoki ruxsat yo'q"}), 403
    try:
        pid = int(body.get("productId") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid <= 0:
        return jsonify({"error": "productId kerak"}), 400

    prices = body.get("prices")
    if not isinstance(prices, list):
        return jsonify({"error": "prices ro'yxat bo'lishi kerak"}), 400

    clean = []
    for p in prices:
        if not isinstance(p, dict):
            continue
        try:
            new_price = int(p.get("newPrice") or 0)
        except (TypeError, ValueError):
            continue
        item = {"newPrice": new_price}
        # skuId'siz Uzum bo'sh qaytaradi — mavjud bo'lsagina qo'shamiz
        # (bandl ham `Number(e.id) || undefined` qiladi).
        if p.get("skuId"):
            try:
                item["skuId"] = int(p["skuId"])
            except (TypeError, ValueError):
                pass
        clean.append(item)

    try:
        rows = client.sku_commission(shop, pid, clean, lang=_lang())
    except NoviyTavarError as e:
        return _portal_error(e)
    return jsonify({"items": [
        {
            "skuId": r.get("skuId"),
            "commission": r.get("commission"),
            "sellPrice": r.get("sellPrice"),
            "marketplaceCommission": r.get("marketplaceCommission"),
            "logisticDeliveryFee": r.get("logisticDeliveryFee"),
            "toWithdraw": r.get("toWithdraw"),
        }
        for r in rows if isinstance(r, dict)
    ]})


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

    care_uz = str(body.get("careUz") or "").strip()
    care_ru = str(body.get("careRu") or "").strip()
    create_body = client.build_create_body(
        category_id=cid,
        title_uz=title_uz, title_ru=title_ru,
        short_uz=short_uz, short_ru=short_ru,
        desc_uz=desc_uz, desc_ru=desc_ru,
        filter_values_sel=body.get("filterValues") or [],
        characteristics_sel=body.get("characteristics") or [],
        images=images,
        product_fields=body.get("productFields") or {},
        desc_is_html=bool(body.get("descIsHtml")),
        color_images=body.get("colorImages") or [],
        color_videos=body.get("colorVideos") or [],
        color_collections=body.get("colorCollections") or [],
        video=body.get("video") or None,
        image_collection=body.get("imageCollection") or None,
        care_uz=care_uz, care_ru=care_ru,
        certificates=body.get("certificates") or [],
        # Ixtiyoriy bo'limlar (Состав/Размеры/Инструкция/Сертификация).
        # Brauzer yubormasa None -> eski care-only xatti-harakat saqlanadi.
        comments_sel=body.get("comments") if isinstance(body.get("comments"), list) else None,
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


@noviy_tavar_bp.post("/noviy-tavar/api/send-sku")
@login_required
def nt_send_sku():
    """2-qadam: SKU/narxlarni saqlash — **YOZUVCHI** (sendSkuData).

    ⚠️ Brauzer validatsiyasiga ISHONMAYMIZ — narx/IKPU/prefiks shu yerda ham
    tekshiriladi. Sabab: bu chaqiruv qoralamaga HAQIQIY SKU qo'shadi;
    noto'g'ri narx bilan ketsa Uzum'da sotuvga chiqib qolishi mumkin.
    """
    body = request.get_json(silent=True) or {}
    shop = str(body.get("shop") or "").strip()
    if not shop:
        return jsonify({"error": "shop kerak"}), 400
    if not _can_access(shop):
        return jsonify({"error": "Do'kon topilmadi yoki ruxsat yo'q"}), 403

    try:
        pid = int(body.get("productId") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid <= 0:
        return jsonify({"error": "productId kerak"}), 400

    prefix = str(body.get("skuForProduct") or "").strip()
    if not prefix:
        return jsonify({"error": "SKU prefiksi kerak"}), 400

    rows = body.get("rows")
    if not isinstance(rows, list) or not rows:
        return jsonify({"error": "Kamida bitta SKU qatori kerak"}), 400

    # Server-side qator validatsiyasi (bandl `isValid` bilan bir xil mantiq).
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            return jsonify({"error": f"{i + 1}-qator buzuq"}), 400
        if not str(r.get("skuTitle") or "").strip():
            return jsonify({"error": f"{i + 1}-qatorda SKU nomi yo'q"}), 400
        if not str(r.get("ikpu") or "").strip():
            return jsonify({"error": f"{i + 1}-qatorda IKPU yo'q"}), 400
        try:
            full = int(r.get("fullPrice") or 0)
            sell = int(r.get("sellPrice") or 0)
        except (TypeError, ValueError):
            return jsonify({"error": f"{i + 1}-qatorda narx raqam emas"}), 400
        if full <= 0 or sell <= 0:
            return jsonify({"error": f"{i + 1}-qatorda narx 0 dan katta bo'lishi kerak"}), 400
        if sell > full:
            # Chegirmali narx to'liq narxdan katta bo'lishi mantiqsiz.
            return jsonify({"error": f"{i + 1}-qatorda sotuv narxi to'liq narxdan katta"}), 400

    sku_body = client.build_sku_body(
        product_id=pid,
        sku_for_product=prefix,
        rows=rows,
        defined_characteristics=body.get("definedCharacteristicList") or [],
    )
    try:
        res = client.send_sku_data(shop, sku_body)
    except NoviyTavarError as e:
        return _portal_error(e)

    created = res.get("skuDataResponseList") if isinstance(res, dict) else None
    print(f"[NoviyTavar] SKU SAVED product={pid} shop={shop} "
          f"rows={len(rows)} created={len(created or [])}")
    return jsonify({
        "skus": [
            {"skuId": s.get("skuId"), "barcode": s.get("sellerItemBarcode")}
            for s in (created or []) if isinstance(s, dict)
        ],
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


# ── 3-QADAM «Свойства» proxy'lari ────────────────────────────────────


@noviy_tavar_bp.get("/noviy-tavar/api/step3")
@login_required
def nt_step3():
    """3-qadam jadval ma'lumoti — filters/product/{id} (O'QISH).

    `skuAttributes` (ustunlar) + `sku` (qatorlar) qaytadi. `sku` bo'sh bo'lsa
    brauzer «taqiq» ekranini ko'rsatadi (Uzumdek: «SKU kodsiz xususiyat
    qo'sha olmaysiz») — bu shu proxy'ning ishi EMAS, faqat ma'lumot uzatadi.
    """
    shop, err = _shop_or_403()
    if err:
        return err
    try:
        pid = int(request.args.get("productId") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid <= 0:
        return jsonify({"error": "productId kerak"}), 400
    try:
        res = client.product_filters(shop, pid, lang=_lang())
    except NoviyTavarError as e:
        return _portal_error(e)
    return jsonify({
        "skuAttributes": res.get("skuAttributes") or [],
        "sku": res.get("sku") or [],
        "filters": res.get("filters") or [],
        "skuFilters": res.get("skuFilters") or [],
        "categoryId": res.get("categoryId"),
    })


@noviy_tavar_bp.get("/noviy-tavar/api/attr-enums")
@login_required
def nt_attr_enums():
    """Enum atribut dropdown qiymatlari — assortment /attributes/enums (O'QISH).

    Dropdown ochilganda lazy chaqiriladi (bandl `fetchEnumValues`). `search`
    bo'lsa serverda filtrlanadi (Uzumdek qidiruv).
    """
    shop, err = _shop_or_403()
    if err:
        return err
    attr_code = (request.args.get("attrCode") or "").strip()
    if not attr_code:
        return jsonify({"error": "attrCode kerak"}), 400
    search = (request.args.get("search") or "").strip()
    try:
        page = max(0, int(request.args.get("page") or 0))
    except (TypeError, ValueError):
        page = 0
    try:
        rows = client.attr_enums(attr_code, search=search, page=page,
                                 lang=_lang())
    except NoviyTavarError as e:
        return _portal_error(e)
    return jsonify({"items": [
        {
            "code": r.get("code"),
            "value": r.get("localizedValue") or {},
            "isCustom": bool(r.get("isCustom")),
        }
        for r in rows if isinstance(r, dict) and r.get("code")
    ]})


@noviy_tavar_bp.post("/noviy-tavar/api/save-filters")
@login_required
def nt_save_filters():
    """3-qadam: xususiyatlarni saqlash — **YOZUVCHI** (filters/product POST).

    Brauzer `skuAttributeValues` ni bandl `RE()` serializatori bilan quradi
    (har `attributeValue` = {valueType, value, ...} yoki bo'sh bo'lsa null).
    Bu yerda faqat egalik + tuzilma tekshiriladi — qiymat ma'nosiga
    aralashmaymiz (Uzum o'zi validatsiya qiladi va aniq 400 beradi).
    """
    body = request.get_json(silent=True) or {}
    shop = str(body.get("shop") or "").strip()
    if not shop:
        return jsonify({"error": "shop kerak"}), 400
    if not _can_access(shop):
        return jsonify({"error": "Do'kon topilmadi yoki ruxsat yo'q"}), 403

    try:
        pid = int(body.get("productId") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid <= 0:
        return jsonify({"error": "productId kerak"}), 400

    sku_attr_values = body.get("skuAttributeValues")
    if not isinstance(sku_attr_values, list) or not sku_attr_values:
        return jsonify({"error": "Kamida bitta SKU uchun xususiyat kerak"}), 400

    portal_body = {
        # Eski filterId tizimi — odatda bo'sh (yangi tizim skuAttributeValues).
        "skuFilters": body.get("skuFilters") or [],
        "skuAttributeValues": sku_attr_values,
    }
    try:
        res = client.save_filters(shop, pid, portal_body)
    except NoviyTavarError as e:
        return _portal_error(e)

    print(f"[NoviyTavar] FILTERS SAVED product={pid} shop={shop} "
          f"skus={len(sku_attr_values)}")
    return jsonify({"ok": True, "result": res if isinstance(res, dict) else {}}), 200
