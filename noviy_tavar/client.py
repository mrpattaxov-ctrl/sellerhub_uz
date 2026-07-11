"""«Новый товар» — Uzum ichki portal API o'rami (mahsulot yaratish oqimi).

Barcha endpointlar tavarsozdat.har (2026-07-11) dan olingan — portal
frontend'i «Создать карточку» sehrgarida chaqiradigan aynan o'sha yo'llar.
Auth = browser token (admin `api_key`), postavki/client.py bilan bir xil
`Authorization: Bearer <token>` sxemasi. OpenAPI token EMAS — mahsulot
yaratish faqat portal API'da bor (seller-openapi'da bunday endpoint yo'q).

Oqim (HAR tartibida):
  1. root_categories / child_categories  — kategoriya kaskadi
  2. category_meta                       — kategoriya qoidalari (6 ta GET birlashtirilgan)
  3. filter_values                       — Brend/Model/Davlat qiymatlari (qidiruvli, 24/sahifa)
  4. check_words                         — sarlavha/tavsif taqiqlangan-so'z tekshiruvi
  5. upload_image                        — images-uploader.uzum.uz/upload (multipart)
  6. create_product                      — POST .../product/createProduct?testVariant=B → 201 QORALAMA
  7. check_sku                           — SKU artikul bandligini tekshirish (kelajak bosqich)

createProduct QORALAMA yaratadi — moderatsiyaga O'ZI KETMAYDI (HAR'da
tasdiqlangan: skuList=[] bilan 201, keyin karta kabinetda «to'ldirilmagan»
holatda turadi). SKU/narx + moderatsiya bosqichi alohida HAR kutmoqda.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from config import HTTP_ACCEPT_LANGUAGE, HTTP_USER_AGENT
from core.auth_helpers import _get_admin_token
from core.http_client import _get_http_session

_BASE = "https://api-seller.uzum.uz/api/seller/shop"
_UPLOAD_URL = "https://images-uploader.uzum.uz/upload"

# Rasm hajmi cheklovi (proxy orqali o'tadigan multipart) — portal o'zi ham
# ~10MB atrofida cheklaydi; biz 15MB da kesamiz (xotira himoyasi).
MAX_IMAGE_BYTES = 15 * 1024 * 1024


class NoviyTavarError(RuntimeError):
    """Portal chaqiruvi xatosi — HTTP status + qisqa body bilan."""

    def __init__(self, http_status: int, body: str, url: str):
        self.http_status = http_status
        self.body = (body or "")[:500]
        self.url = url
        super().__init__(f"portal {url} -> HTTP {http_status}: {self.body[:200]}")


def _headers(content_type: str | None = None) -> dict:
    """Portal so'rovi header'lari — postavki bilan bir xil Bearer sxema."""
    h = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": HTTP_USER_AGENT,
        "Accept-Language": HTTP_ACCEPT_LANGUAGE,
    }
    if content_type:
        h["Content-Type"] = content_type
    tok = (_get_admin_token() or "").strip()
    if tok:
        h["Authorization"] = tok if tok.startswith("Bearer ") else f"Bearer {tok}"
    return h


def _req(method: str, url: str, *, json_body=None, timeout: int = 30):
    """Bitta portal JSON so'rovi. ``json_body`` dict/list/str bo'lishi mumkin
    (check-words JSON-string yuboradi: ``"matn"``). Non-2xx → NoviyTavarError.
    """
    sess = _get_http_session()
    data = None
    headers = _headers("application/json" if json_body is not None else None)
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
    resp = sess.request(method, url, headers=headers, data=data, timeout=timeout)
    text = resp.text or ""
    if not (200 <= resp.status_code < 300):
        print(f"[NoviyTavar] {method} {url} -> HTTP {resp.status_code} body[:200]={text[:200]!r}")
        raise NoviyTavarError(resp.status_code, text, url)
    if not text.strip():
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise NoviyTavarError(resp.status_code, f"JSON emas: {text[:200]}", url)


# ── 1. Kategoriya kaskadi ────────────────────────────────────────────


def root_categories(shop_id: str | int) -> list:
    """GET /product/rootCategories — ildiz kategoriyalar.

    Har element: {id, title, parentId, parentTitle, active, okpd2Required,
    canUse, hasChildren, hasActiveChildren}.
    """
    return _req("GET", f"{_BASE}/{shop_id}/product/rootCategories")


def child_categories(shop_id: str | int, parent_id: int) -> list:
    """GET /product/childCategories?parentId=N — bola kategoriyalar."""
    return _req("GET", f"{_BASE}/{shop_id}/product/childCategories?parentId={int(parent_id)}")


# ── 2. Kategoriya meta (6 ta GET birlashtirilgan) ────────────────────


def category_meta(shop_id: str | int, category_id: int) -> dict:
    """Tanlangan kategoriya uchun formani qurishga kerak bo'lgan HAMMA narsa.

    HAR'da portal frontend'i kategoriya tanlangach shu 6 ta GET'ni otadi —
    biz ularni bitta javobga yig'amiz (parallel, ~1 RTT):
      fields                 — komissiyalar (FBO/FBS/DBS %), sxema ruxsatlari,
                               o'lchov guruhi, qaytarish, DEFAULT_TO_STOCK_AMOUNT
      characteristics        — tayyor xususiyatlar (Rang: uz/ru nom + HEX)
      requiredCharacteristics— majburiy xususiyat id'lari
      filters                — aktiv filtrlar (Brend majburiy, Model, Davlat)
      fieldDescriptions      — qo'shimcha maydonlar (WARRANTY oy va h.k.)
      certification          — sertifikat filltype (OPTIONAL/REQUIRED)
    """
    sid = str(shop_id)
    cid = int(category_id)
    calls = {
        "fields": f"{_BASE}/{sid}/category/{cid}/fields",
        "characteristics": f"{_BASE}/{sid}/product/getDefinedCharacteristics?categoryIds={cid}",
        "requiredCharacteristics": f"{_BASE}/{sid}/product/required-characteristics?categoryId={cid}",
        "filters": f"{_BASE}/{sid}/filters/product/active?categoryId={cid}",
        "fieldDescriptions": f"{_BASE}/{sid}/product/field-descriptions?categoryId={cid}",
        "certification": f"{_BASE}/{sid}/product/product-certification-filltype?categoryId={cid}",
    }

    out: dict = {}

    def _one(key_url):
        key, url = key_url
        try:
            return key, _req("GET", url)
        except NoviyTavarError as e:
            # Bitta yordamchi endpoint yiqilsa forma butunlay o'lmasin —
            # bo'sh qiymat bilan davom etamiz, log'da ko'rinadi.
            print(f"[NoviyTavar] meta.{key} xato: {e}")
            return key, None

    with ThreadPoolExecutor(max_workers=6) as ex:
        for key, val in ex.map(_one, calls.items()):
            out[key] = val
    return out


# ── 3. Filtr qiymatlari (Brend/Model/Davlat) ─────────────────────────


def filter_values(shop_id: str | int, filter_id: int, category_id: int, *,
                  search: str = "", page: int = 0, size: int = 24) -> dict | list:
    """GET /filters/product/values — qidiruvli, sahifalangan (24/sahifa)."""
    from urllib.parse import quote
    url = (f"{_BASE}/{shop_id}/filters/product/values?filterId={int(filter_id)}"
           f"&page={int(page)}&search={quote(search or '')}"
           f"&selectedFilterValues=&size={int(size)}&categoryId={int(category_id)}")
    return _req("GET", url)


# ── 4. Taqiqlangan so'z tekshiruvi ───────────────────────────────────


def check_words(shop_id: str | int, text: str) -> list:
    """POST /moderation/check-words — body = JSON-string (HAR: ``"avto tavar"``).

    Bo'sh ro'yxat ``[]`` = toza; aks holda taqiqlangan so'zlar ro'yxati.
    """
    res = _req("POST", f"{_BASE}/{shop_id}/moderation/check-words",
               json_body=str(text or ""))
    return res if isinstance(res, list) else []


# ── 5. Rasm yuklash ──────────────────────────────────────────────────


def upload_image(file_bytes: bytes, filename: str, mimetype: str) -> dict:
    """POST images-uploader.uzum.uz/upload — multipart (file + tags).

    HAR shakli: form-data ``file`` + ``tags="product,product_3x4"``.
    Javob: {"error":"", "payload":{key, originalUrl, recommendations[],
    transformations{...}}} — ``recommendations`` = Uzum'ning sifat
    ogohlantirishi (masalan 1080×1440 dan kichik).
    """
    if len(file_bytes) > MAX_IMAGE_BYTES:
        raise NoviyTavarError(413, "Rasm 15MB dan katta", _UPLOAD_URL)
    sess = _get_http_session()
    headers = _headers()  # Content-Type'ni requests o'zi multipart bilan qo'yadi
    resp = sess.post(
        _UPLOAD_URL,
        headers=headers,
        files={"file": (filename, file_bytes, mimetype or "image/jpeg")},
        data={"tags": "product,product_3x4"},
        timeout=60,
    )
    text = resp.text or ""
    if not (200 <= resp.status_code < 300):
        print(f"[NoviyTavar] upload -> HTTP {resp.status_code} body[:200]={text[:200]!r}")
        raise NoviyTavarError(resp.status_code, text, _UPLOAD_URL)
    return json.loads(text)


# ── 6. Yaratish (QORALAMA) ───────────────────────────────────────────


def create_product(shop_id: str | int, body: dict) -> dict:
    """POST /product/createProduct?testVariant=B → 201 + yaratilgan karta.

    ``body`` shakli build_create_body() da bitta joyda quriladi — HAR'dagi
    real 201-muvaffaqiyat payload'ining aynan nusxasi.
    """
    return _req("POST", f"{_BASE}/{shop_id}/product/createProduct?testVariant=B",
                json_body=body, timeout=60)


def check_sku(shop_id: str | int, sku: str) -> dict:
    """GET /product/checkSku?sku=X → {"exists": bool} (SKU bosqichi uchun)."""
    from urllib.parse import quote
    return _req("GET", f"{_BASE}/{shop_id}/product/checkSku?sku={quote(sku or '')}")


# ── createProduct body quruvchisi (HAR shakli — YAGONA joy) ─────────


def _html_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _desc_html(s: str) -> str:
    """Oddiy matn → portal kutayotgan <p>...</p> HTML (qatorlar bo'yicha)."""
    lines = [ln.strip() for ln in (s or "").splitlines()]
    lines = [ln for ln in lines if ln]
    if not lines:
        return ""
    return "".join(f"<p>{_html_escape(ln)}</p>" for ln in lines)


def build_create_body(*, category_id: int,
                      title_uz: str, title_ru: str,
                      short_uz: str, short_ru: str,
                      desc_uz: str, desc_ru: str,
                      filter_values_sel: list[dict],
                      characteristics_sel: list[dict],
                      images: list[dict],
                      product_fields: dict) -> dict:
    """HAR'dagi 201-muvaffaqiyat body'sining aynan nusxasi — bitta joyda.

    filter_values_sel   — [{"filterId": 6, "filterValueId": 13942}, ...]
    characteristics_sel — [{"characteristicId": -1,
                            "characteristicTitle": {"uz": "Rang", "ru": "Цвет"},
                            "orderingNumber": 0,
                            "values": [{"title":{...},"value":"#..","skuValue":".."}, ...]}]
                          (values — portal bergan obyektlar VERBATIM)
    images              — [{"key": "...", "url": "https://images.uzum.uz/../original.jpg"}]
    product_fields      — {"WARRANTY": 12} kabi (field-descriptions'dan)
    """
    defined = []
    for ch in characteristics_sel or []:
        vals = [v for v in (ch.get("values") or []) if isinstance(v, dict)]
        if not vals:
            continue
        defined.append({
            "orderingNumber": int(ch.get("orderingNumber") or 0),
            "characteristicValues": vals,
            "characteristicTitle": ch.get("characteristicTitle") or {},
            "characteristicId": ch.get("characteristicId", -1),
            "defined": True,
        })

    prod_images = [
        {"deletable": True, "url": im["url"], "key": im["key"], "status": "ACTIVE"}
        for im in (images or []) if im.get("key") and im.get("url")
    ]

    fvals = [
        {"filterId": int(fv["filterId"]), "filterValueId": int(fv["filterValueId"])}
        for fv in (filter_values_sel or [])
        if fv.get("filterId") is not None and fv.get("filterValueId") is not None
    ]

    return {
        "attributes": {"ru": [], "uz": []},
        "blockReason": "",
        "blockReasons": [],
        "blockComment": "",
        "createdByFlowB": False,
        "blockedImages": {},
        "categoryId": int(category_id),
        "categoryEditable": True,
        "allFiltersFilled": False,
        "colorImages": [],
        "colorCollectionImages": [],
        "colorVideos": [],
        "comments": [{"comment": {"ru": "", "uz": ""}, "commentType": "Инструкция"}],
        "customCharacteristics": [],
        "dateModerated": None,
        "definedCharacteristics": defined,
        "description": {"ru": _desc_html(desc_ru), "uz": _desc_html(desc_uz)},
        "filterValues": fvals,
        "filters": [],
        "imageCollection": None,
        "okpd2": None,
        "hasAnySkuBlocked": False,
        "photoOnPreview": False,
        "productFields": product_fields or {},
        "productImages": prod_images,
        "productCertificates": [],
        "ratingInfo": None,
        "shortDescription": {"ru": short_ru or "", "uz": short_uz or ""},
        "skuBlockReason": None,
        "title": {"ru": title_ru or "", "uz": title_uz or ""},
        "video": None,
        "skuList": [],
        "switchbackActive": False,
    }
