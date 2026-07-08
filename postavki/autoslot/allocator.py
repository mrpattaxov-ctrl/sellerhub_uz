"""Avto-slot ALLOKATOR — konsumer (biznes-logika, asosiy serverda QOLADI).

Vazifasi: `SlotFreedEvent` kelganda → shu bucket+kunni kutayotgan aktlarni
topish → «vaqt + hajm» qoidasi bo'yicha sig'imga tanlash → har birini band
qilish (parallel). Monitoring'ni HECH QACHON import qilmaydi — faqat `bus`
orqali event oladi.

`select_for_capacity` (packing, B5) + booking/atomic-claim (B6) to'liq.
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

from postavki.autoslot import bus, store
from postavki.autoslot.events import SlotFreedEvent
from postavki.autoslot.day import day_of, fmt_slot

# Telegram bildirishnoma — DI (app loop o'rnatadi; allokator app.py'ni import
# qilmaydi, modul mustaqilligini saqlaydi). None bo'lsa — jim (faqat log).
_notifier = None


def set_notifier(fn) -> None:
    """App startup'da chaqiriladi: `fn(text)` Telegramga yuboradi."""
    global _notifier
    _notifier = fn


def _notify(text: str) -> None:
    if _notifier is None:
        return
    try:
        _notifier(text)
    except Exception as e:
        print(f"[autoslot.allocator] notify xato: {e!r}")

# Atomic-claim ijara (sekund) — booking shu vaqt ichida tugashi kutiladi;
# o'tib ketsa release_expired akt'ni qaytaradi.
CLAIM_LEASE_SEC = 30
# Bir vaqtda parallel set_time_slot soni (har akt boshqa do'kon tokeni).
BOOK_MAX_WORKERS = 8
# Poyga yutqazilsa (time-slot-003 va h.k.) o'sha event ichida yana necha
# QO'SHIMCHA raund keyingi nomzodlar sinaladi. KONSERVATIV (Abdulaziz
# 2026-07-03): 1 raund, retry nomzodlari faqat xato bo'lgan hajmga
# sig'adiganlar — boshqa rejalarning attempts budjeti va rate-limit himoyasi.
RETRY_ROUNDS = 1


def autoslot_mode() -> str:
    """Rejim (env `POSTAVKA_AUTOSLOT_MODE`): off/dry/live.

    DEFAULT **dry** — band QILMAYDI, faqat «band qilardik»ни loglaydi. Eski
    `slot_grabber` hali live ishlaydi; ikkalasi urishmasligi uchun yangi
    allokator switchover'gача dry turadi. Yoqish: =live (+ eski grabber'ni
    POSTAVKA_GRAB_LOOP=0 bilan o'chir). Butunlay jim: =off.
    """
    m = os.environ.get("POSTAVKA_AUTOSLOT_MODE", "dry").strip().lower()
    return m if m in ("off", "dry", "live") else "dry"


def select_for_capacity(candidates: list[dict], free_volume: int) -> list[dict]:
    """“VAQT + HAJM” tanlovi: bo'shagan `free_volume` ni nomzodlar bilan
    to'ldiradi — NAVBAT-tartibida first-fit.

    `candidates` — `store.candidates_for` qaytargani: `enabled_at` (navbat)
    bo'yicha o'sish tartibida. Shu tartibda yuramiz; hajmi qolgan joyga
    SIG'SA — olamiz, SIG'MASA — o'tkazib yuboramiz (kichikroq keyingi aktlar
    qolgan joyni TO'LDIRADI; o'tkazilgan katta akt o'z hajmiga mos kattaroq
    slot bo'shaganда oladi). Bu bitta o'tish ADOLAT (birinchi yoqqan yuqori)
    va TO'LDIRISH (joy behuda ketmasin) ni birga beradi.

    ⚠️ Ziddiyat (tunable): qat'iy navbat ba'zan slotni to'liq to'ldirmaydi
    (masalan A=90 olinsa, 10 behuda; B+C=100 ko'proq to'ldirardi-yu, A
    navbatdan tushardi). Abdulaziz qarori: ADOLAT birlamchi → navbatni saqlaymiz.

    -> tanlangan aktlar (hajmlari yig'indisi <= free_volume), navbat tartibida.
    """
    remaining = int(free_volume or 0)
    if remaining <= 0:
        return []
    chosen: list[dict] = []
    for c in candidates:
        vol = int(c.get("volume") or 0)
        if vol <= 0:
            continue  # noto'g'ri/bo'sh akt — o'tkaz
        if vol <= remaining:
            chosen.append(c)
            remaining -= vol
    return chosen


