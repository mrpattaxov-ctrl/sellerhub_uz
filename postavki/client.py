"""Postavki (FBO ombor to'ldirish) — Uzum ichki portal API o'rami.

Bu modul Uzum «Поставки» (FBO накладной) ma'lumotlarini ichki portal
API'dan o'qiydi. Auth = browser token (admin `api_key`), `_get_admin_token`
orqali — OpenAPI token EMAS (FBO yaratish/o'qish faqat portal API'da bor).

Faza 1 — faqat O'QISH (hech narsa yaratmaydi):
  - list_invoices()      → поставкаlar ro'yxati
  - dimensional_groups() → mavjud o'lcham guruhlari (МГТ/СГТ)
  - restock_skus()       → yetkazsa bo'ladigan SKU'lar (+ Uzum tavsiyasi)

Yaratish (sku/stocks → time-slot → create) Faza 2'da shu yerga qo'shiladi.
"""
from __future__ import annotations

import base64
import io
import json
import os
import threading
import time
from urllib.parse import quote

from config import HTTP_ACCEPT_LANGUAGE, HTTP_USER_AGENT
from core.auth_helpers import _get_admin_token
from core.http_client import _get_http_session, http_json

_BASE = "https://api-seller.uzum.uz/api/seller/shop"

# Time-slot qidiruv oynasi: portal frontendi qo'shadigan ANIQ konstanta —
# 31 532 400 000 ms (= 365 kun − 1 soat = 8759 soat). 4 ta ishlaydigan HAR
# («Поставки1/2/3/10+») da timeTo−timeFrom har doim shu qiymat. Toza 365 kun
# (31 536 000 000) ishlatilsa, timeTo Uzum ruxsat etgan ufqdan ~1 soat oshib
# ketadi va `validation-failed-001` qaytadi. Aynan moslab qo'yamiz.
_SLOT_WINDOW_MS = 31_532_400_000

# `timeFrom` Uzum serverining HOZIRGI vaqtidan KELAJAKDA bo'lishi SHART —
# ~2026-06-18'da qo'shilgan validatsiya. `now` yuborilsa, so'rov Uzumga yetib
# borguncha (tarmoq kechikishi) u o'tmishga aylanadi → `validation-failed-001`.
# 5 daqiqalik zaxira kechikish + soat-skewни qoplaydi; FBO slotlari kunlab
# oldinda bo'lgani uchun bironta real slotni o'tkazib yubormaydi. (Probe
# 2026-06-19: now→400, now+1soat/now+1kun/keyingi-yarimtun→200.)
_TIMEFROM_LEAD_MS = 5 * 60 * 1000


def _get(url: str) -> dict | list:
    """GET portal API with the admin browser token auto-injected."""
    return http_json(url, method="GET", _get_admin_token=_get_admin_token)


def _post(url: str, body: dict) -> dict | list:
    """POST portal API with the admin browser token auto-injected."""
    return http_json(url, method="POST", body=body, _get_admin_token=_get_admin_token)


# ── TEZ-O'QISH yo'li (slot-detektor hot-loop uchun) ──────────────────
# Muammo: standart `http_json` 60s timeout + 3× retry ishlatadi → sovuq
# ulanish osilib qolsa ~90s qotadi (2026-07-08 o'lchovi). Detektorga esa
# ISSIQ (keep-alive, doimiy) + retry'siz + qisqa timeout kerak: sekin so'rov
# (straggler) 600ms da TASHLANSIN, o'sha sikl jimgina o'tkazib yuborilsin.
#
# ⚠️ Bu FAQAT `enable_fast_read()` chaqirgan THREAD'ga ta'sir qiladi (detektor
# warm-pool thread'lari). UI/yaratish oqimi (boshqa thread'lar) o'zgarmaydi —
# ular eski `_post` (retry + 60s) yo'lida qoladi. Thread-local izolyatsiya.
_fast_flag = threading.local()   # .timeout (float) — shu thread tez-o'qishda
_fast_sess = threading.local()   # .s (requests.Session) — issiq, retry'siz


