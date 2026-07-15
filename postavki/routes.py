"""Postavki (Поставки — FBO ombor to'ldirish) moduli — barcha kod shu yerda.

Faza 1 — O'QISH: поставкаlar ro'yxati + restock (tugayotgan SKU) tavsiyasi.
Faza 2 (keyin) — yaratish (sku/stocks → time-slot → create) shu yerga qo'shiladi.

Tizimning boshqa modullariga tegmaydi; ulanish faqat app.py'dagi
``register_blueprint(postavki_bp)`` qatori orqali. Uzum chaqiruvlari
``postavki/client.py`` orqali (ichki portal API, browser token).
"""
from __future__ import annotations

from flask import Blueprint, Response, jsonify, render_template, request, session
from flask_login import current_user, login_required
from sqlalchemy import select

from datetime import date, datetime

from extensions import SessionLocal
from models import Shop, Variant, PostavkaGrabPlan, PostavkaAutoConfig
from core.auth_helpers import _user_shop_ids
from core.time_helpers import APP_TZ
from postavki import client, akt_cache, slot_grabber, autoslot_client, restock_plan, auto_plan
from postavki.autoslot import store


def _tashkent_day(ms: int) -> date:
    """epoch ms → Toshkent KUNI. UTC'da hisoblasak kun chegarasi surilib,
    ertalabki slot «kecha»ga tushib qolardi."""
    return datetime.fromtimestamp(int(ms) / 1000, APP_TZ).date()

postavki_bp = Blueprint("postavki_bp", __name__)


# ── Do'kon-egalik yordamchilari ──────────────────────────────────────


def _user_shops() -> list[dict]:
    """Logged-in foydalanuvchi ko'ra oladigan do'konlar: [{uzum_id, name}]."""
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
    """Tanlangan do'kon shu foydalanuvchiga tegishlimi (token-darajali guard)."""
    uid = int(current_user.get_id())
    allowed = _user_shop_ids(uid)
    if not allowed:
        return False
    with SessionLocal() as db:
        shop = db.execute(
            select(Shop).where(Shop.uzum_id == str(shop_uzum_id))
        ).scalar_one_or_none()
    return bool(shop and shop.id in allowed)


# ── Pages ────────────────────────────────────────────────────────────


@postavki_bp.get("/postavki")
@login_required
def postavki_page():
    lang = session.get("lang", "uz")
    shops = _user_shops()
    return render_template(
        "postavki.html",
        title="Yetkazib berish" if lang == "uz" else "Поставки",
        shops=shops,
    )


# ── O'qish API'lari (Faza 1) ─────────────────────────────────────────


@postavki_bp.get("/postavki/api/invoices")
@login_required
def postavki_invoices_api():
    """FBO поставкаlar ro'yxati (bir do'kon, sahifalangan)."""
    shop = (request.args.get("shop") or "").strip()
    if not shop:
        return jsonify({"error": "shop kerak"}), 400
    if not _can_access(shop):
        return jsonify({"error": "Do'kon topilmadi yoki ruxsat yo'q"}), 403
    page = request.args.get("page", 0, type=int)
    size = request.args.get("size", 20, type=int)
    statuses = (request.args.get("statuses") or "").strip()
    try:
        items = client.list_invoices(shop, page=page, size=size, statuses=statuses)
    except Exception as e:  # portal API xatosi → 502
        return jsonify({"error": str(e)}), 502
    # Sahifaга kirilganда: CREATED + slotли aktларni FONda bazaga isitamiz
    # (Uzum saytida slot qo'yilган holatlar ham shu yerda tutiladi). Javobni
    # bloklamaydi — bulk «Акт отправки» keyин oniy bo'ladi.
    try:
        akt_cache.warm_invoices_async(shop, items)
    except Exception:
        pass
    return jsonify({"items": items, "page": page, "size": size})


@postavki_bp.get("/postavki/api/restock")
@login_required
def postavki_restock_api():
    """Yetkazib berish mumkin bo'lgan SKU'lar (+ Uzum recommendQty/forecast)."""
    shop = (request.args.get("shop") or "").strip()
    if not shop:
        return jsonify({"error": "shop kerak"}), 400
    if not _can_access(shop):
        return jsonify({"error": "Do'kon topilmadi yoki ruxsat yo'q"}), 403
    page = request.args.get("page", 0, type=int)
    size = request.args.get("size", 20, type=int)
    search = (request.args.get("search") or "").strip()
    groups = (request.args.get("groups") or "SMALL,MEDIUM").strip()
    try:
        items = client.restock_skus(shop, page=page, size=size, search=search, groups=groups)
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    # DIAG (vaqtinchalik): bloklangan SKU maydonini topish uchun — xom item kalitlari
    # + status/block-o'xshash maydonlar. Topgach bu blok olib tashlanadi.
    # Bloklangan SKU statusini DB'dan (Variant.uzum_sku_id) qo'shamiz — restock
    # javobida bu YO'Q (Uzum bermaydi), bizda OpenAPI sync to'plagan (blocked flag).
    try:
        sku_ids = [str(it.get("skuId")) for it in items if it.get("skuId") is not None]
        if sku_ids:
            with SessionLocal() as db:
                rows = db.execute(
                    select(
                        Variant.uzum_sku_id,
                        Variant.blocking_reason,
                        Variant.sku_block_reason,
                    ).where(
                        Variant.uzum_sku_id.in_(sku_ids),
                        Variant.blocked.is_(True),
                    )
                ).all()
            blk = {r[0]: ((r[1] or r[2] or "").strip()) for r in rows}
            for it in items:
                if str(it.get("skuId")) in blk:
                    it["blocked"] = True
                    reason = blk[str(it.get("skuId"))]
                    if reason:
                        it["blockReason"] = reason
    except Exception as _e:
        print(f"[postavki/restock] block-merge err={_e}", flush=True)
    return jsonify({"items": items, "page": page, "size": size})


