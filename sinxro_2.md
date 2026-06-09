# sinxro_2 — Server-side Product Sync Loop

> **Sana:** 2026-05-24
> **Vazifa:** Mahsulotlar (rang, o'lcham, narx, ombor miqdori) avto-sync —
> faqat server tomondan, brauzer'ga halaqit bermay.

---

## Nima uchun kerak edi?

**Avval (eski holat):**
- Brauzer (`static/uzum_ui.js`) har 10 daqiqada avtomatik `/api/uzum/sync` ni
  HAR shop uchun chaqirardi va keyin `window.location.reload()` qilardi.
- Natijada: **har 10 daqiqada sahifa 30-50 soniyaga qotardi** va o'z-o'zidan reload bo'lardi.
- Bu kod 2026-05-24 da olib tashlandi (Initial import'dan beri bor edi).

**Muammo:** Brauzer kodi olib tashlangach, server tomonda mahsulot sync loop'i **yo'q edi**.
Faqat finance va FBS uchun loop bor edi. Demak yangi tovar/rang/o'lcham qo'shilsa,
avtomatik kelmasdi — qo'lda "Yangilash" bosish kerak edi.

**Yechim:** Server tomondan yangi `_products_sync_loop` — xuddi FBS bilan bir xil pattern.

---

## Qaysi fayllar o'zgardi

| Fayl | Nima qo'shildi |
|---|---|
| [app.py](app.py) | `_products_sync_tick()` va `_products_sync_loop()` funksiyalari |
| [admin/routes.py](admin/routes.py) | `_fire_finance_seed`'ga `_run_products_seed` thread qo'shildi |
| [background/startup.py](background/startup.py) | `products-sync` loop boshlanishi |
| [static/uzum_ui.js](static/uzum_ui.js) | Eski brauzer setInterval olib tashlandi |

---

## Asosiy kod

### 1. `_products_sync_tick()` — bitta tsikl

**Joy:** `app.py`, FBS cleanup loop'idan keyin (qidirish: `# sinxro_2 —`)

```python
def _products_sync_tick():
    """One pass: sync products for every shop that has an OpenAPI token."""
    tick_start = datetime.utcnow()
    with SessionLocal() as db:
        shops = db.execute(select(Shop).order_by(Shop.id)).scalars().all()
    if not shops:
        print("[Products Sync] No shops, nothing to do")
        return

    # Har shop uchun token oldindan olamiz
    work: list[tuple[int, str, str]] = []  # (shop.id, uzum_id, token)
    for s in shops:
        tok = _owner_openapi_token_for_shop(s.uzum_id)
        if not tok:
            continue
        work.append((s.id, str(s.uzum_id), tok))

    print(f"[Products Sync] Tick start: {len(work)}/{len(shops)} shops have tokens")
    if not work:
        return

    def _sync_one(shop_id_int: int, shop_uzum_id: str, token: str) -> None:
        try:
            result = _sync_products_via_openapi(
                shop_uzum_id, token,
                size=100, max_pages=500,
                fetch_uz_titles=True,
            )
            n_p = result.get("products") if isinstance(result, dict) else None
            n_v = result.get("variants") if isinstance(result, dict) else None
            print(f"[Products Sync] shop={shop_uzum_id} products={n_p} variants={n_v}")
        except Exception as e:
            print(f"[Products Sync] shop={shop_uzum_id} ERROR: {e!r}")

    max_parallel = max(1, min(_PRODUCTS_SYNC_PARALLELISM, len(work)))
    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        for shop_id_int, shop_uzum_id, token in work:
            pool.submit(_sync_one, shop_id_int, shop_uzum_id, token)

    elapsed = (datetime.utcnow() - tick_start).total_seconds()
    print(f"[Products Sync] Tick done in {elapsed:.1f}s "
          f"(synced={len(work)}, parallelism={max_parallel})")
```

### 2. `_products_sync_loop()` — doimiy loop

```python
def _products_sync_loop():
    """Run _products_sync_tick every _PRODUCTS_SYNC_INTERVAL_SEC seconds."""
    import time as _t
    import traceback

    _t.sleep(30)  # warm-up

    while True:
        try:
            _products_sync_tick()
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:
            print(f"[Products Sync] Tick unexpected error: {e!r}")
            traceback.print_exc()
        _t.sleep(_PRODUCTS_SYNC_INTERVAL_SEC)
```

### 3. Konstantalar

```python
# 10 daqiqa — eski brauzer ritmi bilan bir xil
_PRODUCTS_SYNC_INTERVAL_SEC = int(os.environ.get("PRODUCTS_SYNC_INTERVAL_SEC", "600") or "600")

# 5 ta shop parallel — har xil token'lar bir-biriga halaqit bermaydi
_PRODUCTS_SYNC_PARALLELISM = int(os.environ.get("PRODUCTS_SYNC_PARALLELISM", "5") or "5")
```

### 4. Yangi do'kon qo'shilganda — initial seed

**Joy:** `admin/routes.py`, `_fire_finance_seed()` funksiyasi ichida

```python
def _run_products_seed(uzum_id=uzum_id, shop_pk=shop_pk):
    tok = _app._owner_openapi_token_for_shop(uzum_id)
    if not tok:
        print(f"[AdminShop] Products seed skipped for {uzum_id} — no OpenAPI token")
        return
    try:
        _app._sync_products_via_openapi(
            uzum_id, tok,
            size=100, max_pages=500,
            fetch_uz_titles=True,
        )
    except Exception as e:
        print(f"[AdminShop] Products seed failed for {uzum_id}: {e}")

threading.Thread(target=_run_products_seed, daemon=True).start()
# ... + 2 ta eski thread (finance seed, full backfill)
```

### 5. Loop'ni boshlash

**Joy:** `background/startup.py`

```python
if os.environ.get("PRODUCTS_SYNC_LOOP", "1").strip().lower() not in ("0", "false", "no"):
    threading.Thread(target=_app._products_sync_loop, daemon=True, name="products-sync").start()
    print("[Background] Started: products-sync loop (sinxro_2)")
```

---

## Qanday ishlaydi

```
┌─────────────────────────────────────────────────────────────┐
│  SERVER ICHIDA (Docker konteyner)                           │
│                                                              │
│  1. App ishga tushgach 30 sek kutadi (warm-up)              │
│                                                              │
│  2. Loop boshlanadi:                                        │
│     ┌─────────────────────────────────────────────────┐    │
│     │ _products_sync_tick()                            │    │
│     │  ├─ Hamma Shop'larni DB'dan o'qiydi             │    │
│     │  ├─ Har biriga token bormi tekshiradi           │    │
│     │  ├─ Token bor bo'lganlarni 5 parallel sync qiladi│   │
│     │  ├─ Har sync: _sync_products_via_openapi(...)   │    │
│     │  │  ├─ Uzum /v1/product/shop/{id} GET           │    │
│     │  │  ├─ RU titles + barcha numeric maydonlar     │    │
│     │  │  ├─ UZ titles (ikkinchi o'tish)              │    │
│     │  │  └─ DB'ga UPSERT (Variant, ProductGroup)     │    │
│     │  └─ Natija: products=X, variants=Y log'da       │    │
│     └─────────────────────────────────────────────────┘    │
│                                                              │
│  3. 600 sekund (10 daqiqa) kutadi                           │
│                                                              │
│  4. Qadam 2 ga qaytadi (cheksiz)                            │
└─────────────────────────────────────────────────────────────┘
```

**Yangi do'kon qo'shilganda:**
```
admin/routes.py:add_shop() → _fire_finance_seed()
                                │
                                ├─ _run_products_seed thread  (sinxro_2 — YANGI)
                                ├─ _run_variant_seed thread   (finance)
                                └─ _run_full_backfill thread  (45 kun moliya tarixi)
```

---

## Sozlamalar (`.env`)

| O'zgaruvchi | Default | Tushuncha |
|---|---|---|
| `PRODUCTS_SYNC_LOOP` | `1` | `0` qilsangiz loop o'chiriladi |
| `PRODUCTS_SYNC_INTERVAL_SEC` | `600` | Tick'lar orasi (sekund). `1800` qilsa 30 daq |
| `PRODUCTS_SYNC_PARALLELISM` | `5` | Bir vaqtda nechta shop sync qilinadi |

---

## Log misollari

**Tick boshlanishi:**
```
[Products Sync] Tick start: 5/5 shops have tokens
```

**Har shop natijasi:**
```
[Products Sync] shop=5983 products=450 variants=2565
[Products Sync] shop=10945 products=27 variants=249
[Products Sync] shop=51948 products=19 variants=261
[Products Sync] shop=40571 products=57 variants=235
[Products Sync] shop=13505 products=5 variants=28
```

**Tick tugashi:**
```
[Products Sync] Tick done in 12.3s (synced=5, parallelism=5)
```

**Token yo'q shop:**
```
[AdminShop] Products seed skipped for 99999 — no OpenAPI token
```

---

## Eski brauzer kodi (olib tashlandi)

**Fayl:** `static/uzum_ui.js`, qator 661-673 (oldin)

```js
// --- Auto Refresh Logic (Every 10 Minutes) ---
setInterval(async () => {
  console.log("Auto-refreshing data...");
  try {
    const shopsRes = await fetch("/api/shops");
    const shopsData = await shopsRes.json();
    if (shopsData.shops) {
      for (const shop of shopsData.shops) {
        await postJson("/api/uzum/sync", { shop_id: shop.uzum_id, size: 100, sync_all: true });
      }
      window.location.reload();    // ← sahifani qotirardi
    }
  } catch (e) { console.error("Auto-sync failed", e); }
}, 10 * 60 * 1000);
```

**Nima uchun yomon edi:**
- Har 10 daqiqada brauzer 5 ta sync chaqirardi
- Har sync ~5 sekund → 25-50 sekund freeze
- Tugagach `window.location.reload()` qilardi → ish vaqtida ishingiz yo'qolardi
- N ta tab ochiq bo'lsa, N marta ko'p yuk

---

## Testlash

**Manual tick:**
```bash
docker exec sellerhub_uz-app-1 python -c "from app import _products_sync_tick; _products_sync_tick()"
```

**Loop o'chirish (debug):**
```bash
# .env ga qo'shing:
PRODUCTS_SYNC_LOOP=0
```
Keyin `docker compose up -d --build app`

**Log'larni kuzatish:**
```bash
docker logs -f sellerhub_uz-app-1 2>&1 | grep "Products Sync"
```

---

## Kelajakda kuzatish kerak

- **Uzum rate limit:** Hozir 5 ta shop parallel. Agar Uzum sekinlashtirsa, `PRODUCTS_SYNC_PARALLELISM=3` qiling.
- **Catalog kattalashishi:** Hozir 5 ta shop = 3300 ta variant. 50+ shop bo'lsa, tick uzayadi (~1-2 daq). Kerak bo'lsa interval'ni 30 daqiqaga uzaytiring.
- **OpenAPI'da yangi maydon:** Agar Uzum yangi maydon qo'shsa (masalan, `color_hex`), `_sync_products_via_openapi` ni yangilash kerak — bu loop u funksiyani chaqirgani uchun avtomatik foydalanadi.

---

## Kim qildi va qachon

- **Olib tashlash + yangi loop:** 2026-05-24
- **Suhbat:** Abdulaziz bilan, sahifa qotib qolish muammosi sababli
- **Tag:** `sinxro_2` — kodda izlash uchun (`grep -r "sinxro_2" .`)
