# FBS/DBS Audit Roadmap — Uzum bilan moslashtirish (sinxron)

> **Maqsad:** SellerHub FBS/DBS arxitekturasini Uzum'ning HAQIQIY (***actual***) FBS/DBS arxitekturasiga aniq va xatosiz moslashtirish. Egasi "hozir ko'p xatolar bor" deb xabar berdi. Bu hujjat audit (***tekshiruv***) uchun BAZAVIY (***baseline***) xaritani quradi.
>
> **Manba (***source***):** uchta xarita (Uzum-docs, bizning kod, gap-hunt) + jonli (***live***) kod tekshiruvi. Barcha iqtiboslar `fayl:satr` ko'rinishida.
>
> **Yaratilgan:** 2026-05-29 (5-agentli audit workflow).

---

## 1. Tavsiya etilgan yondashuv (***recommended approach***)

**To'liq qayta yozish (***full rewrite***) EMAS — bosqichma-bosqich gap-audit.**

Sabablari:
- Tizim HOZIR Uzum'ning rasmiy `seller-openapi` ga JONLI buyurtmalar bilan ishlayapti. Har bir keraksiz/noto'g'ri so'rov (***request***) Uzum'ning hujjatlashtirilgan **per-token burst penalty** (***to'plamli jazo***) xavfini olib keladi — `core/fbs_locks.py` va 1s pauza (***throttle***) aynan shu sababdan mavjud.
- Qayta yozish to'g'ri ishlayotgan qismlarni (token-batched sync, SWR kesh, per-token mutex, yorliq (***label***) PDF'dan mijoz ismini o'qish) yo'qotadi.
- Action endpointlar (confirm/cancel/refund/identifier/invoice) haqiqiy buyurtmalarni o'zgartiradi — ularni xavfsiz qayta sinab bo'lmaydi.

