"""Avto-slot BOOKER — bitta invoice uchun «o'zini-band-qilish» urinishi.

Bu modul DETEKTORGA BOG'LIQ EMAS (Abdulaziz 2026-07-09, yakuniy reja): har
invoice O'Z bo'sh slotini so'raydi (`time-slot/get`) va topilsa O'ZINI band
qiladi (`time-slot/set`). Booking-processor (app.py `_postavka_booking_loop`)
har 10ms da navbatdagi bitta invoice uchun shu `attempt_booking`ni non-blocking
ishga tushiradi.

XAVFSIZLIK QOIDALARI (kod bilan qotirilgan):
  • GET (`invoice_time_slots`) — read-only, cheksiz takrorlanadi: slot chiqquncha
    yoki invoice muddati (`max_date`) o'tguncha.
  • SET (`set_time_slot`) — MUTATSION + Uzum 3× budjet/penalty. Har invoice uchun
    FAQAT BIR MARTA otiladi: slot topilishi bilanoq bir SET → natija (booked yoki
    terminal fail), qayta SET YO'Q. Shuning uchun 3× budjet hech qachon portlamaydi.
  • Hard 1s timeout — booking thread'lari `client.enable_fast_read(1.0)` bilan
    ishlaydi → GET/SET warm, retry'siz, 1s da tashlanadi (`requests.Timeout`).

Yadro (`pick_slot_in_window`) — TOZA funksiya (DB/tarmoq yo'q), pytest bilan
qoplangan. `attempt_booking` — portal yelimi; EXCEPTION OTMAYDI (natijani dict
qaytaradi), booking-loop shuni holat/metrikaga aylantiradi.
"""
from __future__ import annotations

import os
from datetime import date

import requests

from postavki import client
from postavki.autoslot.day import day_of, TASHKENT

# ── Natija «outcome» kodlari (booking-loop shu bo'yicha holat/queue boshqaradi) ──
BOOKED = "booked"            # ✅ SET muvaffaqiyatli — terminal, navbatdan olib tashla
NO_SLOT = "no_slot"          # slot hali yo'q — navbatda QOLADI (keyingi raundда GET)
EXPIRED = "expired"          # max_date o'tdi — terminal fail, navbatdan olib tashla
GET_TIMEOUT = "get_timeout"  # GET 1s hard-limit oshdi — TRANZIENT timeout, navbatda qoladi
GET_ERROR = "get_error"      # GET tarmoq/API xato — TRANZIENT, navbatda qoladi (SET otilmadi)
SET_TIMEOUT = "set_timeout"  # SET 1s timeout + verify «band emas» — terminal (SET bir marta)
SET_FAILED = "set_failed"    # SET bo'sh javob / xato — terminal (SET bir marta)
NO_STOCK = "no_stock"        # stockId yo'q — terminal (band qilib bo'lmaydi)
DRY = "dry"                  # sinov rejimi — slot topildi, band QILMADIK (navbatда qoladi)

# Terminal (invoice navbatdan olib tashlanadi) — booked yoki qaytmas fail.
_TERMINAL = {BOOKED, EXPIRED, SET_TIMEOUT, SET_FAILED, NO_STOCK}
# SET oqibatida qaytmas fail bo'lgan holatlar (booked emas) — Telegram «to'xtadi».
_FAILED_TERMINAL = {EXPIRED, SET_TIMEOUT, SET_FAILED, NO_STOCK}
# 1s hard-limit oshgan (timeout) natijalar — digestда «timeout» sifatida sanaladi.
_TIMEOUT = {GET_TIMEOUT, SET_TIMEOUT}


def is_terminal(outcome: str) -> bool:
    """Invoice navbatdan olib tashlanadimi (booked yoki qaytmas fail)?"""
    return outcome in _TERMINAL


def is_failed_terminal(outcome: str) -> bool:
    """Terminal, lekin muvaffaqiyatsiz (Telegram muammo-digestiga tushadi)?"""
    return outcome in _FAILED_TERMINAL


def is_timeout(outcome: str) -> bool:
    """Timeout hisoblagichini oshiradigan natijami (1s hard-limit oshdi)?"""
    return outcome in _TIMEOUT


def booking_mode() -> str:
    """Rejim (env `POSTAVKA_BOOKING_MODE`): off/dry/live.

    KOD DEFAULT **dry** — band QILMAYDI, faqat «band qilardik»ni loglaydi
    (konteyner env'siz recreate bo'lsa xavfsiz holatga tushadi). Jonli band
    qilish: docker-compose'да `POSTAVKA_BOOKING_MODE=live`. Butunlay o'chirish:
    `=off` yoki `POSTAVKA_BOOKING_LOOP=0`.
    """
    m = os.environ.get("POSTAVKA_BOOKING_MODE", "dry").strip().lower()
    return m if m in ("off", "dry", "live") else "dry"


