"""FBS / DBS orders blueprint.

Surfaces Uzum FBS/DBS orders to the seller via the per-user Seller
OpenAPI token (``User.uzum_openapi_token``). Read-only in Stage 1; later
stages add confirm/cancel/label actions and DB caching.

Architecture: this module imports from ``core.fbs_data`` only, never
``core.uzum_openapi`` directly. Stage 4 will swap ``fbs_data`` to read
from a PostgreSQL ``fbs_orders`` table — this file won't change.
"""
from __future__ import annotations

import base64
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Blueprint, Response, render_template, request, session
from flask_login import current_user, login_required
from sqlalchemy import select, func, or_

from extensions import SessionLocal
from models import FbsOrder, Shop, User, Variant, ProductGroup
from core.auth_helpers import _json_response, _user_shop_ids
from core.fbs_data import (
    get_fbs_orders,
    get_fbs_orders_for_shops,
    get_fbs_count,
    get_fbs_counts_for_shops,
    get_fbs_order_detail,
    get_last_synced_at,
    get_last_synced_at_for_shops,
    invalidate_fbs_cache,
    # Stage 2 action seams.
    confirm_order,
    cancel_order,
    attach_identifiers,
    get_label_pdfs,
    get_return_reasons,
    # Bosqich 5b — bulk seam (label merge).
    merge_label_pdfs,
    # Bosqich 5c — QR-only crop variant.
    crop_label_to_qr,
    # Bosqich 5d — product QR generator (label + product QR per item).
    render_product_qr_pdf,
    # Bosqich 5e — re-styled shipping label with bigger left-side text.
    render_enlarged_label_pdf,
    # Stage 3 DBS action seams.
    dbs_delivering,
    dbs_completed,
    dbs_refund,
    UzumAPIError,
    FBS_ORDER_STATUSES,
    FBS_ORDER_SCHEMES,
)
# Bosqich 5f — DB label cache (instant bulk print, fed by the sync worker's
# background warm). See core/fbs_label_cache.py + models.py::FbsOrderLabel.
from core.fbs_label_cache import get_cached_label, store_label, fetch_label_live
# Bosqich 2 (async bulk-confirm) — Redis-backed per-job progress so the
# browser polls instead of holding a ~60s request open. See core/fbs_bulk_jobs.py.
from core.fbs_bulk_jobs import new_job, get_job, record_result, finish_job


def _warm_labels_async(token, user_id, order_ids):
    """Fire-and-forget: warm the label cache for orders we JUST confirmed.

    The moment an order is confirmed (CREATED→PACKING) its label becomes
    available and immutable (verified 2026-06-12), so we fetch it RIGHT AWAY on
    a background thread — the seller's later "Yorliq" print is then instant
    without waiting up to 10 min for the sync-worker warm. Paced (1s/token gate)
    and best-effort: any miss just falls back to the periodic sync warm. Runs off
    the request thread so it never delays the confirm response.
    """
    ids = [str(i) for i in (order_ids or [])]
    if not token or not user_id or not ids:
        return

    def _run():
        for oid in ids:
            try:
                if get_cached_label(user_id, oid, size="LARGE") is not None:
                    continue
                pdf = fetch_label_live(token, oid, size="LARGE")
                if pdf:
                    store_label(user_id, oid, "LARGE", pdf)
            except Exception as e:
                print(f"[label-warm-onconfirm] order {oid}: {e!r}")

    threading.Thread(target=_run, daemon=True).start()


# ── Uzum error code → bilingual user-facing message ──────────────────
# Keys mirror the ``seller-order-NN`` codes Uzum returns in errors[].
# Sub-dicts are keyed by session language ("uz"/"ru"). When Uzum returns
# an unknown code we fall back to the raw Russian message Uzum sent; if
# that's also empty, a generic "HTTP {n}" string (also localized).
FBS_ERROR_MESSAGES: dict[str, dict[str, str]] = {
    "uz": {
        "seller-order-01": "Buyurtma topilmadi",
        "seller-order-02": "Bu harakatga buyurtma statusi mos kelmaydi",
        "seller-order-03": "Tasdiqlash muddati o'tib ketgan",
        "seller-order-05": "Mahsulot uchun identifikator turi belgilanmagan",
        "seller-order-06": "Juda ko'p identifikator yuborildi",
        "seller-order-07": "Identifikator qiymati noto'g'ri",
        "seller-order-08": "Identifikator boshqa SKU'ga tegishli",
        "seller-order-09": "Identifikator turi ko'rsatilmagan",
        "seller-order-10": "Ombor (WMS) tomondan kutilmagan xato",
        "seller-order-11": "Identifikator xizmati vaqtincha ishlamayapti, keyinroq urinib ko'ring",
        "seller-order-12": "Bekor qilish sababi noto'g'ri",
        "seller-order-13": "Buyurtma allaqachon bekor qilingan",
        "seller-order-14": "Yorliq xizmati vaqtincha ishlamayapti, keyinroq urinib ko'ring",
        "seller-order-15": "Buyurtma uchun identifikatorlar yetishmaydi",
        "seller-order-36": "Buyurtma pozitsiyasi topilmadi",
        # ── Stage 3 DBS-specific error codes ───────────────────────────
        "seller-order-37": "Tasdiqlash kodi topilmadi — mijoz oldidagi kodni kiriting",
        "seller-order-38": "Tasdiqlash kodi noto'g'ri",
        "seller-order-39": "Tasdiqlash kodi noto'g'ri — birozdan keyin qayta urinib ko'ring",
        "fbs-18-invalid-order-type": "Bu amal faqat DBS buyurtmalar uchun",
        # ── Bosqich 7 (накладная) error codes ─────────────────────────────
        "seller-order-19": "Yuk xati topilmadi (avval yaratilmagan)",
        "seller-order-23": "Tanlangan vaqt slot mavjud emas — boshqa slot tanlang",
        "seller-order-04": "Yuk xati pozitsiyasi topilmadi",
        "fbs-2-seller-access-denied": "Sotuvchida bu amal uchun ruxsat yo'q",
        "fbs-19-time-is-up": "Yetkazib berish muddati o'tib ketgan",
        "fbs-20-incompatible-dimensional-groups": "Buyurtmalar gabarit guruhi mos kelmaydi",
        "fbs-24-invoice-wrong-order-status": (
            "Ba'zi buyurtmalar holati o'zgargan (allaqachon boshqa yuk xati ichiga tushgan?) — "
            "«Yangilash» tugmasini bosing va qayta urinib ko'ring"
        ),
    },
    "ru": {
        "seller-order-01": "Заказ не найден",
        "seller-order-02": "Статус заказа не подходит для этого действия",
        "seller-order-03": "Срок подтверждения истёк",
        "seller-order-05": "Для товара не задан тип идентификатора",
        "seller-order-06": "Отправлено слишком много идентификаторов",
        "seller-order-07": "Неверное значение идентификатора",
        "seller-order-08": "Идентификатор принадлежит другому SKU",
        "seller-order-09": "Тип идентификатора не указан",
        "seller-order-10": "Непредвиденная ошибка со стороны склада (WMS)",
        "seller-order-11": "Сервис идентификаторов временно недоступен, повторите позже",
        "seller-order-12": "Неверная причина отмены",
        "seller-order-13": "Заказ уже отменён",
        "seller-order-14": "Сервис этикеток временно недоступен, повторите позже",
        "seller-order-15": "Для заказа не хватает идентификаторов",
        "seller-order-36": "Позиция заказа не найдена",
        # ── Stage 3 DBS-specific error codes ───────────────────────────
        "seller-order-37": "Код подтверждения не найден — введите код, который у клиента",
        "seller-order-38": "Неверный код подтверждения",
        "seller-order-39": "Неверный код подтверждения — повторите чуть позже",
        "fbs-18-invalid-order-type": "Это действие доступно только для заказов DBS",
        # ── Bosqich 7 (накладная) error codes ─────────────────────────────
        "seller-order-19": "Накладная не найдена (ещё не создана)",
        "seller-order-23": "Выбранный временной слот недоступен — выберите другой слот",
        "seller-order-04": "Позиция накладной не найдена",
        "fbs-2-seller-access-denied": "У продавца нет прав на это действие",
        "fbs-19-time-is-up": "Срок доставки истёк",
        "fbs-20-incompatible-dimensional-groups": "Габаритные группы заказов несовместимы",
        "fbs-24-invoice-wrong-order-status": (
            "Статус некоторых заказов изменился (возможно, уже попали в другую накладную) — "
            "нажмите «Обновить» и повторите попытку"
        ),
    },
}


# ── Recurring inline action error strings → bilingual ────────────────
# Every full-Uzbek ``{"error": "..."}`` a seller can hit on the FBS/DBS
# action endpoints, keyed by a short snake_case key. ``{}`` placeholders
# are filled via ``_err(key, **fmt)`` (str.format). Russian translations
# match the page-level localization already shipped.
ACTION_ERRORS: dict[str, dict[str, str]] = {
    "uz": {
        "token_not_set": "Uzum OpenAPI token o'rnatilmagan",
        "token_not_set_long": "Uzum OpenAPI token o'rnatilmagan. «Mening do'konlarim» (/fetch) sahifasidan tokenni kiriting.",
        "seller_id_missing": "Uzum seller ID hali aniqlanmagan — moliya sinxroni bir marta ishlashi kerak",
        "order_not_found_or_denied": "Buyurtma topilmadi yoki ruxsat yo'q",
        "pick_cancel_reason": "Bekor qilish sababini tanlang",
        "id_list_empty": "Identifikator ro'yxati bo'sh",
        "order_ids_empty": "order_ids ro'yxati bo'sh yoki noto'g'ri",
        "order_ids_no_valid": "order_ids ro'yxatida yaroqli ID yo'q",
        "bulk_limit": "Bir martada {n} tadan ko'p buyurtma tanlash mumkin emas",
        "orders_not_found_synced": "Buyurtmalar topilmadi yoki sinxronlanmagan: {sample}",
        "order_no_access": "Buyurtmaga ruxsat yo'q: №{oid}",
        "no_item": "tovar yo'q",
        "no_labels": "Birorta yorliq olinmadi: {first_err}",
        "pdf_merge_error": "PDF birlashtirishda xato (pypdf merge)",
        "only_created_confirm": "Faqat CREATED holatdagi buyurtmalarni tasdiqlash mumkin",
        "bulk_job_unknown": "Jarayon topilmadi (muddati o'tgan yoki mavjud emas)",
        "field_required": "{field} majburiy",
        "no_shop": "Sizda biron do'kon topilmadi",
        "some_orders_not_yours": "Ba'zi buyurtmalar topilmadi yoki sizga tegishli emas",
        "mixed_shops_invoice": "Bitta yuk xati ichiga turli do'konlardan buyurtmalarni qo'shib bo'lmaydi",
        "only_packing_invoice": "Faqat «Yig'ilmoqda» (PACKING) buyurtmalar yuk xati ichiga qo'shiladi. Xato: {n} ta",
        # Uzum bu buyurtmani `seller-order-15 identifiers are missing` bilan rad
        # etadi. Ilgari buni faqat Uzum javobidan bilardik (bekor so'rov + tushunarsiz
        # ruscha xato). Endi oldindan to'sib, nima qilish kerakligini aytamiz.
        "identifiers_missing_invoice": "Yuk xati yaratilmadi: {orders} buyurtma(lar) uchun {type} kiritilmagan. Buyurtmani ochib, kodlarni kiriting.",
        "identifier_type_IMEI": "IMEI",
        "identifier_type_ASL_BELGISI": "ASL Belgisi",
        "identifier_type_UNKNOWN": "identifikator",
        "seller_id_undetected": "Seller ID avtomatik aniqlanmadi (finance ma'lumoti hali yo'q yoki Uzum vaqtincha javob bermadi). Birozdan keyin qayta urinib ko'ring yoki «Mening do'konlarim» sahifasida ?sId=<N> ni kiriting.",
        "seller_id_undetected_short": "Seller ID avtomatik aniqlanmadi (finance ma'lumoti hali yo'q). «Mening do'konlarim» sahifasida Uzum kabinet URL'idagi ?sId=<N> qiymatini kiriting.",
        "invoice_fetch_error": "Yuk xati ma'lumotini olishda xatolik — birozdan keyin qayta urinib ko'ring",
        "invoice_not_yours": "Bu yuk xati sizning do'konlaringizga tegishli emas",
        "invoice_no_orders": "Bu yuk xati uchun buyurtmalar topilmadi (yoki sizga tegishli emas)",
        "no_invoice_selected": "Yuk xati tanlanmadi",
        "bulk_akt_limit": "Bir vaqtda {n} tagacha akt birlashtiriladi. Kamroq tanlang.",
        "no_akt_loaded": "Hech bir akt yuklanmadi. Birozdan keyin urinib ko'ring.",
        "no_sku_to_update": "Yangilash uchun SKU yuborilmadi",
        "ownership_check_failed": "Egalik tekshiruvi vaqtincha ishlamadi, qayta urinib ko'ring",
        "no_valid_sku": "Yangilash uchun yaroqli SKU topilmadi",
        "foreign_sku_update": "{n} ta SKU sizning do'konlaringizga tegishli emas — yangilash rad etildi",
        "foreign_sku_save": "{n} ta SKU sizning do'konlaringizga tegishli emas — saqlash rad etildi",
        "no_file": "Fayl yuborilmadi",
        "only_xlsx": "Faqat .xlsx fayl qabul qilinadi",
        "file_unreadable": "Faylni o'qib bo'lmadi — format noto'g'ri",
        "no_valid_rows": "Faylda yaroqli qator topilmadi",
        "no_changes_to_save": "Saqlash uchun o'zgarish yo'q",
        "issue_code_numeric": "Tasdiqlash kodi raqam bo'lishi kerak",
    },
    "ru": {
        "token_not_set": "Токен Uzum OpenAPI не задан",
        "token_not_set_long": "Токен Uzum OpenAPI не задан. Введите токен на странице «Мои магазины» (/fetch).",
        "seller_id_missing": "Uzum seller ID ещё не определён — должна один раз отработать синхронизация финансов",
        "order_not_found_or_denied": "Заказ не найден или нет доступа",
        "pick_cancel_reason": "Выберите причину отмены",
        "id_list_empty": "Список идентификаторов пуст",
        "order_ids_empty": "Список order_ids пуст или некорректен",
        "order_ids_no_valid": "В списке order_ids нет ни одного корректного ID",
        "bulk_limit": "За один раз нельзя выбрать более {n} заказов",
        "orders_not_found_synced": "Заказы не найдены или не синхронизированы: {sample}",
        "order_no_access": "Нет доступа к заказу: №{oid}",
        "no_item": "нет товара",
        "no_labels": "Не удалось получить ни одной этикетки: {first_err}",
        "pdf_merge_error": "Ошибка объединения PDF (pypdf merge)",
        "only_created_confirm": "Подтверждать можно только заказы в статусе CREATED",
        "bulk_job_unknown": "Процесс не найден (истёк или не существует)",
        "field_required": "{field} обязателен",
        "no_shop": "У вас не найдено ни одного магазина",
        "some_orders_not_yours": "Некоторые заказы не найдены или не принадлежат вам",
        "mixed_shops_invoice": "Нельзя добавлять в одну накладную заказы из разных магазинов",
        "only_packing_invoice": "В накладную можно добавить только заказы в статусе «Сборка» (PACKING). Ошибка: {n} шт.",
        "identifiers_missing_invoice": "Накладная не создана: для заказа(ов) {orders} не указан {type}. Откройте заказ и введите коды.",
        "identifier_type_IMEI": "IMEI",
        "identifier_type_ASL_BELGISI": "Asl Belgisi",
        "identifier_type_UNKNOWN": "идентификатор",
        "seller_id_undetected": "ID продавца не определился автоматически (данных finance пока нет или Uzum временно не ответил). Повторите чуть позже или укажите ?sId=<N> на странице «Мои магазины».",
        "seller_id_undetected_short": "ID продавца не определился автоматически (данных finance пока нет). Укажите значение ?sId=<N> из URL кабинета Uzum на странице «Мои магазины».",
        "invoice_fetch_error": "Ошибка при получении данных накладной — повторите чуть позже",
        "invoice_not_yours": "Эта накладная не принадлежит вашим магазинам",
        "invoice_no_orders": "Для этой накладной заказы не найдены (или не принадлежат вам)",
        "no_invoice_selected": "Накладная не выбрана",
        "bulk_akt_limit": "За один раз объединяется до {n} актов. Выберите меньше.",
        "no_akt_loaded": "Не удалось загрузить ни один акт. Повторите чуть позже.",
        "no_sku_to_update": "Не отправлено ни одного SKU для обновления",
        "ownership_check_failed": "Проверка принадлежности временно не сработала, повторите попытку",
        "no_valid_sku": "Не найдено ни одного корректного SKU для обновления",
        "foreign_sku_update": "{n} SKU не принадлежат вашим магазинам — обновление отклонено",
        "foreign_sku_save": "{n} SKU не принадлежат вашим магазинам — сохранение отклонено",
        "no_file": "Файл не отправлен",
        "only_xlsx": "Принимается только файл .xlsx",
        "file_unreadable": "Не удалось прочитать файл — неверный формат",
        "no_valid_rows": "В файле не найдено ни одной корректной строки",
        "no_changes_to_save": "Нет изменений для сохранения",
        "issue_code_numeric": "Код подтверждения должен быть числом",
    },
}


def _session_lang() -> str:
    """Seller's session language, safe outside a request context (→ "uz")."""
    try:
        return session.get("lang", "uz")
    except Exception:
        return "uz"


def _err(key: str, lang: str | None = None, **fmt) -> str:
    """Resolve an ACTION_ERRORS key to the seller's language, formatted."""
    lang = lang or _session_lang()
    table = ACTION_ERRORS.get(lang, ACTION_ERRORS["uz"])
    s = table.get(key) or ACTION_ERRORS["uz"].get(key, key)
    return s.format(**fmt) if fmt else s


def _variant_titles(db, allowed_shop_db_ids, *, barcodes=None, sku_ids=None,
                    lang: str = "uz") -> tuple[dict, dict]:
    """Localized product titles from our own catalog (``variants``), so the
    FBS views can show the товар nomi in the seller's chosen language.

    The FBS API only carries a single-language title (and often only the SKU
    code), but the product sync stores both ``product_title_uz`` and
    ``product_title_ru`` per variant (Uzum exposes both via Accept-Language).
    We look those up by barcode and/or ``uzum_sku_id``, restricted at the SQL
    level to the user's OWN shops (a foreign seller's title can never leak in).

    Returns ``(by_barcode, by_sku_id)`` — both str-keyed, each value a tuple
    ``(title, ru_color)``. ``title`` is the RU one when ``lang == "ru"`` else
    UZ (UZ fallback when blank). ``ru_color`` is ``variants.color`` — Uzum's
    own RU attribute string (e.g. "Черный, 16" / "42, Манго"), used to render
    the colour value WITHOUT a hand-maintained dictionary. Either filter list
    may be empty; an empty result means "no local match — keep the caller's".
    """
    by_bc: dict[str, tuple] = {}
    by_sku: dict[str, tuple] = {}
    if not allowed_shop_db_ids:
        return by_bc, by_sku
    bcs = [str(b) for b in (barcodes or []) if b]
    sids = [str(s) for s in (sku_ids or []) if s]
    if not (bcs or sids):
        return by_bc, by_sku
    key_conds = []
    if bcs:
        key_conds.append(Variant.barcode.in_(bcs))
    if sids:
        key_conds.append(Variant.uzum_sku_id.in_(sids))
    rows = db.execute(
        select(
            Variant.barcode, Variant.uzum_sku_id,
            Variant.product_title_uz, Variant.product_title_ru,
            Variant.color,
        )
        .join(ProductGroup, Variant.group_id == ProductGroup.id)
        .where(ProductGroup.shop_id.in_(allowed_shop_db_ids))
        .where(or_(*key_conds))
    ).all()
    for bc, sku, t_uz, t_ru, color in rows:
        title = (t_ru if lang == "ru" else t_uz) or t_uz or t_ru
        if not title:
            continue
        val = (title, (color or "").strip())
        if bc and str(bc) not in by_bc:
            by_bc[str(bc)] = val
        if sku and str(sku) not in by_sku:
            by_sku[str(sku)] = val
    return by_bc, by_sku


import re as _re

# Variant-attribute LABELS Uzum bakes into the SKU title, e.g.
# "… (O'lcham: 2.5sm)" / "… (Rang: Qora, O'lcham: 17)". When we swap the base
# name to its RU catalog title we keep this suffix (it's what tells two
# variants apart) and translate the labels + known colour VALUES. Keys are
# normalized: apostrophes folded to ASCII «'», lower-cased.
# Exact-match labels for the non-size / non-colour cases. Size («… o'lcham(i/lari)»,
# razmer, size) and colour («rang(i)», tsvet, color) are matched by SUBSTRING in
# _tr_attr_label so EVERY variant works — "Kiyim o'lchami", "Uzuk o'lchamlari",
# "Poyabzal o'lchami" all → «Размер» without enumerating them.
_ATTR_LABEL_RU = {
    "material": "Материал", "materiali": "Материал",
    "model": "Модель", "modeli": "Модель",
    "hajm": "Объём", "hajmi": "Объём", "vazn": "Вес", "vazni": "Вес",
}


def _tr_attr_label(label: str) -> str:
    """Translate a variant-attribute label to RU. Size/colour are matched by
    substring (handles any "<noun> o'lchami" / "<noun> rangi" form); the rest
    fall back to an exact map, else verbatim."""
    ln = _norm_apos(label).strip().lower()
    # "cham" covers o'lcham / o'lchami / o'lchamlari / kiyim o'lchami AND the
    # seller's real typo "o'lacham" — all → Размер without enumerating them.
    if "cham" in ln or "razmer" in ln or ln == "size":
        return "Размер"
    if "rang" in ln or "tsvet" in ln or ln in ("color", "cvet"):
        return "Цвет"
    return _ATTR_LABEL_RU.get(ln, label.strip())
# Common Uzbek colour/attribute VALUES → RU (cross-checked against the
# seller's own RU SKU-code segments: ЧЕРН/ЗОЛОТ/РЫЖИЙ/АЛЫЙ/БЕЖЕВ/ГОЛУБ…).
# Unknown values are left verbatim — never guessed.
_ATTR_VALUE_RU = {
    "qora": "Чёрный", "oq": "Белый", "qizil": "Красный", "ko'k": "Синий",
    "moviy": "Синий", "havorang": "Голубой", "havo rang": "Голубой",
    "yashil": "Зелёный", "sariq": "Жёлтый", "to'q sariq": "Оранжевый",
    "pushti": "Розовый", "binafsha": "Фиолетовый", "jigarrang": "Коричневый",
    "kulrang": "Серый", "tilla": "Золотой", "tillarang": "Золотой",
    "oltin": "Золотой", "kumush": "Серебристый", "kumushrang": "Серебристый",
    "bej": "Бежевый", "bordo": "Бордовый", "alvon": "Алый", "malla": "Рыжий",
    "indigo": "Индиго", "firuza": "Бирюзовый", "feruza": "Бирюзовый",
    "gilos": "Вишнёвый", "shaffof": "Прозрачный",
}
_APOS_RE = _re.compile("[ʻʼ‘’`´]")


def _norm_apos(s: str) -> str:
    """Fold every apostrophe variant (ʻ ʼ ‘ ’ ` ´) to ASCII «'»."""
    return _APOS_RE.sub("'", s or "")


def _tr_attr_value(v: str) -> str:
    """Translate a known Uzbek colour value to RU; normalize size units
    («2.5sm»/«40cm»→«… см», «10mm»→«10 мм»). Unknown values pass through."""
    key = _norm_apos(v).strip().lower()
    if key in _ATTR_VALUE_RU:
        return _ATTR_VALUE_RU[key]
    out = _re.sub(r"(\d[\d.,]*)\s*(?:sm|cm)\b", r"\1 см", v.strip(), flags=_re.IGNORECASE)
    out = _re.sub(r"(\d[\d.,]*)\s*mm\b", r"\1 мм", out, flags=_re.IGNORECASE)
    return out


# A size-like token inside variants.color: a pure number, a number+unit
# (2.5sm / 10мм / 3.5см / 40cm), or a clothing size (S/M/L/XL/2XL/XXXL…).
# These are dropped so only the colour word(s) remain — independent of how the
# size is formatted in the FBS suffix (sm vs см mismatch no longer matters).
_SIZE_TOKEN_RE = _re.compile(
    r"^(?:\d+(?:[.,]\d+)?\s*(?:sm|cm|см|mm|мм)?|xs|s|m|l|xl|xxl|xxxl|\d+xl)$",
    _re.IGNORECASE,
)


def _ru_color_value(uz_value: str, ru_color: str) -> str:
    """RU colour value, preferring Uzum's own ``variants.color`` string.

    ``ru_color`` (e.g. "Черный, 16" / "42, Манго" / "Золотой, 10мм") bundles
    colour + size in any order/format. We drop every size-like token and keep
    the rest → the pure RU colour, for ANY colour Uzum knows (no hand list).
    Falls back to the small ``_ATTR_VALUE_RU`` map, then the verbatim value.
    """
    if ru_color:
        kept = [t.strip() for t in ru_color.split(",")
                if t.strip() and not _SIZE_TOKEN_RE.match(t.strip())]
        if kept:
            return ", ".join(kept)
    key = _norm_apos(uz_value).strip().lower()
    return _ATTR_VALUE_RU.get(key, _tr_attr_value(uz_value))


def _localize_product_title(orig: str, ru_base: str | None,
                            ru_color: str = "", lang: str = "uz") -> str:
    """Localized product title for an item.

    UZ (or no RU match) → the ORIGINAL FBS title verbatim (never a regression;
    the FBS title already carries the per-variant suffix). RU → the RU catalog
    base name + the trailing "(Label: value, …)" attribute suffix, kept so
    size/colour variants stay distinguishable. Labels are translated by
    substring; the COLOUR value comes from Uzum's own ``variants.color``
    (``ru_color``) with the size tokens stripped; sizes stay verbatim
    («Nsm»→«N см»). Handles multiple comma-separated attrs + any apostrophe.
    """
    orig = orig or ""
    if lang != "ru" or not ru_base:
        return orig
    # Last parenthetical (no nested parens) at the very end = the attr suffix.
    m = _re.search(r"\s*\(([^()]*)\)\s*$", orig)
    if not m or ":" not in m.group(1):
        return ru_base
    out = []
    for chunk in m.group(1).split(","):
        if ":" not in chunk:
            out.append(_tr_attr_value(chunk.strip()))
            continue
        label, _, value = chunk.partition(":")
        label, value = label.strip(), value.strip()
        ln = _norm_apos(label).lower()
        if "rang" in ln or "tsvet" in ln or ln in ("color", "cvet"):
            out.append(f"{_tr_attr_label(label)}: {_ru_color_value(value, ru_color)}")
        else:
            out.append(f"{_tr_attr_label(label)}: {_tr_attr_value(value)}")
    return f"{ru_base} ({', '.join(out)})"


