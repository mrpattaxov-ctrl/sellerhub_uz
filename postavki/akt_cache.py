"""DB-kesh + paced fetch — FBO поставка «Акт отправки» PDF'lari uchun.

FBS'dagi ``core/fbs_akt_cache.py`` bilan bir xil naqsh, lekin postavki
(portal API, browser token) uchun va do'kon (shop_uzum_id) bo'yicha scoped.

Nega kesh: Uzum portal ``invoice/printInvoice`` aktni jonli beradi va ko'p
akt birato'la bosilganда sekin/429 bo'ladi. Background worker har CREATED
поставка aktini oldindan (paced) ``postavki_invoice_akts`` jadvaliga tortib
qo'yadi → bulk «Акт отправки» DB'dan ONIY o'qiladi.

Bu modul — keshни o'qish/yozishning YAGONA joyi; interaktiv route'lar ham,
background prefetch ham shu yerdan o'tadi (siyosat bir joyда).
"""
from __future__ import annotations

import threading

from sqlalchemy import func, delete as _delete
from sqlalchemy.dialects.postgresql import insert as pg_insert

from extensions import SessionLocal
from models import PostavkiInvoiceAkt
from postavki import client


# ── In-flight dedup: bir akt bir vaqtда bir martagina tortilsin ──────
# Sahifa-warm (fon) va bulk so'rov bir aktni bир vaqtda tortib, Uzum'ga ikki
# barobar so'rov yubormasligi uchun har invoice_id bo'yicha lock. Lock kutib
# turгач, kesh qayta tekshiriladi (boshqa oqim allaqachon tortgan bo'lishi mumkin).
_inflight_lock = threading.Lock()
_inflight: dict[int, "threading.Lock"] = {}


def _id_lock(invoice_id) -> "threading.Lock":
    iid = int(invoice_id)
    with _inflight_lock:
        lk = _inflight.get(iid)
        if lk is None:
            lk = threading.Lock()
            _inflight[iid] = lk
        return lk


def get_cached_akt(shop_uzum_id, invoice_id, *, date_updated=None) -> bytes | None:
    """Shu поставка uchun keshlangan PDF baytlari, yoki None (miss/eskirgan).

    ``shop_uzum_id`` bo'yicha scoped — boshqa do'kon aktini id taxmin qilib
    o'qib bo'lmaydi. ``date_updated`` berilsa va kesh versiyasidan farq qilsa,
    qator ESKIRGAN (None) — chaqiruvchi yangisini tortadi.
    """
    with SessionLocal() as db:
        row = db.get(PostavkiInvoiceAkt, int(invoice_id))
        if row is None or str(row.shop_uzum_id) != str(shop_uzum_id):
            return None
        # Chaqiruvchi versiya (du) bersa va kesh qatori BOSHQA yoki NULL versiyali
        # bo'lsa — ESKIRGAN. NULL = legacy qator (du kuzatilmasdan oldin keshlangan):
        # du berilganда eskirgan deb hisoblanadi → qayta tortilib, versiya-shtamp
        # qo'yiladi. Shu yo'l bilan prefetch/warm legacy qatorlarni avtomatik
        # «davolaydi» va slot o'zgargач eski akt KO'CHIRILMAYDI (legacy qatorда ham).
        if date_updated is not None and (
                row.date_updated is None or int(row.date_updated) != int(date_updated)):
            return None
        return bytes(row.pdf)


def store_akt(shop_uzum_id, invoice_id, date_updated, pdf: bytes) -> None:
    """Akt PDF'ini upsert qiladi (INSERT … ON CONFLICT DO UPDATE)."""
    stmt = pg_insert(PostavkiInvoiceAkt).values(
        invoice_id=int(invoice_id),
        shop_uzum_id=str(shop_uzum_id),
        date_updated=(int(date_updated) if date_updated is not None else None),
        pdf=pdf,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[PostavkiInvoiceAkt.invoice_id],
        set_={
            "shop_uzum_id": stmt.excluded.shop_uzum_id,
            "date_updated": stmt.excluded.date_updated,
            "pdf": stmt.excluded.pdf,
            "synced_at": func.now(),
        },
    )
    with SessionLocal() as db:
        db.execute(stmt)
        db.commit()


