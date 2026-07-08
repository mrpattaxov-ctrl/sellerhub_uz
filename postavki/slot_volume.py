"""Postavka slot-HAJM analitikasi (Faza 0b — READ-ONLY).

Maqsad: har timeslotning bo'sh HAJMINI (sig'imini) vaqt bo'yicha kuzatib,
qachon va qancha hajm bo'shashini «event» sifatida yozish — keyin «slotlar
qachon/qancha bo'shaydi?» degan naqshni bilish uchun.

Uzum slot sig'imini TO'G'RIDAN-TO'G'RI bermaydi — uni «narvon» (probe) bilan
o'lchaymiz: bir nechta miqdor so'rab, har slotning hajm-braketini topamiz
(chiqqan eng katta miqdor = sig'im lower-bound).

Logika (har 1 daqiqa):
  1. Narvon bo'yicha so'rov → har slot uchun hajm-bracket (snapshot).
  2. Oldingi snapshot bilan solishtir.
  3. Hajm OSHGAN slot → event (delta). O'zgarmasa/kamaysa → hech narsa (dedup).
  4. Eventni `postavka_volume_event`ga yoz.

Hisobot (har 30 daqiqa): 7 kun bo'yicha cumulative (kun davomida yig'iladi):
  kun | jami hajm | +30daq | jami slot | hozir bo'sh slot.

⚠️ HECH NARSA yaratmaydi/bron qilmaydi. Faqat time-slot o'qiydi.

Yadro (measure_volumes / diff_snapshots / format_volume_digest) — TOZA
funksiyalar (DB/tarmoq yo'q), mock test bilan qoplanadi.
"""
from __future__ import annotations

import os
import time
import threading
from datetime import datetime, timezone, timedelta, date

import requests
from sqlalchemy import select

from extensions import SessionLocal
from models import PostavkaVolumeEvent
from postavki import client, slot_watch


class _StragglerSkip(Exception):
    """Bir narvon-so'rov 600ms cutoff'dan oshdi (yoki tranzient tarmoq xatosi).
    Bu XATO EMAS — o'sha siklni JIMGINA o'tkazib yuboramiz (snapshot yangilanmaydi,
    soxta event yo'q, cooldown/alert yo'q). Keyingi sikl (300ms keyin) davom etadi."""

_TZ = timezone(timedelta(hours=5))
_UZ_MONTHS = slot_watch._UZ_MONTHS


def _load_ladder() -> list[int]:
    """Hajm narvoni. Env `POSTAVKA_VOLUME_LADDER` (vergul bilan) bilan
    moslanadi; bo'sh bo'lsa default [150,500,750,1500]. Har pog'ona =
    +1 so'rov/daqiqa. Saralangan (o'sish bo'yicha) bo'lishi shart."""
    raw = os.environ.get("POSTAVKA_VOLUME_LADDER", "").strip()
    if raw:
        try:
            v = sorted({int(x) for x in raw.split(",") if x.strip()})
            if v:
                return v
        except Exception:
            pass
    # Abdulaziz, 2026-06-26: sig'imni aniqroq o'lchash uchun narvon kengaytirildi
    # (4 → 15 pog'ona). ⚠️ Har pog'ona = +1 time-slot so'rovi/poll — interval bilan
    # birga portal rate-limit'ini hisobga ol (log: grep RATELIMIT / BURST).
    return [50, 100, 200, 300, 500, 600, 700, 800, 900, 1000, 1200, 1400, 1600, 1800, 2000]


LADDER = _load_ladder()


# ── Yadro: hajm o'lchovi (TOZA) ──────────────────────────────────────


