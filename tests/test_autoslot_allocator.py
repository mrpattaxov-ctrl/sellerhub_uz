"""Avto-slot ALLOKATOR packing (Bosqich 5) — select_for_capacity.

Toza funksiya (DB/tarmoq yo'q): bo'shagan slot hajmiga navbatdagi aktlarni
NAVBAT-tartibida first-fit qiladi. Asosiy kafolatlar:
  1. tanlangan hajmlar yig'indisi <= free_volume (HECH QACHON oshmaydi);
  2. navbat tartibi saqlanadi (input enabled_at bo'yicha keladi);
  3. sig'magan akt o'tkaziladi, kichikroq keyingilar joyni to'ldiradi;
  4. nol/manfiy hajm yoki bo'sh sig'im — xavfsiz.
"""
from __future__ import annotations

from postavki.autoslot.allocator import select_for_capacity


def _c(inv, vol):
    return {"invoice_id": inv, "volume": vol}


class TestSelectForCapacity:
    def test_all_fit_selects_all_in_order(self):
        cands = [_c(1, 30), _c(2, 40), _c(3, 20)]
        got = select_for_capacity(cands, 100)
        assert [c["invoice_id"] for c in got] == [1, 2, 3]

    def test_sum_never_exceeds_capacity(self):
        cands = [_c(1, 60), _c(2, 60), _c(3, 60)]
        got = select_for_capacity(cands, 100)
        assert sum(c["volume"] for c in got) <= 100
        assert [c["invoice_id"] for c in got] == [1]   # 60 olindi, 60+60 sig'maydi

    def test_skips_too_big_and_fills_with_smaller_later(self):
        # A (birinchi) sig'maydi, B (kichik, keyingi) joyni oladi.
        cands = [_c(1, 200), _c(2, 50)]
        got = select_for_capacity(cands, 100)
        assert [c["invoice_id"] for c in got] == [2]

    def test_first_fit_leaves_gap_when_fairness_wins(self):
        # A=90 (birinchi) olinadi → 10 qoladi; B,C (50) sig'maydi.
        # Adolat birlamchi: A navbatdan tushmaydi (to'liq to'ldirmasa ham).
        cands = [_c(1, 90), _c(2, 50), _c(3, 50)]
        got = select_for_capacity(cands, 100)
        assert [c["invoice_id"] for c in got] == [1]

    def test_greedy_fills_in_queue_order(self):
        # A=80 (rem20), B=15 (rem5), C=30 sig'maydi → [A,B].
        cands = [_c(1, 80), _c(2, 15), _c(3, 30)]
        got = select_for_capacity(cands, 100)
        assert [c["invoice_id"] for c in got] == [1, 2]

    def test_exact_fill(self):
        cands = [_c(1, 60), _c(2, 40)]
        got = select_for_capacity(cands, 100)
        assert [c["invoice_id"] for c in got] == [1, 2]
        assert sum(c["volume"] for c in got) == 100

    def test_zero_or_negative_capacity_returns_empty(self):
        cands = [_c(1, 10)]
        assert select_for_capacity(cands, 0) == []
        assert select_for_capacity(cands, -5) == []

    def test_skips_nonpositive_volume_rows(self):
        cands = [_c(1, 0), _c(2, -10), _c(3, 40)]
        got = select_for_capacity(cands, 100)
        assert [c["invoice_id"] for c in got] == [3]

    def test_empty_candidates(self):
        assert select_for_capacity([], 100) == []