def _book_one(item: dict, slot_from_ms: int) -> tuple[str, dict]:
    """Bitta aktni bo'shagan slotga band qiladi (LIVE — real set_time_slot).
    Exception otmaydi; natijani DB'ga yozadi. -> (holat, item).
    holat: "booked" | "failed" | "error".
    """
    from postavki import client  # lazy — modul mustaqilligini saqlash uchun
    pool = item.get("pool_source") or "FULLFILMENT"
    stock_id = item.get("stock_id")
    if not stock_id:
        store.mark_failed(item["id"], "stockId yo'q", requeue=False)
        return ("failed", item)
    try:
        inv = client.set_time_slot(item["shop_uzum_id"], int(item["invoice_id"]),
                                   int(slot_from_ms), int(stock_id), pool)
    except Exception as e:
        # Sig'im boshqa olib qo'ydi / 429 / xato — qaytarib qo'yamiz (keyingi event).
        store.mark_failed(item["id"], f"{type(e).__name__}: {e}", requeue=True)
        return ("error", item)
    ok = bool(inv and inv.get("id"))
    if ok:
        store.mark_booked(item["id"], slot_from_ms)
        # Slot o'zgardi (re-booking) → «Акт отправки» PDF mazmuni ham o'zgaradi.
        # Eski keshni o'chirib, yangisini FONda tortamiz — UI `set-slot` route
        # bilan bir xil (aks holda akt eski slotni ko'rsatib qoladi).
        try:
            from postavki import akt_cache  # lazy — modul mustaqilligini saqlash
            akt_cache.warm_one_async(item["shop_uzum_id"], int(item["invoice_id"]),
                                     date_updated=inv.get("dateUpdated"))
        except Exception as e:
            print(f"[autoslot.allocator] akt-kesh yangilash xato "
                  f"invoice={item.get('invoice_id')}: {e!r}")
    else:
        store.mark_failed(item["id"], "javob bo'sh", requeue=True)
    return ("booked" if ok else "failed", item)


def _book_parallel(items: list[dict], slot_from_ms: int) -> list[tuple[str, dict]]:
    """Tanlangan aktlarni PARALLEL band qiladi (har biri o'z do'kon tokeni)."""
    if not items:
        return []
    workers = min(BOOK_MAX_WORKERS, len(items))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(lambda it: _book_one(it, slot_from_ms), items))