def measure_volumes(base_line: dict, ladder: list[int], get_slots_fn) -> dict:
    """Har slot uchun hajm-bracket snapshot.

    get_slots_fn(quantity) -> [{"timeFrom":ms,"timeTo":ms}, ...].
    -> {slot_from_ms: {"vol": int, "to": int}}; vol = slot chiqqan eng katta
    narvon-qiymati (sig'im lower-bound). Narvon o'sish bo'yicha — kichik
    miqdorda chiqqan slot kattaroqlarda ham bo'lishi mumkin, vol oxirgi
    (eng katta) chiqqan miqdorga teng bo'ladi.
    """
    snap: dict[int, dict] = {}
    for q in sorted(ladder):
        slots = get_slots_fn(q) or []
        for s in slots:
            sf = s.get("timeFrom") if isinstance(s, dict) else None
            if not sf:
                continue
            sf = int(sf)
            cur = snap.get(sf)
            if cur is None or q > cur["vol"]:
                snap[sf] = {"vol": int(q), "to": int(s.get("timeTo") or 0)}
    return snap


def diff_snapshots(prev: dict, curr: dict) -> list[dict]:
    """Hajmi OSHGAN slotlar uchun eventlar.

    -> [{slot_from, slot_to, freed (delta), total (yangi vol)}].
    Faqat oshish (yangi paydo bo'lish ham — old=0). Kamayish/yo'qolish →
    hech narsa. O'zgarmasa → hech narsa (DEDUP: bir xil hajm necha daqiqa
    tursa ham faqat ko'tarilgan lahzada bir marta yoziladi).
    """
    events: list[dict] = []
    for sf, info in curr.items():
        new_vol = info["vol"]
        old = prev.get(sf)
        old_vol = old["vol"] if old else 0
        if new_vol > old_vol:
            events.append({
                "slot_from": sf,
                "slot_to": info.get("to") or 0,
                "freed": new_vol - old_vol,
                "total": new_vol,
            })
    return events


def is_unreliable_drop(prev: dict, curr: dict) -> bool:
    """Joriy o'lchov oldingidan KESKIN kichikmi (tranzient bo'sh/qisman javob)?

    True bo'lsa — bu deyarli har doim Uzum/tarmoq hiccup'i (real emas): bir
    daqiqada barcha kunlar bo'yicha slotlarning ~yarmi yo'qolishi mumkin emas.
    Bunday o'lchovni e'tiborsiz qoldirib, oldingi to'liq snapshotni saqlash
    kerak — aks holda keyingi to'liq poll qaytganda hamma slot "yangi bo'shadi"
    deb soxta SPIKE (masalan 195) chiqadi. Faqat oldingi holat yetarlicha katta
    (>=10 slot) bo'lganda qo'llanadi; aks holda oddiy o'sish/kamayish.
    """
    return bool(prev) and len(prev) >= 10 and len(curr) < len(prev) * 0.6


# ── Yadro: hisobot formati (TOZA — design C) ─────────────────────────


def _num(n: int) -> str:
    return f"{int(n):,}".replace(",", " ")


def _fmt_day(d: date) -> str:
    return f"{d.day}-{_UZ_MONTHS[d.month]}"


def format_volume_digest(shop_name: str, now_ms: int, rows: list[dict]) -> str:
    """Design C — kun | hajm | +30daq | slot | hozir (Telegram monospace).

    rows: [{day:str, hajm:int, d30:int, slot:int, hozir:int}, ...] (saralangan).
    """
    safe = "".join(c for c in str(shop_name) if c not in "*_`[")
    dt = datetime.fromtimestamp(now_ms / 1000, _TZ)
    header = f"{'kun':<8}{'hajm':>7}{'+30daq':>8}{'slot':>5}{'hozir':>6}"
    lines = [header]
    th = td = ts = thz = 0
    for r in rows:
        d30 = "0" if r["d30"] == 0 else f"+{_num(r['d30'])}"
        lines.append(
            f"{r['day']:<8}{_num(r['hajm']):>7}{d30:>8}{r['slot']:>5}{r['hozir']:>6}"
        )
        th += r["hajm"]; td += r["d30"]; ts += r["slot"]; thz += r["hozir"]
    d30t = "0" if td == 0 else f"+{_num(td)}"
    lines.append("─" * 34)
    lines.append(f"{'JAMI':<8}{_num(th):>7}{d30t:>8}{ts:>5}{thz:>6}")
    table = "```\n" + "\n".join(lines) + "\n```"
    return (
        f"📦 *{safe} — slot bo'shashi*\n"
        f"_{dt.day}-{_UZ_MONTHS[dt.month]} {dt.strftime('%H:%M')} · bugun jami · faqat-analitika_\n\n"
        f"{table}\n"
        f"_hajm=bugun jami bo'shagan · +30daq=oxirgi 30 daq · "
        f"slot=jami bo'shagan slot soni · hozir=hozir bo'sh slot_"
    )


