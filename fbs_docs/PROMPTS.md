# FBS / DBS — yangi chat uchun copy-paste promtlar

Har bosqichni alohida chatda boshlang. Promtni tanlab copy qiling, yangi Claude Code chat'iga paste qiling.

---

## 📋 Boshlang'ich kontekst bloki (ixtiyoriy)

Agar yangi chatda kontekst yo'q bo'lsa (`/clear` qilingan yoki birinchi marta), avval shu bilan boshlang:

````
Loyiha: SellerHub.uz — Uzum marketplace sotuvchilari uchun Flask SaaS.
Stack: Flask + SQLAlchemy + PostgreSQL + Redis + Bootstrap 5.3 + Alembic + Docker.
Kataloglar: admin/, auth/, core/, finance/, payments/, pos/, products/, telegram/,
warehouse/, fbs/ (yangi), background/, legacy/. Entrypoint: app.py.

Uzum Seller OpenAPI:
- Base: https://api-seller.uzum.uz/api/seller-openapi
- Auth: header "Authorization: <raw_token>" (Bearer prefix YO'Q — admin tasdiqladi 2026-05-20)
- Token manbai: User.uzum_openapi_token (per-user, /fetch sahifasida kiritiladi)
- Mavjud klient: core/uzum_openapi.py — _clean(), _AUTH_VARIANTS, _try_request() helperlar
- Token yo'q user uchun: 400 + "open /fetch and paste token" xabari

UI qoidalari:
- Bootstrap 5.3 + dark mode (qarang dark.md — barcha yangi sahifa shunda yozilishi kerak)
- Til: o'zbekcha matn afzal (user Abdulaziz, o'zbek)
- Static/template o'zgarishidan keyin: docker compose up -d --build (mount yo'q)

Plan: fbs_docs/PLAN.md ichida 5 bosqichli yo'l xaritasi bor.
Endpointlar: fbs_docs/ENDPOINTS.md.
Til: javoblarni o'zbekcha yoz. Texnik terminlar ingliz bo'lishi mumkin.
````

---

## 🔵 BOSQICH 1 — FBS buyurtmalarni KO'RISH

````
SellerHub.uz loyihasida FBS buyurtmalar ko'rish sahifasini to'g'rilash va kengaytirish.