def on_slot_freed(event: SlotFreedEvent) -> None:
    """Konsumer handler: bitta bo'shagan slot uchun to'liq qaror→harakat.

      0. release_expired() — qotgan claim'larni qaytar.
      1. nomzodlar = candidates_for(day)        (muddat; ombor global)
      2. tanlangan = select_for_capacity(...)   (navbat+hajm first-fit)
      3. mode dry → faqat logla; live → claim (atomic) → parallel set_time_slot.
    """
    store.release_expired()
    day = day_of(event.slot_from_ms)
    # slot_from_ms uzatamiz: re-booking aktlar uchun faqat ERTAROQ slot mos.
    candidates = store.candidates_for(day, event.slot_from_ms)
    chosen = select_for_capacity(candidates, event.free_volume)
    mode = autoslot_mode()
    print(f"[autoslot.allocator] event: kun={day} hajm={event.free_volume} "
          f"src={event.source_shop_id} → {len(candidates)} nomzod, "
          f"{len(chosen)} tanlandi, mode={mode}")
    if not chosen or mode == "off":
        return
    slot_s = fmt_slot(event.slot_from_ms)
    nums = ", ".join(f"№{c.get('invoice_number') or c['invoice_id']}" for c in chosen)
    if mode != "live":
        print(f"[autoslot.allocator] DRY — band qilardik: "
              f"{[c['invoice_id'] for c in chosen]} → slot {event.slot_from_ms}")
        _notify(f"🧪 *Avto-slot (sinov)*\nSlot bo'shadi: {slot_s} · {event.free_volume} birlik\n"
                f"Tanlanardi: {nums}\n_band qilinmadi — sinov rejimi_")
        return
    # LIVE — faqat ATOMIC claim qilingan aktlar band qilinadi (ikki event bir
    # aktni urishtirmasin). Claim'dan o'tmaganlar boshqa event qo'lида.
    # Raundlar: 1 asosiy + RETRY_ROUNDS — booking xato bo'lsa (raqobatchi
    # oldin oldi / 429) bo'shagan sig'im shu event ichida KEYINGI nomzodlarga
    # taklif qilinadi, aks holda u boy beriladi (keyingi event kelmasligi mumkin).
    tried: set[int] = set()
    ok_nums: list[str] = []
    capacity = int(event.free_volume or 0)
    for rnd in range(1 + RETRY_ROUNDS):
        if rnd:
            # Retry: faqat hali sinalmagan nomzodlar, faqat XATO bo'lgan hajm
            # doirasida (undan katta akt baribir sig'masdi deb hisoblaymiz).
            chosen = select_for_capacity(
                [c for c in candidates if c["id"] not in tried], capacity)
            if not chosen:
                break
        claimed = set(store.claim([c["id"] for c in chosen], lease_sec=CLAIM_LEASE_SEC))
        tried |= {c["id"] for c in chosen}
        to_book = [c for c in chosen if c["id"] in claimed]
        if not to_book:
            break
        results = _book_parallel(to_book, event.slot_from_ms)
        ok_nums += [f"№{it.get('invoice_number') or it['invoice_id']}"
                    for st, it in results if st == "booked"]
        failed_vol = sum(int(it.get("volume") or 0)
                         for st, it in results if st != "booked")
        print(f"[autoslot.allocator] LIVE raund {rnd + 1}: "
              f"{sum(1 for st, _ in results if st == 'booked')}/{len(to_book)} "
              f"band qilindi → slot {event.slot_from_ms}")
        if failed_vol <= 0:
            break
        capacity = failed_vol
    if ok_nums:
        _notify(f"✅ *Avto-slot band qilindi*\nSlot: {slot_s}\n"
                f"{len(ok_nums)} ta akt: {', '.join(ok_nums)}")


def _notify_terminal(item: dict) -> None:
    """Reja TERMINAL `failed` bo'ldi (urinishlar tugadi / stockId yo'q /
    booking muddati o'tdi) — foydalanuvchi bilsin, aks holda reja jimgina
    yo'qoladi (panel `failed`ni yashiradi)."""
    num = item.get("invoice_number") or item.get("invoice_id")
    print(f"[autoslot.allocator] TERMINAL failed plan={item.get('id')} "
          f"№{num}: {item.get('error')}")
    _notify(f"❌ *Avto-slot to'xtadi*\nAkt №{num}\n"
            f"Sabab: {item.get('error') or 'noma’lum'}\n"
            f"_Reja to'xtatildi — kerak bo'lsa avto-slotni qayta yoqing._")


# Store terminal o'tishlarda shu orqali xabar beradi (DI — store allokatorni
# import qilmaydi; ro'yxatdan o'tkazish shu modul yuklanganda bir marta).
store.set_on_terminal(_notify_terminal)


def run() -> None:
    """Allokator thread kirish nuqtasi — event'larni doimiy iste'mol qiladi."""
    print("[autoslot.allocator] konsumer ishga tushdi")
    bus.consume_forever(on_slot_freed)
