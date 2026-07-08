"""Avto-slot — «qotib qolgan» reja fix + poyga-yutqazish retry raundi.

Ikkala kamchilik uchun mocked sinov (Uzum client CHAQIRILMAYDI):
  FIX 1 — attempts tugasa terminal `failed` + terminal-callback (Telegram):
    1. mark_failed(requeue=True), attempts < MAX → 'waiting' (eski xatti-harakat);
    2. attempts >= MAX → 'failed' TERMINAL + callback (avval abadiy waiting edi);
    3. release_expired: attempts tugagan expired 'booking' → 'failed' + callback;
    4. to'liq sikl: 3× claim+fail → 3-chisida terminal (real stuck-ssenariy);
    5. terminal bo'lgan reja candidates_for'da ko'rinmaydi.
  FIX 2 — booking xato bo'lsa o'sha event ichida keyingi nomzod (1 retry raund):
    6. poyga yutqazildi → keyingi nomzod band qilinadi;
    7. retry FAQAT xato bo'lgan hajmga sig'adigan nomzodni oladi;
    8. raundlar chegaralangan (1 + RETRY_ROUNDS) — cheksiz urinish yo'q;
    9. hammasi birinchi raundda band bo'lsa retry YO'Q;
   10. ikki raund yutug'i bitta Telegram xabarida jamlanadi.
"""
from __future__ import annotations

import contextlib
from datetime import date, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from models import PostavkaGrabPlan
from postavki.autoslot import store, allocator


def _session() -> Session:
    eng = create_engine("sqlite://")
    PostavkaGrabPlan.__table__.create(eng)
    return Session(eng)


def _point_store_at(db, monkeypatch):
    monkeypatch.setattr(store, "SessionLocal",
                        lambda: contextlib.nullcontext(db), raising=False)


def _add(db, **kw):
    return store.enqueue(db, user_id=1, shop_uzum_id=kw.get("shop", "S"),
                         invoice_id=kw["inv"], invoice_number=kw.get("num", "N"),
                         volume=kw.get("vol", 100), dim_group=None,
                         pool_source="P", stock_id=kw.get("stock", 7),
                         max_date=date(2026, 7, 9))[0]


