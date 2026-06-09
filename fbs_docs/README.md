# FBS / DBS tizimi — hujjat va kod nusxalari

Bu papka SellerHub'ga qo'shilayotgan **FBS** (Fulfillment by Seller) va **DBS** (Delivery by Seller) tizimining barcha kod nusxalari, planlari va promtlarini bir joyga yig'adi. Loyihada bardak bo'lib ketmasin uchun.

> ⚠️ **Bu kod nusxalari** — haqiqiy ishlaydigan kod loyihaning asosiy katalogida (`fbs/`, `core/uzum_openapi.py`, `templates/fbs_orders.html`). Bu papka faqat **o'qish va eslab qolish uchun**. Bu yerdagi fayllarni o'zgartirish Flask app'iga ta'sir qilmaydi.

---

## Papka tarkibi

```
fbs_docs/
├── README.md          ← shu fayl
├── PLAN.md            ← 5 bosqichli yo'l xaritasi (nima qilamiz, nega, qaysi tartibda)
├── PROMPTS.md         ← har bosqich uchun copy-paste promtlar (yangi chatga paste qilasiz)
├── ENDPOINTS.md       ← Uzum Seller OpenAPI'dan ko'rilgan endpoint ro'yxati + payload
└── snapshots/         ← hozir yozilgan kod nusxalari (2026-05-21 holati)
    ├── core_uzum_openapi_fbs.py   ← core/uzum_openapi.py'ga qo'shilgan funksiyalar
    ├── fbs_routes.py              ← fbs/routes.py to'liq nusxasi
    ├── fbs_orders.html            ← templates/fbs_orders.html nusxasi
    └── changes.md                 ← app.py, uzum_ui.js, dark.md o'zgarishlari
```

---

## Hozirgi holat (2026-05-21)

- ✅ **Bosqich 1 (qisman)**: ro'yxat sahifasi (`/fbs`) yaratildi, lekin endpoint URL **probe** asosida ishlaydi (5 ta noto'g'ri taxmin)
- 🔄 **Keyingi qadam**: swagger'dan `GET /v2/fbs/orders` parametr va response namunasini olib, probe'ni aniq URL'ga almashtirish

To'liq tartib uchun → [PLAN.md](PLAN.md)

---

## Qanday foydalanish

### Yangi chatda davom etish uchun:
1. [PROMPTS.md](PROMPTS.md) ochi
2. Boshlamoqchi bo'lgan bosqichning promtini topib copy qili
3. Yangi Claude Code chat'iga paste qili
4. Agar yangi chat'da kontekst yo'q bo'lsa, oldindan **boshlang'ich kontekst bloki** (PROMPTS.md tepasida)'ni ham paste qili

### Eski kodga qarash uchun:
1. [snapshots/](snapshots/) papkasidagi fayllarga qarang
2. Real fayllar bilan solishtirish kerak bo'lsa: `core/uzum_openapi.py`, `fbs/routes.py`, `templates/fbs_orders.html`
3. [snapshots/changes.md](snapshots/changes.md) — `app.py`, `static/uzum_ui.js`, `dark.md`'ga qo'shilgan kichik qismlar

---

## Eslatma: bu hujjatlar har bosqich tugaganda yangilanishi kerak

Yangi bosqich tugagandan keyin:
- `PLAN.md`'da o'sha bosqich oldida ✅ belgisi qo'yiladi
- `snapshots/`'ga yangi kod nusxalari qo'shiladi
- `ENDPOINTS.md`'ga yangi ishlatilgan endpoint qo'shiladi

Shunday qilib bu papka loyihaning FBS qismi haqida **eng yangi yagona manba** bo'lib qoladi.
