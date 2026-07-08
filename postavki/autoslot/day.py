"""Vaqt yordamchilari — modul o'zicha mustaqil bo'lishi uchun (alohida
serverga ko'chsa, tashqi importga bog'lanmasin).

O'zbekistonda yozги vaqt yo'q — doimiy UTC+5.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta, date

TASHKENT = timezone(timedelta(hours=5))


_MON = {1: "yan", 2: "fev", 3: "mar", 4: "apr", 5: "may", 6: "iyun",
        7: "iyul", 8: "avg", 9: "sen", 10: "okt", 11: "noy", 12: "dek"}


def day_of(ms: int) -> date:
    """Slot boshlanishi (epoch ms) → Toshkent kuni (date)."""
    return datetime.fromtimestamp(int(ms) / 1000, TASHKENT).date()


def fmt_slot(ms: int) -> str:
    """Slot boshlanishi (epoch ms) → «D-oy HH:MM» (Toshkent) — xabarlar uchun."""
    d = datetime.fromtimestamp(int(ms) / 1000, TASHKENT)
    return f"{d.day}-{_MON[d.month]} {d:%H:%M}"


def now_ms() -> int:
    import time
    return int(time.time() * 1000)