# ── DB yelimi: snapshot/diff/yozuv + hisobot ─────────────────────────


# shop -> oxirgi snapshot ({slot_from: {"vol","to"}}). Difflash + «hozir»
# hisoblash uchun xotirada saqlanadi (restartda yo'qoladi — birinchi poll
# faqat seed bo'ladi, soxta event chiqmaydi).
_SNAPSHOT: dict[str, dict] = {}


def poll_volume_once(shop_uzum_id: str, on_events=None) -> tuple[int, str | None]:
    """Bitta narvon-probe → snapshot → oldingi bilan diff → eventlarni yoz.

    on_events: ixtiyoriy callback `on_events(events, pool, dim)` — DB'ga
    yozilgandan keyin bo'shash-eventlari bilan chaqiriladi (avto-slot
    monitori shu orqali `bus`'ga publish qiladi). DI bilan: slot_volume
    autoslot'ni IMPORT QILMAYDI — chegara toza qoladi. None = eski xulq.

    -> (yangi event soni, xato_xulosasi|None). Restartdan keyingi birinchi
    poll faqat boshlang'ich holatni oladi (event yozmaydi).
    """
    base, dim, pool = slot_watch._get_sku_context(shop_uzum_id)
    if not base:
        return 0, "SKU topilmadi (token/ombor muammosi?)"

    def _get(q):
        return client.get_time_slots(shop_uzum_id, [{**base, "quantityToStock": int(q)}], pool)

    try:
        curr = measure_volumes(base, LADDER, _get)
    except Exception as ex:
        slot_watch._SKU_CACHE.pop(str(shop_uzum_id), None)
        return 0, f"{type(ex).__name__}: {ex}"[:120]

    key = str(shop_uzum_id)
    prev = _SNAPSHOT.get(key)

    # Tranzient bo'sh/qisman javob himoyasi: o'lchov oldingidan keskin kichik
    # bo'lsa — snapshotni YANGILAMAYMIZ va event yozmaymiz (soxta spike'ni
    # oldini olish). Eski to'liq holat saqlanadi, keyingi sog'lom poll diffni
    # to'g'ri qiladi. Alert chiqmasligi uchun err=None (faqat log).
    if is_unreliable_drop(prev, curr):
        print(f"[SlotVolume] ishonchsiz o'lchov: {len(curr)}/{len(prev)} slot — e'tiborsiz, snapshot saqlandi")
        return 0, None

    _SNAPSHOT[key] = curr
    if prev is None:
        return 0, None  # seed — soxta to'lqin yo'q

    events = diff_snapshots(prev, curr)
    _persist_events(key, events, pool, dim, on_events)
    return len(events), None


def _persist_events(key: str, events: list[dict], pool, dim, on_events) -> None:
    """Bo'shash-eventlarini DB'ga yozadi + `on_events` publish qiladi.
    `poll_volume_once` va `poll_fast_once` umumiy yelimi (DRY)."""
    if not events:
        return
    stamp = datetime.utcnow()
    with SessionLocal() as db:
        for e in events:
            db.add(PostavkaVolumeEvent(
                detected_at=stamp,
                shop_uzum_id=key,
                slot_from_ms=e["slot_from"],
                slot_to_ms=e["slot_to"],
                freed_volume=e["freed"],
                total_volume=e["total"],
            ))
        db.commit()
    # Avto-slot monitori: bo'shashni `bus`'ga uzatadi (publish xatosi
    # analitikani buzmasin — alohida try).
    if on_events:
        try:
            on_events(events, pool, dim)
        except Exception as _pe:
            print(f"[SlotVolume] on_events publish xato: {_pe!r}")


