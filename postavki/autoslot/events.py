"""Avto-slot SHARTNOMASI (contract) — `SlotFreedEvent`.

Bu butun arxitekturaning YAGONA shartnomasi: monitoring (produser) shu
shakldagi event chiqaradi, allokator (konsumer) shuni qabul qiladi. Boshqa
hech narsani bo'lishmaydi.

`to_dict` / `from_dict` — JSON-ga seriyalanadigan, chunki KEYIN monitoring
alohida serverga ko'chganda shu event HTTP/Redis orqali tarmoq bo'ylab
uchadi. Hozir in-process bo'lsa ham shaklni shu yerda qotiramiz — keyin
biznes-logika o'zgarmasin.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date


@dataclass(frozen=True)
class SlotFreedEvent:
    """Uzum FBO'da bitta timeslotning sig'imi bo'shaganini bildiradi.

    pool_source:   ombor (warehouse) — slot shu omborga tegishli.
    dim_group:     o'lcham-guruhi (SMALL/MEDIUM/...). Slot shu guruh uchun.
    day:           slot tushadigan Toshkent kuni (deadline solishtirish uchun).
    free_volume:   bo'shagan sig'im (birlik) — narvon-rezolyutsiyasi (~50).
    slot_from_ms:  band qilinadigan ANIQ timeFrom (epoch ms).
    slot_to_ms:    slot tugashi (epoch ms) — diagnostika.
    observed_at_ms: monitoring qachon ko'rdi (epoch ms).
    source_shop_id: qaysi token bilan kuzatildi (diagnostika; allokator
                    aniqlikni baribir AKT o'z tokeni bilan tekshiradi).
    """
    pool_source: str
    dim_group: str | None
    day: date
    free_volume: int
    slot_from_ms: int
    slot_to_ms: int | None = None
    observed_at_ms: int | None = None
    source_shop_id: str | None = None

    def to_dict(self) -> dict:
        """JSON-xavfsiz dict (date → ISO matn) — tarmoq chegarasi uchun."""
        d = asdict(self)
        d["day"] = self.day.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "SlotFreedEvent":
        """`to_dict` teskarisi — alohida serverdan kelgan event'ni tiklaydi."""
        data = dict(d)
        if isinstance(data.get("day"), str):
            data["day"] = date.fromisoformat(data["day"])
        return cls(**data)