def enable_fast_read(timeout_sec: float) -> None:
    """Mark THE CALLING thread to use a warm, no-retry, short-timeout session
    for `get_time_slots`. Used as a ThreadPoolExecutor initializer so the
    detector's persistent warm threads all read this way; every other thread
    (UI, create flow) keeps the normal retrying `http_json` path."""
    _fast_flag.timeout = float(timeout_sec)


def _fast_read_timeout():
    return getattr(_fast_flag, "timeout", None)


def _get_fast_session():
    """Per-thread keep-alive session, NO retry (a straggler must be abandoned,
    not retried). Persistent worker threads → connection stays warm across
    bursts (~181ms warm vs ~409ms cold)."""
    s = getattr(_fast_sess, "s", None)
    if s is None:
        import requests
        from requests.adapters import HTTPAdapter
        s = requests.Session()
        ad = HTTPAdapter(pool_connections=2, pool_maxsize=2, max_retries=0)
        s.mount("https://", ad)
        s.mount("http://", ad)
        _fast_sess.s = s
    return s


def _post_fast(url: str, body: dict, timeout: float) -> dict:
    """Warm, no-retry POST with a (connect, read) timeout. Read-timeout →
    requests.Timeout raised (caller treats as a skippable straggler); HTTP
    4xx/5xx → RuntimeError (caller treats as a real error / cooldown)."""
    req_headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": HTTP_USER_AGENT,
        "Accept-Language": HTTP_ACCEPT_LANGUAGE,
        "Content-Type": "application/json",
    }
    try:
        extra = os.getenv("HTTP_EXTRA_HEADERS_JSON", "").strip()
        if extra:
            req_headers.update(json.loads(extra))
    except Exception:
        pass
    try:
        tok = (_get_admin_token() or "").strip()
        if tok:
            req_headers["Authorization"] = tok if tok.startswith("Bearer ") else f"Bearer {tok}"
    except Exception:
        pass
    sess = _get_fast_session()
    resp = sess.post(url, headers=req_headers, json=body, timeout=(3.0, float(timeout)))
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {(resp.text or '')[:200]}")
    raw = resp.text or ""
    return json.loads(raw) if raw.strip() else {}


def list_invoices(
    shop_uzum_id: str, page: int = 0, size: int = 20, statuses: str = ""
) -> list[dict]:
    """FBO поставкаlar ro'yxati (bir do'kon, sahifalangan). Javob — massiv.

    ``statuses`` — vergul bilan ajratilgan filtr (HAR «Поставки12+» tasdiqladi):
    bo'sh → CREATED+ACCEPTED (default, bekor qilingansiz); ``CREATED`` /
    ``ACCEPTED`` / ``CANCELED`` (bir «L») — aniq status. Bekor qilingan
    поставкаlar FAQAT ``statuses=CANCELED`` bilan keladi.

    Har element: invoiceNumber, dateCreated, invoiceStatus{value,text,color},
    fullPrice, timeSlotReservation, totalAccepted/totalToStock, stock{...}.
    """
    url = f"{_BASE}/{shop_uzum_id}/invoice?page={int(page)}&size={int(size)}"
    if statuses:
        url += f"&statuses={statuses}"
    data = _get(url)
    return data if isinstance(data, list) else []


def dimensional_groups(shop_uzum_id: str) -> list[dict]:
    """Mavjud o'lcham guruhlari, masalan [{group:SMALL,title:МГТ}, ...]."""
    url = f"{_BASE}/{shop_uzum_id}/v2/invoice/dimensional-groups"
    data = _get(url)
    if isinstance(data, dict):
        return ((data.get("payload") or {}).get("dimensionalGroups")) or []
    return []


def restock_skus(
    shop_uzum_id: str,
    page: int = 0,
    size: int = 20,
    search: str = "",
    groups: str = "SMALL,MEDIUM",
) -> list[dict]:
    """Yetkazib berish mumkin bo'lgan SKU'lar (sahifalangan).

    Har element: skuId, productTitle, skuTitle, barcode, image/imageHigh,
    availableToStock, quantityToStock, quantityActive, purchasePrice,
    recommendQty (Uzum tavsiyasi), forecastOutOfStock, dimensionalGroup.
    """
    url = (
        f"{_BASE}/{shop_uzum_id}/v2/invoice/sku-list"
        f"?page={int(page)}&size={int(size)}&search={quote(search, safe='')}&dimensionalGroups={groups}"
    )
    data = _get(url)
    if isinstance(data, dict):
        return data.get("skuList") or []
    return []


