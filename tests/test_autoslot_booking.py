"""Avto-slot BOOKING (Bosqich 6) — atomic claim + mark + band qilish oqimi.

Booking REAL `set_time_slot` chaqiradi (Uzum'da haqiqiy band, penalty xavfi),
shuning uchun bu yerda HAMMASI MOCK: client chaqirilmaydi, faqat mantiq
sinaladi. Kafolatlar:
  1. claim ATOMIC — faqat `waiting` qatorlar olinadi, takror claim bo'sh;
  2. release_expired qotgan `booking`'ni qaytaradi, yangisini emas;
  3. mark_booked/mark_failed (requeue/terminal) holatni to'g'ri yozadi;
  4. _book_one client javobiga qarab booked/failed/error + to'g'ri mark;
  5. on_slot_freed: dry band QILMAYDI, live faqat CLAIM qilinganlarni band qiladi.
"""
from __future__ import annotations

import contextlib
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from models import Base, PostavkaGrabPlan
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
                         invoice_id=kw["inv"], invoice_number="N", volume=kw.get("vol", 100),
                         dim_group=None, pool_source="P", stock_id=kw.get("stock", 7),
                         max_date=date(2026, 7, 9))[0]


# ── claim / release / mark (SQLite) ──────────────────────────────────


class TestClaimAndMarks:
    def test_claim_returns_only_waiting_and_flips_status(self, monkeypatch):
        db = _session(); _point_store_at(db, monkeypatch)
        a = _add(db, inv=1); b = _add(db, inv=2); db.commit()
        claimed = store.claim([a, b], lease_sec=30)
        assert sorted(claimed) == sorted([a, b])
        rows = {r.id: r for r in db.query(PostavkaGrabPlan).all()}
        assert rows[a].status == "booking" and rows[a].lease_until is not None
        assert rows[a].attempts == 1

    def test_double_claim_is_empty(self, monkeypatch):
        db = _session(); _point_store_at(db, monkeypatch)
        a = _add(db, inv=1); db.commit()
        assert store.claim([a]) == [a]
        assert store.claim([a]) == []   # endi 'booking' — qayta olinmaydi

    def test_release_expired_resets_stale_only(self, monkeypatch):
        db = _session(); _point_store_at(db, monkeypatch)
        a = _add(db, inv=1); b = _add(db, inv=2); db.commit()
        # a — muddati o'tgan booking; b — yangi booking.
        ra = db.get(PostavkaGrabPlan, a); ra.status = "booking"; ra.lease_until = datetime.utcnow() - timedelta(seconds=5)
        rb = db.get(PostavkaGrabPlan, b); rb.status = "booking"; rb.lease_until = datetime.utcnow() + timedelta(seconds=60)
        db.commit()
        n = store.release_expired()
        assert n == 1
        assert db.get(PostavkaGrabPlan, a).status == "waiting"
        assert db.get(PostavkaGrabPlan, b).status == "booking"   # yangisi tegilmaydi

    def test_mark_booked(self, monkeypatch):
        db = _session(); _point_store_at(db, monkeypatch)
        a = _add(db, inv=1); db.commit()
        store.mark_booked(a, 1751371200000)
        r = db.get(PostavkaGrabPlan, a)
        assert r.status == "booked" and r.booked_slot_ms == 1751371200000 and r.booked_at

    def test_mark_failed_requeue_vs_terminal(self, monkeypatch):
        db = _session(); _point_store_at(db, monkeypatch)
        a = _add(db, inv=1); b = _add(db, inv=2); db.commit()
        store.mark_failed(a, "429", requeue=True)
        store.mark_failed(b, "stockId yo'q", requeue=False)
        assert db.get(PostavkaGrabPlan, a).status == "waiting"
        assert db.get(PostavkaGrabPlan, b).status == "failed"


# ── _book_one (client + store mock) ──────────────────────────────────