@postavki_bp.get("/postavki/api/restock-plan")
@login_required
def postavki_restock_plan_api():
    """Yarim-avtomat rejim: guruhlangan SKU'lar + «qancha kerak» tavsiyasi.

    Hisob: (oxirgi N kun sotuvi − Uzum qoldig'i),
    bizning ombor qoldig'i bilan cheklangan. Qatorlar Uzum sku-list'idan
    olinadi, ya'ni har biri поставка qatoriga aylana oladi.
    """
    shop = (request.args.get("shop") or "").strip()
    if not shop:
        return jsonify({"error": "shop kerak"}), 400
    if not _can_access(shop):
        return jsonify({"error": "Do'kon topilmadi yoki ruxsat yo'q"}), 403
    days = restock_plan.resolve_days(request.args.get("days"))
    groups = (request.args.get("groups") or "SMALL,MEDIUM").strip()
    refresh = (request.args.get("refresh") or "") in ("1", "true", "yes")
    # Kalendar oraliq (ilovadagi boshqa sahifalar bilan bir xil: YYYY-MM-DD).
    # Berilgan bo'lsa `days` chipidan USTUN turadi.
    date_from = (request.args.get("date_from") or "").strip() or None
    date_to = (request.args.get("date_to") or "").strip() or None
    try:
        plan = restock_plan.build_plan(
            shop, days=days, groups=groups, refresh=refresh,
            date_from=date_from, date_to=date_to,
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    return jsonify(plan)


# ── Yaratish API'lari (Faza 2) — REAL поставка yaratadi ──────────────


def _parse_sku_lines(raw) -> tuple[list[dict] | None, str | None]:
    """Frontend skuList'ini tekshir+tozala → [{skuId,quantityToStock,purchasePrice}].

    Har qatorda skuId va miqdor (>0) bo'lishi shart. Xato bo'lsa
    ``(None, xabar)`` qaytaradi.
    """
    if not isinstance(raw, list) or not raw:
        return None, "skuList bo'sh"
    lines = []
    for r in raw:
        if not isinstance(r, dict):
            return None, "skuList noto'g'ri"
        try:
            sku_id = int(r.get("skuId"))
            qty = int(r.get("quantityToStock"))
            price = int(r.get("purchasePrice") or 0)
        except (TypeError, ValueError):
            return None, "skuId/miqdor/narx raqam bo'lishi kerak"
        if qty <= 0:
            return None, "miqdor 0 dan katta bo'lishi kerak"
        # Kalit tartibi brauzer HAR'iga aniq mos: skuId, purchasePrice, quantityToStock.
        lines.append({"skuId": sku_id, "purchasePrice": price, "quantityToStock": qty})
    return lines, None


@postavki_bp.post("/postavki/api/prepare")
@login_required
def postavki_prepare_api():
    """Tanlangan SKU'lar uchun omborni aniqlab, bo'sh slotlarni qaytaradi.

    Hech narsa YARATMAYDI — faqat resolve_stocks + time-slot o'qish.
    body: {shop, skuList:[{skuId,quantityToStock,purchasePrice}]}
    → {stock:{id,title,poolSource}, slots:[{timeFrom,timeTo}]}
    """
    data = request.get_json(silent=True) or {}
    shop = str(data.get("shop") or "").strip()
    if not shop:
        return jsonify({"error": "shop kerak"}), 400
    if not _can_access(shop):
        return jsonify({"error": "Do'kon topilmadi yoki ruxsat yo'q"}), 403
    lines, err = _parse_sku_lines(data.get("skuList"))
    if err:
        return jsonify({"error": err}), 400
    try:
        stocks = client.resolve_stocks(shop, [l["skuId"] for l in lines])
        if not stocks:
            return jsonify({"error": "Ombor topilmadi (sku/stocks bo'sh)"}), 502
        stock = stocks[0]
        pool = stock.get("poolSource") or "FULLFILMENT"
        slots = client.get_time_slots(shop, lines, pool)
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"stock": stock, "slots": slots})


# ── «Авто» rejim: sozlama + oldindan ko'rish + bir tugmada yaratish ──────


def _auto_cfg_row(db, shop: str):
    return db.execute(
        select(PostavkaAutoConfig).where(
            PostavkaAutoConfig.user_id == int(current_user.get_id()),
            PostavkaAutoConfig.shop_uzum_id == str(shop),
        )
    ).scalar_one_or_none()


def _auto_cfg_dict(row) -> dict:
    return {
        "maxUnits": row.max_units, "maxSkus": row.max_skus,
        "minPerSku": row.min_per_sku, "slotFrom": row.slot_from_offset,
        "slotTo": row.slot_to_offset, "salesDays": row.sales_days,
    }


@postavki_bp.get("/postavki/api/auto/config")
@login_required
def postavki_auto_config_get():
    """Avto sozlamasi. `configured:false` — hali qo'yilmagan (avto ishlamaydi)."""
    shop = str(request.args.get("shop") or "").strip()
    if not shop or not _can_access(shop):
        return jsonify({"error": "Do'kon topilmadi yoki ruxsat yo'q"}), 403
    with SessionLocal() as db:
        row = _auto_cfg_row(db, shop)
        cfg = _auto_cfg_dict(row) if row else dict(auto_plan.DEFAULTS)
    d_from, d_to = auto_plan.slot_window(cfg)
    return jsonify({"configured": bool(row), "config": cfg,
                    "slotFromDate": d_from.isoformat(), "slotToDate": d_to.isoformat(),
                    "uzumMaxSkus": auto_plan.UZUM_MAX_SKUS})


@postavki_bp.post("/postavki/api/auto/config")
@login_required
def postavki_auto_config_save():
    """To'rtta chegara — hammasi majburiy; chegaradan chiqsa qisiladi."""
    data = request.get_json(silent=True) or {}
    shop = str(data.get("shop") or "").strip()
    if not shop or not _can_access(shop):
        return jsonify({"error": "Do'kon topilmadi yoki ruxsat yo'q"}), 403
    cfg, err = auto_plan.clamp_config(data)
    if err:
        return jsonify({"error": err}), 400
    with SessionLocal() as db:
        row = _auto_cfg_row(db, shop)
        if row is None:
            row = PostavkaAutoConfig(user_id=int(current_user.get_id()), shop_uzum_id=shop)
            db.add(row)
        row.max_units = cfg["maxUnits"]
        row.max_skus = cfg["maxSkus"]
        row.min_per_sku = cfg["minPerSku"]
        row.slot_from_offset = cfg["slotFrom"]
        row.slot_to_offset = cfg["slotTo"]
        row.sales_days = cfg["salesDays"]
        db.commit()
    d_from, d_to = auto_plan.slot_window(cfg)
    return jsonify({"configured": True, "config": cfg,
                    "slotFromDate": d_from.isoformat(), "slotToDate": d_to.isoformat()})


@postavki_bp.get("/postavki/api/auto/preview")
@login_required
def postavki_auto_preview():
    """«Nima jo'natiladi» — HECH NARSA yaratmaydi (dry-run).

    Sotuv oynasi ixtiyoriy: `?days=` chipi yoki `?date_from=&date_to=` (kalendar,
    ustun) — yarim-avtomatdagi bilan bir xil. Berilmasa — sozlamadagi salesDays.
    """
    shop = str(request.args.get("shop") or "").strip()
    if not shop or not _can_access(shop):
        return jsonify({"error": "Do'kon topilmadi yoki ruxsat yo'q"}), 403
    with SessionLocal() as db:
        row = _auto_cfg_row(db, shop)
        if row is None:
            return jsonify({"error": "not_configured"}), 409
        cfg = _auto_cfg_dict(row)

    d_from = (request.args.get("date_from") or "").strip() or None
    d_to = (request.args.get("date_to") or "").strip() or None
    days_raw = request.args.get("days")
    if days_raw is not None:
        # Chipni yarim-avtomat bilan bir xil qisamiz ([7,10,15,30,60]).
        cfg = {**cfg, "salesDays": restock_plan.resolve_days(days_raw)}

    try:
        draft = auto_plan.build_auto_draft(shop, cfg, date_from=d_from, date_to=d_to)
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"config": cfg, **draft})


