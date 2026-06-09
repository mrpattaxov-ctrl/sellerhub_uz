# FBS / DBS tizimi — 5 bosqichli plan

SellerHub'ga to'liq FBS va DBS buyurtma boshqaruv tizimini qo'shish. Har bosqich oldingisiga tayanadi — tartib qattiq.

---

## 🔵 Bosqich 1 — KO'RISH (read-only)

**Maqsad:** Sotuvchi FBS/DBS buyurtmalarni SellerHub'da ko'ra olsin. Hech narsani o'zgartirmaydi.

**Uzum endpointlari:**
- `GET /v2/fbs/orders` — buyurtmalar ro'yxati
- `GET /v2/fbs/orders/count` — har status uchun soni (badge'lar)
- `GET /v1/fbs/order/{orderId}` — bitta buyurtma detali
- `GET /v1/fbs/order/return-reasons` — qaytarma sabablari (keyingi bosqich uchun zaxira)

**Yaratilishi/o'zgartirilishi kerak:**
- `core/uzum_openapi.py` — `fetch_fbs_orders_page()`, `fetch_fbs_orders_count()`, `fetch_fbs_order_detail()`, `extract_fbs_orders_list()`
- `fbs/routes.py` — `/fbs`, `/fbs/<id>`, `/fbs/api/orders`, `/fbs/api/count`, `/fbs/api/order/<id>`
- `templates/fbs_orders.html` — yangilash (mavjud)
- `templates/fbs_order_detail.html` — yangi
- `static/uzum_ui.js` — sidebar link (qo'shildi)
- `app.py` — blueprint register (qo'shildi)
- `dark.md` — changelog

**Tugagan hisoblanadi:** `/fbs` sahifa yangi buyurtmalarni Uzum'dan ko'rsatadi, status badge'lari ishlaydi, har buyurtmaga klik qilib detallar ochiladi.

**Holat:** ✅ **TO'LIQ TUGADI (2026-05-21).** Barcha 4 endpoint ulandi:
- ✅ `GET /v2/fbs/orders` — ro'yxat (real schema, klikli qatorlar)
- ✅ `GET /v2/fbs/orders/count` — 11 ta status chip badge sonlari (parallel fetch)
- ✅ `GET /v1/fbs/order/{orderId}` — detail sahifa (Mijoz, Sanalar, Yetkazib berish, Ombor, Mahsulotlar kartalari)
- ⏳ `GET /v1/fbs/order/return-reasons` — Bosqich 2 uchun zaxira (qaytarma sabablari dropdown'i)

---

## 🟢 Bosqich 2 — FBS ish jarayoni (yig'ish-jo'natish)

**Maqsad:** Sotuvchi SellerHub'dan chiqmasdan FBS buyurtmani jo'natishga tayyorlay olsin.

**Uzum endpointlari (POST/GET — state changes):**
- `POST /v1/fbs/order/{orderId}/confirm` — "Qabul qilaman" (sotuvchi rozi)
- `POST /v1/fbs/order/{orderId}/cancel` — bekor qilish + sabab
- `POST /v1/fbs/order/{orderId}/identifier` — IMEI/seriya raqamlari biriktirish
- `GET /v1/fbs/order/{orderId}/labels/print` — etiketka PDF (binary)

**UI:**
- Detail sahifada status'ga qarab katta tugmalar:
  - `CREATED` → ko'k "Tasdiqlash" + qizil "Bekor qilish"
  - `CONFIRMED`/`ASSEMBLED` → yashil "Etiketka chop etish"
  - Telefon/elektronika → "IMEI biriktirish" formasi
- Bekor qilish modal — sabab dropdown (return-reasons'dan)
- Double-click himoyasi, loading state, toast notification

**Tugagan hisoblanadi:** Sotuvchi yangi buyurtmani tasdiqlay oladi, etiketka chiqaradi, kerak bo'lsa IMEI kiritadi, yoki bekor qiladi. Uzum cabinet'ga umuman kirmaydi.

**Holat:** ⏳ Boshlanmadi

---

## 🟡 Bosqich 3 — DBS ish jarayoni (siz yetkazasiz)

**Maqsad:** Sotuvchi o'zi yetkazadigan buyurtmalar uchun status tugmalari.

**Uzum endpointlari (POST'lar):**
- `POST /v1/dbs/order/{orderId}/delivering` — "Yetkazishga oldim" (yo'lda)
- `POST /v1/dbs/order/{orderId}/completed` — "Mijoz oldi" (yakunlandi)
- `POST /v1/dbs/order/{orderId}/refund` — qaytarma yaratish

**UI:**
- `type=DBS` buyurtma sahifasida FBS tugmalari o'rniga DBS workflow:
  - `CONFIRMED` → "Yetkazishga olaman"
  - `DELIVERING` → "Yetkazib berildi" (OTP kod bo'lishi mumkin)
  - `COMPLETED` → "Qaytarma yaratish"
- Sidebar — bitta `/fbs?type=DBS` filtri yoki alohida `/dbs` link

**Tugagan hisoblanadi:** Sotuvchi DBS buyurtmani "yo'lda" → "yetkazildi" zanjirini SellerHub'dan boshqaradi. Refund ham ishlaydi.

**Holat:** ⏳ Boshlanmadi

---

## 🟣 Bosqich 4 — DB keshlash + worker

**Maqsad:** Tezlik + tarix + offline ko'rish. Hozir har sahifa Uzum API'ga uradi.

**Yangi modellar:**
- `FbsOrder` jadvali — `shop_id`, `order_id` (PK), `order_type` (FBS/DBS), `status`, `customer_*`, `total`, `items_json` (JSONB), `synced_at`, `raw_json`
- Indekslar: `(shop_id, status, created_at DESC)`, `(shop_id, order_type)`

**Worker:**
- Har 5 daqiqada barcha do'konlar uchun `GET /v2/fbs/orders` → `DELETE+INSERT` idempotent (mavjud `sales_lines` namunasi)
- Per-shop lock (`core/shop_lock.py`)
- `ShopSyncState`'ga yangi maydon yoki yangi jadval (`last_fbs_sync_at`)

**O'zgarishlar:**
- `fbs/routes.py` — `/fbs/api/orders` default'da DB'dan o'qiydi, `?refresh=1` parametri bilan Uzum'ga uradi
- UI'da "Oxirgi yangilangan: 2 daqiqa oldin" + "Uzum'dan tortib olish" tugmasi

**Tugagan hisoblanadi:** `/fbs` sahifa 100ms'da ochiladi (DB), "Yangilash" tugmasi Uzum'ga uradi.

**Holat:** ⏳ Boshlanmadi

---

## 🔴 Bosqich 5 — Telegram bildirishnomalari

**Maqsad:** Yangi buyurtma kelganda telefon ovoz beradi.

**Yangi xabar turlari:**
- 🆕 Yangi `CREATED` buyurtma → darrov xabar (sync tick'idan keyin)
- ⏰ 30 daqiqadan ortiq tasdiqlanmagan → eslatma
- 🔄 Qaytarma so'rovi → urgent

**Yangi modellar:**
- `NotificationSettings`'ga maydonlar: `fbs_new_order_enabled`, `fbs_unconfirmed_reminder_minutes`, `fbs_return_request_enabled`
- `fbs_notifications_sent` jadvali (duplicate'larni oldini olish)

**Format namunasi:**
```
🆕 Yangi FBS buyurtma!
№12345678 · Shop: MyShop
Mijoz: Ali Valiev (+998901234567)
SKU: 2 ta · Summa: 350 000 so'm
👉 SellerHub'da ko'rish
```

**Tugagan hisoblanadi:** Yangi buyurtma → 5 daqiqa ichida Telegram'da xabar.

**Holat:** ⏳ Boshlanmadi

---

## Qoidalar (har bosqichga taalluqli)

1. **Til:** matn o'zbekcha (RU/UZ — `translations.py` ko'rib chiq)
2. **Dark mode:** `dark.md`'ni har sahifa qo'shilganda yangila
3. **Docker rebuild:** static/template o'zgargandan keyin `docker compose up -d --build`
4. **Token:** har doim `User.uzum_openapi_token`'dan o'qiladi (admin token YO'Q)
5. **Auth header:** `Authorization: <raw_token>` (Bearer prefix yo'q — 2026-05-20 tasdiqlandi)
6. **Xato ishlovi:** 4xx → aniq xabar foydalanuvchiga; 5xx → "Uzum vaqtincha ishlamayapti"
7. **Cache patterni:** mavjud `core/swr.py`, `core/redis_client.py` ishlatish
8. **Per-shop lock:** parallel race'larni `core/shop_lock.py` bilan oldini olish

---

## Hozirgi promtlar uchun → [PROMPTS.md](PROMPTS.md)