# ── Yaratish oqimi (Faza 2) — REAL поставка yaratadi ─────────────────


def resolve_stocks(shop_uzum_id: str, sku_ids: list[int]) -> list[dict]:
    """Tanlangan SKU'lar qaysi omborga borishini aniqlaydi.

    POST /sku/stocks body {"skuList":[{"skuId":N},...]}
    → [{id, externalId, title, poolSource, dimensionalGroups}, ...]
    """
    url = f"{_BASE}/{shop_uzum_id}/v2/invoice/sku/stocks"
    body = {"skuList": [{"skuId": int(s)} for s in sku_ids]}
    data = _post(url, body)
    if isinstance(data, dict):
        return ((data.get("payload") or {}).get("stocks")) or []
    return []


def get_time_slots(
    shop_uzum_id: str,
    sku_lines: list[dict],
    pool_source: str,
    time_from: int | None = None,
    time_to: int | None = None,
) -> list[dict]:
    """Bo'sh vaqt-slotlar ro'yxati (qidiruv oynasi ichida).

    POST /time-slot body {skuList:[{skuId,purchasePrice,quantityToStock}],
    poolSource, timeFrom, timeTo} → [{timeFrom, timeTo}(epoch ms), ...]
    """
    now = int(time.time() * 1000)
    # timeFrom KELAJAKDA bo'lishi shart (Uzum validatsiyasi) — zaxira qo'shamiz.
    tf = int(time_from) if time_from else now + _TIMEFROM_LEAD_MS
    tt = int(time_to) if time_to else now + _SLOT_WINDOW_MS
    url = f"{_BASE}/{shop_uzum_id}/v2/invoice/time-slot"
    body = {
        "skuList": sku_lines,
        "poolSource": pool_source,
        "timeFrom": tf,
        "timeTo": tt,
    }
    # Detektor warm-pool thread'lari → tez-o'qish (issiq, retry'siz, 600ms).
    # Boshqa hamma (UI/yaratish) → eski `_post` (retry + 60s). Farqi thread-local.
    _to = _fast_read_timeout()
    data = _post_fast(url, body, _to) if _to is not None else _post(url, body)
    if isinstance(data, dict):
        return ((data.get("payload") or {}).get("timeSlots")) or []
    return []


def create_invoice(
    shop_uzum_id: str,
    sku_lines: list[dict],
    time_from: int | None,
    stock_id: int,
) -> dict:
    """поставкаni YARATADI (REAL). 201 → yaratilgan invoice obyekti.

    POST /create body {skuList:[{skuId,purchasePrice,quantityToStock}],
    [timeFrom:<slot ms>], stockId}. ``timeFrom`` IXTIYORIY — berilmasa
    поставка slotsiz yaratiladi («Выбрать позже»; slot keyin time-slot/set
    bilan biriktiriladi). HAR «Поставки10+» tasdiqladi.
    """
    url = f"{_BASE}/{shop_uzum_id}/v2/invoice/create"
    body = {"skuList": sku_lines, "stockId": int(stock_id)}
    if time_from:
        body["timeFrom"] = int(time_from)
    data = _post(url, body)
    return data if isinstance(data, dict) else {}


# ── Mavjud поставкани o'zgartirish/bekor qilish (Faza 3) ─────────────