# ── POYGA RADARI (Abdulaziz 2026-07-06): tez detekt + parallel o'lchov ──────
# Muammo: 15 narvon KETMA-KET har poll = ~5.7s, sekin (raqobatchi ~1s da slotni
# oladi). Yechim: har poll FAQAT 1 yengil so'rov bilan slotlarni aniqlaydi; slot
# O'ZGARSA (yangi chiqsa) → 15 narvon PARALLEL (~0.4s) o'lchaydi. Natija: kam
# so'rov (1/poll) + tez detektsiya (0.4s cadence). Booking YO'Q — hozircha faqat
# kuzatuv (Abdulaziz keyin hal qiladi).
_FAST_SNAPSHOT: dict[str, set] = {}  # shop -> oxirgi aniqlangan slot timeFrom to'plami
# Detekt miqdori (Abdulaziz 2026-07-06, AKT-O'LCHAM RADAR). quantity=1 → HAMMA
# slot ko'rinadi (existence), LEKIN «mavjud slot kattalashди» (1→600) sezilmaydi
# (u allaqachon ≥1). Env POSTAVKA_DETECT_Q (masalan 100) → slot shu o'lchamни KESIB
# o'tsa (bookable bo'lsa) «yangi» bo'lib chiqadi → radar TUTADI. Qaytarish: =1.
try:
    _DETECT_Q = max(1, int(os.environ.get("POSTAVKA_DETECT_Q", "1").strip() or "1"))
except Exception:
    _DETECT_Q = 1


# ── ISSIQ (warm) doimiy pool ─────────────────────────────────────────────────
# Muammo (2026-07-08 o'lchovi): har burst YANGI ThreadPoolExecutor → yangi
# thread → yangi (SOVUQ) session → har so'rovda TLS handshake (~409ms), + kamdan-
# kam 90s qotish. Yechim: DOIMIY pool — thread'lar yashaydi → ulanishlar keep-alive
# (~181ms issiq), qotish yo'q. Har thread `client.enable_fast_read` bilan tez-o'qish
# rejimiga o'tadi (issiq, retry'siz, 600ms timeout — env `POSTAVKA_DETECT_TIMEOUT_SEC`).
_WARM_EX = None
_WARM_EX_LOCK = threading.Lock()


def _warm_executor():
    global _WARM_EX
    if _WARM_EX is None:
        with _WARM_EX_LOCK:
            if _WARM_EX is None:
                from concurrent.futures import ThreadPoolExecutor
                try:
                    to = max(0.1, float(os.environ.get("POSTAVKA_DETECT_TIMEOUT_SEC", "0.6") or "0.6"))
                except Exception:
                    to = 0.6
                _WARM_EX = ThreadPoolExecutor(
                    max_workers=min(len(LADDER), 16),
                    thread_name_prefix="slotwarm",
                    initializer=client.enable_fast_read,
                    initargs=(to,),
                )
    return _WARM_EX


def _measure_parallel(shop_uzum_id: str, base: dict, pool, ladder: list[int]) -> dict:
    """`measure_volumes` bilan bir xil natija, lekin narvon so'rovlari PARALLEL
    ISSIQ pool orqali (~0.3s, ketma-ket ~5.7s emas). Sekin so'rov (>600ms) →
    `requests.Timeout` → `_StragglerSkip` (o'sha sikl jimgina o'tkaziladi).
    Probe xatosi (403/HTTP) KO'TARILADI → chaqiruvchi kill-switch/cooldown qiladi."""
    ex = _warm_executor()

    def _probe(q):
        try:
            slots = client.get_time_slots(shop_uzum_id, [{**base, "quantityToStock": int(q)}], pool)
        except (requests.Timeout, requests.ConnectionError):
            # Straggler yoki tranzient ulanish — bu XATO EMAS, siklni o'tkaz.
            raise _StragglerSkip()
        return int(q), (slots or [])

    results = list(ex.map(_probe, sorted(ladder)))
    snap: dict[int, dict] = {}
    for q, slots in results:
        for s in slots:
            sf = s.get("timeFrom") if isinstance(s, dict) else None
            if not sf:
                continue
            sf = int(sf)
            cur = snap.get(sf)
            if cur is None or q > cur["vol"]:
                snap[sf] = {"vol": int(q), "to": int(s.get("timeTo") or 0)}
    return snap