# ── Yadro: [min_date .. max_date] oynasidagi eng erta slot (TOZA) ────────────


def pick_slot_in_window(slots: list[dict], min_date: date | None,
                        max_date: date | None) -> int | None:
    """Invoice'ning o'z bo'sh slotlari ichidan `[min_date .. max_date]` oynasiga
    tushadigan ENG ERTA slotni tanlaydi.

    slots: [{"timeFrom":ms, "timeTo":ms}, ...] — Uzum faqat SHU invoice'ga
           yaroqlilarni qaytaradi (dim/hajm avtomatik mos).
    min_date NULL → pastki chegarasiz; max_date NULL → yuqori chegarasiz.
    -> eng erta mos slot timeFrom (ms) yoki None (oynada slot yo'q).
    """
    best = None
    for s in slots:
        tf = s.get("timeFrom") if isinstance(s, dict) else None
        if not tf:
            continue
        tf = int(tf)
        d = day_of(tf)
        if min_date is not None and d < min_date:
            continue
        if max_date is not None and d > max_date:
            continue
        if best is None or tf < best:
            best = tf
    return best


# ── Verify: SET timeout ambiguity (so'rov server'da qo'llangan bo'lishi mumkin) ──


def verify_booked(shop_uzum_id: str, invoice_id: int) -> bool:
    """SET 1s'da timeout bo'ldi — so'rov Uzum'да ALLAQACHON qo'llangan bo'lishi
    mumkin. Qayta SET otish (idempotent EMAS + budjet) o'rniga bir marta
    TEKSHIRAMIZ: invoice endi slotга ega bo'lsa (timeSlotReservation) → booked.

    Kamdan-kam yo'l (faqat SET timeout) → normal (retrying) list oqimi. Xato →
    False (ehtiyotkor: band emas deb hisoblaymiz, LEKIN qayta SET OTILMAYDI)."""
    try:
        invs = client.list_invoices(shop_uzum_id, page=0, size=100, statuses="")
    except Exception:
        return False
    for inv in invs:
        try:
            if int(inv.get("id") or 0) == int(invoice_id):
                return bool(inv.get("timeSlotReservation"))
        except Exception:
            continue
    return False


# ── Bitta booking urinishi (portal yelimi — EXCEPTION OTMAYDI) ───────────────


def attempt_booking(plan: dict, *, mode: str = "live", today: date | None = None) -> dict:
    """Bitta invoice uchun: O'Z slotlarini so'ra → oynada slot bo'lsa BIR MARTA
    band qil. Exception otmaydi — natijani dict qaytaradi.

    plan: store item dict (shop_uzum_id, invoice_id, invoice_number, pool_source,
          stock_id, min_date, max_date, ...).
    mode: "live" (real SET) | "dry" (loglaydi, SET yo'q) | "off" (hech narsa).
    today: Toshkent kuni (muddat tekshiruvi uchun; test'да in'ektsiya).

    -> {"outcome": <kod>, "invoice_id", "slot_from_ms"|None, "error"|None}.
    """
    if today is None:
        from datetime import datetime
        today = datetime.now(TASHKENT).date()

    shop = plan["shop_uzum_id"]
    inv_id = int(plan["invoice_id"])
    pool = plan.get("pool_source") or "FULLFILMENT"
    min_d = plan.get("min_date")
    max_d = plan.get("max_date")

    if mode == "off":
        return {"outcome": NO_SLOT, "invoice_id": inv_id, "slot_from_ms": None, "error": None}

    # MUDDAT: max_date o'tgan bo'lsa — bu invoice uchun booking oynasi tugadi.
    if max_d is not None and today > max_d:
        return {"outcome": EXPIRED, "invoice_id": inv_id, "slot_from_ms": None,
                "error": f"muddat o'tdi ({max_d})"}

    # 1. GET — invoice'ning O'Z bo'sh slotlari (read-only, hard 1s, retry'siz).
    try:
        slots = client.invoice_time_slots(shop, inv_id, pool)
    except requests.Timeout as e:
        # 1s hard-limit oshdi — TRANZIENT, navbatда qoladi (SET otilmadi, budjet toza).
        return {"outcome": GET_TIMEOUT, "invoice_id": inv_id, "slot_from_ms": None,
                "error": f"GET timeout: {type(e).__name__}"}
    except (requests.ConnectionError, Exception) as e:
        return {"outcome": GET_ERROR, "invoice_id": inv_id, "slot_from_ms": None,
                "error": f"GET {type(e).__name__}: {e}"[:200]}

    # 2. Oynadagi eng erta slot.
    slot_tf = pick_slot_in_window(slots or [], min_d, max_d)
    if not slot_tf:
        return {"outcome": NO_SLOT, "invoice_id": inv_id, "slot_from_ms": None,
                "error": None}

    # 3. Slot topildi → dry bo'lsa faqat logla (navbatда qoladi).
    if mode != "live":
        return {"outcome": DRY, "invoice_id": inv_id, "slot_from_ms": slot_tf, "error": None}

    # 4. LIVE — SET FAQAT BIR MARTA (mutatsion, 3× budjet).
    stock_id = plan.get("stock_id")
    if not stock_id:
        return {"outcome": NO_STOCK, "invoice_id": inv_id, "slot_from_ms": slot_tf,
                "error": "stockId yo'q"}
    try:
        inv = client.set_time_slot(shop, inv_id, int(slot_tf), int(stock_id), pool)
    except (requests.Timeout, requests.ConnectionError) as e:
        # SET timeout — server'да qo'llangan BO'LISHI mumkin. Qayta SET emas → verify.
        if verify_booked(shop, inv_id):
            return {"outcome": BOOKED, "invoice_id": inv_id, "slot_from_ms": slot_tf,
                    "error": None}
        return {"outcome": SET_TIMEOUT, "invoice_id": inv_id, "slot_from_ms": slot_tf,
                "error": f"SET timeout: {type(e).__name__}"}
    except Exception as e:
        return {"outcome": SET_FAILED, "invoice_id": inv_id, "slot_from_ms": slot_tf,
                "error": f"SET {type(e).__name__}: {e}"[:200]}

    ok = bool(inv and inv.get("id"))
    if ok:
        return {"outcome": BOOKED, "invoice_id": inv_id, "slot_from_ms": slot_tf,
                "error": None, "inv": inv}
    return {"outcome": SET_FAILED, "invoice_id": inv_id, "slot_from_ms": slot_tf,
            "error": "SET javob bo'sh"}