def _create_one_auto(shop: str, lines: list[dict], cfg: dict, d_from: date, d_to: date) -> dict:
    """Bitta накладной: ombor → slot → create → (slot yo'q bo'lsa) avto-slot.

    Slotlar HAR накладной uchun QAYTA o'qiladi: birini band qilgach o'sha slot
    band bo'ladi, ikkinchisiga boshqasi kerak.
    """
    stocks = client.resolve_stocks(shop, [l["skuId"] for l in lines])
    if not stocks:
        raise RuntimeError("Ombor topilmadi (sku/stocks bo'sh)")
    stock = stocks[0]
    pool = stock.get("poolSource") or "FULLFILMENT"
    slots = client.get_time_slots(shop, lines, pool)
    chosen = _earliest_slot_in_window(slots, d_from, d_to)

    invoice = client.create_invoice(shop, lines, chosen, int(stock["id"]))
    inv_id = (invoice or {}).get("id")
    if not inv_id:
        raise RuntimeError("Yaratilmadi (javob bo'sh)")

    units = sum(int(l["quantityToStock"]) for l in lines)
    queued, queue_err = False, ""
    if chosen:
        try:
            akt_cache.warm_one_async(shop, inv_id, date_updated=invoice.get("dateUpdated"))
        except Exception:
            pass
    else:
        try:
            with SessionLocal() as db:
                store.enqueue(
                    db, user_id=int(current_user.get_id()), shop_uzum_id=shop,
                    invoice_id=int(inv_id),
                    invoice_number=str(invoice.get("invoiceNumber") or ""),
                    volume=units, dim_group=_dim_of(invoice),
                    pool_source=stock.get("poolSource"), stock_id=stock.get("id"),
                    max_date=d_to, min_date=d_from, current_slot_ms=None,
                )
                db.commit()
            queued = True
        except Exception as e:
            queue_err = str(e)

    return {
        "invoiceId": inv_id,
        "invoiceNumber": invoice.get("invoiceNumber"),
        "slotMs": chosen,
        "queued": queued,
        "queueError": queue_err,
        "totalUnits": units,
        "skuCount": len(lines),
    }


@postavki_bp.post("/postavki/api/auto/create")
@login_required
def postavki_auto_create():
    """Avto-поставка: bitta yoki HAMMA накладнойni yaratadi.

    body: {shop, packs:[[{skuId,quantityToStock,purchasePrice}, ...], ...]}
    Har pack = bitta накладной (UI ko'rsatgan aynan o'sha qatorlar — WYSIWYG).
    Har biri user chegaralariga qarshi qayta TEKSHIRILADI.

    Slot NISBIY oynadan ([bugun+from .. bugun+to]): bo'sh slot bo'lsa eng ertasi
    band qilinadi, bo'lmasa накладной slotsiz ochilib avto-slot navbatiga tushadi.
    """
    data = request.get_json(silent=True) or {}
    shop = str(data.get("shop") or "").strip()
    if not shop or not _can_access(shop):
        return jsonify({"error": "Do'kon topilmadi yoki ruxsat yo'q"}), 403
    with SessionLocal() as db:
        row = _auto_cfg_row(db, shop)
        if row is None:
            return jsonify({"error": "not_configured"}), 409
        cfg = _auto_cfg_dict(row)

    packs = data.get("packs") or []
    if not isinstance(packs, list) or not packs:
        return jsonify({"error": "packs kerak"}), 400
    if len(packs) > auto_plan.MAX_INVOICES:
        return jsonify({"error": f"Слишком много накладных за раз (максимум {auto_plan.MAX_INVOICES})."}), 400

    # Har pack chegaralarga MOSmi — mijozga ishonmaymiz.
    parsed: list[list[dict]] = []
    for pack in packs:
        lines, err = _parse_sku_lines(pack)
        if err:
            return jsonify({"error": err}), 400
        if len(lines) > min(cfg["maxSkus"], auto_plan.UZUM_MAX_SKUS):
            return jsonify({"error": "Превышен лимит SKU в накладной."}), 400
        if sum(int(l["quantityToStock"]) for l in lines) > cfg["maxUnits"]:
            return jsonify({"error": "Превышен лимит единиц в накладной."}), 400
        parsed.append(lines)

    d_from, d_to = auto_plan.slot_window(cfg)

    # Ketma-ket: har накладнойdan keyin slotlar o'zgaradi (biri band bo'ldi).
    results, errors = [], []
    for i, lines in enumerate(parsed):
        try:
            results.append({**_create_one_auto(shop, lines, cfg, d_from, d_to), "index": i})
        except Exception as e:
            # Oldingilari REAL yaratilgan — ularni yo'qotmaymiz, xatoni qaytaramiz.
            errors.append({"index": i, "error": str(e)})
            break

    if not results:
        return jsonify({"error": (errors[0]["error"] if errors else "Не удалось создать.")}), 502

    return jsonify({
        "created": results,
        "errors": errors,
        "slotFromDate": d_from.isoformat(),
        "slotToDate": d_to.isoformat(),
    }), 201


def _earliest_slot_in_window(slots: list[dict], d_from: date, d_to: date) -> int | None:
    """Oyna ICHIDAGI eng erta slot (epoch ms) yoki None.

    Kun Toshkent bo'yicha solishtiriladi — `day_bounds_tashkent` bilan bir xil
    (aks holda UTC'da kun chegarasi surilib, «ertaga»ni «bugun» deb olardik).
    """
    best = None
    for s in slots or []:
        ms = s.get("timeFrom") or s.get("from")
        if not ms:
            continue
        day = _tashkent_day(int(ms))
        if d_from <= day <= d_to and (best is None or int(ms) < best):
            best = int(ms)
    return best


@postavki_bp.post("/postavki/api/create")
@login_required
def postavki_create_api():
    """поставкаni YARATADI (REAL). ⚠️ Uzum'da haqiqiy накладной ochadi.

    body: {shop, skuList:[...], timeFrom:<slot ms>, stockId}
    → yaratilgan invoice obyekti.
    """
    data = request.get_json(silent=True) or {}
    shop = str(data.get("shop") or "").strip()
    if not shop:
        return jsonify({"error": "shop kerak"}), 400
    if not _can_access(shop):
        return jsonify({"error": "Do'kon topilmadi yoki ruxsat yo'q"}), 403
    lines, err = _parse_sku_lines(data.get("skuList"))
    if err:
        return jsonify({"error": err}), 400
    # timeFrom IXTIYORIY — yo'q bo'lsa поставка slotsiz yaratiladi («Выбрать позже»).
    time_from = _int_or_none(data.get("timeFrom"))
    stock_id = _int_or_none(data.get("stockId"))
    if not stock_id or stock_id <= 0:
        return jsonify({"error": "stockId kerak"}), 400
    if time_from is not None and time_from <= 0:
        time_from = None
    try:
        invoice = client.create_invoice(shop, lines, time_from, stock_id)
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    if not invoice or not invoice.get("id"):
        return jsonify({"error": "Yaratilmadi (javob bo'sh)"}), 502
    # Slot bilan yaratilган bo'lsa — aktini darhol FONda keshlaymiz.
    if time_from is not None:
        try:
            akt_cache.warm_one_async(shop, invoice.get("id"), date_updated=invoice.get("dateUpdated"))
        except Exception:
            pass
    return jsonify({"invoice": invoice}), 201


# ── Mavjud поставкани o'zgartirish/bekor qilish (Faza 3) ─────────────


def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