def poll_fast_once(shop_uzum_id: str, on_events=None) -> tuple[int, str | None]:
    """POYGA RADARI: 1 yengil so'rov → slotlarni aniqla; slot O'ZGARSA (yangi
    paydo bo'lsa) → parallel narvon-o'lchov → diff → yoz.

    -> (yangi event soni, xato_xulosasi|None). Restartdan keyingi birinchi poll
    faqat seed. `on_events` — `poll_volume_once` bilan bir xil (bus publish).
    """
    base, dim, pool = slot_watch._get_sku_context(shop_uzum_id)
    if not base:
        return 0, "SKU topilmadi (token/ombor muammosi?)"
    key = str(shop_uzum_id)

    # 1. TEZ DETEKTSIYA — 1 so'rov: quantity=_DETECT_Q sig'adigan slotlar (timeFrom).
    #    _DETECT_Q>1 bo'lsa faqat «shu o'lcham sig'adigan» (bookable) slotlar sanaladi.
    try:
        slots = client.get_time_slots(shop_uzum_id, [{**base, "quantityToStock": _DETECT_Q}], pool)
    except Exception as ex:
        slot_watch._SKU_CACHE.pop(key, None)
        return 0, f"{type(ex).__name__}: {ex}"[:120]
    cur_slots = {int(s["timeFrom"]) for s in (slots or [])
                 if isinstance(s, dict) and s.get("timeFrom")}

    prev_slots = _FAST_SNAPSHOT.get(key)
    _FAST_SNAPSHOT[key] = cur_slots
    if prev_slots is None:
        return 0, None  # seed — soxta to'lqin yo'q

    # 2. Yangi slot YO'Q → qimmat o'lchovni o'tkazamiz (arzon yo'l, faqat 1 so'rov)
    if not (cur_slots - prev_slots):
        return 0, None

    # 3. Yangi slot CHIQDI → parallel narvon-o'lchov → diff → yoz
    try:
        curr = _measure_parallel(shop_uzum_id, base, pool, LADDER)
    except _StragglerSkip:
        return 0, None   # >600ms straggler — siklni jimgina o'tkaz (xato emas, snapshot o'zgarmaydi)
    except Exception as ex:
        slot_watch._SKU_CACHE.pop(key, None)
        return 0, f"{type(ex).__name__}: {ex}"[:120]
    prev = _SNAPSHOT.get(key)
    if is_unreliable_drop(prev, curr):
        print(f"[SlotVolume] ishonchsiz o'lchov: {len(curr)}/{len(prev)} slot — e'tiborsiz")
        return 0, None
    _SNAPSHOT[key] = curr
    if prev is None:
        return 0, None
    events = diff_snapshots(prev, curr)
    _persist_events(key, events, pool, dim, on_events)
    return len(events), None


