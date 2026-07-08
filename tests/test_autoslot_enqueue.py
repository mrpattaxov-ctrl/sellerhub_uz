"""Avto-slot queue YOZISH yo'li (Bosqich 3) — store.enqueue upsert semantikasi.

In-memory SQLite sessiya bilan (DB yelimi sinaladi, tarmoq yo'q). Asosiy
kafolatlar:
  1. create — barcha navbat maydonlari to'g'ri (user_id, enabled_at, max_date,
     hamda eski grabber uchun target_day=max_date).
  2. upsert — (shop, invoice) `waiting` qatori bo'lsa yangilaydi, lekin
     `enabled_at` (navbat o'rni) va birlamchi `user_id` SAQLANADI.
  3. `booked`/boshqa holatdagi qator yangi `waiting`ni TO'SMAYDI.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from models import Base, PostavkaGrabPlan
from postavki.autoslot import store


def _session() -> Session:
    eng = create_engine("sqlite://")
    # Faqat shu jadval — Base.metadata.create_all butun sxemani (JSONB'li
    # fbs_orders) yaratmoqchi bo'lib SQLite'da uziladi.
    PostavkaGrabPlan.__table__.create(eng)
    return Session(eng)


class TestEnqueue:
    def test_create_sets_all_queue_fields(self):
        db = _session()
        pid, action = store.enqueue(
            db, user_id=7, shop_uzum_id="51948", invoice_id=100, invoice_number="A-1",
            volume=300, dim_group="SMALL", pool_source="FULLFILMENT", stock_id=55,
            max_date=date(2026, 7, 1),
        )
        db.commit()
        assert action == "created"
        row = db.get(PostavkaGrabPlan, pid)
        assert row.user_id == 7
        assert row.max_date == date(2026, 7, 1)
        assert row.target_day == date(2026, 7, 1)   # eski grabber compat
        assert row.draft_size == 300
        assert row.dim_group == "SMALL" and row.pool_source == "FULLFILMENT"
        assert row.status == "waiting" and row.enabled_at is not None

    def test_upsert_preserves_enabled_at_and_owner(self):
        db = _session()
        early = datetime(2026, 6, 1, 8, 0, 0)
        pid, _ = store.enqueue(
            db, user_id=7, shop_uzum_id="S", invoice_id=1, invoice_number="A",
            volume=100, dim_group="SMALL", pool_source="P", stock_id=1,
            max_date=date(2026, 7, 1), enabled_at=early,
        )
        db.commit()
        # Qayta yoqish: yangi muddat + kattaroq hajm + boshqa user → o'sha qator.
        pid2, action = store.enqueue(
            db, user_id=9, shop_uzum_id="S", invoice_id=1, invoice_number="A",
            volume=250, dim_group="SMALL", pool_source="P", stock_id=1,
            max_date=date(2026, 7, 5),
        )
        db.commit()
        assert pid2 == pid and action == "updated"
        row = db.get(PostavkaGrabPlan, pid)
        assert row.enabled_at == early              # navbat o'rni surilmadi
        assert row.user_id == 7                     # birlamchi egasi saqlandi
        assert row.max_date == date(2026, 7, 5) and row.draft_size == 250

    def test_booked_row_does_not_block_new_waiting(self):
        db = _session()
        pid, _ = store.enqueue(
            db, user_id=1, shop_uzum_id="S", invoice_id=1, invoice_number="A",
            volume=100, dim_group=None, pool_source="P", stock_id=1,
            max_date=date(2026, 7, 1),
        )
        db.get(PostavkaGrabPlan, pid).status = "booked"
        db.commit()
        pid2, action = store.enqueue(
            db, user_id=1, shop_uzum_id="S", invoice_id=1, invoice_number="A",
            volume=100, dim_group=None, pool_source="P", stock_id=1,
            max_date=date(2026, 7, 2),
        )
        db.commit()
        assert action == "created" and pid2 != pid


class TestCandidatesQuery:
    """candidates_for: ombor bitta + slot global → dim/pool FILTRLAMAYDI,
    faqat deadline (day<=max_date), navbat tartibi (enabled_at)."""

    def _seed(self, db):
        rows = [
            dict(user_id=1, shop_uzum_id="S1", invoice_id=1, invoice_number="A", volume=100,
                 dim_group="SMALL", pool_source="P", stock_id=1, max_date=date(2026, 7, 3),
                 enabled_at=datetime(2026, 6, 1, 9, 0)),
            dict(user_id=2, shop_uzum_id="S2", invoice_id=2, invoice_number="B", volume=50,
                 dim_group="SMALL", pool_source="P", stock_id=2, max_date=date(2026, 7, 1),
                 enabled_at=datetime(2026, 6, 1, 8, 0)),   # oldinroq yoqilgan
            dict(user_id=3, shop_uzum_id="S3", invoice_id=3, invoice_number="C", volume=100,
                 dim_group="LARGE", pool_source="Q", stock_id=3, max_date=date(2026, 7, 9),
                 enabled_at=datetime(2026, 6, 1, 7, 0)),   # boshqa dim+pool — baribir KIRADI
            dict(user_id=4, shop_uzum_id="S4", invoice_id=4, invoice_number="D", volume=100,
                 dim_group="SMALL", pool_source="P", stock_id=4, max_date=date(2026, 6, 30),
                 enabled_at=datetime(2026, 6, 1, 6, 0)),   # muddati o'tib ketgan (deadline < day)
        ]
        for r in rows:
            store.enqueue(db, **r)
        db.commit()

    def test_only_deadline_filters_and_orders_by_enabled_at(self, monkeypatch):
        db = _session()
        self._seed(db)
        # candidates_for SessionLocal ishlatadi — test sessiyaга yo'naltiramiz.
        import contextlib
        monkeypatch.setattr(store, "SessionLocal",
                            lambda: contextlib.nullcontext(db), raising=False)
        got = store.candidates_for(date(2026, 7, 1))
        ids = [g["invoice_id"] for g in got]
        # Faqat S4 (deadline 06-30 < 07-01) tushadi. S3 (LARGE/boshqa pool) ham
        # KIRADI — dim/pool ahamiyatsiz. Tartib: enabled_at S3(07)<S2(08)<S1(09).
        assert ids == [3, 2, 1]

    def test_rebooking_only_earlier_slot_matches(self, monkeypatch):
        db = _session()
        # A — slotsiz draft (current_slot_ms=None) → har qanday slot mos.
        store.enqueue(db, user_id=1, shop_uzum_id="S", invoice_id=1, invoice_number="A",
                      volume=50, dim_group=None, pool_source="P", stock_id=1,
                      max_date=date(2026, 7, 9), current_slot_ms=None)
        # B — allaqachon 1000 slotда. Faqat <1000 slot bo'shasa mos.
        store.enqueue(db, user_id=1, shop_uzum_id="S", invoice_id=2, invoice_number="B",
                      volume=50, dim_group=None, pool_source="P", stock_id=1,
                      max_date=date(2026, 7, 9), current_slot_ms=1000)
        db.commit()
        import contextlib
        monkeypatch.setattr(store, "SessionLocal",
                            lambda: contextlib.nullcontext(db), raising=False)
        # Bo'shagan slot = 800 (B ning 1000'idan ertaroq) → A va B ikkalasi.
        early = [c["invoice_id"] for c in store.candidates_for(date(2026, 7, 1), 800)]
        assert sorted(early) == [1, 2]
        # Bo'shagan slot = 1500 (B'nikidan KECHROQ) → faqat A (slotsiz).
        late = [c["invoice_id"] for c in store.candidates_for(date(2026, 7, 1), 1500)]
        assert late == [1]
        # slot_from_ms berilmasa (eski chaqiruv) — filtr o'chiq, ikkalasi.
        nofilter = [c["invoice_id"] for c in store.candidates_for(date(2026, 7, 1))]
        assert sorted(nofilter) == [1, 2]

    def test_min_date_rejects_earlier_slots(self, monkeypatch):
        """Pastki chegara: min_date to'lgan bo'lsa undan OLDINGI kun slotlari
        rad etiladi; min_date NULL (eski qatorlar) — chegarasiz, hammaga mos."""
        db = _session()
        # A — oraliq [07-02 .. 07-05]: 07-01 slotini OLMAYDI, 07-03 ni oladi.
        store.enqueue(db, user_id=1, shop_uzum_id="S", invoice_id=1, invoice_number="A",
                      volume=50, dim_group=None, pool_source="P", stock_id=1,
                      max_date=date(2026, 7, 5), min_date=date(2026, 7, 2))
        # B — chegarasiz (min_date NULL): har qanday kun (muddat ichida) mos.
        store.enqueue(db, user_id=1, shop_uzum_id="S", invoice_id=2, invoice_number="B",
                      volume=50, dim_group=None, pool_source="P", stock_id=1,
                      max_date=date(2026, 7, 5), min_date=None)
        db.commit()
        import contextlib
        monkeypatch.setattr(store, "SessionLocal",
                            lambda: contextlib.nullcontext(db), raising=False)
        # 07-01 (A ning min_date'idan oldin) → faqat B.
        early = [c["invoice_id"] for c in store.candidates_for(date(2026, 7, 1))]
        assert early == [2]
        # 07-03 (oraliq ichida) → A va B ikkalasi.
        mid = [c["invoice_id"] for c in store.candidates_for(date(2026, 7, 3))]
        assert sorted(mid) == [1, 2]
        # min_date chegarasi = shu kunning O'ZI ham mos (>=).
        boundary = [c["invoice_id"] for c in store.candidates_for(date(2026, 7, 2))]
        assert sorted(boundary) == [1, 2]

    def test_priority_orders_before_enabled_at(self, monkeypatch):
        """Admin prioriteti FIFO'dan USTUN: yuqori priority oldinga chiqadi,
        teng bo'lsa enabled_at (kim oldin yoqsa) hal qiladi."""
        db = _session()
        # A oldin yoqilgan → default (priority 0 teng) da FIFO'да birinchi.
        store.enqueue(db, user_id=1, shop_uzum_id="S", invoice_id=1, invoice_number="A",
                      volume=50, dim_group=None, pool_source="P", stock_id=1,
                      max_date=date(2026, 7, 9), enabled_at=datetime(2026, 6, 1, 8, 0))
        pidB, _ = store.enqueue(db, user_id=1, shop_uzum_id="S", invoice_id=2, invoice_number="B",
                                volume=50, dim_group=None, pool_source="P", stock_id=1,
                                max_date=date(2026, 7, 9), enabled_at=datetime(2026, 6, 1, 9, 0))
        db.commit()
        import contextlib
        monkeypatch.setattr(store, "SessionLocal",
                            lambda: contextlib.nullcontext(db), raising=False)
        # Teng prioritet (0) → FIFO: A(08:00) oldin.
        assert [c["invoice_id"] for c in store.candidates_for(date(2026, 7, 1))] == [1, 2]
        # B ga yuqori prioritet → B navbat boshiga chiqadi (FIFO ustidan).
        assert store.set_priority(pidB, 5) == 5
        assert [c["invoice_id"] for c in store.candidates_for(date(2026, 7, 1))] == [2, 1]
        # set_priority topilmagan reja → None.
        assert store.set_priority(99999, 1) is None

    def test_exhausted_attempts_excluded(self, monkeypatch):
        db = _session()
        pid, _ = store.enqueue(db, user_id=1, shop_uzum_id="S", invoice_id=1, invoice_number="A",
                               volume=50, dim_group=None, pool_source="P", stock_id=1,
                               max_date=date(2026, 7, 9))
        db.get(PostavkaGrabPlan, pid).attempts = store.MAX_ATTEMPTS  # limitга yetdi
        db.commit()
        import contextlib
        monkeypatch.setattr(store, "SessionLocal",
                            lambda: contextlib.nullcontext(db), raising=False)
        assert store.candidates_for(date(2026, 7, 1)) == []   # cheksiz urinilmaydi