def delete_akt(invoice_id) -> None:
    """Bitta поставка keshini o'chiradi (masalan slot o'zgartirilгандан keyин —
    akt sanasi/mazmuni o'zgaradi)."""
    with SessionLocal() as db:
        row = db.get(PostavkiInvoiceAkt, int(invoice_id))
        if row is not None:
            db.delete(row)
            db.commit()


def fetch_akt_live(shop_uzum_id, invoice_id) -> bytes:
    """Bitta aktni Uzum'dan jonli tortadi (http_json'ning token-bucket +
    429-retry'si avtomatik pace qiladi). Uzum PDF'ini o'zgartirmasdan beradi."""
    return client.print_invoice_act(str(shop_uzum_id), int(invoice_id))


def get_or_fetch_akt(shop_uzum_id, invoice_id, *, date_updated=None) -> bytes:
    """Cache-first: DB'da bo'lsa (va yangi bo'lsa) DB'dan, aks holda jonli
    tortib, saqlab, qaytaradi. Jonli xato bo'lsa exception ko'taradi.

    Bir invoice bo'yicha in-flight lock — ikki oqim (warm + bulk) bir aktni
    ikki marta tortmaydi: lock kutгач kesh qayta tekshiriladi."""
    cached = get_cached_akt(shop_uzum_id, invoice_id, date_updated=date_updated)
    if cached is not None:
        return cached
    with _id_lock(invoice_id):
        # Lock kutib turgан paytда boshqa oqim tortib qo'ygan bo'lishi mumkin.
        cached = get_cached_akt(shop_uzum_id, invoice_id, date_updated=date_updated)
        if cached is not None:
            return cached
        pdf = fetch_akt_live(shop_uzum_id, invoice_id)
        if pdf:
            try:
                store_akt(shop_uzum_id, invoice_id, date_updated, pdf)
            except Exception as e:  # keshlash best-effort — so'rovni yiqitmaydi
                print(f"[postavki-akt] store xato invoice={invoice_id}: {e!r}")
        return pdf


def prune_akts_not_in(shop_uzum_id, keep_ids) -> int:
    """Shu do'konning keshidan ``keep_ids``da YO'Q invoice'larni o'chiradi.

    Keshни do'konning HOZIRGI faol (CREATED) поставкаlariga cheklab turadi —
    поставка CREATED'дан chiqsa (qabul/bekor) qayta bosilmaydi → ~64 KB qatori
    keraksiz. Bo'sh ``keep_ids`` → do'kon qatorlarini butunlay o'chiradi.
    O'chirilgan qatorlar sonini qaytaradi.
    """
    keep = [int(i) for i in keep_ids]
    with SessionLocal() as db:
        q = _delete(PostavkiInvoiceAkt).where(
            PostavkiInvoiceAkt.shop_uzum_id == str(shop_uzum_id)
        )
        if keep:
            q = q.where(PostavkiInvoiceAkt.invoice_id.notin_(keep))
        res = db.execute(q)
        db.commit()
        return res.rowcount or 0


