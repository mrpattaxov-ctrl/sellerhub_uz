"""Postavka slot-kuzatuvi (Faza 0 — premissa sinovi, READ-ONLY).

Maqsad bitta savolga isbot yig'ish: «Uzum FBO slotlari vaqt o'tib o'zi
bo'shaydimi?». Agar bo'shasa — keyin maqsad-kun + avto-rebooking quramiz;
bo'shamasa — ortiqcha mehnat va 3-martalik `set` budjeti behuda ketmaydi.

Ishlash: worker har N daqiqada bir nechta tovar-miqdori uchun `time-slot`
(akt-siz) so'rovini o'qiydi va har o'lchovni `postavka_slot_watch` jadvaliga
yozadi. Telegramга soatda bir batafsil xulosa ketadi (app.py loop'i orqali).

⚠️ HECH NARSA yaratmaydi/o'zgartirmaydi. `time-slot` faqat-o'qish so'rovi —
3-martalik o'zgartirish budjetiga TEGMAYDI.

Yadro (measure / format_digest) — TOZA funksiyalar (DB/tarmoq yo'q),
shu sababli mock test bilan to'liq qoplanadi. poll_once / build_digest_text
— DB yelimi.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone, timedelta

from sqlalchemy import select

from extensions import SessionLocal
from models import PostavkaSlotWatch
from postavki import client


def _load_quantities() -> list[int]:
    """Kuzatiladigan tovar-miqdorlari. Env `POSTAVKA_SLOT_WATCH_QUANTITIES`
    (vergul bilan) bilan moslanadi; bo'sh bo'lsa 4 ta default (kichik/o'rta/
    katta/eng katta spread) — har poll = shu sondagi time-slot o'qishi, shuning
    uchun kam soni intervalni qisqartirishga (masalan 1 daq) imkon beradi."""
    raw = os.environ.get("POSTAVKA_SLOT_WATCH_QUANTITIES", "").strip()
    if raw:
        try:
            vals = [int(x) for x in raw.split(",") if x.strip()]
            if vals:
                return vals
        except Exception:
            pass
    return [150, 500, 1000]


# Sinov uchun kuzatiladigan tovar-miqdorlari (bir SKU, faqat miqdor o'zgaradi —
# shunda «miqdorga bog'liqlik» va «vaqt bo'yicha o'zgarish» ajratib o'lchanadi).
QUANTITIES = _load_quantities()

# Toshkent vaqti — O'zbekistonda yozги vaqt yo'q, doimiy UTC+5.
_TZ = timezone(timedelta(hours=5))

_UZ_MONTHS = {
    1: "yanvar", 2: "fevral", 3: "mart", 4: "aprel", 5: "may", 6: "iyun",
    7: "iyul", 8: "avgust", 9: "sentabr", 10: "oktabr", 11: "noyabr", 12: "dekabr",
}

# Hafta kunlari (datetime.weekday(): 0=dushanba).
_UZ_WD = {0: "dush", 1: "sesh", 2: "chor", 3: "pay", 4: "jum", 5: "shan", 6: "yak"}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, _TZ)


def _fmt_date(ms: int) -> str:
    d = _dt(ms)
    return f"{d.day}-{_UZ_MONTHS[d.month]}"


def _fmt_time(ms: int) -> str:
    return _dt(ms).strftime("%H:%M")


def _fmt_date_wd(ms: int) -> str:
    d = _dt(ms)
    return f"{d.day}-{_UZ_MONTHS[d.month]} ({_UZ_WD[d.weekday()]})"


def _when_local(dt_utc, now_ms: int) -> str:
    """Eng yaxshi slot QACHON ko'rilganini Toshkent vaqtida qisqa yozadi.
    Bugun bo'lsa «HH:MM», boshqa kun bo'lsa «DD/HH:MM»."""
    local = dt_utc.replace(tzinfo=timezone.utc).astimezone(_TZ)
    if local.date() == _dt(now_ms).date():
        return local.strftime("%H:%M")
    return local.strftime("%d/%H:%M")


# ── Yadro: o'lchov (TOZA — get_slots_fn orqali tarmoq inʼektsiya qilinadi) ──


def measure(sku_line_base: dict, quantities: list[int], get_slots_fn) -> list[dict]:
    """Har miqdor uchun bo'sh slotlarni o'lchaydi.

    sku_line_base: {"skuId":.., "purchasePrice":..} (miqdorsiz asos)
    get_slots_fn(sku_lines) -> [{"timeFrom":ms, "timeTo":ms}, ...]

    -> [{"quantity":q, "earliest_ms":int|None, "count":int, "slots":[ms,...]}]
       `slots` — eng erta 24 ta bo'sh slot boshlanish vaqti (xulosa uchun).
       So'rov muvaffaqiyatsiz/bo'sh bo'lsa earliest_ms=None, count=0.
    """
    out: list[dict] = []
    for q in quantities:
        line = {**sku_line_base, "quantityToStock": int(q)}
        error = False
        error_msg = None
        try:
            slots = get_slots_fn([line]) or []
        except Exception as ex:
            slots = []
            error = True  # so'rov RAD ETILDI — «slot yo'q» bilan adashtirma
            # Xato matnini saqlaymiz (429/ban'ni Telegramga qisqa yuborish uchun).
            error_msg = f"{type(ex).__name__}: {ex}"[:120]
            print(f"[SlotWatch] q={q} time-slot xato: {ex!r}"[:200])
        froms = sorted(
            int(s["timeFrom"]) for s in slots if isinstance(s, dict) and s.get("timeFrom")
        )
        out.append({
            "quantity": int(q),
            "earliest_ms": froms[0] if froms else None,
            "count": len(froms),
            "slots": froms[:24],
            "error": error,
            "error_msg": error_msg,
        })
    return out


# ── Yadro: Telegram xulosasi (TOZA — «Hozir + Eng yaxshi» jadval uslubi) ──


def format_digest(
    shop_name: str,
    measurements: list[dict],
    now_ms: int,
) -> str:
    """Soatlik xulosa — har miqdor uchun HOZIRGI va kuzatuv davomidagi ENG
    YAXSHI (eng erta) slot (Telegram monospace).

    Har measurement: {quantity, earliest_ms (hozir), error,
    best_ms (eng erta ko'rilgan|None), best_when (qachon, mahalliy str)}.
    «eng yaxshi» = vaqtinchalik ochilib yo'qolgan slot ham yo'qolmaydi.
    """
    safe_name = "".join(c for c in str(shop_name) if c not in "*_`[")
    header = f"{'dona':>5}  {'hozir':<15} {'eng yaxshi':<15} topildi"
    rows = [header]
    for m in measurements:
        q = m["quantity"]
        if m.get("error"):
            hozir = "⚠️ so'rov xato"
        elif m["earliest_ms"] is None:
            hozir = "bo'sh yo'q"
        else:
            hozir = _fmt_date_wd(m["earliest_ms"])
        best = m.get("best_ms")
        best_s = _fmt_date_wd(best) if best is not None else "—"
        when_s = m.get("best_when") or ""
        rows.append(f"{q:>5}  {hozir:<15} {best_s:<15} {when_s}")
    table = "```\n" + "\n".join(rows) + "\n```"
    return (
        f"📊 *{safe_name} — slot kuzatuvi*\n"
        f"_{_fmt_date(now_ms)} {_fmt_time(now_ms)} · faqat-kuzatuv_\n\n"
        f"{table}\n"
        f"_«eng yaxshi» = kuzatuv davomida ko'rilgan eng erta slot._\n"
        f"_Read-only — 3x budjetga tegmaydi._"
    )


# ── DB yelimi: bitta o'lchov sikli ──────────────────────────────────


def _pick_sku_line(shop_uzum_id: str):
    """Do'kondan bitta real SKU tanlaydi (eng ko'p zaxiralisini — miqdor
    cheklovidan qochish uchun). -> (line_base, sku_dict) yoki (None, None).
    """
    try:
        skus = client.restock_skus(shop_uzum_id, page=0, size=50)
    except Exception as e:
        print(f"[SlotWatch] restock_skus xato shop={shop_uzum_id}: {e!r}")
        return None, None
    best = None
    for s in skus:
        if not s.get("skuId"):
            continue
        if best is None or (s.get("availableToStock") or 0) > (best.get("availableToStock") or 0):
            best = s
    if not best:
        return None, None
    line = {
        "skuId": int(best["skuId"]),
        "purchasePrice": int(best.get("purchasePrice") or 0),
    }
    return line, best


# SKU+ombor har poll'da o'zgarmaydi — bir marta olib keshlaymiz (bekorga
# takrorlanadigan 2 so'rovni olib tashlaydi: 12 → 10 so'rov/poll). TTL o'tgach
# yoki SKU yaroqsiz bo'lganда qayta tanlanadi.
_SKU_CACHE: dict[str, dict] = {}
_SKU_CACHE_TTL_MS = 6 * 60 * 60 * 1000  # 6 soat


def _resolve_pool(shop_uzum_id: str, sku_id: int) -> str:
    try:
        stocks = client.resolve_stocks(shop_uzum_id, [sku_id])
        return (stocks[0].get("poolSource") if stocks else None) or "FULLFILMENT"
    except Exception:
        return "FULLFILMENT"


def _get_sku_context(shop_uzum_id: str, force: bool = False):
    """Keshlangan (base_line, dim_group, pool_source). TTL ichида qayta
    so'ramaydi. -> (None, None, None) agar SKU topilmasa."""
    key = str(shop_uzum_id)
    cached = _SKU_CACHE.get(key)
    if cached and not force and (_now_ms() - cached["ts"]) < _SKU_CACHE_TTL_MS:
        return cached["base"], cached["dim"], cached["pool"]

    base, sku = _pick_sku_line(shop_uzum_id)
    if not base:
        return None, None, None
    dim = (sku or {}).get("dimensionalGroup")
    # dimensionalGroup ba'zan dict ({"group":"SMALL","title":"МГТ"}) keladi —
    # String ustunга faqat guruh kodini yozamiz.
    if isinstance(dim, dict):
        dim = dim.get("group") or dim.get("title")
    dim = str(dim) if dim is not None else None
    pool = _resolve_pool(shop_uzum_id, base["skuId"])
    _SKU_CACHE[key] = {"base": base, "dim": dim, "pool": pool, "ts": _now_ms()}
    return base, dim, pool


def poll_once(shop_uzum_id: str) -> tuple[int, str | None]:
    """Bitta do'kon uchun barcha miqdorlarni o'lchab DB'ga yozadi.

    -> (yozilgan qatorlar soni, xato_xulosasi|None). Xato_xulosasi — 429/ban/
    boshqa rad bo'lsa qisqa matn (Telegram signali uchun), aks holda None.
    Hech narsa yaratmaydi/o'zgartirmaydi. SKU+ombor keshlanadi.
    """
    base, dim, pool = _get_sku_context(shop_uzum_id)
    if not base:
        print(f"[SlotWatch] shop={shop_uzum_id} — SKU topilmadi, o'tkazib yuborildi")
        return 0, "SKU topilmadi (token/ombor muammosi?)"

    def _get(lines):
        return client.get_time_slots(shop_uzum_id, lines, pool)

    results = measure(base, QUANTITIES, _get)
    n_err = sum(1 for m in results if m.get("error"))
    err_summary = None
    if n_err:
        # Xato bo'lsa kesh keyingi poll'da yangi SKU/token bilan qayta urinsin.
        _SKU_CACHE.pop(str(shop_uzum_id), None)
        _msgs = [m.get("error_msg") for m in results if m.get("error") and m.get("error_msg")]
        err_summary = f"{n_err}/{len(results)} so'rov rad: {_msgs[0] if _msgs else 'nomaʼlum'}"
        print(f"[SlotWatch] shop={shop_uzum_id} — {n_err}/{len(results)} so'rov RAD ETILDI (slot yo'q emas)")
    stamp = datetime.utcnow()  # bitta poll = bitta measured_at (guruhlash uchun)
    with SessionLocal() as db:
        for m in results:
            db.add(PostavkaSlotWatch(
                measured_at=stamp,
                shop_uzum_id=str(shop_uzum_id),
                sku_id=base["skuId"],
                dim_group=dim,
                pool_source=pool,
                quantity=m["quantity"],
                earliest_slot_ms=m["earliest_ms"],
                # slot_count = -1 → SO'ROV RAD ETILDI (real «0 slot» bilan farqlash uchun)
                slot_count=(-1 if m.get("error") else m["count"]),
                slots_json=json.dumps(m["slots"]),
            ))
        db.commit()
    return len(results), err_summary


def build_digest_text(shop_uzum_id: str, shop_name: str) -> str | None:
    """Soatlik xulosa matnini yasaydi.

    Har miqdor uchun HOZIRGI (eng oxirgi o'lchov) va kuzatuv davomidagi ENG
    YAXSHI (eng erta ko'rilgan) slotni ko'rsatadi — vaqtinchalik ochilib
    yo'qolgan slot ham e'tibordan chetda qolmaydi. So'rov rad etilgan qatorlar
    (slot_count<0) «eng yaxshi»ga kirmaydi. -> Markdown matn yoki None.
    """
    with SessionLocal() as db:
        all_rows = db.execute(
            select(PostavkaSlotWatch)
            .where(PostavkaSlotWatch.shop_uzum_id == str(shop_uzum_id))
            .order_by(PostavkaSlotWatch.measured_at.asc())
        ).scalars().all()

    if not all_rows:
        return None
    latest_t = all_rows[-1].measured_at
    latest_by_q: dict[int, PostavkaSlotWatch] = {}
    best_ms: dict[int, int] = {}
    best_when: dict[int, object] = {}
    for r in all_rows:
        if r.measured_at == latest_t:
            latest_by_q[r.quantity] = r
        # «eng yaxshi» = eng erta (eng kichik) slot; xato qatorlar hisobga olinmaydi.
        if r.slot_count >= 0 and r.earliest_slot_ms is not None:
            if r.quantity not in best_ms or r.earliest_slot_ms < best_ms[r.quantity]:
                best_ms[r.quantity] = r.earliest_slot_ms
                best_when[r.quantity] = r.measured_at

    now_ms = _now_ms()
    measurements = []
    for q in QUANTITIES:
        r = latest_by_q.get(q)
        if r is None:
            continue
        bm = best_ms.get(q)
        measurements.append({
            "quantity": r.quantity,
            "earliest_ms": r.earliest_slot_ms,
            "error": r.slot_count < 0,  # so'rov rad etilgan
            "best_ms": bm,
            "best_when": _when_local(best_when[q], now_ms) if bm is not None else "",
        })
    if not measurements:
        return None
    return format_digest(shop_name, measurements, now_ms)