def invoice_time_slots(
    shop_uzum_id: str,
    invoice_id: int,
    pool_source: str,
    time_from: int | None = None,
) -> list[dict]:
    """Mavjud поставка uchun BOSHQA bo'sh slotlar (o'zgartirish uchun).

    POST /time-slot/get body {invoiceIds:[id], poolSource, timeFrom:<now>}
    → [{timeFrom, timeTo}(epoch ms), ...]
    """
    # timeFrom KELAJAKDA bo'lishi shart (Uzum validatsiyasi, time-slot bilan bir xil).
    tf = int(time_from) if time_from else int(time.time() * 1000) + _TIMEFROM_LEAD_MS
    url = f"{_BASE}/{shop_uzum_id}/v2/invoice/time-slot/get"
    body = {"invoiceIds": [int(invoice_id)], "poolSource": pool_source, "timeFrom": tf}
    data = _post(url, body)
    if isinstance(data, dict):
        return ((data.get("payload") or {}).get("timeSlots")) or []
    return []


def set_time_slot(
    shop_uzum_id: str,
    invoice_id: int,
    time_from: int,
    stock_id: int,
    pool_source: str,
) -> dict:
    """Mavjud поставка slotini O'ZGARTIRADI.

    POST /time-slot/set body {timeFrom:<yangi ms>, invoiceIds:[id],
    stockId, poolSource} → payload:[<yangilangan invoice>]. Birinchisini qaytaradi.
    """
    url = f"{_BASE}/{shop_uzum_id}/v2/invoice/time-slot/set"
    body = {
        "timeFrom": int(time_from),
        "invoiceIds": [int(invoice_id)],
        "stockId": int(stock_id),
        "poolSource": pool_source,
    }
    data = _post(url, body)
    if isinstance(data, dict):
        payload = data.get("payload")
        if isinstance(payload, list) and payload:
            return payload[0]
    return {}


def cancel_invoice(shop_uzum_id: str, invoice_id: int) -> bool:
    """поставкаni BEKOR QILADI. POST /cancelInvoice body {id} → bo'sh 200."""
    url = f"{_BASE}/{shop_uzum_id}/invoice/cancelInvoice"
    _post(url, {"id": int(invoice_id)})
    return True


# ── Hujjatlar (PDF) — Акт отправки + QR/штрихкод yorliqlari ──────────


def _auth_headers(accept: str) -> dict:
    """Portal so'rovi uchun browser-token bilan header (PDF baytlari uchun)."""
    h = {
        "User-Agent": HTTP_USER_AGENT,
        "Accept-Language": HTTP_ACCEPT_LANGUAGE,
        "Accept": accept,
    }
    tok = _get_admin_token()
    if tok:
        h["Authorization"] = tok if tok.startswith("Bearer ") else f"Bearer {tok}"
    return h


def print_invoice_act(shop_uzum_id: str, invoice_id: int) -> bytes:
    """«Акт отправки» PDF (yetkazib berish akti).

    GET /invoice/printInvoice?invoiceId={id} → ``{"pdf":"<base64>"}``.
    base64 dekodlanib xom PDF baytlari qaytadi. Hech narsa o'zgartirmaydi.
    """
    url = f"{_BASE}/{shop_uzum_id}/invoice/printInvoice?invoiceId={int(invoice_id)}"
    data = _get(url)
    b64 = data.get("pdf") if isinstance(data, dict) else None
    return base64.b64decode(b64) if b64 else b""


