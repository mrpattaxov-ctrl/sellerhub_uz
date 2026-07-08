"""Avto-slot QUEUE — DB kirish qatlami (`postavka_grab_plan` jadvali).

Bitta umumiy jadval = barcha user/do'kon avto-slot aktlari. Bu qatlam
allokator va queue-yozish yo'lining YAGONA DB nuqtasi — SQL boshqa joyda
takrorlanmaydi. Issiq yo'l (`candidates_for`, `claim`) bitta indeks-scan
bo'lishi uchun maxsus tuzilgan.

Reja → modul nomi: rejada `queue.py` edi; Python stdlib `queue` bilan
chalkashmaslik uchun `store.py` deb nomlandi (shakl/maqsad o'sha).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select, update, or_

from extensions import SessionLocal
from models import PostavkaGrabPlan

# Bir akt uchun ko'pi bilan necha booking urinish (Uzum `time-slot/set` 3×
# limit / penalty xavfi). Shundan oshgan reja nomzodlardan chiqadi — cheksiz
# set_time_slot otilmaydi.
MAX_ATTEMPTS = 3

# Terminal-failure callback (DI): reja `failed` (terminal) bo'lganda item dict
# bilan chaqiriladi — allokator bunga Telegram ulaydi. Bu qatlam allokatorni
# import qilmaydi (bus/DI chegarasi saqlansin). None = jim.
_on_terminal = None


def set_on_terminal(fn) -> None:
    """Reja terminal `failed` bo'lganda chaqiriladigan callback'ni o'rnatadi."""
    global _on_terminal
    _on_terminal = fn


def _emit_terminal(item: dict) -> None:
    if _on_terminal is None:
        return
    try:
        _on_terminal(item)
    except Exception as e:
        print(f"[autoslot.store] terminal-callback xato plan={item.get('id')}: {e!r}")


def _to_item(r: PostavkaGrabPlan) -> dict:
    """ORM qator → yengil dict (session tashqarisida xavfsiz ishlatish uchun)."""
    return {
        "id": r.id,
        "user_id": r.user_id,
        "shop_uzum_id": r.shop_uzum_id,
        "invoice_id": r.invoice_id,
        "invoice_number": r.invoice_number,
        "volume": r.draft_size,          # akt hajmi (packing uchun)
        "dim_group": r.dim_group,
        "pool_source": r.pool_source,
        "stock_id": r.stock_id,
        "max_date": r.max_date,
        "min_date": r.min_date,
        "enabled_at": r.enabled_at,
        "status": r.status,
        "current_slot_ms": r.current_slot_ms,
        "attempts": r.attempts,
        "priority": r.priority or 0,
        "error": r.error,
    }


def candidates_for(day: date, slot_from_ms: int | None = None) -> list[dict]:
    """ISSIQ YO'L: bo'shagan slotга mos kutayotgan aktlar — navbat tartibida
    (kim oldin yoqsa, oldin).

    Ombor BITTA va slot GLOBAL (Abdulaziz 2026-06-30): bo'shagan slotni —
    hajmi sig'sa — navbatdagi ISTALGAN akt olishi mumkin. Dim/pool bo'yicha
    FILTRLAMAYMIZ. Filtrlar:
      - `day <= max_date` (deadline);
      - `attempts < MAX_ATTEMPTS` (budjet/penalty himoyasi);
      - RE-BOOKING: `current_slot_ms` to'lgan (akt allaqachon slotда) bo'lsa,
        bo'shagan slot UNDAN ERTAROQ bo'lishi shart (`slot_from_ms <
        current_slot_ms`) — faqat «yaxshilash». Slotsiz draft (NULL) — har
        qanday slot mos. `slot_from_ms` berilmasa (eski chaqiruv) bu filtr o'chiq.

    -> [item dict, ...] (enabled_at, id bo'yicha o'sish; eng yuqori prioritet
    birinchi).
    """
    with SessionLocal() as db:
        q = (
            select(PostavkaGrabPlan)
            .where(PostavkaGrabPlan.status == "waiting")
            .where(PostavkaGrabPlan.max_date >= day)
            # PASTKI chegara: min_date to'lgan bo'lsa undan OLDINGI slot rad etiladi.
            .where(or_(PostavkaGrabPlan.min_date.is_(None),
                       PostavkaGrabPlan.min_date <= day))
            .where(PostavkaGrabPlan.attempts < MAX_ATTEMPTS)
        )
        if slot_from_ms is not None:
            # Slotsiz (NULL) YOKI bo'shagan slot hozirgisidan ERTAROQ bo'lsin.
            q = q.where(or_(PostavkaGrabPlan.current_slot_ms.is_(None),
                            PostavkaGrabPlan.current_slot_ms > int(slot_from_ms)))
        rows = db.execute(
            # PRIORITET birinchi (admin qo'ygan, yuqori=oldin), keyin navbat
            # (enabled_at=kim oldin yoqsa), keyin id — teng prioritetlilar
            # «unutilib» qolmasin (FIFO saqlanadi).
            q.order_by(PostavkaGrabPlan.priority.desc(),
                       PostavkaGrabPlan.enabled_at.asc(), PostavkaGrabPlan.id.asc())
        ).scalars().all()
    return [_to_item(r) for r in rows]


