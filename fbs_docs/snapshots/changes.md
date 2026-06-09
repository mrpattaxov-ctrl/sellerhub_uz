# Mavjud fayllarga qo'shilgan o'zgarishlar (2026-05-21)

> ⚡ **Refresh 2026-05-21 (kechqurun):** Bosqich 1'ning katta refactor'i tugadi.
> /v2/fbs/orders endpoint to'liq aniqlangan swagger asosida qayta yozildi.
> Quyidagi diff'lar **dastlabki** holatni ko'rsatadi; hozirgi to'liq kodni
> snapshots/ ichidagi fayllardan ko'ring.

FBS qo'shganda yangi fayllar yaratish bilan birga, **3 ta mavjud fayl**ga kichik qo'shimchalar kiritildi. Bu yerda ularning to'liq diff'i (kelajakda rollback yoki refactor uchun).

---

## 1. `app.py` — Blueprint register

**Joy:** `app.py:969-973` (telegram blueprint registratsiyasidan keyin)

**Qo'shildi:**
```python
# ---------------------------------------------------------------------------
# Register fbs-routes Blueprint (FBS orders viewer via Seller OpenAPI)
# ---------------------------------------------------------------------------
import fbs.routes as _fbs_mod
app.register_blueprint(_fbs_mod.fbs_bp)
```

**Eslatma:** Boshqa blueprint'lar `init_*_routes(__import__("sys").modules[__name__])` chaqiruvini ham qiladi (app.py'dagi global state'ga ko'rsatish uchun). FBS'da bu kerak emas — `fbs/routes.py` toza, app.py'dan hech narsa import qilmaydi.

---

## 2. `static/uzum_ui.js` — Sidebar link

**Joy 1:** `_SIDEBAR_LABELS` (170-200 oraliq)

**Qo'shildi (RU bloki):**
```javascript
fbs: "FBS заказы",
```

**Qo'shildi (UZ bloki):**
```javascript
fbs: "FBS buyurtmalar",
```

**Joy 2:** `links` massivi (~383-393)

**Qo'shildi (POS link'idan keyin, invoice link'idan oldin):**
```javascript
{ label: SL.fbs, href: "/fbs", icon: svgIcon('<path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="7.5 4.21 12 6.81 16.5 4.21"/><polyline points="7.5 19.79 7.5 14.6 3 12"/><polyline points="21 12 16.5 14.6 16.5 19.79"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/>') },
```

**Sidebar ketma-ketligi (yangi):**
```
Mahsulotlar → Birlik Iqtisodiyoti → Kalkulyator → POS Terminal
  → ★ FBS buyurtmalar ★   ← yangi
  → Hujjat yaratish → Import/Export → QR Chop etish → Mening do'konlarim
```

---

## 3. `dark.md` — Changelog qator

**Joy:** "O'zgartirishlar tarixi" jadvalining oxiri (~587-qator)

**Qo'shildi:**
```markdown
| 2026-05-21 | Yangi sahifa: `templates/fbs_orders.html` (FBS buyurtmalar — Seller OpenAPI). `base.html` extend qiladi, dark CSS o'sha faylning `{% block extra_css %}` ichida `.fbs-*` klasslari uchun yozildi. Status pillalar (CREATED/ASSEMBLED/SENT/IN_PROGRESS/MOVED_TO_DELIVERY/COMPLETED) light + dark variantlari bilan. Sidebar'ga `SL.fbs` link qo'shildi (`/fbs`). |
```

---

## Yangi yaratilgan fayllar ro'yxati

| Fayl | Maqsad |
|---|---|
| `fbs/__init__.py` | Bo'sh (Python paket marker) |
| `fbs/routes.py` | Blueprint: `/fbs`, `/fbs/<id>`, `/fbs/api/orders`, `/fbs/api/count`, `/fbs/api/order/<id>` |
| `templates/fbs_orders.html` | UI: ro'yxat sahifasi (11 status, scheme filtri, klikli qatorlar) |
| `templates/fbs_order_detail.html` | UI: detail sahifa (placeholder — endpoint swagger kutilmoqda) |
| `core/fbs_data.py` | Data source seam (Stage 4 uchun) — routes faqat shu yerdan import qiladi |
| `fbs_docs/` (papka) | Bu hujjatlar |

`core/uzum_openapi.py`'ga qo'shilgan funksiyalar (refactor'dan keyin):
- `_LEGACY_FBS_ORDER_URL_VARIANTS` constant + `_fbs_orders_request_with_auth()` helper
- `fetch_fbs_orders_page()` — `/v2/fbs/orders` aniq URL, status/scheme/date_*_ms parametrlari
- `extract_fbs_orders_list()` — faqat `payload.orders` + `payload.totalAmount` o'qiydi
- `FBS_ORDER_STATUSES` (11 ta tuple), `FBS_ORDER_SCHEMES` (FBS, DBS) — yagona manba

## Bosqich 1 holati (2026-05-21)

✅ **Tugagan:**
- `/v2/fbs/orders` to'liq integratsiya (real schema, real field nomlari)
- 11-status enum jadval
- FBS / DBS / both filteri (`scheme`)
- Klikli qatorlar → `/fbs/<id>`
- Status badge'lar (statik chip'lar, ranglar dark mode bilan)
- `core/fbs_data.py` seam — Stage 4 uchun tayyor
- Hech qanday `pick(o, [...])` taxminiy fallback yo'q

⏳ **Kutilmoqda (count + detail endpoint swagger):**
- `GET /v2/fbs/orders/count` — sahifa tepasidagi badge sonlari
- `GET /v1/fbs/order/{orderId}` — detail sahifa real field mapping

Ikkalasi ham route'lari mavjud, lekin `get_fbs_count`/`get_fbs_order_detail`
funksiyalari `NotImplementedError` qaytaradi. UI 501 holatini graceful
boshqaradi (badge'larda "·" placeholder, detail sahifada "endpoint kutilmoqda" banner).

---

## Agar rollback kerak bo'lsa

```bash
# 1. Yangi fayllarni o'chir
rm -rf fbs/ templates/fbs_orders.html fbs_docs/

# 2. core/uzum_openapi.py:311-428 ni o'chir (FBS qismi)

# 3. app.py:969-973 ni o'chir (FBS blueprint register)

# 4. static/uzum_ui.js'da fbs label va link qatorlarini o'chir

# 5. dark.md'dan 2026-05-21 qatorini o'chir

# 6. Docker rebuild
docker compose up -d --build
```