def _capture_terminal(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(store, "_on_terminal", calls.append)
    return calls


# ── FIX 1: attempts tugasa terminal failed + callback ────────────────


class TestMarkFailedExhaustion:
    def test_below_max_requeues_as_before(self, monkeypatch):
        db = _session(); _point_store_at(db, monkeypatch)
        calls = _capture_terminal(monkeypatch)
        a = _add(db, inv=1); db.commit()
        db.get(PostavkaGrabPlan, a).attempts = store.MAX_ATTEMPTS - 1; db.commit()
        assert store.mark_failed(a, "429", requeue=True) == "waiting"
        assert db.get(PostavkaGrabPlan, a).status == "waiting"
        assert calls == []   # terminal emas — callback yo'q

    def test_at_max_goes_terminal_even_with_requeue(self, monkeypatch):
        db = _session(); _point_store_at(db, monkeypatch)
        calls = _capture_terminal(monkeypatch)
        a = _add(db, inv=1, num="A-7"); db.commit()
        db.get(PostavkaGrabPlan, a).attempts = store.MAX_ATTEMPTS; db.commit()
        assert store.mark_failed(a, "time-slot-003", requeue=True) == "failed"
        row = db.get(PostavkaGrabPlan, a)
        assert row.status == "failed" and "urinish tugadi" in row.error
        assert len(calls) == 1 and calls[0]["invoice_number"] == "A-7"
        assert "time-slot-003" in calls[0]["error"]

    def test_explicit_terminal_also_emits(self, monkeypatch):
        db = _session(); _point_store_at(db, monkeypatch)
        calls = _capture_terminal(monkeypatch)
        a = _add(db, inv=1); db.commit()
        assert store.mark_failed(a, "stockId yo'q", requeue=False) == "failed"
        assert len(calls) == 1   # requeue=False terminal ham xabar beradi

    def test_callback_exception_is_swallowed(self, monkeypatch):
        db = _session(); _point_store_at(db, monkeypatch)
        monkeypatch.setattr(store, "_on_terminal",
                            lambda item: (_ for _ in ()).throw(RuntimeError("tg down")))
        a = _add(db, inv=1); db.commit()
        # Callback yiqilsa ham mark_failed o'zi yiqilmasin, DB yozilgan bo'lsin.
        assert store.mark_failed(a, "x", requeue=False) == "failed"
        assert db.get(PostavkaGrabPlan, a).status == "failed"

    def test_full_cycle_third_failure_is_terminal(self, monkeypatch):
        """Real stuck-ssenariy: har event'da claim (attempts+1) + fail.
        Eski kodda 3-chidan keyin reja 'waiting'+attempts=3 bo'lib qotardi."""
        db = _session(); _point_store_at(db, monkeypatch)
        calls = _capture_terminal(monkeypatch)
        a = _add(db, inv=1); db.commit()
        for i in range(store.MAX_ATTEMPTS):
            assert store.claim([a]) == [a]          # waiting → booking, attempts+1
            final = store.mark_failed(a, "429", requeue=True)
        assert final == "failed"                     # 3-chi urinish terminal
        assert db.get(PostavkaGrabPlan, a).status == "failed"
        assert len(calls) == 1

    def test_terminal_row_never_a_candidate(self, monkeypatch):
        db = _session(); _point_store_at(db, monkeypatch)
        _capture_terminal(monkeypatch)
        a = _add(db, inv=1); db.commit()
        db.get(PostavkaGrabPlan, a).attempts = store.MAX_ATTEMPTS; db.commit()
        store.mark_failed(a, "x", requeue=True)
        assert store.candidates_for(date(2026, 7, 3)) == []


class TestReleaseExpiredExhaustion:
    def test_exhausted_expired_booking_goes_failed(self, monkeypatch):
        db = _session(); _point_store_at(db, monkeypatch)
        calls = _capture_terminal(monkeypatch)
        a = _add(db, inv=1); b = _add(db, inv=2); db.commit()
        past = datetime.utcnow() - timedelta(seconds=5)
        ra = db.get(PostavkaGrabPlan, a)
        ra.status = "booking"; ra.lease_until = past; ra.attempts = store.MAX_ATTEMPTS
        rb = db.get(PostavkaGrabPlan, b)
        rb.status = "booking"; rb.lease_until = past; rb.attempts = 1
        db.commit()
        n = store.release_expired()
        assert n == 1                                   # faqat b waiting'ga qaytdi
        assert db.get(PostavkaGrabPlan, a).status == "failed"
        assert "urinish tugadi" in db.get(PostavkaGrabPlan, a).error
        assert db.get(PostavkaGrabPlan, b).status == "waiting"
        assert len(calls) == 1 and calls[0]["id"] == a


# ── FIX 2: event ichida retry raund (poyga fallback) ─────────────────


class _Ev:
    def __init__(self, free_volume=100):
        self.slot_from_ms = 1751371200000
        self.free_volume = free_volume
        self.source_shop_id = "51948"


class TestRetryRound:
    """_book_parallel mock: qaysi invoice xato bo'lishini `fail_ids` belgilaydi.
    select_for_capacity REAL (toza funksiya), claim mock (hammasini beradi)."""

    def _setup(self, monkeypatch, candidates, fail_ids):
        monkeypatch.setenv("POSTAVKA_AUTOSLOT_MODE", "live")
        monkeypatch.setattr(store, "release_expired", lambda *a, **k: 0)
        monkeypatch.setattr(store, "candidates_for",
                            lambda day, slot_from_ms=None: candidates)
        monkeypatch.setattr(store, "claim", lambda ids, lease_sec=30: ids)
        rounds: list[list[int]] = []
        def book(items, ms):
            rounds.append([it["id"] for it in items])
            return [("error" if it["id"] in fail_ids else "booked", it)
                    for it in items]
        monkeypatch.setattr(allocator, "_book_parallel", book)
        msgs: list[str] = []
        allocator.set_notifier(msgs.append)
        return rounds, msgs

    def _c(self, i, vol, num=None):
        return {"id": i, "invoice_id": i * 10, "invoice_number": num or f"I-{i}",
                "shop_uzum_id": "S", "stock_id": 7, "volume": vol}

    def teardown_method(self):
        allocator.set_notifier(None)

    def test_race_lost_books_next_candidate(self, monkeypatch):
        # A(100) poygada yutqazdi → o'sha event ichida B(80) band qilinadi.
        cands = [self._c(1, 100), self._c(2, 80)]
        rounds, msgs = self._setup(monkeypatch, cands, fail_ids={1})
        allocator.on_slot_freed(_Ev(free_volume=100))
        assert rounds == [[1], [2]]                 # raund1: A; raund2: B
        assert len(msgs) == 1 and "I-2" in msgs[0] and "1 ta akt" in msgs[0]

    def test_retry_pool_limited_to_failed_volume(self, monkeypatch):
        # A(50) yutqazdi; B(80) xato hajmga (50) SIG'MAYDI → retry bo'sh, tamom.
        cands = [self._c(1, 50), self._c(2, 80)]
        rounds, msgs = self._setup(monkeypatch, cands, fail_ids={1, 2})
        allocator.on_slot_freed(_Ev(free_volume=50))
        assert rounds == [[1]]                       # B umuman sinalmadi
        assert msgs == []

    def test_rounds_are_bounded(self, monkeypatch):
        # Hammasi xato: A → B → STOP (1 + RETRY_ROUNDS=1). C sinalmaydi.
        cands = [self._c(1, 100), self._c(2, 80), self._c(3, 50)]
        rounds, msgs = self._setup(monkeypatch, cands, fail_ids={1, 2, 3})
        allocator.on_slot_freed(_Ev(free_volume=100))
        assert rounds == [[1], [2, 3]] or rounds == [[1], [2]]
        assert len(rounds) == 1 + allocator.RETRY_ROUNDS
        assert msgs == []

    def test_no_retry_when_all_booked(self, monkeypatch):
        cands = [self._c(1, 60), self._c(2, 40)]
        rounds, msgs = self._setup(monkeypatch, cands, fail_ids=set())
        allocator.on_slot_freed(_Ev(free_volume=100))
        assert rounds == [[1, 2]]                    # bitta raund yetdi
        assert len(msgs) == 1 and "2 ta akt" in msgs[0]

    def test_both_rounds_merge_into_one_notify(self, monkeypatch):
        # Raund1: A(100)+B(90) → A band, B xato; raund2: C(40) band.
        cands = [self._c(1, 100), self._c(2, 90), self._c(3, 40)]
        rounds, msgs = self._setup(monkeypatch, cands, fail_ids={2})
        allocator.on_slot_freed(_Ev(free_volume=200))
        assert rounds == [[1, 2], [3]]
        assert len(msgs) == 1
        assert "2 ta akt" in msgs[0] and "I-1" in msgs[0] and "I-3" in msgs[0]

    def test_already_tried_never_retried_in_same_event(self, monkeypatch):
        # Yutqazgan A retry'da QAYTA sinalmaydi (attempts budjeti himoyasi).
        cands = [self._c(1, 100)]
        rounds, msgs = self._setup(monkeypatch, cands, fail_ids={1})
        allocator.on_slot_freed(_Ev(free_volume=100))
        assert rounds == [[1]]