# ── CHUQUR-PARALLEL SINOV (Abdulaziz 2026-07-06) ─────────────────────────────
# Radar (poll_fast_once) muammosi: 1-so'rovlik detekt slot MAVJUDLIGINI ko'radi,
# LEKIN sig'imni emas — «allaqachon ko'rinadigan slot 1→600 kattalashди» sezilmaydi.
# Bu funksiya DETEKT-DARVOZANI olib tashlaydi: HAR poll 15-narvonni PARALLEL
# o'lchaydi (~0.4s) → har slotning sig'imi har tsiklda qayta o'lchanadi → o'sish
# (bo'shash) DARHOL tutiladi. `poll_volume_once` bilan bir xil mantiq, faqat
# `measure_volumes` (ketma-ket ~5.7s) o'rniga `_measure_parallel` (~0.4s).
#
# ⚠️ NARX: har poll = 15 so'rov. 0.4s cadence → ~37.5 so'rov/s — bu 2026-07-03
# IP-blok tezligidan (~3.5/s) ~10× BALAND. Faqat SINOV; 403 kelsa app.py cooldown
# (60s→5daq→15daq) IPni himoya qiladi. Qaytarish: POSTAVKA_DEEP_PARALLEL=0.
def poll_deep_parallel_once(shop_uzum_id: str, on_events=None) -> tuple[int, str | None]:
    """Har poll = 15-narvon PARALLEL o'lchov (detekt-darvozasiz) → diff → yoz.

    -> (yangi event soni, xato_xulosasi|None). Seed (birinchi poll) + ishonchsiz-
    tushish himoyasi `poll_volume_once` bilan bir xil. `on_events` — bus publish.
    """
    base, dim, pool = slot_watch._get_sku_context(shop_uzum_id)
    if not base:
        return 0, "SKU topilmadi (token/ombor muammosi?)"
    key = str(shop_uzum_id)

    try:
        curr = _measure_parallel(shop_uzum_id, base, pool, LADDER)
    except _StragglerSkip:
        return 0, None   # >600ms straggler — siklni jimgina o'tkaz (xato emas, snapshot o'zgarmaydi)
    except Exception as ex:
        slot_watch._SKU_CACHE.pop(key, None)
        return 0, f"{type(ex).__name__}: {ex}"[:120]

    prev = _SNAPSHOT.get(key)
    if is_unreliable_drop(prev, curr):
        print(f"[SlotVolume] ishonchsiz o'lchov: {len(curr)}/{len(prev)} slot — e'tiborsiz, snapshot saqlandi")
        return 0, None
    _SNAPSHOT[key] = curr
    if prev is None:
        return 0, None  # seed — soxta to'lqin yo'q
    events = diff_snapshots(prev, curr)
    _persist_events(key, events, pool, dim, on_events)
    return len(events), None


# ── PIPELINE-RADAR SINOV (Abdulaziz 2026-07-06) ──────────────────────────────
# Bir o'lchov ~0.6s (15-narvon parallel) — tezlashtirilmaydi (Uzum javob vaqti).
# Ko'proq temporal resolution uchun: launcher (app.py) har ~0.2s'da YANGI o'lchov
# ishga tushiradi — oldingisini KUTMASDAN, ustma-ust. O'lchovlar TARTIPSIZ tugaydi
# → GUARD: har o'lchovga `started_at` (monotonic) belgisi; faqat ENG YANGI natija
# _SNAPSHOT'ga qo'llanadi (kech tugagan eski natija tashlanadi — soxta event yo'q).
# ⚠️ 0.2s × 15 narvon = ~75 so'rov/s — FAQAT SINOV (uzoq ketsa ban).
_PIPELINE_LOCK = threading.Lock()
_PIPELINE_LAST_APPLIED: dict[str, float] = {}  # shop -> qo'llangan eng oxirgi started_at


def poll_pipeline_measure(shop_uzum_id, on_events=None, started_at=None) -> tuple[int, str | None]:
    """Bitta 15-narvon PARALLEL o'lchov (ustma-ust chaqiriladi) → tartip-guard →
    diff → yoz. `started_at` = launcher bergan monotonic vaqt (tartib uchun).

    -> (event soni, xato|None). Kech tugagan ESKI o'lchov e'tiborsiz (0, None) —
    yangisi allaqachon qo'llangan bo'lsa snapshot buzilmaydi.
    """
    if started_at is None:
        started_at = time.monotonic()
    base, dim, pool = slot_watch._get_sku_context(shop_uzum_id)
    if not base:
        return 0, "SKU topilmadi (token/ombor muammosi?)"
    key = str(shop_uzum_id)
    try:
        curr = _measure_parallel(shop_uzum_id, base, pool, LADDER)
    except _StragglerSkip:
        return 0, None   # >600ms straggler — siklni jimgina o'tkaz (xato emas, snapshot o'zgarmaydi)
    except Exception as ex:
        slot_watch._SKU_CACHE.pop(key, None)
        return 0, f"{type(ex).__name__}: {ex}"[:120]

    with _PIPELINE_LOCK:
        # Tartip-guard: bu o'lchov eng oxirgi qo'llangandan eskiroq bo'lsa — tashla.
        if started_at <= _PIPELINE_LAST_APPLIED.get(key, 0.0):
            return 0, None
        _PIPELINE_LAST_APPLIED[key] = started_at
        prev = _SNAPSHOT.get(key)
        if is_unreliable_drop(prev, curr):
            print(f"[SlotVolume] ishonchsiz o'lchov: {len(curr)}/{len(prev)} slot — e'tiborsiz")
            return 0, None
        _SNAPSHOT[key] = curr
        if prev is None:
            return 0, None  # seed
        events = diff_snapshots(prev, curr)
    # DB/tarmoq lock TASHQARISIDA (lockni ushlab turmaymiz).
    _persist_events(key, events, pool, dim, on_events)
    return len(events), None