# ── List-page i18n bundle (Stage 4d) ─────────────────────────────────
# Strings the /fbs orders page renders that aren't covered by
# STATUS_LABELS. Mostly the Yangilash button states, toast messages,
# and the "Oxirgi yangilangan" relative-time indicator.
LIST_LABELS: dict[str, dict[str, str]] = {
    "uz": {
        "btn_refresh":        "Yangilash",
        "btn_refresh_active": "Yangilanmoqda…",
        "toast_refreshed":    "Yangilandi",
        "toast_refresh_error":"Yangilashda xato",
        "last_synced_prefix": "Oxirgi yangilangan: ",
        "auto_sync_note":     " · Avtomatik har 10 daqiqada",
        "just_now":           "hozirgina",
        "minutes_ago":        " daq oldin",
        "hours_ago":          " soat oldin",
        "yesterday":          "kecha",
        "older":              "ancha avval",
        "never_synced":       "Hali sync qilinmagan",
        "aria_close":         "Yopish",
        "toast_rate_limited": "Yuklanmoqda, iltimos kuting…",
        # Search + date filter UI
        "label_search":       "Qidirish",
        "ph_search":          "Mahsulot yoki SKU…",
        "label_date_from":    "Sana (dan)",
        "label_date_to":      "Sana (gacha)",
        "btn_clear_filters":  "Tozalash",
        "filter_active":      "Filtrlangan",
        # Bosqich 5b — bulk-select toolbar
        "bulk_n_selected":           "ta tanlandi",
        "bulk_btn_print":            "Yorliq chop etish",
        "bulk_btn_print_qr":         "QR chop etish",
        "bulk_btn_print_with_qr":    "Yorliq + tovar QR",
        "bulk_btn_print_qr_with_label": "QR + Yorliq",
        "bulk_btn_postavka":         "Yuk xati yaratish",
        "bulk_postavka_hint":        "Tanlangan buyurtmalar uchun yuk xati yaratish — qabul punkti va vaqt tanlanadi",
        # Drop-off points modal
        "dropoff_btn":               "Qabul punktlari",
        "dropoff_btn_hint":          "Buyurtmalarni qaysi Uzum punktiga olib borish kerakligini ko'rish",
        "dropoff_modal_title":       "Qabul punktlari",
        "dropoff_loading":           "Yuklanmoqda…",
        "dropoff_empty":             "Hozircha qabul punkti topilmadi. Yangi buyurtmalar kelganda ro'yxat to'ladi.",
        "dropoff_load_fail":         "Punktlar ro'yxatini olishda xato",
        "dropoff_shops_prefix":      "Do'konlar:",
        "dropoff_active_hint":       "Eltishi kerak bo'lgan faol buyurtmalar (Yig'ilmoqda + Jo'natishga tayyor)",
        "dropoff_total_hint":        "Shu punktga tegishli barcha buyurtmalar",
        "dropoff_copy":              "Manzilni nusxalash",
        "dropoff_copied":            "Nusxalandi ✓",
        "dropoff_copy_fail":         "Nusxalashda xato",
        "dropoff_geo_btn":           "Joylashuvni so'rash",
        "dropoff_geo_busy":          "Aniqlanmoqda…",
        "dropoff_geo_refresh":       "Joylashuvni yangilash",
        "dropoff_geo_ok":            "Joylashuv aniqlandi — yaqin punktlar tepada",
        "dropoff_geo_denied":        "Brauzer joylashuvga ruxsat bermadi",
        "dropoff_geo_fail":          "Joylashuvni olib bo'lmadi",
        "dropoff_geo_unsupported":   "Brauzer geolokatsiyani qo'llab-quvvatlamaydi",
        "dropoff_geo_insecure":      "Geolokatsiya faqat HTTPS yoki localhost'da ishlaydi",
        "dropoff_src_db":            "Mening punktlarim",
        "dropoff_src_uzum":          "Uzum'dan to'liq",
        "dropoff_uzum_fallback":     "Faol buyurtma yo'q — eski ro'yxat ko'rsatildi",
        "dropoff_hours_today":       "Bugun",
        "dropoff_slots_btn":         "Slotlar",
        "dropoff_slots_hide":        "Slotlarni yashirish",
        "dropoff_slots_loading":     "Slotlar yuklanmoqda…",
        "dropoff_slots_empty":       "Bu punktda hozir bo'sh slot yo'q",
        "dropoff_slots_no_orders":   "Slot olish uchun avval CREATED buyurtmani tasdiqlang",
        "dropoff_slots_fail":        "Slotlarni olishda xato",
        "dropoff_search_ph":         "Manzil bo'yicha qidirish… (masalan: Yunusobod, Chilonzor)",
        "dropoff_search_empty":      "Bu qidiruv bo'yicha hech qaysi punkt topilmadi:",
        "dropoff_slot_click_hint":   "Yuk xati yaratish uchun bosing",
        "dropoff_match_only":        "Faqat mos keladigan",
        "dropoff_match_hint":        "Tanlangan buyurtmalar uchun mos vaqtlarnigina ko'rsatamiz. Hammasini ko'rish uchun — filtrni o'chiring",
        "invoice_no_dop":            "Punkt tanlanmagan",
        "invoice_no_packing":        "Yig'ilmoqda holatidagi buyurtma yo'q. Avval CREATED ni tasdiqlang.",
        "invoice_confirm_prompt":    "{n} ta buyurtmani {point} punktiga {slot} vaqtga jo'natamiz. Tasdiqlaysizmi?",
        "invoice_mode_created":      "Yuk xati yaratildi",
        "invoice_mode_updated":      "Yuk xati yangilandi",
        "invoice_create_fail":       "Yuk xati yaratishda xato",
        "invoice_multi_shop_warn":   "Faqat {n} ta buyurtma yuborilmoqda (do'kon bo'yicha eng katta guruh). Boshqa {other} ta buyurtma alohida yuk xati ichiga ketadi.",
        "dropoff_geo_unavailable":   "Joylashuv ma'lumoti mavjud emas (GPS/internet?)",
        "dropoff_geo_timeout":       "Joylashuvni aniqlash juda uzoq cho'zildi",
        "dropoff_dist_hint":         "Sizdan to'g'ri masofa (havoda)",
        "dropoff_nearest":           "EN YAQIN",
        "dropoff_fav_add":           "Sevimli sifatida belgilash",
        "dropoff_fav_remove":        "Sevimlidan olib tashlash",
        "dropoff_panel_hint":        "Slotlarni ko'rish uchun chapdan punkt tanlang",
        "dropoff_selected_point":    "Tanlangan punkt",
        "dropoff_pick_slot":         "Slotni tanlang",
        "dropoff_confirm_btn":       "Tasdiqlash",
        "dropoff_cap_hint":          "Bo'sh joy / jami sig'im",
        "_lang":                     "uz",
        "bulk_qr_size_label":        "QR o'lchami",
        "bulk_qr_size_uzum":         "Yorliq hajmi",
        "bulk_qr_size_hint":         "Tanlangan o'lcham «QR chop etish» va «Yorliq + tovar QR» da bir xil tovar QR yasaydi",
        "bulk_btn_confirm":          "Ishga olish",
        "bulk_btn_clear":            "Bekor qilish",
        "bulk_processing":           "Bajarilmoqda…",
        "bulk_confirm_only_created": "«Ishga olish» faqat «Yangi» (CREATED) buyurtmalarda ishlaydi",
        "bulk_label_only_packing":   "Yorliq hali tayyor emas — avval buyurtmani tasdiqlang",
        "bulk_confirm_prompt":       "{n} ta buyurtmani ishga olasizmi?",
        "bulk_confirm_title":        "Ishga olish",
        "invoice_confirm_title":     "Yuk xati yaratish",
        "confirm_cancel":            "Bekor qilish",
        "bulk_toast_labels_ok":      "ta yorliq tayyorlandi",
        "bulk_toast_labels_partial": "ta yorliq tayyorlandi, {n} tasi xato",
        "bulk_toast_labels_fail":    "Yorliqlarni olishda xato",
        "bulk_toast_confirm_ok":     "ta buyurtma tasdiqlandi",
        "bulk_toast_confirm_partial":"ta tasdiqlandi, {n} tasi xato",
        "bulk_toast_confirm_fail":   "Tasdiqlashda xato",
        # Bosqich 2 — fon (async) bulk-confirm + progress poll
        "bulk_async_started":        "{n} ta qabulga olindi, bajarilmoqda…",
        "bulk_async_progress":       "Qabul qilinmoqda… {done}/{total}",
        "bulk_async_done_ok":        "{n} ta buyurtma qabul qilindi",
        "bulk_async_done_partial":   "{ok} ta qabul qilindi, {fail} ta xato",
        "bulk_async_skipped":        "{n} tasi o'tkazib yuborildi (CREATED emas)",
        "bulk_select_all_aria":      "Hammasini tanlash",
        # ── Orders-page chrome (full RU coverage) ──
        "page_title_orders":         "Buyurtmalar",
        "aria_section_fbs":          "FBS bo'limi",
        "tab_orders":                "Buyurtmalar",
        "tab_stock":                 "Ombor",
        "label_shop":                "Do'kon",
        "opt_no_shop":               "— do'kon yo'q —",
        "opt_all_shops":             "Barcha do'konlar",
        "label_scheme":              "Sxema",
        "opt_scheme_all":            "FBS + DBS",
        "label_page_size":           "Sahifada",
        "warn_token_pre":            "Uzum Seller OpenAPI tokeni o'rnatilmagan. Buyurtmalarni ko'rish uchun avval",
        "link_my_shops":             "«Mening do'konlarim»",
        "warn_token_post":           " sahifasida token kiriting.",
        "delivery_nav_title":        "Yetkazib berish — yuk xatlari",
        "delivery_nav":              "Yetkazib berish",
        "tip_pick_order_first":      "Avval buyurtmani tanlang",
        "empty_pick_status":         "Buyurtmalarni ko'rish uchun status tanlang.",
        "more_statuses":             "Boshqa holatlar…",
        "meta_found":                "Topildi:",
        "meta_page":                 "Sahifa:",
        "meta_status":               "Status:",
        "qr_size_title":             "QR stiker o'lchami (mm)",
        "qr_size_aria":              "QR stiker o'lchami",
        "qr_size_uzum_full":         "Uzum (to'liq)",
        "bulk_yorliq_title":         "Tanlangan buyurtmalar yorlig'ini chop etish",
        "bulk_yorliq_btn":           "Yorliq",
        "bulk_qr_title":             "Tanlangan buyurtmalar QR kodini chop etish",
        "bulk_both_title":           "Yorliq va QR'ni bitta PDF'da chop etish",
        "bulk_both_btn":             "Yorliq + QR",
        "akt_download_title":        "Tanlangan aktlarni PDF qilib yuklab olish",
        "akt_download_btn":          "Yuklab olish",
        "akt_print_title":           "Tanlangan aktlarni birlashtirib chop etish",
        "akt_print_btn":             "Jo'natma akti (PDF)",
        "iframe_order_title":        "Buyurtma",
        "iframe_invoice_title":      "Yuk xati",
        "cd_overdue":                "Muddat o'tdi",
        "cd_hours_left":             "{n} soat qoldi",
        "cd_minutes_left":           "{n} daq qoldi",
        "empty_no_orders":           "Buyurtmalar topilmadi.",
        "empty_no_orders_hint":      "Boshqa status tanlang yoki sxemani o'zgartiring.",
        "row_sku_prefix":            "SKU: ",
        "row_barcode_prefix":        "Shtrix: ",
        "unit_pcs":                  "dona",
        "deadline_tip":              "Qabul punktiga topshirish muddati",
        "aria_order_no":             "Buyurtma №",
        "th_product":                "Mahsulot",
        "th_scheme":                 "Sxema",
        "th_created":                "Yaratildi",
        "th_deadline":               "Muddat",
        "th_shop":                   "Do'kon",
        "th_sum":                    "Summa",
        "prod_collapse":             "Yashirish",
        "loading":                   "Yuklanmoqda…",
        "err_prefix":                "Xatolik:",
        "net_err_prefix":            "Tarmoq xatosi:",
        "net_err":                   "Tarmoq xatosi.",
        "uzum_busy":                 "Uzum hozir band. Bir-ikki soniyadan keyin qayta urinib ko'ring.",
        "uzum_busy_short":           "Uzum hozir band. Birozdan keyin urinib ko'ring.",
        "inv_empty":                 "Yuk xati yo'q.",
        "inv_empty_hint":            "Jo'natishga tayyor yuk xati topilmadi.",
        "inv_err_prefix":            "Xato:",
        "inv_count_tip":             "Qabul qilingan / Jami",
        "pick_order_first":          "Avval buyurtma belgilang",
        "no_label_dbs":              "DBS buyurtmaga jo'natma yorlig'i yo'q — QR'dan foydalaning",
        "dbs_skipped":               "{n} ta DBS o'tkazib yuborildi (yorliq faqat FBS uchun)",
        "labels_preparing":          "{n} ta yorliq tayyorlanmoqda…",
        "labels_fetch_err":          "Yorliqlarni olishda xato:",
        "labels_ready_some_err":     "Yorliqlar tayyor ({n} tasi xato) — chop oynasi ochilmoqda",
        "print_window_opening":      "Chop oynasi ochilmoqda…",
        "qr_print_opening":          "QR chop oynasi ochilmoqda…",
        "shop_not_selected":         "Do'kon tanlanmagan.",
        "shoppick_count_suffix":     "ta",
        "no_packing_among_sel":      "Belgilangan buyurtmalar orasida «Yig'ilmoqda» holatidagisi yo'q",
        "dropoff_match_empty":       "Hozircha mos keladigan punkt yo'q — «Faqat mos keladigan»ni o'chirib barchasini ko'ring.",
        "slot_overdue":              "⏰ Tanlangan buyurtma(lar)dan birining yetkazish muddati o'tgan — Uzum bunga slot bermaydi. Ro'yxatdan muddati o'tmagan (qizil taymersiz) buyurtmalarni tanlab qayta urinib ko'ring.",
        # ── Invoices-page chrome (full RU coverage) ──
        "inv_page_title":            "Yuk xatlari",
        "inv_intro":                 "Sizning FBS «Yuk xatlari» ro'yxatingiz. Yaratish, qabul holati, bekor qilish va detallarni shu yerdan ko'rasiz. Ma'lumotlar Uzum Seller OpenAPI orqali to'g'ridan-to'g'ri olinadi.",
        "warn_token_pre_inv":        "Uzum Seller OpenAPI tokeni o'rnatilmagan. Yuk xatlari ro'yxatini ko'rish uchun",
        "inv_filter_status_aria":    "Status bo'yicha filtr",
        "inv_status_all":            "Hamma statuslar",
        "inv_status_created":        "Yaratilgan",
        "inv_status_acceptance":     "Qabul jarayonida",
        "inv_status_accepted":       "Qabul qilindi",
        "inv_status_cancelled":      "Bekor qilindi",
        "inv_check_all_aria":        "Hammasini belgilash",
        "th_num":                    "№",
        "th_date":                   "Sana",
        "th_stock":                  "Ombor",
        "th_orders":                 "Buyurtmalar",
        "th_price":                  "Narx",
        "th_address":                "Manzil",
        "th_slot":                   "Slot",
        "th_status":                 "Status",
        "pager_prev":                "Oldingi",
        "pager_next":                "Keyingi",
        "inv_both_title_full":       "Yorliq va QR'ni bitta PDF'da chop etish (avval jo'natma yorlig'i, keyin har tovar QR)",
        "row_select_aria":           "Belgilash",
        "row_unselectable_aria":     "Belgilab bo'lmaydi",
        "row_unselectable_title":    "Faqat «Yaratilgan» yuk xatlarini belgilab, ko'p akt chop etish mumkin",
        "uzum_retrying":             "Uzum band — qayta urinilmoqda…",
        "inv_no_response_strong":    "Uzum javob bermadi.",
        "inv_try_later":             "Birozdan keyin qayta urinib ko'ring.",
        "inv_empty_status":          "Bu statusda yuk xatlari yo'q",
        "inv_no_more":               "Boshqa yuk xatlari yo'q",
        "inv_refreshing":            "Yangilanmoqda…",
        "inv_no_items":              "Mahsulot ma'lumotlari topilmadi",
        "inv_barcode_label":         "Shtrix",
        "inv_rail_total":            "Jami",
        "inv_th_dona":               "Dona",
        "inv_fact_slot":             "Vaqt slot",
        "inv_fact_created":          "Yaratilgan",
        "inv_fact_fullsum":          "To'liq summa",
        "inv_fact_accept_start":     "Qabul boshlandi",
        "inv_fact_accept_done":      "Qabul tugadi",
        "inv_akt_short":             "Jo'natma akti",
        "inv_pickup":                "Qabul punkti",
        "inv_pickup_edit_title":     "Qabul punkti va vaqt slotini o'zgartirish",
        "inv_ord_selectall_title":   "Barchasini tanlash",
        "inv_orders_empty":          "Bu yuk xati uchun buyurtmalar topilmadi (eski yoki sinxron qilinmagan bo'lishi mumkin)",
        "inv_busy_no_response":      "Uzum javob bermadi (band). Birozdan keyin qayta urinib ko'ring.",
        "akt_unavailable":           "Akt mavjud emas yoki olishda xato:",
        "akt_fetch_err":             "Akt olishda xato:",
        "akt_some_skipped":          "Diqqat: ba'zi aktlar olinmadi (id: {ids}). Qolganlari PDFda. Birozdan keyin qayta urinib ko'ring.",
        "akt_bulk_cap":              "Bir vaqtda {n} tagacha akt birlashtiriladi. Kamroq tanlang.",
        "inv_preparing":             "Tayyorlanmoqda…",
        "inv_overdue_no_slot":       "⏰ Bu buyurtma muddati o'tgan — unga mos slot yo'q.",
        "inv_show_all_times":        "Hamma vaqtni ko'rsatish",
        "inv_overdue_short":         "⏰ Buyurtma muddati o'tgan.",
        "inv_no_active_orders":      "Aktiv buyurtma yo'q",
        "inv_no_invoice_selected":   "Yuk xati tanlanmagan",
        "inv_point_not_picked":      "Punkt tanlanmadi",
        "inv_slot_invalid":          "Bu slot yaroqsiz (uuid yo'q) — boshqa slot tanlang",
        "inv_change_confirm":        "Yuk xati №{num} qabul punktini «{point}»{slot} ga o'zgartirasizmi?",
        "dropoff_change_title":      "Qabul punktini o'zgartirish",
        "dropoff_change_title_pre":  "Qabul punktini ",
        "dropoff_change_title_grad": "o'zgartirish",
        "dropoff_change_btn":        "O'zgartirish",
        "inv_change_err":            "O'zgartirishda xato",
        "inv_slot_too_late":         "Tanlangan slot juda kech — bu yuk xati uchun ERTAROQ slot tanlang. ",
        "inv_uzum_busy_retry":       "Uzum hozir band — bir-ikki soniyadan keyin qayta urinib ko'ring.",
        "inv_pickup_changed":        "✅ Qabul punkti o'zgartirildi",
        # ── Shop-pick modal (_fbs_shoppick_modal.html) ──
        "shoppick_title":            "Turli do'kondan buyurtma belgilandi",
        "shoppick_msg":              "Bitta yuk xati — bitta do'kon uchun. Qaysi do'kon uchun yaratamiz?",
        "shoppick_cancel":           "Bekor",
        "shoppick_ok":               "Davom etish",
    },
    "ru": {
        "btn_refresh":        "Обновить",
        "btn_refresh_active": "Обновление…",
        "toast_refreshed":    "Обновлено",
        "toast_refresh_error":"Ошибка обновления",
        "last_synced_prefix": "Последнее обновление: ",
        "auto_sync_note":     " · Автоматически каждые 10 минут",
        "just_now":           "только что",
        "minutes_ago":        " мин назад",
        "hours_ago":          " ч назад",
        "yesterday":          "вчера",
        "older":              "давно",
        "never_synced":       "Ещё не синхронизировано",
        "aria_close":         "Закрыть",
        "toast_rate_limited": "Загружается, подождите…",
        # Search + date filter UI
        "label_search":       "Поиск",
        "ph_search":          "Товар или SKU…",
        "label_date_from":    "Дата (с)",
        "label_date_to":      "Дата (по)",
        "btn_clear_filters":  "Очистить",
        "filter_active":      "Фильтр",
        # Bosqich 5b — bulk-select toolbar
        "bulk_n_selected":           "выбрано",
        "bulk_btn_print":            "Печать этикеток",
        "bulk_btn_print_qr":         "Печать QR",
        "bulk_btn_print_with_qr":    "Этикетка + QR товара",
        "bulk_btn_print_qr_with_label": "QR + Этикетка",
        "bulk_btn_postavka":         "Создать поставку",
        "bulk_postavka_hint":        "Создать поставку (накладную) для выбранных заказов — выбор пункта приёма и времени",
        # Drop-off points modal
        "dropoff_btn":               "Пункт приёма",
        "dropoff_btn_hint":          "Куда отвезти заказы",
        "dropoff_modal_title":       "Пункты приёма",
        "dropoff_loading":           "Загрузка…",
        "dropoff_empty":             "Пока нет пунктов приёма. Список заполнится при появлении заказов.",
        "dropoff_load_fail":         "Ошибка загрузки списка",
        "dropoff_shops_prefix":      "Магазины:",
        "dropoff_active_hint":       "Активные заказы для доставки в этот пункт",
        "dropoff_total_hint":        "Все заказы, связанные с этим пунктом",
        "dropoff_copy":              "Скопировать адрес",
        "dropoff_copied":            "Скопировано ✓",
        "dropoff_copy_fail":         "Ошибка копирования",
        "dropoff_geo_btn":           "Запросить геолокацию",
        "dropoff_geo_busy":          "Определение…",
        "dropoff_geo_refresh":       "Обновить геолокацию",
        "dropoff_geo_ok":            "Геолокация определена — ближайшие сверху",
        "dropoff_geo_denied":        "Браузер не дал доступ к геолокации",
        "dropoff_geo_fail":          "Не удалось определить геолокацию",
        "dropoff_geo_unsupported":   "Браузер не поддерживает геолокацию",
        "dropoff_geo_insecure":      "Геолокация работает только по HTTPS или на localhost",
        "dropoff_src_db":            "Мои пункты",
        "dropoff_src_uzum":          "Все пункты Uzum",
        "dropoff_uzum_fallback":     "Нет активных заказов — показан старый список",
        "dropoff_hours_today":       "Сегодня",
        "dropoff_slots_btn":         "Слоты",
        "dropoff_slots_hide":        "Скрыть слоты",
        "dropoff_slots_loading":     "Загрузка слотов…",
        "dropoff_slots_empty":       "Нет свободных слотов",
        "dropoff_slots_no_orders":   "Сначала подтвердите CREATED заказ",
        "dropoff_slots_fail":        "Ошибка загрузки слотов",
        "dropoff_search_ph":         "Поиск по адресу… (напр.: Чилонзор, Юнусобод)",
        "dropoff_search_empty":      "По этому запросу ничего не найдено:",
        "dropoff_slot_click_hint":   "Нажмите, чтобы создать накладную",
        "dropoff_match_only":        "Только подходящее",
        "dropoff_match_hint":        "Показываем только подходящее время для выбранных заказов. Чтобы увидеть все — отключите фильтр",
        "invoice_no_dop":            "Пункт не выбран",
        "invoice_no_packing":        "Нет заказов «В сборке». Сначала подтвердите CREATED.",
        "invoice_confirm_prompt":    "{n} заказов на пункт {point} в {slot}. Подтвердить?",
        "invoice_mode_created":      "Накладная создана",
        "invoice_mode_updated":      "Накладная обновлена",
        "invoice_create_fail":       "Ошибка при создании накладной",
        "invoice_multi_shop_warn":   "Отправляется только {n} заказов (крупнейшая группа по магазину). Остальные {other} пойдут в отдельную накладную.",
        "dropoff_geo_unavailable":   "Местоположение недоступно (GPS/интернет?)",
        "dropoff_geo_timeout":       "Определение геолокации заняло слишком много времени",
        "dropoff_dist_hint":         "Расстояние от вас по прямой",
        "dropoff_nearest":           "БЛИЖАЙШИЙ",
        "dropoff_fav_add":           "В избранное",
        "dropoff_fav_remove":        "Убрать из избранного",
        "dropoff_panel_hint":        "Выберите пункт слева, чтобы увидеть слоты",
        "dropoff_selected_point":    "Выбранный пункт",
        "dropoff_pick_slot":         "Выберите слот",
        "dropoff_confirm_btn":       "Подтвердить",
        "dropoff_cap_hint":          "Свободно / всего мест",
        "_lang":                     "ru",
        "bulk_qr_size_label":        "Размер QR",
        "bulk_qr_size_uzum":         "Как у этикетки",
        "bulk_qr_size_hint":         "Выбранный размер даёт одинаковый QR товара в «Печать QR» и «Этикетка + QR»",
        "bulk_btn_confirm":          "Взять в работу",
        "bulk_btn_clear":            "Отменить выбор",
        "bulk_processing":           "Выполняется…",
        "bulk_confirm_only_created": "«Взять в работу» работает только для «Новых» (CREATED) заказов",
        "bulk_label_only_packing":   "Этикетка ещё не готова — сначала подтвердите заказ",
        "bulk_confirm_prompt":       "Взять в работу {n} заказ(ов)?",
        "bulk_confirm_title":        "Взять в работу",
        "invoice_confirm_title":     "Создать поставку",
        "confirm_cancel":            "Отмена",
        "bulk_toast_labels_ok":      "этикеток готово",
        "bulk_toast_labels_partial": "этикеток готово, {n} с ошибкой",
        "bulk_toast_labels_fail":    "Ошибка при получении этикеток",
        "bulk_toast_confirm_ok":     "заказов подтверждено",
        "bulk_toast_confirm_partial":"подтверждено, {n} с ошибкой",
        "bulk_toast_confirm_fail":   "Ошибка подтверждения",
        # Bosqich 2 — фоновое (async) подтверждение + опрос прогресса
        "bulk_async_started":        "{n} заказ(ов) принято, выполняется…",
        "bulk_async_progress":       "Подтверждается… {done}/{total}",
        "bulk_async_done_ok":        "{n} заказ(ов) подтверждено",
        "bulk_async_done_partial":   "{ok} подтверждено, {fail} с ошибкой",
        "bulk_async_skipped":        "{n} пропущено (не CREATED)",
        "bulk_select_all_aria":      "Выбрать все",
        # ── Orders-page chrome (full RU coverage) ──
        "page_title_orders":         "Заказы",
        "aria_section_fbs":          "Раздел FBS",
        "tab_orders":                "Заказы",
        "tab_stock":                 "Склад",
        "label_shop":                "Магазин",
        "opt_no_shop":               "— нет магазинов —",
        "opt_all_shops":             "Все магазины",
        "label_scheme":              "Схема",
        "opt_scheme_all":            "FBS + DBS",
        "label_page_size":           "На странице",
        "warn_token_pre":            "Токен Uzum Seller OpenAPI не задан. Чтобы видеть заказы, введите токен на странице",
        "link_my_shops":             "«Мои магазины»",
        "warn_token_post":           ".",
        "delivery_nav_title":        "Поставки — накладные",
        "delivery_nav":              "Поставки",
        "tip_pick_order_first":      "Сначала выберите заказ",
        "empty_pick_status":         "Выберите статус, чтобы увидеть заказы.",
        "more_statuses":             "Другие статусы…",
        "meta_found":                "Найдено:",
        "meta_page":                 "Страница:",
        "meta_status":               "Статус:",
        "qr_size_title":             "Размер QR-стикера (мм)",
        "qr_size_aria":              "Размер QR-стикера",
        "qr_size_uzum_full":         "Uzum (полностью)",
        "bulk_yorliq_title":         "Печать этикеток выбранных заказов",
        "bulk_yorliq_btn":           "Этикетка",
        "bulk_qr_title":             "Печать QR-кода выбранных заказов",
        "bulk_both_title":           "Печать этикетки и QR одним PDF",
        "bulk_both_btn":             "Этикетка + QR",
        "akt_download_title":        "Скачать выбранные акты в PDF",
        "akt_download_btn":          "Скачать",
        "akt_print_title":           "Печать выбранных актов одним документом",
        "akt_print_btn":             "Акт отправки (PDF)",
        "iframe_order_title":        "Заказ",
        "iframe_invoice_title":      "Накладная",
        "cd_overdue":                "Срок истёк",
        "cd_hours_left":             "осталось {n} ч",
        "cd_minutes_left":           "осталось {n} мин",
        "empty_no_orders":           "Заказы не найдены.",
        "empty_no_orders_hint":      "Выберите другой статус или измените схему.",
        "row_sku_prefix":            "SKU: ",
        "row_barcode_prefix":        "ШК: ",
        "unit_pcs":                  "шт",
        "deadline_tip":              "Срок сдачи в пункт приёма",
        "aria_order_no":             "Заказ №",
        "th_product":                "Товар",
        "th_scheme":                 "Схема",
        "th_created":                "Создан",
        "th_deadline":               "Срок",
        "th_shop":                   "Магазин",
        "th_sum":                    "Сумма",
        "prod_collapse":             "Скрыть",
        "loading":                   "Загрузка…",
        "err_prefix":                "Ошибка:",
        "net_err_prefix":            "Сетевая ошибка:",
        "net_err":                   "Сетевая ошибка.",
        "uzum_busy":                 "Uzum сейчас занят. Повторите через пару секунд.",
        "uzum_busy_short":           "Uzum сейчас занят. Повторите чуть позже.",
        "inv_empty":                 "Накладных нет.",
        "inv_empty_hint":            "Накладные, готовые к отправке, не найдены.",
        "inv_err_prefix":            "Ошибка:",
        "inv_count_tip":             "Принято / Всего",
        "pick_order_first":          "Сначала отметьте заказ",
        "no_label_dbs":              "У DBS-заказа нет этикетки отправки — используйте QR",
        "dbs_skipped":               "{n} DBS пропущено (этикетка только для FBS)",
        "labels_preparing":          "Готовится {n} этикеток…",
        "labels_fetch_err":          "Ошибка при получении этикеток:",
        "labels_ready_some_err":     "Этикетки готовы ({n} с ошибкой) — открывается окно печати",
        "print_window_opening":      "Открывается окно печати…",
        "qr_print_opening":          "Открывается окно печати QR…",
        "shop_not_selected":         "Магазин не выбран.",
        "shoppick_count_suffix":     "шт",
        "no_packing_among_sel":      "Среди отмеченных заказов нет в статусе «Сборка»",
        "dropoff_match_empty":       "Подходящих пунктов пока нет — отключите «Только подходящее», чтобы увидеть все.",
        "slot_overdue":              "⏰ У одного из выбранных заказов истёк срок доставки — Uzum не выдаёт на него слот. Выберите заказы без истёкшего срока (без красного таймера) и повторите.",
        # ── Invoices-page chrome (full RU coverage) ──
        "inv_page_title":            "Накладные",
        "inv_intro":                 "Ваш список FBS «Накладные». Создание, статус приёмки, отмену и детали вы видите здесь. Данные берутся напрямую через Uzum Seller OpenAPI.",
        "warn_token_pre_inv":        "Токен Uzum Seller OpenAPI не задан. Чтобы видеть список накладных, введите токен на странице",
        "inv_filter_status_aria":    "Фильтр по статусу",
        "inv_status_all":            "Все статусы",
        "inv_status_created":        "Создана",
        "inv_status_acceptance":     "На приёмке",
        "inv_status_accepted":       "Принята",
        "inv_status_cancelled":      "Отменена",
        "inv_check_all_aria":        "Выбрать все",
        "th_num":                    "№",
        "th_date":                   "Дата",
        "th_stock":                  "Склад",
        "th_orders":                 "Заказы",
        "th_price":                  "Цена",
        "th_address":                "Адрес",
        "th_slot":                   "Слот",
        "th_status":                 "Статус",
        "pager_prev":                "Назад",
        "pager_next":                "Вперёд",
        "inv_both_title_full":       "Печать этикетки и QR одним PDF (сначала этикетка отправки, затем QR каждого товара)",
        "row_select_aria":           "Отметить",
        "row_unselectable_aria":     "Нельзя отметить",
        "row_unselectable_title":    "Отмечать и печатать несколько актов можно только у накладных в статусе «Создана»",
        "uzum_retrying":             "Uzum занят — повторная попытка…",
        "inv_no_response_strong":    "Uzum не ответил.",
        "inv_try_later":             "Повторите чуть позже.",
        "inv_empty_status":          "Накладных в этом статусе нет",
        "inv_no_more":               "Больше накладных нет",
        "inv_refreshing":            "Обновление…",
        "inv_no_items":              "Данные о товаре не найдены",
        "inv_barcode_label":         "ШК",
        "inv_rail_total":            "Итого",
        "inv_th_dona":               "шт",
        "inv_fact_slot":             "Слот",
        "inv_fact_created":          "Создана",
        "inv_fact_fullsum":          "Полная сумма",
        "inv_fact_accept_start":     "Приёмка началась",
        "inv_fact_accept_done":      "Приёмка завершена",
        "inv_akt_short":             "Акт отправки",
        "inv_pickup":                "Пункт приёма",
        "inv_pickup_edit_title":     "Изменить пункт приёма и временной слот",
        "inv_ord_selectall_title":   "Выбрать все",
        "inv_orders_empty":          "Для этой накладной заказы не найдены (возможно, старые или не синхронизированы)",
        "inv_busy_no_response":      "Uzum не ответил (занят). Повторите чуть позже.",
        "akt_unavailable":           "Акт недоступен или ошибка при получении:",
        "akt_fetch_err":             "Ошибка при получении акта:",
        "akt_some_skipped":          "Внимание: часть актов не получена (id: {ids}). Остальные в PDF. Повторите чуть позже.",
        "akt_bulk_cap":              "За один раз объединяется до {n} актов. Выберите меньше.",
        "inv_preparing":             "Подготовка…",
        "inv_overdue_no_slot":       "⏰ У этого заказа истёк срок — подходящего слота нет.",
        "inv_show_all_times":        "Показать все слоты",
        "inv_overdue_short":         "⏰ Срок заказа истёк.",
        "inv_no_active_orders":      "Нет активных заказов",
        "inv_no_invoice_selected":   "Накладная не выбрана",
        "inv_point_not_picked":      "Пункт не выбран",
        "inv_slot_invalid":          "Слот недействителен (нет uuid) — выберите другой слот",
        "inv_change_confirm":        "Изменить пункт приёма накладной №{num} на «{point}»{slot}?",
        "dropoff_change_title":      "Изменить пункт приёма",
        "dropoff_change_title_pre":  "Изменить ",
        "dropoff_change_title_grad": "пункт приёма",
        "dropoff_change_btn":        "Изменить",
        "inv_change_err":            "Ошибка при изменении",
        "inv_slot_too_late":         "Выбранный слот слишком поздний — выберите для этой накладной слот ПОРАНЬШЕ. ",
        "inv_uzum_busy_retry":       "Uzum сейчас занят — повторите через пару секунд.",
        "inv_pickup_changed":        "✅ Пункт приёма изменён",
        # ── Shop-pick modal (_fbs_shoppick_modal.html) ──
        "shoppick_title":            "Отмечены заказы из разных магазинов",
        "shoppick_msg":              "Одна накладная — для одного магазина. Для какого магазина создать?",
        "shoppick_cancel":           "Отмена",
        "shoppick_ok":               "Продолжить",
    },
}


# ── Ombor (stock) page i18n bundle ──
STOCK_LABELS: dict[str, dict[str, str]] = {
    "uz": {
        "_lang":                     "uz",
        # Glass header
        "page_title":                "Ombor",
        "aria_section_fbs":          "FBS bo'limi",
        "tab_orders":                "Buyurtmalar",
        "tab_stock":                 "Ombor",
        "label_shop":                "Do'kon",
        "opt_all_shops":             "Barcha do'konlar",
        "export_title":              "Hozirgi qoldiqni Excel'ga yuklab olish",
        "export_btn":                "Excel",
        "import_title":              "Excel fayldan ommaviy yangilash",
        "import_btn":                "Yuklash",
        # Token warning (3-part)
        "warn_token_pre":            "Uzum Seller OpenAPI tokeni o'rnatilmagan. Qoldiqlarni ko'rish uchun",
        "link_my_shops":             "«Mening do'konlarim»",
        "warn_token_post":           " sahifasida token kiriting.",
        # Availability segment
        "seg_all":                   "Hammasi",
        "seg_in":                    "Mavjud",
        "seg_out":                   "Tugagan",
        "ph_search":                 "Nomi, SKU yoki shtrix-kod bo'yicha qidirish…",
        # Table headers
        "th_product":                "Mahsulot / SKU",
        "th_barcode":                "Shtrix-kod",
        "th_scheme":                 "Sxema",
        "th_amount":                 "Qoldiq, dona",
        "loading":                   "Yuklanmoqda…",
        "loading_live":              "Uzum'dan jonli yuklanmoqda…",
        # Save bar
        "savebar_text":              "ta o'zgartirildi",
        "btn_cancel":                "Bekor",
        "btn_save":                  "Saqlash",
        # Import modal
        "imp_title":                 "Excel'dan yangilash",
        "aria_close":                "Yopish",
        "imp_th_barcode":            "Shtrix-kod",
        "imp_th_amount":             "Qoldiq",
        "imp_note":                  "Faqat o'zgargan qatorlar Uzum'ga yoziladi.",
        "imp_btn_cancel":            "Bekor",
        "imp_btn_confirm":           "Tasdiqlash",
        # JS — scheme toggles
        "scheme_unavailable":        "Bu sxema bu SKU uchun mavjud emas",
        "scheme_toggle":             "Yoqish / o'chirish",
        "aria_amount":               "Qoldiq",
        "step_up":                   "Ko'paytirish",
        "step_down":                 "Kamaytirish",
        # JS — empty states
        "empty_nothing":             "Hech narsa topilmadi",
        "empty_no_sku":              "Bu yerda SKU yo'q",
        "sum_out":                   "tugagan",
        # JS — save flow
        "saving":                    "Saqlanmoqda…",
        "err_http":                  "Xato (HTTP {n})",
        "saved_ok":                  "{n} ta qoldiq saqlandi ✓",
        "mode_linked":               "Omborda",
        "mode_linked_hint":          "Faqat FBS/DBS sxemasiga ulangan tovarlar (Uzum «Ombor»i)",
        "mode_all":                  "Barcha tovarlar",
        "mode_all_hint":             "Katalogdagi hamma SKU — omborga yangi tovar qo'shish uchun",
        "removed_ok":                "{n} ta tovar ombordan olib tashlandi ✓",
        "saved_mixed":               "{n} ta qoldiq saqlandi, {m} ta tovar ombordan olib tashlandi ✓",
        "save_no_effect":            "Uzum o'zgarishni qabul qilmadi — hech narsa yangilanmadi",
        "scheme_removed":            "Ombordan olib tashlanadi (sxemadan uziladi)",
        "net_err_prefix":            "Tarmoq xatosi:",
        # JS — fetch flow
        "uzum_no_response":          "Uzum javob bermadi.",
        "err_prefix":                "Xato:",
        "try_later":                 " Birozdan keyin qayta urinib ko'ring.",
        "btn_retry":                 "Qayta urinish",
        # JS — import flow
        "checking":                  "Tekshirilmoqda…",
        "err_paren":                 "Xato ({n})",
        "imp_summary":               "{n} ta o'zgaradi · {u} o'zgarmaydi",
        "imp_unknown_sku":           "{n} noma'lum SKU",
        "imp_skipped":               "{n} o'tkazib yuborildi",
        "imp_no_changes":            "O'zgarish topilmadi",
        "imp_confirm_n":             "Tasdiqlash ({n})",
        "imp_no_changes_btn":        "O'zgarish yo'q",
        "imp_applied":               "{n} ta SKU yangilandi",
    },
    "ru": {
        "_lang":                     "ru",
        # Glass header
        "page_title":                "Склад",
        "aria_section_fbs":          "Раздел FBS",
        "tab_orders":                "Заказы",
        "tab_stock":                 "Склад",
        "label_shop":                "Магазин",
        "opt_all_shops":             "Все магазины",
        "export_title":              "Скачать текущие остатки в Excel",
        "export_btn":                "Excel",
        "import_title":              "Массовое обновление из файла Excel",
        "import_btn":                "Загрузить",
        # Token warning (3-part)
        "warn_token_pre":            "Токен Uzum Seller OpenAPI не задан. Чтобы видеть остатки, введите токен на странице",
        "link_my_shops":             "«Мои магазины»",
        "warn_token_post":           ".",
        # Availability segment
        "seg_all":                   "Все",
        "seg_in":                    "В наличии",
        "seg_out":                   "Закончились",
        "ph_search":                 "Поиск по названию, SKU или штрих-коду…",
        # Table headers
        "th_product":                "Товар / SKU",
        "th_barcode":                "Штрих-код",
        "th_scheme":                 "Схема",
        "th_amount":                 "Остаток, шт",
        "loading":                   "Загрузка…",
        "loading_live":              "Загрузка напрямую из Uzum…",
        # Save bar
        "savebar_text":              "изменено",
        "btn_cancel":                "Отмена",
        "btn_save":                  "Сохранить",
        # Import modal
        "imp_title":                 "Обновление из Excel",
        "aria_close":                "Закрыть",
        "imp_th_barcode":            "Штрих-код",
        "imp_th_amount":             "Остаток",
        "imp_note":                  "В Uzum записываются только изменённые строки.",
        "imp_btn_cancel":            "Отмена",
        "imp_btn_confirm":           "Подтвердить",
        # JS — scheme toggles
        "scheme_unavailable":        "Эта схема недоступна для данного SKU",
        "scheme_toggle":             "Включить / выключить",
        "aria_amount":               "Остаток",
        "step_up":                   "Увеличить",
        "step_down":                 "Уменьшить",
        # JS — empty states
        "empty_nothing":             "Ничего не найдено",
        "empty_no_sku":              "Здесь нет SKU",
        "sum_out":                   "закончились",
        # JS — save flow
        "saving":                    "Сохранение…",
        "err_http":                  "Ошибка (HTTP {n})",
        "saved_ok":                  "Сохранено остатков: {n} ✓",
        "mode_linked":               "На складе",
        "mode_linked_hint":          "Только товары, привязанные к схеме FBS/DBS (склад Uzum)",
        "mode_all":                  "Все товары",
        "mode_all_hint":             "Все SKU каталога — чтобы добавить новый товар на склад",
        "removed_ok":                "Убрано со склада: {n} ✓",
        "saved_mixed":               "Сохранено остатков: {n}, убрано со склада: {m} ✓",
        "save_no_effect":            "Uzum не принял изменения — ничего не обновлено",
        "scheme_removed":            "Будет убран со склада (отвязка от схемы)",
        "net_err_prefix":            "Сетевая ошибка:",
        # JS — fetch flow
        "uzum_no_response":          "Uzum не ответил.",
        "err_prefix":                "Ошибка:",
        "try_later":                 " Повторите попытку чуть позже.",
        "btn_retry":                 "Повторить",
        # JS — import flow
        "checking":                  "Проверка…",
        "err_paren":                 "Ошибка ({n})",
        "imp_summary":               "Изменится: {n} · без изменений: {u}",
        "imp_unknown_sku":           "{n} неизвестных SKU",
        "imp_skipped":               "{n} пропущено",
        "imp_no_changes":            "Изменений не найдено",
        "imp_confirm_n":             "Подтвердить ({n})",
        "imp_no_changes_btn":        "Нет изменений",
        "imp_applied":               "Обновлено SKU: {n}",
    },
}