@postavki_bp.post("/postavki/api/invoice/slots")
@login_required
def postavki_invoice_slots_api():
    """Mavjud поставка uchun BOSHQA bo'sh slotlar (o'zgartirish uchun).

    Hech narsa o'zgartirmaydi — faqat o'qish.
    body: {shop, invoiceId, poolSource} → {slots:[{timeFrom,timeTo}]}
    """
    data = request.get_json(silent=True) or {}
    shop = str(data.get("shop") or "").strip()
    inv_id = _int_or_none(data.get("invoiceId"))
    if not shop or not inv_id:
        return jsonify({"error": "shop/invoiceId kerak"}), 400
    if not _can_access(shop):
        return jsonify({"error": "Do'kon topilmadi yoki ruxsat yo'q"}), 403
    pool = (data.get("poolSource") or "FULLFILMENT").strip()
    try:
        slots = client.invoice_time_slots(shop, inv_id, pool)
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"slots": slots})


@postavki_bp.post("/postavki/api/invoice/set-slot")
@login_required
def postavki_set_slot_api():
    """Mavjud поставка slotini O'ZGARTIRADI.

    body: {shop, invoiceId, timeFrom, stockId, poolSource} → {invoice}
    """
    data = request.get_json(silent=True) or {}
    shop = str(data.get("shop") or "").strip()
    inv_id = _int_or_none(data.get("invoiceId"))
    time_from = _int_or_none(data.get("timeFrom"))
    stock_id = _int_or_none(data.get("stockId"))
    if not shop or not inv_id or not time_from or not stock_id:
        return jsonify({"error": "shop/invoiceId/timeFrom/stockId kerak"}), 400
    if not _can_access(shop):
        return jsonify({"error": "Do'kon topilmadi yoki ruxsat yo'q"}), 403
    pool = (data.get("poolSource") or "FULLFILMENT").strip()
    try:
        invoice = client.set_time_slot(shop, inv_id, time_from, stock_id, pool)
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    if not invoice or not invoice.get("id"):
        return jsonify({"error": "O'zgartirilmadi (javob bo'sh)"}), 502
    # Slot o'zgardi → akt mazmuni o'zgaradi → eskisini o'chirib, yangisini FONda
    # darhol qayta keshlaymiz (bulk «Акт отправки» oniy qolsin).
    try:
        akt_cache.warm_one_async(shop, inv_id, date_updated=invoice.get("dateUpdated"))
    except Exception as e:
        print(f"[postavki] set-slot akt-kesh yangilash xato invoice={inv_id}: {e!r}")
    return jsonify({"invoice": invoice})


# ── Avto-band rejalari (Faza 2 — tanlangan kunga slot olish) ─────────


def _draft_index(shop: str) -> dict:
    """Avto-slotга mos aktlar id→inv xaritasi (reja yaratishda snapshot).
    Slotsiz draft + slotli CREATED (re-booking) — ikkalasi ham."""
    idx = {}
    for inv in slot_grabber.list_autoslot_invoices(shop):
        if inv.get("id"):
            idx[int(inv["id"])] = inv
    return idx


def _dim_of(inv: dict):
    d = inv.get("dimensionalGroup")
    if isinstance(d, dict):
        return d.get("group") or d.get("title")
    return str(d) if d is not None else None


@postavki_bp.post("/postavki/api/grab-plan/create")
@login_required
def postavki_grab_plan_create():
    """Tanlangan slotsiz draft'lar uchun «o'sha kunga avto-band» reja yaratadi.

    body: {shop, invoiceIds:[..], targetDay:"YYYY-MM-DD"} → {created, plans}.
    Mavjud waiting reja bo'lsa kunini yangilaydi (dubl yaratmaydi).
    """
    data = request.get_json(silent=True) or {}
    shop = str(data.get("shop") or "").strip()
    inv_ids = data.get("invoiceIds") or []
    # maxDate (deadline «shu kungacha») — yangi navbat semantikasi; targetDay
    # eski UI bilan moslik uchun zaxira nomi.
    day_s = str(data.get("maxDate") or data.get("targetDay") or "").strip()
    # minDate (pastki chegara «shu kundan») — ixtiyoriy. Bo'sh = chegarasiz.
    min_s = str(data.get("minDate") or "").strip()
    if not shop or not inv_ids or not day_s:
        return jsonify({"error": "shop/invoiceIds/maxDate kerak"}), 400
    if not _can_access(shop):
        return jsonify({"error": "Do'kon topilmadi yoki ruxsat yo'q"}), 403
    try:
        max_date = date.fromisoformat(day_s)
    except ValueError:
        return jsonify({"error": "maxDate YYYY-MM-DD bo'lsin"}), 400
    min_date = None
    if min_s:
        try:
            min_date = date.fromisoformat(min_s)
        except ValueError:
            return jsonify({"error": "minDate YYYY-MM-DD bo'lsin"}), 400
        if min_date > max_date:
            return jsonify({"error": "minDate maxDate'dan katta bo'lmasin"}), 400
    try:
        idx = _draft_index(shop)
    except Exception as e:
        return jsonify({"error": f"draftlar o'qilmadi: {e}"}), 502

    uid = int(current_user.get_id())
    created, updated, skipped = 0, 0, []
    out = []
    # AUTOSLOT_URL qo'yilgan bo'lsa navbatga yozishni autoslot servisiga
    # o'tkazamiz (HTTP); aks holda BUGUNGIDEK mahalliy `store.enqueue`.
    use_remote = autoslot_client.enabled()
    with SessionLocal() as db:
        for raw in inv_ids:
            inv_id = _int_or_none(raw)
            inv = idx.get(inv_id) if inv_id else None
            if not inv:
                skipped.append(raw)  # slotsiz draft emas (yoki allaqachon slotli)
                continue
            stock = inv.get("stock") or {}
            # Akt allaqachon slotда bo'lsa — current_slot_ms (re-booking uchun;
            # faqat undan ertaroq slot bo'shasa ko'chiriladi). Slotsiz → None.
            ts = inv.get("timeSlotReservation") or {}
            current_slot_ms = ts.get("timeFrom") or ts.get("from")
            csm = int(current_slot_ms) if current_slot_ms else None
            if use_remote:
                try:
                    resp = autoslot_client.post_plan({
                        "user_id": uid,
                        "shop_uzum_id": shop,
                        "invoice_id": inv_id,
                        "invoice_number": str(inv.get("invoiceNumber") or ""),
                        "volume": int(inv.get("totalToStock") or 0),
                        "dim_group": _dim_of(inv),
                        "pool_source": stock.get("poolSource"),
                        "stock_id": stock.get("id"),
                        "max_date": max_date.isoformat(),
                        "min_date": min_date.isoformat() if min_date else None,
                        "current_slot_ms": csm,
                    })
                    action = resp.get("action")
                except Exception as e:
                    return jsonify({"error": f"autoslot servisi: {e}"}), 502
            else:
                _, action = store.enqueue(
                    db,
                    user_id=uid,
                    shop_uzum_id=shop,
                    invoice_id=inv_id,
                    invoice_number=str(inv.get("invoiceNumber") or ""),
                    volume=int(inv.get("totalToStock") or 0),
                    dim_group=_dim_of(inv),
                    pool_source=stock.get("poolSource"),
                    stock_id=stock.get("id"),
                    max_date=max_date,
                    min_date=min_date,
                    current_slot_ms=csm,
                )
            created += (action == "created")
            updated += (action == "updated")
            out.append({"invoice_id": inv_id, "size": int(inv.get("totalToStock") or 0)})
        if not use_remote:
            db.commit()
    return jsonify({"created": created, "updated": updated, "skipped": skipped,
                    "maxDate": day_s, "plans": out})


