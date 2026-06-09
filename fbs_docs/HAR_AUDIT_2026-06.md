# HAR audit — har bir FBS/DBS funksiya bo'yicha (2026-06)

Jarayon: Abdulaziz har funksiyadan HAR (Uzum rasmiy portali `seller.uzum`)
tashlaydi → topilgan **xato** va **yangilik**lar shu yerga yig'iladi →
oxirida guruhlab birga tuzatiladi.

> ⚠️ MUHIM: bu HAR'lar Uzum **ichki portal API** (`/api/seller/fbs/...`)
> chaqiruvlari. Bizning ilova **OpenAPI** (`/api/seller-openapi/...`)
> ishlatadi — maydon nomlari/qobiqlari farq qilishi mumkin. Shuning uchun
> har bir "yangilik" uchun: AVVAL bizning OpenAPI o'sha maydonni
> qaytaradimi — live tekshirilsin, keyin UI'ga chiqarilsin.

---

## 1) DBS «Новый» (CREATED) — ro'yxat + zakaz detali
HAR: `seller.uzum,DBS,NOVIY.uz.har`, `seller.uzum,DBS,NOVIY1.uz.har` (2026-06-02 16:07)

**Status:** hamma so'rov HTTP 200 — **xato yo'q**. Quyidagilar — yangiliklar.

Bosilgan zakaz: `GET /api/seller/fbs/order/109608872` → DBS, shop 10945 (BLUMMY), CREATED, price 7950.