class TestBookOne:
    def _mock_marks(self, monkeypatch):
        calls = {"booked": [], "failed": [], "akt": []}
        monkeypatch.setattr(store, "mark_booked", lambda pid, ms: calls["booked"].append((pid, ms)))
        monkeypatch.setattr(store, "mark_failed", lambda pid, err, requeue=True: calls["failed"].append((pid, err, requeue)))
        # Akt-kesh yangilash — real thread/Uzum so'rov o'rniga chaqiruvni yozamiz.
        import postavki.akt_cache as ac
        monkeypatch.setattr(ac, "warm_one_async",
                            lambda shop, iid, date_updated=None: calls["akt"].append((shop, iid, date_updated)),
                            raising=False)
        return calls

    def _mock_client(self, monkeypatch, fn):
        import postavki.client as c
        monkeypatch.setattr(c, "set_time_slot", fn, raising=False)

    def test_success_marks_booked(self, monkeypatch):
        calls = self._mock_marks(monkeypatch)
        self._mock_client(monkeypatch, lambda *a, **k: {"id": 999, "dateUpdated": 1700000000000})
        st, _ = allocator._book_one({"id": 5, "shop_uzum_id": "S", "invoice_id": 1, "stock_id": 7}, 123)
        assert st == "booked" and calls["booked"] == [(5, 123)] and not calls["failed"]
        # Re-booking: slot o'zgardi → akt PDF keshi yangi dateUpdated bilan
        # invalidatsiya qilinishi SHART (aks holda «Акт отправки» eski slotni beradi).
        assert calls["akt"] == [("S", 1, 1700000000000)]

    def test_failed_booking_does_not_touch_akt_cache(self, monkeypatch):
        calls = self._mock_marks(monkeypatch)
        self._mock_client(monkeypatch, lambda *a, **k: {})   # javob bo'sh → band bo'lmadi
        allocator._book_one({"id": 5, "shop_uzum_id": "S", "invoice_id": 1, "stock_id": 7}, 123)
        assert calls["akt"] == []   # band bo'lmasa kesh tegilmaydi

    def test_empty_response_requeues(self, monkeypatch):
        calls = self._mock_marks(monkeypatch)
        self._mock_client(monkeypatch, lambda *a, **k: {})
        st, _ = allocator._book_one({"id": 5, "shop_uzum_id": "S", "invoice_id": 1, "stock_id": 7}, 123)
        assert st == "failed" and calls["failed"][0] == (5, "javob bo'sh", True)

    def test_exception_requeues(self, monkeypatch):
        calls = self._mock_marks(monkeypatch)
        def boom(*a, **k): raise RuntimeError("429")
        self._mock_client(monkeypatch, boom)
        st, _ = allocator._book_one({"id": 5, "shop_uzum_id": "S", "invoice_id": 1, "stock_id": 7}, 123)
        assert st == "error" and calls["failed"][0][2] is True   # requeue

    def test_missing_stock_fails_terminal_without_calling_client(self, monkeypatch):
        calls = self._mock_marks(monkeypatch)
        def must_not_call(*a, **k): raise AssertionError("client chaqirilmasligi kerak")
        self._mock_client(monkeypatch, must_not_call)
        st, _ = allocator._book_one({"id": 5, "shop_uzum_id": "S", "invoice_id": 1, "stock_id": None}, 123)
        assert st == "failed" and calls["failed"][0] == (5, "stockId yo'q", False)


# ── on_slot_freed dispatch (dry / live / off) ────────────────────────


class _Ev:
    def __init__(self, slot_from_ms=1751371200000, free_volume=100):
        self.slot_from_ms = slot_from_ms
        self.free_volume = free_volume
        self.source_shop_id = "51948"


