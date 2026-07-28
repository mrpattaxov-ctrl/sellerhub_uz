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
holatda turadi).

⚠️ TUZATISH (2026-07-17): bu yerda ilgari «SKU/narx + moderatsiya bosqichi
alohida HAR kutmoqda» deb yozilgan edi — BU NOTO'G'RI. Dalil allaqachon bor:
`artifacts/uzum-product-form-reference/har/full-flow/` (236 HAR) ichida
230 ta `sendSkuData` tanasi (2-bosqich «Цены и SKU»), 230 ta
`POST filters/product/{productId}` (3-bosqich «Свойства») va eco-flag
xatosining 5 ta 500-javobi bor. Yangi capture buyurtirishdan oldin o'sha
katalogni skanerlang.
"""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor

from config import HTTP_USER_AGENT
from core.auth_helpers import _get_admin_token
from core.http_client import _get_http_session

_BASE = "https://api-seller.uzum.uz/api/seller/shop"
_LOUIS_BASE = "https://images-uploader.uzum.uz"
_UPLOAD_URL = f"{_LOUIS_BASE}/upload"

# ⚠️ 3-QADAM «Свойства» — QIYMATLAR ASSORTMENT GATEWAY'DA (portal shop API'da EMAS).
#
# Bandl `public` API mijozi (mf-products `hX()`) `ASSORTMENT_BASE_URL` bazasini
# ishlatadi — auth AYNI shu Bearer admin token (FBS seller-openapi tokeni EMAS).
# Prod qiymat app.js'da: VUE_APP_ASSORTMENT_BASE_URL. mf-products'dagi `eX`
# bloki DEV klasterni ko'rsatadi (api.dev.cluster.daymarket.uz) — undan OLMADIM.
#
# ⚠️ GATEWAY `/public/api/v1` PREFIKSINI O'ZI QO'SHADI. Bandldagi to'liq
# `/public/api/v1/...` yo'lni yuborsang → gateway ikkilantiradi
# (`/public/api/v1/public/api/v1/...`) → 404. Shuning uchun bu yerdagi
# yo'llar PREFIKSSIZ (`/categories/...`, `/attributes/enums`).
#
# DALIL (jonli read-only probe 2026-07-17, kategoriya 14007):
#   GET .../api/assortment/v1/categories/14007/infomodel        → 200 JSON, 33 atribut
#   GET .../api/assortment/v1/attributes/enums?attrCode=filter_1687 → 200 JSON, 13 qiymat
#   GET .../api/assortment/v1/public/api/v1/categories/14007/infomodel → 404 (ikkilangan yo'l)
_ASSORT_BASE = "https://api.uzum.uz/api/assortment/v1"

# ⚠️ VIDEO RASMDAN BOSHQA HOSTDA VA BOSHQA AUTH'DA — teskarisini qilma.
#
#   rasm / 360 → images-uploader.uzum.uz  + XOM LOUIS_KEY   (Bearer → 401)
#   video      → api-seller.uzum.uz/api   + Bearer <sotuvchi tokeni>  (LOUIS EMAS)
#
# DALIL (jonli probe 2026-07-18, 3 nomzod host):
#   api-seller.uzum.uz/api/public/video/upload → 200 ✅ JSON
#   api-seller.uzum.uz/public/video/upload     → 403 «RBAC: access denied»
#   seller.uzum.uz/api/public/video/upload     → 200, lekin text/html (SPA qobig'i)
#
# ⚠️ HANDOFF_2026_07_18.md §5 «seller.uzum.uz» degan — XATO. U qiymat bandlning
# `eX` zaxira env blokidan olingan, u yerda LOUIS ham DEV klasterni ko'rsatadi
# (ish paytida setEnv() ustidan yozadi). Tarmoq dalili buni rad etadi:
# 707 HAR / 322 717 yozuvda `seller.uzum.uz/api/*` → 0 yo'l, hammasi
# api-seller.uzum.uz da. Batafsil: artifacts/.../PROBE_FINDINGS_2026_07_18.md
_VIDEO_UPLOAD_URL = "https://api-seller.uzum.uz/api/public/video/upload"

# ⚠️ MUHIM — product-modul Accept-Language'ga QATTIQ bog'liq. Uzum'ning
# `/product/*` portal endpointlari faqat `uz`/`ru` locale'ni tushunadi;
# app-wide `HTTP_ACCEPT_LANGUAGE` (`en-US,en;q=0.9,ru;q=0.8`) yuborilsa
# `internal-server-error-001` (HTTP 500) qaytadi — FBO/postavki esa xuddi
# shu qiymatni bemalol qabul qiladi (live probe 2026-07-11 tasdiqladi:
# en-US→500, uz-UZ→200 «Hayvonlar», ru→200 «Животные»). Shu sababli bu
# modul HAR chaqiruvda `uz-UZ` majburlaydi (o'zbekcha-birinchi konvensiya,
# [[project-fbs-language-uzbek-first]]). Xato token/rol EMAS edi — aynan
# shu bitta header. Bilib turib global config qiymatiga qaytarmang.
_PRODUCT_ACCEPT_LANGUAGE = "uz-UZ"

# Sahifa tili -> portal locale. Yuqoridagi probe aynan shu ikkitasini
# tasdiqlagan (uz-UZ→«Hayvonlar», ru→«Животные»). Boshqa qiymat qo'shmang:
# en-US 500 beradi. Noma'lum til kelsa uz-UZ'ga tushamiz (default konvensiya).
_LOCALE_BY_LANG = {"uz": "uz-UZ", "ru": "ru"}


def _accept_language(lang: str | None = None) -> str:
    """Kategoriya/xususiyat NOMLARI shu locale'da qaytadi.

    Sabab: kategoriya `title` — tekis string, portal uni header bo'yicha
    tarjima qiladi. `uz-UZ` qotirilgan bo'lsa RU sahifada ham «Hayvonlar»
    chiqadi (Uzum'da esa «Животные») — foydalanuvchi shu farqni ko'rsatdi.
    """
    return _LOCALE_BY_LANG.get((lang or "").strip().lower(), _PRODUCT_ACCEPT_LANGUAGE)

# Rasm hajmi cheklovi (proxy orqali o'tadigan multipart) — portal o'zi ham
# ~10MB atrofida cheklaydi; biz 15MB da kesamiz (xotira himoyasi).
MAX_IMAGE_BYTES = 15 * 1024 * 1024

# Video / 360-arxiv chegaralari — Uzum bandlidan O'LCHANGAN (taxmin emas):
# MediaUploader.ce (chunk-3ca9d890 @16337) `K = 10` va `size/1024/1024 > K`,
# arxiv uchun ham qattiq `> 10`. ⚠️ Ekrandagi maslahat «3 Mb» deydi — u i18n
# matni, TEKSHIRUV EMAS (HANDOFF_2026_07_18.md §5 shu yerda adashgan).
MAX_VIDEO_BYTES = 10 * 1024 * 1024
MAX_COLLECTION_BYTES = 10 * 1024 * 1024

# ⚠️ RASM YUKLAGICHI BOSHQA KALIT ISHLATADI — sotuvchi tokeni EMAS.
#
# DALIL 1 (portal bandli, chunk-6dbbb9d8):
#     fetch(env.LOUIS_API_HOST + "/upload", {method:"POST", body: form,
#           headers:{Authorization: env.LOUIS_KEY, "Accept-Language": ...}})
#   — ya'ni XOM kalit, «Bearer» prefiksisiz.
# DALIL 2 (jonli probe, 2026-07-17, 4 variant):
#     sotuvchi tokeni -> 401 · LOUIS_KEY xom -> 200 ✅ · LOUIS_KEY+Bearer -> 401
#     · Authorization'siz -> 401
#
# Kalit portalning OCHIQ frontend bandlida (VUE_APP_LOUIS_KEY) — har brauzerga
# ketadi, foydalanuvchi siri emas. Shunga qaramay repoga YOZILMAYDI: qiymat
# `.env` dan olinadi (docker-compose `env_file`), chunki (a) repoda ikkita
# GitHub remote bor, (b) Uzum frontendni qayta yig'sa kalit yangilanadi —
# konfigda bo'lsa bir qator + restart, kodda bo'lsa deploy kerak bo'lardi.
_LOUIS_KEY = (os.getenv("UZUM_LOUIS_KEY") or "").strip()

# Ixtiyoriy bo'limlarning API kalitlari — Uzum bandlidan (addComment chaqiruvlari
# + comments.Сертификация). ⚠️ UI yorlig'i bilan bir xil EMAS:
#   «Размерная сетка» → «Размеры» · «Сертификаты» → «Сертификация»
# Ro'yxatdan tashqari tur Uzum'ga yuborilmaydi.
_COMMENT_TYPES = frozenset({"Состав", "Размеры", "Инструкция", "Сертификация"})


class NoviyTavarError(RuntimeError):
    """Portal chaqiruvi xatosi — HTTP status + qisqa body bilan."""

    def __init__(self, http_status: int, body: str, url: str):
        self.http_status = http_status
        self.body = (body or "")[:500]
        self.url = url
        super().__init__(f"portal {url} -> HTTP {http_status}: {self.body[:200]}")


def _headers(content_type: str | None = None, lang: str | None = None) -> dict:
    """Portal so'rovi header'lari — postavki bilan bir xil Bearer sxema.

    ``lang`` berilmasa — o'zbekcha-birinchi default (o'zgarmagan xatti-harakat).
    """
    h = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": HTTP_USER_AGENT,
        # product-modul faqat uz/ru locale'ni qabul qiladi — global
        # en-US config'i 500 beradi (yuqoridagi _PRODUCT_ACCEPT_LANGUAGE izohi).
        "Accept-Language": _accept_language(lang),
    }
    if content_type:
        h["Content-Type"] = content_type
    tok = (_get_admin_token() or "").strip()
    if tok:
        h["Authorization"] = tok if tok.startswith("Bearer ") else f"Bearer {tok}"
    return h


def _req(method: str, url: str, *, json_body=None, timeout: int = 30,
         lang: str | None = None):
    """Bitta portal JSON so'rovi. ``json_body`` dict/list/str bo'lishi mumkin
    (check-words JSON-string yuboradi: ``"matn"``). Non-2xx → NoviyTavarError.

    ``lang`` — O'QISH chaqiruvlari uchun (kategoriya/xususiyat nomlari shu
    tilda qaytadi). Berilmasa uz-UZ (o'zgarmagan default).
    """
    sess = _get_http_session()
    data = None
    headers = _headers("application/json" if json_body is not None else None,
                       lang=lang)
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


def root_categories(shop_id: str | int, lang: str | None = None) -> list:
    """GET /product/rootCategories — ildiz kategoriyalar.

    Har element: {id, title, parentId, parentTitle, active, okpd2Required,
    canUse, hasChildren, hasActiveChildren}.

    ⚠️ `title` — TEKIS string, portal uni Accept-Language bo'yicha tarjima
    qiladi (dict emas!). Shuning uchun `lang` shu yerda muhim.
    ⚠️ `active:false` — «o'chirilgan» degani EMAS: ildizlarning HAMMASI shunday
    (`canUse:true`). Barg/tugun farqi `hasChildren` orqali.
    """
    return _req("GET", f"{_BASE}/{shop_id}/product/rootCategories", lang=lang)


def child_categories(shop_id: str | int, parent_id: int,
                     lang: str | None = None) -> list:
    """GET /product/childCategories?parentId=N — bola kategoriyalar."""
    return _req("GET", f"{_BASE}/{shop_id}/product/childCategories?parentId={int(parent_id)}",
                lang=lang)


# ── 2. Kategoriya meta (6 ta GET birlashtirilgan) ────────────────────


def category_meta(shop_id: str | int, category_id: int,
                  lang: str | None = None) -> dict:
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
            return key, _req("GET", url, lang=lang)
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
                  search: str = "", page: int = 0, size: int = 24,
                  lang: str | None = None) -> dict | list:
    """GET /filters/product/values — qidiruvli, sahifalangan (24/sahifa)."""
    from urllib.parse import quote
    url = (f"{_BASE}/{shop_id}/filters/product/values?filterId={int(filter_id)}"
           f"&page={int(page)}&search={quote(search or '')}"
           f"&selectedFilterValues=&size={int(size)}&categoryId={int(category_id)}")
    return _req("GET", url, lang=lang)


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
    if not _LOUIS_KEY:
        # Jim 401 o'rniga aniq sabab (yuqoridagi _LOUIS_KEY izohiga qarang).
        raise NoviyTavarError(
            500,
            "UZUM_LOUIS_KEY sozlanmagan — rasm yuklagich sotuvchi tokenini "
            "qabul qilmaydi (401). Kalitni muhit o'zgaruvchisiga qo'ying.",
            _UPLOAD_URL,
        )
    sess = _get_http_session()
    # ⚠️ _headers() ISHLATILMAYDI — u `Authorization: Bearer <sotuvchi tokeni>`
    # qo'yadi, bu yerda esa XOM LOUIS_KEY kerak (jonli probe: Bearer -> 401).
    headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": HTTP_USER_AGENT,
        "Accept-Language": _PRODUCT_ACCEPT_LANGUAGE,
        "Authorization": _LOUIS_KEY,
    }  # Content-Type'ni requests o'zi multipart bilan qo'yadi
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


def _parse_media_json(resp, url: str) -> dict:
    """Media javobini tekshirib JSON qaytar.

    ⚠️ ``200`` NI MUVAFFAQIYAT DEB O'QIMA — Content-Type'ni tekshir. Uzum
    `User-Agent` yoqmasa anti-bot sahifasini HTTP **200** + `text/html` bilan
    qaytaradi («Siz robot emasmisiz?»), noto'g'ri hostda esa SPA qobig'ini —
    ikkalasi ham 200. Jonli probe 2026-07-18 da aynan shu chalg'itgan.
    """
    text = resp.text or ""
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if not (200 <= resp.status_code < 300):
        print(f"[NoviyTavar] media {url} -> HTTP {resp.status_code} "
              f"body[:200]={text[:200]!r}")
        raise NoviyTavarError(resp.status_code, text, url)
    if "json" not in ctype:
        print(f"[NoviyTavar] media {url} -> HTTP 200 lekin JSON EMAS "
              f"ctype={ctype!r} body[:120]={text[:120]!r}")
        raise NoviyTavarError(
            502,
            f"Uzum JSON o'rniga {ctype or 'nomalum tur'} qaytardi "
            "(anti-bot sahifasi yoki noto'g'ri host)",
            url,
        )
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise NoviyTavarError(502, f"JSON emas: {text[:200]}", url)


def upload_video(file_bytes: bytes, filename: str,
                 mimetype: str | None = None) -> dict:
    """POST api-seller.uzum.uz/api/public/video/upload → {key, originalUrl}.

    ⚠️ RASMDAN FARQLI — bu yerda SOTUVCHI tokeni (Bearer) ishlaydi, LOUIS_KEY
    EMAS. Yuqoridagi ``_VIDEO_UPLOAD_URL`` izohiga qarang.

    Bandl (chunk-3ca9d890 @22396): ``FormData: metadata="{}", video=<fayl>``.
    Jonli probe (2026-07-18) javobi:
        {"payload": {"key": "...", "original_url": "https://storage.yandexcloud.net/
                     um-prod-videos/videos/<key>/original.mp4"}}
    ⚠️ ``original_url`` — **snake_case** (rasmda ``originalUrl`` — camelCase!).
    Chaqiruvchi ikki xil nom bilan ovora bo'lmasin uchun shu yerda
    ``{"key", "originalUrl"}`` ga normallashtiramiz.
    """
    if len(file_bytes) > MAX_VIDEO_BYTES:
        raise NoviyTavarError(413, "Video 10MB dan katta", _VIDEO_UPLOAD_URL)
    sess = _get_http_session()
    # _headers() TO'G'RI — Bearer + admin token + uz-UZ + User-Agent.
    # Content-Type'ni requests multipart bilan o'zi qo'yadi (shuning uchun
    # _headers() ga content_type BERILMAYDI).
    resp = sess.post(
        _VIDEO_UPLOAD_URL,
        headers=_headers(),
        files={"video": (filename, file_bytes, mimetype or "video/mp4")},
        data={"metadata": "{}"},
        timeout=120,
    )
    payload = (_parse_media_json(resp, _VIDEO_UPLOAD_URL).get("payload") or {})
    key = payload.get("key") or ""
    url = payload.get("original_url") or ""
    if not key or not url:
        raise NoviyTavarError(
            502, f"video javobida key/original_url yo'q: {payload!r}"[:200],
            _VIDEO_UPLOAD_URL)
    return {"key": key, "originalUrl": url}


def upload_collection(file_bytes: bytes, filename: str) -> int:
    """POST {LOUIS}/upload/collection → ``collection_id`` (360-foto arxivi).

    ASINXRON: bu chaqiruv faqat id beradi, rasmlar TAYYOR EMAS. Kadrlarni olish
    uchun ``collection_status()`` ni poll qiling.

    ⚠️ Bandl komponenti (chunk-3ca9d890 @21143) ``payload.collections[0]`` ni
    YUKLASH javobidan kutgandek ko'rinadi — bu CHALG'ITADI: u oradagi
    ``baseCollectionUploadHandler`` ichida poll qilib, STATUS javobini ko'radi.
    Jonli probe (2026-07-18) yuklash javobi:
        {"error": "", "payload": {"collection_id": 9144}}    ← collections YO'Q
    """
    if len(file_bytes) > MAX_COLLECTION_BYTES:
        raise NoviyTavarError(413, "Arxiv 10MB dan katta",
                              f"{_LOUIS_BASE}/upload/collection")
    if not _LOUIS_KEY:
        raise NoviyTavarError(
            500,
            "UZUM_LOUIS_KEY sozlanmagan — 360 yuklagich sotuvchi tokenini "
            "qabul qilmaydi. Kalitni muhit o'zgaruvchisiga qo'ying.",
            f"{_LOUIS_BASE}/upload/collection",
        )
    url = f"{_LOUIS_BASE}/upload/collection"
    sess = _get_http_session()
    # ⚠️ _headers() ISHLATILMAYDI — XOM LOUIS_KEY kerak (rasm yo'li bilan bir xil).
    resp = sess.post(
        url,
        headers={
            "Accept": "application/json, text/plain, */*",
            "User-Agent": HTTP_USER_AGENT,
            "Accept-Language": _PRODUCT_ACCEPT_LANGUAGE,
            "Authorization": _LOUIS_KEY,
        },
        files={"archive": (filename, file_bytes, "application/zip")},
        data={"metadata": "{}"},
        timeout=120,
    )
    payload = (_parse_media_json(resp, url).get("payload") or {})
    cid = payload.get("collection_id")
    if not isinstance(cid, int):
        raise NoviyTavarError(
            502, f"collection javobida collection_id yo'q: {payload!r}"[:200], url)
    return cid


def collection_status(collection_id: int | str) -> dict:
    """GET {LOUIS}/status/collection/{id} → ``collections[0]``.

    Jonli probe (2026-07-18) javobi:
        {"payload": {"collections": [{"id": 9144, "uploaded": 2, "total": 2,
          "process_job_status": "succeeded",
          "images": [{"key": "...", "url": "https://images.uzum.uz/<key>/360_720x960.jpg",
                      "url_prefix": "...", "is_3x4": false,
                      "is_made_in_ke_studio": false}]}]}}

    ``process_job_status`` tayyorlik belgisi (probe'da ``succeeded``).
    Bandl 5000ms interval bilan poll qiladi.
    """
    if not _LOUIS_KEY:
        raise NoviyTavarError(
            500, "UZUM_LOUIS_KEY sozlanmagan",
            f"{_LOUIS_BASE}/status/collection/{collection_id}")
    url = f"{_LOUIS_BASE}/status/collection/{int(collection_id)}"
    sess = _get_http_session()
    resp = sess.get(
        url,
        headers={
            "Accept": "application/json, text/plain, */*",
            "User-Agent": HTTP_USER_AGENT,
            "Accept-Language": _PRODUCT_ACCEPT_LANGUAGE,
            "Authorization": _LOUIS_KEY,
        },
        timeout=30,
    )
    payload = (_parse_media_json(resp, url).get("payload") or {})
    cols = payload.get("collections") or []
    if not cols:
        raise NoviyTavarError(502, "collections bo'sh", url)
    return cols[0]


# ── 6. Yaratish (QORALAMA) ───────────────────────────────────────────


def create_product(shop_id: str | int, body: dict) -> dict:
    """POST /product/createProduct?testVariant=B → 201 + yaratilgan karta.

    ``body`` shakli build_create_body() da bitta joyda quriladi — HAR'dagi
    real 201-muvaffaqiyat payload'ining aynan nusxasi.
    """
    return _req("POST", f"{_BASE}/{shop_id}/product/createProduct?testVariant=B",
                json_body=body, timeout=60)


def can_edit_product(shop_id: str | int, product_id: int | str) -> dict:
    """POST /product/editable?productId=X — kartani tahrirlash mumkinmi.

    Bandl `canEditProduct` (mf-products @3234674): POST, TANASIZ, faqat query.
    Uzum portali tahrirlash sahifasini ochganda shuni chaqiradi va javob
    «tahrirlash mumkin emas» desa 1-qadam nishonini «RESTRICTED» qiladi.
    """
    return _req("POST", f"{_BASE}/{shop_id}/product/editable"
                        f"?productId={int(product_id)}")


def edit_product(shop_id: str | int, body: dict) -> dict:
    """POST /product/editProduct → MAVJUD kartani yangilaydi.

    ⚠️ `create_product` bilan ADASHTIRMANG: createProduct HAR SAFAR YANGI
    qoralama yaratadi. Foydalanuvchi 1-qadamga qaytib «Saqlash» bosganda
    createProduct yuborilsa — dublikat karta paydo bo'ladi.

    DALIL (Uzum bandli `chunk-6dbbb9d8` @79125 — saqlash tugmasi):
        isEdit ? editProduct(shopId, {...v}) : createProduct(shopId, v, {testVariant})
    ya'ni TANA IKKALASIDA BIR XIL obyekt; farqi — endpoint va tanadagi `id`
    (store `Oe` klassida `id` maydoni bor, `isEdit = !!route.params.productId`).
    Saqlashdan oldin bandl `delete v.categories` va `delete v.status` qiladi —
    bizning tanamizda u kalitlar umuman yo'q.
    """
    return _req("POST", f"{_BASE}/{shop_id}/product/editProduct",
                json_body=body, timeout=60)


def archive_product(shop_id: str | int, product_id: int | str,
                    *, restore: bool = False) -> dict:
    """POST /shop/{shopId}/product/{productId}/archive[/restore] — kartani
    arxivlaydi yoki arxivdan chiqaradi.

    ⚠️ HAQIQIY UZUM MUTATSIYASI — sotuvchi katalogining holatini o'zgartiradi
    (`PRODUCT_ARCHIVING`, ichki `PRODUCT_DELETE` huquqi). Qaytariladigan amal:
    `restore=True` arxivdan chiqaradi.

    DALIL (Uzum portal bandli `mf-products`, `.uicheck/uzum-har/new.har`):
        archiveProduct(shopId, productId)  → path "/seller/shop/{s}/product/{p}/archive",  POST, secure
        restoreFromArchive(shopId, productId) → path "…/archive/restore", POST
    IDlar FAQAT path'da — request TANASI YO'Q (bandl `opts={}` yuboradi).
    ⚠️ Bitta POST = bitta mahsulot (bulk-array yo'q).
    """
    suffix = "/archive/restore" if restore else "/archive"
    url = f"{_BASE}/{shop_id}/product/{product_id}{suffix}"
    return _req("POST", url, timeout=30)


def check_sku(shop_id: str | int, sku: str) -> dict:
    """GET /product/checkSku?sku=X → {"exists": bool} (SKU bosqichi uchun)."""
    from urllib.parse import quote
    return _req("GET", f"{_BASE}/{shop_id}/product/checkSku?sku={quote(sku or '')}")


# ── 7. 2-QADAM «Цены и SKU» ──────────────────────────────────────────


def get_product(shop_id: str | int, product_id: int | str,
                lang: str | None = None) -> dict:
    """GET /product?productId=X — mahsulot kartasi (o'qish).

    ⚠️ `productId` — QUERY parametri, yo'l segmenti EMAS.
    `/product/{id}` deb yozsangiz Spring «Not Found» (404) beradi va uni
    `entity-not-found-001` bilan adashtirasiz (jonli urilgan xato).
    HAR: 233 ta 200-javob shu shaklda.
    """
    return _req("GET", f"{_BASE}/{shop_id}/product?productId={int(product_id)}",
                lang=lang)


def product_description_response(shop_id: str | int, product_id: int | str,
                                 lang: str | None = None) -> dict:
    """GET /product/{id}/description-response — **2-QADAMNING MA'LUMOT MANBAI**.

    ⚠️ Bu `get_product()` dan BOSHQA shakl qaytaradi. 2-qadamga aynan SHU kerak:
    `shopSkuTitle` va `productSkuTitle` `get_product()` javobida YO'Q.

    Jonli o'lchangan (2026-07-17, qoralama 3068623, do'kon BLUMMY) — 13 kalit:
        id, title, shopSkuTitle, productSkuTitle, commission, commissionDto,
        definedCharacteristicList, customCharacteristicList,
        hasCustomCharacteristics, hasActiveCalendarEvents, filters,
        newYearStatus, skuList

    Muhimlari:
      · `shopSkuTitle`  — do'kon prefiksi, Uzum O'ZI beradi (BLUMMY / SACVOYA).
        Biz sozlamaymiz. HAR: createProduct JAVOBIDA ham aynan shu keladi.
      · `productSkuTitle` — foydalanuvchi 2-qadamda kiritadigan prefiks
        (yangi qoralamada `""`).
      · `skuList` — ⭐ Uzum har rang qiymati uchun qatorni **OLDINDAN quradi**
        (jonli: 3 rang → 3 qator), hammasi `null`, faqat `skuTitle` to'la.
      · `commissionDto` — {minCommission, maxCommission} (jonli: 20.0/20.0).

    HAR'da bu endpoint atigi 1 marta uchraydi (tahrirlash yo'li), lekin
    **jonli tasdiqlandi**: yangi qoralamada ham ishlaydi.
    """
    return _req("GET",
                f"{_BASE}/{shop_id}/product/{int(product_id)}/description-response",
                lang=lang)


# ⚠️ IKPU endpointlari do'kon ostida EMAS — boshqa prefiks.
# HAR: /api/seller/product/ikpu/{search,check} (do'kon id'siz).
_IKPU_BASE = "https://api-seller.uzum.uz/api/seller/product/ikpu"


def ikpu_search(category_id: int | str, search: str, *, page: int = 0,
                size: int = 20, lang: str | None = None) -> list:
    """GET /ikpu/search — soliq kodi (IKPU) qidiruvi → `payload[]`.

    Jonli o'lchangan (2026-07-17) element kalitlari:
        ikpu, ikpuName, subPositionName, positionName, className, groupName,
        isValidForCategory, isVatRelief
    HAR: `size=20&page=0` (3 namuna).
    """
    from urllib.parse import quote
    url = (f"{_IKPU_BASE}/search?search={quote(search or '')}"
           f"&size={int(size)}&page={int(page)}&categoryId={int(category_id)}")
    res = _req("GET", url, lang=lang)
    payload = res.get("payload") if isinstance(res, dict) else None
    return payload if isinstance(payload, list) else []


def ikpu_check(category_id: int | str, ikpu: str,
               lang: str | None = None) -> dict:
    """GET /ikpu/check → `{"payload": {"privileged": bool}}`.

    Jonli tasdiqlangan (2026-07-17): `{"privileged": false}`.
    `privileged` — imtiyozli soliq rejimi belgisi.
    """
    from urllib.parse import quote
    url = f"{_IKPU_BASE}/check?ikpu={quote(ikpu or '')}&categoryId={int(category_id)}"
    res = _req("GET", url, lang=lang)
    payload = res.get("payload") if isinstance(res, dict) else None
    return payload if isinstance(payload, dict) else {}


def send_sku_data(shop_id: str | int, body: dict) -> dict:
    """POST /product/sendSkuData → 201 (**YOZUVCHI** — qoralamaga SKU qo'shadi).

    Tana `build_sku_body()` da quriladi — 231 ta haqiqiy 201-tananing nusxasi.
    Javob (HAR, 231/231 → 201):
        {"skuDataResponseList": [{"skuId": 3568617, "sellerItemBarcode": null}]}

    ⚠️ XATO SHAKLI BOSHQACHA: 400 da bandl `response.data.error` ni (SATR)
    ko'rsatadi — 3-qadamdagi `{"payload":[{in,path,msg}]}` dan FARQ QILADI.
    """
    return _req("POST", f"{_BASE}/{shop_id}/product/sendSkuData",
                json_body=body, timeout=60)


def sku_commission(shop_id: str | int, product_id: int | str,
                   prices: list[dict], lang: str | None = None) -> list:
    """POST /product/{id}/commission → komissiya kalkulyatori (O'QISH).

    ⚠️ POST bo'lsa ham MUTATSIYA EMAS: bandl uni har narx kiritilganda
    (300ms debounce) chaqiradi — sof hisoblagich, hech narsa yozmaydi.

    Bandl (chunk-2619e7c4 @61997, `getNewSkuCommission`):
        path : /seller/shop/{shopId}/product/{productId}/commission
        body : {"prices": [{"newPrice": N, "skuId": N|undefined}]}
        →      payload.skuPrices[]

    ⚠️ HAR'da bu endpoint YO'Q — capture skripti sendSkuData tanasini O'ZI
    qurgan, portal UI'sidan o'tmagan. Ya'ni bandl (3-daraja) yagona manba edi.
    """
    body = {"prices": prices or []}
    res = _req("POST", f"{_BASE}/{shop_id}/product/{int(product_id)}/commission",
               json_body=body, lang=lang)
    payload = res.get("payload") if isinstance(res, dict) else None
    if isinstance(payload, dict):
        rows = payload.get("skuPrices")
        return rows if isinstance(rows, list) else []
    return []


def build_sku_body(*, product_id: int, sku_for_product: str,
                   rows: list[dict],
                   defined_characteristics: list[dict] | None = None) -> dict:
    """sendSkuData tanasi — HAR'dagi 231 ta 201-tananing aynan nusxasi.

    ``rows`` — brauzer qatorlari:
        {skuTitle, fullPrice, sellPrice, ikpu, barcode, sellerItemCode, id,
         width, height, length, weight}

    ``defined_characteristics`` — description-response'dagi
    `definedCharacteristicList` (skuValue → xususiyat qiymatini topish uchun).

    Bandl (chunk-3ca9d890 @84796, `lt()`), so'zma-so'z ko'chirilgan qoidalar:
      · `id: t.id || undefined`  → yangi SKU'da kalit BUTUNLAY yo'q
        (HAR: `id` atigi 5/235 tanada — ular tahrirlash yo'li).
      · `sellerItemCode: ... || undefined` → bo'sh bo'lsa YUBORILMAYDI
        (shuning uchun HAR tanalarida bu kalit ko'rinmaydi).
      · `dimensions: u ? undefined : {...}` — ⚠️ HAMMASI-YOKI-HECHNARSA:
        w/h/l/weight dan BITTASI bo'sh bo'lsa, `dimensions` BUTUNLAY tushadi.
      · `skuTitle` = `shopSkuTitle-productSkuTitle-skuValue1[-skuValue2...]`;
        bandl `skuTitle.split("-").slice(2)` bilan qiymatlarni ajratadi →
        birinchi IKKI bo'lak (do'kon + prefiks) rezerv qilingan.
      · `status: null` (235/235).
    """
    sku_list = []
    for r in rows or []:
        dims = {}
        ok_dims = True
        for k in ("width", "height", "length", "weight"):
            v = r.get(k)
            if v in (None, "", 0):
                ok_dims = False
                break
            dims[k] = int(v)

        item = {
            "fullPrice": int(r.get("fullPrice") or 0),
            "sellPrice": int(r.get("sellPrice") or 0),
            "ikpu": str(r.get("ikpu") or ""),
            "skuTitle": str(r.get("skuTitle") or ""),
            "barcode": (str(r["barcode"]) if r.get("barcode") else None),
            "skuCharacteristicList": _sku_chars_for(r, defined_characteristics),
            "status": None,
        }
        # ⚠️ `undefined` semantikasi: kalitni QO'SHMAYMIZ (None yuborish BOSHQA
        # narsa — bandl `|| undefined` bilan uni tanadan butunlay chiqaradi).
        if ok_dims:
            item["dimensions"] = dims
        if r.get("id"):
            item["id"] = int(r["id"])
        if (r.get("sellerItemCode") or "").strip():
            item["sellerItemCode"] = str(r["sellerItemCode"]).strip()
        sku_list.append(item)

    return {
        "productId": int(product_id),
        "skuForProduct": str(sku_for_product or ""),
        "skuList": sku_list,
        # Maxsus xususiyatlar bizda hali yo'q (customCharacteristics = 0/235).
        "skuTitlesForCustomCharacteristics": [],
    }


def _sku_chars_for(row: dict, defined: list[dict] | None) -> list:
    """Qatorning `skuTitle` idan xususiyat qiymatlarini tiklash.

    Bandl `ot()` (chunk-3ca9d890 @84796) aynan shunday qiladi:
        l = skuList[t].skuTitle.split("-").slice(2)   // qiymat bo'laklari
        u = c.skuValue || De(cb(c.title))             // qiymat kaliti
        if (l[d] === u) push({characteristicTitle, definedType, characteristicValue})
    `d` — xususiyatning `orderingNumber` bo'yicha tartibdagi indeksi.
    """
    out = []
    if not defined:
        return out
    parts = str(row.get("skuTitle") or "").split("-")[2:]
    ordered = sorted(defined, key=lambda c: int(c.get("orderingNumber") or 0))
    for idx, ch in enumerate(ordered):
        if idx >= len(parts):
            break
        want = parts[idx]
        for val in (ch.get("characteristicValues") or []):
            if not isinstance(val, dict):
                continue
            if (val.get("skuValue") or "") != want:
                continue
            title = ch.get("characteristicTitle") or {}
            vt = val.get("title") or {}
            out.append({
                "characteristicTitle": {"ru": title.get("ru", ""),
                                        "uz": title.get("uz", "")},
                "definedType": True,
                "characteristicValue": {"ru": vt.get("ru", ""),
                                        "uz": vt.get("uz", "")},
            })
            break
    return out


# ── 8. 3-QADAM «Свойства» (xususiyatlar / filtrlar) ──────────────────
#
# Bandl (chunk-1527337c — 3-qadam komponenti) ikki manba ishlatadi:
#   · JADVAL (ustunlar + qatorlar + mavjud qiymatlar):
#       GET  /seller/shop/{shop}/filters/product/{productId}   (portal API)
#       →    {filters, skuFilters, skuAttributes, sku, categoryId}
#   · DROPDOWN QIYMATLARI (har enum atribut uchun, dropdown ochilganda):
#       GET  {ASSORT}/attributes/enums?attrCode=&page=&size=&search=   (assortment)
#   · SAQLASH (YOZUVCHI):
#       POST /seller/shop/{shop}/filters/product/{productId}   (portal API)
#       body {skuFilters, skuAttributeValues}
#
# ⚠️ JADVAL uchun mahsulotда SKU BO'LISHI SHART — `sku` bo'sh bo'lsa Uzum
# «taqiq» ekranini ko'rsatadi (i18n `create_filters.filters_forbidden`:
# «SKU kodsiz mahsulot xususiyatlarini qoʻsha olmaysiz»). Bu XATO EMAS —
# Uzumning O'Z xatti-harakati (jonli i18n bloki tasdiqladi).


def product_filters(shop_id: str | int, product_id: int | str,
                    lang: str | None = None) -> dict:
    """GET /filters/product/{id} — 3-qadam jadval ma'lumoti (O'QISH).

    ⚠️ `productId` — bu yerda YO'L segmenti (2-qadamdagi `?productId=` query
    EMAS). HAR: 460 ta GET shu shaklda.

    Jonli o'lchangan javob kalitlari (5): `filters`, `skuFilters`,
    `skuAttributes`, `sku`, `categoryId`.
      · `skuAttributes` — [{skuId, attributes:[{attributeCode, attributeName
        {uz,ru}, valueType, required, dataProperties{maxValues,...}, ordering,
        ...}]}]. Ustunlar shundan quriladi (attributeName ikki tilda inline).
      · `sku` — mahsulot SKU qatorlari; BO'SH bo'lsa → «taqiq» ekrani.
      · `filters`/`skuFilters` — ESKI filterId tizimi (236 HAR'da deyarli
        bo'sh: filters 1 ta, skuFilters 0 ta). Yangi tizim `skuAttributes`.
    """
    return _req("GET", f"{_BASE}/{shop_id}/filters/product/{int(product_id)}",
                lang=lang)


def attr_enums(attr_code: str, *, search: str = "", page: int = 0,
               size: int = 1000, lang: str | None = None) -> list:
    """GET {ASSORT}/attributes/enums — enum atribut dropdown qiymatlari (O'QISH).

    Bandl `sellerSearchEnumValues({attrCode, page, size, search})` — dropdown
    ochilganda lazy chaqiriladi (`fetchEnumValues`).

    Jonli o'lchangan (2026-07-17, attrCode=filter_1687) element (`attributeEnums[]`):
        {id, code, value, localizable, localizedValue:{uz,ru}, isCustom,
         displayMetadata, ordering}
    ⚠️ Yorliq `localizedValue` da (ikki til inline). Saqlashda `code` yuboriladi.
    """
    from urllib.parse import quote

    def _page(p: int) -> list:
        url = (f"{_ASSORT_BASE}/attributes/enums?attrCode={quote(attr_code or '')}"
               f"&page={int(p)}&size={int(size)}&search={quote(search or '')}")
        res = _req("GET", url, lang=lang)
        if isinstance(res, dict):
            rows = res.get("attributeEnums")
            return rows if isinstance(rows, list) else []
        return res if isinstance(res, list) else []

    rows = _page(page)
    # ⚠️ BITTA SAHIFA YETMAYDI. JONLI XATO (2026-07-22): «Qurilma modeli»
    # (filter_4770) 1000 dan ko'p qiymatga ega — 1-sahifadan keyingi kodlar
    # keshda topilmay, jadvalda XOM KOD ko'rinardi («filter_value_672150»),
    # chunki yorliq izlash `code`ga fallback qiladi (bandl `g()` ham shunday).
    # Qidiruvsiz to'liq ro'yxatni yig'amiz; `search` berilganda Uzum allaqachon
    # filtrlab beradi, sahifalash shart emas.
    if not search:
        pages = 1
        while len(rows) and len(rows) % int(size) == 0 and pages < 12:
            nxt = _page(page + pages)
            if not nxt:
                break
            rows = rows + nxt
            pages += 1
    return rows


def save_filters(shop_id: str | int, product_id: int | str,
                 body: dict) -> dict:
    """POST /filters/product/{id} → 3-qadam xususiyatlarni saqlash (**YOZUVCHI**).

    Tana (bandl `editSkuFilters`): `{skuFilters, skuAttributeValues}`.
      · `skuAttributeValues` — [{skuId, attributes:[{attributeCode,
        attributeName(ru), attributeValue}]}]. `attributeValue` bandl `RE()`
        serializatori bilan quriladi (frontend), bo'sh bo'lsa `null`:
          enum/localizableEnum → {valueType, value: "<code>"}
          enumArray/localizableEnumArray → {valueType, value: ["<code>", ...]}
          numeric → {valueType, value, unitType, unit}
          boolean → {valueType, value}
      · `skuFilters` — eski filterId tizimi (odatda []).

    ⚠️ 400 XATO SHAKLI: `{"payload":[{in,path,msg}]}` (2-qadamdagi
    `response.data.error` SATRIDAN farq qiladi). Majburiy atribut bo'sh bo'lsa
    `msg:"Заполните значение"`.
    """
    return _req("POST", f"{_BASE}/{shop_id}/filters/product/{int(product_id)}",
                json_body=body, timeout=60)


# ── createProduct body quruvchisi (HAR shakli — YAGONA joy) ─────────


def filled_characteristic_count(characteristics: list[dict] | None) -> int:
    """Qiymatga ega defined-xususiyatlar soni (TUR AHAMIYATSIZ — rang ham sanaladi).

    JONLI dalil (QAT'IY probe 2026-07-19): qiymatli defined-char soni >2 bo'lsa
    createProduct `validation-failed-001` bilan rad etadi. Uzum SKU = 2-o'lchovli
    matritsa, shuning uchun eng ko'pi bilan 2 ta xususiyat qiymatga ega bo'ladi.
    - 2 razmer (rangsiz) → 201 · 3 razmer (rangsiz) → 400
    - rang + 1 razmer → 201 · rang + 2 razmer → 400
    - 12434 Braslet: rang+Длина+Обхват → 400 (ikkalasi NOT_REQUIRED!)

    ⚠️ `requiredType` «razmer»likni AJRATMAYDI — qoida sof SON (≤2). Oldingi
    `filled_size_system_count` FAQAT REQUIRED_ONE_OF_SIZE'ni sanardi → NOT_REQUIRED
    razmerlar (Bilaguzuk 12434, 12811) o'tib ketardi. Bitta xususiyat ICHIDA ko'p
    qiymat NORMAL (poyabzal 36/37/38 → 201) — QIYMATNI emas, qiymatli XUSUSIYAT
    sonini sanaymiz.

    Darvoza route'da (nt_create) qo'llanadi — «brauzerga ishonmaymiz» naqshi
    ([[project_noviy_tavar_size_constraint]]).
    """
    n = 0
    for c in characteristics or []:
        if not isinstance(c, dict):
            continue
        if c.get("values"):
            n += 1
    return n


# createProduct 400 kodlari → foydalanuvchi tiliga tushunarli xabar.
# HAMMASI JONLI probe bilan uchraган (2026-07-18) — [[project_noviy_tavar_createproduct_rules]]:
#   validation-failed-001                        → qiymatli xususiyat >2 (≤2 cap bilan oldi olindi)
#   category-defined-characteristics-missed      → rang yoki razmer majburiy, to'ldirilmagan
#   category-defined-characteristics-forbidden   → razmer bu kategoriyaga to'g'ri kelmaydi
#   bad-request-001                              → majburiy filtr (Бренд) yo'q
# ⚠️ Read-only signal YO'Q: forbidden'ni oldindan bilib bo'lmaydi, faqat shu 400.
_CREATE_ERROR_MESSAGES = {
    "category-defined-characteristics-forbidden":
        "Tanlangan razmer-tizim bu kategoriyaga to'g'ri kelmaydi — boshqa razmer tanlang.",
    "category-defined-characteristics-missed":
        "Kategoriya majburiy xususiyatni talab qiladi — rang va kamida bitta razmer tanlang.",
    "bad-request-001":
        "Majburiy filtr tanlanmagan (masalan «Бренд»).",
    "validation-failed-001":
        "Ko'pi bilan 2 ta xususiyat tanlash mumkin (masalan rang + o'lcham).",
    # 2-BOSQICH (sendSkuData) — JONLI 2026-07-18: o'lchovsiz SKU rad etiladi.
    "weight-and-size-characteristics-required-error":
        "Har SKU uchun vazn va o'lchamlarni (eni/bo'yi/uzunligi/vazn) to'ldiring.",
}


def explain_create_error(body: str) -> tuple[str, str]:
    """Portal 400 tanasidan ``(code, foydalanuvchi_xabari)`` ajratadi.

    ⚠️ Kod REGEX bilan olinadi (json.loads emas): NoviyTavarError tanani 500
    belgiga KESADI, bu esa uzun ko'p-xatoli tanada JSON'ni buzadi. `errors[0].code`
    esa tananing boshida — regex uni kesilgan tanadan ham topadi.

    Xabar `''` bo'lsa — kod noma'lum (chaqiruvchi eski _portal_error'ga tushadi).
    """
    m = _re.search(r'"code"\s*:\s*"([^"]+)"', body or "")
    code = m.group(1) if m else ""
    return code, _CREATE_ERROR_MESSAGES.get(code, "")


def explain_filter_error(body: str) -> str:
    """3-BOSQICH (save-filters) 400 → foydalanuvchi xabari.

    ⚠️ Bu shakl createProduct'nikidan BOSHQA (JONLI 2026-07-18):
        {"payload":[{"in":"body","path":"skus.<id>.attributes.<code>",
                     "msg":"Qiymatni to'ldiring"}, ...]}
    Har element — bo'sh MAJBURIY skuAttribute. Kod nomlari texnik
    (handcrafted/gender/ring_material) — foydalanuvchiga umumiy, aniq harakat
    beramiz. `"msg"` mavjudligi shu shaklga xos (createProduct `"message"` beradi),
    kesilgan tanada ham ishlaydi. [[project_noviy_tavar_createproduct_rules]]
    """
    if body and '"msg"' in body:
        return "Majburiy xususiyatlar to'ldirilmagan — «Свойства» bo'limini to'liq to'ldiring."
    return ""


def _html_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _desc_html(s: str) -> str:
    """Oddiy matn → portal kutayotgan <p>...</p> HTML (qatorlar bo'yicha)."""
    lines = [ln.strip() for ln in (s or "").splitlines()]
    lines = [ln for ln in lines if ln]
    if not lines:
        return ""
    return "".join(f"<p>{_html_escape(ln)}</p>" for ln in lines)


# Rich-text muharrir (contenteditable) chiqaradigan HTML uchun oq-ro'yxat
# sanitayzeri — Uzum tavsifi faqat oddiy matn belgilashini kutadi (portal
# HAR'da <p>/<b>/<i> ko'rindi). bleach kutubxonasi yo'q, shu sabab minimal
# regex-asosli tozalash: ruxsat etilgan teglar qoldiriladi, boshqasi olib
# tashlanadi, hamma atributlar (on*, style, href javascript:) yo'q qilinadi.
import re as _re

_ALLOWED_TAGS = {"p", "b", "strong", "i", "em", "u", "br", "ul", "ol", "li"}
_TAG_RE = _re.compile(r"</?([a-zA-Z0-9]+)[^>]*>")


def _sanitize_desc_html(s: str) -> str:
    """Contenteditable HTML'ni xavfsiz oq-ro'yxatga tushiradi.

    Ruxsat: p, b/strong, i/em, u, br, ul, ol, li — atributsiz. Bo'sh yoki
    faqat teglar bo'lsa '' qaytaradi. Tegsiz kelsa _desc_html'ga tushadi.
    """
    s = (s or "").strip()
    if not s:
        return ""
    if "<" not in s:
        return _desc_html(s)

    # Xavfli bloklarni butunlay (matni bilan) olib tashlash.
    s = _re.sub(r"(?is)<(script|style)\b.*?</\1>", "", s)

    def _rep(m):
        tag = m.group(1).lower()
        # contenteditable qatorlarni <div> bilan o'raydi — portal <p> kutadi.
        if tag == "div":
            tag = "p"
        if tag not in _ALLOWED_TAGS:
            return ""
        closing = m.group(0).startswith("</")
        return f"</{tag}>" if closing else f"<{tag}>"

    out = _TAG_RE.sub(_rep, s)
    # &nbsp; kabi bo'sh-joylarni tozalash, ketma-ket bo'sh <p> larni yig'ish
    out = out.replace("&nbsp;", " ")
    out = _re.sub(r"<p>\s*</p>", "", out)
    return out.strip()


def build_create_body(*, category_id: int,
                      title_uz: str, title_ru: str,
                      short_uz: str, short_ru: str,
                      desc_uz: str, desc_ru: str,
                      filter_values_sel: list[dict],
                      characteristics_sel: list[dict],
                      images: list[dict],
                      product_fields: dict,
                      desc_is_html: bool = False,
                      color_images: list[dict] | None = None,
                      color_videos: list[dict] | None = None,
                      color_collections: list[dict] | None = None,
                      video: dict | None = None,
                      image_collection: dict | None = None,
                      care_uz: str = "", care_ru: str = "",
                      certificates: list[dict] | None = None,
                      comments_sel: list[dict] | None = None) -> dict:
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
    custom = []
    for ch in characteristics_sel or []:
        vals = [v for v in (ch.get("values") or []) if isinstance(v, dict)]
        if not vals:
            continue
        # ── MAXSUS (foydalanuvchi yaratgan) xususiyat ──────────────────────
        # DALIL (Uzum bandli, `it()` submit-yig'uvchisi):
        #   definedCharacteristics = tanlanganlarning `defined` bo'lganlari
        #   customCharacteristics  = qolgani, `orderingNumber: 100 + indeks`
        # Bandl `ne()` maxsus xususiyatga characteristicId BERMAYDI, faqat
        # `custom: true` + characteristicTitle + characteristicValues.
        # Chegara: 3 tadan ko'p bo'lsa Uzum saqlashda xato beradi
        # (errors.limiting_number_of_custom_characteristics) — UI ham to'sadi.
        if ch.get("custom"):
            custom.append({
                "orderingNumber": 100 + len(custom),
                "characteristicValues": vals,
                "characteristicTitle": ch.get("characteristicTitle") or {},
                "custom": True,
            })
            continue
        # ⚠️ JONLI ETALON (t8.har, 2026-07-18 — Uzumda yasagan haqiqiy karta):
        # createProduct definedCharacteristics `defined:true` yuboradi; REQUIRED
        # (rang) uchun qo'shimcha `fillType:"REQUIRED"` + `isRequired:true`.
        # `requiredType`/`flowA` YUBORILMAYDI (eski HAR'larда bor edi — Uzum
        # ikkalasini ham qabul qiladi; 1:1 uchun t8 shaklini beramiz).
        # `orderingNumber` — xususiyatning O'Z tartibi (rang=0, Длина=44), QATOR
        # INDEKSI EMAS (frontend meta'dan uzatadi). [[project_noviy_tavar_createproduct_rules]]
        entry = {
            "orderingNumber": int(ch.get("orderingNumber") or 0),
            "characteristicValues": vals,
            "characteristicTitle": ch.get("characteristicTitle") or {},
            "characteristicId": ch.get("characteristicId", -1),
            "defined": True,
        }
        if (ch.get("requiredType") or "") == "REQUIRED":
            entry["fillType"] = "REQUIRED"
            entry["isRequired"] = True
        defined.append(entry)

    # productFields — {WARRANTY: <oy>} kabi. Bandl WARRANTY'ni INTEGER yuboradi;
    # bo'sh/string qiymatlarni tozalab, sonli holatga o'giramiz (brauzer allaqachon
    # shunday yuboradi, lekin write endpoint uchun ishonmay tozalaymiz).
    clean_fields: dict = {}
    for k, v in (product_fields or {}).items():
        if v in (None, ""):
            continue
        if k == "WARRANTY":
            try:
                clean_fields[k] = int(v)
            except (TypeError, ValueError):
                continue
        else:
            clean_fields[k] = v

    prod_images = [
        {"deletable": True, "url": im["url"], "key": im["key"], "status": "ACTIVE"}
        for im in (images or []) if im.get("key") and im.get("url")
    ]

    fvals = [
        {"filterId": int(fv["filterId"]), "filterValueId": int(fv["filterValueId"])}
        for fv in (filter_values_sel or [])
        if fv.get("filterId") is not None and fv.get("filterValueId") is not None
    ]

    # Har-rang media (HAR shakli: color{uz,ru} + colorImage(key) + imageUrl
    # + ordering + status + deletable). Rang bo'yicha ketma-ketlik saqlanadi.
    col_imgs = []
    for ci in (color_images or []):
        if not (ci.get("key") and ci.get("url") and isinstance(ci.get("color"), dict)):
            continue
        col_imgs.append({
            "color": {"uz": ci["color"].get("uz", ""), "ru": ci["color"].get("ru", "")},
            "colorImage": ci["key"],
            "imageUrl": ci["url"],
            "ordering": int(ci.get("ordering") or 0),
            "status": "ACTIVE",
            "deletable": True,
        })
    # ── Har-rang video / 360 ────────────────────────────────────────
    #
    # ⚠️ DALIL KUCHI: bu shakllar BANDL KODIDAN (3-daraja), tarmoq yozuvidan
    # EMAS — 236 HAR / 6576 yozuvda `colorVideos` va `colorCollectionImages`
    # 0/235 to'ldirilgan. Yuklash endpointlarining O'ZI jonli tasdiqlangan
    # (2026-07-18), lekin bu kalitlar bilan createProduct SINALMAGAN.
    #
    # Manba — ColorMedia store (chunk-6dbbb9d8 @44131), so'zma-so'z:
    #   colorVideosChange(key, url, color) ->
    #       {color, status: ACTIVE, deletable: true, videoUrl: url, videoKey: key}
    #   colorCollectionsChange(id, preview, color) ->
    #       {color, status: ACTIVE, deletable: true, collectionId: id}
    #
    # `color` — xususiyat qiymatining `title` DICT'i: store'ning `E(t, r)`
    # funksiyasi `color: r.title` yozadi (`r` = qiymat obyekti). Ya'ni
    # colorImages bilan bir xil shakl — bu avval «xulosa» edi, endi kodda aniq.
    col_vids = []
    for cv in (color_videos or []):
        if not (cv.get("videoKey") and cv.get("videoUrl")
                and isinstance(cv.get("color"), dict)):
            continue
        col_vids.append({
            "color": {"uz": cv["color"].get("uz", ""), "ru": cv["color"].get("ru", "")},
            "status": "ACTIVE",
            "deletable": True,
            "videoUrl": cv["videoUrl"],
            "videoKey": cv["videoKey"],
        })

    col_cols = []
    for cc in (color_collections or []):
        cid = cc.get("collectionId")
        if not (isinstance(cid, int) and isinstance(cc.get("color"), dict)):
            continue
        col_cols.append({
            "color": {"uz": cc["color"].get("uz", ""), "ru": cc["color"].get("ru", "")},
            "status": "ACTIVE",
            "deletable": True,
            "collectionId": cid,
        })

    # Umumiy video — bandl (chunk-6dbbb9d8, videoUploadHandler `We`):
    #   P.value.video = {deletable: true, status: ACTIVE, url: "", key: ""}
    vid = None
    if isinstance(video, dict) and video.get("key") and video.get("url"):
        vid = {"deletable": True, "status": "ACTIVE",
               "url": video["url"], "key": video["key"]}

    # Umumiy 360 — bandl (editProductCard/Photo360 store @58913):
    #   updateProductCollection(id) -> {status: ACTIVE, deletable: true,
    #                                   collectionId: id}  (yoki null)
    img_col = None
    if isinstance(image_collection, dict):
        cid = image_collection.get("collectionId")
        if isinstance(cid, int):
            img_col = {"status": "ACTIVE", "deletable": True, "collectionId": cid}

    # ── Ixtiyoriy bo'limlar → comments[] ────────────────────────────
    #
    # DALIL (Uzum portal bandli, editProductCard/Comments store): comment
    # turlari — «Состав», «Размеры», «Инструкция», «Сертификация».
    # ⚠️ Kalitlar UI yorlig'idan FARQ QILADI: «Размерная сетка» → «Размеры»,
    # «Сертификаты» → «Сертификация». Tarmoq HAR'ida bu dalil YO'Q edi —
    # hech bir createProduct tanasi comments'ni to'ldirmagan.
    #
    # `comments_sel` berilsa (brauzer 4 ta bo'limni ham yuboradi) — o'sha
    # ishlatiladi. Berilmasa eski xatti-harakat: faqat care → «Инструкция».
    if comments_sel is not None:
        comments = []
        for c in comments_sel:
            if not isinstance(c, dict):
                continue
            ctype = str(c.get("commentType") or "").strip()
            if ctype not in _COMMENT_TYPES:
                continue          # noma'lum turni Uzum'ga yubormaymiz
            body = c.get("comment") or {}
            uz = str(body.get("uz") or "").strip()
            ru = str(body.get("ru") or "").strip()
            if not uz and not ru:
                continue          # bo'sh bo'limni yubormaymiz
            # Uzum `fillEmptyLocaleComments`: yo'q locale "" bilan to'ldiriladi.
            comments.append({"comment": {"ru": ru, "uz": uz}, "commentType": ctype})
    else:
        comments = [{"comment": {"ru": "", "uz": ""}, "commentType": "Инструкция"}]
        if (care_uz or "").strip() or (care_ru or "").strip():
            comments = [{
                "comment": {"ru": (care_ru or care_uz or ""), "uz": (care_uz or care_ru or "")},
                "commentType": "Инструкция",
            }]

    desc_fn = _sanitize_desc_html if desc_is_html else _desc_html

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
        "colorImages": col_imgs,
        "colorCollectionImages": col_cols,
        "colorVideos": col_vids,
        "comments": comments,
        "customCharacteristics": custom,
        "dateModerated": None,
        "definedCharacteristics": defined,
        "description": {"ru": desc_fn(desc_ru), "uz": desc_fn(desc_uz)},
        "filterValues": fvals,
        "filters": [],
        "imageCollection": img_col,
        "okpd2": None,
        "hasAnySkuBlocked": False,
        "photoOnPreview": False,
        "productFields": clean_fields,
        "productImages": prod_images,
        "productCertificates": list(certificates or []),
        "ratingInfo": None,
        "shortDescription": {"ru": short_ru or "", "uz": short_uz or ""},
        "skuBlockReason": None,
        "title": {"ru": title_ru or "", "uz": title_uz or ""},
        "video": vid,
        "skuList": [],
        "switchbackActive": False,
    }


# ── 6b. Yangilash (MAVJUD karta) ─────────────────────────────────────
#
# Tana `build_create_body` bilan bir xil — bandl saqlashda AYNAN bitta
# obyektni ikkala endpointga yuboradi (chunk-6dbbb9d8 @79125). Farqi:
#   · `id` — qaysi kartani yangilash (store `Oe` klassidagi maydon)
#   · kartaning O'Z holati (SKU ro'yxati, moderatsiya izlari) tanada
#     SAQLANIB qolishi kerak — portalda ular store'ga GET'dan yuklangan
#     bo'ladi. Biz esa tanani formadan quramiz, shuning uchun ularni
#     joriy kartadan KO'CHIRAMIZ.
#
# ⚠️ `skuList` eng muhimi: bo'sh ro'yxat yuborsak, 2-qadamda yaratilgan
# SKU'lar yo'qolib ketishi mumkin. Joriy karta berilmasa — funksiya
# yangilashni bajarmaydi (routes uni majburiy oldindan o'qiydi).
_EDIT_CARRY_KEYS = (
    "skuList", "createdByFlowB", "dateModerated", "blockReason",
    "blockReasons", "blockComment", "hasAnySkuBlocked", "okpd2",
    "allFiltersFilled", "categoryEditable", "switchbackActive",
)


def build_edit_body(*, product_id: int, current: dict, **create_kwargs) -> dict:
    """`editProduct` tanasi = create tanasi + `id` + joriy kartadan ko'chirmalar.

    ``current`` — `get_product()` javobi (GET /product?productId=X).
    """
    body = build_create_body(**create_kwargs)
    body["id"] = int(product_id)
    for key in _EDIT_CARRY_KEYS:
        if isinstance(current, dict) and key in current:
            body[key] = current[key]
    return body