def _day_key(ms: int) -> date:
    return datetime.fromtimestamp(int(ms) / 1000, _TZ).date()


def build_volume_digest(shop_uzum_id: str, shop_name: str) -> str | None:
    """30-daqiqalik design-C hisoboti: 7 kun bo'yicha cumulative (bugun).

    «jami hajm/slot» = bugun (Tashkent yarim tunidan) yig'ilgan; «+30daq» =
    oxirgi 30 daqiqa; «hozir» = oxirgi snapshotdagi bo'sh slotlar. -> matn|None.
    """
    now = time.time()
    now_ms = int(now * 1000)
    today = datetime.fromtimestamp(now, _TZ).date()
    midnight_local = datetime(today.year, today.month, today.day, tzinfo=_TZ)
    midnight_utc = midnight_local.astimezone(timezone.utc).replace(tzinfo=None)
    cutoff_30 = datetime.utcnow() - timedelta(minutes=30)

    with SessionLocal() as db:
        evs = db.execute(
            select(PostavkaVolumeEvent)
            .where(PostavkaVolumeEvent.shop_uzum_id == str(shop_uzum_id))
            .where(PostavkaVolumeEvent.detected_at >= midnight_utc)
        ).scalars().all()

    per_day: dict[date, dict] = {}
    for r in evs:
        d = _day_key(r.slot_from_ms)
        e = per_day.setdefault(d, {"hajm": 0, "slot": 0, "d30": 0})
        e["hajm"] += r.freed_volume
        e["slot"] += 1
        if r.detected_at >= cutoff_30:
            e["d30"] += r.freed_volume

    snap = _SNAPSHOT.get(str(shop_uzum_id), {})
    hozir: dict[date, int] = {}
    for sf in snap:
        d = _day_key(sf)
        hozir[d] = hozir.get(d, 0) + 1

    # POYLASH OYNASI (Abdulaziz, 2026-06-20): odam o'zi oson oladigan faol
    # kunlar (27-30) EMAS, balki YAQIN kunlarni poylash — chunki programmaning
    # ma'nosi shu: kimdir yaqin kun poslavkasini BEKOR QILSA, o'sha qisqa
    # umrli slotni darhol ushlash. Shu sababli oyna BO'SH kunlarni ham
    # ko'rsatadi (default: BUGUNDAN 9 kun → bugun 22–30 iyun; Abdulaziz so'rovi
    # 2026-06-22: bugun ham ko'rinsin). Env bilan moslanadi: _FROM_OFFSET
    # (bugundan necha kun keyin boshlanadi, 0=bugun), _WINDOW_DAYS (necha kun).
    try:
        off = max(0, int(os.environ.get("POSTAVKA_VOLUME_FROM_OFFSET", "0")))
        win = max(1, int(os.environ.get("POSTAVKA_VOLUME_WINDOW_DAYS", "9")))
    except Exception:
        off, win = 0, 9
    start = today + timedelta(days=off)
    days = [start + timedelta(days=i) for i in range(win)]

    rows = []
    for d in days:
        e = per_day.get(d, {"hajm": 0, "slot": 0, "d30": 0})
        rows.append({
            "day": _fmt_day(d),
            "hajm": e["hajm"],
            "d30": e["d30"],
            "slot": e["slot"],
            "hozir": hozir.get(d, 0),
        })
    return format_volume_digest(shop_name, now_ms, rows)