@postavki_bp.get("/postavki/api/grab-plan/list")
@login_required
def postavki_grab_plan_list():
    """Do'kon uchun avto-band rejalari (holati bilan). ?shop=&status="""
    shop = (request.args.get("shop") or "").strip()
    if not shop:
        return jsonify({"error": "shop kerak"}), 400
    if not _can_access(shop):
        return jsonify({"error": "Do'kon topilmadi yoki ruxsat yo'q"}), 403
    status = (request.args.get("status") or "").strip()
    if autoslot_client.enabled():
        try:
            plans = autoslot_client.list_for_shop(shop, status or None)
        except Exception as e:
            return jsonify({"error": f"autoslot servisi: {e}"}), 502
        return jsonify({"plans": plans})
    with SessionLocal() as db:
        q = (select(PostavkaGrabPlan)
             .where(PostavkaGrabPlan.shop_uzum_id == shop)
             .order_by(PostavkaGrabPlan.target_day, PostavkaGrabPlan.id))
        if status:
            q = q.where(PostavkaGrabPlan.status == status)
        rows = db.execute(q).scalars().all()
        plans = [{
            "id": r.id, "invoiceId": r.invoice_id, "invoiceNumber": r.invoice_number,
            "size": r.draft_size, "targetDay": r.target_day.isoformat(),
            "maxDate": r.max_date.isoformat() if r.max_date else r.target_day.isoformat(),
            "enabledAt": r.enabled_at.isoformat() if r.enabled_at else None,
            "status": r.status, "bookedSlotMs": r.booked_slot_ms, "error": r.error,
        } for r in rows]
    return jsonify({"plans": plans})


@postavki_bp.get("/postavki/api/grab-plan/all")
@login_required
def postavki_grab_plan_all():
    """Foydalanuvchining BARCHA do'konlari bo'yicha avto-band rejalari (cross-shop,
    «Навбат» tabи uchun). ?status= ixtiyoriy filtr."""
    uid = int(current_user.get_id())
    allowed = _user_shop_ids(uid)
    if not allowed:
        return jsonify({"plans": []})
    status = (request.args.get("status") or "").strip()
    with SessionLocal() as db:
        shop_rows = db.execute(
            select(Shop.uzum_id, Shop.name).where(Shop.id.in_(allowed))
        ).all()
        names = {r.uzum_id: r.name for r in shop_rows}
        if not names:
            return jsonify({"plans": []})
        if autoslot_client.enabled():
            try:
                plans = autoslot_client.list_for_shops(names, status or None)
            except Exception as e:
                return jsonify({"error": f"autoslot servisi: {e}"}), 502
            return jsonify({"plans": plans})
        q = select(PostavkaGrabPlan).where(PostavkaGrabPlan.shop_uzum_id.in_(list(names.keys())))
        if status:
            q = q.where(PostavkaGrabPlan.status == status)
        q = q.order_by(PostavkaGrabPlan.status, PostavkaGrabPlan.target_day, PostavkaGrabPlan.id)
        rows = db.execute(q).scalars().all()
        plans = [{
            "id": r.id, "shop": r.shop_uzum_id, "shopName": names.get(r.shop_uzum_id),
            "invoiceId": r.invoice_id, "invoiceNumber": r.invoice_number, "size": r.draft_size,
            "targetDay": r.target_day.isoformat(),
            "maxDate": r.max_date.isoformat() if r.max_date else r.target_day.isoformat(),
            "enabledAt": r.enabled_at.isoformat() if r.enabled_at else None,
            "status": r.status, "bookedSlotMs": r.booked_slot_ms, "error": r.error,
        } for r in rows]
    return jsonify({"plans": plans})


@postavki_bp.post("/postavki/api/grab-plan/cancel")
@login_required
def postavki_grab_plan_cancel():
    """Rejani bekor qiladi. body: {shop, planId} → {ok}."""
    data = request.get_json(silent=True) or {}
    shop = str(data.get("shop") or "").strip()
    plan_id = _int_or_none(data.get("planId"))
    if not shop or not plan_id:
        return jsonify({"error": "shop/planId kerak"}), 400
    if not _can_access(shop):
        return jsonify({"error": "Do'kon topilmadi yoki ruxsat yo'q"}), 403
    if autoslot_client.enabled():
        try:
            p = autoslot_client.get_plan(plan_id)
            # Egalik SellerHub tomonida tekshiriladi (autoslot'da shop-egalik yo'q).
            if not p or str(p.get("shop_uzum_id")) != shop:
                return jsonify({"error": "Reja topilmadi"}), 404
            autoslot_client.cancel(plan_id)
        except Exception as e:
            return jsonify({"error": f"autoslot servisi: {e}"}), 502
        return jsonify({"ok": True})
    with SessionLocal() as db:
        row = db.get(PostavkaGrabPlan, plan_id)
        if not row or row.shop_uzum_id != shop:
            return jsonify({"error": "Reja topilmadi"}), 404
        row.status = "canceled"
        db.commit()
    return jsonify({"ok": True})


@postavki_bp.post("/postavki/api/invoice/cancel")
@login_required
def postavki_cancel_api():
    """поставкаni BEKOR QILADI. ⚠️ Uzum'da haqiqiy bekor qiladi.

    body: {shop, invoiceId} → {ok:true}
    """
    data = request.get_json(silent=True) or {}
    shop = str(data.get("shop") or "").strip()
    inv_id = _int_or_none(data.get("invoiceId"))
    if not shop or not inv_id:
        return jsonify({"error": "shop/invoiceId kerak"}), 400
    if not _can_access(shop):
        return jsonify({"error": "Do'kon topilmadi yoki ruxsat yo'q"}), 403
    try:
        client.cancel_invoice(shop, inv_id)
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    try:
        akt_cache.delete_akt(inv_id)  # bekor qilingan поставка akti qayta bosilmaydi
    except Exception as e:
        print(f"[postavki] cancel akt-kesh tozalash xato invoice={inv_id}: {e!r}")
    return jsonify({"ok": True})


# ── Detal + Возвраты (o'qish) ────────────────────────────────────────


@postavki_bp.get("/postavki/api/invoice/<int:invoice_id>")
@login_required
def postavki_invoice_detail_api(invoice_id: int):
    """Bitta поставка to'liq ma'lumoti + tarkibi (mahsulotlar)."""
    shop = (request.args.get("shop") or "").strip()
    if not shop:
        return jsonify({"error": "shop kerak"}), 400
    if not _can_access(shop):
        return jsonify({"error": "Do'kon topilmadi yoki ruxsat yo'q"}), 403
    try:
        detail = client.invoice_detail(shop, invoice_id)
        products = client.invoice_products(shop, invoice_id)
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"detail": detail, "products": products})