class TestOnSlotFreedDispatch:
    def _stub_pipeline(self, monkeypatch, chosen):
        monkeypatch.setattr(store, "release_expired", lambda *a, **k: 0)
        monkeypatch.setattr(store, "candidates_for", lambda day, slot_from_ms=None: chosen)
        monkeypatch.setattr(allocator, "select_for_capacity", lambda c, v: c)

    def test_dry_does_not_claim_or_book(self, monkeypatch):
        self._stub_pipeline(monkeypatch, [{"id": 1, "invoice_id": 11, "shop_uzum_id": "S", "stock_id": 7, "volume": 50}])
        monkeypatch.setenv("POSTAVKA_AUTOSLOT_MODE", "dry")
        claimed_calls = []
        monkeypatch.setattr(store, "claim", lambda ids, lease_sec=30: claimed_calls.append(ids) or ids)
        monkeypatch.setattr(allocator, "_book_parallel", lambda *a, **k: (_ for _ in ()).throw(AssertionError("dry band qilmasin")))
        allocator.on_slot_freed(_Ev())
        assert claimed_calls == []   # dry — claim ham yo'q

    def test_live_books_only_claimed(self, monkeypatch):
        chosen = [{"id": 1, "invoice_id": 11, "shop_uzum_id": "S", "stock_id": 7, "volume": 50},
                  {"id": 2, "invoice_id": 22, "shop_uzum_id": "S", "stock_id": 7, "volume": 40}]
        self._stub_pipeline(monkeypatch, chosen)
        monkeypatch.setenv("POSTAVKA_AUTOSLOT_MODE", "live")
        monkeypatch.setattr(store, "claim", lambda ids, lease_sec=30: [1])  # faqat 1 claim bo'ldi
        booked = []
        monkeypatch.setattr(allocator, "_book_parallel",
                            lambda items, ms: [("booked", it) for it in items] or booked)
        monkeypatch.setattr(allocator, "_book_one", lambda it, ms: ("booked", it))
        captured = {}
        def cap(items, ms): captured["ids"] = [i["id"] for i in items]; return [("booked", it) for it in items]
        monkeypatch.setattr(allocator, "_book_parallel", cap)
        allocator.on_slot_freed(_Ev())
        assert captured["ids"] == [1]   # faqat claim qilingan (1), 2 emas

    def test_off_does_nothing(self, monkeypatch):
        self._stub_pipeline(monkeypatch, [{"id": 1, "invoice_id": 11, "shop_uzum_id": "S", "stock_id": 7, "volume": 50}])
        monkeypatch.setenv("POSTAVKA_AUTOSLOT_MODE", "off")
        monkeypatch.setattr(store, "claim", lambda *a, **k: (_ for _ in ()).throw(AssertionError("off — hech narsa")))
        allocator.on_slot_freed(_Ev())   # xato otmasligi kerak


class TestNotify:
    def _capture(self, monkeypatch):
        msgs = []
        allocator.set_notifier(msgs.append)
        monkeypatch.setattr(store, "release_expired", lambda *a, **k: 0)
        return msgs

    def test_dry_notifies_would_book(self, monkeypatch):
        msgs = self._capture(monkeypatch)
        chosen = [{"id": 1, "invoice_id": 11, "invoice_number": "A-7", "shop_uzum_id": "S", "stock_id": 7, "volume": 50}]
        monkeypatch.setattr(store, "candidates_for", lambda day, slot_from_ms=None: chosen)
        monkeypatch.setattr(allocator, "select_for_capacity", lambda c, v: c)
        monkeypatch.setenv("POSTAVKA_AUTOSLOT_MODE", "dry")
        allocator.on_slot_freed(_Ev())
        allocator.set_notifier(None)
        assert len(msgs) == 1 and "sinov" in msgs[0] and "A-7" in msgs[0]

    def test_live_notifies_on_booked(self, monkeypatch):
        msgs = self._capture(monkeypatch)
        chosen = [{"id": 1, "invoice_id": 11, "invoice_number": "A-7", "shop_uzum_id": "S", "stock_id": 7, "volume": 50}]
        monkeypatch.setattr(store, "candidates_for", lambda day, slot_from_ms=None: chosen)
        monkeypatch.setattr(allocator, "select_for_capacity", lambda c, v: c)
        monkeypatch.setattr(store, "claim", lambda ids, lease_sec=30: ids)
        monkeypatch.setattr(allocator, "_book_parallel", lambda items, ms: [("booked", it) for it in items])
        monkeypatch.setenv("POSTAVKA_AUTOSLOT_MODE", "live")
        allocator.on_slot_freed(_Ev())
        allocator.set_notifier(None)
        assert len(msgs) == 1 and "band qilindi" in msgs[0] and "A-7" in msgs[0]

    def test_live_no_notify_when_nothing_booked(self, monkeypatch):
        msgs = self._capture(monkeypatch)
        chosen = [{"id": 1, "invoice_id": 11, "invoice_number": "A-7", "shop_uzum_id": "S", "stock_id": 7, "volume": 50}]
        monkeypatch.setattr(store, "candidates_for", lambda day, slot_from_ms=None: chosen)
        monkeypatch.setattr(allocator, "select_for_capacity", lambda c, v: c)
        monkeypatch.setattr(store, "claim", lambda ids, lease_sec=30: ids)
        monkeypatch.setattr(allocator, "_book_parallel", lambda items, ms: [("failed", it) for it in items])
        monkeypatch.setenv("POSTAVKA_AUTOSLOT_MODE", "live")
        allocator.on_slot_freed(_Ev())
        allocator.set_notifier(None)
        assert msgs == []
