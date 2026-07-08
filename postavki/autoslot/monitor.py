"""Avto-slot MONITOR — produser (KEYIN alohida serverga ko'chadi).

Yagona vazifasi: Uzum'da bo'shagan slotlarni iloji boricha tez aniqlash va
HAR bittasi uchun `bus.publish(SlotFreedEvent)` chiqarish. Biznes-logikani
(navbat, allokatsiya, booking) BILMAYDI — faqat chok'ka event tashlaydi.

Mavjud `postavki.slot_volume` allaqachon narvon-probe bilan bo'shagan hajmni
aniqlaydi (Bosqich 0 tasdiqladi). Bu modul shu aniqlashni event'ga aylantiradi.

Bu fayl — KARKAS (Bosqich 2). Poll-loop'ga ulanish va ko'p-bucket qamrovi
Bosqich 4'da to'ldiriladi.
"""
from __future__ import annotations

from postavki.autoslot import bus
from postavki.autoslot.day import now_ms, day_of
from postavki.autoslot.events import SlotFreedEvent


def to_event(vol_event: dict, *, pool_source: str, dim_group: str | None,
             source_shop_id: str | None = None) -> SlotFreedEvent:
    """`slot_volume.diff_snapshots` chiqargan dict → `SlotFreedEvent`.

    vol_event: {"slot_from":ms, "slot_to":ms, "freed":int, "total":int}.
    `day` event ichida emas — allokator slot_from_ms'dan hisoblaydi (yagona
    manba). Shuning uchun bu yerda kun bermaymiz.
    """
    return SlotFreedEvent(
        pool_source=pool_source,
        dim_group=dim_group,
        day=day_of(vol_event["slot_from"]),
        free_volume=int(vol_event.get("freed") or 0),
        slot_from_ms=int(vol_event["slot_from"]),
        slot_to_ms=int(vol_event.get("slot_to") or 0) or None,
        observed_at_ms=now_ms(),
        source_shop_id=source_shop_id,
    )


def publish_freed(vol_events: list[dict], *, pool_source: str,
                  dim_group: str | None, source_shop_id: str | None = None) -> int:
    """Aniqlangan bo'shash-eventlarini chok'ka chiqaradi. -> yuborilgan soni.
    """
    n = 0
    for ve in vol_events:
        if not ve.get("slot_from"):
            continue
        bus.publish(to_event(ve, pool_source=pool_source, dim_group=dim_group,
                             source_shop_id=source_shop_id))
        n += 1
    return n


# TODO[Bosqich 4]: poll-loop. Kerakli (pool, dim) bucket'larni aniqlab (kutayotgan
# aktlardan), har birini tez probe qilib, slot_volume.diff bilan bo'shashni
# sezib `publish_freed(...)` chaqirish. Hozir wiring app.py slot-watch loop'da.