@postavki_bp.get("/postavki/api/invoice/<int:invoice_id>/act")
@login_required
def postavki_invoice_act_api(invoice_id: int):
    """«Акт отправки» PDF (yetkazib berish akti). Brauzerda ochiladi.

    GET printInvoice → {pdf:base64} → dekodlangan PDF. Cache-first
    (postavki_invoice_akts) — DB'da bo'lsa oniy, aks holda jonli + saqlanadi.
    O'zgartirmaydi.
    """
    shop = (request.args.get("shop") or "").strip()
    if not shop:
        return jsonify({"error": "shop kerak"}), 400
    if not _can_access(shop):
        return jsonify({"error": "Do'kon topilmadi yoki ruxsat yo'q"}), 403
    du = request.args.get("du", type=int)  # поставка dateUpdated (ixtiyoriy, freshness)
    try:
        pdf = akt_cache.get_or_fetch_akt(shop, invoice_id, date_updated=du)
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    if not pdf:
        return jsonify({"error": "PDF bo'sh (akt topilmadi)"}), 502
    return Response(
        pdf,
        mimetype="application/pdf",
        headers={"Content-Disposition": f'inline; filename="akt-{invoice_id}.pdf"'},
    )


@postavki_bp.get("/postavki/api/invoices/act-bulk")
@login_required
def postavki_invoices_act_bulk_api():
    """Bir nechta поставка «Акт отправки»ни BITTA PDF qilib qaytaradi.

    Tanlangan накладныйlar uchun (``ids`` — vergul bilan) har birining aktini
    tortib, pypdf bilan birlashtiradi. http_json o'zida token-bucket + 429-retry
    bo'lgani uchun ketma-ket tortish avtomatik pace qilinadi. O'zgartirmaydi.
    """
    shop = (request.args.get("shop") or "").strip()
    if not shop:
        return jsonify({"error": "shop kerak"}), 400
    if not _can_access(shop):
        return jsonify({"error": "Do'kon topilmadi yoki ruxsat yo'q"}), 403
    def _int_list(raw):
        return [int(x) for x in (raw or "").split(",") if x.strip().lstrip("-").isdigit()]

    ids = _int_list(request.args.get("ids"))
    # Har поставканинг dateUpdated'i (ixtiyoriy, ``ids`` bilan PARALLEL). Berilsa
    # va uzunligi mos kelsa — freshness guard: keshdagi versiya bundan farq qilsa
    # (masalan slot o'zgargан) qator ESKIRGAN deb hisoblanib, jonli qayta tortiladi.
    # Bu fon-thread (warm_one_async) vaqtiga bog'liq emas — eski akt KO'CHIRILMAYDI.
    dus = _int_list(request.args.get("dus"))
    du_map = dict(zip(ids, dus)) if len(dus) == len(ids) else {}
    ids = [i for i in ids if i > 0][:100]  # zararsizlik chegarasi
    if not ids:
        return jsonify({"error": "ids kerak"}), 400
    pdfs: list[bytes] = []
    for iid in ids:
        try:
            # cache-first + freshness (background prefetch isitadi; slot o'zgarса yangi du → miss → jonli)
            pdf = akt_cache.get_or_fetch_akt(shop, iid, date_updated=du_map.get(iid))
            if pdf:
                pdfs.append(pdf)
        except Exception as e:  # bittasi yiqilsa — qolganini davom ettir
            print(f"[postavki] act-bulk: №{iid} akt olinmadi: {e!r}")
    merged = client.merge_pdfs(pdfs)
    if not merged:
        return jsonify({"error": "Akt PDF bo'sh (hech biri olinmadi)"}), 502
    # Qisman muvaffaqiyat: frontend «N tadan M ta olindi» deb ogohlantirsin.
    return Response(
        merged,
        mimetype="application/pdf",
        headers={
            "Content-Disposition": 'inline; filename="akt-bulk.pdf"',
            "X-Akt-Count": str(len(pdfs)),       # haqiqatda olingan akt soni
            "X-Akt-Requested": str(len(ids)),    # so'ralган akt soni
        },
    )


@postavki_bp.get("/postavki/api/invoice/<int:invoice_id>/barcodes")
@login_required
def postavki_invoice_barcodes_api(invoice_id: int):
    """«QR-коды» / штрихкод yorliqlari PDF (har birlik uchun). Brauzerda ochiladi.

    Yorliq miqdori Uzum'ning o'z поставка rejasidan olinadi (client'ga
    ishonmaymiz). ``type``: 1=штрихкод, 5=QR (default). O'zgartirmaydi.
    """
    shop = (request.args.get("shop") or "").strip()
    if not shop:
        return jsonify({"error": "shop kerak"}), 400
    if not _can_access(shop):
        return jsonify({"error": "Do'kon topilmadi yoki ruxsat yo'q"}), 403
    btype = request.args.get("type", 5, type=int)
    try:
        products = client.invoice_products(shop, invoice_id)
        lines = []
        for g in products:
            skus = g.get("skuForInvoiceDtoList") or [
                {"id": g.get("id"), "quantityToStock": g.get("quantityToStock")}
            ]
            for sk in skus:
                lines.append(
                    {"skuId": sk.get("id"), "amount": sk.get("quantityToStock") or 0}
                )
        if not any(l["amount"] for l in lines):
            return jsonify({"error": "Mahsulot topilmadi"}), 502
        pdf = client.print_barcodes(shop, lines, barcode_type=btype)
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    if not pdf:
        return jsonify({"error": "PDF bo'sh"}), 502
    return Response(
        pdf,
        mimetype="application/pdf",
        headers={"Content-Disposition": f'inline; filename="qr-{invoice_id}.pdf"'},
    )