def prefetch_akts_for_shop(
    shop_uzum_id, *, statuses=("CREATED",), max_pages=25, max_akts=40
) -> tuple[int, int]:
    """Background: do'konning faol (CREATED) поставкаlari aktini keshга isitadi
    va endi faol bo'lmaganларни tozalaydi (jadval CHEGARALANGAN qoladi).

    Worker har do'kon uchun chaqiradi. ``statuses`` ro'yxatini varaqlaydi va
    har поставка akti yo'q/eskirgan bo'lsa tortib saqlaydi. Barqaror holatда
    aksar akt allaqachon keshда — tick faqat YANGI/o'zgarganларни tortadi.

    Keyin PRUNE: kesh hozirgi faol to'plamga aniq mos kelishi uchun CREATED'дан
    chiqqan qatorlar o'chiriladi. Ro'yxatни to'liq varaqlay olmaган bo'lsak
    (fetch xatosi) prune o'tkazib yuboriladi — ko'rmaган aktni o'chirib
    qo'ymaymiz. ``max_akts`` — bir tickда yuklanadigan akt soni chegarasi.
    ``(fetched, pruned)`` qaytaradi.
    """
    fetched = 0
    seen_ids: set[int] = set()
    complete = True
    for status in statuses:
        page = 0
        status_done = False
        while page < max_pages:
            try:
                invs = client.list_invoices(
                    str(shop_uzum_id), page=page, size=20, statuses=status
                )
            except Exception as e:
                print(f"[postavki-akt] list xato (shop={shop_uzum_id} status={status} p={page}): {e!r}")
                complete = False
                break
            if not invs:
                status_done = True
                break
            for inv in invs:
                iid = inv.get("id")
                if iid is None:
                    continue
                seen_ids.add(int(iid))
                # Slotsiz поставка akt bermaydi (Uzum: 003-invoice-time-slot-required)
                # → bekorga so'rab 400 olmaymiz; slot belgilanganда keyingi tick tortadi.
                if not inv.get("timeSlotReservation"):
                    continue
                du = inv.get("dateUpdated")
                if fetched >= max_akts:
                    continue
                if get_cached_akt(shop_uzum_id, iid, date_updated=du) is not None:
                    continue
                try:
                    pdf = fetch_akt_live(shop_uzum_id, iid)
                    if pdf:
                        store_akt(shop_uzum_id, iid, du, pdf)
                        fetched += 1
                except Exception as e:
                    print(f"[postavki-akt] akt fetch xato invoice={iid}: {e!r}")
            if len(invs) < 20:
                status_done = True
                break
            page += 1
        if not status_done:
            complete = False

    pruned = 0
    if complete:
        try:
            pruned = prune_akts_not_in(shop_uzum_id, seen_ids)
        except Exception as e:
            print(f"[postavki-akt] prune xato shop={shop_uzum_id}: {e!r}")
    return (fetched, pruned)


def _status_created(inv) -> bool:
    return ((inv.get("invoiceStatus") or {}).get("value")) == "CREATED"


def warm_invoices_async(shop_uzum_id, invoices, *, max_akts=25) -> None:
    """Sahifaга kirilганда: berilgan ro'yxatdan CREATED + slotли + keshlanmaган
    поставкаlar aktini FONda (alohida thread) tortib bazaga yozadi. Javobni
    bloklamaydi. Slotsizларni o'tkazadi (akt bermaydi). Bitta sahifa uchun
    ``max_akts`` bilan chegaralanган; token-bucket Uzum so'rovларини pace qiladi.
    """
    try:
        items = [
            inv for inv in (invoices or [])
            if inv.get("id") and inv.get("timeSlotReservation") and _status_created(inv)
        ]
    except Exception:
        items = []
    if not items:
        return

    def _run():
        n = 0
        for inv in items:
            if n >= max_akts:
                break
            iid = inv.get("id")
            du = inv.get("dateUpdated")
            try:
                if get_cached_akt(shop_uzum_id, iid, date_updated=du) is not None:
                    continue
                # get_or_fetch — in-flight lock bilan (bulk so'rov bilan ikki marta tortmaydi)
                if get_or_fetch_akt(shop_uzum_id, iid, date_updated=du):
                    n += 1
            except Exception as e:
                print(f"[postavki-akt] sahifa-warm xato invoice={iid}: {e!r}")
        if n:
            print(f"[postavki-akt] sahifa-warm: {n} ta akt keshlandi (shop={shop_uzum_id})")

    threading.Thread(target=_run, daemon=True, name="postavki-akt-warm").start()


def warm_one_async(shop_uzum_id, invoice_id, *, date_updated=None) -> None:
    """Bitta поставка aktини FONda (qayta) keshlaydi — slot belgilangач yoki
    o'zgargач chaqiriladi. Eskisini o'chirib, yangisini tortadi (in-flight lock
    bilan — bulk/warm bilan urishmaydi)."""
    def _run():
        try:
            with _id_lock(invoice_id):
                delete_akt(invoice_id)
                pdf = fetch_akt_live(shop_uzum_id, invoice_id)
                if pdf:
                    store_akt(shop_uzum_id, invoice_id, date_updated, pdf)
        except Exception as e:
            print(f"[postavki-akt] warm-one xato invoice={invoice_id}: {e!r}")

    threading.Thread(target=_run, daemon=True, name="postavki-akt-warm1").start()


__all__ = [
    "get_cached_akt", "store_akt", "delete_akt", "fetch_akt_live",
    "get_or_fetch_akt", "prune_akts_not_in", "prefetch_akts_for_shop",
    "warm_invoices_async", "warm_one_async",
]
