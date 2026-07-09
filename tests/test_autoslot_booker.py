"""Mocked tests for the avto-band BOOKER (yakuniy reja — detektorsiz).

Covers the pure slot-window pick, the single-attempt booking state machine
(`attempt_booking` — GET unlimited, SET exactly once), the hourly digest
formatter, and a 50-invoice 10ms non-blocking scheduling SIMULATION with fake
300–400ms requests (mirrors `app._postavka_booking_loop`).

No DB, no network — `postavki.client` is monkeypatched.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone, timedelta, date

import requests

from postavki.autoslot import booker

_TZ = timezone(timedelta(hours=5))


def _ms(y, mo, d, h=10, mi=0) -> int:
    return int(datetime(y, mo, d, h, mi, tzinfo=_TZ).timestamp() * 1000)


TODAY = date(2026, 7, 9)
MIN_D = date(2026, 7, 9)
MAX_D = date(2026, 7, 12)


def _plan(**over) -> dict:
    p = {
        "id": 1, "shop_uzum_id": "51948", "invoice_id": 1001,
        "invoice_number": "N1001", "pool_source": "FULLFILMENT",
        "stock_id": 34, "min_date": MIN_D, "max_date": MAX_D,
    }
    p.update(over)
    return p


# ── pick_slot_in_window (TOZA) ───────────────────────────────────────────────


class TestPickSlotInWindow:
    def test_earliest_in_window(self):
        slots = [
            {"timeFrom": _ms(2026, 7, 11, 16)},
            {"timeFrom": _ms(2026, 7, 10, 9)},   # earliest in [9..12]
            {"timeFrom": _ms(2026, 7, 12, 8)},
        ]
        assert booker.pick_slot_in_window(slots, MIN_D, MAX_D) == _ms(2026, 7, 10, 9)

    def test_ignores_before_min_and_after_max(self):
        slots = [
            {"timeFrom": _ms(2026, 7, 8, 8)},    # before min → skip
            {"timeFrom": _ms(2026, 7, 13, 8)},   # after max → skip
            {"timeFrom": _ms(2026, 7, 11, 18)},  # only valid
        ]
        assert booker.pick_slot_in_window(slots, MIN_D, MAX_D) == _ms(2026, 7, 11, 18)

    def test_none_when_no_slot_in_window(self):
        slots = [{"timeFrom": _ms(2026, 7, 8, 8)}, {"timeFrom": _ms(2026, 7, 13, 8)}]
        assert booker.pick_slot_in_window(slots, MIN_D, MAX_D) is None

    def test_null_bounds_accept_any(self):
        slots = [{"timeFrom": _ms(2026, 7, 30, 8)}]
        assert booker.pick_slot_in_window(slots, None, None) == _ms(2026, 7, 30, 8)


# ── attempt_booking state machine ────────────────────────────────────────────


class _Client:
    """Configurable stand-in for postavki.client (per-test)."""
    def __init__(self, slots=None, get_exc=None, set_exc=None, set_ret=None,
                 verify=False):
        self.slots = slots or []
        self.get_exc = get_exc
        self.set_exc = set_exc
        self.set_ret = set_ret if set_ret is not None else {"id": 1001, "dateUpdated": 1}
        self.verify = verify
        self.get_calls = 0
        self.set_calls = 0

    def invoice_time_slots(self, shop, inv_id, pool):
        self.get_calls += 1
        if self.get_exc:
            raise self.get_exc
        return self.slots

    def set_time_slot(self, shop, inv_id, tf, stock, pool):
        self.set_calls += 1
        if self.set_exc:
            raise self.set_exc
        return self.set_ret

    def list_invoices(self, shop, page=0, size=100, statuses=""):
        # used by verify_booked on SET timeout
        return [{"id": 1001, "timeSlotReservation": ({"x": 1} if self.verify else None)}]


def _wire(monkeypatch, c: _Client):
    monkeypatch.setattr(booker.client, "invoice_time_slots", c.invoice_time_slots)
    monkeypatch.setattr(booker.client, "set_time_slot", c.set_time_slot)
    monkeypatch.setattr(booker.client, "list_invoices", c.list_invoices)


class TestAttemptBooking:
    def test_success_books_and_sets_once(self, monkeypatch):
        c = _Client(slots=[{"timeFrom": _ms(2026, 7, 10, 9)}])
        _wire(monkeypatch, c)
        res = booker.attempt_booking(_plan(), mode="live", today=TODAY)
        assert res["outcome"] == booker.BOOKED
        assert res["slot_from_ms"] == _ms(2026, 7, 10, 9)
        assert c.set_calls == 1          # SET fired EXACTLY once
        assert booker.is_terminal(res["outcome"])

    def test_no_slot_stays_queued_no_set(self, monkeypatch):
        c = _Client(slots=[{"timeFrom": _ms(2026, 7, 8, 9)}])  # before window
        _wire(monkeypatch, c)
        res = booker.attempt_booking(_plan(), mode="live", today=TODAY)
        assert res["outcome"] == booker.NO_SLOT
        assert c.set_calls == 0          # never touches SET budget
        assert not booker.is_terminal(res["outcome"])

    def test_expired_skips_get_and_set(self, monkeypatch):
        c = _Client(slots=[{"timeFrom": _ms(2026, 7, 10, 9)}])
        _wire(monkeypatch, c)
        res = booker.attempt_booking(_plan(), mode="live", today=date(2026, 7, 13))
        assert res["outcome"] == booker.EXPIRED
        assert c.get_calls == 0 and c.set_calls == 0
        assert booker.is_failed_terminal(res["outcome"])

    def test_dry_does_not_set(self, monkeypatch):
        c = _Client(slots=[{"timeFrom": _ms(2026, 7, 10, 9)}])
        _wire(monkeypatch, c)
        res = booker.attempt_booking(_plan(), mode="dry", today=TODAY)
        assert res["outcome"] == booker.DRY
        assert c.set_calls == 0

    def test_get_timeout_is_transient(self, monkeypatch):
        c = _Client(get_exc=requests.Timeout("slow"))
        _wire(monkeypatch, c)
        res = booker.attempt_booking(_plan(), mode="live", today=TODAY)
        assert res["outcome"] == booker.GET_TIMEOUT
        assert booker.is_timeout(res["outcome"])
        assert not booker.is_terminal(res["outcome"])   # requeued
        assert c.set_calls == 0

    def test_get_network_error_is_transient(self, monkeypatch):
        c = _Client(get_exc=requests.ConnectionError("net"))
        _wire(monkeypatch, c)
        res = booker.attempt_booking(_plan(), mode="live", today=TODAY)
        assert res["outcome"] == booker.GET_ERROR
        assert not booker.is_terminal(res["outcome"])
        assert c.set_calls == 0

    def test_set_timeout_verify_booked(self, monkeypatch):
        c = _Client(slots=[{"timeFrom": _ms(2026, 7, 10, 9)}],
                    set_exc=requests.Timeout("slow set"), verify=True)
        _wire(monkeypatch, c)
        res = booker.attempt_booking(_plan(), mode="live", today=TODAY)
        # SET timed out but verify shows the invoice DID get a slot → booked.
        assert res["outcome"] == booker.BOOKED
        assert c.set_calls == 1          # still only one SET attempt

    def test_set_timeout_verify_not_booked_is_terminal(self, monkeypatch):
        c = _Client(slots=[{"timeFrom": _ms(2026, 7, 10, 9)}],
                    set_exc=requests.Timeout("slow set"), verify=False)
        _wire(monkeypatch, c)
        res = booker.attempt_booking(_plan(), mode="live", today=TODAY)
        assert res["outcome"] == booker.SET_TIMEOUT
        assert booker.is_timeout(res["outcome"])
        assert booker.is_failed_terminal(res["outcome"])   # NOT re-SET
        assert c.set_calls == 1

    def test_set_empty_response_failed(self, monkeypatch):
        c = _Client(slots=[{"timeFrom": _ms(2026, 7, 10, 9)}], set_ret={})
        _wire(monkeypatch, c)
        res = booker.attempt_booking(_plan(), mode="live", today=TODAY)
        assert res["outcome"] == booker.SET_FAILED
        assert booker.is_failed_terminal(res["outcome"])

    def test_no_stock_terminal(self, monkeypatch):
        c = _Client(slots=[{"timeFrom": _ms(2026, 7, 10, 9)}])
        _wire(monkeypatch, c)
        res = booker.attempt_booking(_plan(stock_id=None), mode="live", today=TODAY)
        assert res["outcome"] == booker.NO_STOCK
        assert c.set_calls == 0

    def test_never_raises_on_bad_api(self, monkeypatch):
        c = _Client(get_exc=ValueError("weird payload"))
        _wire(monkeypatch, c)
        res = booker.attempt_booking(_plan(), mode="live", today=TODAY)
        assert res["outcome"] == booker.GET_ERROR   # swallowed, not raised


# ── Hourly digest formatter ──────────────────────────────────────────────────


class TestDigest:
    def test_no_problems_message(self):
        txt = booker.format_booking_digest(
            _ms(2026, 7, 9, 15), {"attempted": 5, "booked": 5, "failed": 0, "timed_out": 0}, [])
        assert "Muammoli invoice yo'q" in txt
        assert "Band bo'ldi: *5*" in txt

    def test_would_book_message(self):
        txt = booker.format_would_book(
            _plan(invoice_number="1100037133586", volume=37), _ms(2026, 7, 10, 9))
        assert "Band qilardim" in txt
        assert "№1100037133586" in txt
        assert "37 dona" in txt
        assert "band qilinmadi" in txt   # aniq sinov ekanini bildiradi

    def test_lists_problem_invoices(self):
        probs = [{"invoice_number": "N7", "attempts": 4, "timeouts": 2,
                  "failures": 1, "last_error": "SET timeout: Timeout"}]
        txt = booker.format_booking_digest(
            _ms(2026, 7, 9, 15),
            {"attempted": 10, "booked": 9, "failed": 1, "timed_out": 2}, probs)
        assert "№N7" in txt
        assert "timeout 2" in txt
        assert "SET timeout" in txt


# ── 50-invoice, 10ms non-blocking SIMULATION (mirrors _postavka_booking_loop) ──


class TestSchedulerSimulation:
    def test_50_invoices_10ms_nonblocking(self, monkeypatch):
        """50 invoice, har 10ms bitta SUBMIT (non-blocking), fake 300–400ms
        so'rov. Tekshiradi: (1) hammasi band bo'ladi, (2) har invoice SET
        AYNAN 1 marta (dubl booking yo'q), (3) bir invoice bir vaqtda faqat 1
        faol so'rov (single-flight), (4) non-blocking (jami vaqt ketma-ketдан
        ancha kam)."""
        from concurrent.futures import ThreadPoolExecutor

        N = 50
        SLOT = _ms(2026, 7, 10, 9)
        active: dict[int, int] = {}       # invoice_id -> hozirgi parallel so'rov
        active_lock = threading.Lock()
        max_conc = {"v": 0}
        set_calls: dict[int, int] = {}
        # inv_id%3==0 lar dastlab 2 marta «slot yo'q» qaytaradi (requeue yo'lini
        # sinash uchun — shu invoice qayta-qayta jadvalga tushadi).
        get_rounds: dict[int, int] = {}

        def fake_get(shop, inv_id, pool):
            with active_lock:
                active[inv_id] = active.get(inv_id, 0) + 1
                if active[inv_id] > max_conc["v"]:
                    max_conc["v"] = active[inv_id]
                assert active[inv_id] == 1, f"single-flight buzildi: inv {inv_id}"
            try:
                time.sleep(0.30 + (inv_id % 10) * 0.01)   # 300–390ms
                get_rounds[inv_id] = get_rounds.get(inv_id, 0) + 1
                if inv_id % 3 == 0 and get_rounds[inv_id] <= 2:
                    return [{"timeFrom": _ms(2026, 7, 8, 9)}]   # oynadan tashqari → no_slot
                return [{"timeFrom": SLOT, "timeTo": SLOT + 3600000}]
            finally:
                with active_lock:
                    active[inv_id] -= 1

        def fake_set(shop, inv_id, tf, stock, pool):
            set_calls[inv_id] = set_calls.get(inv_id, 0) + 1
            return {"id": inv_id, "dateUpdated": 1}

        monkeypatch.setattr(booker.client, "invoice_time_slots", fake_get)
        monkeypatch.setattr(booker.client, "set_time_slot", fake_set)

        plans = {i: _plan(id=i, invoice_id=2000 + i, invoice_number=f"N{i}")
                 for i in range(N)}
        order = list(plans)
        inflight: set[int] = set()
        pending: list = []
        booked: set[int] = set()
        rr = {"i": 0}
        submit_ts: list[float] = []

        def next_plan():
            n = len(order)
            for _ in range(n):
                pid = order[rr["i"] % n]
                rr["i"] += 1
                if pid in inflight or pid not in plans:
                    continue
                return plans[pid]
            return None

        pool = ThreadPoolExecutor(max_workers=64, thread_name_prefix="simbook")
        start = time.monotonic()
        with pool:
            while len(booked) < N and time.monotonic() - start < 30:
                # SCHEDULE — bitta SUBMIT (non-blocking).
                if len(inflight) < 100:
                    p = next_plan()
                    if p is not None:
                        pid = p["id"]
                        inflight.add(pid)
                        submit_ts.append(time.monotonic())
                        fut = pool.submit(booker.attempt_booking, p, mode="live", today=TODAY)
                        pending.append((fut, pid))
                # REAP — tugaganlarni qayta ishla.
                stilldone = [it for it in pending if it[0].done()]
                pending = [it for it in pending if not it[0].done()]
                for fut, pid in stilldone:
                    inflight.discard(pid)
                    res = fut.result()
                    if res["outcome"] == booker.BOOKED:
                        booked.add(pid)
                        plans.pop(pid, None)
                    # no_slot → navbatда qoladi (plans'дан olib tashlanmaydi)
                time.sleep(0.01)

        # (1) hammasi band
        assert len(booked) == N, f"faqat {len(booked)}/{N} band bo'ldi"
        # (2) har invoice SET aynan 1 marta (dubl booking yo'q)
        assert all(v == 1 for v in set_calls.values()), f"dubl SET: {set_calls}"
        assert len(set_calls) == N
        # (3) single-flight — hech qachon bir invoice 2 parallel so'rov
        assert max_conc["v"] == 1, f"parallel dubl so'rov: {max_conc['v']}"
        # (4) non-blocking: ketma-ket bo'lsa ≥ N*0.3=15s ketardi; parallel → << 15s
        assert time.monotonic() - start < 12, "loop bloklanyapti (non-blocking emas)"
        # 10ms cadence: dastlabki SUBMIT'lar ~10ms oralig'ida (bloklanmaydi)
        if len(submit_ts) > 10:
            gaps = [submit_ts[i + 1] - submit_ts[i] for i in range(10)]
            assert max(gaps) < 0.2, "SUBMIT bloklanyapti (10ms cadence buzildi)"
