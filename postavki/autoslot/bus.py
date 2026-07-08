"""Avto-slot CHOK (seam) — event uzatish nuqtasi.

Bu butun «keyin alohida serverga ko'chirish» rejasining KALITI. Monitoring
faqat `publish(event)` ni chaqiradi; allokator `consume_forever(handler)`
bilan tinglaydi. Ikkalasi bir-birini import qilmaydi — faqat shu chok orqali.

HOZIR: in-process navbat (`queue.Queue`) — publish bloklamaydi (monitor
event'ni tashlab o'z ishini davom ettiradi), allokator alohida thread'da
o'qiydi. Bu allaqachon «produser/konsumer» ajratilishi.

KEYIN: `_transport` ni `HttpTransport` (monitoring → asosiy serverga POST)
yoki `RedisTransport` (pub/sub) bilan almashtirish kifoya. `publish` /
`consume_forever` interfeysi o'zgarmaydi → biznes-logika bir qatordan ham
tegmaydi.
"""
from __future__ import annotations

import queue as _stdqueue
from typing import Callable

from postavki.autoslot.events import SlotFreedEvent

# Sentinel — consume loop'ni toza to'xtatish uchun.
_STOP = object()


class InProcessTransport:
    """Thread-xavfsiz in-process navbat. Bitta process ichida produser va
    konsumer turli thread'larda ishlaganda yetarli."""

    def __init__(self) -> None:
        self._q: _stdqueue.Queue = _stdqueue.Queue()

    def publish(self, event: SlotFreedEvent) -> None:
        self._q.put(event)

    def get(self, timeout: float | None = None):
        return self._q.get(timeout=timeout)

    def stop(self) -> None:
        self._q.put(_STOP)

    def qsize(self) -> int:
        return self._q.qsize()


# Yagona transport instansi. KEYIN: shu qatorni HttpTransport/RedisTransport
# bilan almashtiramiz — qolgani o'zgarmaydi.
_transport = InProcessTransport()


def publish(event: SlotFreedEvent) -> None:
    """Monitoring shu yagona funksiyani chaqiradi (bloklamaydi)."""
    _transport.publish(event)


def consume_forever(handler: Callable[[SlotFreedEvent], None]) -> None:
    """Allokator thread'i: event kelganda `handler(event)` ni chaqiradi.
    `stop()` chaqirilsa toza chiqadi. Handler exception'i loop'ni o'ldirmaydi.
    """
    while True:
        ev = _transport.get()
        if ev is _STOP:
            return
        try:
            handler(ev)
        except Exception as e:  # bitta event xatosi butun loop'ni o'ldirmasin
            print(f"[autoslot.bus] handler xato: {e!r}")


def stop() -> None:
    """Consume loop'ni to'xtatish (test/shutdown)."""
    _transport.stop()


def pending() -> int:
    """Navbatdagi qayta ishlanmagan event soni (diagnostika)."""
    return _transport.qsize()