# ── Quyidagilar keyingi bosqichlarda to'ldiriladi (shartnoma signaturlari) ──


def enqueue(db, *, user_id: int | None, shop_uzum_id: str, invoice_id: int,
            invoice_number: str | None, volume: int, dim_group: str | None,
            pool_source: str | None, stock_id: int | None, max_date: date,
            min_date: date | None = None,
            enabled_at: datetime | None = None,
            current_slot_ms: int | None = None) -> tuple[int, str]:
    """Avto-slotni yoqish: aktni navbatga qo'shadi/yangilaydi — UPSERT
    (shop, invoice) `waiting` qatori bo'yicha (dubl bo'lmasin).

    `db` — ochiq sessiya (chaqiruvchi commit qiladi). `flush` qilinadi, id
    qaytariladi. Yangilashda `enabled_at` (navbat o'rni) va birlamchi
    `user_id` SAQLANADI — muddatni o'zgartirish navbatda orqaga surmaydi.
    Eski grabber bilan moslik uchun `target_day` ham `max_date`ga teng yoziladi.

    -> (plan_id, "created" | "updated").  [Bosqich 3]
    """
    now = datetime.utcnow()
    existing = db.execute(
        select(PostavkaGrabPlan)
        .where(PostavkaGrabPlan.shop_uzum_id == shop_uzum_id)
        .where(PostavkaGrabPlan.invoice_id == invoice_id)
        .where(PostavkaGrabPlan.status == "waiting")
    ).scalars().first()
    if existing:
        existing.max_date = max_date
        existing.min_date = min_date
        existing.target_day = max_date          # eski grabber compat
        existing.draft_size = volume
        existing.dim_group = dim_group
        existing.pool_source = pool_source
        existing.stock_id = stock_id
        existing.current_slot_ms = current_slot_ms
        if existing.enabled_at is None:          # navbat o'rni saqlansin
            existing.enabled_at = enabled_at or now
        if existing.user_id is None:
            existing.user_id = user_id
        db.flush()
        return existing.id, "updated"
    row = PostavkaGrabPlan(
        user_id=user_id,
        shop_uzum_id=shop_uzum_id,
        invoice_id=invoice_id,
        invoice_number=invoice_number,
        draft_size=volume,
        dim_group=dim_group,
        pool_source=pool_source,
        stock_id=stock_id,
        max_date=max_date,
        min_date=min_date,
        target_day=max_date,                     # eski grabber compat
        enabled_at=enabled_at or now,
        current_slot_ms=current_slot_ms,
        status="waiting",
    )
    db.add(row)
    db.flush()
    return row.id, "created"


def claim(ids: list[int], lease_sec: int = 30) -> list[int]:
    """ATOMIC CLAIM: berilgan id'larni `waiting`→`booking` ga o'tkazadi va
    `lease_until = now()+lease_sec`, `attempts += 1`. FAQAT haqiqatan
    o'zgargan (o'sha lahzada `waiting` bo'lgan) qatorlar id'sini qaytaradi —
    ikki event bir aktni urishtirmasligi KAFOLATI shu (bitta UPDATE,
    RETURNING). Booking faqat qaytarilgan id'lar uchun otiladi.
    """
    ids = [int(i) for i in ids]
    if not ids:
        return []
    lease_until = datetime.utcnow() + timedelta(seconds=int(lease_sec))
    with SessionLocal() as db:
        rows = db.execute(
            update(PostavkaGrabPlan)
            .where(PostavkaGrabPlan.id.in_(ids))
            .where(PostavkaGrabPlan.status == "waiting")
            .values(status="booking", lease_until=lease_until,
                    attempts=PostavkaGrabPlan.attempts + 1)
            .returning(PostavkaGrabPlan.id)
        ).all()
        db.commit()
    return [r[0] for r in rows]


