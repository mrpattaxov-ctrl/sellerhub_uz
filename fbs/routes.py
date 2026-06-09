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
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Blueprint, Response, render_template, request, session
from flask_login import current_user, login_required
from sqlalchemy import select, func

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


# ── Uzum error code → UZ user-facing message ─────────────────────────
# Keys mirror the ``seller-order-NN`` codes Uzum returns in errors[].
# When Uzum returns an unknown code we fall back to the raw Russian
# message Uzum sent; if that's also empty, a generic "HTTP {n}" string.
FBS_ERROR_MESSAGES_UZ: dict[str, str] = {
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
    "seller-order-14": "Etiketka xizmati vaqtincha ishlamayapti, keyinroq urinib ko'ring",
    "seller-order-15": "Buyurtma uchun identifikatorlar yetishmaydi",
    "seller-order-36": "Buyurtma pozitsiyasi topilmadi",
    # ── Stage 3 DBS-specific error codes ───────────────────────────
    "seller-order-37": "Tasdiqlash kodi topilmadi — mijoz oldidagi kodni kiriting",
    "seller-order-38": "Tasdiqlash kodi noto'g'ri",
    "seller-order-39": "Tasdiqlash kodi noto'g'ri — birozdan keyin qayta urinib ko'ring",
    "fbs-18-invalid-order-type": "Bu amal faqat DBS buyurtmalar uchun",
    # ── Bosqich 7 (накладная) error codes ─────────────────────────────
    "seller-order-19": "Накладная topilmadi (avval yaratilmagan)",
    "seller-order-23": "Tanlangan vaqt slot mavjud emas — boshqa slot tanlang",
    "seller-order-04": "Накладная pozitsiyasi topilmadi",
    "fbs-2-seller-access-denied": "Sotuvchida bu amal uchun ruxsat yo'q",
    "fbs-19-time-is-up": "Yetkazib berish muddati o'tib ketgan",
    "fbs-20-incompatible-dimensional-groups": "Buyurtmalar gabarit guruhi mos kelmaydi",
    "fbs-24-invoice-wrong-order-status": (
        "Ba'zi buyurtmalar holati o'zgargan (allaqachon boshqa накладная ichiga tushgan?) — "
        "«Yangilash» tugmasini bosing va qayta urinib ko'ring"
    ),
}


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
        "bulk_btn_postavka":         "Postavka yaratish",
        "bulk_postavka_hint":        "Tanlangan buyurtmalar uchun postavka (накладная) yaratish — qabul punkti va vaqt tanlanadi",
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
        "dropoff_search_ph":         "Manzil yoki do'kon bo'yicha qidirish… (masalan: Yunusobod, Chilonzor)",
        "dropoff_search_empty":      "Bu qidiruv bo'yicha hech qaysi punkt topilmadi:",
        "dropoff_slot_click_hint":   "Накладная yaratish uchun bosing",
        "dropoff_match_only":        "Faqat mos keladigan",
        "dropoff_match_hint":        "Tanlangan buyurtmalar uchun mos vaqtlarnigina ko'rsatamiz. Hammasini ko'rish uchun — filtrni o'chiring",
        "invoice_no_dop":            "Punkt tanlanmagan",
        "invoice_no_packing":        "Yig'ilmoqda holatidagi buyurtma yo'q. Avval CREATED ni tasdiqlang.",
        "invoice_confirm_prompt":    "{n} ta buyurtmani {point} punktiga {slot} vaqtga jo'natamiz. Tasdiqlaysizmi?",
        "invoice_mode_created":      "Накладная yaratildi",
        "invoice_mode_updated":      "Накладная yangilandi",
        "invoice_create_fail":       "Накладная yaratishda xato",
        "invoice_multi_shop_warn":   "Faqat {n} ta buyurtma yuborilmoqda (do'kon bo'yicha eng katta guruh). Boshqa {other} ta buyurtma alohida накладная ichiga ketadi.",
        "dropoff_geo_unavailable":   "Joylashuv ma'lumoti mavjud emas (GPS/internet?)",
        "dropoff_geo_timeout":       "Joylashuvni aniqlash juda uzoq cho'zildi",
        "dropoff_dist_hint":         "Sizdan to'g'ri masofa (havoda)",
        "dropoff_nearest":           "EN YAQIN",
        "dropoff_fav_add":           "Sevimli sifatida belgilash",
        "dropoff_fav_remove":        "Sevimlidan olib tashlash",
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
        "invoice_confirm_title":     "Postavka yaratish",
        "confirm_cancel":            "Bekor qilish",
        "bulk_toast_labels_ok":      "ta yorliq tayyorlandi",
        "bulk_toast_labels_partial": "ta yorliq tayyorlandi, {n} tasi xato",
        "bulk_toast_labels_fail":    "Yorliqlarni olishda xato",
        "bulk_toast_confirm_ok":     "ta buyurtma tasdiqlandi",
        "bulk_toast_confirm_partial":"ta tasdiqlandi, {n} tasi xato",
        "bulk_toast_confirm_fail":   "Tasdiqlashda xato",
        "bulk_select_all_aria":      "Hammasini tanlash",
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
        "dropoff_search_ph":         "Поиск по адресу или магазину… (напр.: Чилонзор, Юнусобод)",
        "dropoff_search_empty":      "По этому запросу ничего не найдено:",
        "dropoff_slot_click_hint":   "Нажмите чтобы создать накладную",
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
        "bulk_select_all_aria":      "Выбрать все",
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
        "identifier_required_flag": "IMEI talab qilinadi",
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
        # Actions
        "act_confirm": "Tasdiqlash",
        "act_cancel": "Bekor qilish",
        "act_print": "Etiketka chop etish",
        "act_print_enlarged": "Yorliq (katta matn)",
        "act_print_qr": "QR chop etish",
        "act_imei": "IMEI biriktirish",
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
        "imei_desc": "Har mahsulot uchun zarur identifikatorlarni (IMEI/seriya) kiriting. Bo'sh maydonlar yuborilmaydi.",
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
        "toast_label_opened_suffix": "ta etiketka ochildi",
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
        "identifier_required_flag": "Требуется IMEI",
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
        # Actions
        "act_confirm": "Подтвердить",
        "act_cancel": "Отменить",
        "act_print": "Печать этикетки",
        "act_print_enlarged": "Этикетка (крупно)",
        "act_print_qr": "Печать QR",
        "act_imei": "Привязать IMEI",
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
        "imei_desc": "Для каждого товара введите нужные идентификаторы (IMEI/серийник). Пустые поля не отправляются.",
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


