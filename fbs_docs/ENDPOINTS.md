# Uzum Seller OpenAPI — FBS / DBS endpointlari

Manba: Uzum seller cabinet'dagi swagger sahifasi (2026-05-21 holati).
Base URL: `https://api-seller.uzum.uz/api/seller-openapi`
Auth header: `Authorization: <raw_token>` (Bearer prefix YO'Q)

---

## 📥 GET endpointlar (ma'lumot olish)

| Endpoint | Tavsif | Bosqich |
|---|---|---|
| `GET /v2/fbs/orders` | Sotuvchi buyurtmalari ro'yxati | 1 |
| `GET /v2/fbs/orders/count` | Buyurtmalar soni (single-status, FBS faqat) | 1 |

### Count detali (swagger'dan, 2026-05-21)

```
GET /v2/fbs/orders/count

Query params:
  shopIds  — array<integer>, query (required-marker ko'rinmaydi, lekin majburiy)
  status   — string, single value. Default: CREATED. 
             Enum: CREATED, PACKING, PENDING_DELIVERY, DELIVERING, DELIVERED,
                   ACCEPTED_AT_DP, DELIVERED_TO_CUSTOMER_DELIVERY_POINT, 
                   COMPLETED, CANCELED, PENDING_CANCELLATION, RETURNED
  dateFrom — integer($int64), optional (epoch?)
  dateTo   — integer($int64), optional

  ❗ NO scheme parametri — count faqat FBS uchun (swagger description: 
     "Возвращает количество заказов FBS")

Response 200:
  { "payload": <int>, "errors": [...], "timestamp": "...", ... }

  ⚠️ payload bitta integer. 11 ta status uchun 11 marta chaqirish kerak.
```
| `GET /v1/fbs/order/{orderId}` | Bitta buyurtma ma'lumoti | 1 |

### Order detail (swagger'dan, 2026-05-21)

```
GET /v1/fbs/order/{orderId}

Path params:
  orderId — integer($int64), REQUIRED

Query params: yo'q
Headers:    standart (Authorization, Accept)

Response 200:
  {
    "payload": { <order> },     // ⚠️ /v2/fbs/orders payload.orders[0] bilan AYNAN bir xil shape
    "errors": [...],
    "timestamp": "...",
    "trace": "...",
    "error": "null"
  }

Response 400: standart {payload: {}, errors: [{code, message, payload}], ...}
```

**Field ro'yxati (`payload` ichida):**
- `id`, `status` (11 enum), `scheme` (FBS/DBS), `price`, `shopId`, `cancelReason`, `identifierRequired`
- `dateCreated`, `acceptUntil`, `deliverUntil`, `deliveringDate`, `deliveryDate`, `acceptedDate`, `deliveredToDeliveryPointDate`, `completedDate`, `dateCancelled`, `returnDate` — ISO 8601 sanalar
- `stock`: `{id, externalId, title, address, timeFrom, timeTo, poolSource, dimensionalGroups[]}`
- `orderItems[]`: har element — `{id, status, orderId, skuTitle, productTitle, productId, productImage{photo, photoKey, color, hasVerticalPhoto}, amount, sellerPrice, cancelled, sellerProfit, commission, comment, skuCharTitle, skuCharValue, ...}`
- `place`, `invoiceNumber`
- `timeSlot`: `{timeFrom, timeTo}`
- `dropOffPoint`: `{uuid, address, type}` (`type` = "ISSUE_POINT")
- `deliveryInfo`: `{deliveryAddress, customerFullname, customerPhone, deliveryComment}`

> ⚠️ **Scope check**: bu endpoint shopIds qabul qilmaydi — javobdan keyin `payload.shopId` ni user'ning ruxsat etilgan do'konlari bilan tekshirish kerak (fbs/routes.py allaqachon shu qiladi).
| `GET /v1/fbs/order/return-reasons` | Qaytarma sabablari (enum) | 1 |
| `GET /v1/fbs/order/{orderId}/labels/print` | FBS etiketkasi (PDF, Base64 array) | 2 |

### Labels/print detali (swagger'dan, 2026-05-21) — Bosqich 2 uchun saqlangan

```
GET /v1/fbs/order/{orderId}/labels/print

Path params:
  orderId — integer(int64), REQUIRED

Query params:
  size — string, REQUIRED. Available: LARGE (58×40mm), BIG (43×25mm). Default: LARGE.

Response 200:
  {
    "payload": { "document": ["string", ...] },  // PDF Base64 (yoki link?)
    "errors": [...], "timestamp": "...", "trace": "...", "error": "null"
  }

Error codes (HTTP 400):
  seller-order-01 — Seller order not found
  seller-order-14 — Label service unavailable, try later
  seller-order-15 — Customer order identifiers are missing
```

> ⚠️ `payload.document` array — har element nima ekanligi (Base64 PDF, URL, yoki sahifa raqami) sinab ko'rilishi kerak. seller-order-15 xato kodi shuni anglatadiki, etiketka chiqarish uchun avval `/v1/fbs/order/{id}/identifier` chaqirilgan bo'lishi kerak (IMEI biriktirilgan).

> ❗ **Diqqat:** Aniq query parametrlari va response shape'lari swagger'ning har endpoint blokini ochib (`^` strelka) ko'rish kerak. Bu hujjat tepa-yuqori ro'yxat. Har bosqich boshlanganda kerakli endpoint detali user'dan so'raladi.

---

## 📤 POST endpointlar (action — state o'zgartirish)

### FBS actions

| Endpoint | Tavsif | Bosqich |
|---|---|---|
| `POST /v1/fbs/order/{orderId}/confirm` | Buyurtmani tasdiqlash (sotuvchi roziligi) | 2 |
| `POST /v1/fbs/order/{orderId}/cancel` | Buyurtmani bekor qilish | 2 |
| `POST /v1/fbs/order/{orderId}/identifier` | Tovarlarga IMEI/seriya raqamlarini biriktirish | 2 |

### DBS actions

| Endpoint | Tavsif | Bosqich |
|---|---|---|
| `POST /v1/dbs/order/{orderId}/delivering` | DBS buyurtmani yetkazib berishga jo'natish | 3 |
| `POST /v1/dbs/order/{orderId}/completed` | DBS buyurtma yetkazib berildi (mijoz qabul qildi) | 3 |
| `POST /v1/dbs/order/{orderId}/refund` | DBS buyurtma bo'yicha qaytarma yaratish | 3 |

---

## 🔍 Tushuncha: FBS vs DBS

| Xususiyat | FBS | DBS |
|---|---|---|
| **Saqlash** | Sotuvchi omborida | Sotuvchi omborida |
| **Etiketka** | Uzum chiqaradi (`/labels/print`) | Sotuvchi o'zi chop etadi |
| **Yetkazib berish** | Uzum kuryeri | Sotuvchi o'zi |
| **Yakunlash** | Uzum avtomatik | Sotuvchi qo'lda (`/completed`) |
| **OTP kod** | — | Ehtimol kerak (yakunlash uchun) |

`/v2/fbs/orders` ehtimol ikkala turni ham qaytaradi — buyurtma `type` maydoni bilan farqlanadi (`FBS` yoki `DBS`).

---

## 📝 Status enum'lari (taxminiy — swagger'dan tasdiqlanadi)

`debug_routes.py:2277` ga ko'ra browser API'da bor:

```
statuses: CREATED, SENT, IN_PROGRESS, MOVED_TO_DELIVERY, ASSEMBLED, COMPLETED, UTILIZED
types:    FBS, DEFECTED, RETURN
```

OpenAPI'da xuddi shu enum bo'lishi mumkin yoki o'zgargan bo'lishi mumkin — 1-bosqichda aniqlanadi.

### Taxminiy status ketma-ketligi

**FBS:**
```
CREATED → CONFIRMED → ASSEMBLED → SENT → IN_PROGRESS → MOVED_TO_DELIVERY → COMPLETED
                   ↘ CANCELLED
                                                        ↘ UTILIZED (qaytmagan)
```

**DBS:**
```
CREATED → CONFIRMED → DELIVERING → COMPLETED
                                 ↘ REFUND
```

---

## 🔗 Bog'liq fayllar (mavjud kod)

- `core/uzum_openapi.py` — OpenAPI klient (shu yerda yangi funksiyalar yoziladi)
- `core/uzum_finance_openapi.py` — finance endpointlari (namuna sifatida ishlatish)
- `admin/routes.py:193` — `/v1/shops` probe namuna
- `debug_routes.py:2271+` — browser-token bilan return'larni olish (eski yo'l)