# «QR-коды» — BIZNING HTML chop sahifamiz (FBS «Печать QR» bilan AYNAN bir xil
# i18n/shablon/usul). Brauzer chop etadi, QR `api.qrserver.com`'dan +
# image-rendering:pixelated → o'tkir; Uzum PDF'ining xiraligi yo'q.
#
# Yorliq o'lchamlari — Uzum «QR-коды» dialogidagi bilan bir xil ro'yxat, ikki
# guruh: «Принтер» (oddiy A4 yorliq varaqlari) + «Термопринтер» (termal rulon).
# Har preset: w/h — sahifa o'lchami (mm); tcol — matn ustuni; qr — QR tomoni;
# *_fs — shrift (pt). FBS `_QR_PRINT_SIZES` uslubida o'lchangan; mavjud kalitlar
# (70x37/43x25/30x20) FBS bilan bir xil qiymatda.
_PKX_QR_SIZES = {
    # Принтер (oddiy printer → A4 varaqqa GRID, `_PKX_QR_SHEET_SIZES`). qr ≤ h
    # bo'lishi shart — aks holda QR yorliq balandligidan oshib kesiladi.
    # Uzum «70×37» — chop PDF'i o'lchandi (Uzum_Business2.pdf): brauzer yorliqni nominal
    # 70mm emas, ~85% — 59.3×31.2mm chizadi, A4'ga 3 ustun (chekka ~2mm). QR=59.3×0.383≈22.7mm.
    # Menyu nomi «70×37» qoladi — bu Uzum'ning haqiqiy chop ko'rinishi. 48.5 bilan bir xil uslub.
    "70x37":     {"w": 59.3, "h": 31.2, "tcol": 16.5, "qr": 22.7, "sku_fs": 8,   "num_fs": 7, "num_last4_fs": 11, "pad": 3},
    # Uzum «48.5×25.4» — chop PDF'i o'lchandi (Uzum_Business1.pdf): brauzer yorliqni
    # 41mm eniga kichraytirib A4'ga 5 ustun ZICH teradi (chekka ~2mm), QR esa
    # 0.379×41≈15.5mm. Shuning uchun yorliq=41×21.4, qr=15.5, pad=2 (5 ustun sig'adi:
    # (210−4)/41≈5). Menyu nomi «48.5×25.4» qoladi — bu Uzum'ning haqiqiy chop ko'rinishi.
    "48.5x25.4": {"w": 40.5, "h": 21.4, "tcol": 10.5, "qr": 15.4, "sku_fs": 5,   "num_fs": 5, "num_last4_fs": 8, "pad": 3},
    # A4'da 6 ustun, yorliqlar TEGIB turadi (bo'shliqsiz) → o'ng+pastki punktir
    # chiziqlari birlashib UZLUKSIZ kesish to'ri beradi. 6×32=192 ≤ usable
    # (210 − 2×8mm margin). QR kichik (12mm); oxirgi 4 raqam yo'g'on/katta (7pt).
    "38x21.2":   {"w": 32,   "h": 18,   "tcol": 6,   "qr": 12, "sku_fs": 3.3, "num_fs": 4, "num_last4_fs": 7},
    # Термопринтер (rulon → har yorliq alohida sahifa). O'lchamlar FBS/DBS «QR»
    # tugmasi bilan AYNAN bir xil: fbs/routes.py `_QR_PRINT_SIZES` va
    # core/fbs_data.py `_PRODUCT_LABEL_SIZES`. FBS ham shu `fbs_qr_print.html`
    # shablonini sheet'SIZ render qiladi → bir xil shablon + bir xil qiymat = bir
    # xil chiqadi. 70×37 termo (qr=40) Принтер «70x37» (A4-grid, Uzum) bilan
    # to'qnashmasligi uchun alohida kalit «70x37_t».
    "30x20":     {"w": 30, "h": 20, "tcol": 7,  "qr": 14, "sku_fs": 5.4, "num_fs": 6, "num_last4_fs": 8},
    "43x25":     {"w": 43, "h": 25, "tcol": 8,  "qr": 20, "sku_fs": 6,   "num_fs": 7, "num_last4_fs": 9},
    "40x30":     {"w": 40, "h": 30, "tcol": 9,  "qr": 20, "sku_fs": 6.5, "num_fs": 7, "num_last4_fs": 9},
    "60x60":     {"w": 60, "h": 60, "tcol": 12, "qr": 34, "sku_fs": 8,   "num_fs": 8, "num_last4_fs": 11},
    "70x37_t":   {"w": 70, "h": 37, "tcol": 12, "qr": 40, "sku_fs": 8,   "num_fs": 8, "num_last4_fs": 11},
}
_PKX_QR_SIZE_DEFAULT = "43x25"
# Bu o'lchamlar «Принтер» — A4 varaqqa GRID qilib teriladi (Uzum «Принтер» PDF'i
# kabi: oddiy printer A4 yorliq varag'i). Qolganlari termoprinter (1 yorliq=1 sahifa).
_PKX_QR_SHEET_SIZES = {"70x37", "48.5x25.4", "38x21.2"}
# `reprint` = ko'rinadigan tabdagi (Принтер) chop tugmasi matni. Bu rejimda
# avto-chop yo'q — foydalanuvchi zoom qilib tekshirib, shu tugmadan chop etadi.
_PKX_QR_I18N = {
    "uz": {"title": "QR chop — Поставка", "count_prefix": "Yorliqlar:", "reprint": "Chop etish"},
    "ru": {"title": "Печать QR — Поставка", "count_prefix": "Этикеток:", "reprint": "Печать"},
}


def _flatten_qr_lines(products: list[dict]) -> list[dict]:
    """``getInvoiceProducts`` javobini tekis SKU-qatorlarga yoyadi.

    Har qator: ``{skuId, skuTitle, barcode, qty}``. ``barcode`` Uzum javobida
    bo'lsa o'sha (eng ishonchli), bo'lmasa bo'sh — chaqiruvchi keyin lokal
    ``Variant``'dan to'ldiradi.
    """
    flat: list[dict] = []
    for g in products:
        skus = g.get("skuForInvoiceDtoList") or [
            {
                "id": g.get("id"),
                "skuTitle": g.get("skuTitle"),
                "quantityToStock": g.get("quantityToStock"),
                "barcode": g.get("barcode"),
            }
        ]
        for sk in skus:
            flat.append(
                {
                    "skuId": sk.get("id"),
                    "skuTitle": (sk.get("skuTitle") or g.get("productTitle") or "").strip(),
                    "barcode": str(sk.get("barcode") or "").strip(),
                    "qty": int(sk.get("quantityToStock") or 0),
                }
            )
    return flat


def _build_qr_labels(flat: list[dict]) -> list[dict]:
    """Tekis SKU-qatorlardan chop yorliqlari — har birlik uchun bittadan.

    Har SKU `qty` (quantityToStock) marta takrorlanadi (Uzum `barcodes/print`
    bilan bir xil). QR mazmuni: shtrix-kod, bo'lmasa skuTitle (FBS bilan bir
    xil zaxira). Har yorliq: ``{sku, barcode}`` — ``fbs_qr_print.html`` kutadi.
    """
    labels: list[dict] = []
    for f in flat:
        content = f["barcode"] or f["skuTitle"]
        if not content or f["qty"] <= 0:
            continue
        if not f["barcode"]:
            print(
                f"[postavki/qr] shtrix-kod topilmadi skuId={f['skuId']} → skuTitle fallback",
                flush=True,
            )
        for _ in range(f["qty"]):
            labels.append({"sku": f["skuTitle"] or content, "barcode": content})
    return labels