# ── QR-print page i18n bundle (templates/fbs_qr_print.html) ──────────
QR_PRINT_LABELS: dict[str, dict[str, str]] = {
    "uz": {
        "title":        "FBS QR chop",
        "count_prefix": "Yorliqlar:",
        "reprint":      "Qayta chop etish",
        "err_no_order": "Buyurtma tanlanmagan",
        "err_no_goods": "Tovar topilmadi",
    },
    "ru": {
        "title":        "Печать QR — FBS",
        "count_prefix": "Этикеток:",
        "reprint":      "Повторить печать",
        "err_no_order": "Заказ не выбран",
        "err_no_goods": "Товар не найден",
    },
}


# ── Detail-page i18n bundle ──────────────────────────────────────────
# Every user-visible string the detail template/JS renders. The list
# page already follows the same pattern via STATUS_LABELS; here we
# extend it to cover card titles, field labels, buttons, modals and
# toasts. Anything user-visible in the template MUST come from this
# dict — no hardcoded strings in HTML or JS.
DETAIL_LABELS: dict[str, dict[str, str]] = {
    "uz": {
        # Page chrome
        "back_to_list": "← Buyurtmalar ro'yxatiga",
        "order_no": "Buyurtma №",
        "loading_meta": "Yuklanmoqda…",
        "loading_body": "Buyurtma ma'lumotlari yuklanmoqda…",
        "endpoint_label": "Endpoint",
        "endpoint_pending": "Endpoint kutilmoqda",
        "error_short": "Xato",
        "error_title": "Xatolik:",
        "network_error_title": "Tarmoq xatosi:",
        "detail_pending_strong": "Detail endpoint hali tayyor emas.",
        "detail_pending_body": "swagger ma'lumotlari kutilmoqda.",
        # Identifikator turi Uzumdan keladi (identifierInfo.type): IMEI yoki
        # ASL_BELGISI (O'zbekiston markirovka kodi). Ilgari bu yer hamma narsani
        # «IMEI» derdi — «Магнит браслет» aslida ASL Belgisi talab qilardi
        # (Abdulaziz 2026-07-14). {type} — JS almashtiradigan o'rin.
        "identifier_required_flag": "{type} talab qilinadi",
        "identifier_type_IMEI": "IMEI",
        "identifier_type_ASL_BELGISI": "ASL Belgisi",
        "identifier_type_UNKNOWN": "Identifikator",
        "identifier_desc_IMEI": "Har bir dona uchun IMEI / seriya raqamini kiriting.",
        "identifier_desc_ASL_BELGISI": "O'zbekiston qonunlariga ko'ra bu tovar uchun markirovka kodi (ASL Belgisi) ko'rsatilishi shart.",
        "identifier_desc_UNKNOWN": "Har bir dona uchun identifikatorni kiriting.",
        # Card titles
        "card_customer": "Mijoz",
        "card_status_dates": "Status va sanalar",
        "card_delivery": "Yetkazib berish",
        "card_stock": "Ombor",
        "card_items": "Mahsulotlar",
        # Customer fields
        "f_full_name": "Ism-familiya",
        "f_phone": "Telefon",
        "f_address": "Manzil",
        "f_comment": "Izoh",
        # Date fields
        "f_created": "Yaratildi",
        "f_accept_until": "Tasdiqlash muddati",
        "f_deliver_until": "Yetkazib berish muddati",
        "f_accepted": "Tasdiqlangan",
        "f_delivering": "Yo'lda",
        "f_dp_delivered": "DP'ga yetkazilgan",
        "f_delivered": "Yetkazilgan",
        "f_completed": "Yakunlangan",
        "f_cancelled": "Bekor qilingan",
        "f_returned": "Qaytarilgan",
        "f_cancel_reason": "Bekor qilish sababi",
        # Delivery
        "f_drop_address": "Olish nuqtasi",
        "f_drop_type": "Olish nuqtasi turi",
        "f_slot_from": "Slot boshlanishi",
        "f_slot_to": "Slot tugashi",
        "f_place": "Joy",
        "f_invoice": "Hisob raqami",
        # Stock
        "f_stock_title": "Ombor nomi",
        "f_stock_address": "Ombor manzili",
        "f_stock_external_id": "Tashqi ID",
        "f_stock_time_from": "Ish vaqti (boshlanishi)",
        "f_stock_time_to": "Ish vaqti (tugashi)",
        "f_stock_source": "Manba",
        # Minimal "Uzum-style" flat detail view
        "f_order_id": "Buyurtma ID",
        "f_shop": "Do'kon",
        "f_composition": "Tarkib va summa",
        "sec_items": "Tarkib",
        "f_pickup": "Qabul punkti",
        "f_unit_price": "Sotuv narxi / dona",
        "f_marking": "Markirovka",
        "f_barcode": "Barkod",
        "comp_unit": "dona",
        "cd_days": "kun",
        "cd_hours": "soat",
        "cd_min": "daqiqa",
        "cd_overdue": "Muddat o'tdi",
        # Items table
        "th_product": "Mahsulot",
        "th_qty": "Soni",
        "th_unit": "Donasi",
        "th_total": "Jami",
        "no_items": "Mahsulotlar yo'q",
        # Bosqich A.14 (HAR audit A1) — gabarit/og'irlik (qadoqlash uchun).
        # "Qadoq o'lchami" — tovar nomidagi "(O'lcham: …)" bilan chalkashmasin.
        "item_dims": "Qadoq o'lchami",
        "item_weight": "Og'irlik",
        "items_subtotal": "Mahsulotlar jami:",
        "order_total": "Jami summa",
        # Rail panel (Rail dizayni — buyurtma kartasi o'ng paneli)
        "rail_status": "Status",
        "rail_qty": "Jami",
        "rail_sum": "Summa",
        # Actions
        "act_confirm": "Tasdiqlash",
        "act_cancel": "Bekor qilish",
        "act_print": "Yorliq chop etish",
        "act_print_enlarged": "Yorliq (katta matn)",
        "act_print_qr": "QR chop etish",
        # {type} — JS almashtiradi (IMEI / ASL Belgisi). Turi Uzumdan keladi.
        "act_imei": "{type} biriktirish",
        "act_empty": "Ushbu statusda harakatlar mavjud emas.",
        "confirm_msg": "Buyurtmani tasdiqlaysizmi? Bu yig'ishni boshlash signalini Uzum'ga yuboradi.",
        "confirm_title": "Buyurtmani tasdiqlash",
        "confirm_cancel": "Bekor qilish",
        "dbs_delivering_title": "Yetkazishga olish",
        "dbs_refund_title": "Qaytarma yaratish",
        # Cancel modal
        "cancel_title": "Buyurtmani bekor qilish",
        "cancel_reason_label": "Sabab",
        "cancel_reason_placeholder": "— sababni tanlang —",
        "cancel_reason_loading": "Sabablar ro'yxati yuklanmoqda…",
        "cancel_reason_load_failed": "Sabablarni yuklab bo'lmadi: ",
        "cancel_reason_empty": "Uzum sabablar ro'yxatini qaytarmadi",
        "cancel_reasons_loaded_suffix": "ta sabab yuklandi",
        "cancel_comment_label": "Izoh (ixtiyoriy)",
        "cancel_comment_placeholder": "Mijozga ko'rinmaydi",
        "btn_close": "Yopish",
        "btn_select_reason_first": "Avval sababni tanlang",
        # IMEI modal
        "imei_title": "Identifikatorlarni biriktirish",
        # Tur nomi endi har tovar ostida alohida yoziladi (JS), shuning uchun bu
        # yer NEYTRAL: «IMEI/seriya» deb qotirib qo'yish noto'g'ri edi —
        # ASL_BELGISI tovarlarida sotuvchini chalg'itardi (Abdulaziz 2026-07-14).
        "imei_desc": "Quyidagi tovarlar uchun kodlarni kiriting. Bo'sh maydonlar yuborilmaydi.",
        "imei_placeholder_prefix": "IMEI / seriya raqami #",
        "imei_qty_prefix": "Soni:",
        "imei_oi_prefix": "orderItemId:",
        "imei_no_inputs": "Hech bo'lmasa bitta identifikator kiriting",
        "btn_attach": "Biriktirish",
        "aria_close": "Yopish",
        # Toasts
        "toast_confirmed": "Buyurtma tasdiqlandi",
        "toast_cancelled": "Buyurtma bekor qilindi",
        "toast_imei_attached": "Identifikatorlar biriktirildi",
        "toast_label_opened_suffix": "ta yorliq ochildi",
        "toast_no_pdf": "Uzum hech qanday PDF qaytarmadi",
        "toast_http_error_prefix": "Xato: HTTP ",
        "toast_network_error_prefix": "Tarmoq xatosi: ",
        # ── Stage 3: DBS workflow buttons & confirmations ──────
        "dbs_act_delivering": "Yetkazishga olaman",
        "dbs_act_completed":  "Yetkazib berildi",
        "dbs_act_refund":     "Qaytarma yaratish",
        "dbs_code_placeholder": "Tasdiqlash kodi (agar bo'lsa)",
        # Bosqich A.14 (HAR audit B2) — kod urinishlari qoldi (3 xatodan keyin blok)
        "dbs_code_retries": "Kodni kiritishga {n} urinish qoldi",
        "dbs_code_invalid": "Tasdiqlash kodi raqam bo'lishi kerak",
        "dbs_confirm_delivering": "Buyurtmani yetkazishga olasizmi? Status DELIVERING'ga o'tadi.",
        "dbs_confirm_refund": "Rostan ham qaytarma yaratasizmi? Bu amalni qaytarib bo'lmaydi.",
        "dbs_toast_delivering": "Yetkazib berishga olindi",
        "dbs_toast_completed": "Buyurtma yetkazildi",
        "dbs_toast_refund": "Qaytarma boshlandi",
    },
    "ru": {
        # Page chrome
        "back_to_list": "← К списку заказов",
        "order_no": "Заказ №",
        "loading_meta": "Загрузка…",
        "loading_body": "Загрузка данных заказа…",
        "endpoint_label": "Endpoint",
        "endpoint_pending": "Ожидается endpoint",
        "error_short": "Ошибка",
        "error_title": "Ошибка:",
        "network_error_title": "Сетевая ошибка:",
        "detail_pending_strong": "Endpoint детали ещё не готов.",
        "detail_pending_body": "ожидаются данные swagger.",
        # См. uz-блок: тип приходит из Uzum (identifierInfo.type).
        "identifier_required_flag": "Требуется {type}",
        "identifier_type_IMEI": "IMEI",
        "identifier_type_ASL_BELGISI": "Asl Belgisi",
        "identifier_type_UNKNOWN": "идентификатор",
        "identifier_desc_IMEI": "Укажите IMEI / серийный номер для каждой единицы.",
        "identifier_desc_ASL_BELGISI": "По законам Узбекистана для этого товара нужно указать код маркировки (Asl Belgisi).",
        "identifier_desc_UNKNOWN": "Укажите идентификатор для каждой единицы.",
        # Card titles
        "card_customer": "Клиент",
        "card_status_dates": "Статус и даты",
        "card_delivery": "Доставка",
        "card_stock": "Склад",
        "card_items": "Товары",
        # Customer fields
        "f_full_name": "ФИО",
        "f_phone": "Телефон",
        "f_address": "Адрес",
        "f_comment": "Комментарий",
        # Date fields
        "f_created": "Создан",
        "f_accept_until": "Срок подтверждения",
        "f_deliver_until": "Срок доставки",
        "f_accepted": "Подтверждён",
        "f_delivering": "В пути",
        "f_dp_delivered": "Доставлен в ПВЗ",
        "f_delivered": "Доставлен",
        "f_completed": "Завершён",
        "f_cancelled": "Отменён",
        "f_returned": "Возвращён",
        "f_cancel_reason": "Причина отмены",
        # Delivery
        "f_drop_address": "Пункт выдачи",
        "f_drop_type": "Тип пункта выдачи",
        "f_slot_from": "Начало слота",
        "f_slot_to": "Конец слота",
        "f_place": "Место",
        "f_invoice": "Номер инвойса",
        # Stock
        "f_stock_title": "Название склада",
        "f_stock_address": "Адрес склада",
        "f_stock_external_id": "Внешний ID",
        "f_stock_time_from": "Время работы (с)",
        "f_stock_time_to": "Время работы (до)",
        "f_stock_source": "Источник",
        # Minimal "Uzum-style" flat detail view
        "f_order_id": "ID заказа",
        "f_shop": "Магазин",
        "f_composition": "Состав и сумма",
        "sec_items": "Состав",
        "f_pickup": "Место приёма",
        "f_unit_price": "Цена продажи за шт",
        "f_marking": "Маркировка",
        "f_barcode": "ШК",
        "comp_unit": "шт",
        "cd_days": "дн",
        "cd_hours": "ч",
        "cd_min": "мин",
        "cd_overdue": "Срок истёк",
        # Items table
        "th_product": "Товар",
        "th_qty": "Кол-во",
        "th_unit": "За шт.",
        "th_total": "Итого",
        "no_items": "Товары отсутствуют",
        # Bosqich A.14 (HAR audit A1) — габариты/вес (для упаковки)
        "item_dims": "Габариты упаковки",
        "item_weight": "Вес",
        "items_subtotal": "Сумма товаров:",
        "order_total": "Итого",
        # Rail panel
        "rail_status": "Статус",
        "rail_qty": "Итого",
        "rail_sum": "Сумма",
        # Actions
        "act_confirm": "Подтвердить",
        "act_cancel": "Отменить",
        "act_print": "Печать этикетки",
        "act_print_enlarged": "Этикетка (крупно)",
        "act_print_qr": "Печать QR",
        "act_imei": "Привязать {type}",
        "act_empty": "Для этого статуса нет действий.",
        "confirm_msg": "Подтвердить заказ? Это отправит в Uzum сигнал о начале сборки.",
        "confirm_title": "Подтвердить заказ",
        "confirm_cancel": "Отмена",
        "dbs_delivering_title": "Взять на доставку",
        "dbs_refund_title": "Создать возврат",
        # Cancel modal
        "cancel_title": "Отменить заказ",
        "cancel_reason_label": "Причина",
        "cancel_reason_placeholder": "— выберите причину —",
        "cancel_reason_loading": "Загрузка списка причин…",
        "cancel_reason_load_failed": "Не удалось загрузить причины: ",
        "cancel_reason_empty": "Uzum не вернул список причин",
        "cancel_reasons_loaded_suffix": "причин загружено",
        "cancel_comment_label": "Комментарий (необязательно)",
        "cancel_comment_placeholder": "Клиенту не виден",
        "btn_close": "Закрыть",
        "btn_select_reason_first": "Сначала выберите причину",
        # IMEI modal
        "imei_title": "Привязать идентификаторы",
        "imei_desc": "Введите коды для товаров ниже. Пустые поля не отправляются.",
        "imei_placeholder_prefix": "IMEI / серийный номер #",
        "imei_qty_prefix": "Кол-во:",
        "imei_oi_prefix": "orderItemId:",
        "imei_no_inputs": "Введите хотя бы один идентификатор",
        "btn_attach": "Привязать",
        "aria_close": "Закрыть",
        # Toasts
        "toast_confirmed": "Заказ подтверждён",
        "toast_cancelled": "Заказ отменён",
        "toast_imei_attached": "Идентификаторы привязаны",
        "toast_label_opened_suffix": "этикеток открыто",
        "toast_no_pdf": "Uzum не вернул ни одного PDF",
        "toast_http_error_prefix": "Ошибка: HTTP ",
        "toast_network_error_prefix": "Сетевая ошибка: ",
        # ── Stage 3: DBS workflow buttons & confirmations ──────
        "dbs_act_delivering": "Взять на доставку",
        "dbs_act_completed":  "Доставлен клиенту",
        "dbs_act_refund":     "Создать возврат",
        "dbs_code_placeholder": "Код подтверждения (если есть)",
        # Bosqich A.14 (HAR audit B2)
        "dbs_code_retries": "Осталось попыток ввода кода: {n}",
        "dbs_code_invalid": "Код подтверждения должен быть числом",
        "dbs_confirm_delivering": "Взять заказ на доставку? Статус перейдёт в DELIVERING.",
        "dbs_confirm_refund": "Действительно создать возврат? Это действие необратимо.",
        "dbs_toast_delivering": "Заказ взят на доставку",
        "dbs_toast_completed": "Заказ доставлен",
        "dbs_toast_refund": "Возврат начат",
    },
}


def _format_iso_to_tashkent(value: str) -> str | None:
    """Parse an ISO-8601 string and render it as Tashkent-local
    ``DD.MM.YYYY HH:MM``.

    Returns ``None`` when ``value`` doesn't look like a parseable
    timestamp — callers treat that as "no date detail to show". Naive
    (zone-less) inputs are assumed to already be Tashkent time, matching
    how the rest of this module treats seller-facing dates.
    """
    if not isinstance(value, str) or len(value) < 10:
        return None
    from datetime import datetime, timezone, timedelta
    tashkent = timezone(timedelta(hours=5))
    s = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tashkent)
    return dt.astimezone(tashkent).strftime("%d.%m.%Y %H:%M")


def _format_uzum_payload_detail(payload, lang: str | None = None) -> str | None:
    """Distil Uzum's structured ``errors[].payload`` into a short localized
    suffix to append to the user-facing message.

    The payload shape is per-error-code and not fully documented, so this
    stays conservative: it only surfaces detail it can confidently make
    human-readable — currently a date/deadline value (the most common and
    most useful case, e.g. seller-order-03 "muddat o'tib ketgan" carries
    the exact deadline). Anything it can't confidently format is left out
    of the message (the raw payload still rides along in ``uzum_payload``
    for diagnostics). Returns ``None`` when there's nothing clean to add.
    """
    if not isinstance(payload, dict) or not payload:
        return None
    _date_hint = ("date", "deadline", "until", "expire", "time", "till")
    for key, val in payload.items():
        if not isinstance(val, str):
            continue
        key_l = str(key).lower()
        looks_date = any(h in key_l for h in _date_hint) or ("T" in val and "-" in val)
        if not looks_date:
            continue
        formatted = _format_iso_to_tashkent(val)
        if formatted:
            prefix = "срок" if (lang or _session_lang()) == "ru" else "muddat"
            return f"{prefix}: {formatted}"
    return None


def _uzum_error_response(err: UzumAPIError, lang: str | None = None):
    """Convert ``UzumAPIError`` into a JSON response with a localized message.

    Status mapping:
      * Uzum 429 → 429 ("Uzum band, bir-ikki soniyadan keyin urinib ko'ring")
      * Uzum 5xx → 502 ("vaqtincha ishlamayapti, qaytadan urinib ko'ring")
      * Uzum 4xx → 400 (user-facing reason, e.g. wrong status / bad input)
      * Anything else → 502 (defensive default; network/timeout layer)
    """
    lang = lang or _session_lang()
    code = err.code or ""
    # Bosqich A.9 (#5) — 429 (Uzum per-token throttle) means "wait", not a
    # per-order problem. Branch on it FIRST, before the code/message lookups,
    # so the seller always gets the passive wait-hint (and HTTP 429 the
    # frontend can detect to show it) even when Uzum attaches a seller-order
    # code or a Russian message to the throttle response. With fail-fast (#5)
    # this arrives in ~2-3s instead of after a 6-min retry storm. The hint is
    # passive — the frontend must NOT auto-retry (every manual retry is still
    # paced 1s/token by the gate, but we don't want to invite a re-hammer).
    # Diagnostic fields carried on EVERY error response (both 429 and the
    # normal branch). They're "yashirin" — the frontend shows only
    # ``error`` in the toast, but trace/timestamp/payload sit in the JSON
    # for DevTools + server logs and (trace especially) for quoting to
    # Uzum support so they can find the exact failed request.
    trace = err.trace or None
    timestamp = err.timestamp or None
    payload = err.error_payload if err.error_payload not in (None, {}, [], "") else None

    if err.http_status == 429:
        _wait_msg = (
            "Uzum сейчас занят. Повторите через пару секунд."
            if lang == "ru"
            else "Uzum hozir band. Bir-ikki soniyadan keyin qayta urinib ko'ring."
        )
        return _json_response(
            {
                "error": _wait_msg,
                "uzum_code": code or None,
                "uzum_message": err.message or None,
                "uzum_http": 429,
                "uzum_raw": None,
                "uzum_trace": trace,
                "uzum_timestamp": timestamp,
                "uzum_payload": payload,
            },
            429,
        )

    _msgs = FBS_ERROR_MESSAGES.get(lang, FBS_ERROR_MESSAGES["uz"])
    if code in _msgs or code in FBS_ERROR_MESSAGES["uz"]:
        msg = _msgs.get(code) or FBS_ERROR_MESSAGES["uz"][code]
    elif err.message:
        # Uzum has its own Russian message — show it as-is rather than
        # invent a translation we don't actually know.
        msg = err.message
    elif err.http_status >= 500:
        msg = (
            "Сервис Uzum временно недоступен. Повторите через пару минут."
            if lang == "ru"
            else "Uzum xizmati vaqtincha ishlamayapti. Bir-ikki daqiqadan keyin urinib ko'ring."
        )
    else:
        msg = (
            f"Ошибка Uzum (HTTP {err.http_status})"
            if lang == "ru"
            else f"Uzum xatosi (HTTP {err.http_status})"
        )

    # Enrich the message with a clean detail distilled from Uzum's
    # structured payload (e.g. the exact deadline for a "muddat o'tgan"
    # error). Only appended when we can confidently format it — otherwise
    # the message stays as-is and the raw payload rides in ``uzum_payload``.
    detail_suffix = _format_uzum_payload_detail(payload, lang)
    if detail_suffix and detail_suffix not in msg:
        msg = f"{msg} ({detail_suffix})"

    http_code = 502 if err.http_status >= 500 or err.http_status == 0 else 400
    # Surface Uzum's raw response body when we couldn't extract a code —
    # otherwise the seller sees only the generic fallback in DevTools and
    # we have no way to diagnose what Uzum actually returned. Truncate to
    # 800 chars to keep the JSON small.
    body_preview = (err.raw_body or "")[:800] if not code else None
    return _json_response(
        {
            "error": msg,
            "uzum_code": code or None,
            "uzum_message": err.message or None,
            "uzum_http": err.http_status,
            "uzum_raw": body_preview,
            "uzum_trace": trace,
            "uzum_timestamp": timestamp,
            "uzum_payload": payload,
        },
        http_code,
    )


def _refresh_requested() -> bool:
    """True if the caller passed ?refresh=1 (any truthy value)."""
    return (request.args.get("refresh") or "").strip().lower() in ("1", "true", "yes")