def release_expired(now: datetime | None = None) -> int:
    """Ijara muddati o'tgan `booking` qatorlarni `waiting`ga qaytaradi (band
    qilish o'rtasida halokat bo'lsa, akt qotib qolmasin).

    `attempts` allaqachon tugagan (>= MAX_ATTEMPTS) qator `waiting`ga
    QAYTMAYDI — u nomzod bo'lolmaydi va abadiy qotib qolardi; o'rniga
    terminal `failed` + `_on_terminal` callback. -> `waiting`ga qaytganlar soni.
    """
    now = now or datetime.utcnow()
    dead: list[dict] = []
    released = 0
    with SessionLocal() as db:
        rows = db.execute(
            select(PostavkaGrabPlan)
            .where(PostavkaGrabPlan.status == "booking")
            .where(PostavkaGrabPlan.lease_until.isnot(None))
            .where(PostavkaGrabPlan.lease_until < now)
        ).scalars().all()
        for r in rows:
            r.lease_until = None
            if (r.attempts or 0) >= MAX_ATTEMPTS:
                r.status = "failed"
                r.error = f"booking muddati o'tdi — {MAX_ATTEMPTS} urinish tugadi"
                dead.append(_to_item(r))
            else:
                r.status = "waiting"
                released += 1
        db.commit()
    for it in dead:
        _emit_terminal(it)
    return released


def mark_booked(plan_id: int, slot_ms: int) -> None:
    """Reja band bo'ldi — `booked`, slot va vaqt yoziladi, ijara tozalanadi."""
    with SessionLocal() as db:
        row = db.get(PostavkaGrabPlan, int(plan_id))
        if not row:
            return
        row.status = "booked"
        row.booked_slot_ms = int(slot_ms)
        row.booked_at = datetime.utcnow()
        row.lease_until = None
        row.error = None
        db.commit()


def mark_failed(plan_id: int, error: str, *, requeue: bool = True) -> str | None:
    """Band qilish muvaffaqiyatsiz — `requeue` bo'lsa `waiting`ga qaytaradi
    (keyingi event'da yana urinadi), aks holda `failed` (terminal).

    MUHIM: `requeue=True` bo'lsa ham `attempts >= MAX_ATTEMPTS` bo'lsa reja
    TERMINAL `failed` bo'ladi — aks holda u `waiting`da abadiy qoladi-yu
    `candidates_for` (attempts<MAX) uni boshqa hech qachon ko'rmaydi («qotib
    qolgan» reja). Terminal o'tishda `_on_terminal` callback chaqiriladi
    (foydalanuvchiga Telegram xabar shu orqali). -> yakuniy status.
    """
    with SessionLocal() as db:
        row = db.get(PostavkaGrabPlan, int(plan_id))
        if not row:
            return None
        exhausted = requeue and (row.attempts or 0) >= MAX_ATTEMPTS
        if exhausted:
            error = f"{error} — {MAX_ATTEMPTS} urinish tugadi"
        row.status = "waiting" if (requeue and not exhausted) else "failed"
        row.error = (error or "")[:255]
        row.lease_until = None
        final = row.status
        item = _to_item(row) if final == "failed" else None
        db.commit()
    if item is not None:
        _emit_terminal(item)
    return final


# ── Admin (faqat operator) — prioritet boshqaruvi va navbat ko'rinishi ──


def set_priority(plan_id: int, priority: int) -> int | None:
    """ADMIN: rejaning navbat prioritetini o'rnatadi (yuqori = oldin). Faqat
    `waiting`/`booking` rejaga ma'noli (booked/failed'га ta'sirsiz, lekin
    yozamiz). -> yangi priority, yoki reja topilmasa None."""
    with SessionLocal() as db:
        row = db.get(PostavkaGrabPlan, int(plan_id))
        if not row:
            return None
        row.priority = int(priority)
        val = row.priority
        db.commit()
    return val


def list_for_admin(status: str | None = None, limit: int = 500) -> list[dict]:
    """ADMIN: GLOBAL navbat — barcha do'kon/userlarning avto-slot rejalari,
    navbat tartibida (prioritet↓, enabled_at↑). `status` berilsa filtr; aks
    holda faol navbat (waiting+booking) birinchi, keyin qolganlari.

    Faqat o'qish (panel jadvali uchun). -> [item dict + created_at, ...].
    """
    with SessionLocal() as db:
        q = select(PostavkaGrabPlan)
        if status:
            q = q.where(PostavkaGrabPlan.status == status)
        rows = db.execute(
            q.order_by(PostavkaGrabPlan.priority.desc(),
                       PostavkaGrabPlan.enabled_at.asc(),
                       PostavkaGrabPlan.id.asc())
        ).scalars().all()
    out = []
    for r in rows[:int(limit)]:
        it = _to_item(r)
        it["shop_uzum_id"] = r.shop_uzum_id
        it["booked_slot_ms"] = r.booked_slot_ms
        it["enabled_at"] = r.enabled_at.isoformat() if r.enabled_at else None
        it["max_date"] = r.max_date.isoformat() if r.max_date else None
        it["min_date"] = r.min_date.isoformat() if r.min_date else None
        out.append(it)
    return out