**LEKIN ikkita qatlam (***layer***) maqsadli qayta yozishga loyiq** (jonli ma'lumot tasdiqlangach):
1. Proyeksiya qatlami `core/fbs_sync.py` — `row_to_dict`/`dict_from_order`. `stock`'ni `{id,title}` ga toraytirish + `raw_json` exclude bug'i (`fbs_sync.py:305-308,320-328`) va tushib qolgan `dropOffPoint.type` keshlangan detal sahifani strukturaviy ravishda noto'liq qiladi.
2. `orderItems` o'qish joylari — agar jonli tekshiruv rasmiy shaklda `sellerPrice`/`productImage` ekanini tasdiqlasa (`ENDPOINTS.md:67`), `fbs/routes.py:2486-2504`, `core/fbs_data.py:1043-1045`, `templates/fbs_orders.html` ga normalizatsiya (***normalization***) shim kerak.

**MUHIM TARTIB:** Avval jonli DevTools tekshiruvini (2-bo'lim) bajaring. Eng yuqori jiddiylikdagi 5 ta topilma (`places=` default, status enum, item maydon nomlari, dateFrom/dateTo birligi, confirm/identifier echo body) jonli javobni ko'rmasdan xavfsiz tuzatib bo'lmaydi — taxmin qilish buyurtmalarni yo'qotish yoki keshni buzish xavfini tug'diradi. Jonli verifikatsiya talab qilmaydigan kod bug'lari parallel ravishda boshlanishi mumkin.

---

## 2. Skrinshot ro'yxati (***screenshot checklist***) — sahifa-ba-sahifa

> F12 → Network → XHR filtri yoqilgan holda. **Rasmiy** `api-seller.uzum.uz/api/seller-openapi/...` ni qidiring, **ichki** (***internal***) `api/seller/fbs/...` EMAS. Agar faqat ichkisi chiqsa, uni ham oling, lekin "internal" deb belgilang.

| # | Sahifa / Tab | Qaysi Network qatorini bosish | Qaysi F12 tabni skrinshot | Bizning kodda nimani tasdiqlaydi |
|---|---|---|---|---|
| 1 | FBS/DBS buyurtmalar ro'yxati (barcha status + FBS va DBS) | `GET /v2/fbs/orders` | Headers (to'liq URL + Authorization formati), Payload, Response (2-3 buyurtma; biri DROP_OFF, biri DBS) | `places=STOCK,DROP_OFF` bormi (`uzum_openapi.py:534`); status enum (`uzum_openapi.py:1565`, `models.py:813`); item kalitlari price↔sellerPrice, photo↔productImage; `dropOffPoint.type`; `stock` ichki maydonlari |
| 2 | Sana oralig'i (***date range***) filtri | `GET /v2/fbs/orders` (filtr so'rovi) | Headers (URL query), Payload — `dateFrom`/`dateTo` qiymatini SANANG | ms (13 raqam) yoki sekund (10 raqam) — `uzum_openapi.py:509-512,538-541`; `fbs/routes.py:559-592` |
| 3 | Bitta buyurtma detali | `GET /v1/fbs/order/{id}` | Response (to'liq JSON), Headers | Detal shakli `orders[0]` bilan bir xilmi; `drop.type`, `stock.*` (`fbs_order_detail.html:1034-1047`); `identifierInfo.type` (ASL_BELGISI?) |
| 4 | CONFIRM (Qabul qilish) — test buyurtmada | `POST /v1/fbs/order/{id}/confirm` | Payload (body bo'shmi?), Response (to'liq buyurtmami yoki ingichka?) | `uzum_openapi.py:766`; echo regress xavfi (`fbs_data.py:704-727`) |
| 5 | CANCEL (Bekor qilish) modal | `GET /v1/fbs/order/return-reasons` + `POST /v1/fbs/order/{id}/cancel` | return-reasons Response (enum shakli); cancel Payload (reason maydon nomi: reason/reasonId/reasonCode + comment) | `uzum_openapi.py:801`; `ENDPOINTS.md:74` bo'sh joyini to'ldiradi |
| 6 | IMEI/ASL_BELGISI kiritish | `POST /v1/fbs/order/{id}/identifier` | Payload (items[] tuzilishi: orderItemId↔id, values), Response | `uzum_openapi.py:855-909`; tip IMEI↔ASL_BELGISI (`:866`); modal `fbs/routes.py:380` |
| 7 | Yorliq (***label***) chop etish | `GET /v1/fbs/order/{id}/labels/print` | Headers (size qiymatlari), Response (`payload.document` — base64 massivmi?) | size enum (`uzum_openapi.py:1346-1348`); document element turi |
| 8 | Postavkalar / Invoices (Поставки) + invoice yaratish | `GET /v1/fbs/invoice`, `/{id}`, `/dop/drop-off-points`, `/dop/time-slot`, `POST /v1/fbs/invoice` | Har birining Headers (query: statuses[], customerOrderIds[], dopId, sellerOrderIds) + Response; POST Payload | invoice param'lar (`uzum_openapi.py:922,997,1057,1179,1234`); invoiceNumber join asosi → LIKE-suffix bug (`fbs/routes.py:2575-2585`); sellerId + idempotencyKey |
| 9 | DBS buyurtma hayot tsikli (***lifecycle***) | `POST /v1/dbs/order/{id}/delivering`, `/completed`, `/refund` + DBS detal | Har action Payload+Response; completed: OTP/issueCode bormi; refund: items[]/reason; DBS deliveryInfo kalitlari | `uzum_openapi.py:1479,1531`; `fbs_sync.py:225-228` DBS mijoz maydonlari |
| 10 | Status chip count + Склад/Ombor | `GET /v2/fbs/orders/count` (scheme bormi? places bormi?) + stock endpoint | Headers, Response (bare int?) | no-scheme gap (`uzum_openapi.py:686-707`); count ga places kerakmi |

---

## 3. Endpoint solishtirma jadvali (***comparison matrix***)

| Endpoint | Uzum'da bor | Bizda bor | Farq (***gap***) |
|---|---|---|---|
| `GET /v2/fbs/orders` | shopIds, status, scheme, date, page, size, **places=STOCK,DROP_OFF**; boy stock/dropOffPoint/orderItems | `places` YO'Q; stock→{id,title}; drop→uuid+address; item verbatim | **HIGH**: places tushib qolgan (DROP_OFF yo'qolishi mumkin); type tushgan; stock toraytirilgan; item kalit nomlari; date birligi |
| `GET /v2/fbs/orders/count` | per-status, bare int, FBS-only | shopIds/status/date; bare int parse | **MEDIUM**: scheme yo'q → DBS count olinmaydi |
| `GET /v1/fbs/order/{id}` | to'liq buyurtma; type=ISSUE_POINT | DB-first; row_to_dict | **MEDIUM**: keshda stock.*/drop.type bo'sh (`fbs_sync.py:305-308,320-328`) |
| `POST .../confirm` | echo shakli noma'lum | body yo'q; echo upsert | **MEDIUM**: ingichka echo NULL-overwrite qilishi mumkin |
| `POST .../cancel` | reason body nomi noma'lum; reasons enum noma'lum | {reason, comment} | **MEDIUM**: noto'g'ri nom → 400 |
| `POST .../identifier` | items[] noma'lum; tip ASL_BELGISI | {items:[{orderItemId,values}]} | **MEDIUM**: id kaliti/tip tasdiqlanmagan |
| `GET .../labels/print` | LARGE/BIG; document element noma'lum | LARGE/BIG normalize; base64 | **LOW**: qayta tekshirish |
| `GET .../return-reasons` | enum (shakl noma'lum) | verbatim modalga | **LOW**: shaklni olish |
| `POST /v1/dbs/.../delivering\|completed\|refund` | bodylar noma'lum; OTP? | delivering no-body, completed issueCode, refund no-body | **MEDIUM**: OTP/refund body tasdiqlanmagan |
| `GET /v1/fbs/invoice*` | number, status{}, stock, ettn | jonli stream | **MEDIUM**: LIKE-suffix join collision (`fbs/routes.py:2575-2585`) |
| dropoff/time-slot/create | to'liq | dropoff persist, qolgani jonli | **LOW**: sellerId/idempotencyKey tasdiqlash |
| Background worker | — (Uzum dateCreated bo'yicha) | 11 status, stop_on_known, MAX_PAGES=50, quantity_fbs>0 gate | **HIGH**: gate FBS-stock'ga bog'liq, buyurtmaga emas; stale 30-kun izoh; 2500/status truncate |
| Status enum | rasmiy 11; legacy boshqa | 11 hardcoded + CHECK | **HIGH**: noma'lum status SKIP + CHECK rad etadi → buyurtma yo'qoladi |

---

## 4. Ustuvor tuzatishlar (***prioritized fixes***)

> **QOIDA (loyiha):** action endpointlarni (cancel/confirm/refund/identifier/invoice) o'zgartirsangiz, prodda sinashdan OLDIN mocked pytest yozing — Uzum penalty/ban xavfi.

### HIGH
1. **`places=STOCK,DROP_OFF` qo'shish** (`core/uzum_openapi.py`) — *avval probe*. Effort: kichik. Migration: yo'q. Agar rasmiy endpoint `places` siz STOCK-only qaytarsa, DROP_OFF buyurtmalar umuman sinxronlanmaydi.
2. **Status enum'ni tasdiqlash + noma'lum statusni xavfsiz qilish** (`uzum_openapi.py`, `models.py`, `fbs_sync.py`, `app.py`, migration) — *avval probe*. Effort: o'rta. Migration: **HA** (CHECK). Hozir noma'lum status SKIP (`fbs_sync.py:355`) + CHECK rad.
3. **Shop-activation gate'ni tuzatish** (`app.py`) — FBS/DBS buyurtmasi bor har qanday do'konni sinxronlash, faqat `quantity_fbs>0` emas. Effort: o'rta. Migration: yo'q.
4. **orderItems kalitlarini normalizatsiya** (`fbs_sync.py`, `fbs_data.py`, `fbs/routes.py`, `templates/fbs_orders.html`) — *avval probe*. Effort: o'rta. Migration: yo'q. Narx/rasm/QR/jami 0/bo'sh chiqmasligi uchun bitta shim.

### MEDIUM
5. **`row_to_dict` da nested obyektlarni tushirmaslik** (`fbs_sync.py`) — exclude set'dan `stock`+`dropOffPoint` ni olib tashlash. Effort: kichik. Migration: yo'q. Live verify: yo'q.
6. **`drop_off_point_type` ustuni qo'shish** (`models.py`, `fbs_sync.py`, migration). Effort: o'rta. Migration: **HA**.
7. **dateFrom/dateTo birligini tasdiqlash** (`uzum_openapi.py`, `fbs/routes.py`, `fbs_data.py`) — *avval probe*. Effort: kichik. Migration: yo'q.
8. **Action-echo NULL-overwrite himoyasi** (`fbs_data.py`, `fbs_sync.py`, `tests/`) — faqat mavjud maydonlarni update yoki har doim to'liq detal refetch. *Avval probe + mocked pytest.* Effort: o'rta.
9. **cancel body nomi + return-reasons shakli** (`uzum_openapi.py`, `fbs/routes.py`, `tests/`) — *avval probe + mocked pytest*. Effort: o'rta.
10. **Invoice join'ni tuzatish** (`fbs/routes.py`) — LIKE '%id%' o'rniga aniq tenglik; detal sahifa bilan birlashtirish. Effort: kichik. Migration: yo'q.
11. **DBS action bodylari + deliveryInfo kalitlari** (`uzum_openapi.py`, `fbs_data.py`, `tests/`) — *avval probe + mocked pytest*. Effort: o'rta.
12. **COMPLETED sync siyosati** (`app.py`, `fbs_sync.py`) — haqiqiy sana-oyna yoki stale izohni olib tashlash; MAX_PAGES truncate uchun log/alert. Effort: o'rta. Migration: yo'q.

### LOW
13. **Label size enum + document element turini qayta tekshirish** (`uzum_openapi.py`, `fbs/routes.py`) — *avval probe*. Effort: kichik.
14. **publicId/carrierCode/prolongation count uchun qidiriladigan ustun/JSONB index** (`models.py`, `fbs_sync.py`, migration) — penalty timerlari SQL'da ko'rinishi uchun. Effort: o'rta. Migration: **HA**.

---

## 5. Bajarish tartibi (xulosa)

1. **Bosqich A — Jonli tekshiruv:** 2-bo'limdagi 10 ta skrinshotni oling. Bu HIGH/MEDIUM tuzatishlarning yarmini ochadi.
2. **Bosqich B — Live-verify shart bo'lmagan kod bug'lari (parallel):** #3 (shop gate), #5 (nested passthrough), #6 (drop type ustun), #10 (invoice join), #12 (COMPLETED izoh/alert).
3. **Bosqich C — Live-verify natijasiga ko'ra:** #1 (places), #2 (enum), #4 (item kalitlari), #7 (date birligi).
4. **Bosqich D — Action endpointlar (mocked pytest AVVAL):** #8, #9, #11, #13.
5. **Bosqich E — Sayqal:** #14 (qidiriladigan ustunlar).

> Har action o'zgarishi: **mocked pytest → kod → bitta test buyurtmada jonli sinov → keyin to'liq**.

---

## 6. Eslatma — rasmiy vs ichki API (muhim)

Brauzer F12 faqat Uzum'ning **ichki frontend API**'sini (`api/seller/fbs/v2/orders`) ko'rsatadi — bizning kod esa **rasmiy OpenAPI**'ni (`api/seller-openapi/...`) server tomonda chaqiradi. Shakllar odatda bir xil, lekin `places` default xulqi va aniq rasmiy maydon nomlari uchun bizning backend'da **probe/debug route** kerak (brauzerdan ko'rib bo'lmaydi). Skrinshotlar data model, lifecycle va action payload'larni tushunish uchun zo'r; rasmiy-endpoint-specific savollar uchun backend probe.

---

## 7. Topilmalar jurnali (***findings log***)

### ✅ Bosqich A.1 — "Новые" sahifa HAR (2026-05-29, ichki API)

**Tasdiqlangan (xavfsiz):**
- **Status enum** — `count` javobi 11 ta status berdi: `CREATED, PACKING, PENDING_DELIVERY, DELIVERING, DELIVERED, ACCEPTED_AT_DP, DELIVERED_TO_CUSTOMER_DELIVERY_POINT, COMPLETED, CANCELED, PENDING_CANCELLATION, RETURNED`. Bizning `FBS_ORDER_STATUSES` bilan **MOS** → **HIGH #2 FBS uchun hal bo'ldi** (DBS enum hali tekshirilmagan).
- **return-reasons qiymatlari** — `OUT_OF_STOCK, OUT_OF_PACKAGE, OUT_OF_TIME, OTHER` (maydon: `reason`+`title`). → bekor sabablari gap'i to'ldirildi.
- **Label o'lchamlari** — `LARGE` (58×40, A4), `BIG` (43×25, A4+THERMAL). Kod to'g'ri (#13 yopildi).
- **availableSchemas: ["FBS","DBS"]** — seller DBS'ni HAM ishlatadi → DBS support muhim; shop gate (#3) shopni `availableSchemas`/buyurtma bo'yicha tanlashi mumkin.
- **Tab↔status:** Новые=CREATED, В сборке=PACKING, Приняты Uzum=DELIVERED, Выданы=COMPLETED, Отменены=CANCELED, Возвраты=RETURNED.

**⚠️ Yangi flaglar (RASMIY endpoint'da probe kerak — ichki ≠ rasmiy):**
- **count shakli** — ichki `count` bitta call'da `{payload:{ordersCount:[{status,counter}]}}` qaytaradi (HAMMA status). Bizning kod (`uzum_openapi.py:669`) 11× chaqirib **bare int** kutadi. Rasmiy `/v2/fbs/orders/count` shunaqa bo'lsa: (a) parse noto'g'ri, (b) 11→1 call katta tezlik yutug'i. **PROBE.**
- **return-reasons shakli** — ichki `{payload:{reasons:[...]}}` (obyekt), bare list EMAS. Kod (`uzum_openapi.py:1391`) `payload`'ni list deb modalga beradi → rasmiy ham shunaqa bo'lsa **cancel dropdown bo'sh chiqadi**. **PROBE/VERIFY.**

**🆕 Yangi ma'lumot manbalari (kelajakdagi imkoniyat):**
- `fbs/legal-info` → **komitent**: `sellerName: "ИП PATTAXOV ULUG'BEK ISKANDAROVICH"` + `shops[]`. [[project-uzum-seller-profile-unavailable]] ga ZID (ichki API beradi). Token/auth tekshirilishi kerak.
- `stock/stocks` → seller ombori (Чинабад) + `deliveryMethods{shipmentPeriodDays:3, cutOffTime, returnDropUuid}`. Order'dagi `stock` (Сергели = Uzum hub) dan FARQLI tushuncha.
- `fbs/rating` → FBS reyting (`successRate:0%`, `criticalFlag:true`) + lockout (`lockoutDays:3, minimumOrders:10, lockEnabled:true`). Penalty/lock tizimi — UI'da ko'rsatish mumkin.

**Hali kerak:** order detail, action'lar (confirm/cancel/identifier payload), Поставки/invoice, DBS lifecycle, Склад. Va rasmiy-endpoint probe (count + return-reasons + places).

### ⭐ Bosqich A.2 — "В сборке" (PACKING) HAR (2026-05-29, ichki API)

**🔴 ENG MUHIM TOPILMA — maydon to'liqligi STATUS bo'yicha o'zgaradi:**
- В сборке tabи **boshqa endpoint** ishlatadi: `GET /api/seller/fbs/orders` (v1, versiyasiz) — Новые esa `/fbs/v2/orders` (v2). Frontend tab'ga qarab har xil endpoint chaqiradi.
- PACKING buyurtma javobi **INGICHKA**: o'sha order (109044484) Новые'da `stock={Сергели...}`, `scheme="FBS"`, `carrierCode="DEFAULT"`, `acceptUntil` to'la edi — PACKING'da `stock:null`, `scheme:null`, `carrierCode:null`, `acceptUntil:null`.
- **XAVF (ehtimol "ko'p xato"ning asosiy manbai):** bizning `upsert_orders` HAR safar barcha ustunlarni so'nggi javob bilan yozadi. Order CREATED→PACKING'ga o'tib, ingichka javob kelsa — oldin saqlangan `stock_id/stock_title` (+ scheme, carrierCode) **NULL bilan o'chiriladi**. Detal sahifa keyin bo'sh "Ombor" ko'rsatadi. → **Fix #8 (merge, don't blank) endi KRITIK, faqat MEDIUM emas.**
- **scheme:null default bug** — `dict_from_order` `o.get("scheme") or "FBS"` qiladi. DBS buyurtma ingichka javobda `scheme:null` kelsa → **noto'g'ri FBS deb saqlanadi**. DBS uchun xavfli.

**⚠️ PROBE (rasmiy v2):** bu ichki v1 endpoint. Bizning kod rasmiy **v2** ishlatadi. Savol: rasmiy `/v2/fbs/orders?status=PACKING` `stock`/`scheme` ni TO'LIQ qaytaradimi yoki u ham null? Agar v2 ham null bo'lsa — #8 darhol kerak. Qanday bo'lsa ham, upsert "ustiga yozma, birlashtir" bo'lishi kerak.

**Tasdiq (takror):** count enum yana 11 ta (CANCELED 265→266, CREATED 2→1 — order PACKING'ga o'tdi). Qolган endpointlar (shop/legal-info/stocks/settings/rating/locks/return-reasons/sizes) Новые bilan AYNAN bir xil — har sahifa yuklanganda qayta chaqiriladi.

### Bosqich A.3 — Order detail (2026-05-29, ichki API)

- **Detail endpoint:** `GET /api/seller/fbs/order/{id}` (ichki; bizники rasmiy `/v1/fbs/order/{id}`). Javob **LIST shakli bilan AYNAN** + **2 ta qo'shimcha maydon**: `cancelPrice` (null) va `penaltyParameters` (null).
- ✅ Detail≈list round-trip taxmini TO'G'RI — `row_to_dict` shakli mos, detal uchun alohida katta xato yo'q.
- ⚠️ **YANGI GAP:** `cancelPrice`/`penaltyParameters` faqat DETAIL javobida keladi — LIST'da YO'Q. Bizning kesh LIST endpoint'dan (`/v2/fbs/orders`) to'ldiriladi → keshlangan buyurtmalarда bu maydonlar **yo'q**. `penaltyParameters` bekor qilishda jarima ogohlantirishi uchun muhim. (DB-miss'da live detail fallback keltiradi, lekin keshda yo'q.) → Yangi past-o'rta tuzatish: detail'da bu 2 maydonni surface qilish (raw_json'dan yoki alohida).
- DETAIL bu safar v2 emas, versiyasiz `/fbs/order/{id}` (ichki frontend). Bizning kod rasmiy `/v1/fbs/order/{id}` — kutilgan farq.

### ⚡ Bosqich A.4 — "Yangilash" sekinligi (Hammasi ko'rinish) (2026-05-29, perf audit)

**Sabab (perf workflow, 3 agent):**
- "Yangilash" handler 2 bosqichli (`fbs_orders.html:2150-2207`): **1-bosqich** keshdan o'qiydi (~100-200ms, tez ✅); **2-bosqich** har ikkala endpoint'ga `refresh=1` qo'yib **JONLI Uzum sync** qiladi → bu sekinlik. Hammasi (`shop=='all'`) ko'rinishda: orders 1 ta jonli page fetch (~1-2s) + counts 3 ta parallel `/count` Uzum call — hammasi **bitta per-token mutex'da ketma-ket** (`core/fbs_locks.py`), bg worker ushlab tursa 5s kutadi.
- **MUHIM:** bu jonli sync ataylab — Abdulaziz 2026-05-26 da so'ragan ("yuzer har doim uzumdan olsin, dbdan emas", `core/fbs_data.py:351`). Endi tezlik xohlanyapti — ikkisi **qarama-qarshi**, tanlov kerak.
- 2-bosqich ro'yxatni "Yuklanmoqda…" bilan **bo'shatadi** (`fbs_orders.html:1167`) → bo'sh ekran + spinner = "osilib qolgandek".
- ✅ Per-chip fan-out YO'Q (dastlabki taxmin rad etildi): counts-all bitta GROUP BY (`fbs_data.py:586-590`), frontend bir marta chaqiradi. Backend allaqachon Uzum'dek bitta-call dizaynда.

**Tuzatishlar:**
1. (kichik) Hammasi ko'rinishda 2-bosqichni o'chirish → faqat keshdan (~0.2s, 5-15× tez). ⚠️ 2026-05-26 afzalligini teskari qiladi — foydalanuvchidan so'rash kerak. "Yangilangan: HH:MM" yorlig'i qo'shiladi.
2. (kichik) Refresh paytida ro'yxatni bo'shatmaslik (`fbs_orders.html:1167`) — toza UX yutug'i, har holatda foydali.
3. (o'rta) Bitta-do'kon jonli refresh uchun server-throttle (60s) — himoya.
4. (kichik) Eski/yanglish izohlarni tuzatish (`fbs_orders.html:2143`, `fbs_data.py:533`).

**Bog'liq:** chip sanog'i ilovada Uzumdan PAST (Yakunlandi 234 vs 252, Qaytarildi 183 vs 192) → ~27 buyurtma keshda yo'q. Bu places(#1)/shop-gate(#3)/MAX_PAGES truncate xatolariga bog'liq — tuzatilsa to'liqlik ham tiklanadi.

### ⚡ Bosqich A.5 — Tezlik (perf) arxitektura ko'rigi (2026-05-29, 4-agent)

**Tezkor yutuqlar (kichik, xavfsiz, katta foyda — birinchi shular):**
- **QW1.** JSON'ni siqish: `core/auth_helpers.py:22` `indent=2` → `separators=(',',':')`. Har API javobi 20-40% kichik. ⚠️ SHARED fayl (butun app).
- **QW2.** Worker'dagi ortiqcha `sleep(1.0)` ni olib tashlash (`app.py:4309`) — inter-page throttle allaqachon bor; bu ~11s ortiqcha mutex band qiladi.
- **QW3.** Bitta-do'kon count fan-out'ni 1 GROUP BY ga yig'ish (`fbs/routes.py:1083-1111`) — 11 SELECT COUNT(*) → 1.

**Saralangan tuzatishlar:**
1. (o'rta) **Birinchi sahifa + sanoqlarni HTML'ga inline** qilish (`fbs/routes.py:730`, `fbs_orders.html:2251-2255/1167/1227-1239`) — 2 ta client round-trip + "Yuklanmoqda" miltirashini yo'qotadi. ⭐ ENG KATTA "Uzumdek darhol" yutug'i.
2. (o'rta) **List o'qishda raw_json + to'liq items_json tortmaslik** (`fbs_data.py:152/564`, `fbs_sync.py:313/318-329`, denorm ustun: `models.py`) — ~50× kichik payload + CPU. Detail to'liq qoladi.
3. (o'rta) **Worker mutex'ni har status uchun** ushlab-qo'yib turish (`app.py:4286-4309`, `fbs_locks.py`) — "Yangilash hech narsa qilmaydi" (5s timeout) ni tuzatadi (~16-19s→~1-2s kontentsiya).
4. (kichik) Retry backoff'ni FBS uchun qisqartirish (`core/http_client.py`) — 6 daq muzlashни ~35s ga. ⚠️ SHARED fayl.
5. (o'rta) SWR'ni Hammasi view + filter/count'ga kengaytirish (`fbs_data.py:557/585/418/467`).
6. (o'rta) Terminal 8 statusni full-drain o'rniga `/count` bilan yangilash (`app.py:4291-4309`).
7. (o'rta) Kechqurun **dated re-backfill** (stop_on_known'siz) — 234 vs 252 to'liqlik gap'ini yopadi.
8. (kichik, probe) Qidiruv uchun **pg_trgm GIN index** (`items_json::text` ILIKE full-scan → index). EXPLAIN bilan tekshirish.
9. (kichik) `renderOrders` ni tbody **event delegation** ga o'tkazish (50 qator × 3 listener churn).
10. (kichik) Har so'rovdagi auth/scope preamble so'rovlarini qisqartirish (`auth_helpers.py:131`, `routes.py:682-722`).
11. (kichik, **PROBE**) `/v2/fbs/orders/count` CSV statuslarni qabul qiladimi va `{ordersCount:[...]}` qaytaradimi → worker count 11→1.

**Allaqachon optimal (TEGMA):** rasm lazy-hover (storm yo'q), polling/SSE yo'q, Hammasi chip sanog'i 1 GROUP BY, dropoff client-cache, composite index (shop_id,status,date DESC).

⚠️ **Scope:** QW2/QW3, #1/#2/#3/#5/#6/#7/#9 — FBS fayllar ✅. QW1 (`auth_helpers`) va #4 (`http_client`) — SHARED, butun app'ga ta'sir → alohida ruxsat kerak.

### ⚡ Bosqich A.6 — Tezlik fixlari amalga oshirildi (2026-05-30, tunги avtonom ish)

Abdulaziz uxlаётgan paytда, "eng tez + eng riski kam" yo'nalishida. Har biri: test/compile → review. **Commit qilinmagan** — working tree'да (oldindan mavjud WIP bilan aralashmaslik uchun); `git diff` bilan ko'rib commit qilinadi.

**BAJARILDI ✅**
- **QW3** — `fbs/routes.py` single-shop count (~1058-1092). 11 parallel `SELECT COUNT(*)` → 1 `GROUP BY` (`get_fbs_counts_for_shops([shop_id])`); refresh esa tez `/count` (`_refresh_shops_counts`). "All" branch pattern'iga mirror (prodда sinalgan). Response shakli o'zgarmadi. `/count` burst-exempt (`_refresh_shops_counts` docstring) → burst xavfi yo'q.
- **#2** — `core/fbs_sync.py` `row_to_dict(include_raw=True)` param; `core/fbs_data.py` 2 ta list query `.options(defer(FbsOrder.raw_json))` + `include_raw=False`. Detal yo'li o'zgarmadi (raw_json to'liq qoladi). List renderOrders raw_json-only maydon ishlatmaydi (tasdiqlandi). 3 yangi pytest (jumladan defer xavfsizligi: `include_raw=False` raw_json'ga umuman tegmasligi). **75 test o'tdi.**
- **#8** — `migrations/versions/20260530_0001_fbs_orders_items_json_trgm_index.py` yozildi, **QO'LLANMADI**. pg_trgm GIN index `items_json::text` uchun. Docstring'да: extension/superuser, katta jadval uchun CONCURRENTLY, VARCHAR↔TEXT mosligi, EXPLAIN bilan tekshirish.
- `tests/conftest.py` — `redis` stub qo'shildi (route/data-layer testlarini DB/Redis serversiz import qilish uchun).

**QAYTARILDI 🔙 (review + `fbs_locks` docstring asosida):**
- **QW2 + #3 (worker mutex per-status release)** — yozildi, keyin **qaytarildi**. Sabab: `core/fbs_locks` docstring aniq aytadi — Uzum burst penalty **bir tokenда 1 soniyada >1 chaqiruv** bo'lganда trip qiladi. Per-status release Yangilash'ning worker bilan bir soniyada chaqirishiga yo'l ochadi → invariant buziladi. Bundan tashqari `_refresh_shop_status` docstring'i: Yangilash mid-tick'da skip qilishi **ataylab** (DB allaqachon fresh). Ya'ni "Yangilash mid-tick sekin" — bug emas, burst-xavfsizlik dizayni. Review buni LOW dedi, lekin Abdulaziz'ning ban-sensitivligi tufayli qaytarildi. **To'g'ri yechim** (uyg'oqда): ikkala yo'l (worker + Yangilash) uchun umumiy **per-token min-interval (≥1s) gate** — kuchsizroq qulf emas.

**DEFER QILINDI ⏸️ (yuqori risk — haqiqiy DB/jonli ma'lumot bilan, Abdulaziz uyg'oqда):**
- **#5 (SWR kengaytirish)** — cache-key xatosi **bir sotuvchining buyurtmasini boshqasiga** ko'rsatishi mumkin (jiddiy xavfsizlik). Stub'langan testда cache'ni tekshirib bo'lmaydi.
- **#6 + #7 (terminal /count + kechqurun backfill)** — sync correctness'ni o'zgartiradi (buyurtma noto'g'ri statusда qolishi mumkin). `stop_on_known` allaqachon terminallarni 1 sahifадан keyin to'xtatadi → 100/500 masshtab uchun shart emas. Juftlik sifatida, real ma'lumot bilan qilinishi kerak.

**Kelajak (frontend, brauzer-verify kerak):** #1 (inline first page) — eng katta "darhol" yutug'i, lekin UI brauzerда tekshirilishi kerak; #9 (event delegation). **Probe:** #11 (live `/count` CSV) — jonli Uzum, read-only, uyg'oqда.

### ⚡ Bosqich A.7 — Per-token min-interval gate (2026-05-30, Abdulaziz uyg'oq, tanladi: variant B)

QAYTARILDI bo'limдagi "to'g'ri yechim" amalga oshirildi. **Variant B (in-process)** tanlandi (Redis emas) — portativlik uchun (boshqa baza/infraga ko'chsa ishlaydi, Redis hot-path'ga kirmaydi). Burst profili hozirgi coarse-lock bilan bir xil (in-process), lekin Yangilash endi **skip qilmaydi, paced interleave qiladi**.

**Dizayn:** `core/fbs_locks.py` ga `pace_uzum_call(token, *, min_interval=1.0)` — har token uchun monotonik o'sib boruvchi slot band qiladi (`slot = max(now, last+interval)`), qisqa `_pace_lock` ostida, keyin **qulf tashqarisida** slotgача uxlaydi. Inject qilinadigan soat/sleep → deterministik test.

**Ulanish (har Uzum `/orders` chaqirig'i aynan 1 marta paced):**
- `core/fbs_sync.py` `fetch_all_pages` — har sahifадан oldin `pace_uzum_call` (worker + multi-page JIT shu yo'l orqali). `inter_page_sleep` param **olib tashlandi**.
- `app.py` `_fbs_sync_one_token` — coarse `with token_lock` va inter-status `_t.sleep(1.0)` **olib tashlandi**; gate hammasini pace qiladi.
- `core/fbs_data.py` 3 ta JIT yo'li — `lock.acquire(timeout=5)/skip` **olib tashlandi**; first-page to'g'ridan-to'g'ri chaqiruv oldidan `pace_uzum_call`.
- O'lik `_fbs_get_token_lock` import'lari (app.py, fbs_data.py) tozalandi; `get_token_lock` util fbs_locks'да qoldi.

**Test:** `tests/test_fbs_locks.py` — 9 ta deterministik test (first-call no-wait, 1s spacing, gap-no-wait, per-token mustaqillik, same-instant concurrency, custom interval). **Jami 84 test o'tdi.**

**Natija:** Yangilash mid-tick'да endi **jonli ishlaydi** (skip emas), ~1-2s paced kutadi (worker bir vaqtда faqat 1 slot oldinга band qiladi → kutish chegaralangan). Burst invarianti in-process saqlanadi.

**⚠️ Ma'lum cheklov (B tanlanгани bois):** cross-process pacing kafolatlanmaydi (worker 1 jarayonда, Yangilash istalganида). Hozirgi kod ham shunday edi, ban bo'lmagan. To'liq kafolat kerak bo'lsa — Redis-asoslangan limiter (A), keyinги bosqich.

**Review (7-agent adversarial) → 3 topilma, 2 tuzatildi:**
- ✅ MEDIUM: `_token_next_slot` cheksiz o'sishi → `prune_token_slots(active_tokens)` qo'shildi, har tick'да chaqiriladi (o'chgan sotuvchilar token'larini tozalaydi). 4 test.
- ✅ MEDIUM: `debug_routes.py` probe route pacing'siz 11 chaqiruv → `pace_uzum_call` qo'shildi (debug ham real token'ни penalty'ga tushirmasin).
- ℹ️ LOW: gunicorn thread-pool — review xavf past tasdiqladi, tuzatish shart emas (sleep'lar parallel, GIL bo'shaydi). **Jami 88 test o'tdi.**

### 🔴 Bosqich A.8 — 429 burst penalty ildiz sababi va tuzatish (2026-05-30)

**Muammo:** Yangilash sekin/spinner abadiy aylanadi. **Ildiz sabab (4-agent tracing workflow → 44 manba):** token 429 (burst penalty) oladi, chunki bir nechta joyда **concurrent pacing'siz** Uzum chaqiruvlari otiladi. Gate faqat `/orders` ni qoplagan edi.

**Manbalar (chastota bo'yicha):**
1. ⭐ **Har Yangilash bosishда 3 ta `/count` BIR VAQTDA** (`_refresh_shops_counts` `ThreadPoolExecutor(3)`) + 1 `/orders` (Promise.all) = ~4 chaqiruv <1s. "/count burst-exempt" — **tasdiqlanmagan taxmin**, ehtimol noto'g'ri.
2. **Bulk confirm** — 5 ta `/confirm` bir vaqtда (`fbs/routes.py:1750`, `ThreadPoolExecutor(5)`), pacing'siz.
3. **Bulk print** — 5 ta label chaqirig'i bir vaqtда (`fbs/routes.py:1601`), pacing'siz.
4. **Cross-process** worker+JIT `/orders` (gate in-process).
- **Kuchaytirgich:** 429 → `http_client` 60/120/180s retry = **6 daqiqa** osilish (spinner). Va **429 logi yo'q** edi.

**Official `/count`:** swagger'да status'iga **bitta integer** qaytaradi → 3→1 collapse **mumkin emas** (per-status). Demak yagona yo'l — pacing.

**BAJARILDI ✅ (88 test o'tdi):**
- **#1** — `fetch_fbs_orders_count` ichida `pace_uzum_call(token)` ([uzum_openapi.py](../core/uzum_openapi.py)); `_refresh_shops_counts` **ketma-ket** (ThreadPoolExecutor olib tashlandi). → 3 concurrent `/count` burst **yo'qoldi**. Endi `/count` ham `/orders` bilan bir gate'да. Narx: refresh ~3s (lekin 429/6-daqiqa xavfi yo'q).
- **#2** — `_fbs_orders_request_with_auth` da **429 BURST logi** (greppable: `grep BURST`). Endi burst'ни tasdiqlash mumkin.
- **#3** — `templates/fbs_orders.html` `fetchWithTimeout` (15s) — spinner backend osilsa ham **doim to'xtaydi**.
- **#4** — **action endpoint'lar pace qilindi** (96 test o'tdi, pytest-first). `confirm_fbs_order`, `cancel_fbs_order`, `download_fbs_label`, `attach_fbs_identifiers` — har biri `_fbs_orders_request_with_auth` chaqiruvидан oldin `pace_uzum_call(token)` (tozalangan token bilan). → bulk confirm (5 concurrent) va bulk print (5 concurrent) endi gate orqали ~1s oraliqда interleave bo'ladi, burst yo'q. Narx: 5 ta order ≈ 4s + HTTP (xavfsiz, lekin tez emas); label cache (5min) takroriy bosishларни 0s qiladi. Test: [tests/test_fbs_action_pacing.py](../tests/test_fbs_action_pacing.py) (8 test — pace BEFORE request + bir xil token kalit). Bulk route kommentlari yangilandi (eski "per-token mutex" → gate).

**QOLDI ⏳:**
- **Cross-process** (#3 manba) — to'liq yopish uchun Redis-based limiter (variant A). Hozircha kerak emas (in-process gate + Uzum cross-process near-collision'ни kechiradi).

---

## A.9 — #5 interaktiv fail-fast retry (workflow bilan loyihalandi)

**Muammo (workflow tahlili — 7 agent, blast-radius + 3 adversarial lens):** 429/5xx kelса `http_client` shared session 60/120/180s = ~6 daqiqa **bitta `sess.request()` ICHIDA** retry qiladi. Bu fon worker uchun OK, lekin **interaktiv** yo'l uchun halokatли: brauzer AbortController (#3 15s / #4 60s) faqat **brauzerни** ozod qiladi — gunicorn worker thread'и to'liq 6 daqiqa retry'ни oxirigача ishlatadi (requests yopilgan klientдан abort signalини olmaydi). Yuklamада N throttled seller = N osilgan thread → pool bo'g'iladi → **butun ilova osiladi**.

**Dizayn (adversarial tuzatishlар bilan):** `http_client.py` ga TEGILMAYDI (finance/products/POS/reports himoyalanadi). `core/uzum_openapi.py` ga alohida **`_get_fbs_fastfail_session()`** (`Retry(total=0)` = 0 retry, 0 backoff sleep, `raise_on_status=False` → 429/5xx oddiy tuple qaytadi, error shape o'zgarmaydi). Chokepoint'га `fail_fast: bool=False` flag; interaktiv chaqiruvlар `True`, fon worker + cancel Phase-4 daemon `False` (explicit ARGUMENT, thread-local emas — daemon patient qoladi). **`fail_fast=False` standart → tushirib qoldirilган har qanday yo'l patient qoladi (xavfsiz, regressiyа yo'q).**

**Workflow topgan 4 ta xato (qo'lда o'tkazib yuborilardi):** (1) plumbing teskari — routes → fbs_data **wrapper** → openapi (3 qatlam); (2) `_apply_action_response_to_db` ни cancel daemon ham chaqiradi → `True` hardcode patient daemon'ни buzardi; (3) `fetch_fbs_orders_page` 2 ta chokepoint chaqiruvи (559+584); (4) `_uzum_error_response` 429'ни code/message'дан **keyин** tekshiradi → friendly hint ko'rinmasdi.

**BAJARILDI ✅ — 5a (Yangilash list/count yo'li, 112 test o'tdi):**
- `core/uzum_openapi.py` — `_get_fbs_fastfail_session()` + `_fbs_orders_request_with_auth(fail_fast=)`; `fetch_fbs_orders_page` (IKKALA chaqiruv) + `fetch_fbs_orders_count` flag.
- `core/fbs_sync.py` — `fetch_all_pages(fail_fast=False)` → page'га forward (worker default False = patient).
- `core/fbs_data.py` — 4 ta JIT caller `fail_fast=True`: `_refresh_shop_status`, `_refresh_shops_status`, `_refresh_shops_status_first_page`, `_refresh_shops_counts`.
- `fbs/routes.py` — `_uzum_error_response` 429 tarmoqи **birinchи** (friendly "Uzum band, kuting" + HTTP 429, code bo'lса ham).
- `templates/fbs_orders.html` — loadOrders 429 → passiv toast (auto-retry'siz). **Eslatma:** list/count refresh **best-effort** (429 ни yutib, DB 200 qaytaradi) — 5a'да bu tarmoq uxlab turadi, 5b action endpoint'larида yonadi.
- Test: [tests/test_fbs_fastfail.py](../tests/test_fbs_fastfail.py) (16 test — session total=0/distinct, chokepoint tanlash, 429 one-shot, forwarding, **background-patient** invariant, JIT=True, route 429).

**BAJARILDI ✅ — 5b (action + DBS + detail, 142 test o'tdi, pytest-first):**
3-qatlam plumbing (route → fbs_data wrapper → openapi → chokepoint) bo'ylab `fail_fast` o'tkazildi:
- `core/uzum_openapi.py` — 10 funksiya: confirm/cancel/attach/label/detail/dropoff/return-reasons + DBS (delivering/completed/refund).
- `core/fbs_data.py` — 11 wrapper: confirm_order, cancel_order, attach_identifiers, get_label_pdfs, `_get_label_pdfs_cached`, get_fbs_order_detail, `_apply_action_response_to_db`, get_return_reasons, dbs_delivering/completed/refund.
- `fbs/routes.py` — 13 interaktiv call-site `fail_fast=True` (single confirm/cancel/label/identifier, bulk-confirm + bulk-labels pool.submit, DBS, detail, pre-flight, dropoff, return-reasons).
- **KRITIK invariant:** cancel Phase-4 fon daemon (`_bg_refetch`) `_apply_action_response_to_db(..., fail_fast=False)` — **aniq** patient (response yuborilgandan keyин ishlaydi, throttle'дagi token'ни urmaslik uchun).
- Test: [tests/test_fbs_action_fastfail.py](../tests/test_fbs_action_fastfail.py) (30 test — 10 openapi forward True+default, 9 wrapper forward, **cancel daemon patient** invarianti). [test_fbs_cancel_order.py](../tests/test_fbs_cancel_order.py) yangi imzога moslandi.

**Xavfsizlik to'ri:** `fail_fast=False` standart → faqat route'lar `True` uzatadi. Worker, cancel daemon, va men o'tkazib yuborgan har qanday yo'l **patient qoladi (regressiyа yo'q)**.

**QOLDI ⏳:** invoice funksiyalари (create/list/time-slots) ataylab patient qoldirildi (kam ishlatiladi, `fail_fast=True` bilan keyин qo'shilishi mumkin). DBS funksiyalарini `pace_uzum_call` bilan #4 darajасида gate qilish — alohida kichik follow-up.

---

## 8. HAR re-verifikatsiya (jonli fayllar) — 2026-05-30

> Abdulaziz haqiqiy HAR fayllarini sahifa-ba-sahifa olib kelmoqda. Men har faylni kod bilan solishtirib xatolarni yig'aman; oxirida guruhlab tuzatamiz. **Manba: ichki API `api/seller/fbs/...` (rasmiy `seller-openapi` EMAS) — farqlar belgilangan.** Rasmiy spec uchun esa swagger «Try it out» (Eshik 2).

#### 🗂️ Yig'ilgan tuzatishlar (RUNNING — har HAR'dan keyin yangilanadi; HALI TUZATILMAGAN)

| ID | Jiddiylik | Xato | Joy | Risk | Manba |
|---|---|---|---|---|---|
| **N4** | **HIGH** (shartli) | **Merge-don't-blank (Fix #8) — KOD TASDIQLANDI.** upsert HAR ustunni `excluded.<col>` bilan SO'ZSIZ ustiga yozadi (COALESCE yo'q). Ingichka tick → `stock`/`accept_until`/sanalar **NULL bilan o'chadi**. Yagona yozuv yo'li (replace_orders o'lik kod). | [fbs_sync.py:493-501](../core/fbs_sync.py#L493) + [:322-355](../core/fbs_sync.py#L322) | o'rta (sync correctness) | A.13 |
| **N4a** | **HIGH** (shartli) | **DBS misclassify.** `order_type = scheme or "FBS"` → ingichka DBS (`scheme:null`) **doimiy FBS** bo'lib qoladi → noto'g'ri UI workflow (FBS tugmalari), DBS filtrда ko'rinmaydi. Detail DB-first → o'z-o'zini tuzatmaydi. | [fbs_sync.py:328](../core/fbs_sync.py#L328) | o'rta | A.13 |
| **N4b** | MEDIUM (shartli) | **Action-echo thin-write.** confirm echo'sида top-level `id` bo'lsa → verbatim upsert (refetch/merge yo'q). Ingichka echo → tasdiqlashdan keyин ombor bo'shaydi (~10 daq o'zini tiklaydi). | [fbs_data.py:690-716](../core/fbs_data.py#L690) | o'rta (interaktiv) | A.13 |
| **N5** | ENHANCE ⭐ | **Deadline sort.** PACKING/aktiv buyurtmalar `date_created DESC` bo'yicha — `deliverUntil` muddatига emas → eng shoshilinchи pastда qoladi, o'tkazib yuboriladi. | [fbs_data.py:163,551](../core/fbs_data.py#L163) | past (UX, katta foyda) | A.13 |
| **N6** | ENHANCE | List jadvalда `deliverUntil`/`acceptUntil` **muddat ustuni yo'q** (faqat `dateCreated`). Sotuvchi har buyurtmани ochishi kerak. | [fbs_orders.html:1094,1118](../templates/fbs_orders.html#L1094) | past (presentation) | A.13 |
| **N1** | MEDIUM | List thumbnail `productImage` o'rniga faqat `photo` o'qiydi → rasm bo'sh | [fbs_orders.html:1068](../templates/fbs_orders.html#L1068) | past (frontend, brauzer-verify) | A.11 |
| **N2** | LOW | `dropOffPoint.type` (ISSUE_POINT) ustun yo'q (raw_json'da bor) | models.py, fbs_sync.py + migration | o'rta (migration) | A.11 |
| **N3** | ENHANCE | `x-ratelimit-*` headerlardan haqiqiy burst-limit o'qish (1s taxmin o'rniga) | uzum_openapi.py, fbs_locks.py | o'rta | A.11 |
| **O1** | ENHANCE | FBS rating/lockout badge — `fetch_fbs_rating` yo'q (rasmiy endpoint tekshirilsin) | uzum_openapi.py + UI | o'rta | A.12 |

> **DISMISSED:** F1 (`places`) — rasmiy endpointда yo'q (A.11). · **O3 (termal printer)** — local presetlar allaqachon termal-roll PDF beradi; rasmiy endpoint `printType` qabul qilmasligi mumkin → spekulyativ qo'shilmaydi (A.13).
>
> ⚠️ **N4/N4a/N4b — barchasi SHARTLI:** rasmiy v2 `/orders?status=PACKING` ingichka qaytaradimi degan savolga bog'liq (faqat ichки-v1 ingichkaligi isbotlangan). **Lekin tuzatish (defensive merge) har holda to'g'ri** — rasmiy javob shaklidan qat'i nazar. Gating probe: bitta read-only swagger «Try it out» `status=PACKING`.

#### 💎 Imkoniyatlar (OPPORTUNITIES — foydali ma'lumot/feature, xato emas)

| ID | Qiymat | Imkoniyat | Manba endpoint | Eslatma |
|---|---|---|---|---|
| **O1** | YUQORI | **FBS rating + lockout ogohlantirishi** (★ badge) — `successRate`, `criticalFlag`, `lockoutDays`, `minimumOrders` | `/fbs/rating/{sellerId}` | rasmiy ekvivalentini swagger'дан tekshirish kerak |
| **O2** | O'RTA | **Bekor jarima** (`penaltyParameters`, `cancelPrice`) — bekordan oldin ogohlantirish | `/fbs/order/{id}` (faqat detail) | ichки; rasmiy detail bermаydi (ENDPOINTS.md) — tekshirish |
| **O3** | PAST | **Termal printer** qo'llab-quvvatlash (`BIG → THERMAL_PRINTER`) | `/fbs/order/labels/sizes` | `printTypes` maydoni |
| **O4** | PAST | Shop logo + 2-tilli tavsif | `/shop/` | `chatAvatarUrl`, `description{uz,ru}` |

### A.12 — Order detail + rating sahifa HAR (`...noviye,zakaz.uz.har`, 26 entry, 2026-05-30)

**Endpoint:** `/fbs/order/{id}`, `/fbs/rating/{sellerId}`, `/fbs/rating/{sellerId}/references`, `/fbs/order/labels/sizes`, `/shop/`, `/notifications`.

**💎 Eng qimmatli — FBS rating (★0% badge'ning manbai):**
```
successRate: 0.0, allOrders: 2, cancelOrders: 2, criticalFlag: true, warningFlag: true
references: {criticalRate:0.8, warningRate:0.9, lockoutDays:3, calculateDays:8, minimumOrders:10, lockEnabled:true}
```
- Sotuvchi 2 buyurtmани bekor qilgan → successRate 0% → **criticalFlag** → **FBS bloklanish xavfi** (8 kunда, agar ≥10 buyurtma'дан successRate<0.8 bo'lsa → 3 kun lock). Bu **O1**. SellerHub'да ko'rsatсak — katta foyda (sotuvchini lockdан saqlaydi).
- `rating` javobi `references`ни ICHIDA beradi → alohida `/rating/{id}/references` chaqirig'i **ortiqcha** (Uzum frontend ikkalasini chaqiradi — biz faqat `/rating` olсak yetadi).

**✅ Detail≈list tasdiqlandi (yana):** order `109155748` list bilan AYNAN bir xil + **2 qo'shimcha** maydon: `cancelPrice:null`, `penaltyParameters:null` (faqat detail'да — **O2**). Bu ichки detail; rasmiy `/v1/fbs/order/{id}` bu 2 maydonни beradimi — swagger'дан tekshirish kerak (A.3'дagi gap).

**🟢 labels/sizes — `printTypes` topildi:** `LARGE`→[A4], `BIG`→[A4, **THERMAL_PRINTER**]. Bizning label kod LARGE/BIG bor, lekin `printTypes`/termal printer variantini ko'rsatmaydi (**O3**).

**🟢 /shop/ — boyitish:** `chatAvatarUrl` (do'kon logosi), `description{uz,ru}`, `skuTitle` prefiks. (**O4**). Eslatма: shop `3442 LUGOOD transferred:true` — FBS shopIds ro'yxatида yo'q (o'tkazilган do'kon).

**Tezlik:** detail sahifa allaqachon DB-keshдан o'qiladi (tez). Bu HAR'да yangi sekinlik manbai yo'q. Rating/shop/labels-sizes — kamdan-kam o'zgaradigan reference ma'lumot → uzoq TTL kesh mos.

**Xato yo'q** bu HAR'да (faqat imkoniyatlar). Running tally o'zgarmadi (N1-N3).

### 🔴 A.13 — «В сборке» (PACKING) HAR + 22-agent adversarial workflow (2026-05-30)

> 2 HAR (`...vsbore.uz.har`, `...vsbore1.uz.har`) — ikkalasi GET-only. Ichki **v1 `/fbs/orders?status=PACKING`** LIST javobi **INGICHKA** (`stock=null, scheme=null, carrierCode=null, acceptUntil=null`); o'sha order DETAIL'i (`/fbs/order/{id}`) **TO'LIQ**. Workflow: 4 dimension finder → har topilma adversarial verify (refute-first). 22 agent, ~1M token.

**🎯 MARKAZIY XULOSA — Fix #8 (merge-don't-blank) KOD DARAJASIDA TASDIQLANDI:**
- [fbs_sync.py:493-501](../core/fbs_sync.py#L493) `upsert_orders` ON CONFLICT DO UPDATE — har ustun `excluded.<col>` ga SO'ZSIZ map qilinadi. **COALESCE yo'q, null-guard yo'q** → REPLACE, MERGE emas.
- `dict_from_order` ingичка qatorни nullга aylantiradi: `stock={}`→stock_id/title None; `acceptUntil:null`→accept_until None; **`scheme:null`→order_type "FBS"** (DBS'ни noto'g'ri tamg'alaydi); `orderItems or []`→bo'sh massiv yozadi.
- **Yagona yozuv yo'li:** `replace_orders` — **o'lik kod** (0 call-site). Worker (app.py:4332) + 3 JIT yo'li ([fbs_data.py:233,269,352](../core/fbs_data.py#L233)) hammasi `upsert_orders` ishlatadi.
- **Seller-ko'rinadi:** `get_fbs_order_detail` **DB-first** ([fbs_data.py:488-493](../core/fbs_data.py#L488)) — faqat DB-MISS'да live detail oladi. PACKING doim DB'да → bo'shatilган qator to'g'ridan-to'g'ri ko'rsatiladi (bo'sh «Ombor», yo'qolган muddat).

**Klassifikatsiya: CONDITIONAL** — rasmiy v2 `/orders?status=PACKING` ingичка qaytarsa otadi (faqat ichki-v1 isbotlangan; rasmiy v2 swagger to'liq sxema reklama qiladi, lekin real PACKING populatsiyasi NOMA'LUM). **Lekin defensive merge har holда to'g'ri** → hozir tuzatish kerak.

**3 ta blast-zona:**
1. **Data blank** (N4): stock_id/title, accept_until, drop_off uuid/address, lifecycle sanalar, raw_json (+ carrierCode/publicId/penaltyParameters faqat raw_json'да) → bo'shaydi.
2. **DBS misclassify** (N4a, eng yomon): `scheme:null→"FBS"` **doimiy** → detail UI `order.scheme==="DBS"` ga bog'liq ([fbs_order_detail.html:538-543](../templates/fbs_order_detail.html#L538)) → FBS tugmalari ko'rsatiladi, DBS delivering/completed YO'Q → sotuvchi yetkazolmaydi → SLA penalty. DBS filtrда ham ko'rinmaydi ([fbs_data.py:136,531](../core/fbs_data.py#L136)).
3. **Action-echo** (N4b): `confirm_order`→`_apply_action_response_to_db` Strategy 1 (echo'да `id` bo'lsa) verbatim upsert, refetch/merge yo'q ([fbs_data.py:690-716](../core/fbs_data.py#L690)). Ingичка echo → tasdiqlashdан keyin ombor bo'shaydi (~10 daq worker tiklaydi).

**⚠️ Tuzatish nozikligi:** SQL COALESCE **yetarli emas** — `dict_from_order` null-signalни SQLдан OLDIN yo'qotadi:
- `order_type = scheme or "FBS"` → ingичка DBS "FBS" bo'lib keladi (null emas) → COALESCE tiklay olmaydi. **Projeksiyani None o'tkazadigan qilish** (yoki DBS-signaldan infer).
- `items_json = orderItems or []` → bo'sh massiv truthy-overwrite. **Guard kerak.**
- `status`/`price`/`synced_at`/`raw_json` — yangi qiymatни OLISH kerak (merge emas).

**💎 Imkoniyatlar (workflow tasdiqladi):**
- **N5 ⭐ (deadline sort):** list `date_created DESC` ([fbs_data.py:163,551](../core/fbs_data.py#L163)) — `deliverUntil` muddatига emas. PACKING/DELIVERING uchun `deliver_until ASC nulls_last` → eng shoshilinchи tepada. Katta UX foyda, kichik o'zgarish.
- **N6 (deadline ustun):** jadvalда muddat ustuni yo'q → «Muddat» ustuni qo'shish (CREATED→acceptUntil, aks holда deliverUntil).
- **O1 (rating):** `fetch_fbs_rating` yo'q — tasdiqlandi. Rasmiy endpoint tekshirilsin.
- **O3 (termal printer):** DISMISSED — local presetlar termal-roll PDF allaqachon beradi; spekulyativ qo'shilmaydi.

**Gating probe (N4/N4a/N4b uchun):** rasmiy swagger «Try it out» `GET /v2/fbs/orders` **`status=PACKING`** (+ iloji bo'lsa DBS order) → Execute → `stock`/`scheme`/`acceptUntil` null'mi tekshirish. Bu shartli xatolarni «confirmed-real» yoki «theoretical» ga aylantiradi.

### A.10 — «Новые» (CREATED) HAR (`seller.uzum,noviye.uz.har`, 27 entry)

**Tekshirilgan endpointlar:** `/fbs/v2/orders`, `/fbs/orders/count`, `/fbs/order/return-reasons`, `/fbs/legal-info`, `/fbs/locks`, `/storage`.

**🔴 TOPILGAN XATO (actionable):**
- **F1 (HIGH) — `places=STOCK,DROP_OFF` tushib qolgan.** Ichki frontend buni HAM `/v2/orders` HAM `/orders/count` ga yuboradi (tasdiqlandi, query'da ko'rindi). Bizning kod ([uzum_openapi.py:625-635](../core/uzum_openapi.py#L625) `fetch_fbs_orders_page` + [:797-803](../core/uzum_openapi.py#L797) `fetch_fbs_orders_count`) `places` YUBORMAYDI. **Xavf:** rasmiy endpoint `places`siz STOCK-only default qilsa, DROP_OFF buyurtmalar sync'dан tushib qoladi (234 vs 252 to'liqlik gap'ига bog'liq bo'lishi mumkin). → Roadmap HIGH #1 ni JONLI tasdiqladi. **Tuzatish:** ikkala chaqiruvga `places=STOCK,DROP_OFF` qo'shish (additive, read-only, past risk). Deploy oldidan backend probe: places bilan/siz count'ni solishtirish.

**✅ ALLAQACHON HAL QILINGAN (HAR tasdiqladi — xato YO'Q):**
- **Item kalitlari** `photo`/`price`/`title`/`skuTitle` (NOT `productImage`/`sellerPrice`/`productTitle`). Template ikkala to'plamni fallback bilan o'qiydi ([fbs_order_detail.html:463-470](../templates/fbs_order_detail.html#L463), [fbs_orders.html:1063-1068](../templates/fbs_orders.html#L1063)). → **HIGH #4 yopildi.**
- **Photo strukturasi** `photo.photo.{800,720,...}.{high,low}` + `photoKey` — template dinamik resolution kalitlarini hal qiladi.
- **return-reasons** `{payload:{reasons:[{reason,title}]}}` (obyekt, bare list emas) — parser `reasons/items/data` kalitlarини tekshiradi ([uzum_openapi.py:1545-1549](../core/uzum_openapi.py#L1545)). → bo'sh dropdown xavfi YO'Q.
- **Sanalar epoch-ms** (`dateCreated:1780118955951`, 13 raqam) — `parse_iso_naive_utc` epoch-ms ni ham hal qiladi (test bor). → xato yo'q.
- **Status enum** — count 11 statusни berdi, `FBS_ORDER_STATUSES` bilan MOS (yana tasdiq).

**ℹ️ DIVERGENSIYA (ichki ≠ rasmiy, harakat YO'Q — prod ishlaydi):**
- **count shakli:** ichki `{payload:{ordersCount:[{status,counter}]}}` (1 call, 11 status). Bizning kod rasmiy `payload:<int>` (bare int, per-status, 11 call) kutadi — prod ishlaydi → rasmiy bare int. 11→1 collapse faqat ichki endpointga o'tsak mumkin (o'tmaymiz).
- **`status` (bizда, singular) vs `statuses` (ichки, plural)** — prod ishlaydi → rasmiy `status` qabul qiladi.

**🟡 LOW / ixtiyoriy (raw_json'da bor, yo'qolmaydi):**
- `sortBy=CREATED_DATE&sortOrder=DESC` biz yubormaymiz (faqat tartib).
- Yangi maydonlar surface qilinmagan: `publicId` («856114-0033»), `carrierCode` («DEFAULT»), `acceptanceProlongationsCount`, `barcode`, `weight`, `skuDimension`, `identifierInfo` — penalty/timer UI uchun (Roadmap LOW #14).
- `stock` faqat `{id,title}` ga denormalizatsiya; to'liq `{externalId,address,poolSource,dimensionalGroups}` raw_json'da (detail o'qiy oladi).

**Yangi manba:** `/storage` (onboarding flaglar), `/fbs/locks` (`payload:null` = lock yo'q), `/fbs/legal-info` (komitent: «ИП PATTAXOV ULUG'BEK ISKANDAROVICH» + 10 shop).

### ⭐ A.11 — RASMIY swagger `GET /v2/fbs/orders` (2026-05-30, «Try it out» skrinshot — ESHIK 2, haqiqiy spec)

> **Bu yagona haqiqat manbai** — bizning kod aynan shu rasmiy OpenAPI'ni chaqiradi. Ichki HAR (A.10) bilan solishtirma quyida. **Ko'p taxminlar teskari chiqdi.**

**🔄 F1 (`places`) — BEKOR QILINDI (xato EMAS):**
- Rasmiy `/v2/fbs/orders` Parameters: `shopIds`(req), `status`, `scheme`, `dateFrom`, `dateTo`, `page`, `size`. **`places` PARAMETRI YO'Q.**
- Demak `places` — faqat ichki frontend parametri. Rasmiy endpointga qo'shsak → e'tiborsiz qoldiriladi yoki 400. **Bizning kod to'g'ri ishlagan (yubormagan).** DROP_OFF buyurtmalar default holda keladi (rasmiy filter yo'q). → **234vs252 gap places'dан EMAS** (shop-gate/MAX_PAGES/stop_on_known'дан). Ehtiyotkorlik o'zini oqladi.

**🔴 YANGI HAQIQIY XATO (tuzatish kerak):**
- **N1 (MEDIUM) — List sahifa thumbnail bug.** Rasmiy item shakli `productImage` ishlatadi (NOT `photo`). [fbs_orders.html:1068](../templates/fbs_orders.html#L1068) `firstItem.photo && firstItem.photo.photo` — faqat `photo`ni tekshiradi → **rasmiy ma'lumotда thumbnail bo'sh chiqadi**. Detal sahifa to'g'ri (`it.photo || it.productImage`, [:463](../templates/fbs_order_detail.html#L463)); faqat list buzuq. **Tuzatish:** `const pimg = firstItem.productImage || firstItem.photo; const photoSet = pimg && pimg.photo;` (frontend-only, ban xavfi yo'q, brauzer-verify).

**🔑 ASOSIY TUSHUNCHA — rasmiy ≠ ichki item kalitlari (TESKARI!):**
| Maydon | Ichki (Eshik 1, A.10) | **Rasmiy (Eshik 2, bizники)** |
|---|---|---|
| Rasm | `photo` | **`productImage`** |
| Narx | `price` | **`sellerPrice`** |
| Nom | `title` | **`productTitle`** |
| Sana | epoch-ms (`1780...951`) | **ISO string** (`2026-05-30T13:25:14.848Z`) |
| Qo'shimcha | `barcode,weight,skuDimension` | `commission,sellerProfit,purchasePrice,logisticDeliveryFee,amountReturns,withdrawnProfit` (finance) |

→ Bizning app **rasmiy**ni oladi: detal sahifa dual-key tufayli ishlaydi; list faqat rasmда buzuq (N1). `parse_iso_naive_utc` ISO + epoch ikkisini ham hal qiladi ✅.

**✅ TASDIQLANDI — to'g'ri (xato yo'q):**
- `status` (singular) — rasmiy shunaqa. Bizники to'g'ri.
- `sortBy`/`sortOrder` — rasmiyda YO'Q. Bizники yubormaydi → to'g'ri (ichki-only edi).
- **`scheme` bo'sh → FBS+DBS ikkalasi** ("Если ничего не передать, вернутся и FBS и DBS"). Worker `scheme` yubormaydi ([fbs_sync.py:276-281](../core/fbs_sync.py#L276)) → **DBS yo'qolmaydi** ✅. `dict_from_order` per-order `scheme` o'qiydi.
- `stock` to'liq (externalId/address/poolSource/dimensionalGroups) — raw_json'da; detal o'qiy oladi.
- Status enum 11 ta — mos.

**🟡 LOW / kelajak:**
- **N2 — `dropOffPoint.type`** rasmiyda bor (`ISSUE_POINT`); ustun sifatida saqlanmaydi (raw_json'da) — Roadmap #6.
- **N3 (enhancement) — Rate-limit headerlar!** Rasmiy javob `x-ratelimit-remaining`, `x-ratelimit-burst-capacity`, `x-ratelimit-replenish-rate`, `x-ratelimit-limit-per-day`, `x-ratelimit-remaining-per-day` qaytaradi. → A.7/A.8 burst-pacing'ни taxminiy 1s o'rniga **haqiqiy limit**ga qarab boshqarish mumkin. Katta imkoniyat.
- **400 misol:** `seller-order-12 — dateTo is before dateFrom` (error kod katalogiga).
- Rasmiy javobда `publicId`/`carrierCode`/`acceptanceProlongationsCount` **YO'Q** (ular ichки-only) → Roadmap LOW #14 (bu ustunlar) rasmiy uchun befoyda.

### ✅ A.14 — «В поставке» (PENDING_DELIVERY) HAR — N4 otish ehtimoli hal qilindi (2026-05-30)

> 2 HAR (`...vPOSTAFKE.uz.har`, `...vPOSTAFKE1.uz.har`) — ikkalasi GET-only. **«В поставке» tab `/fbs/v2/orders` (v2!) ishlatadi** — «В сборке» (v1) dan farqli.

**🎯 HAL QILUVCHI DALIL — v2 o'tgan statusda ham TO'LIQ:**
Ichki **v2** `/fbs/v2/orders?status=PENDING_DELIVERY` LIST javobi (va detail) **to'liq**:
`stock={...}`, `scheme=FBS`, `carrierCode=DEFAULT`, `acceptUntil=to'la`, `dropOffPoint={...}`, `timeSlot={...}`, `invoiceNumber=120000958392`, `deliverUntil=to'la`.

→ **Ingичkalik FAQAT v1 endpointга xos** (PACKING tab v1 chaqiradi). Bizning kod **faqat v2** chaqiradi ([uzum_openapi.py:637](../core/uzum_openapi.py#L637); v1 legacy faqat `UZUM_FBS_PROBE_LEGACY=1` da). Hozirgача har bir v2 namuna (CREATED, PENDING_DELIVERY) **to'liq**, biror v2 ingичка yo'q.

**📉 N4/N4a downgrade:** list-tick'дan NULL-overwrite **deyarli otmaydi** (v2 to'liq qaytaradi). Defensive merge baribir arzon va to'g'ri (regressiya himoyasi), lekin endi **PAST shoshilinch**. **N4b (action-echo) HALI OCHIQ** — confirm/cancel echo shakli hali ko'rilmagan (list endpointдan mustaqil). Rasmiy «Try it out» `status=PACKING` — endi ixtiyoriy yakuniy tasdiq.

**📦 Qo'shimcha kuzatuvlar:**
- Bu buyurtmada **`invoiceNumber=120000958392` to'la** (PENDING_DELIVERY → поставкага biriktirilган). CREATED/PACKING'да null edi. Invoice-join (Roadmap #10, `fbs/routes.py:2575-2585`) shu yerда ishlaydi — «Поставки» HAR kelганда tekshiramiz.
- **`dropOffPoint` to'la** (bu DROP_OFF buyurtma) — DROP_OFF sync'дан o'tdi → F1 (places) bekorini yana tasdiqlaydi.
- **`timeSlot` to'la** — yetkazib berish oynаси; ustun emas (raw_json'да), detail o'qiy oladi.
- **N5 (deadline sort)** kuchaydi — PENDING_DELIVERY'да ham `deliverUntil` muddati bor.

**Running tally:** N4/N4a/N4b PAST shoshilinchga tushdi; yangi xato yo'q.

### ✅ A.15 — «Приняты Uzum» (DELIVERED) HAR (`...PRINETIY.uz.har` x2, 2026-06-01)

> 2 HAR, GET-only. v2 endpoint. **Tab→status: «Приняты Uzum» = `DELIVERED + DELIVERING + ACCEPTED_AT_DP`** (3 status, bitta CSV call: `statuses=DELIVERED,DELIVERING,ACCEPTED_AT_DP`).

**✅ v2 yana TO'LIQ (3-tasdiq):** DELIVERED order (id 69451254) list+detail — `stock`(8 kalit), `scheme=FBS`, `carrierCode`, `dropOffPoint`(3), `timeSlot`(7), `invoiceNumber=120000259258`, `acceptedDate`, `deliveryDate` — hammasi to'la. → CREATED, PENDING_DELIVERY, DELIVERED ning hammasi v2'da to'liq → **N4 downgrade mustahkamlandi**.

**💎 N7 (ENHANCE, probe kerak) — multi-status CSV batch:** ichki v2 `statuses=DELIVERED,DELIVERING,ACCEPTED_AT_DP` ni **bitta call**да qabul qiladi. Bizning kod `status=` (singular) — har status alohida call ([uzum_openapi.py:626](../core/uzum_openapi.py#L626)). **Agar rasmiy v2 ham CSV/multi `status` qabul qilsa** → terminal/grouped statuslarни 1 call'да olib, **chaqiruvlar 3× kamayadi** (burst + tezlik). Rasmiy swagger `status` ni single-string deydi, lekin Spring ko'pinча CSV→List bind qiladi → **probe: swagger «Try it out» `status=DELIVERED,DELIVERING`**.

**Reconfirm:** detail-only `cancelPrice`/`penaltyParameters` (O2) yana ko'rindi. `acceptedDate`/`deliveryDate` DELIVERED'да to'la — `dict_from_order` ularni oladi ✅.

**UI mapping eslatma:** bizning app har statusни **alohida chip** qiladi ([fbs_orders.html:152-154](../templates/fbs_orders.html#L152)), Uzum 3 tasini birlashtiradi — dizayn farqi, **xato emas** (bizники granular, foydaliroq bo'lishi mumkin).

**Running tally:** +N7 (ENHANCE, probe). Yangi hard-bug yo'q.

### ✅ A.16 — «Ждут выдачи» (DELIVERED_TO_CUSTOMER_DELIVERY_POINT) HAR (`...jdut.uz.har`, 2026-06-01)

> GET-only, **bo'sh tab** (0 buyurtma — kutilgan, screenshot ham 0 edi). v2 endpoint. Yangi xato/imkoniyat yo'q — **tab→status xaritasini yakunlaydi**.

**📋 To'liq tab→status xaritasi (8 tab, HAR'lar bilan tasdiqlangan):**
| Uzum tab | Status(lar) |
|---|---|
| Новые | `CREATED` |
| В сборке | `PACKING` (ichки v1 endpoint!) |
| В поставке | `PENDING_DELIVERY` |
| Приняты Uzum | `DELIVERED` + `DELIVERING` + `ACCEPTED_AT_DP` (CSV) |
| Ждут выдачи | `DELIVERED_TO_CUSTOMER_DELIVERY_POINT` |
| Выданы | `COMPLETED` |
| Отменены | `CANCELED` |
| Возвраты | `RETURNED` |

count tasdig'i: DELIVERED=5 (=«Приняты Uzum 5»), COMPLETED=252, CANCELED=266, RETURNED=192 — A.1/A.10 bilan mos. Running tally o'zgarmadi.

### ✅ A.17 — «Выданы» (COMPLETED) HAR (`...VIDANIY.uz.har` x2, 2026-06-01)

> GET-only (FBS bo'yicha). v2 endpoint, `statuses=COMPLETED`, 20/sahifa (252 jami → paginatsiya ishlaydi). **Yangi xato yo'q.**

**✅ v2 yana TO'LIQ (4-tasdiq):** COMPLETED order (id 75623920) list+detail to'liq — barcha lifecycle sanalari to'la: `dateCreated, acceptUntil, acceptedDate, deliverUntil, deliveryDate, deliveredToDeliveryPointDate, completedDate` + `stock`/`scheme`/`dropOffPoint`/`timeSlot`/`invoiceNumber`. → **CREATED, PENDING_DELIVERY, DELIVERED, COMPLETED — hammasi v2'da to'liq.** N4 downgrade qat'iy.

**POST'lar bor edi, lekin FBS action EMAS:** `sentry.../envelope` (xato-tracking), `/api/auth/seller/check_token` (token validatsiya), `seller-resources.../events` (analitика). FBS confirm/cancel yo'q. → **N4b hali ochiq** (action HAR kerak).

`dict_from_order` barcha sanalarni oladi ✅. detail `cancelPrice`/`penaltyParameters` (O2) yana. Running tally o'zgarmadi.

### ⭐ A.18 — «Отменены» (CANCELED) HAR (`...otmeniniy.uz.har` x2, 2026-06-01)

> GET-only (FBS). v2 endpoint, **`statuses=CANCELED,PENDING_CANCELLATION`** (CSV). v2 CANCELED list+detail **TO'LIQ** (5-tasdiq, stock to'la). FBS action POST yo'q.

**💎 O2 KUCHAYDI — `penaltyParameters` HAQIQIY narvon (ladder) bilan ko'rindi (detail-only):**
```
freeCancellationHours: 12   ·   minutesFromCreation: 2313 (~38.5 soat → 24-48h pog'onasi → 5%)
ladder (yaratilgandan beri soat → jarima):
  0–12h   FREE
  12–24h  3%  (min 10k, max 120k)
  24–48h  5%  (min 10k, max 200k)
  48–72h  6%  (240k) · 72–96h 7% (280k) · 96–120h 8% (320k) · 120h+ 9% (360k)
```
→ **Sotuvchi «Bekor qilish» bosishdan OLDIN jarimani ko'rsatish** = haqiqiy himoya (kutilmagan jarimadan saqlaydi). **O2 endi yuqori qiymatли imkoniyat.** ⚠️ detail-only + ichки → rasmiy `/v1/fbs/order/{id}` `penaltyParameters` beradimi — **probe kerak**. Bizning kesh LIST'дан to'ladi (bu maydon yo'q) + detail DB-first → ko'rsatish uchun cancel-intent'да rasmiy detail fetch kerak.

**📋 Tab mapping aniqlandi:** «Отменены» = `CANCELED + PENDING_CANCELLATION` (A.16 jadvalини yangilaydi).

**Boshqa:** `cancelReason` **erkin matn** bo'lishi mumkin («Заказал не по тому адресу» — mijoz sababi), enum kod emas — `dict_from_order` verbatim saqlaydi ✅. `cancelPrice=null` (mijoz bekor qilган → sotuvchi jarimasi yo'q). dropOffPoint/timeSlot/invoiceNumber null (erta bekor). **Running tally:** O2 yuqoriga ko'tarildi; yangi hard-bug yo'q.

---

### 🔶 A.19 — «Возвраты» (RETURNED) HAR — ALOHIDA endpoint + model (`...vazvratiy.uz.har` x2, 2026-06-01)

> 8/8 tab. **🔴 «Возвраты» butunlay BOSHQA endpoint + shakl ishlatadi** — bu butun auditning eng ahamiyatli strukturaviy farqi.

**Dedicated endpoint:** `GET /api/seller/fbs/v2/orders/returned` (NOT `/v2/fbs/orders?status=RETURNED`!). Query: `scheme, sortBy, sortOrder, shopIds, size` — **`status` YO'Q, `places` YO'Q.**

**Return-summary shakli (standart order'дан butunlay farqli):**
```
{ id, returnDate, returnCount, orderItemsCount, extendedReason, returnDescription,
  lastReturnedItem: {id, skuTitle, barcode, title, amount, photo} }   ← status/stock/scheme YO'Q
```
**Detail item ичида `returns[]`:** `{id, returnedAmount, extendedReason, createdAt, penalty}` — qaytarма sababi + **penalty** + miqdor. (Sana nomlari ham har xil: `createdDate`/`createdAt` vs standart `dateCreated`.)

**🔴 N8 (GAP, probe) — bizning RETURNED to'liq emas bo'lishi mumkin:**
- Bizning kod `RETURNED` ni **standart** `/v2/fbs/orders?status=RETURNED` orqali oladi ([fbs_sync.py:99](../core/fbs_sync.py#L99), FBS_ORDER_STATUSES) — dedicated `/returned` endpoint **YO'Q** (grep tasdiqladi).
- **A.4 dagi «Qaytarildi 183 vs 192» (9 ta kam) gap** ehtimol shundan — ikki endpoint har xil to'plam qaytaradi.
- **PROBE:** rasmiy swagger'da `/v2/fbs/orders/returned` (yoki returns endpoint) bormi? `?status=RETURNED` hammasini (192) beradimi?

**💎 O5 (imkoniyat) — to'liq qaytarма/refund ko'rinishi:** dedicated endpoint return-specific ma'lumot beradi (`returnCount`, per-item `returns[]` + `returnedAmount`/`penalty`/`extendedReason`, `lastReturnedItem`). Hozir biz qaytarмаni oddiy buyurtma sifatida ko'rsatamiz (return detali yo'q). To'g'ri refund-boshqaruv sahifasi uchun shu endpoint manba.

**Eslatма:** `penalty` qaytarмада ham bor (O2 jarima mavzusiga bog'liq). **Running tally:** +N8 (gap/probe), +O5 (imkoniyat).

---

### ⭐⭐ A.20 — RASMIY OpenAPI to'liq endpoint ro'yxati (swagger skrinshot, 2026-06-01) — bir nechta probe HAL QILINDI

> Abdulaziz rasmiy swagger'ning 3 bo'limini ko'rsatdi: **«Работа с заказами FBS/DBS»**, **«FBS Invoice»**, **«Stocks»**. Bu YAGONA HAQIQAT MANBAI — bizning kod aynan shularni chaqiradi.

**📋 Rasmiy FBS/DBS endpointlar (to'liq):**
- Orders: `GET /v2/fbs/orders`, `GET /v2/fbs/orders/count`, `GET /v1/fbs/order/{id}`, `GET /v1/fbs/order/return-reasons`
- FBS action: `POST .../confirm`, `.../cancel`, `.../identifier`, `GET .../labels/print`
- DBS action: `POST /v1/dbs/order/{id}/completed`, `.../delivering`, `.../refund`
- **Invoice:** `GET/POST /v1/fbs/invoice`, `GET /v1/fbs/invoice/{id}`, `POST .../cancel`, `GET .../closing-documents`, **`GET .../orders`**, `GET .../print`, `POST .../update-content`, `GET .../dop/drop-off-points`, `GET/POST .../dop/time-slot`
- **Stocks:** `GET/POST /v2/fbs/sku/stocks`

**🔴 PROBE'lar HAL QILINDI (rasmiy ro'yxatda YO'Q):**
- **PROBE-D → N8/O5 DISMISSED:** rasmiy `/v2/fbs/orders/returned` **YO'Q**. Qaytarmalar uchun yagona rasmiy yo'l — `/v2/fbs/orders?status=RETURNED` (biz aynan shuni ishlatamiz ✅). Ichки `/returned` (returnCount/returns[]/penalty) **rasmiyда mavjud emas** → O5 (boy refund ko'rinishi) qurib bo'lmaydi. **183 vs 192 gap endpoint tanlovидан EMAS** — sync (MAX_PAGES/stop_on_known/timing)дан. Bu gapни sync-correctness sifatида ko'rish kerak, returns-endpoint emas.
- **PROBE-B → O1 DISMISSED (ko'rsatilгan bo'limlarга ko'ra):** rasmiy OpenAPI'да **rating/lockout endpoint YO'Q**. ★ reyting ma'lumoti faqat ichки. → O1 (reyting badge) rasmiy orqали qurib bo'lmaydi. _(Tasdiq: agar swagger'да yana bo'lim bo'lsa tekshirish; hozir 3 bo'limда yo'q.)_

**🟢 YANGI actionable topilmalar:**
- **N9 (MEDIUM) — Invoice-join'ни to'g'rilash rasmiy yo'l bilan.** `GET /v1/fbs/invoice/{invoiceId}/orders` — «накладной bo'yicha FBS buyurtmalар». → bizning LIKE-suffix hack (#10, [fbs/routes.py:2575-2585](../fbs/routes.py#L2575)) o'rniga shu endpoint order↔invoice bog'lashни **aniq** beradi. «Поставки» HAR + bizning invoice kod bilan tasdiqlanadi.
- **O6 (imkoniyat) — FBS SKU ombor boshqaruvi.** `GET/POST /v2/fbs/sku/stocks` — FBS qoldiqlarини o'qish/yangilash. SellerHub'дан FBS stock boshqarish mumkin (hozir yo'q). Yangi feature imkoniyati.

**🟡 OCHIQ qolган (rasmiy detail schema kerak):**
- **PROBE-A → O2:** `GET /v1/fbs/order/{id}` rasmiyда bor, lekin javobда `penaltyParameters`/`cancelPrice` bor-yo'qligi noma'lum. → Abdulaziz shu endpointни swagger'да OCHIB (Response schema) ko'rsatса hal bo'ladi.
- **PROBE-C → N7:** `GET /v2/fbs/orders` `status` CSV qabul qiladimi — «Try it out» bilan.

**Running tally:** N8/O5 DISMISSED · O1 DISMISSED · +N9 (invoice-orders fix) · +O6 (stocks). Confirm/cancel/identifier/label + DBS hammasi rasmiyда tasdiqlandi (bizда bor).

---

### ✅ A.21 — PROBE-A natijasi: rasmiy detail schema → O2 DISMISSED + FAZA 1 tasdiqlandi (2026-06-01)

> Abdulaziz rasmiy `GET /v1/fbs/order/{id}` Response schema'sini ko'rsatdi.

**🔴 O2 DISMISSED:** rasmiy detail javobida **`penaltyParameters` YO'Q, `cancelPrice` YO'Q** (ichki detailда bor edi — rasmiyда YO'Q). → bekor-jarima ogohlantirishi rasmiy API orqali **qurib bo'lmaydi**. Endi O1, O2, O5 — uchаласи ham ichки-only (rasmiyда endpoint/maydon yo'q).

**✅ FAZA 1 (N5/N6/N1) TASDIQLANDI** — bu detail API kerak emas edi (FAZA 1 LIST `/v2/fbs/orders` ma'lumotidan o'qiydi), lekin schema maydonlarni tasdiqladi: `acceptUntil`/`deliverUntil` (rasmiy detail+list, ISO) ✅; `productImage` (rasmiy item kaliti — aynan N1 qo'shgani) ✅. **Tuzatish shart emas.**

**Rasmiy detail = LIST shakli − `penaltyParameters`/`cancelPrice`.** Ya'ni A.3 dagi «detail ≈ list» taxmini RASMIYда ham to'g'ri (ichки 2 qo'shimcha maydon rasmiyда yo'q). Item finance maydonlari (`sellerPrice`/`commission`/`sellerProfit`/`purchasePrice`/...) rasmiy itemда bor — kelajakда per-item foyda ko'rsatish manbai (items_json'да saqlanadi).

---

### ⭐⭐ A.22 — 6 ta rasmiy action/endpoint schema + N4b kod ko'rigi (swagger, 2026-06-01)

> Abdulaziz 6 ta rasmiy endpoint schema'sini ko'rsatdi (xavfsiz — Execute YO'Q). Bular N4b ni hal qiladi va payloadlarni tasdiqlaydi.

**✅ Bizning payloadlar — HAMMASI TO'G'RI:**
| Endpoint | Request body | Bizда | Echo (Response) |
|---|---|---|---|
| `POST .../confirm` | YO'Q (path only) | ✅ body-less | **TO'LIQ buyurtma** |
| `POST .../cancel` | `{reason, comment}` | ✅ aynan | **bo'sh `{}`** |
| `POST .../identifier` | `{items:[{orderItemId, values:[str]}]}` | ✅ aynan | **LIST** `[{type,required,values}]` |
| `POST /dbs/.../refund` | YO'Q (path only) | ✅ body-less | **bo'sh `{}`** (faqat COMPLETED) |
| `GET /invoice/{id}/orders` | — | — | `[{orderId, fullPrice, items[]}]` (N9) |
| `GET /v2/fbs/orders` | — | `status` **single** enum (CSV emas) | to'liq buyurtmalar |

**✅ N4b — XATO EMAS (kod ko'rigi + schema bilan tasdiqlandi):**
- `_apply_action_response_to_db` ([fbs_data.py:688](../core/fbs_data.py#L688)): Strategy 1 = echo'да `id` bo'lsa upsert; Strategy 2 = id yo'q → `token+order_id` bilan full detail refetch.
- 4 handler (confirm/cancel/identifier/dbs_refund) HAMMASI `token+order_id` uzatadi.
- **Yagona id-li echo = confirm = TO'LIQ** → upsert bo'shatmaydi. cancel/refund (`{}`) + identifier (list) → id yo'q → refetch. → **«ingichka-lekin-id-bor» xavfli holat YUZ BERMAYDI.** Defensive merge hozir kerak emas. (Docstring tuzatildi — identifier aslida Strategy 2.)

**🔴 N7 DISMISSED — PROBE-C YOPILDI (UI-tasdiq, 2026-06-01):** «Try it out»da `status` = **single-select dropdown** (11 enum + `--`, bittasini tanlaysiz; CSV kiritadigan text-maydon YO'Q), `array` EMAS (`shopIds` esa `array<integer>` + «Add integer item» tugmasi). → CSV multi-status batch **rasmiyда bo'lmaydi**. Bizning per-status yondashuv to'g'ri.

**🟢 N9 tayyor:** `GET /invoice/{id}/orders` → invoice→`orderId` aniq join. «Поставки» HAR + invoice kod ko'rigi qoldi.

**📛 Error kod katalogi (UZ mapping uchun):**
- confirm: `seller-order-01` topilmadi · `-02` noto'g'ri status · `-03` muddat o'tgan
- cancel: `-01` · `-02` · `-12` noto'g'ri sabab · `-13` allaqachon bekor
- identifier: `-01/-02/-05/-06/-07/-08/-09/-10/-11/-36` (tip yo'q/ko'p/noto'g'ri qiymat/boshqa SKU/WMS/...)
- dbs refund: `-01` · `-02` faqat COMPLETED · `-13`
- invoice/orders: `403 fbs-2-seller-access-denied`

**Running tally:** N4b RESOLVED-SAFE · N7 DISMISSED · payloadlar tasdiq · N9 tayyor.

---

### 🆕 A.23 — «Поставки» HAR → N9 ildizi topildi + fix specked (2026-06-01)

> 2 HAR (`seller.uzum,postafki*.uz.har`, ichki cabinet API). Rasmiy kodimiz bilan solishtirildi.

**🔑 Asosiy kashfiyot:** `number = 120000000000 + id` (4 invoice tasdiq: 327497→120000327497, 333569→120000333569, 310886→120000310886, 308591→120000308591). **LEKIN bu KERAK EMAS** — rasmiy list/detail `id` ni to'g'ridan-to'g'ri qaytaradi.

**Invoice→orders join (ichki, A.22 rasmiy schema bilan AYNAN bir xil):**
- `GET .../invoice/{id}/orders` → `[{orderId, fullPrice, items:[{orderId, barcode, skuTitle, title, amount, price, skuId, photo, status, deliverUntil}]}]`
- Path `{id}` = **qisqa id (327497)**, `number` EMAS.

**🐛 N9 = bizning kodda 2 ta workaround (ikkalasi DB-bog'liq + mo'rt):**
1. `fbs_invoice_detail_api` ([routes.py:2466](../fbs/routes.py#L2466)): orders'ni `raw_json->>'invoiceNumber' == number` bilan **DB-match** → order hali sync bo'lmasa invoice ichi BO'SH ko'rinadi.
2. `fbs_invoice_change_pickup_api` ([routes.py:2611](../fbs/routes.py#L2611)): `invoice_number.like('%suffix')` + `.endswith()` — **mo'rt suffix-match**, DB-bog'liq. **MUTATSIYA qiladi (`create_fbs_invoice`) → pytest-first.**

**❌ Bizda YO'Q:** `fetch_fbs_invoice_orders` funksiyasi → bu N9 gap.

**✅ N9 FIX (rasmiy, toza):**
1. `fetch_fbs_invoice_orders(token, invoice_id)` qo'shish → `GET /v1/fbs/invoice/{invoiceId}/orders` (avtoritar manba).
2. `detail_api`: a'zolik manbasi = rasmiy endpoint; har `order_id`'ni DBdan boyitish (mijoz/status/rasm). `invoiceNumber` DB-match olib tashlanadi.
3. `change_pickup`: `LIKE %suffix` → `fetch_fbs_invoice_orders` (aniq orderIds). **action → mocked pytest FIRST.**

**Bonus topilmalar:**
- ✅ **N2 TASDIQ:** `dropOffPoint.type ∈ {DROP_OFF_POINT, ISSUE_POINT}` (real data ikkalasi ham ko'rindi).
- ✅ `FBS_INVOICE_STATUSES` (CREATED/ACCEPTANCE_IN_PROGRESS/ACCEPTED/CANCELLED) = Uzum bilan **AYNAN mos** ([uzum_openapi.py:1293](../core/uzum_openapi.py#L1293)).
- ✅ Invoice payload to'liq shakl tasdiq (stock, timeSlot, dropOffPoint, ettn, acceptedPrice, numberOrders, acceptanceStartedDate).

**Running tally:** N9 fix SPECKED (2 sayt) · N2 tasdiq · invoice enum tasdiq.

**✅ N9 IMPLEMENT QILINDI (FAZA 2, 2026-06-01, pytest-first):**
- `fetch_fbs_invoice_orders(token, invoice_id)` → `GET /v1/fbs/invoice/{id}/orders` ([uzum_openapi.py](../core/uzum_openapi.py)). Avtoritar a'zolik.
- `change_invoice_pickup(...)` — testable mutatsiya core: orders olib → orderId ajratib → `create_fbs_invoice(update_only=True)`. Bo'sh/xato → `ValueError`, mutatsiya CHAQIRILMAYDI.
- `change_pickup` route: `LIKE %suffix` DB-hack OLIB TASHLANDI → `change_invoice_pickup`.
- `detail_api` route: `invoiceNumber` DB-match → avtoritar endpoint + `order_id` boyitish.
- **8 yangi mocked test** (`tests/test_fbs_invoice_orders.py`), jami **178 passed**. Mutatsiya faqat to'g'ri orderId bilan, bo'sh/403/xato'da skip.

---

## 9. ✅ QILINADIGAN ISHLAR (action plan) — 2026-06-01

> HAR auditдан yig'ilган barcha topilmalar → bajariladigan ishlar. Tartib: **foyda × tayyorlik**. Har biri: joy, mehnat, risk, bog'liqlik. `[ ]` = qilinmagan.

### 🥇 Tier 1 — tayyor, xavfsiz, probe kerak emas (BIRINCHI shular)
- [x] **N5 ⭐ — Muddat bo'yicha saralash.** ✅ BAJARILDI (2026-06-01, kod+5 pytest; brauzer-verify kutilmoqda). Deadline-bearing statuslar (PACKING, PENDING_DELIVERY, DELIVERING) uchun `order_by(deliver_until.asc().nulls_last(), date_created.desc())`. Joy: [fbs_data.py:163](../core/fbs_data.py#L163) (per-shop) + [:551](../core/fbs_data.py#L551) («Hammasi»). Mehnat: kichik. Risk: past. _Foyda: eng shoshilinchи tepada → muddat o'tmaydi._
- [x] **N6 — «Muddat» ustuni.** ✅ BAJARILDI (2026-06-01, overdue→qizil). List jadvalга `<th>Muddat</th>` + katak (CREATED→acceptUntil, aks holда deliverUntil). Joy: [fbs_orders.html:1094,1118](../templates/fbs_orders.html#L1094) + `translations.py` (uz/ru/en kalit). Mehnat: kichik. Risk: past (frontend). _N5 bilan juftlik._
- [x] **N1 — List thumbnail.** ✅ BAJARILDI (2026-06-01). `const pimg = firstItem.productImage || firstItem.photo; const photoSet = pimg && pimg.photo;`. Joy: [fbs_orders.html:1068](../templates/fbs_orders.html#L1068). Mehnat: 1 qator. Risk: past. _Rasmiy item `productImage` (internal `photo` emas)._

### 🥈 Tier 2 — eng katta biznes qiymati (avval 1 ta probe)
- [ ] **PROBE-A** — swagger «Try it out» `GET /v1/fbs/order/{id}` → javobда `penaltyParameters` + `cancelPrice` bormi? (rasmiy detail ichки bilan bir xilmi)
- [ ] **O2 — Bekor jarimasi ogohlantirishi** (PROBE-A ga bog'liq). «Bekor qilish» modalида narvonдан hozirgi pog'onani ko'rsatish: «⚠️ Jarima ~5% (min 10 000 so'm)». `minutesFromCreation` + `ladderParameters` dan. Joy: cancel modal [fbs_order_detail.html](../templates/fbs_order_detail.html) + detail fetch. _Foyda: pul saqlaydi._
- [ ] **N9 — Invoice-join'ni rasmiy yo'l bilan.** LIKE-suffix hack o'rniga `GET /v1/fbs/invoice/{invoiceId}/orders`. Joy: [fbs/routes.py:2575-2585](../fbs/routes.py#L2575). _«Поставки» HAR + invoice kod bilan tasdiqlanadi. #10 ni yopadi._

### 🥉 Tier 3 — ma'lumot/probe kutilmoqda yoki past shoshilinch
- [ ] **N4b — Action-echo merge** (action HAR kerak). `_apply_action_response_to_db` ингичка echo'ни verbatim upsert qiladi → full detail refetch YOKI COALESCE-merge. Joy: [fbs_data.py:690-716](../core/fbs_data.py#L690). _pytest-first (action endpoint)._
- [x] **PROBE-C** ✅ YOPILDI → **N7 — Multi-status CSV batch.** swagger `status=DELIVERED,DELIVERING` qabul qiladimi? Ha bo'lса worker terminal statuslarни 1 call'да (3× kam). Joy: [uzum_openapi.py:626](../core/uzum_openapi.py#L626).
- [ ] **N3 — Rate-limit headerlar.** `x-ratelimit-remaining`/`-burst-capacity`dан haqiqiy burst-limit → pacing'ни 1s taxminдан aniqга. Joy: [uzum_openapi.py](../core/uzum_openapi.py), [fbs_locks.py](../core/fbs_locks.py).
- [ ] **N2 — `drop_off_point_type` ustun** (`ISSUE_POINT`). models.py + fbs_sync.py + migration. Mehnat: o'rta (migration).
- [ ] **N4/N4a — Defensive merge** (past shoshilinch — v2 doim to'liq). Regressiya himoyasi: upsert COALESCE + `dict_from_order` `scheme`→None projeksiyа. Joy: [fbs_sync.py:328,493-501](../core/fbs_sync.py#L493). _pytest-first._
- [ ] **N8 — RETURNED to'liqligi (183 vs 192)** — rasmiy `/returned` YO'Q (A.20), shuning uchun **sync-correctness** sifatida: MAX_PAGES/stop_on_known/timing tekshirish. Joy: [fbs_sync.py:231-290](../core/fbs_sync.py#L231). _Endpoint emas, sync masalasi._
- [ ] **O6 — FBS SKU ombor boshqaruvi** (yangi feature). `GET/POST /v2/fbs/sku/stocks` → SellerHub'дан FBS qoldiqlарни o'qish/yangilash. _Foyda: Uzum cabinet'siz stock boshqarish._
- [ ] **Поставки flow** (katta) — to'liq invoice lifecycle: `POST /v1/fbs/invoice` (create) + `dop/drop-off-points` + `dop/time-slot` + `closing-documents`/`print` (aktlar). _Foyda: поставкани SellerHub'да yaratish + aktlar chop etish._

### 📥 Hali kerak bo'lган ma'lumot (HAR/probe)
- [ ] **Action HAR** (confirm/cancel **bosib**) → N4b ni yopadi
- [ ] **«Поставки» HAR** → invoice-join bug (#10, [fbs/routes.py:2575-2585](../fbs/routes.py#L2575))
- [x] **«Возвраты» HAR** (RETURNED) → 8/8 tab TUGADI ✅ (A.19)
- [x] **PROBE-B** (rating) → rasmiy OpenAPI'да YO'Q (A.20) → O1 dismissed
- [x] **PROBE-D** (returns endpoint) → rasmiy OpenAPI'да YO'Q (A.20) → N8 sync-masala, O5 dismissed
- [ ] **PROBE-A** — `GET /v1/fbs/order/{id}` swagger Response schema'sини OCHIB `penaltyParameters`/`cancelPrice` bor-yo'qлигини ko'rsatish → O2 ni hal qiladi
- [x] **PROBE-C** ✅ YOPILDI (UI: `status` single-select dropdown) — `GET /v2/fbs/orders` `status` CSV qabul qiladimi («Try it out») → N7

### ❌ Bekor qilingan (qilinmaydi)
- F1 (`places`) — rasmiy endpointда yo'q · O3 (termal printer) — local presetlar yetarli
- **O1** (reyting badge), **O5** (boy refund ko'rinishi) — rasmiy OpenAPI'да endpoint YO'Q (A.20). **O2** (bekor jarima ogohlantirishi) — rasmiy detailда `penaltyParameters` YO'Q (A.21). N8 — endpoint emas, sync-correctness masalasi. **N7** (CSV batch) — rasmiy `status` single-enum (A.22). **N4b** — xato emas, kod allaqachon to'g'ri ishlaydi (A.22).

---

## 10. 🗺️ ISH REJASI (execution plan) — 2026-06-01

> **Tamoyil:** probe/HAR (bloklarni ochadi) → tayyor xavfsiz yutuqlar → qiymatli feature → action-correctness (pytest-first) → yangi feature → sayqal.
> **Qoidalar:** faqat FBS/DBS fayllar · action endpoint = **mocked pytest AVVAL** (Uzum ban xavfi) · static/template o'zgarса `docker compose up -d --build` · frontend = brauzer-verify · `User.uzum_openapi_token`.

### 🔹 FAZA 0 — Bloklarni ochish (probe + HAR · ~30 daq · **Abdulaziz** · hammasi read-only)
Parallel bajariladi, keyingi fazalarни ochadi:
- [ ] **PROBE-A** — swagger `GET /v1/fbs/order/{id}` → Response schema → `penaltyParameters`/`cancelPrice` bormi? → **O2 ni ochadi**
- [x] **PROBE-C** ✅ — swagger `GET /v2/fbs/orders` «Try it out» `status=DELIVERED,DELIVERING` → CSV ishlaydimi? → **N7 ni ochadi**
- [ ] **Action HAR** — bitta test buyurtmada «confirm»/«cancel» bosib HAR → echo shakli → **N4b ni ochadi**
- [ ] **«Поставки» HAR** — invoice list/detail/yaratish → **N9 + invoice flow ni ochadi**

### 🔹 FAZA 1 — Tezkor xavfsiz yutuqlar (~yarim kun · probe kerak emas · bitta commit)
- [x] **N5** — deadline sort ([fbs_data.py](../core/fbs_data.py) `_fbs_list_order_by`) ✅
- [x] **N6** — «Muddat» ustuni ([fbs_orders.html](../templates/fbs_orders.html)) ✅
- [x] **N1** — thumbnail `productImage` ([fbs_orders.html](../templates/fbs_orders.html)) ✅
- [ ] _Verify:_ docker rebuild + brauzer (FAZA 1 — kod+170 pytest green, brauzer kutilmoqda)
- _Verify:_ docker rebuild + brauzer (ro'yxat muddat bo'yicha tartiblanadi, rasm chiqadi)

### 🔹 FAZA 2 — Invoice-join + Поставки (N9) [FAZA 0 «Поставки» HAR ga bog'liq]
- [ ] **N9** — `GET /v1/fbs/invoice/{id}/orders` bilan order↔invoice bog'lash (LIKE-hack o'rniga, [fbs/routes.py:2575](../fbs/routes.py#L2575))
- [ ] _(ixtiyoriy)_ `closing-documents` (акт приёмки) + `cancel` + `update-content` qo'shish
- _Verify:_ invoice detalida buyurtmalar to'g'ri ko'rinadi

### 🔹 FAZA 3 — Bekor jarimasi ogohlantirishi (O2) [PROBE-A = ha bo'lsa]
- [ ] **O2** — cancel modalда `penaltyParameters` + `minutesFromCreation` dan hozirgi pog'ona: «⚠️ Jarima ~5% (min 10 000 so'm)»
- _Verify:_ brauzer, turli yoshdagi buyurtmalar (12h ichi = bepul, 24-48h = 5%)

### 🔹 FAZA 4 — Action-echo merge (N4b) [Action HAR · **PYTEST-FIRST** · ban-sensitive]
- [x] **N4b** ✅ XATO EMAS (A.22) — kod allaqachon to'g'ri; eski tavsif: ingичка echo'ни full-refetch YOKI COALESCE-merge qiladi
- _Tartib:_ mocked pytest → kod → bitta test buyurtmада jonli → to'liq

### 🔹 FAZA 5 — FBS SKU ombor boshqaruvi (O6 · yangi feature) [POST uchun **PYTEST-FIRST**]
- [ ] **O6 GET** — `GET /v2/fbs/sku/stocks` → ombor ko'rinishi (read, xavfsiz)
- [ ] **O6 POST** — `POST /v2/fbs/sku/stocks` → stock yangilash (pytest-first, ehtiyot)

### 🔹 FAZA 6 — Sayqal (past shoshilinch · mustaqil)
- [x] **N7** — ❌ DISMISSED (A.22): rasmiy `status` single-enum (CSV emas). Per-status yondashuv to'g'ri.
- [ ] **N3** — `x-ratelimit-*` headerlardan haqiqiy burst-limit → pacing aniqlashtirish
- [ ] **N2** — `drop_off_point_type` ustun (models.py + fbs_sync.py + migration)
- [ ] **N4/N4a** — defensive merge (regressiya himoyasi, pytest-first)
- [ ] **N8** — RETURNED 183 vs 192 sync-completeness tekshiruvi (MAX_PAGES/timing)

### 📊 Bog'liqlik xulosasi
```
FAZA 0 (probe/HAR) ──┬─→ FAZA 1 (N5,N6,N1)        [mustaqil, hoziroq boshlash mumkin]
                     ├─→ FAZA 2 (N9)              [Поставки HAR]
                     ├─→ FAZA 3 (O2)              [PROBE-A]
                     ├─→ FAZA 4 (N4b)             [Action HAR]
                     └─→ FAZA 6 N7                [PROBE-C]
FAZA 5 (O6), FAZA 6 (N3,N2,N4,N8) — mustaqil, istalgan vaqtda
```
**Tavsiya boshlash:** FAZA 1 ni hoziroq boshlash mumkin (probe kerak emas). Parallel — Abdulaziz FAZA 0 probe/HAR'larini yig'adi.