# ── DRY «band qilardim» oniy xabari (TOZA formatter) ─────────────────────────


def format_would_book(plan: dict, slot_from_ms: int) -> str:
    """DRY rejimda slot topilganda «band qilardim» Telegram xabari. Loop buni
    har invoice uchun tanlangan slot O'ZGARGANDA yuboradi (dedup — spam yo'q).
    """
    from postavki.autoslot.day import fmt_slot
    num = plan.get("invoice_number") or plan.get("invoice_id")
    vol = plan.get("volume")
    vol_s = f" · {vol} dona" if vol else ""
    min_d, max_d = plan.get("min_date"), plan.get("max_date")
    window = ""
    if min_d or max_d:
        window = f"\nOyna: {min_d or '—'} → {max_d or '—'}"
    return (f"🧪 *Band qilardim (sinov)*\n"
            f"Akt: №{num}{vol_s}\n"
            f"Slot: {fmt_slot(int(slot_from_ms))}{window}\n"
            f"_sinov rejimi — band qilinmadi_")


# ── Soatlik Telegram muammo-digesti (TOZA formatter) ─────────────────────────


def format_booking_digest(now_ms: int, totals: dict, problems: list[dict],
                          window_min: int = 60) -> str:
    """Davriy xulosani Telegram markdown'iga aylantiradi.

    totals: {"attempted","booked","failed","timed_out"} — oxirgi `window_min`
            daqiqada.
    problems: [{"invoice_number"|"invoice_id","attempts","timeouts","failures",
                "last_error"}, ...] — faqat MUAMMOLI invoice'lar.
    window_min: xulosa oynasi (daqiqa) — sarlavhada ko'rsatiladi.
    -> Telegram matn (markdown).
    """
    from datetime import datetime
    dt = datetime.fromtimestamp(now_ms / 1000, TASHKENT)
    head = (f"📦 *Avto-band — xulosa*\n"
            f"_{dt.day:02d}.{dt.month:02d} {dt:%H:%M} · oxirgi {window_min} daqiqa_\n\n")
    stat = (f"• Urinilgan invoice: *{totals.get('attempted', 0)}*\n"
            f"• ✅ Band bo'ldi: *{totals.get('booked', 0)}*\n"
            f"• ❌ Xato urinish: *{totals.get('failed', 0)}*\n"
            f"• ⏱ Timeout: *{totals.get('timed_out', 0)}*\n")
    if not problems:
        return head + stat + "\n_Muammoli invoice yo'q — hammasi joyida._"
    lines = ["", f"⚠️ *Muammoli invoice ({len(problems)}):*"]
    for p in problems:
        num = p.get("invoice_number") or p.get("invoice_id")
        lines.append(
            f"• №{num} — urinish {p.get('attempts', 0)}, "
            f"timeout {p.get('timeouts', 0)}, xato {p.get('failures', 0)}"
        )
        if p.get("last_error"):
            safe = str(p["last_error"]).replace("`", "'")[:120]
            lines.append(f"  ↳ _{safe}_")
    return head + stat + "\n".join(lines)
