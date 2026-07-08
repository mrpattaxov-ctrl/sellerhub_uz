"""Postavka slot-grabber (Faza 2D — накладной-bo'yicha avto-band).

Yondashuv (Abdulaziz 2026-06-26): tasodifiy SKU + «narvon» bilan sig'imni
TAXMIN qilish O'RNIGA — har bir navbatdagi (waiting) draft накладнойning O'Z
bo'sh slotlarini to'g'ridan-to'g'ri so'raymiz (`time-slot/get`). Накладной qaysi
slotni ko'rsa — o'lcham-guruh (dim) va hajm AVTOMATIK to'g'ri (Uzum faqat shu
накладнойга yaroqli slotlarni qaytaradi). Maqsad-kunда slot chiqsa — darhol
band qilamiz (`set_time_slot`).

Hamma do'kon rejalari bitta navbatда — grab-loop ularni do'kondan qat'i nazar
ko'rib chiqadi.

⚠️ Rejim (env `POSTAVKA_SLOT_GRAB_MODE`):
  - "dry"  (DEFAULT) — band qilmaydi, faqat «band qilardik»ni loglaydi.
  - "live" — REAL band qiladi (`set_time_slot`, mutatsion → 3× budjet).
  - "off"  — butunlay o'chiq.

Yadro (`pick_slot_for_day`) — TOZA funksiya (DB/tarmoq yo'q), pytest bilan
qoplangan. Qolgani — portal/DB yelimi.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta, date

from sqlalchemy import select

from extensions import SessionLocal
from models import PostavkaGrabPlan
from postavki import client

# Toshkent vaqti — doimiy UTC+5 (slot kunini hisoblash uchun).
_TZ = timezone(timedelta(hours=5))


def _day_key(ms: int) -> date:
    """Slot boshlanishi (epoch ms) → Toshkent kuni (date)."""
    return datetime.fromtimestamp(int(ms) / 1000, _TZ).date()


# ── Yadro: maqsad-kunдagi slotni tanlash (TOZA) ──────────────────────


def pick_slot_for_day(slots: list[dict], target_day: date) -> int | None:
    """Накладнойning bo'sh slotlari ichidan MAQSAD-KUNдagi eng erta slotni
    tanlaydi.

    slots: [{"timeFrom":ms, "timeTo":ms}, ...] — накладнойning o'z slotlari
           (Uzum faqat unга yaroqlilarni qaytaradi → dim/hajm avtomatik mos).
    target_day: tanlangan kun (Toshkent).
    -> eng erta mos slot timeFrom (ms) yoki None (o'sha kunда slot yo'q).
    """
    best = None
    for s in slots:
        tf = s.get("timeFrom") if isinstance(s, dict) else None
        if not tf:
            continue
        tf = int(tf)
        if _day_key(tf) == target_day and (best is None or tf < best):
            best = tf
    return best


# ── DB yelimi: rejalar ───────────────────────────────────────────────


def _all_waiting_plans() -> list[dict]:
    """Barcha do'kon bo'yicha `waiting` rejalar (grab-loop uchun, cross-shop)."""
    with SessionLocal() as db:
        rows = db.execute(
            select(PostavkaGrabPlan)
            .where(PostavkaGrabPlan.status == "waiting")
            .order_by(PostavkaGrabPlan.target_day, PostavkaGrabPlan.id)
        ).scalars().all()
    return [{
        "id": r.id,
        "shop_uzum_id": r.shop_uzum_id,
        "invoice_id": r.invoice_id,
        "invoice_number": r.invoice_number,
        "draft_size": r.draft_size,
        "target_day": r.target_day,
        "pool_source": r.pool_source,
        "stock_id": r.stock_id,
        "dim_group": r.dim_group,
    } for r in rows]


def _mark_plan(plan_id: int, status: str, *, slot_ms: int | None = None, error: str | None = None) -> None:
    """Reja holatini yangilaydi (booked/failed)."""
    with SessionLocal() as db:
        row = db.get(PostavkaGrabPlan, int(plan_id))
        if not row:
            return
        row.status = status
        if slot_ms is not None:
            row.booked_slot_ms = int(slot_ms)
        if error is not None:
            row.error = error[:255]
        if status == "booked":
            row.booked_at = datetime.utcnow()
        db.commit()


# ── Draft ma'lumotini olish (reja yaratishda ishlatiladi) ────────────


def list_draft_invoices(shop_uzum_id: str) -> list[dict]:
    """Band qilishga tayyor draft'lar: slotsiz (timeSlotReservation yo'q) +
    CREATED holatidagi накладные. -> [inv, ...] (list_invoices elementlari).

    statuses="CREATED" portal filtri ba'zan to'liq qaytarmagani uchun default
    (statuses bo'sh) bilan olib, client-side filtrlaymiz (barqarorroq)."""
    invs = client.list_invoices(shop_uzum_id, page=0, size=100, statuses="")
    out = []
    for inv in invs:
        if inv.get("timeSlotReservation"):
            continue  # allaqachon slotga ega
        if ((inv.get("invoiceStatus") or {}).get("value")) != "CREATED":
            continue
        out.append(inv)
    return out


def list_autoslot_invoices(shop_uzum_id: str) -> list[dict]:
    """Avto-slotга MOS aktlar: CREATED holatdagi BARCHA накладные — slotsiz
    draft (yangi slot oladi) HAM, allaqachon slotli (re-booking: ertaroq slot
    bo'shasa ko'chiriladi) HAM. ACCEPTED/boshqa holat chiqmaydi (qabul qilingan
    aktni qayta slot qilib bo'lmaydi). -> [inv, ...]."""
    invs = client.list_invoices(shop_uzum_id, page=0, size=100, statuses="")
    return [inv for inv in invs
            if ((inv.get("invoiceStatus") or {}).get("value")) == "CREATED"]


# ── Bitta rejani ishlash + butun siklni aylantirish ──────────────────


def process_plan(plan: dict, *, mode: str = "dry") -> dict:
    """Bitta waiting reja: накладнойning o'z slotlarini olib, maqsad-kunда slot
    bo'lsa band qiladi (live) yoki loglaydi (dry). Exception otmaydi."""
    shop = plan["shop_uzum_id"]
    inv_id = plan["invoice_id"]
    pool = plan.get("pool_source") or "FULLFILMENT"
    target = plan["target_day"]

    try:
        slots = client.invoice_time_slots(shop, inv_id, pool)
    except Exception as e:
        print(f"[SlotGrab] shop={shop} draft#{inv_id} time-slot xato: {e!r}")
        return {"matched": False, "error": str(e)}

    slot_tf = pick_slot_for_day(slots or [], target)
    if not slot_tf:
        return {"matched": False, "day": str(target), "slots_seen": len(slots or [])}

    size = plan.get("draft_size")
    if mode != "live":
        print(f"[SlotGrab] DRY — band qilardik: shop={shop} draft#{inv_id} (№{plan.get('invoice_number')}, "
              f"hajm {size}) → kun {target} slot {slot_tf}")
        return {"matched": True, "dry": True, "plan_id": plan["id"], "invoice_id": inv_id, "slot_from_ms": slot_tf}

    # ── LIVE — REAL band qilish (mutatsion, 3× budjet sarflaydi) ──
    stock_id = plan.get("stock_id")
    if not stock_id:
        _mark_plan(plan["id"], "failed", error="stockId yo'q")
        return {"matched": True, "error": "stockId yo'q", "invoice_id": inv_id}
    try:
        inv = client.set_time_slot(shop, inv_id, int(slot_tf), int(stock_id), pool)
    except Exception as e:
        _mark_plan(plan["id"], "failed", error=str(e))
        print(f"[SlotGrab] LIVE band XATO shop={shop} draft#{inv_id}: {e!r}")
        return {"matched": True, "dry": False, "booked": False, "error": str(e), "invoice_id": inv_id}
    ok = bool(inv and inv.get("id"))
    _mark_plan(plan["id"], "booked" if ok else "failed", slot_ms=slot_tf if ok else None,
               error=None if ok else "javob bo'sh")
    print(f"[SlotGrab] LIVE band {'✅OK' if ok else '❌FAIL'} shop={shop} draft#{inv_id} "
          f"(№{plan.get('invoice_number')}) → kun {target} slot {slot_tf}")
    return {"matched": True, "dry": False, "booked": ok, "invoice_id": inv_id}


def run_grab_cycle(mode: str | None = None) -> int:
    """Bitta sikl: barcha waiting rejalarni (hamma do'kon) ko'rib chiqadi.
    -> mos slot topilgan (band qilingan/qilinardi) rejalar soni."""
    m = mode or grab_mode()
    if m == "off":
        return 0
    plans = _all_waiting_plans()
    matched = 0
    for p in plans:
        try:
            if process_plan(p, mode=m).get("matched"):
                matched += 1
        except Exception as e:
            print(f"[SlotGrab] reja#{p.get('id')} xato: {e!r}")
    return matched


def grab_mode() -> str:
    """Joriy rejim (env `POSTAVKA_SLOT_GRAB_MODE`): off/dry/live.

    DEFAULT **live** (Abdulaziz 2026-06-29 — global avto-band yoqildi): har reja
    maqsad-kunда slot bo'shashi bilan o'zi band qiladi. Bitta-marta sinov
    tasdiqlangach yoqildi (set_time_slot slotsiz draftда ishlaydi). To'xtatish:
    env `POSTAVKA_SLOT_GRAB_MODE=dry` (faqat logla) yoki `=off` / `POSTAVKA_GRAB_LOOP=0`.
    """
    m = os.environ.get("POSTAVKA_SLOT_GRAB_MODE", "live").strip().lower()
    return m if m in ("off", "dry", "live") else "live"