KONTEKST: Oldingi chat'da men taxminiy URL'lar bilan boshlangan kod yozgan edim —
core/uzum_openapi.py'da fetch_fbs_orders_page() 5 ta noto'g'ri URL probe qiladi.
Endi haqiqiy endpoint'lar ma'lum (Uzum swagger'idan):

- GET /v2/fbs/orders          — buyurtmalar ro'yxati
- GET /v2/fbs/orders/count    — har status uchun soni
- GET /v1/fbs/order/{orderId} — bitta buyurtma detali
- GET /v1/fbs/order/return-reasons — qaytarma sabablari (enum)

VAZIFA:
1. core/uzum_openapi.py — fetch_fbs_orders_page() ichidagi _fbs_order_url_variants
   probe ro'yxatini OLIB TASHLA. Bitta to'g'ri URL qoldir: /v2/fbs/orders.
   Query parametrlarini foydalanuvchi swagger screenshot orqali aniqlasin —
   undan oldin user'dan param ro'yxati va response namunasini so'ra.

2. fbs/routes.py — mavjud /fbs/api/orders endpoint'ini yangi shape'ga moslash.
   Yangi qo'sh:
   - GET /fbs/api/count — /v2/fbs/orders/count proxy (status badge'lari uchun)
   - GET /fbs/<order_id> — detail sahifasi
   - GET /fbs/api/order/<order_id> — JSON detail

3. templates/fbs_orders.html — status badge'lari ro'yxati tepasida
   (CREATED: N | ASSEMBLED: M ...), AJAX bilan /fbs/api/count chaqiradi.

4. templates/fbs_order_detail.html — yangi sahifa, buyurtmaning to'liq
   ma'lumotlari: mijoz, manzil, SKU'lar jadvali, status tarixi.

5. dark.md — yangi sahifa qoidalarini yangila (changelog'ga 2026-mm-dd qator).

QILMA:
- POST endpointlarini bu bosqichda QO'SHMA (faqat ko'rish)
- DB keshlash YO'Q — har request Uzum'ga uradi (4-bosqichda qo'shamiz)
- DBS endpointlarini ham QO'SHMA (3-bosqich)

AVVAL: user'dan swagger'da "GET /v2/fbs/orders" va "GET /v1/fbs/order/{orderId}"
bloklarining query parametrlari + response namunasini so'rab ol.
Aniq field nomlari bo'lmaguncha pick(o, [...]) fallback ishlatma.

TUGAGAN HISOBLANADI: /fbs sahifasi yangi buyurtmalarni Uzum'dan ko'rsatadi,
status badge'lari ishlaydi, har buyurtmaga klik qilib detallar ochiladi.
````

---

## 🟢 BOSQICH 2 — FBS ish jarayoni (yig'ish-jo'natish)

````
SellerHub.uz loyihasida FBS buyurtmalar uchun action tugmalarini qo'shish.

KONTEKST: 1-bosqich tugagan — /fbs ro'yxat va /fbs/<id> detail sahifalari ishlaydi.
Endi sotuvchi SellerHub'dan chiqmasdan buyurtmani jo'natishga tayyorlay olsin.

Uzum endpointlari (action — POST/GET):
- POST /v1/fbs/order/{orderId}/confirm    — "Qabul qilaman" (sotuvchi roziligi)
- POST /v1/fbs/order/{orderId}/cancel     — bekor qilish + sabab
- POST /v1/fbs/order/{orderId}/identifier — IMEI/seriya raqamlari biriktirish
- GET  /v1/fbs/order/{orderId}/labels/print — etiketka PDF (binary stream)

VAZIFA:
1. core/uzum_openapi.py — yangi funksiyalar:
   - confirm_fbs_order(token, order_id) -> dict
   - cancel_fbs_order(token, order_id, reason_code, comment) -> dict
   - attach_fbs_identifiers(token, order_id, items) -> dict
   - download_fbs_label(token, order_id) -> tuple[bytes, str]  # PDF + content-type

   POST'lar uchun _try_request_post helperini qo'shish kerak bo'lishi mumkin
   (mavjud _try_request faqat GET).

2. fbs/routes.py — yangi API endpointlari (POST'lar, CSRF kerak emas, login_required):
   - POST /fbs/api/order/<id>/confirm
   - POST /fbs/api/order/<id>/cancel  (JSON body: reason_code, comment)
   - POST /fbs/api/order/<id>/identifiers (JSON body: items array)
   - GET  /fbs/api/order/<id>/label   (returns PDF, Content-Disposition attachment)

3. templates/fbs_order_detail.html — status'ga qarab tugmalar:
   - CREATED → ko'k "Tasdiqlash" tugmasi + qizil "Bekor qilish" tugmasi
   - CONFIRMED/ASSEMBLED → yashil "Etiketka chop etish" tugmasi
   - Telefon/elektronika kategoriyasi bo'lsa → "IMEI biriktirish" formasi
   - Tugmalar fetch() bilan ishlaydi, loading state ko'rsatadi
   - Muvaffaqiyat → toast notification + sahifa qayta yuklanadi

4. Bekor qilish modal — sabab tanlash dropdown
   (sabablar /v1/fbs/order/return-reasons dan oldindan keshlanib o'qiladi,
   1-bosqichda qo'yilgan funksiya orqali).

5. IMEI/identifier UI — har SKU uchun input maydonlari, validatsiya
   (uzunlik, format Uzum talabiga ko'ra — swagger'da response'ni ko'rib aniqla).

6. dark.md changelog yangilash.

XATOLIK ISHLOV BERISH:
- Uzum 4xx qaytarsa — sotuvchiga aniq xabar (ruscha/o'zbekcha)
- 5xx — "Uzum vaqtincha ishlamayapti, keyinroq qayta urinib ko'ring"
- Tugmalarda double-click himoyasi (action davom etayotganda disabled)

AVVAL: user'dan har action endpoint'ining request body shape'ini swagger
screenshot orqali so'ra (ayniqsa cancel reason format, identifier struktura).

TUGAGAN HISOBLANADI: sotuvchi yangi buyurtmani tasdiqlay oladi, etiketka
yuklab olib chop etadi, kerak bo'lsa IMEI kiritadi, yoki bekor qiladi.
Hammasi /fbs/<id> sahifasidan, Uzum cabinet'siz.
````

---

## 🟡 BOSQICH 3 — DBS ish jarayoni (siz yetkazasiz)

````
SellerHub.uz loyihasida DBS (Delivery by Seller) buyurtmalar uchun action tugmalari.

KONTEKST: 2-bosqich tugagan — FBS to'liq ishlaydi. Endi DBS qo'shamiz.
DBS = sotuvchi o'zi yetkazadigan buyurtmalar (Uzum kuryer chaqirmaydi).

Uzum endpointlari (DBS faqat 3 ta action — list va detail FBS bilan bir xil):
- POST /v1/dbs/order/{orderId}/delivering — "Yetkazishga oldim" (yo'lda)
- POST /v1/dbs/order/{orderId}/completed  — "Mijoz oldi" (yakunlandi)
- POST /v1/dbs/order/{orderId}/refund     — qaytarma yaratish

Ro'yxat olish: GET /v2/fbs/orders endpoint type=DBS filtri bilan
(yoki 1-bosqichda /v2/fbs/orders ikkala turni qaytaradi — type maydoni bilan farqlanadi).

VAZIFA:
1. core/uzum_openapi.py — yangi funksiyalar:
   - mark_dbs_delivering(token, order_id) -> dict
   - mark_dbs_completed(token, order_id, [otp_code? — swagger'dan tekshir]) -> dict
   - refund_dbs_order(token, order_id, reason_code, items, [comment]) -> dict

2. fbs/routes.py — yangi API endpointlari:
   - POST /fbs/api/dbs/<id>/delivering
   - POST /fbs/api/dbs/<id>/completed
   - POST /fbs/api/dbs/<id>/refund

3. templates/fbs_order_detail.html — type='DBS' bo'lganda boshqa workflow:
   - CONFIRMED → "Yetkazishga olaman" tugmasi (mavjud "Etiketka" o'rniga)
   - DELIVERING → "Yetkazib berildi" tugmasi (OTP kod input bo'lishi mumkin)
   - COMPLETED → faqat "Qaytarma yaratish" tugmasi
   - FBS workflow tugmalari (Etiketka, IMEI) bu turda KO'RINMASLIGI kerak

4. templates/fbs_orders.html — types filtriga DBS variant qo'sh
   (hozir "FBS", "FBS+DEFECTED", "Hammasi" bor — "DBS faqat" qo'shilsin).

5. Sidebar (static/uzum_ui.js) — agar DBS alohida sahifa kerak bo'lsa,
   yangi link ("DBS buyurtmalar"). Aks holda /fbs?type=DBS bilan bitta sahifa.

6. dark.md changelog yangilash.

OTP HOLATI (agar kerak bo'lsa):
- Uzum DBS yakunlash uchun ba'zan mijozdan OTP kod so'raydi
- Swagger'da /completed endpoint'ida `confirmationCode` yoki shunga o'xshash
  field bo'lishi mumkin — user'dan aniqlashtirib ol

AVVAL: user'dan 3 ta POST endpoint'ining request body'sini swagger orqali
so'rab ol (refund eng murakkab — items array bo'lishi taxmin).

TUGAGAN HISOBLANADI: sotuvchi DBS buyurtmani "yo'lda" → "yetkazildi"
zanjirini SellerHub'dan boshqaradi. Refund ham ishlaydi.
````

---

## 🟣 BOSQICH 4 — DB keshlash + worker

````
SellerHub.uz'da FBS/DBS buyurtmalarni PostgreSQL'ga keshlash + background sync.

KONTEKST: 3-bosqich tugagan — barcha CRUD ishlaydi, lekin har sahifa Uzum API'ga
uradi (sekin, rate limit xavfi). Endi mavjud sales_lines paradigmasini takrorlaymiz:
DB'ga yoz → sahifa DB'dan o'qisin → worker fon'da sync qilsin.

Mavjud namuna: core/uzum_finance_openapi.py + worker.py FinanceNightly loop.
Models: models.py'da SalesLine, ExpensesLedger, ShopSyncState — shu pattern.

VAZIFA:
1. models.py — yangi jadval:
   class FbsOrder(Base):
     __tablename__ = "fbs_orders"
     shop_id: str (Shop.uzum_id bilan mos)
     order_id: str (Uzum order id) — PK
     order_type: str  # FBS / DBS
     status: str
     customer_name, customer_phone, address_*: str
     total_amount: int (sum)
     items_json: JSONB (SKU array verbatim)
     created_at, updated_at: datetime (Uzum'dan)
     synced_at: datetime (UTC, sync vaqti)
     raw_json: JSONB (xavfsizlik uchun to'liq response)
     Indexes: (shop_id, status, created_at DESC), (shop_id, order_type)

2. migrations/versions/YYYYMMDD_NNNN_fbs_orders.py — Alembic migration:
   create_table + indexes + check constraints (order_type IN ('FBS','DBS'),
   status IN aniq enum).

3. background/ — yangi loop yoki existing finance loop'ga qo'sh:
   - Har 5 daqiqada (configurable) barcha do'konlar uchun:
     - GET /v2/fbs/orders (page=0, size=200, statuses=CREATED,ASSEMBLED,SENT...)
     - DELETE+INSERT idempotent yozish (per shop_id)
   - last_synced_at jadvalda yozib qo'yiladi (ShopSyncState'ga yangi maydon yoki yangi jadval)
   - Per-shop lock (core/shop_lock.py bor) bilan parallel race oldini olish

4. fbs/routes.py — mavjud /fbs/api/orders ni DB'dan o'qiydigan qil:
   - Default: DB'dan (instant)
   - ?refresh=1 parametri bilan — Uzum'ga urilib DB yangilanadi
   - DB query: SQLAlchemy select() FbsOrder filterlar bilan
   - Detail sahifasi ham DB'dan (Uzum'ga faqat refresh tugmasi bossa)

5. templates/fbs_orders.html — "Yangilash" tugmasi yonida
   "Uzum'dan tortib olish" (refresh=1) tugmasi alohida.
   Pastda: "Oxirgi yangilangan: 2 daqiqa oldin" indikatori.

6. dark.md changelog yangilash.

QOIDALAR:
- Worker'da ConnectionPool exhaustion oldini olish (SessionLocal bilan)
- 429/5xx'da exponential backoff (mavjud namuna: AdditiveBackoffRetry)
- Faqat aktiv statuslar sync'da (COMPLETED'lar 30 kun keyin trim)
- Idempotent: bir necha bor ishlasa duplicate yo'q

ETIBOR BERING:
- Action endpointlari (confirm, cancel, etc.) hamon Uzum'ga to'g'ri uradi
  va keyin DB'da o'sha order'ni darrov yangilaydi (real-time UX uchun)
- Cron-like loop'ni docker-compose'da worker servis ishlatadi (mavjud)

TUGAGAN HISOBLANADI: /fbs sahifasi 100ms'da ochiladi, real-time emas,
"Yangilash" tugmasi bossa Uzum'ga so'rov ketadi va DB yangilanadi.
````

---

## 🔴 BOSQICH 5 — Telegram bildirishnomalar

````
SellerHub.uz'da yangi FBS/DBS buyurtma kelganda Telegram'ga bildirishnoma.

KONTEKST: 4-bosqich tugagan — DB'da fbs_orders bor, worker har 5 daqiqada sync qiladi.
Endi sotuvchi telefondan ko'z uzmasin — Telegram bot xabar qiladi.

Mavjud namuna: telegram/ moduli, background/ ichida hourly notification
loop'lari (NotificationSettings model). Bot token: .env'da TELEGRAM_BOT_TOKEN.

VAZIFA:
1. models.py — NotificationSettings'ga yangi maydonlar:
   fbs_new_order_enabled: bool (default True)
   fbs_unconfirmed_reminder_minutes: int (default 30 — bu vaqtdan ortiq
     CREATED holatdagi buyurtma esga olinadi)
   fbs_return_request_enabled: bool (default True)

2. background/ — yangi tick (yoki mavjud loop ichiga):
   - fbs_orders sync tugagandan keyin:
     a) Yangi CREATED buyurtmalar (synced_at > last_check_at) → darrov xabar
     b) CREATED holatda > 30 daqiqa qolgan buyurtmalar → eslatma
     c) Yangi return/refund so'rovi → urgent xabar
   - Har user uchun NotificationSettings tekshiriladi
   - Last-sent tracking (duplicate yuborilmasin):
     yangi jadval fbs_notifications_sent (user_id, order_id, kind, sent_at)

3. telegram/ — yangi xabar formatlash funksiyalari:
   - format_new_order_message(order) -> str — emoji + qisqa info +
     "Tasdiqlash" inline button (https://sellerhub.uz/fbs/<id>)
   - format_unconfirmed_reminder(order, age_min) -> str
   - format_return_request(order) -> str

4. templates/settings_notifications.html — yangi toggle'lar:
   - FBS yangi buyurtma — ON/OFF
   - Tasdiqlanmagan eslatma — daqiqa input
   - Qaytarma so'rovi — ON/OFF

5. dark.md changelog yangilash (settings_notifications'da yangi UI).

XABAR MISOLI (o'zbek):
   🆕 Yangi FBS buyurtma!
   №12345678 · Shop: MyShop
   Mijoz: Ali Valiev (+998901234567)
   SKU: 2 ta · Summa: 350 000 so'm
   👉 SellerHub'da ko'rish

QOIDALAR:
- NotificationSettings.window_from/to_hour hisobga olinadi (uyqu vaqti yo'q)
- Bot xabar yubora olmasa (user blokladi bot'ni) — bir marta log, qaytarma yo'q
- Inline tugmalar (callback_data) bilan bot'dan to'g'ri tasdiqlash imkoni
  (ixtiyoriy — alohida sub-bosqich bo'lishi mumkin)

TUGAGAN HISOBLANADI: yangi FBS buyurtma kelsa Telegram'da xabar 5 daqiqa
ichida keladi, tasdiqlamasdan 30 daqiqa o'tsa eslatma kelishi.
````

---

## Eslatma

Har bosqich tugagandan keyin:
1. PLAN.md'da bosqich holatini ✅ ga o'zgartiring
2. snapshots/ ga yangi kod nusxalarini qo'shing
3. ENDPOINTS.md'ni yangi ishlatilgan endpoint'lar bilan to'ldiring