def _format_uzum_payload_detail(payload) -> str | None:
    """Distil Uzum's structured ``errors[].payload`` into a short UZ
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
            return f"muddat: {formatted}"
    return None


def _uzum_error_response(err: UzumAPIError):
    """Convert ``UzumAPIError`` into a JSON response with UZ message.

    Status mapping:
      * Uzum 429 → 429 ("Uzum band, bir-ikki soniyadan keyin urinib ko'ring")
      * Uzum 5xx → 502 ("vaqtincha ishlamayapti, qaytadan urinib ko'ring")
      * Uzum 4xx → 400 (user-facing reason, e.g. wrong status / bad input)
      * Anything else → 502 (defensive default; network/timeout layer)
    """
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
        return _json_response(
            {
                "error": "Uzum hozir band. Bir-ikki soniyadan keyin qayta urinib ko'ring.",
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

    if code in FBS_ERROR_MESSAGES_UZ:
        msg = FBS_ERROR_MESSAGES_UZ[code]
    elif err.message:
        # Uzum has its own Russian message — show it as-is rather than
        # invent a translation we don't actually know.
        msg = err.message
    elif err.http_status >= 500:
        msg = "Uzum xizmati vaqtincha ishlamayapti. Bir-ikki daqiqadan keyin urinib ko'ring."
    else:
        msg = f"Uzum xatosi (HTTP {err.http_status})"

    # Enrich the message with a clean detail distilled from Uzum's
    # structured payload (e.g. the exact deadline for a "muddat o'tgan"
    # error). Only appended when we can confidently format it — otherwise
    # the message stays as-is and the raw payload rides in ``uzum_payload``.
    detail_suffix = _format_uzum_payload_detail(payload)
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
        "RETURNED":                             "Возврат",
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
    lang = session.get("lang", "ru")
    labels = STATUS_LABELS.get(lang, STATUS_LABELS["ru"])
    list_labels = LIST_LABELS.get(lang, LIST_LABELS["ru"])
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
    lang = session.get("lang", "ru")
    # Same label dict the orders page uses — the "Изменить" pickup modal
    # reuses the rich drop-off picker UI (dropoff_* labels) ported from
    # fbs_orders.html, so it needs the localized strings.
    list_labels = LIST_LABELS.get(lang, LIST_LABELS["ru"])
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
        title="FBS — Накладные",
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
    lang = session.get("lang", "ru")
    labels = DETAIL_LABELS.get(lang, DETAIL_LABELS["ru"])
    status_labels = STATUS_LABELS.get(lang, STATUS_LABELS["ru"])
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

    # "all" → aggregate the DB across every shop this user can access.
    # On ?refresh=1, sync the orders list from Uzum ONLY when the
    # current chip is one of the 3 active-work statuses (CREATED /
    # PACKING / PENDING_DELIVERY) — Abdulaziz 2026-05-26: "faqat shu
    # 3 knopka, qolgani har 10 daqiqada bg worker". For the other 8
    # chips the press is a pure DB read; the 10-min worker keeps them
    # current enough.
    #
    # Single-page variant: for active chips, low volume means one page
    # (~50 orders) is usually the full set anyway; even if it isn't,
    # the visible page-0 list is what the seller is staring at, so
    # that's what we make live.
    if shop_id == "all":
        user_shops = _current_user_shop_uzum_ids()
        if (
            _refresh_requested()
            and user_shops
            and status_val in _FBS_REFRESH_ON_PRESS_SYNC
        ):
            uid = int(current_user.get_id())
            with SessionLocal() as db:
                user = db.get(User, uid)
                token = (user.uzum_openapi_token or "").strip() if user else ""
            if token:
                from core.fbs_data import _refresh_shops_status_first_page, invalidate_fbs_cache
                try:
                    _refresh_shops_status_first_page(
                        token, list(user_shops), status_val, size=size,
                    )
                except Exception as e:
                    print(f"[orders-all/sync-refresh] shops={user_shops} status={status_val}: {e!r}")
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

    # ?refresh=1: pass through to the data layer, which (Stage 4c) runs
    # a just-in-time Uzum sync for this (shop, status), writes to
    # ``fbs_orders``, then reads back. Stale SWR entries are wiped
    # inside get_fbs_orders before the read.
    refresh = _refresh_requested()

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
                # Fast path: hit Uzum's ``/count`` endpoint for the 3
                # active-work chips (CREATED/PACKING/PENDING_DELIVERY).
                # /count returns a single integer per call (~200-500ms
                # multi-shop), vs draining /orders pages which was ~1-2s
                # per status. For the 8 non-active chips we just read
                # whatever the bg worker last wrote — sellers don't
                # watch them in real time.
                #
                # Multi-shop optimization preserved: one /count call
                # carries every shopId for the user's token, so an N-
                # shop admin still pays 3 Uzum calls (not 3N).
                from core.fbs_data import _refresh_shops_counts
                try:
                    fresh_counts = _refresh_shops_counts(
                        token, list(user_shops), _FBS_REFRESH_ON_PRESS_SYNC,
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
            "error": "Uzum OpenAPI token o'rnatilmagan. «Mening do'konlarim» (/fetch) sahifasidan tokenni kiriting."
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
            {"error": "Buyurtma topilmadi yoki ruxsat yo'q"}, 404
        ))
    with SessionLocal() as db:
        shop = db.execute(
            select(Shop).where(Shop.uzum_id == shop_uzum_id)
        ).scalar_one_or_none()
        if not shop or shop.id not in allowed_shop_db_ids:
            return (None, None, None, _json_response(
                {"error": "Buyurtma topilmadi yoki ruxsat yo'q"}, 404
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
        return _json_response({"error": "Bekor qilish sababini tanlang"}, 400)

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
            {"error": "Identifikator ro'yxati bo'sh"}, 400
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
        # Replace shipping label PDFs with product QR sticker(s) — one
        # per orderItem. Use the order detail's orderItems (already
        # fetched during _resolve_token_and_check_order).
        items = order.get("orderItems") if isinstance(order, dict) else []
        product_pdfs: list[bytes] = []
        for item in (items or []):
            qr_pdf = render_product_qr_pdf(item)
            if qr_pdf:
                product_pdfs.append(qr_pdf)
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
            {"error": "order_ids ro'yxati bo'sh yoki noto'g'ri"}, 400
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
            {"error": "order_ids ro'yxatida yaroqli ID yo'q"}, 400
        ))
    if len(ids) > _BULK_LIMIT:
        return (None, _json_response(
            {"error": f"Bir martada {_BULK_LIMIT} tadan ko'p buyurtma tanlash mumkin emas"}, 400
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
                "error": "Uzum OpenAPI token o'rnatilmagan. «Mening do'konlarim» (/fetch) sahifasidan tokenni kiriting."
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
            "error": f"Buyurtmalar topilmadi yoki sinxronlanmagan: {sample}"
            + ("…" if len(missing) > 3 else "")
        }, 404))

    rows: list[dict] = []
    for oid in order_ids:
        r = found[str(oid)]
        if r.shop_id not in owned_uzum_ids:
            return (None, _json_response({
                "error": f"Buyurtmaga ruxsat yo'q: №{oid}"
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
    # product_qr_size — picks the physical size of the product QR
    # sticker pages. In mode="qr" (standalone print) the seller's
    # choice rules — they're printing on a thermal-label roll of that
    # exact size. In modes that mix label+QR we OVERRIDE to "uzum" so
    # the merged PDF has consistent page sizes (otherwise the QR sticker
    # appears as a tiny 40×30mm island between two giant 242×166mm
    # shipping labels — visually wrong on screen and awkward to print
    # on A4 paper).
    product_qr_size = (payload.get("product_qr_size") or "").strip().lower() or None
    if mode in ("label_with_qr", "qr_with_label"):
        product_qr_size = "uzum"

    rows, err = _resolve_bulk_orders(ids)
    if err is not None:
        return err
    token = request.environ.get("_fbs_bulk_token") or ""

    pdfs: list[bytes] = []
    errors: list[dict] = []
    # Fan-out is parallel but burst-safe: each download_fbs_label call
    # reserves a per-token slot via the shared gate
    # (core.fbs_locks.pace_uzum_call, Bosqich A.8/#4), so N concurrent
    # labels interleave ~1s apart instead of bursting Uzum's 429 penalty.
    # The pool lets all threads reserve their slots up front (O(1) under a
    # brief lock) and then sleep to their slot — wall-clock ≈ (N-1)×1s,
    # safe not fast. The 5-min label cache short-circuits repeat clicks.
    with ThreadPoolExecutor(max_workers=min(5, len(rows))) as pool:
        future_to_row = {
            pool.submit(get_label_pdfs, token, r["order_id"], size=size, fail_fast=True): r
            for r in rows
        }
        for fut in as_completed(future_to_row):
            r = future_to_row[fut]
            try:
                order_pdfs, _ = fut.result()
                if order_pdfs:
                    if mode == "qr":
                        # Product QR per item — no shipping label. The
                        # API call above fetched the shipping PDF only
                        # to validate the order's printability (PACKING+
                        # status had a label), but we don't include it
                        # in the output for this mode.
                        for item in (r.get("items") or []):
                            qr_pdf = (
                                render_product_qr_pdf(item, size=product_qr_size)
                                if product_qr_size
                                else render_product_qr_pdf(item)
                            )
                            if qr_pdf:
                                pdfs.append(qr_pdf)
                    elif mode == "shipping_qr":
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
                        for item in (r.get("items") or []):
                            qr_pdf = (
                                render_product_qr_pdf(item, size=product_qr_size)
                                if product_qr_size
                                else render_product_qr_pdf(item)
                            )
                            if qr_pdf:
                                pdfs.append(qr_pdf)
                    elif mode == "qr_with_label":
                        # Reverse order: product QR stickers FIRST, then
                        # the shipping label. Some warehouses pick by QR
                        # before printing the shipping label, others do
                        # the opposite — we support both flows.
                        for item in (r.get("items") or []):
                            qr_pdf = (
                                render_product_qr_pdf(item, size=product_qr_size)
                                if product_qr_size
                                else render_product_qr_pdf(item)
                            )
                            if qr_pdf:
                                pdfs.append(qr_pdf)
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
            "error": f"Birorta yorliq olinmadi: {first_err}",
            "errors": errors,
        }, 502)

    merged = merge_label_pdfs(pdfs)
    if not merged:
        return _json_response({
            "error": "PDF birlashtirishda xato (pypdf merge)",
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
                "error": "Faqat CREATED holatdagi buyurtmalarni tasdiqlash mumkin",
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
                    msg = FBS_ERROR_MESSAGES_UZ.get(code) or e.message or f"Uzum xatosi (HTTP {e.http_status})"
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
    return _json_response({
        "ok": error_count == 0,
        "results": ordered,
        "success_count": success_count,
        "error_count": error_count,
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

    # 2-min in-memory cache — modal open + tab switches hit this without
    # re-running the DB GROUP BY or re-calling Uzum. Per-user keyed so
    # multi-tenant isolation is preserved.
    uid = int(current_user.get_id())
    cached = _dropoff_cache_get(uid, source)
    if cached is not None:
        return _json_response(cached)

    # ── Source = uzum: call Uzum's real endpoint ──────────────────────
    if source == "uzum":
        # Grab user's token (same token used for all their shops).
        with SessionLocal() as db:
            user = db.get(User, uid)
            token = (user.uzum_openapi_token or "").strip() if user else ""
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
                {"error": "Uzum OpenAPI token o'rnatilmagan"}, 400
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
                _dropoff_cache_put(uid, "uzum", payload)
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

    payload = {"ok": True, "source": "db", "points": points}
    _dropoff_cache_put(uid, "db", payload)
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
        return _json_response({"error": "Uzum OpenAPI token o'rnatilmagan"}, 400)
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
        return _json_response({"error": "order_ids majburiy"}, 400)
    if not dop_uuid:
        return _json_response({"error": "drop_off_point_uuid majburiy"}, 400)
    if not slot_uuid:
        return _json_response({"error": "time_slot_uuid majburiy"}, 400)

    # Scope check: all orders must belong to the user's shops, and they
    # must all share the SAME shop (Uzum invoice is per-shop — sellerId
    # is a single integer).
    user_shops = _current_user_shop_uzum_ids()
    if not user_shops:
        return _json_response({"error": "Sizda biron do'kon topilmadi"}, 403)

    from models import FbsOrder, User
    str_ids = [str(i) for i in order_ids]
    with SessionLocal() as db:
        rows = db.execute(
            select(FbsOrder.order_id, FbsOrder.shop_id, FbsOrder.status)
            .where(FbsOrder.order_id.in_(str_ids))
            .where(FbsOrder.shop_id.in_(user_shops))
        ).all()
    if len(rows) != len(set(str_ids)):
        return _json_response({"error": "Ba'zi buyurtmalar topilmadi yoki sizga tegishli emas"}, 403)

    shop_ids = {r.shop_id for r in rows}
    if len(shop_ids) > 1:
        return _json_response({
            "error": "Bitta накладная ichiga turli do'konlardan buyurtmalarni qo'shib bo'lmaydi"
        }, 400)
    # Verify orders are in a state that can be put on an invoice (PACKING).
    bad = [r.order_id for r in rows if r.status != "PACKING"]
    if bad:
        return _json_response({
            "error": f"Faqat «Yig'ilmoqda» (PACKING) buyurtmalar накладная ichiga qo'shiladi. Xato: {len(bad)} ta"
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
        return _json_response({"error": "Uzum OpenAPI token o'rnatilmagan"}, 400)
    if seller_id is None:
        return _json_response({
            "error": "Seller ID avtomatik aniqlanmadi (finance ma'lumoti hali yo'q yoki Uzum vaqtincha javob bermadi). Birozdan keyin qayta urinib ko'ring yoki «Mening do'konlarim» sahifasida ?sId=<N> ni kiriting."
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

    def _reflect_pending_delivery():
        # The orders just moved PACKING → PENDING_DELIVERY at Uzum. Reflect
        # it in our local cache NOW so the «К отправке» list shows them on
        # the next read — not only after a manual «Обновить». The count
        # endpoint reports live numbers, which is exactly why the chip
        # showed «1» while the DB-backed list was still empty. Mirrors the
        # cancel_order fast-path (optimistic UPDATE + cache invalidation);
        # the periodic sync reconciles later if Uzum's status differs.
        try:
            from sqlalchemy import update as _sql_update
            with SessionLocal() as _db:
                _db.execute(
                    _sql_update(FbsOrder)
                    .where(FbsOrder.order_id.in_(str_ids))
                    .where(FbsOrder.shop_id.in_(user_shops))
                    .where(FbsOrder.status == "PACKING")
                    .values(status="PENDING_DELIVERY")
                )
                _db.commit()
            for _sid in shop_ids:
                invalidate_fbs_cache(_sid)
        except Exception as _e:
            print(f"[invoice/create] local PENDING_DELIVERY reflect failed: {_e!r}")

    try:
        invoice, used_url = _do_call(update_only=False)
        _reflect_pending_delivery()
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
                _reflect_pending_delivery()
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
        return _json_response({"error": "Uzum OpenAPI token o'rnatilmagan"}, 400)

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

    return _json_response({
        "ok": True,
        "invoices": invoices,
        "page": page,
        "size": size,
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
        return _json_response({"error": "Uzum OpenAPI token o'rnatilmagan"}, 400)

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
    try:
        from core.uzum_openapi import fetch_fbs_invoice_orders
        inv_orders, _ = fetch_fbs_invoice_orders(token, invoice_id, fail_fast=True)
    except Exception as e:
        print(f"[invoices/detail] authoritative orders join failed: {e!r}")

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
            items_compact.append({
                "title": it.get("title") or it.get("productTitle") or "",
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
        return _json_response({"error": "point_uuid majburiy"}, 400)
    if not slot_uuid:
        return _json_response({"error": "slot_uuid majburiy"}, 400)

    user_shops = _current_user_shop_uzum_ids()
    if not user_shops:
        return _json_response({"error": "Sizda biron do'kon topilmadi"}, 403)

    uid = int(current_user.get_id())
    with SessionLocal() as db:
        user = db.get(User, uid)
        token = (user.uzum_openapi_token or "").strip() if user else ""
        # sellerId is auto-detected from finance + persisted (see
        # _ensure_seller_id) — the seller never pastes it manually.
        seller_id = _ensure_seller_id(db, user, token, user_shops)
    if not token:
        return _json_response({"error": "Uzum OpenAPI token o'rnatilmagan"}, 400)
    if seller_id is None:
        return _json_response({
            "error": "Seller ID avtomatik aniqlanmadi (finance ma'lumoti hali yo'q). «Mening do'konlarim» sahifasida Uzum kabinet URL'idagi ?sId=<N> qiymatini kiriting."
        }, 400)

    # The orders attached to this invoice come straight from Uzum — the
    # authoritative membership (GET /v1/fbs/invoice/{id}/orders). The old
    # `invoice_number LIKE '%suffix'` DB-match was fragile (suffix
    # collisions) and empty before the sync worker populated the column.
    # Uzum scopes the call to the token owner, so a cross-seller invoice
    # is rejected (403) Uzum-side — no local shop filter needed here.
    from core.uzum_openapi import change_invoice_pickup
    try:
        invoice, used_url = change_invoice_pickup(
            token,
            invoice_id=invoice_id,
            seller_id=seller_id,
            drop_off_point_uuid=point_uuid,
            time_slot_uuid=slot_uuid,
            fail_fast=True,  # interactive mutation — fail fast on a burst-429
        )
    except ValueError:
        # Uzum reports no orders on this invoice — nothing to move.
        return _json_response({
            "error": "Bu накладная uchun buyurtmalar topilmadi (yoki sizga tegishli emas)"
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
    from core.fbs_akt_cache import get_or_fetch_akt

    uid = int(current_user.get_id())
    with SessionLocal() as db:
        user = db.get(User, uid)
        token = (user.uzum_openapi_token or "").strip() if user else ""
    if not token:
        return _json_response({"error": "Uzum OpenAPI token o'rnatilmagan"}, 400)

    # Cache-first: the background worker usually has this akt pre-fetched, so
    # this serves straight from the DB (0 Uzum calls). On a miss it fetches
    # live (paced + 429-retry) and stores for next time.
    try:
        pdf_bytes = get_or_fetch_akt(token, uid, invoice_id)
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
    from core.fbs_akt_cache import get_or_fetch_akt

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
        return _json_response({"error": "Накладной tanlanmadi"}, 400)
    if len(ids) > _BULK_AKT_MAX:
        return _json_response(
            {"error": f"Bir vaqtda {_BULK_AKT_MAX} tagacha akt birlashtiriladi. "
                      f"Kamroq tanlang."},
            400,
        )

    uid = int(current_user.get_id())
    with SessionLocal() as db:
        user = db.get(User, uid)
        token = (user.uzum_openapi_token or "").strip() if user else ""
    if not token:
        return _json_response({"error": "Uzum OpenAPI token o'rnatilmagan"}, 400)

    writer = PdfWriter()
    merged = 0
    failed: list[int] = []
    for iid in ids:
        try:
            pdf_bytes = get_or_fetch_akt(token, uid, iid)
            reader = PdfReader(BytesIO(pdf_bytes))
            for page in reader.pages:
                writer.add_page(page)
            merged += 1
        except Exception as e:
            print(f"[invoices/akt-merged] skip invoice {iid}: {e!r}")
            failed.append(iid)

    if merged == 0:
        return _json_response(
            {"error": "Hech bir akt yuklanmadi. Birozdan keyin urinib ko'ring."},
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
    return render_template(
        "fbs_stock.html",
        title="FBS — ombor",
        has_token=bool(token),
    )


# ── Shop-scoping for the SELLER-level stock list ───────────────────────
# ``GET /v2/fbs/sku/stocks`` takes NO shopId — it returns every SKU under
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


@fbs_bp.get("/fbs/api/sku-stocks")
@login_required
def fbs_sku_stocks_list_api():
    """GET: the seller's updatable FBS/DBS SKU stocks.

    The official ``/v2/fbs/sku/stocks`` carries no product image, so we
    enrich each row from our local ``Variant`` table by skuId. Needs
    ``SKU_READ`` on the token (else Uzum 403).
    """
    from core.uzum_openapi import fetch_fbs_sku_stocks

    uid = int(current_user.get_id())
    with SessionLocal() as db:
        user = db.get(User, uid)
        token = (user.uzum_openapi_token or "").strip() if user else ""
    if not token:
        return _json_response({"error": "Uzum OpenAPI token o'rnatilmagan"}, 400)

    try:
        skus, used_url = fetch_fbs_sku_stocks(token, fail_fast=True)
    except UzumAPIError as e:
        return _uzum_error_response(e)
    except Exception as e:
        print(f"[sku-stocks/list] unexpected error: {e!r}")
        return _json_response({"error": str(e)[:200]}, 502)

    # Enrich each SKU with its local product image + owning shop, joined by
    # skuId (Variant.uzum_sku_id is VARCHAR → coerce the int skuId to str),
    # then DROP every SKU that isn't one of the user's own registered shops.
    # The official stock API is SELLER-level (no shopId), so it also returns
    # SKUs from Uzum shops the seller never added to SellerHub — those must
    # not appear in the «Ombor» grid (or the «Do'kon» filter).
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

        # Keep only the user's own shops, then attach image + shop name.
        skus, hidden = _filter_stock_skus_to_shops(skus, shop_by_sku, user_shop_db_ids)
        for s in skus:
            key = str(s.get("skuId"))
            s["image"] = img_by_sku.get(key)
            shop_id = shop_by_sku.get(key)
            s["shopId"] = shop_id
            s["shopName"] = shop_name_by_id.get(shop_id) if shop_id is not None else None
    except Exception as e:
        print(f"[sku-stocks/list] image/shop enrich failed: {e!r}")

    print(f"[sku-stocks/list] user_id={uid} shown={len(skus)} hidden_foreign={hidden}", flush=True)
    return _json_response({
        "ok": True, "skus": skus, "shops": shops_out,
        "hidden_foreign": hidden, "used_url": used_url,
    })


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
        return _json_response({"error": "Yangilash uchun SKU yuborilmadi"}, 400)

    uid = int(current_user.get_id())
    with SessionLocal() as db:
        user = db.get(User, uid)
        token = (user.uzum_openapi_token or "").strip() if user else ""
    if not token:
        return _json_response({"error": "Uzum OpenAPI token o'rnatilmagan"}, 400)

    try:
        result, used_url = update_fbs_sku_stocks(token, sku_amounts=items, fail_fast=True)
    except ValueError:
        return _json_response({"error": "Yangilash uchun yaroqli SKU topilmadi"}, 400)
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
        return _json_response({"error": "Uzum OpenAPI token o'rnatilmagan"}, 400)

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
        return _json_response({"error": "Fayl yuborilmadi"}, 400)
    if not f.filename.lower().endswith((".xlsx", ".xlsm")):
        return _json_response({"error": "Faqat .xlsx fayl qabul qilinadi"}, 400)

    uid = int(current_user.get_id())
    with SessionLocal() as db:
        user = db.get(User, uid)
        token = (user.uzum_openapi_token or "").strip() if user else ""
    if not token:
        return _json_response({"error": "Uzum OpenAPI token o'rnatilmagan"}, 400)

    try:
        desired, stats = parse_stock_rows(f.read())
    except Exception as e:
        print(f"[sku-stocks/import-preview] parse error: {e!r}")
        return _json_response({"error": "Faylni o'qib bo'lmadi — format noto'g'ri"}, 400)
    if not desired:
        return _json_response({"error": "Faylda yaroqli qator topilmadi", "stats": stats}, 400)

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
        return _json_response({"error": "Saqlash uchun o'zgarish yo'q"}, 400)

    uid = int(current_user.get_id())
    with SessionLocal() as db:
        user = db.get(User, uid)
        token = (user.uzum_openapi_token or "").strip() if user else ""
    if not token:
        return _json_response({"error": "Uzum OpenAPI token o'rnatilmagan"}, 400)

    applied = 0
    last_url = ""
    try:
        for i in range(0, len(changes), _IMPORT_CHUNK):
            chunk = changes[i:i + _IMPORT_CHUNK]
            _, last_url = update_fbs_sku_stocks(token, sku_amounts=chunk, fail_fast=True)
            applied += len(chunk)
    except ValueError:
        return _json_response({"error": "Yangilash uchun yaroqli SKU topilmadi"}, 400)
    except UzumAPIError as e:
        return _uzum_error_response(e)
    except Exception as e:
        print(f"[sku-stocks/import-apply] unexpected error: {e!r}")
        return _json_response({"error": str(e)[:200], "applied": applied}, 502)

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
            {"error": "Uzum OpenAPI token o'rnatilmagan"}, 400
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
                {"error": "Tasdiqlash kodi raqam bo'lishi kerak"}, 400
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
