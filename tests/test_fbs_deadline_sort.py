"""N5 (HAR audit A.13) — deadline-aware list ordering.

``_fbs_list_order_by`` is a pure helper: it maps a status to the ORDER BY
clause used by both the per-shop and "Hammasi" list reads. Deadline-bearing
statuses must sort by their deadline ASC (soonest first); every other status
keeps ``date_created DESC``. These assertions compile each clause to SQL text
so no DB/session is required.
"""
from core.fbs_data import _fbs_list_order_by


def _first_sql(status: str) -> str:
    return str(_fbs_list_order_by(status)[0]).lower()


def test_created_sorts_by_accept_until_asc():
    # CREATED races the confirm deadline (acceptUntil), soonest first.
    sql = _first_sql("CREATED")
    assert "accept_until" in sql
    assert "asc" in sql


def test_packing_sorts_by_deliver_until_asc():
    sql = _first_sql("PACKING")
    assert "deliver_until" in sql
    assert "asc" in sql


def test_pending_delivery_and_delivering_use_deliver_until():
    assert "deliver_until" in _first_sql("PENDING_DELIVERY")
    assert "deliver_until" in _first_sql("DELIVERING")


def test_terminal_status_keeps_date_created_desc():
    # COMPLETED/CANCELED/RETURNED have no actionable deadline → newest first.
    for status in ("COMPLETED", "CANCELED", "RETURNED"):
        sql = _first_sql(status)
        assert "date_created" in sql
        assert "desc" in sql
        assert "accept_until" not in sql
        assert "deliver_until" not in sql


def test_deadline_statuses_keep_date_created_as_tiebreaker():
    # Soonest-deadline first, but ties fall back to newest-created.
    clauses = _fbs_list_order_by("CREATED")
    assert len(clauses) >= 2
    assert "date_created" in str(clauses[1]).lower()