def merge_pdfs(pdf_bytes_list: list[bytes]) -> bytes:
    """Bir nechta akt PDF'ini bitta yuklab olinadigan faylga birlashtiradi.

    pypdf bilan; har bir kirish bir akt (ko'p sahifali bo'lishi mumkin) —
    barcha sahifalar saqlanadi. Bo'sh/None elementlar jim o'tkazib yuboriladi.
    Hammasi bo'sh/buzuq bo'lsa ``b""`` qaytaradi (route uni 502 qiladi).
    """
    try:
        from pypdf import PdfWriter, PdfReader  # lazy — import-vaqt sikldan saqlanish
    except ImportError:
        print("[postavki] pypdf o'rnatilmagan — bulk akt birlashtirish ishlamaydi")
        return b""
    writer = PdfWriter()
    appended = 0
    for raw in pdf_bytes_list:
        if not raw:
            continue
        try:
            reader = PdfReader(io.BytesIO(raw))
            for page in reader.pages:
                writer.add_page(page)
            appended += 1
        except Exception as e:
            print(f"[postavki] merge: 1 PDF o'tkazib yuborildi ({len(raw)} bayt): {e!r}")
            continue
    if appended == 0:
        return b""
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def print_barcodes(
    shop_uzum_id: str, lines: list[dict], barcode_type: int = 5
) -> bytes:
    """«QR-коды» / штрихкод yorliqlari PDF (har birlik uchun yorliq).

    POST /products/v3/barcodes/print body
    ``{"data":[{"barcodeTypeId":<type>,"skuId":N,"amount":N},...]}`` →
    xom ``application/pdf``. ``barcode_type``: 1=штрихкод, 5=QR
    (HAR «Поставки13+» tasdiqladi). ``lines`` har biri {skuId, amount};
    amount — поставка rejasi (quantityToStock). O'zgartirmaydi.
    """
    url = f"{_BASE}/{shop_uzum_id}/products/v3/barcodes/print"
    body = {
        "data": [
            {
                "barcodeTypeId": int(barcode_type),
                "skuId": int(l["skuId"]),
                "amount": int(l["amount"]),
            }
            for l in lines
            if l.get("skuId") and int(l.get("amount") or 0) > 0
        ]
    }
    sess = _get_http_session()
    resp = sess.request(
        "POST",
        url,
        json=body,
        headers=_auth_headers("application/pdf, */*"),
        timeout=60,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {(resp.text or '')[:200]}")
    return resp.content


# ── Detal + Возвраты (o'qish) ────────────────────────────────────────


def invoice_detail(shop_uzum_id: str, invoice_id: int) -> dict:
    """Bitta поставка to'liq ma'lumoti. GET /invoice/{id}."""
    url = f"{_BASE}/{shop_uzum_id}/invoice/{int(invoice_id)}"
    data = _get(url)
    return data if isinstance(data, dict) else {}


def invoice_products(shop_uzum_id: str, invoice_id: int) -> list[dict]:
    """поставка tarkibi (mahsulotlar, productga guruhlangan).

    GET /invoice/getInvoiceProducts?invoiceId={id} → [{id, productTitle,
    skuTitle, quantityToStock, quantityAccepted, purchasePrice,
    skuForInvoiceDtoList:[{id, skuTitle, quantityToStock, quantityAccepted,
    purchasePrice, photo}]}]
    """
    url = f"{_BASE}/{shop_uzum_id}/invoice/getInvoiceProducts?invoiceId={int(invoice_id)}"
    data = _get(url)
    return data if isinstance(data, list) else []


def list_returns(
    shop_uzum_id: str,
    page: int = 0,
    size: int = 20,
    statuses: str = "",
    types: str = "",
    number_filter: str = "",
) -> list[dict]:
    """Возвраты (qaytarish накладнойlari) ro'yxati. GET /return → {payload:[...]}.

    Filtrlar (HAR «Поставки20+» tasdiqladi, ``ids`` PARALLEL emas — query):
      ``statuses`` — vergul ro'yxati: CREATED, SENT, IN_PROGRESS,
        MOVED_TO_DELIVERY, ASSEMBLED, COMPLETED, UTILIZED, CANCELED.
      ``types``    — vergul ro'yxati: RETURN, DEFECTED, FBS.
      ``number_filter`` — nomer bo'yicha qidiruv (returnNumberFilter).

    Har element: id, dateCreated(ms), status, type, stock{title}, executionDate,
    assembledDate, completedDate, canceledDate, paidStorage{startDate,endDate,
    status}, ettnInfo, returnDropInfo, timeSlotReservation, externalNumber.
    """
    url = f"{_BASE}/{shop_uzum_id}/return?page={int(page)}&size={int(size)}"
    if statuses:
        url += f"&statuses={statuses}"
    if types:
        url += f"&types={types}"
    if number_filter:
        url += f"&returnNumberFilter={quote(str(number_filter), safe='')}"
    data = _get(url)
    if isinstance(data, dict):
        return data.get("payload") or []
    return data if isinstance(data, list) else []


def return_detail(shop_uzum_id: str, return_id: int) -> dict:
    """Bitta возврат to'liq ma'lumoti. GET /return/{id} → {payload:{...}}.

    ``payload`` qo'shimcha: ``returnItems`` [{id, skuId, amount, packedAmount,
    skuTitle, productTitle, purchasePrice, photo{...}}], ``totalAmount``,
    ``totalPackedAmount``, ``countAllowedChange``, ``maxCountAllowedChange``.
    """
    url = f"{_BASE}/{shop_uzum_id}/return/{int(return_id)}"
    data = _get(url)
    if isinstance(data, dict):
        return data.get("payload") or {}
    return {}


def return_sku_source(
    shop_uzum_id: str, page: int = 0, size: int = 20, filter_q: str = ""
) -> dict:
    """Возврат yaratish uchun qaytsa bo'ladigan SKU'lar (FBO ombordagi qoldiq).

    GET /product/stock-sku?page&size → TOP-LEVEL ``{quantitySku:N, skuList:[<to'liq
    sku DTO>]}`` (live probe 2026-06-24 tasdiqladi — ``payload`` o'rami YO'Q). Har
    sku DTO create POST'ga AYNAN qaytariladi (+ ``amount``) — portal shunday qiladi.
    ``{items:[...], total:N}`` qaytaradi. Boshqa o'ramlar (payload.content / list)
    ham himoyalanган — portal kelajakda o'zgartirса yiqilmaymiz.

    ``filter_q`` berilsa server-side qidiruv (``&filter=<q>`` — HAR «Поставки21+»
    tasdiqladi: nom/SKU bo'yicha qidiradi). Bo'sh bo'lsa to'liq katalog (sahifali).
    """
    url = f"{_BASE}/{shop_uzum_id}/product/stock-sku?page={int(page)}&size={int(size)}"
    if filter_q:
        url += f"&filter={quote(str(filter_q), safe='')}"
    data = _get(url)
    if isinstance(data, dict):
        # Asosiy holat: top-level {quantitySku, skuList}
        if "skuList" in data:
            items = data.get("skuList") or []
            total = data.get("quantitySku")
            return {"items": items, "total": int(total if total is not None else len(items))}
        # Zaxira: payload o'rami (Spring Page yoki tekis massiv)
        payload = data.get("payload")
        if isinstance(payload, dict):
            items = payload.get("content") or payload.get("skuList") or payload.get("items") or []
            total = payload.get("totalElements")
            return {"items": items, "total": int(total if total is not None else len(items))}
        if isinstance(payload, list):
            return {"items": payload, "total": len(payload)}
    if isinstance(data, list):
        return {"items": data, "total": len(data)}
    return {"items": [], "total": 0}


def create_return(shop_uzum_id: str, return_items: list[dict]) -> dict:
    """Возврат YARATADI (REAL). ⚠️ Uzum'da haqiqiy qaytarish накладной ochadi.

    POST /return body ``{"returnItems":[<to'liq sku DTO + amount>, ...]}``. Portal
    stock-sku'dan olingan sku obyektini O'ZGARTIRMASDAN qaytaradi, faqat
    ``amount`` (qaytariladigan miqdor) qo'shadi — biz ham aynan shunday qilamiz
    (server faqat skuId+amount o'qishi mumkin, lekin to'liq echo xavfsizroq).
    Javob: {payload:{id, status:CREATED, returnItems:[...], totalAmount, ...}}.
    """
    url = f"{_BASE}/{shop_uzum_id}/return"
    data = _post(url, {"returnItems": return_items})
    if isinstance(data, dict):
        return data.get("payload") or {}
    return {}


def cancel_return(shop_uzum_id: str, return_id: int) -> dict:
    """Возвратни BEKOR QILADI. ⚠️ Uzum'da haqiqiy bekor qiladi.

    POST /return/{id}/cancel (bo'sh body) → {payload:{...status:CANCELED}}.
    Faqat CREATED возврат bekor qilinadi (portal shunday cheklaydi).
    """
    url = f"{_BASE}/{shop_uzum_id}/return/{int(return_id)}/cancel"
    data = _post(url, {})
    if isinstance(data, dict):
        return data.get("payload") or {}
    return {}