@postavki_bp.get("/postavki/api/invoice/<int:invoice_id>/qr-print")
@login_required
def postavki_invoice_qr_print(invoice_id: int):
    """«QR-коды» — har birlik uchun QR yorlig'i, BIZNING chop sahifamizda.

    FBS «Печать QR» (fbs_qr_print.html) bilan AYNAN bir xil: brauzer chop etadi,
    QR qrserver'dan + image-rendering:pixelated → o'tkir (Uzum PDF'ining xiraligi
    yo'q). Har SKU shtrix-kodi `quantityToStock` marta takrorlanadi — Uzum'ning
    `barcodes/print` (type=5) bilan bir xil semantika.

    Shtrix-kod manbasi (ustuvorlik): invoice_products javobidagi `barcode` →
    lokal `Variant.barcode` (skuId bo'yicha) → oxirgi chora `skuTitle`. Hech
    narsa o'zgartirmaydi (faqat o'qiydi).
    """
    shop = (request.args.get("shop") or "").strip()
    if not shop:
        return jsonify({"error": "shop kerak"}), 400
    if not _can_access(shop):
        return jsonify({"error": "Do'kon topilmadi yoki ruxsat yo'q"}), 403
    try:
        products = client.invoice_products(shop, invoice_id)
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    flat = _flatten_qr_lines(products)

    # Shtrix-kod yetishmaganlarni lokal Variant jadvalidan to'ldiramiz (skuId bo'yicha).
    need = [str(f["skuId"]) for f in flat if not f["barcode"] and f["skuId"] is not None]
    if need:
        with SessionLocal() as db:
            rows = db.execute(
                select(Variant.uzum_sku_id, Variant.barcode).where(
                    Variant.uzum_sku_id.in_(need), Variant.barcode.isnot(None)
                )
            ).all()
        bc_by_sku = {str(sid): bc for (sid, bc) in rows if bc}
        for f in flat:
            if not f["barcode"]:
                f["barcode"] = (bc_by_sku.get(str(f["skuId"])) or "").strip()

    labels = _build_qr_labels(flat)
    if not labels:
        return jsonify({"error": "Mahsulot topilmadi"}), 502

    size = (request.args.get("size") or "").strip()
    lbl = _PKX_QR_SIZES.get(size) or _PKX_QR_SIZES[_PKX_QR_SIZE_DEFAULT]
    # «Принтер» o'lchami → A4 varaqqa GRID (Uzum kabi); termoprinter → 1 yorliq/sahifa.
    sheet = size in _PKX_QR_SHEET_SIZES
    lang = session.get("lang", "uz")
    i18n = _PKX_QR_I18N.get(lang, _PKX_QR_I18N["uz"])
    html = render_template("fbs_qr_print.html", labels=labels, lbl=lbl, i18n=i18n, sheet=sheet)
    # Keshни taqiqlaymiz — aks holda brauzer bir xil URL (?size=...) uchun eski
    # HTML'ni keshlab qoladi va dizayn o'zgarishlari (margin/o'lcham) ko'rinmaydi.
    resp = Response(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


# Возврат filtrlarining ruxsat etilgan qiymatlari (HAR «Поставки20+» tasdiqladi).
# Begona qiymat Uzum'ga uzatilmasin — server xatosi/validatsiya o'rniga oldindan toza.
_RETURN_STATUSES = {
    "CREATED", "SENT", "IN_PROGRESS", "MOVED_TO_DELIVERY",
    "ASSEMBLED", "COMPLETED", "UTILIZED", "CANCELED",
}
_RETURN_TYPES = {"RETURN", "DEFECTED", "FBS"}


def _clean_csv(raw, allowed):
    """Vergulli ro'yxatdan faqat ``allowed`` ichidagilarni qoldiradi (tartib saqlanadi)."""
    seen, out = set(), []
    for tok in (raw or "").split(","):
        t = tok.strip().upper()
        if t in allowed and t not in seen:
            seen.add(t)
            out.append(t)
    return ",".join(out)


@postavki_bp.get("/postavki/api/returns")
@login_required
def postavki_returns_api():
    """Возвраты (qaytarish) ro'yxati (bir do'kon, sahifalangan + filtrlangan).

    Query: page, size, statuses (CSV), types (CSV), number (returnNumberFilter).
    """
    shop = (request.args.get("shop") or "").strip()
    if not shop:
        return jsonify({"error": "shop kerak"}), 400
    if not _can_access(shop):
        return jsonify({"error": "Do'kon topilmadi yoki ruxsat yo'q"}), 403
    page = request.args.get("page", 0, type=int)
    size = request.args.get("size", 20, type=int)
    statuses = _clean_csv(request.args.get("statuses"), _RETURN_STATUSES)
    types = _clean_csv(request.args.get("types"), _RETURN_TYPES)
    number = (request.args.get("number") or "").strip()
    try:
        items = client.list_returns(
            shop, page=page, size=size,
            statuses=statuses, types=types, number_filter=number,
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"items": items, "page": page, "size": size})


@postavki_bp.get("/postavki/api/return/<int:return_id>")
@login_required
def postavki_return_detail_api(return_id: int):
    """Bitta возврат to'liq ma'lumoti + tarkibi (returnItems)."""
    shop = (request.args.get("shop") or "").strip()
    if not shop:
        return jsonify({"error": "shop kerak"}), 400
    if not _can_access(shop):
        return jsonify({"error": "Do'kon topilmadi yoki ruxsat yo'q"}), 403
    try:
        detail = client.return_detail(shop, return_id)
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"detail": detail})


@postavki_bp.get("/postavki/api/return-skus")
@login_required
def postavki_return_skus_api():
    """Возврат yaratish uchun qaytsa bo'ladigan SKU'lar (FBO qoldiq), sahifalangan."""
    shop = (request.args.get("shop") or "").strip()
    if not shop:
        return jsonify({"error": "shop kerak"}), 400
    if not _can_access(shop):
        return jsonify({"error": "Do'kon topilmadi yoki ruxsat yo'q"}), 403
    page = request.args.get("page", 0, type=int)
    size = request.args.get("size", 20, type=int)
    flt = (request.args.get("search") or request.args.get("filter") or "").strip()
    try:
        res = client.return_sku_source(shop, page=page, size=size, filter_q=flt)
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"items": res.get("items", []), "total": res.get("total", 0),
                    "page": page, "size": size})


@postavki_bp.post("/postavki/api/return/create")
@login_required
def postavki_return_create_api():
    """Возврат YARATADI (REAL). ⚠️ Uzum'da haqiqiy qaytarish накладной ochadi.

    body: {shop, returnItems:[<to'liq sku DTO + amount>]} → {return:{...}}.
    ``returnItems`` — frontend stock-sku'dan olgan obyektlarni AYNAN qaytaradi,
    har biriga ``amount`` (>0) qo'shilган. Biz skuId/amount tekshirib o'tkazamiz.
    """
    data = request.get_json(silent=True) or {}
    shop = str(data.get("shop") or "").strip()
    if not shop:
        return jsonify({"error": "shop kerak"}), 400
    if not _can_access(shop):
        return jsonify({"error": "Do'kon topilmadi yoki ruxsat yo'q"}), 403
    raw = data.get("returnItems")
    if not isinstance(raw, list) or not raw:
        return jsonify({"error": "returnItems bo'sh"}), 400
    items = []
    for it in raw:
        if not isinstance(it, dict):
            return jsonify({"error": "returnItems noto'g'ri"}), 400
        try:
            sku_id = int(it.get("skuId"))
            amount = int(it.get("amount") or 0)
        except (TypeError, ValueError):
            return jsonify({"error": "skuId/amount raqam bo'lishi kerak"}), 400
        if sku_id <= 0 or amount <= 0:
            return jsonify({"error": "skuId va amount > 0 bo'lishi kerak"}), 400
        items.append(it)  # to'liq obyektni AYNAN saqlaymiz (portal echo qiladi)
    try:
        result = client.create_return(shop, items)
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    if not result or not result.get("id"):
        return jsonify({"error": "Возврат yaratilmadi (javob bo'sh)"}), 502
    return jsonify({"return": result})


@postavki_bp.post("/postavki/api/return/<int:return_id>/cancel")
@login_required
def postavki_return_cancel_api(return_id: int):
    """Возвратни BEKOR QILADI. ⚠️ Uzum'da haqiqiy bekor qiladi.

    body: {shop} → {return:{...status:CANCELED}}. Faqat CREATED bekor qilinadi.
    """
    data = request.get_json(silent=True) or {}
    shop = str(data.get("shop") or "").strip()
    if not shop:
        return jsonify({"error": "shop kerak"}), 400
    if not _can_access(shop):
        return jsonify({"error": "Do'kon topilmadi yoki ruxsat yo'q"}), 403
    try:
        result = client.cancel_return(shop, return_id)
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"return": result})