### Portal list chaqiruvi (taqqoslash uchun)
```
GET /api/seller/fbs/v2/orders?page=0&size=20&scheme=DBS&statuses=CREATED
    &sortBy=CREATED_DATE&sortOrder=DESC&shopId...
GET /api/seller/fbs/orders/count?shopIds=<10 ta>&scheme=DBS
```
- Portal `statuses` (ko'plik) + `sortBy=CREATED_DATE&sortOrder=DESC` ishlatadi;
  bizning OpenAPI `status` (birlik). Bu ataylab — boshqa namespace, muammo emas.
- 🔎 **Yangilik:** portal `orders/count`'ga `scheme=DBS` BERADI. Bizning memory'da
  OpenAPI `/v2/fbs/orders/count` scheme'ni qo'llamaydi (faqat FBS) deb yozilgan.
  DBS count'ni biz `size=1 + scheme=DBS` orqali olamiz — ishlaydi, lekin
  OpenAPI count ham scheme'ni qabul qiladimi — bir marta live sinab ko'rish arzon.

### Detal javobida BIZDA YO'Q maydonlar (yangilik nomzodlari)
| Maydon (portal) | Qiymat namunasi | Bizda holat |
|---|---|---|
| `publicId` | `"856114-0035"` | ❌ saqlanmaydi/ko'rsatilmaydi — sotuvchi taniydigan inson-o'qiy raqam |
| `deliveryInfo.issueCodeRetriesLeft` | `3` | ❌ — DBS topshirish kodida necha urinish qolgani (seller-order-39 blokdan oldin ogohlantirish uchun zo'r) |
| `identifierInfo.type` | `"ASL_BELGISI"` | ⚠️ identifikator turi enumi (Asl belgisi/Markirovka). Bizda identifier oqimi bor, lekin bu enum hujjatlanmagan |
| `carrierCode` | `"DEFAULT"` | ❌ — tashuvchi kodi |
| `cancelPrice`, `penaltyParameters` | `null` | ❌ — bekor qilish narxi / **jarima** parametrlari (muhim bo'lishi mumkin) |
| `skuDimension` {length,width,height}, `weight` | 69×50×5, 5 | ❌ — gabarit/og'irlik (postavka punkt mosligi uchun foydali) |
| `acceptanceProlongationsCount`, `sellerAcceptanceProlongationsCount` | 0, 0 | ❌ — muddat uzaytirishlar soni |
| `barcode`, `productId` | 1000103129447, 2798761 | ⚠️ qisman |

### Bizda allaqachon BOR (tasdiq)
- `issueCode` DBS completed oqimi — ✅ (`mark_dbs_completed`, fbs_order_detail.html).
- `deliveryInfo` (manzil/ism/telefon/komment) — ✅.
- 5 do'kon ro'yxatdagi shopId'lar portal 10 do'koni ichida bor — ✅.

**Bu funksiya bo'yicha xulosa:** sof xato yo'q. Eng qimmatli 2 yangilik:
**(a) `publicId`** ko'rsatish, **(b) `issueCodeRetriesLeft`** DBS topshirishda
ko'rsatish. Qolganlar — ixtiyoriy. HAMMASI avval OpenAPI javobida bor-yo'qligini
live tekshirgandan keyin (tuzatish bosqichida).

---
## 2) DBS «В сборке» (PACKING) — ro'yxat + zakaz detali
HAR: `seller.uzum,DBS,Vsborke.uz.har`, `Vsborke1.uz.har` (2026-06-02 16:16–16:17)

**Status:** hamma so'rov HTTP 200 — **xato yo'q**. Action POST yo'q (CREATED→PACKING
oldin tasdiqlangan; bu HAR'lar faqat ko'rish).

O'sha zakaz `109608872` endi `status=PACKING`. `acceptedDate` hali `null`,
`place/timeSlot/dropOffPoint/invoiceNumber` ham `null` (DBS — sotuvchi o'zi
yetkazadi, postavka punkti yo'q). `publicId`, `issueCodeRetriesLeft=3`,
`carrierCode=DEFAULT` — o'zgarmadi.

### Yangilik
- 🔎 **Saralash holatga qarab o'zgaradi:** PACKING ro'yxati
  `sortBy=DELIVER_UNTIL&sortOrder=ASC` (eng shoshilinchi tepada).
  CREATED esa `sortBy=CREATED_DATE&sortOrder=DESC` (eng yangi tepada) edi.
  → Bizning ro'yxatda ham PACKING'ni deadline bo'yicha o'sish tartibida
  ko'rsatish mantiqan to'g'ri (UX yaxshilanishi).

**Xulosa:** xato yo'q. Bitta UX yangiligi (holatga moslangan saralash).
#1'dagi yangiliklar (`publicId`, `issueCodeRetriesLeft`) shu yerda ham tasdiqlandi.

---
## 3) DBS «В доставке» (DELIVERING) — ro'yxat + zakaz detali
HAR: `seller.uzum,DBS,Vdastafke.uz.har`, `Vdastafke1.uz.har` (2026-06-02 16:24)

**Status:** hamma so'rov HTTP 200 — **xato yo'q**. Action POST yo'q (ko'rish).

O'sha zakaz `109608872` endi `status=DELIVERING`. `deliveringDate` to'ldi
(1780399356684). Qolgan sanalar (`deliveryDate/acceptedDate/completedDate`) hali
`null` — bosqichma-bosqich to'ladi.

### Yangilik
- 🔎 **Saralash yana o'zgardi:** DELIVERING ro'yxati `sortBy=CREATED_DATE&sortOrder=DESC`
  (yana eng yangi tepada). Ya'ni saralash mantiqi: **CREATED → DESC by created,
  PACKING → ASC by deliver_until, DELIVERING → DESC by created**. #2 dagi
  "holatga-mos saralash" yangiligini mustahkamlaydi.

**Xulosa:** xato yo'q. Saralash xaritasi to'lдi (yuqorida).

---
## 4) SellerHub renderini portal bilan solishtirish (DBS detal, o'sha zakaz)
Manba: SellerHub `/fbs` order detail skrini (DELIVERING) vs portal HAR (#1–#3).

Bizning sahifa toza va to'liq: mijoz (ism/tel/manzil) ✅, sanalar (Создан/Срок
подтверждения/Срок доставки/В пути) ✅, tovarlar (rasm+nom+SKU+narx) ✅,
DBS "Доставлен клиенту" + kod inputi ✅, `Сумма заказа (price)` ✅.

### Aniqlangan farqlar (kodga tekshirib tasdiqlangan)
- 🟡 **Zakaz raqami:** biz "Заказ №109608872" (ichki `id`) ko'rsatamiz; portal
  `publicId="856114-0035"` (sotuvchi taniydigan qisqa raqam) ishlatadi.
  → `core/fbs_sync.py` `dict_from_order` (l.324) `publicId`ни **olmaydi**;
  `FbsOrder` modelida ham yo'q. UI'da uzun ichki id chiqyapti.
- 🟡 **issueCodeRetriesLeft:** "Код подтверждения" inputi bor, lekin necha urinish
  qolgani (portal: `deliveryInfo.issueCodeRetriesLeft=3`) ko'rsatilmaydi. Sync
  `deliveryInfo`dan faqat ism/tel/manzil/komment oladi, retriesLeft'ni emas.
- 🟢 **Gabarit/og'irlik/rasm/identifierInfo:** bular `items_json` ichida ALLAQACHON
  SAQLANADI (sync `orderItems`ни verbatim yozadi) — faqat UI'da ko'rsatilmaydi.
  Ya'ni yangi sync/migration kerak emas, faqat template'da chiqarish kifoya.

### ⚠️ Tuzatish OLDIDAN majburiy tekshiruv
`publicId` va `issueCodeRetriesLeft` — bular portal **ichki API**'da bor. Bizning
**OpenAPI** (`/v1/fbs/order/{id}`, `/v2/fbs/orders`) bu maydonlarni qaytaradimi —
HALI noma'lum. Yangi DB ustun qo'shishdan oldin bitta live OpenAPI javobini
`grep publicId` qilib tasdiqlash shart. Agar OpenAPI bermasa — bu maydonlarni
ko'rsatib bo'lmaydi (ichki API'ni ishlatmaymiz).

---
## 5) DBS COMPLETED — kod kiritib tasdiqlash (uchidan-uchiga test)
Manba: SellerHub skrinlar — kod kiritildi + "Доставлен клиенту" bosildi.

**✅ MUVAFFAQIYAT:** o'sha zakaz `109608872` to'liq sikldan o'tdi:
CREATED → PACKING → DELIVERING → **COMPLETED («Завершён»)**, xatosiz.
Bu — `mark_dbs_completed` (issue-code bilan) **prodda haqiqatan ishlayotganini**
tasdiqlaydi (seller-order-37/38/39 oqimi).

- COMPLETED zakazda **"Создать возврат"** tugmasi chiqdi → DBS qaytarish (refund)
  seam ulanган. ✅
- Ro'yxatda zakaz «Завершён 235» tab ostida ko'rindi. ✅
- Sanalar to'liq: Создан / Срок подтверждения / Срок доставки / В пути /
  **Завершён 16:34** — bosqichma-bosqich to'g'ri to'ldi. ✅

### Kichik UX kuzatuv (xato emas)
- Ro'yxat tablari: «В ПВЗ» va «В пункте выдачи» ikkalasi ham bor (0/0). Bular ikki
  alohida Uzum statusi (ACCEPTED_AT_DP va DELIVERED_TO_CUSTOMER_DELIVERY_POINT),
  lekin ruscha nomlari deyarli bir xil — sotuvchini chalg'itishi mumkin. Nomlarni
  aniqlashtirish (yoki UZ'da farqli atash) arzimas yaxshilanish.

**Xulosa:** xato yo'q — aksincha, DBS action oqimi prodda tasdiqlandi. Bitta
arzimas UX kuzatuv (tab nomlari).

---
## 6) DBS «Выданный» (COMPLETED) ro'yxat + detal
HAR: `seller.uzum,DBS,Vidaniy1.uz.har`, `Vidaniy2.uz.har` (2026-06-02 16:43–16:44)

**Status:** HTTP 200 — **xato yo'q**. Yangi maydon **yo'q**.

- 🔎 **Terminologiya:** portal «Выданный» tabi `statuses=COMPLETED` so'raydi (sort
  CREATED_DATE DESC). Ya'ni portal "Выданный" = bizning "Завершён" (COMPLETED).
  Bizda bu zakaz to'g'ri «Завершён» da turibdi — moslik bor, faqat nom farqi.
- Detal: `completedDate` to'lgan; `returnDate/cancelPrice/penaltyParameters` = null
  (qaytarish hali yo'q). Yangi top-level kalit topilmadi — detal shakli barqaror.

**Xulosa:** xato yo'q, yangilik yo'q (#5 ni tasdiqlaydi).

---
## 7) DBS «Возврат» (RETURNED) + «Отмена» (CANCELED) ro'yxatlari
HAR: `seller.uzum,DBS,VAZVRAT.uz.har`, `OTMENA.uz.har` (2026-06-02 16:55)

**Status:** ikkalasi ham HTTP 200 — **xato yo'q** (action POST yo'q, faqat ro'yxat).

Portal so'rovlari:
```
Возврат: statuses=RETURNED
Отмена:  statuses=CANCELED,PENDING_CANCELLATION   ← IKKI status CSV bilan
```

### 🟡 YANGILIK / DIVERGENSIYA — PENDING_CANCELLATION
Portal «Отмена» tabi **CANCELED + PENDING_CANCELLATION** ikkalasini bitta tabga
qo'shadi. Bizda esa (Bosqich A.11, `core/fbs_sync.py:81`) PENDING_CANCELLATION
**ataylab tashlangan**: sync qilinmaydi, chip yashirilgan (`FBS_HIDDEN_CHIP_STATUSES`).
→ Oqibat: PENDING_CANCELLATION («Bekor qilinmoqda») holatidagi zakaz bizning
ilovada **vaqtincha hech qaysi tabda ko'rinmaydi** (CANCELED bo'lguncha "yo'qoladi").
Bu — ataylab qilingan quota-tejash qarori, lekin portal boshqacha qiladi.
**Muhokama kerak:** PENDING_CANCELLATION'ni «Отменён» tabига qo'shamizmi (portaldek),
yo'qmi? Qo'shsak — bitta qo'shimcha quota uniti ketadi (yoki OpenAPI CSV
`status`'ni qabul qilsa, bepul — buni live tekshirish kerak).

### 🟢 RETURNED — qamralgan
«Возврат» = `statuses=RETURNED`. Bizning sync `FBS_ALL_SYNC_STATUSES` da RETURNED
**bor** (l.99) → biz qaytarishlarni olamiz. Memory'dagi "RETURNED to'liqligi"
xavotiri shu yerda yopiladi — status sync qilinadi.

### 🔎 Texnik kuzatuv — CSV ko'p-status
Portal ichki API `statuses=A,B` (CSV) qabul qiladi. Bizning OpenAPI swaggerда
`status` BIRLIK deyilган. Agar PENDING_CANCELLATION'ni Отменён ga qo'shmoqchi
bo'lsak — OpenAPI CSV'ni qabul qiladimi, live sinash arzon (qabul qilsa bitta
chaqiruvda 2 status; qilmasa 2-chaqiruv quota'ga tushadi).

**Xulosa:** xato yo'q. 1 ta muhokama-talab divergensiya (PENDING_CANCELLATION
ko'rinmasligi), RETURNED esa qamralgan.

---
<!-- Keyingi funksiyalar shu yerga qo'shiladi -->

---

# ✅✅ BAJARILDI (2026-06-03) — A + B guruh PRODDA

- **A1** gabarit/og'irlik → detal tovar tagida «Qadoq o'lchami: 10×10×100 mm · Og'irlik: 10 g».
  Data `items_json`'da bor edi (migration yo'q). Yorliq «Qadoq o'lchami» (tovar nomidagi
  «O'lcham» bilan chalkashmasin).
- **A2** tab nomlari → «В пункте приёма» (ACCEPTED_AT_DP) vs «В пункте выдачи»
  (DELIVERED_TO_CUSTOMER_DELIVERY_POINT); uz «Qabul punktida»/«Olish punktida».
- **B1** publicId → detal sarlavhasi `№856114-0036`. `raw_json`'dan keladi (migration/model/sync YO'Q).
- **B2** issueCodeRetriesLeft → DBS DELIVERING'da kod inputi tagida «N urinish qoldi» (1 da qizil).
  `row_to_dict` include_raw ichida `deliveryInfo`ga qo'shiladi (list defer'iga tegmaydi).
- **C1** PENDING_CANCELLATION → **QILINMADI** (user so'radi; 429 quota xavfi).
- Probe (`UZUM_FBS_DEBUG_KEYS`/PROBEKEYS) butunlay o'chirildi.
- Test: **248 passed** (+5 yangi row_to_dict B1/B2). Action endpoint tegilmadi, migration yo'q.
- Tegilgan fayllar: `core/fbs_sync.py` (row_to_dict), `fbs/routes.py` (labels),
  `templates/fbs_order_detail.html` (gabarit + header + retries), `tests/test_fbs_sync.py`.

> Ochiq: FBS-maxsus oqim (накладная/postavka/drop-off/time-slot) HAR bilan AUDIT
> qilinmagan — kelajakda alohida.

---

# ✅ YAKUNIY XULOSA — tuzatish rejasi (2026-06-02)

**Audit natijasi: HECH QANDAY XATO TOPILMADI.** Butun DBS sikli (CREATED →
PACKING → DELIVERING → COMPLETED) prodda uchidan-uchiga ishladi. Quyidagilar —
faqat yaxshilanish ("yangilik") nomzodlari.

> ⚠️ Saralash (holatga-mos) — bu YANGILIK EMAS: biz allaqachon qilamiz va
> portaldan ham yaxshiroq (N5/A.13, `core/fbs_data.py:118`). Ro'yxatdan chiqarildi.

### A guruh — OSON & XAVFSIZ (migration/probe kerak emas)
- **A1. Gabarit/og'irlik ko'rsatish.** `skuDimension`+`weight` `items_json` ichida
  ALLAQACHON bor — faqat `templates/fbs_order_detail.html` da chiqarish.
- **A2. Tab nomlarini aniqlashtirish.** «В ПВЗ» vs «В пункте выдачи» chalg'itadi —
  UZ'da farqli/aniqroq atash (`translations.py` yoki template).

### B guruh — AVVAL OPENAPI PROBE KERAK (keyin DB+sync+UI)
- **B1. publicId** (sotuvchi taniydigan qisqa raqam, masalan `856114-0035`).
- **B2. issueCodeRetriesLeft** (DBS kod uchun necha urinish qoldi).
  ⚠️ Bular portal ICHKI API'da bor. Bizning OpenAPI beradimi — NOMA'LUM. Avval
  bitta live OpenAPI order-detail javobini probe qilib `grep publicId`/
  `issueCodeRetriesLeft` — bo'lsa → DB ustun + sync + UI; bo'lmasa → imkonsiz.

### C guruh — QAROR TALAB
- **C1. PENDING_CANCELLATION.** Portal «Отменён» tabга qo'shadi; biz ataylab
  yashiramiz (A.11, quota). Qo'shamizmi? (probe: OpenAPI `status` CSV qabul
  qiladimi — qilsa bepul.)

### ✅ PROBE NATIJASI (2026-06-03, env-gated PROBEKEYS, bizning OpenAPI list)
- **B1 publicId: BOR** — har status uchun qaytadi (`856114-0035`, ...). → qo'shish mumkin.
- **B2 issueCodeRetriesLeft: BOR (faqat DBS)** — COMPLETED DBS zakazда
  `deliveryInfo.issueCodeRetriesLeft=3`; FBS zakazlarda `deliveryInfo` bo'sh.
  → DBS uchun qo'shish mumkin.
- Probe `UZUM_FBS_DEBUG_KEYS` env bilan; tugagach o'chiriladi.

### Ixtiyoriy / kichik
- cancelPrice/penaltyParameters (jarima), carrierCode, acceptanceProlongationsCount,
  identifierInfo.type enumi — hozircha kerak emas, kelajakda.