def _parse_yyyy_mm_dd_to_ms(s: str | None, *, end_of_day: bool) -> int | None:
    """``YYYY-MM-DD`` (from ``<input type="date">``) → epoch milliseconds.

    The seller picks a date in their local calendar — for SellerHub
    that's Asia/Tashkent (UTC+5). We interpret the picked day in that
    zone so the filter matches what the UI shows: an order that the
    list page renders as "23.05.2026 02:44" must match a filter of
    ``date_from=2026-05-23`` even though its UTC ``date_created`` is
    actually 2026-05-22T21:44 (5 hours earlier).

    ``end_of_day=True`` shifts the timestamp to 23:59:59.999 *Tashkent
    time* — inclusive of late-night orders the seller still sees in
    that calendar day.

    Returns ``None`` for missing/empty/malformed input — invalid dates
    are treated as "no filter" rather than 400'd, matching how the
    other optional list params behave.
    """
    if not s:
        return None
    s = s.strip()
    if not s:
        return None
    try:
        from datetime import datetime, timezone, timedelta
        dt = datetime.strptime(s, "%Y-%m-%d")
        if end_of_day:
            dt = dt.replace(hour=23, minute=59, second=59, microsecond=999000)
        # Attach the seller's local zone (Asia/Tashkent = UTC+5, no DST)
        # then convert to epoch ms. Hard-coded offset is fine — Uzbekistan
        # has been UTC+5 year-round since 1991.
        tashkent = timezone(timedelta(hours=5))
        return int(dt.replace(tzinfo=tashkent).timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def _parse_order_ids_param(raw: str | None) -> set[str]:
    """Comma-separated ``order_ids`` query string → set of normalised ids.

    Used by the time-slots endpoint to scope the slot query to the
    seller's SELECTED orders. ``FbsOrder.order_id`` is a numeric STRING
    column, so each token is run through ``int()`` (validates it's numeric
    and strips whitespace / leading zeros) and re-stringified — giving a
    canonical form that compares cleanly against the column. Non-numeric or
    empty tokens are silently dropped; an absent/blank param yields the
    empty set, which the caller treats as "no scoping → legacy behaviour".
    """
    out: set[str] = set()
    for token in (raw or "").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            out.add(str(int(token)))
        except (TypeError, ValueError):
            continue
    return out


fbs_bp = Blueprint("fbs_bp", __name__)


@fbs_bp.before_request
def _claim_uzum_budget_for_this_screen():
    """Tell the FBS sync worker to step aside: a seller is on the screen.

    Uzum's budget is 2 req/s per token and the background sweep used to spend it
    all, so a chip press landed as the third call in that second and took a
    429 + retry (~2s instead of ~0.3s). The worker now yields while this flag is
    hot — see ``core.fbs_locks.pace_background_call``. It costs the worker
    nothing: its statuses run on 23-63 minute cadences.

    Scoped to the FBS blueprint on purpose (Abdulaziz 2026-07-13: "faqat FBS/DBS
    tizimida o'zgartirish"). The products/finance syncs share the same token
    bucket but are left alone.

    Best-effort and never fatal: an anonymous request, a user with no token, or
    a Redis hiccup just means no claim is made.
    """
    try:
        if not current_user.is_authenticated:
            return
        uid = int(current_user.get_id())
        with SessionLocal() as db:
            user = db.get(User, uid)
            token = (user.uzum_openapi_token or "").strip() if user else ""
        if token:
            from core.fbs_locks import mark_interactive_activity
            mark_interactive_activity(token)
    except Exception:
        pass


# Default status when the user lands on /fbs without picking one — the
# new-order queue is what sellers care about most.
DEFAULT_STATUS = "CREATED"


# The 4 "active work" chips a seller actually moves orders through
# (Yangi → Yig'ilmoqda → Jo'natishga tayyor → Yo'lda).
# On Yangilash press these MUST be fresh — Abdulaziz hit the bug where
# a buyer placed a new order and the "Yangi" chip stayed at 0 for 10
# minutes because the press only kicked off a fire-and-forget bg refresh.
# We now sync these 4 synchronously before returning. The other terminal
# statuses (DELIVERED/COMPLETED/cancellation/RETURNED/etc.) wait for
# the 10-min bg worker — sellers don't watch them in real time, so the
# extra Uzum calls per press aren't worth the wait.
#
# DELIVERING ("Yo'lda") added 2026-06-08 per Abdulaziz: he watches the
# on-the-way chip in real time too, so the one extra /count call per press
# (~1s, sequential + paced through the per-token gate → burst-safe) is
# worth it. Still one call per token even with multiple shops.
_FBS_REFRESH_ON_PRESS_SYNC: tuple[str, ...] = (
    "CREATED",
    "PACKING",
    "PENDING_DELIVERY",
    "DELIVERING",
)


def _parse_live_count_statuses(raw: str | None) -> tuple[str, ...]:
    """Parse the counts-all ``?statuses=`` param into the set of chips to
    LIVE-refresh from Uzum (Bosqich A.12).

    Rules:
      * empty / ``None`` → ALL active-work statuses (the default: refresh
        every active chip, preserving the pre-A.12 behaviour);
      * a comma list → only the named statuses that are active-work
        (``_FBS_REFRESH_ON_PRESS_SYNC``); terminal/unknown values are
        dropped, so a caller can never force a live /count on a terminal
        chip (those always come from the bg worker's DB write);
      * a list with NO valid active status → empty tuple → caller does no
        live refresh at all (every chip falls back to the DB read).

    Order always follows ``_FBS_REFRESH_ON_PRESS_SYNC`` so the result is
    stable regardless of how the caller ordered the input.
    """
    if not raw or not raw.strip():
        return _FBS_REFRESH_ON_PRESS_SYNC
    wanted = {s.strip().upper() for s in raw.split(",") if s.strip()}
    return tuple(s for s in _FBS_REFRESH_ON_PRESS_SYNC if s in wanted)

# Total seconds the route will spend on synchronous shop×status
# refreshes before falling back to whatever's in the DB. Per-token
# mutex serializes all Uzum calls anyway, so this is roughly:
# 3 statuses × ~1-2s each = ~3-6s for a typical user. The 8s cap
# protects against pathological Uzum latency from hanging the press.
_FBS_SYNC_DEADLINE = 8.0


# Bosqich A.11 — statuses we deliberately HIDE from the chip row.
# PENDING_CANCELLATION ("Bekor qilinmoqda") is a transient Uzum-internal
# state the seller can't act on; the chip was noise and syncing it burned
# a per-token quota unit on every full sweep. Hidden here AND dropped from
# the worker sweep (core.fbs_sync.FBS_ALL_SYNC_STATUSES). The canonical
# FBS_ORDER_STATUSES enum still includes it so the list/detail endpoints
# still validate + render any legacy row already cached in the DB.
FBS_HIDDEN_CHIP_STATUSES: tuple[str, ...] = ("PENDING_CANCELLATION",)

# The statuses actually rendered as chips, in lifecycle order, minus the
# hidden ones. Derived (not hand-listed) so it can never drift from the
# canonical enum: a new Uzum status shows up automatically unless hidden.
FBS_CHIP_STATUSES: tuple[str, ...] = tuple(
    s for s in FBS_ORDER_STATUSES if s not in FBS_HIDDEN_CHIP_STATUSES
)


# Human-readable labels for the status chips. Keys mirror FBS_ORDER_STATUSES
# (the raw Uzum enum); values are localized. The chip's data-status attribute
# keeps the English enum so backend/frontend communication is unchanged —
# only the visible label is translated.
STATUS_LABELS: dict[str, dict[str, str]] = {
    "uz": {
        "CREATED":                              "Yangi",
        "PACKING":                              "Yig'ilmoqda",
        "PENDING_DELIVERY":                     "Jo'natishga tayyor",
        "DELIVERING":                           "Yo'lda",
        "DELIVERED":                            "Yetkazildi",
        "ACCEPTED_AT_DP":                       "Qabul punktida",
        "DELIVERED_TO_CUSTOMER_DELIVERY_POINT": "Olish punktida",
        "COMPLETED":                            "Yakunlandi",
        "CANCELED":                             "Bekor qilindi",
        "PENDING_CANCELLATION":                 "Bekor qilinmoqda",
        "RETURNED":                             "Qaytarildi",
    },
    "ru": {
        "CREATED":                              "Новый",
        "PACKING":                              "Сборка",
        "PENDING_DELIVERY":                     "К отправке",
        "DELIVERING":                           "В пути",
        "DELIVERED":                            "Доставлен",
        "ACCEPTED_AT_DP":                       "В пункте приёма",
        "DELIVERED_TO_CUSTOMER_DELIVERY_POINT": "В пункте выдачи",
        "COMPLETED":                            "Завершён",
        "CANCELED":                             "Отменён",
        "PENDING_CANCELLATION":                 "Отменяется",
        "RETURNED":                             "Возвращён",
    },
}


def _current_user_token_and_shops() -> tuple[str | None, list[dict]]:
    """Return ``(token, [{uzum_id, name}, ...])`` for the logged-in user.

    Token is read from the user's row (admin or regular). If the user
    has no token yet, returns ``(None, shops)`` so the UI can render a
    hint pointing to /fetch (My Shops page) where the token is pasted.
    """
    uid = int(current_user.get_id())
    allowed_shop_db_ids = _user_shop_ids(uid)
    with SessionLocal() as db:
        user = db.get(User, uid)
        token = (user.uzum_openapi_token or "").strip() if user else ""
        if not allowed_shop_db_ids:
            return (token or None, [])
        shops = db.execute(
            select(Shop.uzum_id, Shop.name)
            .where(Shop.id.in_(allowed_shop_db_ids))
            .order_by(Shop.name.is_(None), Shop.name, Shop.uzum_id)
        ).all()
    return (token or None, [{"uzum_id": s.uzum_id, "name": s.name} for s in shops])


def _current_user_shop_uzum_ids() -> list[str]:
    """Uzum IDs of every shop the logged-in user can access.

    Used by the "Hammasi" branch of the list/count APIs to filter the
    DB query by ``shop_id IN (...)``. Empty list when the user has no
    shops attached.
    """
    uid = int(current_user.get_id())
    allowed_shop_db_ids = _user_shop_ids(uid)
    if not allowed_shop_db_ids:
        return []
    with SessionLocal() as db:
        rows = db.execute(
            select(Shop.uzum_id).where(Shop.id.in_(allowed_shop_db_ids))
        ).all()
    return [r.uzum_id for r in rows]


def _resolve_user_token_for_shop(shop_uzum_id: str) -> tuple[str | None, str | None]:
    """Look up the calling user's token + verify shop scope.

    Returns ``(token_or_None, error_message_or_None)``. ``error_message``
    is non-None when the caller has no access to the shop or has no
    OpenAPI token set; the route turns that into a 400/404 JSON response.
    """
    uid = int(current_user.get_id())
    allowed_shop_db_ids = _user_shop_ids(uid)
    with SessionLocal() as db:
        shop = db.execute(
            select(Shop).where(Shop.uzum_id == shop_uzum_id)
        ).scalar_one_or_none()
        if not shop or shop.id not in allowed_shop_db_ids:
            return (None, "Shop not found or not accessible")
        user = db.get(User, uid)
        token = (user.uzum_openapi_token or "").strip() if user else ""
    if not token:
        return (None, (
            "Uzum OpenAPI token not set. Open «My Shops» (/fetch) "
            "and paste your Seller OpenAPI token first."
        ))
    return (token, None)


# ── Pages ────────────────────────────────────────────────────────────


@fbs_bp.get("/fbs")
@login_required
def fbs_page():
    token, shops = _current_user_token_and_shops()
    lang = session.get("lang", "uz")
    labels = STATUS_LABELS.get(lang, STATUS_LABELS["uz"])
    list_labels = LIST_LABELS.get(lang, LIST_LABELS["uz"])
    return render_template(
        "fbs_orders.html",
        title="FBS / DBS — buyurtmalar",
        shops=shops,
        has_token=bool(token),
        default_status=DEFAULT_STATUS,
        status_enum=list(FBS_CHIP_STATUSES),
        status_labels=labels,
        list_labels=list_labels,
        scheme_enum=list(FBS_ORDER_SCHEMES),
    )


@fbs_bp.get("/fbs/invoices")
@login_required
def fbs_invoices_page():
    """Render the "Накладные" page (invoice list).

    Pure template render — data is fetched client-side via
    /fbs/api/invoices so we can show a loading state and keep the page
    fast even when Uzum is slow. Token presence is passed in so the
    template can warn if the seller hasn't set one up yet.
    """
    from core.uzum_openapi import FBS_INVOICE_STATUSES
    token, _ = _current_user_token_and_shops()
    lang = session.get("lang", "uz")
    # Same label dict the orders page uses — the "Изменить" pickup modal
    # reuses the rich drop-off picker UI (dropoff_* labels) ported from
    # fbs_orders.html, so it needs the localized strings.
    list_labels = LIST_LABELS.get(lang, LIST_LABELS["uz"])
    # ?embed=1 → render inside a bare layout (no sidebar/navbar/hero) so the
    # orders page can show this exact Накладные page INSIDE its content area
    # via <iframe> when the «Поставка» chip is clicked — no navigation away,
    # same data + same action handlers; only the surrounding chrome differs.
    embed = bool(request.args.get("embed"))
    layout = "_fbs_embed_base.html" if embed else "base.html"
    # ?detail=<id> → faqat bitta накладна detalini (ro'yxatsiz) render qiladi.
    # Orders sahifasi «Поставка» ro'yxatida qator bosilganda, bu sahifani
    # KENG MODAL ichidagi iframe sifatida ochadi (buyurtma-detal modali kabi):
    # fon o'zgarmaydi, detal ustki oynada chiqadi. renderDetailBody() qayta
    # ishlatiladi — dublikat yo'q.
    detail_id = request.args.get("detail") or None
    return render_template(
        "fbs_invoices.html",
        title="FBS — Yuk xatlari",
        has_token=bool(token),
        invoice_statuses=list(FBS_INVOICE_STATUSES),
        list_labels=list_labels,
        layout=layout,
        embed=embed,
        detail_id=detail_id,
    )


@fbs_bp.get("/fbs/<int:order_id>")
@login_required
def fbs_order_detail_page(order_id: int):
    # Detail data is fetched client-side via /fbs/api/order/<id> so the
    # page renders fast even when Uzum is slow. Stage 4 will switch the
    # data source to DB; this template doesn't care.
    lang = session.get("lang", "uz")
    labels = DETAIL_LABELS.get(lang, DETAIL_LABELS["uz"])
    status_labels = STATUS_LABELS.get(lang, STATUS_LABELS["uz"])
    # ?embed=1 → render inside a bare layout (no sidebar/navbar) so the
    # orders list can show this exact page inside a modal via <iframe>.
    # Same data + same action handlers; only the surrounding chrome differs.
    embed = bool(request.args.get("embed"))
    layout = "_fbs_embed_base.html" if embed else "base.html"
    return render_template(
        "fbs_order_detail.html",
        title=f"{labels['order_no']}{order_id}",
        order_id=order_id,
        labels=labels,
        status_labels=status_labels,
        layout=layout,
        embed=embed,
    )


# ── JSON APIs ────────────────────────────────────────────────────────


@fbs_bp.get("/fbs/api/orders")
@login_required
def fbs_orders_api():
    """JSON: list FBS/DBS orders for a shop.

    Query params:
      shop_id    — required, Uzum shop id (string of digits)
      status     — one of FBS_ORDER_STATUSES (default CREATED)
      scheme     — "FBS" / "DBS" / empty (both)
      page       — default 0
      size       — default 20, max 50 (Uzum caps)
      q          — optional, search substring matched against the
                   ``items_json`` JSONB column (catches productTitle,
                   skuTitle, and sku in a single ILIKE)
      date_from  — optional, ``YYYY-MM-DD`` lower bound on dateCreated
      date_to    — optional, ``YYYY-MM-DD`` upper bound on dateCreated
    """
    shop_id = (request.args.get("shop_id") or "").strip()
    if not shop_id:
        return _json_response({"error": "shop_id is required"}, 400)

    status_val = (request.args.get("status") or DEFAULT_STATUS).strip().upper()
    if status_val not in FBS_ORDER_STATUSES:
        return _json_response(
            {"error": f"status must be one of {list(FBS_ORDER_STATUSES)}"}, 400
        )

    scheme_val = (request.args.get("scheme") or "").strip().upper() or None
    if scheme_val and scheme_val not in FBS_ORDER_SCHEMES:
        return _json_response(
            {"error": f"scheme must be one of {list(FBS_ORDER_SCHEMES)} or empty"}, 400
        )

    try:
        page = max(0, int(request.args.get("page") or 0))
    except ValueError:
        page = 0
    try:
        size = max(1, min(50, int(request.args.get("size") or 20)))
    except ValueError:
        size = 20

    # Search + date filters (Bosqich 5a). q is normalized/trimmed so the
    # frontend's debounced fetch doesn't fire a no-op DB query on bare
    # whitespace. Dates come in as ``YYYY-MM-DD`` and convert to epoch
    # ms — date_to is shifted to end-of-day so a "2026-05-22 → 22" range
    # still includes orders created at 23:59 that day.
    q_val = (request.args.get("q") or "").strip() or None
    date_from_ms = _parse_yyyy_mm_dd_to_ms(request.args.get("date_from"), end_of_day=False)
    date_to_ms = _parse_yyyy_mm_dd_to_ms(request.args.get("date_to"), end_of_day=True)

    # "all" → aggregate across every shop this user can access.
    #
    # ACTIVE chips (Yangi / Yig'ilmoqda / Yo'lda + Jo'natishga tayyor) are
    # ALWAYS reconciled against Uzum first — every press, no ?refresh gate
    # (Abdulaziz 2026-07-13: "har bosilganda jonli olinsin, Uzum bilan 100%
    # bir xil bo'lsin"). ``refresh_shops_status_live`` drains the whole status
    # (not just the visible page) so the set is authoritative, then deletes the
    # rows Uzum no longer reports — which is what actually kills the phantom.
    # The old path here refreshed only page 0 and SKIPPED the prune whenever
    # that page came back full, so a status with more orders than the page size
    # could never shed a phantom.
    #
    # Terminal chips stay a pure DB read: their queues run to thousands of rows
    # (a drain would be punishingly slow) and sellers don't watch them live —
    # the 10-min worker keeps them current enough.
    if shop_id == "all":
        user_shops = _current_user_shop_uzum_ids()
        if user_shops and status_val in _FBS_REFRESH_ON_PRESS_SYNC:
            uid = int(current_user.get_id())
            with SessionLocal() as db:
                user = db.get(User, uid)
                token = (user.uzum_openapi_token or "").strip() if user else ""
            if token:
                from core.fbs_data import refresh_shops_status_live, invalidate_fbs_cache
                try:
                    refresh_shops_status_live(token, list(user_shops), status_val)
                except Exception as e:
                    print(f"[orders-all/live] shops={user_shops} status={status_val}: {e!r}")
                for sid in user_shops:
                    try:
                        invalidate_fbs_cache(sid)
                    except Exception:
                        pass
        orders, total, used_url = get_fbs_orders_for_shops(
            user_shops,
            status=status_val, scheme=scheme_val,
            page=page, size=size,
            date_from_ms=date_from_ms, date_to_ms=date_to_ms,
            q=q_val,
        )
        return _json_response({
            "shop_id": "all",
            "status": status_val,
            "scheme": scheme_val,
            "page": page,
            "size": size,
            "used_url": used_url,
            "total": total,
            "orders": orders,
            "last_synced_at": get_last_synced_at_for_shops(user_shops),
        })

    token, err = _resolve_user_token_for_shop(shop_id)
    if err:
        # 404 for ownership failure, 400 for missing token — caller
        # checks the message to distinguish.
        return _json_response({"error": err}, 404 if "not accessible" in err else 400)

    # Active chips are ALWAYS live (same rule as the "all" branch above): the
    # data layer reconciles the status against Uzum — full drain, upsert, prune
    # what Uzum no longer reports — and reads back with SWR bypassed. ?refresh=1
    # still forces the live path for a terminal chip, so an explicit refresh of
    # e.g. CANCELED keeps working.
    refresh = _refresh_requested() or status_val in _FBS_REFRESH_ON_PRESS_SYNC

    try:
        orders, total, used_url = get_fbs_orders(
            token, shop_id,
            status=status_val, scheme=scheme_val,
            page=page, size=size,
            date_from_ms=date_from_ms, date_to_ms=date_to_ms,
            q=q_val,
            refresh=refresh,
        )
    except Exception as e:
        return _json_response({"error": str(e)}, 502)

    return _json_response({
        "shop_id": shop_id,
        "status": status_val,
        "scheme": scheme_val,
        "page": page,
        "size": size,
        "used_url": used_url,
        "total": total,
        "orders": orders,
        # Stage 4d: drives the "Oxirgi yangilangan: N daq oldin" badge on
        # /fbs. None when no row of this shop has been synced yet.
        "last_synced_at": get_last_synced_at(shop_id),
    })


@fbs_bp.get("/fbs/api/count")
@login_required
def fbs_count_api():
    """JSON: single-status count for one of the status chips.

    Uzum's ``GET /v2/fbs/orders/count`` returns a count for ONE status
    at a time — the response payload is a bare integer. Frontend fans
    out one call per status to fill all chip badges.

    Query params:
      shop_id — required
      status  — required, one of FBS_ORDER_STATUSES
    """
    shop_id = (request.args.get("shop_id") or "").strip()
    if not shop_id:
        return _json_response({"error": "shop_id is required"}, 400)

    status_val = (request.args.get("status") or "").strip().upper()
    if status_val not in FBS_ORDER_STATUSES:
        return _json_response(
            {"error": f"status must be one of {list(FBS_ORDER_STATUSES)}"}, 400
        )

    token, err = _resolve_user_token_for_shop(shop_id)
    if err:
        return _json_response({"error": err}, 404 if "not accessible" in err else 400)

    # ?refresh=1: passthrough — get_fbs_count handles the JIT sync.
    refresh = _refresh_requested()

    try:
        count, used_url = get_fbs_count(
            token, shop_id, status=status_val, refresh=refresh,
        )
    except Exception as e:
        return _json_response({"error": str(e)}, 502)

    return _json_response({
        "shop_id": shop_id,
        "status": status_val,
        "used_url": used_url,
        "count": count,
    })


@fbs_bp.get("/fbs/api/counts-all")
@login_required
def fbs_counts_all_api():
    """JSON: per-status counts for ALL 11 statuses in one round-trip.

    Collapses the browser-side 11-way fan-out (which serialized through
    gunicorn's worker pool — typically 3 batches of ~4 calls each, since
    workers=4) into a single browser request. Inside, a
    ``ThreadPoolExecutor`` runs all 11 ``get_fbs_count`` calls in
    parallel. Each call still goes through SWR (``core.fbs_data``), so
    warm-cache hits are near-instant and only cold paths actually fire
    HTTP at Uzum.

    Returns shape::

        {
          "shop_id": "<id>",
          "counts":  {"CREATED": 3, "PACKING": 1, ..., "RETURNED": 0},
          "errors":  {"PACKING": "Uzum 502 ..."}   # only present if any failed
        }

    Partial failure policy: one slow/erroring status does NOT fail the
    whole response. Failed statuses are absent from ``counts`` and
    listed in ``errors``. Frontend leaves their chip badges as "·".

    Prerequisites:
      * Gunicorn worker class must be threaded (``gthread`` / ``gevent``);
        the default ``sync`` worker would serialize the inner threads
        and defeat the purpose. The project already uses gthread, no
        change needed.
    """
    shop_id = (request.args.get("shop_id") or "").strip()
    if not shop_id:
        return _json_response({"error": "shop_id is required"}, 400)

    # "all" → single GROUP BY query across every user-accessible shop.
    # No token, no fan-out, no Uzum hit. The worker keeps the DB fresh.
    # ?refresh=1 in this mode used to be a no-op — the DB returned stale
    # counts after invoice creation moved orders between statuses. Now
    # we fan out _FBS_REFRESH_ON_PRESS × user_shops in parallel before
    # the GROUP BY so the count reflects the post-invoice state.
    if shop_id == "all":
        user_shops = _current_user_shop_uzum_ids()
        # ``fresh_counts`` MUST be bound even when ?refresh is not requested
        # (the common case: initial load, tab switch, silent revalidate).
        # It used to be initialised inside the ``if _refresh_requested()``
        # block, so every non-refresh counts-all load raised
        # UnboundLocalError at the merge loop below and 500'd (the frontend
        # swallowed it silently, so the "all" chips just stayed empty until
        # a Yangilash press). Bind it up-front.
        fresh_counts: dict[str, int] = {}
        if _refresh_requested() and user_shops:
            uid = int(current_user.get_id())
            with SessionLocal() as db:
                user = db.get(User, uid)
                token = (user.uzum_openapi_token or "").strip() if user else ""
            if token:
                # Fast path: hit Uzum's ``/count`` endpoint for the active-
                # work chips. /count returns a single integer per call
                # (~200-500ms multi-shop), vs draining /orders pages. For
                # the non-active (terminal) chips we just read whatever the
                # bg worker last wrote — sellers don't watch them live.
                #
                # ``?statuses=CREATED,PACKING`` narrows which chips get a
                # LIVE /count (Bosqich A.12). The page-entry load uses it to
                # refresh ONLY the 2 chips it shows live (Yangi +
                # Yig'ilmoqda) instead of firing all 4 /count calls at once —
                # this kills the per-press burst. Only active-work statuses
                # are ever live-refreshed; anything else (terminal chips, or
                # an unknown value) is dropped and stays on the DB read.
                # Without the param we fall back to all active chips.
                #
                # Multi-shop optimization preserved: one /count call carries
                # every shopId for the user's token (N-shop admin pays one
                # call per status, not N).
                sync_statuses = _parse_live_count_statuses(request.args.get("statuses"))
                if sync_statuses:
                    from core.fbs_data import _refresh_shops_counts
                    try:
                        fresh_counts = _refresh_shops_counts(
                            token, list(user_shops), sync_statuses,
                        )
                    except Exception as e:
                        print(f"[counts-all/sync-refresh] shops={list(user_shops)}: {e!r}")
        counts = get_fbs_counts_for_shops(user_shops)
        # Merge: fresh /count values for active 3 override the DB read.
        # Non-active chips stay as the bg worker's last write.
        for status, n in fresh_counts.items():
            counts[status] = n
        return _json_response({
            "shop_id": "all",
            "counts": counts,
            "errors": {},
        })

    token, err = _resolve_user_token_for_shop(shop_id)
    if err:
        return _json_response({"error": err}, 404 if "not accessible" in err else 400)

    # Bosqich A.5 (QW3): one ``GROUP BY status`` over this shop's rows
    # instead of 11 parallel ``SELECT COUNT(*)`` (one thread per status).
    # ``get_fbs_counts_for_shops`` already accepts a list, so a one-element
    # list reuses the exact one-query path the "all" branch above uses.
    #
    # ?refresh=1: JIT-refresh ONLY the 3 active-work chips (CREATED,
    # PACKING, PENDING_DELIVERY) via Uzum's fast ``/count`` endpoint —
    # same policy as the "all" branch and the 2026-05-26 agreement, and
    # far cheaper than the old per-status ``/orders`` drain. The other 8
    # chips stay as the bg worker's last DB write (sub-10-minute current).
    refresh = _refresh_requested()
    errors: dict[str, str] = {}
    fresh_counts: dict[str, int] = {}
    if refresh and token:
        from core.fbs_data import _refresh_shops_counts
        try:
            fresh_counts = _refresh_shops_counts(
                token, [shop_id], _FBS_REFRESH_ON_PRESS_SYNC,
            )
        except Exception as e:
            print(f"[counts/sync-refresh] shop={shop_id}: {e!r}")
            errors["refresh"] = str(e)[:200]

    counts = get_fbs_counts_for_shops([shop_id])
    # Merge: fresh /count values for the active 3 override the DB read;
    # the non-active chips stay as the bg worker's last write.
    for status, n in fresh_counts.items():
        counts[status] = n

    body: dict = {"shop_id": shop_id, "counts": counts}
    if errors:
        body["errors"] = errors
    return _json_response(body)


@fbs_bp.get("/fbs/api/order/<int:order_id>")
@login_required
def fbs_order_detail_api(order_id: int):
    """JSON: single-order detail via ``GET /v1/fbs/order/{orderId}``.

    Currently 501 — swagger details for this endpoint not yet captured.
    Routing scaffold is here so the detail page can wire up the fetch
    call already; once ``get_fbs_order_detail`` is implemented, this
    flips to returning the real body.

    Scope check: we don't know the order's shop_id until we call Uzum,
    so the scope check happens against the response's ``shopId`` field
    after the call (rejecting orders from shops the user doesn't own).
    """
    uid = int(current_user.get_id())
    allowed_shop_db_ids = _user_shop_ids(uid)
    with SessionLocal() as db:
        user = db.get(User, uid)
        token = (user.uzum_openapi_token or "").strip() if user else ""
    if not token:
        return _json_response({
            "error": "Uzum OpenAPI token not set. Open «My Shops» (/fetch) first."
        }, 400)

    try:
        order, used_url = get_fbs_order_detail(token, order_id, fail_fast=True)
    except NotImplementedError as e:
        return _json_response({"error": str(e), "pending": True}, 501)
    except Exception as e:
        return _json_response({"error": str(e)}, 502)

    # Post-fetch scope check (we only know the shop after Uzum responds).
    order_shop_uzum_id = str(order.get("shopId") or "").strip()
    with SessionLocal() as db:
        shop = db.execute(
            select(Shop).where(Shop.uzum_id == order_shop_uzum_id)
        ).scalar_one_or_none()
        if not shop or shop.id not in allowed_shop_db_ids:
            return _json_response({"error": "Order not accessible"}, 404)

    # Localize each item's product title to the seller's language from our
    # own catalog (the FBS payload carries only a single-language title).
    lang = session.get("lang", "uz")
    items = order.get("orderItems") or []
    bcs = [it.get("barcode") for it in items if isinstance(it, dict)]
    if lang == "ru" and bcs:
        with SessionLocal() as db:
            by_bc, _ = _variant_titles(db, allowed_shop_db_ids, barcodes=bcs, lang=lang)
        for it in items:
            if not isinstance(it, dict):
                continue
            tup = by_bc.get(str(it.get("barcode") or ""))
            if not tup:
                continue
            ru_base, ru_color = tup
            new = _localize_product_title(
                it.get("title") or it.get("productTitle") or "", ru_base, ru_color, lang)
            it["title"] = new
            it["productTitle"] = new

    return _json_response({
        "order_id": order_id,
        "used_url": used_url,
        "order": order,
    })


# ── Stage 2: action endpoints ─────────────────────────────────────────


def _resolve_token_and_check_order(order_id: int):
    """Common pre-flight for every action endpoint.

    Steps:
      1. Read the calling user's OpenAPI token. If missing → 400.
      2. Fetch the order detail (cheapest reliable way to learn its
         ``shopId`` for the ownership check).
      3. Verify the order's shop is one the caller can access.

    Returns either:
      ``(token, shop_uzum_id, order_dict, None)`` on success, or
      ``(None, None, None, error_response)`` where ``error_response``
      is the Flask Response object ready to return.
    """
    uid = int(current_user.get_id())
    allowed_shop_db_ids = _user_shop_ids(uid)

    with SessionLocal() as db:
        user = db.get(User, uid)
        token = (user.uzum_openapi_token or "").strip() if user else ""
    if not token:
        return (None, None, None, _json_response({
            "error": _err("token_not_set_long")
        }, 400))

    try:
        order, _ = get_fbs_order_detail(token, order_id, fail_fast=True)
    except UzumAPIError as e:
        return (None, None, None, _uzum_error_response(e))
    except Exception as e:
        return (None, None, None, _json_response({"error": str(e)}, 502))

    shop_uzum_id = str(order.get("shopId") or "").strip()
    if not shop_uzum_id:
        return (None, None, None, _json_response(
            {"error": _err("order_not_found_or_denied")}, 404
        ))
    with SessionLocal() as db:
        shop = db.execute(
            select(Shop).where(Shop.uzum_id == shop_uzum_id)
        ).scalar_one_or_none()
        if not shop or shop.id not in allowed_shop_db_ids:
            return (None, None, None, _json_response(
                {"error": _err("order_not_found_or_denied")}, 404
            ))

    return (token, shop_uzum_id, order, None)


@fbs_bp.post("/fbs/api/order/<int:order_id>/confirm")
@login_required
def fbs_confirm_api(order_id: int):
    """POST: Confirm an FBS order. Body: none.

    Returns 200 + the post-confirm order JSON on success, or a
    UZ-localized error JSON. After success the per-shop cache is wiped
    so the next list view shows the new PACKING state.
    """
    token, shop_id, _order, err_resp = _resolve_token_and_check_order(order_id)
    if err_resp is not None:
        return err_resp

    try:
        order, used_url = confirm_order(token, order_id, shop_id, fail_fast=True)
    except UzumAPIError as e:
        return _uzum_error_response(e)
    except Exception as e:
        return _json_response({"error": str(e)}, 502)

    # Darhol-warm: confirmed → PACKING → label available + immutable. Fetch it in
    # the background so the seller's next "Yorliq" print is instant.
    _warm_labels_async(token, int(current_user.get_id()), [order_id])

    return _json_response({
        "ok": True,
        "order_id": order_id,
        "used_url": used_url,
        "order": order,
    })


@fbs_bp.post("/fbs/api/order/<int:order_id>/cancel")
@login_required
def fbs_cancel_api(order_id: int):
    """POST: Cancel an FBS order. Body JSON: {reason, comment?}.

    ``reason`` must come from the /fbs/api/return-reasons enum; ``comment``
    is optional free-text.

    Logs every stage with the ``[fbs.cancel]`` tag so you can follow a
    single attempt end-to-end with::
        docker compose logs -f app | grep fbs.cancel
    """
    t_total = time.perf_counter()
    payload = request.get_json(silent=True) or {}
    reason = (payload.get("reason") or "").strip()
    comment = payload.get("comment")
    print(f"[fbs.cancel] BEGIN order_id={order_id} reason={reason!r} "
          f"comment={(comment or '')[:60]!r} user_id={current_user.get_id()}", flush=True)
    if not reason:
        print(f"[fbs.cancel] REJECT order_id={order_id} — empty reason", flush=True)
        return _json_response({"error": _err("pick_cancel_reason")}, 400)

    t_resolve = time.perf_counter()
    token, shop_id, _order, err_resp = _resolve_token_and_check_order(order_id)
    print(f"[fbs.cancel] phase=resolve_token order={order_id} "
          f"took={time.perf_counter()-t_resolve:.2f}s", flush=True)
    if err_resp is not None:
        print(f"[fbs.cancel] REJECT order_id={order_id} — token/ownership check failed",
              flush=True)
        return err_resp

    try:
        body, used_url = cancel_order(
            token, order_id, shop_id, reason=reason, comment=comment,
            fail_fast=True,
        )
    except UzumAPIError as e:
        code = (getattr(e, "code", "") or "").lower()
        print(f"[fbs.cancel] UZUM_ERROR order_id={order_id} code={code or '?'} "
              f"msg={str(e)[:200]!r}", flush=True)
        # Self-heal stale cache: "already canceled" (seller-order-13) and
        # "order not found" (seller-order-01) both mean Uzum has already moved
        # this order on (typically an auto-cancel once the deadline passed),
        # while our row still shows it as active. Flip the local row to CANCELED
        # NOW so the phantom leaves the active list/count immediately, instead
        # of lingering until the next bg reconcile. No extra Uzum call.
        if code in ("seller-order-13", "seller-order-01"):
            try:
                from sqlalchemy import update as _sql_update
                from models import FbsOrder as _FbsOrder
                with SessionLocal() as _db:
                    _db.execute(
                        _sql_update(_FbsOrder)
                        .where(_FbsOrder.order_id == str(order_id))
                        .values(status="CANCELED")
                    )
                    _db.commit()
                print(f"[fbs.cancel] self-heal order_id={order_id} -> CANCELED "
                      f"(stale cache, code={code})", flush=True)
            except Exception as _he:
                print(f"[fbs.cancel] self-heal FAILED order_id={order_id}: {_he!r}", flush=True)
        return _uzum_error_response(e)
    except Exception as e:
        print(f"[fbs.cancel] EXCEPTION order_id={order_id} {type(e).__name__}: {e}",
              flush=True)
        return _json_response({"error": str(e)}, 502)

    # Optimistic local update: Uzum accepted the cancel, so flip our row to
    # CANCELED RIGHT NOW. Without this the order keeps showing as active
    # («Сборка») in the DB-backed list while Uzum's live count already
    # dropped it — the exact count(3) vs list(4) drift the seller sees right
    # after cancelling. ``body`` is usually {} so we don't read a status from
    # it; the periodic sync reconciles the precise terminal status later.
    try:
        from sqlalchemy import update as _sql_update
        from models import FbsOrder as _FbsOrder
        with SessionLocal() as _db:
            _db.execute(
                _sql_update(_FbsOrder)
                .where(_FbsOrder.order_id == str(order_id))
                .values(status="CANCELED")
            )
            _db.commit()
    except Exception as _ue:
        print(f"[fbs.cancel] optimistic-update FAILED order_id={order_id}: {_ue!r}", flush=True)

    print(f"[fbs.cancel] OK order_id={order_id} used_url={used_url} "
          f"total_took={time.perf_counter()-t_total:.2f}s", flush=True)
    return _json_response({
        "ok": True,
        "order_id": order_id,
        "used_url": used_url,
        "result": body,
    })


@fbs_bp.post("/fbs/api/order/<int:order_id>/identifiers")
@login_required
def fbs_identifiers_api(order_id: int):
    """POST: Attach IMEI/serial identifiers to order items.

    Body JSON:
        {"items": [
            {"orderItemId": int, "values": ["IMEI1", "IMEI2"]},
            ...
        ]}

    Empty values are filtered out client-side; backend additionally
    drops rows that arrive blank so Uzum never sees an empty values[].
    """
    payload = request.get_json(silent=True) or {}
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        return _json_response(
            {"error": _err("id_list_empty")}, 400
        )

    token, shop_id, _order, err_resp = _resolve_token_and_check_order(order_id)
    if err_resp is not None:
        return err_resp

    try:
        body, used_url = attach_identifiers(
            token, order_id, shop_id, items=items,
            fail_fast=True,
        )
    except UzumAPIError as e:
        return _uzum_error_response(e)
    except RuntimeError as e:
        # Validation failures from attach_fbs_identifiers (empty values
        # after normalization, bad shape) — surface as 400.
        return _json_response({"error": str(e)}, 400)
    except Exception as e:
        return _json_response({"error": str(e)}, 502)

    return _json_response({
        "ok": True,
        "order_id": order_id,
        "used_url": used_url,
        "result": body,
    })


@fbs_bp.get("/fbs/api/order/<int:order_id>/label")
@login_required
def fbs_label_api(order_id: int):
    """GET ?size=LARGE|BIG&mode=full|qr|shipping_qr: Returns base64-encoded label PDFs.

    Why JSON-base64 rather than streaming raw PDF bytes? Uzum can return
    multiple PDFs in one response (multi-package orders). Letting the
    browser receive an array and create each blob/tab itself is simpler
    than synthesizing a multi-part download server-side.

    Modes:
      * ``full``         (default) Uzum's whole shipping label
      * ``qr``           Product QR sticker(s) per orderItem — no
                         shipping label, no crop. Matches the FBO
                         "print QR" semantics sellers are used to.
      * ``shipping_qr``  Legacy: shipping label cropped to the right
                         QR column.

    Failures fall back to the original label so a parse / render hiccup
    doesn't block the seller from printing.
    """
    size = (request.args.get("size") or "LARGE").strip().upper()
    if size not in ("LARGE", "BIG"):
        size = "LARGE"
    mode = (request.args.get("mode") or "full").strip().lower()
    if mode not in ("full", "qr", "shipping_qr", "enlarged"):
        mode = "full"

    token, _shop_id, order, err_resp = _resolve_token_and_check_order(order_id)
    if err_resp is not None:
        return err_resp

    try:
        pdfs, used_url = get_label_pdfs(token, order_id, size=size, fail_fast=True)
    except UzumAPIError as e:
        return _uzum_error_response(e)
    except Exception as e:
        return _json_response({"error": str(e)}, 502)

    if mode == "shipping_qr":
        cropped: list[bytes] = []
        for raw in pdfs:
            c = crop_label_to_qr(raw)
            cropped.append(c if c else raw)  # graceful fallback per page
        pdfs = cropped
    elif mode == "enlarged":
        # Re-style the shipping label with bigger left-side text.
        # Falls back to the original on render failure so the seller
        # still gets a usable label.
        order_row = {
            "order_id": order_id,
            "items": order.get("orderItems") if isinstance(order, dict) else [],
            "customer_fullname": (order or {}).get("customer_fullname"),
            "delivery_address": (order or {}).get("delivery_address"),
        }
        enlarged: list[bytes] = []
        for raw in pdfs:
            e_pdf = render_enlarged_label_pdf(raw, order_row=order_row)
            enlarged.append(e_pdf if e_pdf else raw)
        pdfs = enlarged
    elif mode == "qr":
        # Replace shipping label PDFs with product QR sticker(s) — one per
        # UNIT (amount), not per orderItem line. Use the order detail's
        # orderItems (already fetched during _resolve_token_and_check_order).
        items = order.get("orderItems") if isinstance(order, dict) else []
        product_pdfs = _item_qr_pdfs(items)
        if product_pdfs:
            pdfs = product_pdfs

    pdfs_b64 = [base64.b64encode(p).decode("ascii") for p in pdfs]
    return _json_response({
        "ok": True,
        "order_id": order_id,
        "used_url": used_url,
        "size": size,
        "mode": mode,
        "count": len(pdfs_b64),
        "pdfs_base64": pdfs_b64,
    })


# ── Bosqich 5b: bulk endpoints (multi-select + print + confirm) ──────
# Both endpoints validate every order in ONE SQL query against
# ``fbs_orders`` rather than calling Uzum N times. The ``fbs_orders``
# table is the source of truth for ownership (shop_id is denormalized
# onto each row), so a single ``SELECT ... WHERE order_id IN (...)``
# tells us both "does this order exist" and "which shop does it belong
# to" with no Uzum traffic. The cost is that orders the worker hasn't
# synced yet won't be selectable — acceptable since the toolbar only
# appears after the order list renders, which means those orders are
# already in the DB by definition.

# Bulk operations are capped to keep one click from spawning a
# pathological burst. 50 = "one page worth + a comfort margin"; Uzum's
# rate budget can handle ~5 confirms/sec per token, so 50 confirms ≈ 10s
# of sustained calls, well below burst-penalty territory.
_BULK_LIMIT = 50


def _parse_bulk_order_ids() -> tuple[list[int] | None, object | None]:
    """Pull ``order_ids`` out of a JSON body, normalize + dedupe.

    Returns ``(ids_list, None)`` on success, or ``(None, error_response)``
    when the input is shaped wrong. Validates:
      * body is a dict with ``order_ids: [...]``
      * list is non-empty after filtering bad values
      * size ≤ ``_BULK_LIMIT``
    """
    payload = request.get_json(silent=True) or {}
    raw = payload.get("order_ids")
    if not isinstance(raw, list) or not raw:
        return (None, _json_response(
            {"error": _err("order_ids_empty")}, 400
        ))

    ids: list[int] = []
    seen: set[int] = set()
    for v in raw:
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if n in seen:
            continue
        seen.add(n)
        ids.append(n)

    if not ids:
        return (None, _json_response(
            {"error": _err("order_ids_no_valid")}, 400
        ))
    if len(ids) > _BULK_LIMIT:
        return (None, _json_response(
            {"error": _err("bulk_limit", n=_BULK_LIMIT)}, 400
        ))
    return (ids, None)


def _resolve_bulk_orders(order_ids: list[int]) -> tuple[list[dict] | None, object | None]:
    """Look up rows in fbs_orders + verify the calling user owns each shop.

    Returns ``(rows, None)`` where ``rows`` is a list of dicts shaped
    ``{order_id: int, shop_uzum_id: str, status: str}`` — one per
    requested ID, in the same order. Returns ``(None, error_response)``
    when any ID is missing from the DB or belongs to a shop the user
    can't access — we don't process partial sets, since the seller
    would have no way to know which orders were skipped.

    The user's OpenAPI token is also resolved here. If absent → 400.
    """
    uid = int(current_user.get_id())
    allowed_shop_db_ids = _user_shop_ids(uid)

    with SessionLocal() as db:
        user = db.get(User, uid)
        token = (user.uzum_openapi_token or "").strip() if user else ""
        if not token:
            return (None, _json_response({
                "error": _err("token_not_set_long")
            }, 400))

        # Map user shop ids → Uzum ids for the ownership check.
        owned_uzum_ids = set(
            r.uzum_id for r in db.execute(
                select(Shop.uzum_id).where(Shop.id.in_(allowed_shop_db_ids))
            ).all()
        ) if allowed_shop_db_ids else set()

        # Stringify because FbsOrder.order_id is String(64).
        id_strs = [str(i) for i in order_ids]
        rows_db = db.execute(
            select(
                FbsOrder.order_id, FbsOrder.shop_id, FbsOrder.status,
                FbsOrder.items_json,
            )
            .where(FbsOrder.order_id.in_(id_strs))
        ).all()

    found = {r.order_id: r for r in rows_db}
    missing = [i for i in order_ids if str(i) not in found]
    if missing:
        sample = ", ".join(str(m) for m in missing[:3])
        return (None, _json_response({
            "error": _err("orders_not_found_synced", sample=sample)
            + ("…" if len(missing) > 3 else "")
        }, 404))

    rows: list[dict] = []
    for oid in order_ids:
        r = found[str(oid)]
        if r.shop_id not in owned_uzum_ids:
            return (None, _json_response({
                "error": _err("order_no_access", oid=oid)
            }, 404))
        rows.append({
            "order_id": oid,
            "shop_uzum_id": r.shop_id,
            "status": r.status,
            "items": list(r.items_json) if r.items_json else [],
        })

    # Stash the token on the request so callers don't have to re-resolve.
    request.environ["_fbs_bulk_token"] = token
    return (rows, None)


def _item_qr_pdfs(items, product_qr_size=None, *, match_uzum_label=False) -> list[bytes]:
    """Render the product-QR sticker for each order item, repeated PER UNIT.

    Uzum's ``orderItems[].amount`` is the piece count: an item with amount=3 is
    three physical pieces, each needing its OWN sticker. One sticker per SKU
    line was a bug (Abdulaziz 2026-06-12: 9 dona tovarga 4 ta QR chiqdi). The
    sticker is identical for every unit of the same SKU, so render once and
    repeat the bytes ``amount`` times.
    """
    out: list[bytes] = []
    for item in (items or []):
        qr_pdf = (
            render_product_qr_pdf(item, size=product_qr_size,
                                  match_uzum_label=match_uzum_label)
            if product_qr_size
            else render_product_qr_pdf(item, match_uzum_label=match_uzum_label)
        )
        if not qr_pdf:
            continue
        try:
            qty = int(item.get("amount") or 1)
        except (TypeError, ValueError):
            qty = 1
        out.extend([qr_pdf] * max(1, qty))
    return out


def _get_label_pdfs_429_retry(token, order_id, *, size="LARGE", attempts=3, wait=4.0):
    """``get_label_pdfs`` with a SHORT 429-aware retry — same shape as
    ``core.fbs_akt_cache.fetch_akt_live``.

    The bulk fan-out fires several label calls per click; Uzum's per-token
    print bucket 429s after a few rapid hits and refills in ~3-4s (see the
    akt-print rate-limit reference). On a 429 we wait ``wait`` s and retry up
    to ``attempts`` times instead of counting the order as a hard error — the
    per-token pacing gate inside ``download_fbs_label`` re-spaces the retries
    ~1s apart so they don't re-burst. ``fail_fast=True`` keeps us off the
    shared session's 60/120/180s storm.
    """
    last_exc = None
    for attempt in range(attempts):
        try:
            return get_label_pdfs(token, order_id, size=size, fail_fast=True)
        except UzumAPIError as e:
            if getattr(e, "http_status", None) == 429 and attempt < attempts - 1:
                time.sleep(wait)
                last_exc = e
                continue
            raise
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("_get_label_pdfs_429_retry: no result")


@fbs_bp.get("/fbs/print/qr")
@login_required
def fbs_print_qr_page():
    """HTML page of product-QR stickers for the selected orders, printed by the
    BROWSER — the EXACT mechanism the POS «QR Chop etish» page
    (templates/print_labels.html) uses. The QR is an <img> from api.qrserver.com
    scaled with image-rendering:pixelated, so it prints razor-sharp with no PDF
    rasteriser blur (Abdulaziz 2026-06-16: server-PDF QR read as "past sifat";
    seller pointed to the FBO page as the reference — we now match it exactly).
    One sticker per UNIT (orderItems[].amount). Ownership-guarded via
    ``_resolve_bulk_orders``.
    """
    lang = session.get("lang", "uz")
    _qr_i18n = QR_PRINT_LABELS.get(lang, QR_PRINT_LABELS["uz"])
    ids_str = request.args.get("order_ids") or ""
    ids = [int(x) for x in ids_str.split(",") if x.strip().isdigit()]
    if not ids:
        return _qr_i18n["err_no_order"], 400

    # Dimensions MIRROR the FBO «QR Chop etish» page EXACTLY
    # (products/routes.py LABEL_SIZES) so the FBS QR sticker prints at the same
    # physical size on the same thermal roll. `_PRODUCT_LABEL_SIZES` (PDF path)
    # used a slightly larger qr/tcol for 30×20 and 40×30 which overflowed the
    # label (Abdulaziz 2026-06-16: "qog'ozga sig'may qolmoqda"). Kept local so
    # FBS stays self-contained.
    _QR_PRINT_SIZES = {
        "30x20": {"w": 30, "h": 20, "tcol": 7,  "qr": 14, "sku_fs": 5.4, "num_fs": 6, "num_last4_fs": 8},
        "43x25": {"w": 43, "h": 25, "tcol": 8,  "qr": 20, "sku_fs": 6,   "num_fs": 7, "num_last4_fs": 9},
        "40x30": {"w": 40, "h": 30, "tcol": 9,  "qr": 20, "sku_fs": 6.5, "num_fs": 7, "num_last4_fs": 9},
        "60x60": {"w": 60, "h": 60, "tcol": 12, "qr": 34, "sku_fs": 8,   "num_fs": 8, "num_last4_fs": 11},
        "70x37": {"w": 70, "h": 37, "tcol": 12, "qr": 40, "sku_fs": 8,   "num_fs": 8, "num_last4_fs": 11},
    }
    size = (request.args.get("size") or "40x30").strip().lower()
    # "uzum" (242×166mm) is the full-page merge size — meaningless for a
    # standalone QR sticker, so fall back to a sane thermal preset.
    if size not in _QR_PRINT_SIZES:
        size = "40x30"
    lbl = _QR_PRINT_SIZES[size]

    rows, err = _resolve_bulk_orders(ids)
    if err is not None:
        return err

    labels: list[dict] = []
    for r in rows:
        for item in (r.get("items") or []):
            sku = str(item.get("skuTitle") or "").strip()
            barcode = str(item.get("barcode") or "").strip()
            content = barcode or sku
            if not content:
                continue
            try:
                qty = int(item.get("amount") or 1)
            except (TypeError, ValueError):
                qty = 1
            for _ in range(max(1, qty)):
                labels.append({"sku": sku or content, "barcode": content})

    if not labels:
        return _qr_i18n["err_no_goods"], 400
    return render_template("fbs_qr_print.html", labels=labels, lbl=lbl, i18n=_qr_i18n)


@fbs_bp.post("/fbs/api/orders/bulk-labels")
@login_required
def fbs_bulk_labels_api():
    """POST: Download multiple order labels merged into ONE PDF.

    Body JSON: ``{"order_ids": [int, ...], "size": "LARGE"|"BIG"}``

    Returns ``application/pdf`` with a synthesized filename like
    ``labels-2026-05-24-N5.pdf`` so the seller's browser saves it with a
    meaningful name rather than the route URL.

    Failure policy: any orders that error during the per-order Uzum call
    are skipped (logged to stdout), and the merged PDF only contains
    labels for the orders that succeeded. The response header
    ``X-Fbs-Bulk-Errors`` reports the count of skipped orders so the
    frontend can surface a partial-success toast. If EVERY order
    errored, we return 502 instead of an empty PDF.
    """
    ids, err = _parse_bulk_order_ids()
    if err is not None:
        return err
    payload = request.get_json(silent=True) or {}
    size = (payload.get("size") or "LARGE").strip().upper()
    if size not in ("LARGE", "BIG"):
        size = "LARGE"
    # mode controls what gets put in the merged PDF:
    #   "full"          → Uzum's whole shipping label (default)
    #   "qr"            → product QR sticker per item — NO shipping
    #                     label. Matches FBO "Печать QR" semantics:
    #                     sellers expect "QR chop etish" to give them
    #                     the product code, not Uzum's shipping QR.
    #   "label_with_qr" → shipping label + a product QR sticker per item
    #                     in the order (warehouse picking workflow)
    #   "qr_with_label" → product QR sticker per item FIRST, then the
    #                     shipping label (warehouse picks by QR, label
    #                     comes after for packaging)
    #   "shipping_qr"   → legacy: shipping label cropped to the right
    #                     QR column. Kept for API back-compat.
    mode = (payload.get("mode") or "full").strip().lower()
    if mode not in ("full", "qr", "label_with_qr", "qr_with_label", "shipping_qr"):
        mode = "full"
    # product_qr_size — physical size of the product-QR sticker pages. The
    # seller's dropdown choice ALWAYS rules now, in EVERY mode (Abdulaziz
    # 2026-06-17): «Yorliq + QR» must render the QR at the SAME size as the
    # standalone «QR» button (both read this exact value). We used to OVERRIDE
    # to "uzum" here for the mixed modes — that made the merged QR a different
    # (bigger) proportion than the «QR» print. The QR page is rotated to the
    # label's portrait+/Rotate-90 geometry (match_uzum_label) so it still prints
    # consistently with the shipping label regardless of the sticker size.
    product_qr_size = (payload.get("product_qr_size") or "").strip().lower() or None

    rows, err = _resolve_bulk_orders(ids)
    if err is not None:
        return err
    token = request.environ.get("_fbs_bulk_token") or ""

    pdfs: list[bytes] = []
    errors: list[dict] = []
    if mode == "qr":
        # Pure product-QR print: stickers are rendered LOCALLY from each
        # order's items (barcode/SKU) — we NEVER call Uzum's label endpoint.
        # Two wins:
        #   • DBS buyurtmalar uchun ham ishlaydi — Uzum DBS'ga FBS jo'natma
        #     yorlig'i bermaydi («fbs-18-invalid-order-type»), lekin tovar QR
        #     bizniki: uni har qanday buyurtma uchun chizamiz.
        #   • Uzum'ning per-token 429 print-bucket'iga umuman tegmaydi.
        for r in rows:
            items = r.get("items") or []
            if not items:
                errors.append({"order_id": r["order_id"], "error": _err("no_item")})
                continue
            # Bir DONAga bitta stiker (amount hisobida) — _item_qr_pdfs.
            pdfs.extend(_item_qr_pdfs(items, product_qr_size))
    else:
        # Bosqich 5f — DB label cache first. The background sync worker pre-warms
        # each active order's label into ``fbs_order_labels`` (paced, never 429),
        # so in steady state EVERY order here is a DB hit → the whole bulk print
        # is instant, zero Uzum calls. A cache MISS (order not warmed yet — e.g.
        # a накладна just created) falls back to the live paced fetch + stores the
        # result, so the next print is instant too. The label is immutable once
        # confirmed (verified 2026-06-12), so a cached row is never stale.
        #
        # Cache only the LARGE size the warm path stores, and skip it for the
        # legacy ``shipping_qr`` crop (which needs the raw per-package list).
        uid = int(current_user.get_id())
        use_label_cache = (size == "LARGE" and mode != "shipping_qr")

        def _fetch_order_label_pdfs(order_id):
            """Return this order's label PDF(s) as a list — DB-cached when
            possible, else a live paced fetch (which we then store)."""
            if use_label_cache:
                cached = get_cached_label(uid, order_id, size=size)
                if cached:
                    return [cached]
            raw, _ = _get_label_pdfs_429_retry(token, order_id, size=size)
            if use_label_cache and raw:
                try:
                    store_label(uid, order_id, size,
                                merge_label_pdfs(raw) if len(raw) > 1 else raw[0])
                except Exception as e:
                    print(f"[bulk-labels] cache store failed for {order_id}: {e!r}")
            return raw

        # Fan-out is parallel but burst-safe: each LIVE download_fbs_label call
        # reserves a per-token slot via the shared gate (core.fbs_locks
        # .pace_uzum_call), so concurrent MISSES interleave ~1s apart instead of
        # bursting Uzum's 429. DB hits skip Uzum entirely. Wall-clock ≈ (misses−1)×1s.
        with ThreadPoolExecutor(max_workers=min(5, len(rows))) as pool:
            future_to_row = {
                pool.submit(_fetch_order_label_pdfs, r["order_id"]): r
                for r in rows
            }
            for fut in as_completed(future_to_row):
                r = future_to_row[fut]
                try:
                    order_pdfs = fut.result()
                    if order_pdfs:
                        if mode == "shipping_qr":
                            # Legacy: crop shipping label to QR column.
                            for raw in order_pdfs:
                                cropped = crop_label_to_qr(raw)
                                pdfs.append(cropped if cropped else raw)
                        elif mode == "label_with_qr":
                            # Shipping label first, then a product QR sticker
                            # per orderItem. The whole order's items come from
                            # the DB row we resolved earlier — no extra Uzum
                            # call, the items_json column already has them.
                            pdfs.extend(order_pdfs)
                            # Har DONAga bitta QR (amount hisobida). QR sahifasi
                            # Uzum yorlig'i bilan bir xil orientatsiyada bo'lishi
                            # uchun match_uzum_label=True (aks holda QR siqilib
                            # kichrayadi — Abdulaziz 2026-06-17).
                            pdfs.extend(_item_qr_pdfs(r.get("items"), product_qr_size,
                                                      match_uzum_label=True))
                        elif mode == "qr_with_label":
                            # Reverse order: product QR stickers FIRST, then
                            # the shipping label. Some warehouses pick by QR
                            # before printing the shipping label, others do
                            # the opposite — we support both flows.
                            pdfs.extend(_item_qr_pdfs(r.get("items"), product_qr_size,
                                                      match_uzum_label=True))
                            pdfs.extend(order_pdfs)
                        else:
                            pdfs.extend(order_pdfs)
                    else:
                        errors.append({"order_id": r["order_id"], "error": "no PDF"})
                except UzumAPIError as e:
                    errors.append({"order_id": r["order_id"],
                                   "error": e.message or e.code or "Uzum error",
                                   "uzum_trace": e.trace or None})
                except Exception as e:
                    errors.append({"order_id": r["order_id"], "error": str(e)[:120]})

    if not pdfs:
        # Everyone failed — return the first error verbatim so the seller
        # has a clue. Still 502 because nothing usable came back.
        first_err = errors[0]["error"] if errors else "labels unavailable"
        return _json_response({
            "error": _err("no_labels", first_err=first_err),
            "errors": errors,
        }, 502)

    merged = merge_label_pdfs(pdfs)
    if not merged:
        return _json_response({
            "error": _err("pdf_merge_error"),
            "errors": errors,
        }, 502)

    # Filename: easy date stamp + count so the seller knows which batch
    # this is when they have 5 downloads in their Downloads folder.
    from datetime import date as _date
    suffix = {
        "qr": "-qr",
        "shipping_qr": "-ship-qr",
        "label_with_qr": "-with-qr",
        "qr_with_label": "-qr-first",
    }.get(mode, "")
    fname = f"labels{suffix}-{_date.today().isoformat()}-N{len(rows)}.pdf"

    resp = Response(merged, mimetype="application/pdf")
    resp.headers["Content-Disposition"] = f'attachment; filename="{fname}"'
    resp.headers["X-Fbs-Bulk-Total"] = str(len(rows))
    resp.headers["X-Fbs-Bulk-Errors"] = str(len(errors))
    resp.headers["X-Fbs-Bulk-Success"] = str(len(rows) - len(errors))
    return resp


@fbs_bp.post("/fbs/api/orders/bulk-confirm")
@login_required
def fbs_bulk_confirm_api():
    """POST: Confirm many CREATED orders in one click.

    Body JSON: ``{"order_ids": [int, ...]}``

    Per-order outcome is reported back in ``results``::

        {"results": [
            {"order_id": 12345, "ok": true,  "status": "PACKING"},
            {"order_id": 12346, "ok": false, "error": "Bu harakatga..."},
            ...
        ],
         "success_count": 4,
         "error_count": 1}

    Frontend uses the per-row outcome to refresh checkmarks + show a
    toast like "4 ta tasdiqlandi, 1 ta xatolik". Orders that aren't in
    CREATED status are skipped pre-flight (reported as an error) — we
    don't even try the Uzum call, since Uzum would just return
    seller-order-02 and we'd burn a rate-limit slot for nothing.
    """
    ids, err = _parse_bulk_order_ids()
    if err is not None:
        return err
    rows, err = _resolve_bulk_orders(ids)
    if err is not None:
        return err
    token = request.environ.get("_fbs_bulk_token") or ""

    # Pre-filter: only CREATED orders can be confirmed. Anything else
    # gets an error entry without an Uzum call.
    results: list[dict] = []
    confirmable: list[dict] = []
    for r in rows:
        if (r["status"] or "").upper() != "CREATED":
            results.append({
                "order_id": r["order_id"], "ok": False,
                "error": _err("only_created_confirm"),
                "status": r["status"],
            })
        else:
            confirmable.append(r)

    # Fan-out the actual confirms. Each confirm_fbs_order reserves a
    # per-token slot via the shared gate (pace_uzum_call, Bosqich A.8/#4),
    # so the 5-wide fan-out interleaves ~1s apart instead of bursting
    # Uzum's 429 penalty. Same burst-safe serialisation as bulk labels.
    if confirmable:
        with ThreadPoolExecutor(max_workers=min(5, len(confirmable))) as pool:
            future_to_row = {
                pool.submit(
                    confirm_order, token, r["order_id"], r["shop_uzum_id"],
                    fail_fast=True,
                ): r for r in confirmable
            }
            for fut in as_completed(future_to_row):
                r = future_to_row[fut]
                try:
                    order, _ = fut.result()
                    new_status = (order or {}).get("status") or "PACKING"
                    results.append({
                        "order_id": r["order_id"], "ok": True,
                        "status": new_status,
                    })
                except UzumAPIError as e:
                    code = e.code or ""
                    _lang = _session_lang()
                    _msgs = FBS_ERROR_MESSAGES.get(_lang, FBS_ERROR_MESSAGES["uz"])
                    msg = (
                        _msgs.get(code) or FBS_ERROR_MESSAGES["uz"].get(code)
                        or e.message
                        or (f"Ошибка Uzum (HTTP {e.http_status})" if _lang == "ru"
                            else f"Uzum xatosi (HTTP {e.http_status})")
                    )
                    _detail = _format_uzum_payload_detail(e.error_payload)
                    if _detail and _detail not in msg:
                        msg = f"{msg} ({_detail})"
                    results.append({
                        "order_id": r["order_id"], "ok": False,
                        "error": msg, "uzum_code": code or None,
                        "uzum_trace": e.trace or None,
                    })
                except Exception as e:
                    results.append({
                        "order_id": r["order_id"], "ok": False,
                        "error": str(e)[:160],
                    })

    # Reorder results to match the input order so the frontend can map
    # row → outcome without an O(N) scan. ThreadPool returns in completion
    # order which is non-deterministic.
    by_id = {r["order_id"]: r for r in results}
    ordered = [by_id[i] for i in ids if i in by_id]

    success_count = sum(1 for r in ordered if r["ok"])
    error_count = len(ordered) - success_count

    # Darhol-warm: just-confirmed orders are now PACKING with an available,
    # immutable label — fetch them in the background so the seller's next
    # "Yorliq" print is instant (no 10-min sync-warm wait).
    _warm_labels_async(token, int(current_user.get_id()),
                       [r["order_id"] for r in ordered if r.get("ok")])

    return _json_response({
        "ok": error_count == 0,
        "results": ordered,
        "success_count": success_count,
        "error_count": error_count,
    })


def _partition_confirmable(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split resolved order rows into ``(confirmable, skipped)``.

    Only CREATED orders can be confirmed. Anything else is reported as a
    skip WITHOUT an Uzum call — Uzum would just return seller-order-02 and
    we'd burn a rate-limit slot (and risk a penalty) for nothing. Extracted
    to a module-level helper so it's unit-testable without a request context.
    """
    confirmable: list[dict] = []
    skipped: list[dict] = []
    for r in rows:
        if (r["status"] or "").upper() != "CREATED":
            skipped.append({
                "order_id": r["order_id"],
                "error": _err("only_created_confirm"),
                "status": r["status"],
            })
        else:
            confirmable.append({
                "order_id": r["order_id"],
                "shop_uzum_id": r["shop_uzum_id"],
            })
    return confirmable, skipped


# Async bulk-confirm is a BACKGROUND job with no user waiting, so a 429
# ("you nudged just past the per-token bucket at the boundary") is SAFE to
# wait out and retry — unlike the synchronous/interactive path, which fails
# fast (Bosqich A.9) to avoid pinning a request thread. We retry ONLY on 429:
# a 429 means Uzum REJECTED the call (it was never processed), so re-confirming
# has no double-action risk; any OTHER error re-raises immediately (penalty
# risk). The short wait lets the shared 2/s bucket — and Uzum's own bucket —
# refill. Observed cause (2026-06-17): a confirm landed when Uzum's `remaining`
# was momentarily 0 because the live list-poll + bg worker were drawing from
# the same token in the same second.
_BULK_CONFIRM_429_RETRIES = 3
_BULK_CONFIRM_429_WAIT_SEC = 1.5


def _confirm_with_429_retry(token, order_id, shop_uzum_id):
    """``confirm_order``, but patiently retry a transient 429 (background only)."""
    last_err = None
    for attempt in range(_BULK_CONFIRM_429_RETRIES):
        try:
            return confirm_order(token, order_id, shop_uzum_id, fail_fast=True)
        except UzumAPIError as e:
            if e.http_status == 429 and attempt < _BULK_CONFIRM_429_RETRIES - 1:
                print(f"[bulk-confirm-async] 429 on order {order_id} — wait "
                      f"{_BULK_CONFIRM_429_WAIT_SEC}s + retry "
                      f"({attempt + 1}/{_BULK_CONFIRM_429_RETRIES})", flush=True)
                last_err = e
                time.sleep(_BULK_CONFIRM_429_WAIT_SEC)
                continue
            raise
    raise last_err  # defensive: loop always returns or raises above


def _run_bulk_confirm_job(job_id: str, token: str, confirmable: list[dict],
                          lang: str, user_id: int) -> None:
    """Background worker for the async bulk-confirm (runs on a daemon thread).

    Confirms each order one-by-one — ``confirm_order`` writes PACKING to the
    DB row + invalidates the per-shop cache the instant it lands, so the
    poller sees the flip immediately. Pacing is automatic: every confirm
    reserves a slot in the shared 2/s token bucket inside the chokepoint, so
    this never bursts Uzum's 429. Each outcome (success or per-order error)
    is recorded; an error NEVER aborts the loop. Module-level (not a route
    closure) so it can be unit-tested with mocked confirm_order — no Flask
    request context, no real threads.
    """
    for c in confirmable:
        oid = c["order_id"]
        try:
            _confirm_with_429_retry(token, oid, c["shop_uzum_id"])
            record_result(job_id, oid, ok=True)
        except UzumAPIError as e:
            code = e.code or ""
            _msgs = FBS_ERROR_MESSAGES.get(lang, FBS_ERROR_MESSAGES["uz"])
            msg = (
                _msgs.get(code) or FBS_ERROR_MESSAGES["uz"].get(code)
                or e.message
                or (f"Ошибка Uzum (HTTP {e.http_status})" if lang == "ru"
                    else f"Uzum xatosi (HTTP {e.http_status})")
            )
            _detail = _format_uzum_payload_detail(e.error_payload, lang)
            if _detail and _detail not in msg:
                msg = f"{msg} ({_detail})"
            record_result(job_id, oid, ok=False, error=msg, code=code or None)
        except Exception as e:
            record_result(job_id, oid, ok=False, error=str(e)[:160])
    finish_job(job_id)
    # Just-confirmed orders are now PACKING with an available, immutable label
    # — warm the cache so the seller's next "Yorliq" print is instant. Best-
    # effort, off the confirm loop.
    try:
        _warm_labels_async(token, user_id, [c["order_id"] for c in confirmable])
    except Exception as e:
        print(f"[bulk-confirm-async] label warm failed: {e!r}")


@fbs_bp.post("/fbs/api/orders/bulk-confirm-async")
@login_required
def fbs_bulk_confirm_async_api():
    """POST: confirm many CREATED orders in the BACKGROUND.

    The synchronous ``/bulk-confirm`` holds the request open until every
    order is processed (~0.5s/order via the shared 2/s token bucket → ~60s
    for 120 orders), so the seller stares at a spinner. This variant returns
    IMMEDIATELY with a ``job_id`` and runs the confirms on a daemon thread.

    Each confirm writes PACKING to the DB row the instant it lands (see
    ``core.fbs_data.confirm_order``), so the orders flip CREATED→PACKING
    one-by-one. The front-end polls ``/bulk-confirm-status/<job_id>`` for
    progress + per-order failures and refreshes the list to show the live
    transition — no fake "done", only real outcomes.

    Body: ``{"order_ids": [int, ...]}``
    Returns: ``{"job_id", "queued", "skipped": [{order_id, error, status}]}``
    """
    ids, err = _parse_bulk_order_ids()
    if err is not None:
        return err
    rows, err = _resolve_bulk_orders(ids)
    if err is not None:
        return err
    token = request.environ.get("_fbs_bulk_token") or ""

    confirmable, skipped = _partition_confirmable(rows)

    uid = int(current_user.get_id())
    # Capture the seller's language NOW — the daemon thread has no request
    # context, so _session_lang() inside it would always degrade to "uz".
    lang = _session_lang()
    job_id = new_job(uid, total=len(confirmable))

    if confirmable:
        threading.Thread(
            target=_run_bulk_confirm_job,
            args=(job_id, token, confirmable, lang, uid),
            daemon=True,
        ).start()
    else:
        # Nothing confirmable — mark the (empty) job done so the poller stops.
        finish_job(job_id)

    return _json_response({
        "job_id": job_id,
        "queued": len(confirmable),
        "skipped": skipped,
    })


@fbs_bp.get("/fbs/api/orders/bulk-confirm-status/<job_id>")
@login_required
def fbs_bulk_confirm_status_api(job_id):
    """GET: progress of an async bulk-confirm job.

    Returns ``{total, done, ok, failed:[{order_id, error, code}], status,
    finished}``. 404 (``bulk_job_unknown``) if the job never existed, has
    expired, or belongs to another user — we never leak another seller's job.
    """
    job = get_job(job_id)
    if job is None or int(job.get("user_id", -1)) != int(current_user.get_id()):
        return _json_response({"error": _err("bulk_job_unknown")}, 404)
    return _json_response({
        "total": job.get("total", 0),
        "done": job.get("done", 0),
        "ok": job.get("ok", 0),
        "failed": job.get("failed", []),
        "status": job.get("status", "running"),
        "finished": job.get("status") == "done",
    })


# In-memory cache for the DB-aggregated drop-off points list. The
# underlying GROUP BY is fast but called every modal open + tab switch;
# caching for 2 min avoids the repeat SQL and lets the prefetch be
# basically free. Keyed by (uid, source) so multi-tenant + tab.
_dropoff_cache: dict[tuple, tuple[float, dict]] = {}
_dropoff_cache_lock = __import__("threading").Lock()
_DROPOFF_CACHE_TTL = 120  # seconds


def _dropoff_cache_get(uid: int, source: str):
    with _dropoff_cache_lock:
        entry = _dropoff_cache.get((uid, source))
        if entry and (__import__("time").time() - entry[0] < _DROPOFF_CACHE_TTL):
            return entry[1]
    return None


def _dropoff_cache_put(uid: int, source: str, payload: dict):
    with _dropoff_cache_lock:
        _dropoff_cache[(uid, source)] = (__import__("time").time(), payload)
        # Keep the cache bounded.
        if len(_dropoff_cache) > 200:
            oldest = min(_dropoff_cache, key=lambda k: _dropoff_cache[k][0])
            _dropoff_cache.pop(oldest, None)


def _matching_dropoff_uuids(uid: int, user_shops: list, match_ids) -> set[str]:
    """Uzum invoice-points'dan shu order'larga MOS punkt UUID'lari to'plami.

    «Faqat mos keladigan» ON'da db («Mening punktlarim») punktlarini ham shu
    накладна'ga moslash uchun ishlatiladi. Egalik-guard: order_id'lar
    user_shops bilan filtrlanadi. Xato/overdue/token-yo'q bo'lsa — bo'sh
    to'plam (chaqiruvchi bunda hamma punktni ko'rsatadi, bo'shab qolmaydi).
    """
    from models import FbsOrder, User
    try:
        with SessionLocal() as db:
            user = db.get(User, uid)
            token = (user.uzum_openapi_token or "").strip() if user else ""
            want = db.execute(
                select(FbsOrder.order_id)
                .where(FbsOrder.shop_id.in_(user_shops))
                .where(FbsOrder.order_id.in_(list(match_ids)))
            ).scalars().all()
        if not token or not want:
            return set()
        from core.uzum_openapi import fetch_fbs_dropoff_points
        uzum_points, _used = fetch_fbs_dropoff_points(
            token, [int(o) for o in want], fail_fast=True,
        )
        return {
            str(p.get("uuid"))
            for p in (uzum_points or [])
            if isinstance(p, dict) and p.get("uuid")
        }
    except Exception as e:  # noqa: BLE001 — best-effort; bo'sh to'plam fallback
        print(f"[dropoff-points] matching uuids fetch failed: {e!r}")
        return set()


@fbs_bp.get("/fbs/api/dropoff-points")
@login_required
def fbs_dropoff_points_api():
    """GET: Drop-off (qabul) points for the seller.

    Query param ``source``:
      * ``db`` (default) — aggregate from past orders in our DB cache.
        Fast, offline, but only shows points the seller has used before
        (5 unique points for a typical seller). Used for the "where did
        I deliver?" view.
      * ``uzum`` — call ``GET /v1/fbs/invoice/dop/drop-off-points`` to
        get the FULL list of available Uzum points suitable for the
        seller's current PACKING orders (typically 50+ points). Used
        for the "where SHOULD I deliver next?" view — matches what
        Market Plus / Uzum's own UI shows.

    The ``uzum`` source needs at least one PACKING order to call Uzum
    (the endpoint filters by orders' dimensional groups + earliest
    deliver-by date). If the seller has none, we fall back to ``db``.
    On Uzum API failure we also fall back to ``db`` so the seller
    always gets SOMETHING.

    Response::

        {
          "ok": true,
          "source": "uzum" | "db",
          "points": [
            {"uuid": "...", "address": "Tashkent, Chilonzor 5",
             "active": 3, "total": 27, "shops": ["MyShop1", "MyShop2"]},
            ...
          ]
        }
    """
    user_shops = _current_user_shop_uzum_ids()
    if not user_shops:
        return _json_response({"ok": True, "source": "db", "points": []})

    from models import FbsOrder, Shop, User
    # Status buckets that still need physical delivery to the drop-off
    # point. CREATED + PACKING + PENDING_DELIVERY are FBS, DELIVERING is
    # the rare mid-flight bucket — none of YETKAZILDI/YAKUNLANDI/BEKOR
    # are "active" from the seller's logistics perspective.
    #
    # CREATED is included even though Uzum *technically* wants only
    # accepted orders: in practice including the broader pool of order
    # IDs gives Uzum a wider mix of dimensional groups to match against
    # → more drop-off points come back. Sellers complained the previous
    # PACKING-only filter returned just 2 points when their active queue
    # was small.
    _ACTIVE_STATUSES = ("CREATED", "PACKING", "PENDING_DELIVERY", "DELIVERING")
    source = (request.args.get("source") or "db").strip().lower()
    if source not in ("db", "uzum"):
        source = "db"

    # «Faqat mos keladigan» — agar KONKRET накладна order ID'lari berilsa
    # (`order_ids=`), punktlarni aynan shu order'larga moslab Uzum'dan olamiz
    # (Uzum portalining /drop-off/invoice-points?customerOrderIds=… kabi);
    # berilmasa — butun aktiv pool (kengroq, "kelajakda qayerga?" ko'rinishi).
    _match_ids = _parse_order_ids_param(request.args.get("order_ids"))

    # 2-min in-memory cache — modal open + tab switches hit this without
    # re-running the DB GROUP BY or re-calling Uzum. Per-user keyed so
    # multi-tenant isolation is preserved. Mos-rejimda order'lar to'plamini
    # ham kalitga qo'shamiz (har накладна alohida keshlansin).
    uid = int(current_user.get_id())
    _cache_key = source
    if _match_ids and source in ("uzum", "db"):
        _cache_key = f"{source}:m:" + ",".join(sorted(_match_ids))
    cached = _dropoff_cache_get(uid, _cache_key)
    if cached is not None:
        return _json_response(cached)

    # ── Source = uzum: call Uzum's real endpoint ──────────────────────
    if source == "uzum":
        # Grab user's token (same token used for all their shops).
        with SessionLocal() as db:
            user = db.get(User, uid)
            token = (user.uzum_openapi_token or "").strip() if user else ""
            if _match_ids:
                # «Faqat mos keladigan» — aynan shu накладна order'lari bilan
                # so'raymiz → Uzum faqat shu order'larga mos punktlarni qaytaradi.
                # EGALIK GUARD: faqat user o'z do'konlari order'lari (boshqa
                # sellernikini so'rab bo'lmaydi).
                active_orders = db.execute(
                    select(FbsOrder.order_id)
                    .where(FbsOrder.shop_id.in_(user_shops))
                    .where(FbsOrder.order_id.in_(list(_match_ids)))
                ).scalars().all()
            else:
                # Find PACKING-state orders so we can pass their IDs to Uzum.
                # Uzum's dropoff endpoint uses the orders' dimensional groups
                # + earliest deliver-by date to filter suitable points.
                active_orders = db.execute(
                    select(FbsOrder.order_id)
                    .where(FbsOrder.shop_id.in_(user_shops))
                    .where(FbsOrder.status.in_(_ACTIVE_STATUSES))
                    .limit(50)  # don't spam Uzum with hundreds of IDs
                ).scalars().all()
        if not token:
            return _json_response(
                {"error": _err("token_not_set")}, 400
            )
        if not active_orders:
            # No active orders → fall back to DB-aggregated, with a hint.
            print("[dropoff-points] uzum requested but no active orders; falling back to db")
            source = "db"
        else:
            try:
                from core.uzum_openapi import fetch_fbs_dropoff_points
                uzum_points, used_url = fetch_fbs_dropoff_points(
                    token, [int(o) for o in active_orders],
                    fail_fast=True,
                )
                # Project Uzum's dropOffPoints into the dict shape the
                # frontend already renders. Bonus: latitude/longitude
                # come as STRUCTURED fields — no need to regex them out
                # of the address text like we did for DB-sourced points.
                points = []
                for p in (uzum_points or []):
                    if not isinstance(p, dict):
                        continue
                    addr = (p.get("address") or "").strip()
                    if not addr:
                        continue
                    points.append({
                        "uuid": p.get("uuid"),
                        "address": addr,
                        # Uzum's response doesn't carry per-seller order
                        # counts — those are only meaningful in our DB
                        # view. Leave both at 0; UI hides the badges when
                        # both are 0.
                        "active": 0,
                        "total": 0,
                        "shops": [],
                        # Structured coords — feeds the haversine distance
                        # ranking directly, no address-text regex needed.
                        "latitude": p.get("latitude"),
                        "longitude": p.get("longitude"),
                        # workingHours: dict mapping weekday → {start, end}.
                        # Frontend renders the "today's hours" line.
                        "workingHours": p.get("workingHours") or {},
                        # Whether the point accepts large-dimensional
                        # groups (some only handle small parcels).
                        "dimensionalGroupIsLarge": bool(
                            p.get("dimensionalGroupIsLarge")
                        ),
                        "type": p.get("type"),
                    })
                # Upsert every Uzum-returned point into the global
                # catalog so the next caller (or this same seller next
                # session) sees them even when their own active queue
                # doesn't trigger Uzum to return that specific point.
                # The catalog is shared across all platform sellers — PVZ
                # infrastructure is global, not per-tenant.
                from models import FbsDropoffPoint
                from sqlalchemy.dialects.postgresql import insert as pg_insert
                from datetime import datetime as _dt
                now = _dt.utcnow()
                with SessionLocal() as db2:
                    for p in points:
                        if not p.get("uuid"):
                            continue
                        stmt = pg_insert(FbsDropoffPoint).values(
                            uuid=str(p["uuid"]),
                            address=p["address"],
                            latitude=p.get("latitude"),
                            longitude=p.get("longitude"),
                            working_hours_json=p.get("workingHours") or {},
                            dimensional_group_is_large=bool(
                                p.get("dimensionalGroupIsLarge")
                            ),
                            point_type=p.get("type"),
                            first_seen_at=now,
                            last_seen_at=now,
                        ).on_conflict_do_update(
                            index_elements=["uuid"],
                            set_={
                                "address": p["address"],
                                "latitude": p.get("latitude"),
                                "longitude": p.get("longitude"),
                                "working_hours_json": p.get("workingHours") or {},
                                "dimensional_group_is_large": bool(
                                    p.get("dimensionalGroupIsLarge")
                                ),
                                "point_type": p.get("type"),
                                "last_seen_at": now,
                            },
                        )
                        db2.execute(stmt)
                    db2.commit()
                # Mark every Uzum-returned point as currently suitable so
                # the frontend can badge them ("hozir mos") vs catalog-
                # only entries.
                _suitable_uuids: set[str] = {
                    str(p["uuid"]) for p in points if p.get("uuid")
                }
                for p in points:
                    p["suitable"] = bool(
                        p.get("uuid") and str(p["uuid"]) in _suitable_uuids
                    )
                _seen_uuids: set[str] = set(_suitable_uuids)
                _seen_addrs: set[str] = {
                    (p.get("address") or "").strip().lower() for p in points
                }
                # Now overlay every point in the global catalog from the
                # last 30 days — gives the picker a comprehensive view of
                # all known PVZ even when this seller's own active queue
                # is small. 30-day TTL filters out points Uzum has
                # decommissioned.
                from datetime import timedelta as _td
                stale_cutoff = now - _td(days=30)
                with SessionLocal() as db3:
                    catalog_rows = db3.execute(
                        select(FbsDropoffPoint).where(
                            FbsDropoffPoint.last_seen_at >= stale_cutoff
                        )
                    ).scalars().all()
                for cr in catalog_rows:
                    uuid_str = str(cr.uuid)
                    addr_norm = (cr.address or "").strip().lower()
                    if uuid_str in _seen_uuids:
                        continue
                    if addr_norm in _seen_addrs:
                        continue
                    points.append({
                        "uuid": cr.uuid,
                        "address": cr.address,
                        "active": 0,
                        "total": 0,
                        "shops": [],
                        "latitude": cr.latitude,
                        "longitude": cr.longitude,
                        "workingHours": cr.working_hours_json or {},
                        "dimensionalGroupIsLarge": bool(cr.dimensional_group_is_large),
                        "type": cr.point_type,
                        "suitable": False,
                    })
                    _seen_uuids.add(uuid_str)
                    _seen_addrs.add(addr_norm)
                # Also overlay seller-historical points (DB orders) for
                # legacy uuids that the catalog hasn't picked up yet.
                with SessionLocal() as db4:
                    hist_rows = db4.execute(
                        select(
                            FbsOrder.drop_off_point_uuid,
                            FbsOrder.drop_off_point_address,
                            func.count().label("n"),
                        )
                        .where(FbsOrder.shop_id.in_(user_shops))
                        .where(FbsOrder.drop_off_point_address.is_not(None))
                        .group_by(
                            FbsOrder.drop_off_point_uuid,
                            FbsOrder.drop_off_point_address,
                        )
                    ).all()
                for hr in hist_rows:
                    addr = (hr.drop_off_point_address or "").strip()
                    if not addr:
                        continue
                    uuid_str = str(hr.drop_off_point_uuid) if hr.drop_off_point_uuid else ""
                    addr_norm = addr.lower()
                    if uuid_str and uuid_str in _seen_uuids:
                        continue
                    if addr_norm in _seen_addrs:
                        continue
                    points.append({
                        "uuid": hr.drop_off_point_uuid,
                        "address": addr,
                        "active": 0,
                        "total": int(hr.n),
                        "shops": [],
                        "latitude": None,
                        "longitude": None,
                        "workingHours": {},
                        "dimensionalGroupIsLarge": False,
                        "type": None,
                        "suitable": False,
                    })
                    if uuid_str:
                        _seen_uuids.add(uuid_str)
                    _seen_addrs.add(addr_norm)
                # Sort: currently suitable first, then by address.
                points.sort(key=lambda p: (not p.get("suitable"), p["address"]))
                payload = {
                    "ok": True, "source": "uzum",
                    "used_url": used_url,
                    "points": points,
                }
                _dropoff_cache_put(uid, _cache_key, payload)
                return _json_response(payload)
            except UzumAPIError as e:
                print(f"[dropoff-points] Uzum error: {e!r}; falling back to db")
                # fall through to db
            except Exception as e:
                print(f"[dropoff-points] unexpected error: {e!r}; falling back to db")
                # fall through to db

    with SessionLocal() as db:
        rows = db.execute(
            select(
                FbsOrder.drop_off_point_uuid,
                FbsOrder.drop_off_point_address,
                FbsOrder.shop_id,
                FbsOrder.status,
                func.count().label("n"),
            )
            .where(FbsOrder.shop_id.in_(user_shops))
            .where(FbsOrder.drop_off_point_address.is_not(None))
            .group_by(
                FbsOrder.drop_off_point_uuid,
                FbsOrder.drop_off_point_address,
                FbsOrder.shop_id,
                FbsOrder.status,
            )
        ).all()

        # Map shop_uzum_id → human name for the "shops" badge in the UI.
        shop_rows = db.execute(
            select(Shop.uzum_id, Shop.name).where(Shop.uzum_id.in_(user_shops))
        ).all()
        shop_name_by_uid = {r.uzum_id: (r.name or r.uzum_id) for r in shop_rows}

    # Pivot the (uuid, address, shop, status) → count rows into one
    # entry per drop-off point. Address is the primary natural key —
    # the same Uzum point can have null uuid in some legacy rows.
    by_addr: dict[str, dict] = {}
    for r in rows:
        addr = r.drop_off_point_address or ""
        entry = by_addr.setdefault(addr, {
            "uuid": r.drop_off_point_uuid,
            "address": addr,
            "active": 0,
            "total": 0,
            "_shops": set(),
        })
        # Prefer a non-null uuid if any row has one.
        if r.drop_off_point_uuid and not entry["uuid"]:
            entry["uuid"] = r.drop_off_point_uuid
        entry["total"] += int(r.n)
        if r.status in _ACTIVE_STATUSES:
            entry["active"] += int(r.n)
        entry["_shops"].add(shop_name_by_uid.get(r.shop_id, r.shop_id))

    points = []
    for addr, entry in by_addr.items():
        entry["shops"] = sorted(entry.pop("_shops"))
        points.append(entry)
    # Sort: points with active orders first (descending), then by total.
    points.sort(key=lambda p: (-p["active"], -p["total"], p["address"]))

    # «Faqat mos keladigan» — db («Mening punktlarim») uchun ham: bu накладна'ga
    # mos punkt UUID'larini Uzum'dan olib, o'z punktlarimizga `suitable` qo'yamiz
    # → frontend ON'da faqat shu накладна'ga mos o'z punktlarimni ko'rsatadi.
    if _match_ids:
        match_uuids = _matching_dropoff_uuids(uid, user_shops, _match_ids)
        for p in points:
            p["suitable"] = bool(p.get("uuid") and str(p["uuid"]) in match_uuids)

    payload = {"ok": True, "source": "db", "points": points}
    _dropoff_cache_put(uid, _cache_key, payload)
    return _json_response(payload)


@fbs_bp.get("/fbs/api/dropoff-points/<dop_id>/time-slots")
@login_required
def fbs_dropoff_time_slots_api(dop_id: str):
    """GET: Available delivery time-slots for a specific drop-off point.

    Calls Uzum's ``GET /v1/fbs/invoice/dop/time-slot`` with the user's
    currently-active PACKING orders. The slot set returned depends on:

      * the point's remaining capacity vs. order count
      * the earliest deliver-by deadline among the orders

    The frontend uses this to render an expandable "available slots"
    list when the seller clicks a drop-off point in the modal.

    Response::

        {
          "ok": true,
          "slots": [
            {"timeFrom": "2026-05-25T09:00:00Z", "timeTo": "2026-05-25T18:00:00Z"},
            ...
          ]
        }
    """
    # Basic UUID-ish sanity check — Uzum's dopId is a UUID. Reject obvious
    # garbage before we even reach the API to fail fast.
    if not dop_id or len(dop_id) < 8 or len(dop_id) > 64:
        return _json_response({"error": "Invalid dopId"}, 400)

    user_shops = _current_user_shop_uzum_ids()
    if not user_shops:
        return _json_response({"ok": True, "slots": []})

    # Uzum computes the slot window as [now .. earliest deliverUntil] across
    # the orders we pass. If the caller scopes the request to specific orders
    # (the seller's SELECTED PACKING orders — the exact set that will land on
    # the накладная), honour that: bundling unrelated/overdue orders makes the
    # earliest deadline sit in the past and Uzum rejects the whole batch with
    # fbs-19-time-is-up. Without the param we fall back to all active orders
    # (legacy behaviour, kept for backward compatibility).
    requested_ids = _parse_order_ids_param(request.args.get("order_ids"))

    from models import FbsOrder, User
    _ACTIVE_STATUSES = ("PACKING", "PENDING_DELIVERY", "DELIVERING")
    uid = int(current_user.get_id())
    with SessionLocal() as db:
        user = db.get(User, uid)
        token = (user.uzum_openapi_token or "").strip() if user else ""
        if requested_ids:
            # Scope to the requested orders belonging to this user's shops.
            # We accept any ACTIVE status (not just PACKING): the orders-list
            # «Qabul punktlari» modal passes selected PACKING orders (building
            # a NEW накладная), while the накладна «Изменить» modal passes the
            # invoice's already-PENDING_DELIVERY orders (the «Faqat mos
            # keladigan» filter — suitable slots for THIS invoice). Ownership
            # is always re-checked server-side; status is widened to active so
            # both flows get a correct deadline window.
            active_orders = db.execute(
                select(FbsOrder.order_id)
                .where(FbsOrder.shop_id.in_(user_shops))
                .where(FbsOrder.order_id.in_(requested_ids))
                .where(FbsOrder.status.in_(_ACTIVE_STATUSES))
                .limit(50)
            ).scalars().all()
        else:
            # «Faqat mos keladigan» OFF → barcha aktiv buyurtmalar bo'yicha
            # kengroq oyna. LEKIN muddati o'tgan (deliver_until < hozir)
            # buyurtmani tashlaymiz: u hech qanday kelajak slotга sig'maydi va
            # Uzum oynasini o'tmishga tortib, fbs-19 bilan BARCHA slotni
            # yashiradi (foydalanuvchi ko'rgan «Yetkazib berish muddati o'tib
            # ketgan» xato). Uzum portali ham doim faqat shu накладна
            # buyurtmalariga (customerOrderIds) scope qiladi — HAR tahlili
            # tasdiqladi; biz hech bo'lmasa muddati o'tganlarini chiqarib
            # tashlab, kengroq lekin yaroqli oyna beramiz.
            from datetime import datetime as _dt
            _now = _dt.utcnow()
            _rows = db.execute(
                select(FbsOrder.order_id, FbsOrder.deliver_until)
                .where(FbsOrder.shop_id.in_(user_shops))
                .where(FbsOrder.status.in_(_ACTIVE_STATUSES))
                .limit(80)
            ).all()
            active_orders = [
                oid for (oid, du) in _rows if du is None or du > _now
            ][:50]
    if not token:
        return _json_response({"error": _err("token_not_set")}, 400)
    if not active_orders:
        return _json_response({"ok": True, "slots": [], "reason": "no_active_orders"})

    try:
        from core.uzum_openapi import fetch_fbs_time_slots
        slots, used_url = fetch_fbs_time_slots(
            token, dop_id, [int(o) for o in active_orders],
            fail_fast=True,  # interactive slot fetch — fail fast on a burst-429
        )
        return _json_response({
            "ok": True,
            "used_url": used_url,
            "slots": slots,
        })
    except UzumAPIError as e:
        return _uzum_error_response(e)
    except Exception as e:
        print(f"[time-slots] unexpected error: {e!r}")
        return _json_response({"error": str(e)[:200]}, 502)


# ── sellerId auto-detection ────────────────────────────────────────────
# POST /v1/fbs/invoice needs Uzum's ``sellerId`` (the ``?sId=N`` from the
# cabinet URL) — a SELLER-account number, distinct from shopId. No seller/
# profile OpenAPI endpoint exposes it (35-endpoint probe → 403) and the
# token is an opaque 44-char string (not a JWT). BUT every
# ``GET /v1/finance/expenses`` row carries it (verified live 2026-06-02:
# sellerId=95673). So instead of making the seller paste it, we read it
# from their OWN finance data the first time an invoice op needs it and
# persist it on the User — later calls are free. Manual entry on «Mening
# do'konlarim» stays only as a fallback for a brand-new shop with no
# finance history yet.
def _detect_seller_id_from_finance(token, shop_uzum_ids):
    """Read ``sellerId`` from one of the user's shops' finance-expenses
    rows. Read-only (GET); tries each shop until one yields a row. Returns
    the int sellerId, or None when nothing could be read."""
    import time
    from core.uzum_openapi import fetch_finance_expenses_page

    to_sec = int(time.time()) + 86_400          # +1d guards any TZ skew
    from_sec = to_sec - 760 * 86_400            # ~2y back — any active shop has rows
    for shop_uzum in (shop_uzum_ids or []):
        try:
            parsed = fetch_finance_expenses_page(
                token, shop_uzum,
                date_from_sec=from_sec, date_to_sec=to_sec,
                page=0, size=1,
            )
        except Exception as e:
            print(f"[seller-id] finance probe failed shop={shop_uzum}: {e!r}")
            continue
        payments = ((parsed or {}).get("payload") or {}).get("payments") or []
        for p in payments:
            if not isinstance(p, dict):
                continue
            sid = p.get("sellerId")
            if sid is not None:
                try:
                    return int(sid)
                except (TypeError, ValueError):
                    continue
    return None


def _ensure_seller_id(db, user, token, shop_uzum_ids):
    """Return the user's Uzum sellerId, auto-detecting + persisting it from
    the finance API when it isn't stored yet. Returns None only when it
    can't be resolved (no user/token/shops, or no finance row yet) — the
    caller then shows the manual «Mening do'konlarim» fallback prompt."""
    if user is None:
        return None
    if user.uzum_seller_id is not None:
        return user.uzum_seller_id
    if not token or not shop_uzum_ids:
        return None
    sid = _detect_seller_id_from_finance(token, shop_uzum_ids)
    if sid is None:
        return None
    user.uzum_seller_id = sid
    db.commit()
    print(f"[seller-id] auto-detected sellerId={sid} for user_id={user.id} (saved)", flush=True)
    return sid


@fbs_bp.post("/fbs/api/invoices")
@login_required
def fbs_invoice_create_api():
    """POST: Create a new FBS invoice (накладная) for the given orders.

    Body JSON::

        {
          "order_ids": [12345, 67890],     # PACKING orders to put in invoice
          "drop_off_point_uuid": "<uuid>", # from /fbs/api/dropoff-points
          "time_slot_uuid": "<uuid>",      # from /fbs/api/dropoff-points/<id>/time-slots
          "shop_uzum_id": "<id>"           # informational only — sellerId
                                           # is read from User.uzum_seller_id
        }

    Calls ``POST /v1/fbs/invoice`` on Uzum. On 400 with code
    ``seller-order-19`` (invoice already exists for these orders) we
    automatically retry the .../dop/time-slot variant — same body shape,
    semantic is "update the point/slot of the existing invoice".

    Returns the Uzum invoice payload verbatim plus a ``mode`` field so
    the UI knows whether we created or updated.
    """
    payload = request.get_json(silent=True) or {}
    order_ids = payload.get("order_ids") or []
    dop_uuid = (payload.get("drop_off_point_uuid") or "").strip()
    slot_uuid = (payload.get("time_slot_uuid") or "").strip() or None
    shop_uzum_id = (payload.get("shop_uzum_id") or "").strip()

    if not isinstance(order_ids, list) or not order_ids:
        return _json_response({"error": _err("field_required", field="order_ids")}, 400)
    if not dop_uuid:
        return _json_response({"error": _err("field_required", field="drop_off_point_uuid")}, 400)
    if not slot_uuid:
        return _json_response({"error": _err("field_required", field="time_slot_uuid")}, 400)

    # Scope check: all orders must belong to the user's shops, and they
    # must all share the SAME shop (Uzum invoice is per-shop — sellerId
    # is a single integer).
    user_shops = _current_user_shop_uzum_ids()
    if not user_shops:
        return _json_response({"error": _err("no_shop")}, 403)

    from models import FbsOrder, User
    str_ids = [str(i) for i in order_ids]
    with SessionLocal() as db:
        rows = db.execute(
            select(FbsOrder.order_id, FbsOrder.shop_id, FbsOrder.status,
                   FbsOrder.items_json)
            .where(FbsOrder.order_id.in_(str_ids))
            .where(FbsOrder.shop_id.in_(user_shops))
        ).all()
    if len(rows) != len(set(str_ids)):
        return _json_response({"error": _err("some_orders_not_yours")}, 403)

    shop_ids = {r.shop_id for r in rows}
    if len(shop_ids) > 1:
        return _json_response({
            "error": _err("mixed_shops_invoice")
        }, 400)
    # Verify orders are in a state that can be put on an invoice (PACKING).
    bad = [r.order_id for r in rows if r.status != "PACKING"]
    if bad:
        return _json_response({
            "error": _err("only_packing_invoice", n=len(bad))
        }, 400)

    # IDENTIFIER GUARD (Abdulaziz 2026-07-14). Uzum refuses the накладная with
    # `seller-order-15 "identifiers are missing"` when an order carries goods
    # that need a per-unit code (IMEI or ASL_BELGISI — O'zbekiston markirovka
    # kodi) and the codes were never attached. Before this guard the seller only
    # found out from Uzum's raw Russian 400, AFTER we had already spent the
    # create call. We hold the same evidence locally: fbs_orders.items_json keeps
    # each item's `identifierInfo` block.
    #
    # The check keys off the PRESENCE of identifierInfo, not its `required` flag
    # — Uzum sent required=false for the very order it then rejected (verified
    # live on order 116914839). See core.fbs_sync.identifier_need.
    #
    # KILL-SWITCH `FBS_IDENTIFIER_GUARD=0` (Abdulaziz 2026-07-14): Uzum's own
    # PORTAL created a накладная for an order with the SAME shape (ASL_BELGISI
    # item, values=[], required=false) — HAR t4, order 116914758 → HTTP 200.
    # So "identifierInfo present" may NOT mean "codes mandatory", and this guard
    # could be a false positive. Turn it off to let the request reach Uzum and
    # see what the OPENAPI actually answers.
    from core.fbs_sync import identifier_need
    _guard_on = os.getenv("FBS_IDENTIFIER_GUARD", "1").strip().lower() not in (
        "0", "false", "no", "off", "")
    _missing_orders: list[str] = []
    _missing_types: set[str] = set()
    for r in rows:
        need = identifier_need({"orderItems": r.items_json or []})
        if need["required"]:
            _missing_orders.append(str(r.order_id))
            _missing_types.update(need["types"])
    if _missing_orders and not _guard_on:
        print(f"[invoice/create] IDENTIFIER GUARD OFF — letting Uzum decide "
              f"(orders={_missing_orders} types={sorted(_missing_types)})")
        _missing_orders = []
    if _missing_orders:
        _type_txt = " + ".join(
            _err(f"identifier_type_{t}") for t in sorted(_missing_types)
        )
        print(f"[invoice/create] blocked: identifiers missing for "
              f"orders={_missing_orders} types={sorted(_missing_types)}")
        return _json_response({
            "error": _err("identifiers_missing_invoice",
                          orders=", ".join(_missing_orders), type=_type_txt),
            "uzum_code": "seller-order-15",
            "identifiers_missing": _missing_orders,
        }, 400)

    # Uzum's sellerId is the SELLER account (e.g. 95673 in the cabinet
    # URL — ?sId=<N>), NOT the shopId. The two are different namespaces:
    # one user owns many shops, all sharing the same sellerId. Uzum
    # doesn't expose this through any OpenAPI endpoint, so the user
    # pastes it manually on the My Shops page (User.uzum_seller_id).
    # Passing the shopId instead → fbs-24-invoice-wrong-order-status;
    # passing None → validation-failed-001. Account-wide value, used
    # for every invoice regardless of which shop the orders belong to.
    uid = int(current_user.get_id())
    with SessionLocal() as db:
        user = db.get(User, uid)
        token = (user.uzum_openapi_token or "").strip() if user else ""
        # sellerId is auto-detected from the seller's own finance data and
        # persisted — they never paste it (see _ensure_seller_id). Probe the
        # ORDER's OWN shop FIRST (that's where the activity is, so it's the
        # one most likely to carry an expense row), and fall back to the
        # other shops only if it comes up empty. This keeps the common case
        # to a SINGLE finance call on the time-sensitive invoice-create path
        # instead of walking all of the seller's shops.
        order_shop = next(iter(shop_ids))
        probe_shops = [order_shop] + [s for s in user_shops if s != order_shop]
        seller_id = _ensure_seller_id(db, user, token, probe_shops)
    if not token:
        return _json_response({"error": _err("token_not_set")}, 400)
    if seller_id is None:
        return _json_response({
            "error": _err("seller_id_undetected")
        }, 400)

    from core.uzum_openapi import create_fbs_invoice

    def _do_call(update_only: bool):
        return create_fbs_invoice(
            token,
            order_ids=order_ids,
            drop_off_point_uuid=dop_uuid,
            time_slot_uuid=slot_uuid,
            seller_id=seller_id,
            update_only=update_only,
            fail_fast=True,  # interactive: a burst-429 must fail fast, not stall the page
        )

    def _reflect_pending_delivery(invoice_number=None):
        # The orders just moved PACKING → PENDING_DELIVERY at Uzum. Reflect
        # it in our local cache NOW so the «К отправке» list shows them on
        # the next read — not only after a manual «Обновить». The count
        # endpoint reports live numbers, which is exactly why the chip
        # showed «1» while the DB-backed list was still empty. Mirrors the
        # cancel_order fast-path (optimistic UPDATE + cache invalidation);
        # the periodic sync reconciles later if Uzum's status differs.
        #
        # Also stamp ``invoice_number`` from the just-created invoice: the
        # накладная list is scope-filtered by the set of local
        # ``fbs_orders.invoice_number`` (see _filter_invoices_to_owned), so
        # without this the brand-new invoice would be hidden until the next
        # worker tick (~10 min). Stamping it makes the new накладная appear
        # on the very next list reload.
        #
        # NOT gated on ``status == 'PACKING'``: creating the invoice takes a few
        # seconds of paced Uzum I/O, and the active chips are now live-drained on
        # every press, so a refresh landing in that window already re-homed these
        # rows to PENDING_DELIVERY. The old status gate then matched ZERO rows and
        # the number was never stamped — leaving the seller's own brand-new
        # накладная without its ownership proof. The order-id + shop scope is the
        # real guard; the status is just what we're setting.
        try:
            from sqlalchemy import update as _sql_update
            values = {"status": "PENDING_DELIVERY"}
            if invoice_number:
                values["invoice_number"] = str(invoice_number)
            with SessionLocal() as _db:
                res = _db.execute(
                    _sql_update(FbsOrder)
                    .where(FbsOrder.order_id.in_(str_ids))
                    .where(FbsOrder.shop_id.in_(user_shops))
                    .values(**values)
                )
                _db.commit()
            stamped = res.rowcount or 0
            if stamped != len(str_ids):
                print(f"[invoice/create] reflect stamped {stamped}/{len(str_ids)} "
                      f"row(s) — invoice={invoice_number}")
            for _sid in shop_ids:
                invalidate_fbs_cache(_sid)
        except Exception as _e:
            print(f"[invoice/create] local PENDING_DELIVERY reflect failed: {_e!r}")

    try:
        invoice, used_url = _do_call(update_only=False)
        _reflect_pending_delivery(invoice.get("number") if isinstance(invoice, dict) else None)
        return _json_response({
            "ok": True,
            "mode": "created",
            "used_url": used_url,
            "invoice": invoice,
        })
    except UzumAPIError as e:
        code = (e.code or "").lower()
        # seller-order-19 = invoice already exists for these orders.
        # Retry with the update endpoint to change its point/slot instead
        # of failing the seller's click.
        if "seller-order-19" in code or "already" in (e.message or "").lower():
            try:
                invoice, used_url = _do_call(update_only=True)
                _reflect_pending_delivery(invoice.get("number") if isinstance(invoice, dict) else None)
                return _json_response({
                    "ok": True,
                    "mode": "updated",
                    "used_url": used_url,
                    "invoice": invoice,
                })
            except UzumAPIError as e2:
                return _uzum_error_response(e2)
            except Exception as e2:
                return _json_response({"error": str(e2)[:200]}, 502)
        return _uzum_error_response(e)
    except Exception as e:
        print(f"[invoice/create] unexpected error: {e!r}")
        return _json_response({"error": str(e)[:200]}, 502)


def _filter_invoices_to_owned(invoices: list[dict], owned_numbers) -> list[dict]:
    """Keep only invoices whose ``number`` belongs to a registered shop.

    ``GET /v1/fbs/invoice`` is TOKEN-scoped: one seller account owns many
    shops and the token returns invoices for ALL of them — including shops the
    user never added to SellerHub (e.g. a 5th shop on a "Do'konlar: 4/5"
    account). Uzum's invoice payload carries NO ``shopId`` (its ``stock``
    warehouse is shared across shops, so it can't discriminate either).

    The reliable signal: every order carries ``invoiceNumber`` (== the
    invoice's ``number``), and the order-sync writes ONLY the user's
    registered shops into ``fbs_orders``. So ``owned_numbers`` — the set of
    ``fbs_orders.invoice_number`` for those shops — is exactly the invoices
    the user may see. A number absent from it is a foreign shop's invoice and
    is dropped.

    Pure (no DB/IO) so the filter is unit-testable in isolation.
    """
    owned = {str(n) for n in (owned_numbers or set()) if n is not None and str(n).strip()}
    return [iv for iv in invoices if str(iv.get("number")) in owned]


def _owned_invoice_numbers(user_shops: list[str]) -> set[str]:
    """Distinct ``fbs_orders.invoice_number`` for the user's registered shops.

    Empty set when the user has no shops. Terminal orders are retained 365
    days (see app.py ``_FBS_TERMINAL_RETENTION_DAYS``) and the worker syncs
    every status, so this covers historical ACCEPTED/CANCELLED invoices too —
    not just the active queue.
    """
    from core.fbs_data import get_owned_invoice_numbers
    return get_owned_invoice_numbers(user_shops)


def _invoice_numbers_from_orders(orders, shop_uzum_ids) -> set[str]:
    """Pure: owned ``invoiceNumber`` set from a list of Uzum order dicts,
    scoped to ``shop_uzum_ids``.

    Each ``/v2/fbs/orders`` order carries its own ``shopId`` + ``invoiceNumber``,
    so this yields exactly the invoices those shops own — WITHOUT touching the
    DB. An order counts only when its ``shopId`` is one of the user's shops:
    the batched fetch only ever *requests* the user's shopIds, but this guard
    is fail-safe against Uzum ever echoing an unexpected shop.

    Unit-tested in isolation (no Uzum, no Postgres).
    """
    id_set = {str(s).strip() for s in (shop_uzum_ids or []) if s is not None and str(s).strip()}
    if not id_set:
        return set()
    out: set[str] = set()
    for o in (orders or []):
        num = o.get("invoiceNumber")
        if not num:
            continue
        sid = o.get("shopId")
        if sid is not None and str(sid) in id_set:
            out.add(str(num))
    return out


def _live_owned_invoice_numbers(token: str, shop_uzum_ids: list[str]) -> set[str]:
    """LIVE owned-invoice set: batched ``/v2/fbs/orders?status=PENDING_DELIVERY``
    for the user's shops, projected to ``invoiceNumber`` via
    :func:`_invoice_numbers_from_orders`.

    WHY (verified live 2026-07-12): the DB-backed :func:`_owned_invoice_numbers`
    MISSES a postavka the seller created directly on Uzum — PENDING_DELIVERY is
    never background-synced, so that order's ``invoice_number`` never lands in
    ``fbs_orders`` and the накладная list filter wrongly drops the seller's OWN
    invoice as "foreign". One batched call (~420ms for 4 shops, vs ~3.6s
    per-shop) rebuilds the *fresh* owned set straight from Uzum, so a
    just-created postavka shows up immediately. Foreign shops can't leak: we
    only request the user's shopIds, and Uzum 403s a shopId outside the token's
    account anyway.

    Best-effort: any Uzum hiccup returns an empty set so the caller falls back
    to the DB-only owned set (no worse than before this helper existed).
    """
    ids = [str(s).strip() for s in (shop_uzum_ids or []) if s is not None and str(s).strip()]
    if not ids or not token:
        return set()
    try:
        from core.fbs_sync import fetch_all_pages
        orders = fetch_all_pages(token, ids, status="PENDING_DELIVERY", fail_fast=True)
    except Exception as e:
        print(f"[invoices/list] live owned-invoice fetch failed shops={ids}: {e!r}")
        return set()
    return _invoice_numbers_from_orders(orders, ids)


def _live_owned_invoice_evidence(
    token: str, shop_uzum_ids: list[str],
) -> tuple[set[str], set[str]]:
    """LIVE ownership evidence straight from Uzum: ``(invoice_numbers, order_ids)``
    for every PENDING_DELIVERY order belonging to the user's shops.

    :func:`_live_owned_invoice_numbers` gives only the numbers, which is enough
    for the LIST filter. The by-id guard needs the order ids too, because Uzum's
    invoice payload does not always carry ``number`` (prod logs 2026-07-13 show
    ``number=None`` on the detail call) — without an id-based signal such an
    invoice can never be proven ours and 403s even though it IS ours.

    Both sets are scoped by per-order ``shopId``, so a foreign shop on the same
    Uzum account cannot leak in.

    Best-effort: any Uzum hiccup returns empty sets → the caller falls back to
    the DB-only decision, i.e. no worse than before this helper existed.
    """
    ids = [str(s).strip() for s in (shop_uzum_ids or []) if s is not None and str(s).strip()]
    if not ids or not token:
        return (set(), set())
    try:
        from core.fbs_sync import fetch_all_pages
        orders = fetch_all_pages(token, ids, status="PENDING_DELIVERY", fail_fast=True)
    except Exception as e:
        print(f"[invoice-guard] live ownership fetch failed shops={ids}: {e!r}")
        return (set(), set())
    allowed = set(ids)
    order_ids = {
        str(o.get("id")) for o in orders
        if o.get("id") is not None and str(o.get("shopId") or "") in allowed
    }
    return (_invoice_numbers_from_orders(orders, ids), order_ids)


def _decide_invoice_ownership(
    invoice_number,
    inv_order_ids,
    owned_numbers,
    owned_order_ids,
) -> bool:
    """Decide whether an invoice belongs to the current user. Pure — unit-tested.

    The by-id invoice endpoints (detail / akt / change-pickup) are TOKEN-scoped
    at Uzum: the token covers EVERY shop on the seller account, so Uzum happily
    serves (and MUTATES) invoices of shops the user never registered here. The
    local ownership signal is two-fold, either one suffices:

      1. ``invoice_number`` ∈ ``owned_numbers`` (the registered shops'
         ``fbs_orders.invoice_number`` set) — the fast path for synced data.
      2. Any of the invoice's order ids (from Uzum's authoritative
         ``/invoice/{id}/orders``) appears in ``owned_order_ids`` (the subset
         of those ids found locally under the user's shops) — covers a
         freshly-created invoice whose number hasn't synced yet.

    Both signals empty/miss → foreign invoice → deny. Blank/None values are
    never treated as wildcards.
    """
    if invoice_number is not None:
        num = str(invoice_number).strip()
        owned = {str(n) for n in (owned_numbers or set()) if n is not None and str(n).strip()}
        if num and num in owned:
            return True
    inv_ids = {str(i) for i in (inv_order_ids or []) if i is not None}
    owned_ids = {str(i) for i in (owned_order_ids or []) if i is not None}
    return bool(inv_ids & owned_ids)


def _user_owns_invoice(
    token: str,
    invoice_id: int,
    user_shops: list[str],
    *,
    invoice_number=None,
    inv_orders: list[dict] | None = None,
) -> tuple[bool, list[dict] | None]:
    """Scope guard for the by-id invoice endpoints (detail / akt / pickup).

    Returns ``(owns, inv_orders)`` — ``inv_orders`` is Uzum's authoritative
    order list for the invoice, passed through so callers that need it anyway
    (detail render, change-pickup body) never fetch it twice. When the caller
    already has it, pass it in and NO extra Uzum call is made; otherwise ONE
    paced ``/invoice/{id}/orders`` call happens only on the owned-numbers
    cache miss (rare: invoice created in the last ~10 min, or foreign).

    Fail-closed: if the Uzum orders fetch errors out, ``owns`` is False —
    a transient error must never open a foreign invoice.
    """
    if not user_shops:
        return (False, inv_orders)
    owned_numbers = _owned_invoice_numbers(user_shops)
    # Fast local path — number already synced into fbs_orders.
    if _decide_invoice_ownership(invoice_number, [], owned_numbers, []):
        return (True, inv_orders)

    if inv_orders is None:
        try:
            from core.uzum_openapi import fetch_fbs_invoice_orders
            inv_orders, _ = fetch_fbs_invoice_orders(token, invoice_id, fail_fast=True)
        except Exception as e:
            print(f"[invoice-guard] orders fetch failed for invoice {invoice_id}: {e!r}")
            return (False, None)

    inv_order_ids = [str(o.get("orderId")) for o in inv_orders if o.get("orderId") is not None]
    owned_order_ids: set[str] = set()
    if inv_order_ids:
        from models import FbsOrder
        with SessionLocal() as db:
            rows = db.execute(
                select(FbsOrder.order_id)
                .where(FbsOrder.shop_id.in_(user_shops))
                .where(FbsOrder.order_id.in_(inv_order_ids))
            ).all()
        owned_order_ids = {str(r[0]) for r in rows}

    owns = _decide_invoice_ownership(invoice_number, inv_order_ids, owned_numbers, owned_order_ids)

    # LIVE fallback before denying. The DB signals only know about orders WE
    # synced: a накладная the seller built in the Uzum app (or one whose local
    # rows were lost) has no local trace at all, so both signals miss and the
    # seller's OWN invoice 403s — it shows in the list (which already consults
    # Uzum) but refuses to open. Ask Uzum who owns it: one batched
    # PENDING_DELIVERY call (~400ms), only on the DB miss, and only for the
    # user's own shopIds. Foreign invoices still get denied — they are absent
    # from this evidence exactly because they belong to another shop.
    if not owns:
        live_numbers, live_order_ids = _live_owned_invoice_evidence(token, user_shops)
        owns = _decide_invoice_ownership(
            invoice_number, inv_order_ids,
            owned_numbers | live_numbers,
            owned_order_ids | live_order_ids,
        )
        if owns:
            print(f"[invoice-guard] ALLOW invoice={invoice_id} via LIVE evidence "
                  f"(number={invoice_number!r}) — DB had no local trace")

    if not owns:
        print(f"[invoice-guard] DENY invoice={invoice_id} "
              f"(number={invoice_number!r}, {len(inv_order_ids)} order(s), "
              f"{len(owned_order_ids)} owned) — foreign shop")
    return (owns, inv_orders)


def _foreign_sku_ids(items: list[dict], shop_by_sku: dict, allowed_shop_ids) -> list[str]:
    """SkuIds in a stock MUTATION that do NOT belong to the user's shops.

    Pure — unit-tested. Same ownership semantics as the read-side
    ``_filter_stock_skus_to_shops`` (skuId → local Variant → shop), but for
    writes we REJECT the whole request instead of silently dropping rows: a
    partial write would leave the seller believing every row saved.

    Returns the offending skuIds (empty list = all owned). A missing/unmapped
    skuId counts as foreign — fail-closed, same as the read side.
    """
    allowed = {int(s) for s in (allowed_shop_ids or [])}
    bad: list[str] = []
    for it in items or []:
        sid = it.get("skuId") if isinstance(it, dict) else None
        shop_id = shop_by_sku.get(str(sid)) if sid is not None else None
        if shop_id is None or int(shop_id) not in allowed:
            bad.append(str(sid))
    return bad


@fbs_bp.get("/fbs/api/invoices")
@login_required
def fbs_invoices_list_api():
    """GET: List the seller's invoices.

    Query params:
      statuses (repeated, optional) — one or more of FBS_INVOICE_STATUSES.
        Defaults to all four if omitted.
      page (int, default 0) — 0-based page index.
      size (int, default 20, max 50) — page size.

    Returns ``{"ok": True, "invoices": [...], "page": N, "size": N}``.
    """
    from core.uzum_openapi import (
        fetch_fbs_invoices_list,
        FBS_INVOICE_STATUSES,
    )

    statuses_raw = request.args.getlist("statuses")
    statuses = [s for s in statuses_raw if s in FBS_INVOICE_STATUSES] or list(FBS_INVOICE_STATUSES)
    try:
        page = max(0, int(request.args.get("page") or 0))
    except (TypeError, ValueError):
        page = 0
    try:
        size = int(request.args.get("size") or 20)
    except (TypeError, ValueError):
        size = 20
    size = max(1, min(50, size))

    uid = int(current_user.get_id())
    with SessionLocal() as db:
        user = db.get(User, uid)
        token = (user.uzum_openapi_token or "").strip() if user else ""
    if not token:
        return _json_response({"error": _err("token_not_set")}, 400)

    try:
        invoices, used_url = fetch_fbs_invoices_list(
            token, statuses=statuses, page=page, size=size,
            fail_fast=True,  # interactive list reload — fail fast on a burst-429
        )
    except UzumAPIError as e:
        return _uzum_error_response(e)
    except Exception as e:
        print(f"[invoices/list] unexpected error: {e!r}")
        return _json_response({"error": str(e)[:200]}, 502)

    # SCOPE GUARD: the token returns invoices for EVERY shop on the seller
    # account; restrict to invoices belonging to the user's registered shops.
    # Without this, a 5th (un-added) shop's накладные leak into the list.
    user_shops = _current_user_shop_uzum_ids()
    # Owned-invoice set = DB (synced history) ∪ LIVE (fresh PENDING_DELIVERY).
    # The live union is what makes a postavka the seller just created on Uzum
    # appear immediately: its invoice_number isn't in the DB yet (PENDING_DELIVERY
    # is never bg-synced), so without this the filter would drop the seller's OWN
    # накладная as "foreign" (verified live 2026-07-12). Foreign shops still can't
    # leak — the live fetch only requests the user's shopIds.
    owned_numbers = _owned_invoice_numbers(user_shops)
    owned_numbers |= _live_owned_invoice_numbers(token, user_shops)
    before = len(invoices)
    invoices = _filter_invoices_to_owned(invoices, owned_numbers)
    dropped = before - len(invoices)
    if dropped:
        print(f"[invoices/list] scope-filtered {dropped} foreign-shop "
              f"invoice(s) of {before} (page={page}, shops={len(user_shops)})")

    return _json_response({
        "ok": True,
        "invoices": invoices,
        "page": page,
        "size": size,
        # Pagination signal MUST use the PRE-filter count. The scope-filter can
        # drop same-account-but-foreign-shop invoices out of a full Uzum page,
        # so the post-filter ``len(invoices) < size`` does NOT mean "last page"
        # — that false signal disabled the "Keyingi" button and stranded the
        # user on page 0 (Abdulaziz 2026-06-13 bug). Uzum returning a full page
        # (``before >= size``) means another page may exist.
        "has_more": before >= size,
        "raw_count": before,
        "statuses": statuses,
        "used_url": used_url,
    })


@fbs_bp.get("/fbs/api/invoices/<int:invoice_id>")
@login_required
def fbs_invoice_detail_api(invoice_id: int):
    """GET: Single invoice detail by ID.

    The response includes the full Uzum payload (stock, dropOffPoint,
    timeSlot, ettn). Orders inside the invoice are joined from our
    local ``fbs_orders`` table via the ``raw_json->>'invoiceNumber'``
    match — Uzum doesn't echo orderIds in the invoice payload.
    """
    from core.uzum_openapi import fetch_fbs_invoice_detail

    uid = int(current_user.get_id())
    with SessionLocal() as db:
        user = db.get(User, uid)
        token = (user.uzum_openapi_token or "").strip() if user else ""
    if not token:
        return _json_response({"error": _err("token_not_set")}, 400)

    # ``orders_only=1`` (the inline «К отправке» accordion): the caller
    # already has the invoice header from the list endpoint and only needs
    # the orders inside. Skipping the full-detail call drops one PACED Uzum
    # round-trip (~1s on the per-token gate), so a card expands in ~1 call
    # instead of 2 — the difference the seller felt as a slow "Yuklanmoqda…".
    orders_only = bool(request.args.get("orders_only"))
    invoice = None
    used_url = None
    if not orders_only:
        try:
            invoice, used_url = fetch_fbs_invoice_detail(token, invoice_id, fail_fast=True)
        except UzumAPIError as e:
            return _uzum_error_response(e)
        except Exception as e:
            print(f"[invoices/detail] unexpected error: {e!r}")
            return _json_response({"error": str(e)[:200]}, 502)

    # Orders inside the invoice — authoritative membership straight from
    # Uzum (GET /v1/fbs/invoice/{id}/orders). The old
    # raw_json->>'invoiceNumber' DB-match fired before the sync worker had
    # populated invoiceNumber, so a freshly-created invoice looked empty.
    # Each Uzum order is then enriched with local fields (customer, status
    # chip, created date) keyed by order_id, which is reliable unlike the
    # invoiceNumber match. (The detail call already paced one Uzum request;
    # this second one is serialized behind it by the per-token gate, so it
    # won't 429 within a single view.)
    user_shops = _current_user_shop_uzum_ids()
    orders_in_invoice: list[dict] = []
    inv_orders: list[dict] = []
    inv_orders_failed = False
    try:
        from core.uzum_openapi import fetch_fbs_invoice_orders
        inv_orders, _ = fetch_fbs_invoice_orders(token, invoice_id, fail_fast=True)
    except Exception as e:
        inv_orders_failed = True
        print(f"[invoices/detail] authoritative orders join failed: {e!r}")

    # SCOPE GUARD: the token serves any invoice on the seller account — also
    # ones from shops the user never registered here. The list endpoint is
    # already filtered; enforce the same boundary for direct by-id access.
    # inv_orders is passed in, so the guard adds NO extra Uzum call.
    owns, _ = _user_owns_invoice(
        token, invoice_id, user_shops,
        invoice_number=(invoice or {}).get("number"),
        inv_orders=inv_orders,
    )
    if not owns:
        if inv_orders_failed:
            # Ownership UNDETERMINED (transient Uzum error, no local number
            # match) — surface a retryable error, not a misleading 403.
            return _json_response({
                "error": _err("invoice_fetch_error")
            }, 502)
        return _json_response({
            "error": _err("invoice_not_yours")
        }, 403)

    db_by_id: dict = {}
    # order_id is a VARCHAR column — coerce to str so the IN clause matches
    # (Uzum returns orderId as an int; comparing int vs varchar 500s in PG).
    order_ids = [str(o.get("orderId")) for o in inv_orders if o.get("orderId") is not None]
    if order_ids and user_shops:
        from models import FbsOrder
        with SessionLocal() as db:
            rows = db.execute(
                select(
                    FbsOrder.order_id, FbsOrder.shop_id, FbsOrder.status,
                    FbsOrder.customer_fullname, FbsOrder.price,
                    FbsOrder.date_created,
                )
                .where(FbsOrder.shop_id.in_(user_shops))
                .where(FbsOrder.order_id.in_(order_ids))
            ).all()
        db_by_id = {str(r.order_id): r for r in rows}

    # Localize товар nomi to the seller's language from our own catalog,
    # keyed by barcode (the invoice payload's title is single-language).
    lang = session.get("lang", "uz")
    allowed_shop_db_ids = _user_shop_ids(uid)
    all_bcs = [
        it.get("barcode")
        for o in inv_orders
        for it in (o.get("items") or [])
        if isinstance(it, dict) and it.get("barcode")
    ]
    title_by_bc: dict = {}
    if lang == "ru" and all_bcs and allowed_shop_db_ids:
        with SessionLocal() as db:
            title_by_bc, _ = _variant_titles(
                db, allowed_shop_db_ids, barcodes=all_bcs, lang=lang
            )

    for o in inv_orders:
        oid = o.get("orderId")
        db_row = db_by_id.get(str(oid)) if oid is not None else None
        # Compact the items for the product card — image, title, sku,
        # price, qty, barcode. The invoice-orders items carry the same
        # photo.photo size map as the list endpoint.
        items_compact: list[dict] = []
        item_status = None
        for it in (o.get("items") or []):
            if not isinstance(it, dict):
                continue
            if item_status is None and it.get("status"):
                item_status = it.get("status")
            photo_set = ((it.get("photo") or {}).get("photo") or {})
            img = ""
            for size in ("240", "480", "540", "120", "720"):
                bag = photo_set.get(size)
                if isinstance(bag, dict):
                    img = bag.get("high") or bag.get("low") or ""
                    if img:
                        break
            _tup = title_by_bc.get(str(it.get("barcode") or ""))
            _rb, _rc = _tup if _tup else (None, "")
            items_compact.append({
                "title": _localize_product_title(
                    it.get("title") or it.get("productTitle") or "", _rb, _rc, lang),
                "sku": it.get("skuTitle") or "",
                "barcode": it.get("barcode") or "",
                "price": it.get("price") or 0,
                "amount": it.get("amount") or 1,
                "image": img,
            })
        orders_in_invoice.append({
            "order_id": oid,
            "shop_id": db_row.shop_id if db_row else None,
            # Prefer our cached chip status; fall back to the per-item
            # status the invoice-orders payload carries.
            "status": (db_row.status if db_row else None) or item_status,
            "customer_name": db_row.customer_fullname if db_row else None,
            "price": (db_row.price if db_row else None) or o.get("fullPrice") or 0,
            "date_created": db_row.date_created.isoformat() if (db_row and db_row.date_created) else None,
            "items": items_compact,
        })

    return _json_response({
        "ok": True,
        "invoice": invoice,
        "orders": orders_in_invoice,
        "used_url": used_url,
    })


def _pickup_update_values(invoice_payload: dict | None) -> dict:
    """``fbs_orders`` column values implied by a successful change-pickup.

    Reads the MUTATION RESPONSE (the authority for what the invoice now
    points at) — Uzum's order payloads may keep echoing the original
    drop-off point, and the sync worker's ``stop_on_known`` short-circuit
    never re-reads a row whose status didn't change, so this response is
    the only reliable moment to learn the new point.

    Only fields actually present in the payload are returned: a partial
    response must never NULL-out columns we already have.
    """
    inv = invoice_payload or {}
    dop = inv.get("dropOffPoint") or {}
    values: dict = {}
    if dop.get("uuid"):
        values["drop_off_point_uuid"] = str(dop["uuid"])
    if dop.get("address"):
        values["drop_off_point_address"] = str(dop["address"])
    # dop/time-slot has no invoice_id in the request body — Uzum may answer
    # with a re-issued number. fbs_orders.invoice_number is the ownership
    # source for the invoice scope-guard, so it MUST follow, or the guard
    # would hide the moved invoice from its own seller.
    if inv.get("number"):
        values["invoice_number"] = str(inv["number"])
    return values


def _reflect_pickup_change_in_db(
    invoice_payload: dict | None,
    *,
    invoice_id: int | str,
    order_ids: list,
    user_shops: list[str],
) -> int:
    """Mirror a successful change-pickup into ``fbs_orders``. Best-effort:
    the Uzum-side move already succeeded, so a local write failure is
    logged, never raised. Returns the number of rows updated."""
    values = _pickup_update_values(invoice_payload)
    if not values or not order_ids:
        return 0
    new_id = (invoice_payload or {}).get("id")
    if new_id is not None and str(new_id) != str(invoice_id):
        print(f"[invoice/change-pickup] Uzum re-issued invoice: "
              f"id {invoice_id} -> {new_id}, "
              f"number={values.get('invoice_number')!r}")
    try:
        from sqlalchemy import update as sa_update
        from models import FbsOrder
        with SessionLocal() as db:
            res = db.execute(
                sa_update(FbsOrder)
                .where(FbsOrder.shop_id.in_([str(s) for s in user_shops]))
                .where(FbsOrder.order_id.in_([str(o) for o in order_ids]))
                .values(**values)
            )
            db.commit()
        rowcount = int(getattr(res, "rowcount", 0) or 0)
        print(f"[invoice/change-pickup] DB reflected: invoice={invoice_id} "
              f"orders={len(order_ids)} rows={rowcount} "
              f"dop={values.get('drop_off_point_uuid')!r} "
              f"addr={str(values.get('drop_off_point_address'))[:60]!r} "
              f"number={values.get('invoice_number')!r}")
        return rowcount
    except Exception as e:
        print(f"[invoice/change-pickup] DB reflect failed: {e!r}")
        return 0


@fbs_bp.post("/fbs/api/invoices/<int:invoice_id>/change-pickup")
@login_required
def fbs_invoice_change_pickup_api(invoice_id: int):
    """POST: Move an existing invoice to a different drop-off point + slot.

    Body JSON::

        {
          "point_uuid": "<uuid>",   # new drop-off point
          "slot_uuid":  "<uuid>",   # preferred — Uzum's canonical slot id
          "time_from":  "<ISO>",    # fallback (rarely used)
          "time_to":    "<ISO>"     # fallback (rarely used)
        }

    Routes to Uzum's ``POST /v1/fbs/invoice/dop/time-slot`` — same body
    shape as invoice creation, semantic is "update the point/slot of
    the existing invoice". The orderIds in the body MUST match the
    orders Uzum already has linked to this invoice; we pull them from
    our local DB cache (the ``invoice_number`` column is populated by
    the sync worker) and pass them through verbatim.

    Only CREATED-status invoices can be changed; once acceptance has
    begun Uzum returns ``seller-order-04`` and we surface the error.
    """
    payload = request.get_json(silent=True) or {}
    point_uuid = (payload.get("point_uuid") or "").strip()
    slot_uuid = (payload.get("slot_uuid") or "").strip()
    if not point_uuid:
        return _json_response({"error": _err("field_required", field="point_uuid")}, 400)
    if not slot_uuid:
        return _json_response({"error": _err("field_required", field="slot_uuid")}, 400)

    user_shops = _current_user_shop_uzum_ids()
    if not user_shops:
        return _json_response({"error": _err("no_shop")}, 403)

    uid = int(current_user.get_id())
    with SessionLocal() as db:
        user = db.get(User, uid)
        token = (user.uzum_openapi_token or "").strip() if user else ""
        # sellerId is auto-detected from finance + persisted (see
        # _ensure_seller_id) — the seller never pastes it manually.
        seller_id = _ensure_seller_id(db, user, token, user_shops)
    if not token:
        return _json_response({"error": _err("token_not_set")}, 400)
    if seller_id is None:
        return _json_response({
            "error": _err("seller_id_undetected_short")
        }, 400)

    # SCOPE GUARD before the MUTATION. Uzum only scopes to the token's
    # SELLER ACCOUNT — which covers EVERY shop on it, registered here or not —
    # so "Uzum will 403 a foreign invoice" was a wrong assumption: it happily
    # moves an unregistered shop's invoice. Verify against OUR shops first.
    # The guard fetches the invoice's authoritative orders (one paced call);
    # we hand them to change_invoice_pickup so the trip isn't repeated.
    owns, inv_orders = _user_owns_invoice(token, invoice_id, user_shops)
    if not owns:
        return _json_response({
            "error": _err("invoice_not_yours")
        }, 403)
    guard_order_ids = [
        o.get("orderId") for o in (inv_orders or [])
        if isinstance(o, dict) and o.get("orderId") is not None
    ] or None

    # The orders attached to this invoice come straight from Uzum — the
    # authoritative membership (GET /v1/fbs/invoice/{id}/orders). The old
    # `invoice_number LIKE '%suffix'` DB-match was fragile (suffix
    # collisions) and empty before the sync worker populated the column.
    from core.uzum_openapi import change_invoice_pickup
    try:
        invoice, used_url = change_invoice_pickup(
            token,
            invoice_id=invoice_id,
            seller_id=seller_id,
            drop_off_point_uuid=point_uuid,
            time_slot_uuid=slot_uuid,
            fail_fast=True,  # interactive mutation — fail fast on a burst-429
            order_ids=guard_order_ids,
        )
    except ValueError:
        # Uzum reports no orders on this invoice — nothing to move.
        return _json_response({
            "error": _err("invoice_no_orders")
        }, 404)
    except UzumAPIError as e:
        return _uzum_error_response(e)
    except Exception as e:
        print(f"[invoice/change-pickup] unexpected error: {e!r}")
        return _json_response({"error": str(e)[:200]}, 502)

    # The akt's address + slot just changed → its cached PDF is now stale.
    # Drop it so the next print re-fetches (or the worker re-prefetches) it.
    try:
        from core.fbs_akt_cache import delete_akt
        delete_akt(invoice_id)
    except Exception as e:
        print(f"[invoice/change-pickup] akt cache invalidation failed: {e!r}")

    # Reflect the move into fbs_orders IMMEDIATELY. The sync worker won't:
    # a pickup change keeps the order status, so the incremental stop
    # (`stop_on_known`) never re-reads these rows — without this write the
    # drop-off columns (and every view fed by them: «Mening punktlarim»,
    # order cards) keep the OLD point forever.
    _reflect_pickup_change_in_db(
        invoice, invoice_id=invoice_id,
        order_ids=guard_order_ids or [], user_shops=user_shops,
    )

    return _json_response({
        "ok": True,
        "mode": "updated",
        "used_url": used_url,
        "invoice": invoice,
    })


@fbs_bp.get("/fbs/api/invoices/<int:invoice_id>/akt.pdf")
@login_required
def fbs_invoice_akt_pdf(invoice_id: int):
    """Stream the "Акт поставки" PDF for an FBS invoice.

    Thin proxy over Uzum's ``GET /v1/fbs/invoice/{invoiceId}/print`` —
    Uzum returns a base64-encoded PDF with every field already populated
    (komitent legal data, slot, address, orders). We just decode and
    stream the bytes back. No local rendering, no komitent fields, no
    layout to maintain.

    Older revisions of this route built the PDF locally via ReportLab;
    that path was removed once Uzum's print endpoint was discovered.
    """
    uid = int(current_user.get_id())
    with SessionLocal() as db:
        user = db.get(User, uid)
        token = (user.uzum_openapi_token or "").strip() if user else ""
    if not token:
        return _json_response({"error": _err("token_not_set")}, 400)

    # Cache-first + SCOPE GUARD: a cache hit serves straight from the DB
    # (0 Uzum calls); a miss verifies the invoice belongs to one of the
    # user's registered shops BEFORE hitting Uzum's print endpoint — the
    # token alone would happily print any shop's akt on the seller account.
    user_shops = _current_user_shop_uzum_ids()
    try:
        pdf_bytes = _get_akt_guarded(token, uid, invoice_id, user_shops)
    except _ForeignInvoiceError:
        return _json_response({
            "error": _err("invoice_not_yours")
        }, 403)
    except UzumAPIError as e:
        return _uzum_error_response(e)
    except Exception as e:
        print(f"[invoices/akt.pdf] proxy failed: {e!r}")
        return _json_response({"error": str(e)[:200]}, 502)

    filename = f"akt_{invoice_id}.pdf"
    resp = Response(pdf_bytes, mimetype="application/pdf")
    # ``inline`` opens in a browser tab; users can still download with
    # Ctrl+S. Switch to ``attachment`` if you want a forced download.
    resp.headers["Content-Disposition"] = f'inline; filename="{filename}"'
    return resp


class _ForeignInvoiceError(Exception):
    """Raised by _get_akt_guarded when the invoice isn't the user's."""


def _get_akt_guarded(token: str, uid: int, invoice_id: int, user_shops: list[str]) -> bytes:
    """Cache-first akt fetch WITH the ownership boundary enforced.

    Cache hit (keyed by user_id) = ownership proof: rows only enter the cache
    through this guard or the worker prefetch, both of which are scoped to the
    user's registered shops — so a hit costs 0 Uzum calls, same as before.
    On a miss the invoice's ownership is verified first (one paced call via
    ``_user_owns_invoice``); a foreign invoice raises ``_ForeignInvoiceError``
    and Uzum's print endpoint is never hit for it.
    """
    from core.fbs_akt_cache import get_cached_akt, fetch_akt_live, store_akt
    cached = get_cached_akt(uid, invoice_id)
    if cached is not None:
        return cached
    owns, _ = _user_owns_invoice(token, invoice_id, user_shops)
    if not owns:
        raise _ForeignInvoiceError(str(invoice_id))
    pdf = fetch_akt_live(token, invoice_id)
    try:
        store_akt(uid, invoice_id, None, pdf)
    except Exception as e:  # caching is best-effort — never fail the request
        print(f"[akt-guard] store failed for invoice {invoice_id}: {e!r}")
    return pdf


# Cap the bulk-akt fan-out: each id is one paced Uzum print call, so a huge
# selection would block for minutes and hammer the token. 30 covers a full
# 20-row page plus slack; beyond that we ask the user to narrow down.
_BULK_AKT_MAX = 30


@fbs_bp.get("/fbs/api/invoices/akt-merged.pdf")
@login_required
def fbs_invoices_akt_merged_pdf():
    """Stream ONE merged "Akt поставки" PDF for several invoices.

    Query: ``ids`` — comma-separated invoice ids (max ``_BULK_AKT_MAX``).
    Fetches each invoice's akt via Uzum's print endpoint and concatenates
    them with pypdf into a single print-ready document.

    Each akt is served CACHE-FIRST via ``get_or_fetch_akt`` — the background
    worker usually has it pre-fetched, so a bulk print is mostly DB reads
    (0 Uzum calls, never 429). On a cache miss it fetches live with a short
    429-aware retry (the ``/print`` endpoint allows ~4 quick calls then 429,
    recovering in ~3–4s — probed live 2026-06-08; a plain ``fail_fast=True``
    would drop the 429'd akt and ``fail_fast=False`` would stall ~60s). Akts
    that still fail are skipped, logged, and reported via X-Akt-Skipped.
    """
    from io import BytesIO
    from pypdf import PdfReader, PdfWriter

    raw = request.args.get("ids") or ""
    ids: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except (TypeError, ValueError):
            continue
    # De-dupe while preserving order.
    seen: set[int] = set()
    ids = [i for i in ids if not (i in seen or seen.add(i))]
    if not ids:
        return _json_response({"error": _err("no_invoice_selected")}, 400)
    if len(ids) > _BULK_AKT_MAX:
        return _json_response(
            {"error": _err("bulk_akt_limit", n=_BULK_AKT_MAX)},
            400,
        )

    uid = int(current_user.get_id())
    with SessionLocal() as db:
        user = db.get(User, uid)
        token = (user.uzum_openapi_token or "").strip() if user else ""
    if not token:
        return _json_response({"error": _err("token_not_set")}, 400)

    # SCOPE GUARD per id: cache hits (the common case — the worker prefetches
    # the user's own akts) stay 0-Uzum-call; misses verify ownership before
    # printing. Foreign ids are skipped and reported like other failures.
    user_shops = _current_user_shop_uzum_ids()
    writer = PdfWriter()
    merged = 0
    failed: list[int] = []
    for iid in ids:
        try:
            pdf_bytes = _get_akt_guarded(token, uid, iid, user_shops)
            reader = PdfReader(BytesIO(pdf_bytes))
            for page in reader.pages:
                writer.add_page(page)
            merged += 1
        except _ForeignInvoiceError:
            print(f"[invoices/akt-merged] DENY foreign invoice {iid}")
            failed.append(iid)
        except Exception as e:
            print(f"[invoices/akt-merged] skip invoice {iid}: {e!r}")
            failed.append(iid)

    if merged == 0:
        return _json_response(
            {"error": _err("no_akt_loaded")},
            502,
        )

    out = BytesIO()
    writer.write(out)
    out.seek(0)
    filename = f"aktlar_{merged}ta.pdf"
    resp = Response(out.read(), mimetype="application/pdf")
    resp.headers["Content-Disposition"] = f'inline; filename="{filename}"'
    # Tell the client how many were skipped so the UI can warn the seller.
    if failed:
        resp.headers["X-Akt-Skipped"] = ",".join(str(i) for i in failed)
    return resp


# ─────────────────────────────────────────────────────────────────────────
# O6 — FBS «Ombor» (SKU stock) page + API (GET read · POST update)
# ─────────────────────────────────────────────────────────────────────────
@fbs_bp.get("/fbs/stock")
@login_required
def fbs_stock_page():
    """Render the FBS «Ombor» (SKU stock) page.

    Pure template render — the SKU list is fetched client-side via
    /fbs/api/sku-stocks so the page stays fast even when Uzum is slow.
    """
    token, _ = _current_user_token_and_shops()
    lang = session.get("lang", "uz")
    labels = STOCK_LABELS.get(lang, STOCK_LABELS["uz"])
    return render_template(
        "fbs_stock.html",
        title="FBS — ombor",
        has_token=bool(token),
        labels=labels,
    )


# ── Shop-scoping for the SELLER-level stock list ───────────────────────
# ``GET /v3/fbs/sku/stocks`` takes NO shopId — it returns every SKU under
# the token's seller ACCOUNT, including Uzum shops the seller never added
# to SellerHub (a separate cosmetics / clothing shop, etc.). We map each
# skuId → local shop_id via the Variant table and drop anything that does
# not belong to one of the user's OWN registered shops, so the «Ombor»
# grid and the Excel export show only the seller's SellerHub shops — never
# a foreign / unregistered one.
def _stock_shop_maps(db, user_shop_db_ids, sku_ids):
    """``skuId(str) → shop_id`` and ``→ image_url`` for the given SKU ids,
    restricted to the user's shops at the SQL level (so a foreign seller's
    SKU can never leak in). Returns ``(shop_by_sku, img_by_sku)``; the
    image is pre-downscaled 540→240 for the grid thumbnails."""
    shop_by_sku: dict[str, int] = {}
    img_by_sku: dict[str, str] = {}
    if not (sku_ids and user_shop_db_ids):
        return shop_by_sku, img_by_sku
    rows = db.execute(
        select(Variant.uzum_sku_id, Variant.image_url, ProductGroup.shop_id)
        .join(ProductGroup, Variant.group_id == ProductGroup.id)
        .where(Variant.uzum_sku_id.in_(sku_ids))
        .where(ProductGroup.shop_id.in_(user_shop_db_ids))
    ).all()
    for (sid, url, shop_id) in rows:
        key = str(sid)
        if shop_id is not None and key not in shop_by_sku:
            shop_by_sku[key] = shop_id
        if url and key not in img_by_sku:
            img_by_sku[key] = url.replace("/t_product_540_high.jpg", "/t_product_240_high.jpg")
    return shop_by_sku, img_by_sku


def _filter_stock_skus_to_shops(skus, shop_by_sku, allowed_shop_ids):
    """Keep only stock rows whose skuId maps to one of the user's shops.

    Pure (no DB) so it is unit-tested directly. A row is dropped when its
    skuId has no local Variant (foreign / unregistered shop) or maps to a
    shop outside ``allowed_shop_ids``. Returns ``(kept, hidden_count)``.
    """
    allowed = {int(s) for s in (allowed_shop_ids or [])}
    kept: list[dict] = []
    hidden = 0
    for s in skus:
        sid = s.get("skuId")
        shop_id = shop_by_sku.get(str(sid)) if sid is not None else None
        if shop_id is not None and int(shop_id) in allowed:
            kept.append(s)
        else:
            hidden += 1
    return kept, hidden


def _enrich_and_filter_stock_skus(uid: int, skus: list[dict]) -> tuple[list[dict], list[dict], int]:
    """Attach per-SKU image + owning shop, RU-localize titles, and DROP every
    SKU that isn't one of the user's own registered shops.

    The stock API is SELLER-level (no shopId), so it also returns SKUs from
    Uzum shops the seller never added to SellerHub — those must not appear in
    the «Ombor» grid (or the «Do'kon» filter). Shared by the cache/all path and
    the live-paginated path. Returns ``(kept_skus, shops_out, hidden_foreign)``.
    """
    shops_out: list[dict] = []
    hidden = 0
    try:
        user_shop_db_ids = _user_shop_ids(uid)
        with SessionLocal() as db:
            shop_rows = db.execute(
                select(Shop.id, Shop.name).where(Shop.id.in_(user_shop_db_ids))
            ).all() if user_shop_db_ids else []
            shop_name_by_id = {sid: (name or f"Do'kon #{sid}") for (sid, name) in shop_rows}
            shops_out = [{"id": sid, "name": nm} for sid, nm in shop_name_by_id.items()]

            sku_ids = [str(s.get("skuId")) for s in skus if s.get("skuId") is not None]
            # variants.image_url carries the TRUE per-SKU (per-colour) image:
            # the product sync overlays the cabinet sku-list ``imageHigh`` on
            # top of the OpenAPI product-level previewImage (see
            # _sync_cabinet_sku_images in app.py). We deliberately do NOT pull
            # from finance_orders: its image is a per-ORDER historical snapshot
            # that goes stale and mismatches the current listing.
            shop_by_sku, img_by_sku = _stock_shop_maps(db, user_shop_db_ids, sku_ids)
            # Localized товар nomi from our own catalog, keyed by skuId.
            # Only for RU — UZ keeps the live FBS title verbatim (no regression).
            _stk_lang = session.get("lang", "uz")
            title_by_sku = {}
            if _stk_lang == "ru":
                _, title_by_sku = _variant_titles(
                    db, user_shop_db_ids, sku_ids=sku_ids, lang=_stk_lang
                )

        # Keep only the user's own shops, then attach image + shop name.
        skus, hidden = _filter_stock_skus_to_shops(skus, shop_by_sku, user_shop_db_ids)
        for s in skus:
            key = str(s.get("skuId"))
            s["image"] = img_by_sku.get(key)
            tup = title_by_sku.get(key)
            if tup:
                ru_base, ru_color = tup
                s["productTitle"] = _localize_product_title(
                    s.get("productTitle") or s.get("skuTitle") or "", ru_base, ru_color, _stk_lang)
            shop_id = shop_by_sku.get(key)
            s["shopId"] = shop_id
            s["shopName"] = shop_name_by_id.get(shop_id) if shop_id is not None else None
    except Exception as e:
        print(f"[sku-stocks/list] image/shop enrich failed: {e!r}")
    return skus, shops_out, hidden


@fbs_bp.get("/fbs/api/sku-stocks")
@login_required
def fbs_sku_stocks_list_api():
    """GET: SKU stocks page — HYBRID ikki endpoint (Abdulaziz, 2026-07-03).

    Har bir endpoint bittasini bermaydi (probe bilan tasdiqlangan):
      * OpenAPI ``/v3/fbs/sku/stocks`` — bitta so'rovda 100 qator (tez!),
        LEKIN server-qidiruv YO'Q (searchText e'tiborsiz).
      * Portal ``/v1/seller/stock/sku`` — searchText/shopIds/IN_STOCK
        SERVERDA, LEKIN qat'iy 20 qator/so'rov (size=30 → 400).

    Marshrutlash (segment «amount» bo'yicha, endpoint tezligiga qarab):
      * «Hammasi» va «Tugagan» (qidiruvsiz, do'konsiz) → v3, 100/sahifa —
        asosiy og'ir scroll shular, eng tez. «Tugagan» = v3 sahifadan
        ``amount==0`` client-side ajratiladi (bulk nol → sahifa ~to'la).
      * «Mavjud» → portal ``IN_STOCK`` (amount>0, odatda kam natija, 20 kifoya).
      * QIDIRUV yoki DO'KON tanlansa → portal (server searchText/shopIds);
        «Tugagan» bo'lsa ``amount==0`` client-side ajratiladi.

    Nega SOLD_OUT emas: portalning ``SOLD_OUT`` enum'i «qo'lda nolga
    tushirilgan» tor ro'yxat (~15), «amount=0» (1200) EMAS — probe tasdiqladi.
    Shuning uchun «Tugagan» doim ``amount==0`` client-filtri.

    Params: ``page`` (0+), ``search``, ``shop`` (o'z do'koni DB id'si),
    ``avail`` (all|in|out). Ikki javob shakli har xil, lekin
    ``normalize_portal_sku`` / v3-enrich ikkalasini ``{skuId, image, amount,
    …}`` ga keltiradi — frontend farqni sezmaydi.
    """
    from core.fbs_portal_stock import fetch_portal_sku_stocks_page
    from core.uzum_openapi import fetch_fbs_sku_stocks_page

    uid = int(current_user.get_id())
    try:
        page = max(0, min(int(request.args.get("page", 0)), 100_000))
    except (TypeError, ValueError):
        page = 0
    search = (request.args.get("search") or "").strip()
    shop_f = (request.args.get("shop") or "").strip()
    avail = (request.args.get("avail") or "all").strip().lower()
    # «Ombor» (default) = FAQAT sxemaga ulangan SKU — Uzum'ning o'z sahifasi
    # ham shunday (`linked=true`). `linked=0` → «Barcha tovarlar» ko'rinishi,
    # omborga yangi SKU qo'shish uchun kerak.
    linked_only = (request.args.get("linked") or "1").strip() != "0"

    user_shop_db_ids = _user_shop_ids(uid)
    with SessionLocal() as db:
        user = db.get(User, uid)
        seller_id = user.uzum_seller_id if user else None
        openapi_token = (user.uzum_openapi_token or "").strip() if user else ""
        shop_rows = db.execute(
            select(Shop.id, Shop.uzum_id, Shop.name).where(Shop.id.in_(user_shop_db_ids))
        ).all() if user_shop_db_ids else []
    shops_out = [{"id": sid, "name": name or f"Do'kon #{sid}"}
                 for (sid, _uz, name) in shop_rows]

    def _out_only(rows):
        # «Tugagan» = amount==0 (portal SOLD_OUT yaramaydi).
        return [s for s in rows if int(s.get("amount") or 0) == 0]

    # ── Endpoint tanlash ─────────────────────────────────────────────────
    # «Ombor» ko'rinishi (linked_only, default) → DOIM portal: `linked=true`
    # faqat o'sha yerda bor, OpenAPI v3'da bunday filtr YO'Q. seller_id
    # bo'lmasa (portal ishlamaydi) — v3 ga tushib, ulanganlarni Python'da
    # ajratamiz (sekinroq, lekin ishlaydi).
    # «Barcha tovarlar» ko'rinishi → eski gibrid: toza-ko'rish v3 (100/sahifa),
    # qidiruv/do'kon/«Mavjud» portal (server-side).
    if linked_only:
        use_v3 = (not seller_id) and bool(openapi_token)
    else:
        use_v3 = (not search) and (not shop_f) and (avail in ("all", "out")) and openapi_token
    if use_v3:
        try:
            raw, _ = fetch_fbs_sku_stocks_page(
                openapi_token, page=page, size=100, fail_fast=True)
        except UzumAPIError as e:
            return _uzum_error_response(e)
        except Exception as e:
            print(f"[sku-stocks/list] v3 error: {e!r}", flush=True)
            return _json_response({"error": str(e)[:200]}, 502)
        # Rasm/do'kon enrich + chet (ro'yxatdan tashqari) do'konni tashlaydi.
        skus, shops_v3, hidden = _enrich_and_filter_stock_skus(uid, raw)
        # ── Begona-token himoyasi (Abdulaziz 2026-07-04) ─────────────────
        # v3 SELLER-darajali: OpenAPI token foydalanuvchining SellerHub
        # do'konlaridan BOSHQA seller akkauntiga tegishli bo'lsa, butun sahifa
        # begona bo'lib filtrlanadi (raw>0, sent=0). Bunda ilgari `has_more`
        # XOM sahifadan (100) hisoblanib `hasMore=True` qaytardi → frontend
        # bo'sh jadval ustidan butun begona katalogni cheksiz «Загрузка…»
        # qilardi (user_id=3 real bug). Begona token HAR sahifada begona
        # bo'lgani uchun bu holatda o'z-o'zini-yetkazuvchi PORTAL yo'liga
        # tushamiz — hamma sahifa bir xilda portalga o'tadi, aralashuv yo'q.
        if raw and not skus:
            print(f"[sku-stocks/list] user_id={uid} v3 page={page} avail={avail} "
                  f"raw={len(raw)} sent=0 hidden={hidden} → PORTAL fallback "
                  f"(token boshqa sellerники?)", flush=True)
        else:
            if shops_v3:
                shops_out = shops_v3
            if linked_only:
                # v3'da `linked` filtri yo'q → o'zimiz ajratamiz (sxemaga
                # ulanmagan SKU omborda EMAS).
                skus = [s for s in skus
                        if s.get("fbsLinked") or s.get("dbsLinked")]
            if avail == "out":
                skus = _out_only(skus)
            # hasMore = XOM sahifa to'liq (100) → yana bor (filtr qisqartirsa ham).
            has_more = len(raw) >= 100
            print(f"[sku-stocks/list] user_id={uid} v3 page={page} avail={avail} "
                  f"raw={len(raw)} sent={len(skus)} hidden={hidden} hasMore={has_more}", flush=True)
            resp = {"ok": True, "skus": skus, "page": page, "hasMore": has_more}
            if page == 0:
                resp["shops"] = shops_out
            return _json_response(resp)

    # ── «Mavjud» / QIDIRUV / DO'KON → portal (server-side, 20/sahifa) ─────
    if not seller_id:
        return _json_response({"error": _err("seller_id_missing")}, 400)
    if shop_f:
        uzum_ids = [uz for (sid, uz, _n) in shop_rows if str(sid) == shop_f and uz]
    else:
        uzum_ids = [uz for (_sid, uz, _n) in shop_rows if uz]

    try:
        skus, has_more = fetch_portal_sku_stocks_page(
            seller_id=seller_id, shop_uzum_ids=uzum_ids,
            page=page, search=search, in_stock_only=(avail == "in"),
            linked_only=linked_only,
        )
    except Exception as e:
        print(f"[sku-stocks/list] portal error: {e!r}", flush=True)
        return _json_response({"error": str(e)[:200]}, 502)

    # Himoyaviy segment-filtri: IN_STOCK server-side to'g'ri (probe), lekin
    # SOLD_OUT'dagidek kutilmagan semantikadan saqlanish uchun «amount»ni
    # o'zimiz ham tekshiramiz — segment doim aniq to'g'ri bo'ladi.
    if avail == "in":
        skus = [s for s in skus if int(s.get("amount") or 0) > 0]
    elif avail == "out":
        skus = _out_only(skus)

    print(f"[sku-stocks/list] user_id={uid} portal page={page} q={search!r} "
          f"shop={shop_f!r} avail={avail} linked={linked_only} "
          f"sent={len(skus)} hasMore={has_more}", flush=True)
    resp = {"ok": True, "skus": skus, "page": page, "hasMore": has_more}
    if page == 0:
        resp["shops"] = shops_out   # shop dropdown only needs filling once
    return _json_response(resp)


@fbs_bp.post("/fbs/api/sku-stocks")
@login_required
def fbs_sku_stocks_update_api():
    """POST: update FBS/DBS stock for the given SKUs (MUTATION).

    Body: ``{"skuAmountList": [{skuId, amount, skuTitle, productTitle,
    barcode, fbsLinked, dbsLinked}, ...]}`` — the frontend round-trips the
    exact rows it loaded, changing only ``amount``. Mocked-pytest covered
    (tests/test_fbs_sku_stocks.py) before going live (Uzum penalty risk).
    Needs ``SKU_UPDATE`` on the token (else Uzum 403).
    """
    from core.uzum_openapi import update_fbs_sku_stocks

    payload = request.get_json(silent=True) or {}
    items = payload.get("skuAmountList")
    if not isinstance(items, list) or not items:
        return _json_response({"error": _err("no_sku_to_update")}, 400)

    uid = int(current_user.get_id())
    with SessionLocal() as db:
        user = db.get(User, uid)
        token = (user.uzum_openapi_token or "").strip() if user else ""
    if not token:
        return _json_response({"error": _err("token_not_set")}, 400)

    # SCOPE GUARD: the token writes stock for EVERY shop on the seller
    # account. The grid the user sees is already shop-filtered, but the API
    # itself must enforce it too — otherwise a hand-crafted request can zero
    # a foreign (unregistered) shop's stock. Whole-request reject, no partial
    # writes. Same Variant-mapping semantics as the read side.
    try:
        user_shop_db_ids = _user_shop_ids(uid)
        sku_ids = [str(it.get("skuId")) for it in items
                   if isinstance(it, dict) and it.get("skuId") is not None]
        with SessionLocal() as db:
            shop_by_sku, _ = _stock_shop_maps(db, user_shop_db_ids, sku_ids)
        bad = _foreign_sku_ids(items, shop_by_sku, user_shop_db_ids)
    except Exception as e:
        print(f"[sku-stocks/update] scope-guard failed: {e!r}")
        return _json_response({"error": _err("ownership_check_failed")}, 502)
    if bad:
        print(f"[sku-stocks/update] DENY user_id={uid}: {len(bad)} foreign/unknown "
              f"skuId(s): {','.join(bad[:10])}", flush=True)
        return _json_response({
            "error": _err("foreign_sku_update", n=len(bad))
        }, 403)

    try:
        result, used_url = update_fbs_sku_stocks(token, sku_amounts=items, fail_fast=True)
    except ValueError:
        return _json_response({"error": _err("no_valid_sku")}, 400)
    except UzumAPIError as e:
        return _uzum_error_response(e)
    except Exception as e:
        print(f"[sku-stocks/update] unexpected error: {e!r}")
        return _json_response({"error": str(e)[:200]}, 502)

    return _json_response({"ok": True, "used_url": used_url, "result": result})


# ── O6 «Обновить через файл» — Excel export / import ───────────────────
# Three endpoints, intentionally split so the bulk MUTATION is a deliberate
# two-step (preview → apply):
#   GET  /fbs/api/sku-stocks/export          → download current stock .xlsx
#   POST /fbs/api/sku-stocks/import-preview  → parse + diff vs live (no write)
#   POST /fbs/api/sku-stocks/import-apply    → write ONLY the confirmed diff
# Parse/diff are unit-tested (tests/test_fbs_excel.py) before prod.
_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_IMPORT_CHUNK = 500  # rows per Uzum POST when applying a large change-set


@fbs_bp.get("/fbs/api/sku-stocks/export")
@login_required
def fbs_sku_stocks_export_api():
    """GET: download the seller's current FBS/DBS stock as an .xlsx whose
    format matches Uzum's own template (so it re-imports here or there)."""
    from flask import Response
    from core.uzum_openapi import fetch_fbs_sku_stocks
    from core.fbs_excel import build_stock_workbook, workbook_to_bytes

    uid = int(current_user.get_id())
    with SessionLocal() as db:
        user = db.get(User, uid)
        token = (user.uzum_openapi_token or "").strip() if user else ""
    if not token:
        return _json_response({"error": _err("token_not_set")}, 400)

    try:
        skus, _ = fetch_fbs_sku_stocks(token, fail_fast=True)
    except UzumAPIError as e:
        return _uzum_error_response(e)
    except Exception as e:
        print(f"[sku-stocks/export] unexpected error: {e!r}")
        return _json_response({"error": str(e)[:200]}, 502)

    # Mirror the «Ombor» grid: export ONLY the user's own shops, never the
    # raw SELLER-level dump (which includes foreign / unregistered shops).
    hidden = 0
    try:
        user_shop_db_ids = _user_shop_ids(uid)
        sku_ids = [str(s.get("skuId")) for s in skus if s.get("skuId") is not None]
        with SessionLocal() as db:
            shop_by_sku, _img = _stock_shop_maps(db, user_shop_db_ids, sku_ids)
        skus, hidden = _filter_stock_skus_to_shops(skus, shop_by_sku, user_shop_db_ids)
    except Exception as e:
        print(f"[sku-stocks/export] shop-filter failed: {e!r}")

    data = workbook_to_bytes(build_stock_workbook(skus))
    print(f"[sku-stocks/export] user_id={uid} rows={len(skus)} hidden_foreign={hidden} bytes={len(data)}", flush=True)
    return Response(
        data, mimetype=_XLSX_MIME,
        headers={"Content-Disposition": 'attachment; filename="ombor_qoldiq.xlsx"'},
    )


@fbs_bp.post("/fbs/api/sku-stocks/import-preview")
@login_required
def fbs_sku_stocks_import_preview_api():
    """POST (multipart ``file``): parse the upload, diff against the LIVE
    stock, and return what WOULD change — without writing anything."""
    from core.uzum_openapi import fetch_fbs_sku_stocks
    from core.fbs_excel import parse_stock_rows, diff_against_current

    f = request.files.get("file")
    if f is None or not f.filename:
        return _json_response({"error": _err("no_file")}, 400)
    if not f.filename.lower().endswith((".xlsx", ".xlsm")):
        return _json_response({"error": _err("only_xlsx")}, 400)

    uid = int(current_user.get_id())
    with SessionLocal() as db:
        user = db.get(User, uid)
        token = (user.uzum_openapi_token or "").strip() if user else ""
    if not token:
        return _json_response({"error": _err("token_not_set")}, 400)

    try:
        desired, stats = parse_stock_rows(f.read())
    except Exception as e:
        print(f"[sku-stocks/import-preview] parse error: {e!r}")
        return _json_response({"error": _err("file_unreadable")}, 400)
    if not desired:
        return _json_response({"error": _err("no_valid_rows"), "stats": stats}, 400)

    try:
        current, _ = fetch_fbs_sku_stocks(token, fail_fast=True)
    except UzumAPIError as e:
        return _uzum_error_response(e)
    except Exception as e:
        print(f"[sku-stocks/import-preview] live fetch error: {e!r}")
        return _json_response({"error": str(e)[:200]}, 502)

    # Scope the live baseline to the user's own shops so a row in the upload
    # that points at a foreign / unregistered shop is treated as UNKNOWN and
    # can never be written (the apply step only ever sends matched rows).
    try:
        user_shop_db_ids = _user_shop_ids(uid)
        cur_ids = [str(s.get("skuId")) for s in current if s.get("skuId") is not None]
        with SessionLocal() as db:
            shop_by_sku, _img = _stock_shop_maps(db, user_shop_db_ids, cur_ids)
        current, _hidden = _filter_stock_skus_to_shops(current, shop_by_sku, user_shop_db_ids)
    except Exception as e:
        print(f"[sku-stocks/import-preview] shop-filter failed: {e!r}")

    diff = diff_against_current(desired, current)
    print(f"[sku-stocks/import-preview] user_id={uid} parsed={stats['parsed']} "
          f"changes={len(diff['changes'])} unchanged={diff['unchanged']} "
          f"unknown={diff['unknown']}", flush=True)
    return _json_response({
        "ok": True,
        "changes": diff["changes"],
        "counts": {
            "changed": len(diff["changes"]),
            "unchanged": diff["unchanged"],
            "unknown": diff["unknown"],
            "skipped_no_id": stats["skipped_no_id"],
            "skipped_no_amount": stats["skipped_no_amount"],
        },
    })


@fbs_bp.post("/fbs/api/sku-stocks/import-apply")
@login_required
def fbs_sku_stocks_import_apply_api():
    """POST ``{changes:[...]}``: write the confirmed diff to Uzum (MUTATION).

    The rows come straight from import-preview, so they are already the
    minimal changed set. Large sets are chunked to keep each Uzum POST
    sane. Same SKU_UPDATE-permission + penalty rules as the live editor.
    """
    from core.uzum_openapi import update_fbs_sku_stocks

    payload = request.get_json(silent=True) or {}
    changes = payload.get("changes")
    if not isinstance(changes, list) or not changes:
        return _json_response({"error": _err("no_changes_to_save")}, 400)

    uid = int(current_user.get_id())
    with SessionLocal() as db:
        user = db.get(User, uid)
        token = (user.uzum_openapi_token or "").strip() if user else ""
    if not token:
        return _json_response({"error": _err("token_not_set")}, 400)

    # SCOPE GUARD — same as the live editor's: the Excel rows come from the
    # filtered export, but the apply API must verify shop ownership itself.
    try:
        user_shop_db_ids = _user_shop_ids(uid)
        sku_ids = [str(it.get("skuId")) for it in changes
                   if isinstance(it, dict) and it.get("skuId") is not None]
        with SessionLocal() as db:
            shop_by_sku, _ = _stock_shop_maps(db, user_shop_db_ids, sku_ids)
        bad = _foreign_sku_ids(changes, shop_by_sku, user_shop_db_ids)
    except Exception as e:
        print(f"[sku-stocks/import-apply] scope-guard failed: {e!r}")
        return _json_response({"error": _err("ownership_check_failed")}, 502)
    if bad:
        print(f"[sku-stocks/import-apply] DENY user_id={uid}: {len(bad)} foreign/unknown "
              f"skuId(s): {','.join(bad[:10])}", flush=True)
        return _json_response({
            "error": _err("foreign_sku_save", n=len(bad))
        }, 403)

    applied = 0
    last_url = ""
    try:
        for i in range(0, len(changes), _IMPORT_CHUNK):
            chunk = changes[i:i + _IMPORT_CHUNK]
            _, last_url = update_fbs_sku_stocks(token, sku_amounts=chunk, fail_fast=True)
            applied += len(chunk)
    except ValueError:
        return _json_response({"error": _err("no_valid_sku")}, 400)
    except UzumAPIError as e:
        return _uzum_error_response(e)
    except Exception as e:
        print(f"[sku-stocks/import-apply] unexpected error: {e!r}")
        return _json_response({"error": str(e)[:200], "applied": applied}, 502)

    # (Ilgari bu yerda `_patch_sku_stock_cache_amounts(uid, changes)` turardi —
    # «Ombor» SWR keshiga ko'chirish. Kesh 2026-07-03 da butunlay olib
    # tashlangan, funksiya ham o'chirilgan, lekin chaqiruv qolib ketgan edi:
    # Uzum'ga yozuv KETGANDAN KEYIN NameError → foydalanuvchi 500 ko'rar,
    # aslida import muvaffaqiyatli bo'lgan edi. Kesh yo'q — ko'chiradigan joy
    # ham yo'q.)
    print(f"[sku-stocks/import-apply] user_id={uid} applied={applied}", flush=True)
    return _json_response({"ok": True, "applied": applied, "used_url": last_url})


@fbs_bp.get("/fbs/api/return-reasons")
@login_required
def fbs_return_reasons_api():
    """GET: Cached enum of cancel reasons for the cancel modal.

    Shared across all users — the loader is keyed globally; whichever
    user's request warms the cache fills it for everyone else.

    Logs every fetch with the ``[fbs.reasons]`` tag — surfaces both the
    count and the first item's keys so a future field-name change at
    Uzum is obvious in logs immediately.
    """
    uid = int(current_user.get_id())
    with SessionLocal() as db:
        user = db.get(User, uid)
        token = (user.uzum_openapi_token or "").strip() if user else ""
    if not token:
        print(f"[fbs.reasons] REJECT user_id={uid} — no token", flush=True)
        return _json_response(
            {"error": _err("token_not_set")}, 400
        )

    try:
        reasons, used_url = get_return_reasons(token, fail_fast=True)
    except UzumAPIError as e:
        print(f"[fbs.reasons] UZUM_ERROR code={getattr(e, 'code', '?')} "
              f"msg={str(e)[:200]!r}", flush=True)
        return _uzum_error_response(e)
    except Exception as e:
        print(f"[fbs.reasons] EXCEPTION {type(e).__name__}: {e}", flush=True)
        return _json_response({"error": str(e)}, 502)

    sample_keys = list((reasons[0] if reasons else {}).keys())
    print(f"[fbs.reasons] OK count={len(reasons)} first_item_keys={sample_keys} "
          f"used_url={used_url}", flush=True)
    return _json_response({
        "ok": True,
        "used_url": used_url,
        "reasons": reasons,
    })


# ── Stage 3: DBS action endpoints ─────────────────────────────────────
# Same ownership/error pattern as the FBS action endpoints above. URL
# prefix ``/fbs/api/dbs/`` deliberately keeps these under the existing
# ``fbs_bp`` blueprint — DBS isn't a separate domain in this codebase,
# it's a shipping scheme on the same order objects.


@fbs_bp.post("/fbs/api/dbs/<int:order_id>/delivering")
@login_required
def dbs_delivering_api(order_id: int):
    """POST: Seller takes the DBS order out for delivery.

    PACKING → DELIVERING. Body: none. Uzum rejects with
    ``fbs-18-invalid-order-type`` if called on a FBS order — we surface
    that as a UZ-localized error so the frontend doesn't need to know.
    """
    token, shop_id, _order, err_resp = _resolve_token_and_check_order(order_id)
    if err_resp is not None:
        return err_resp

    try:
        order, used_url = dbs_delivering(token, order_id, shop_id, fail_fast=True)
    except UzumAPIError as e:
        return _uzum_error_response(e)
    except Exception as e:
        return _json_response({"error": str(e)}, 502)

    return _json_response({
        "ok": True,
        "order_id": order_id,
        "used_url": used_url,
        "order": order,
    })


@fbs_bp.post("/fbs/api/dbs/<int:order_id>/completed")
@login_required
def dbs_completed_api(order_id: int):
    """POST: Seller marks the DBS order as delivered to customer.

    DELIVERING → COMPLETED. Body JSON (optional): ``{"issue_code": N}``
    where ``N`` is the SMS verification code the customer reads to the
    courier. Orders that don't require a code work with body ``{}`` or
    no body at all; the int gate below normalises both cases to None.
    """
    payload = request.get_json(silent=True) or {}
    raw_code = payload.get("issue_code")
    issue_code: int | None
    if raw_code is None or raw_code == "":
        issue_code = None
    else:
        try:
            issue_code = int(raw_code)
        except (TypeError, ValueError):
            return _json_response(
                {"error": _err("issue_code_numeric")}, 400
            )

    token, shop_id, _order, err_resp = _resolve_token_and_check_order(order_id)
    if err_resp is not None:
        return err_resp

    try:
        order, used_url = dbs_completed(
            token, order_id, shop_id, issue_code=issue_code,
            fail_fast=True,
        )
    except UzumAPIError as e:
        return _uzum_error_response(e)
    except Exception as e:
        return _json_response({"error": str(e)}, 502)

    return _json_response({
        "ok": True,
        "order_id": order_id,
        "used_url": used_url,
        "order": order,
    })


@fbs_bp.post("/fbs/api/dbs/<int:order_id>/refund")
@login_required
def dbs_refund_api(order_id: int):
    """POST: Open a refund flow on a completed DBS order.

    COMPLETED → RETURNED (or refund-pending depending on Uzum's state
    machine). Body: none — Uzum's endpoint takes no items list and no
    reason enum here, it's a single-button action.

    The frontend shows a confirm() dialog before hitting this endpoint
    because the action is hard to reverse.
    """
    token, shop_id, _order, err_resp = _resolve_token_and_check_order(order_id)
    if err_resp is not None:
        return err_resp

    try:
        body, used_url = dbs_refund(token, order_id, shop_id, fail_fast=True)
    except UzumAPIError as e:
        return _uzum_error_response(e)
    except Exception as e:
        return _json_response({"error": str(e)}, 502)

    return _json_response({
        "ok": True,
        "order_id": order_id,
        "used_url": used_url,
        "result": body,
    })
