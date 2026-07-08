"""Avto-slot moduli — bo'shagan FBO slotlarini global kuzatib, navbatdagi
aktlarga «vaqt + hajm» bo'yicha taqsimlab avtomatik band qilish.

Qatlamlar (chegara qat'iy — keyin monitoring alohida serverga ko'chsin):
  events.py    — SlotFreedEvent SHARTNOMASI (yagona umumiy til)
  bus.py       — CHOK (in-process navbat; keyin HTTP/Redis)
  monitor.py   — PRODUSER (slotni aniqlab event chiqaradi)
  allocator.py — KONSUMER (qaror + booking; biznes-logika)
  store.py     — DB queue (postavka_grab_plan)
  day.py       — vaqt yordamchilari (Toshkent)

Produser KONSUMERni import qilmaydi va aksincha — faqat `bus` orqali.
"""
from postavki.autoslot.events import SlotFreedEvent

__all__ = ["SlotFreedEvent"]
