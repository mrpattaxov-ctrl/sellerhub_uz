"""Tests for the change-pickup → fbs_orders DB reflection (2026-06-13).

BUG: after a successful ``POST /fbs/api/invoices/<id>/change-pickup`` the
``fbs_orders.drop_off_point_*`` columns kept the OLD point forever:

  * the route only invalidated the akt-PDF cache, never touched the rows;
  * the sync worker can't repair it — a pickup change keeps the order
    status, so the incremental ``stop_on_known`` short-circuit never
    re-reads those rows (and Uzum's order payload may keep echoing the
    original point anyway, like the immutable label).

Every view fed by those columns («Mening punktlarim» in the drop-off
modal, order cards) therefore showed the old point.

FIX: the mutation response payload (the authority for what the invoice
now points at) is mirrored into ``fbs_orders`` right after the move.
Per the action-endpoint policy these tests prove the wiring with mocks —
no Postgres, no Uzum HTTP, no real invoice gets moved.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import fbs.routes as routes
from fbs.routes import _pickup_update_values, _reflect_pickup_change_in_db


FULL_PAYLOAD = {
    "id": 327497,
    "number": 120001009487,
    "status": {"value": "CREATED"},
    "dropOffPoint": {
        "uuid": "dop-uuid-new",
        "address": "Toshkent sh., Olmazor tumani, Tansikboyev ko'chasi, 1-uy",
    },
    "timeSlot": {"from": "x", "to": "y"},
}


class TestPickupUpdateValues:
    """Pure projection: response payload → fbs_orders column values."""

    def test_full_payload_projects_all_three(self):
        assert _pickup_update_values(FULL_PAYLOAD) == {
            "drop_off_point_uuid": "dop-uuid-new",
            "drop_off_point_address":
                "Toshkent sh., Olmazor tumani, Tansikboyev ko'chasi, 1-uy",
            "invoice_number": "120001009487",
        }

    def test_int_number_coerced_to_str(self):
        # invoice_number is a VARCHAR column AND the ownership-guard key —
        # an int here would silently break the IN-match.
        vals = _pickup_update_values({"number": 120001009487})
        assert vals == {"invoice_number": "120001009487"}

    def test_partial_dop_never_nulls_missing_fields(self):
        # uuid present, address absent → only uuid lands; a partial
        # response must not blank columns we already have.
        vals = _pickup_update_values(
            {"dropOffPoint": {"uuid": "u-1", "address": ""}})
        assert vals == {"drop_off_point_uuid": "u-1"}

    def test_empty_payload_yields_no_writes(self):
        assert _pickup_update_values({}) == {}
        assert _pickup_update_values(None) == {}
        assert _pickup_update_values({"dropOffPoint": None}) == {}


def _mock_session(rowcount=3):
    """``with SessionLocal() as db:`` capturing the executed statement."""
    db = MagicMock(name="db")
    db.execute.return_value = MagicMock(rowcount=rowcount)
    session = MagicMock(name="SessionLocal")
    session.return_value.__enter__.return_value = db
    session.return_value.__exit__.return_value = False
    return session, db


class TestReflectPickupChangeInDb:
    """The UPDATE wiring — gated, scoped, best-effort."""

    def test_happy_path_updates_scoped_rows(self, monkeypatch):
        session, db = _mock_session(rowcount=2)
        monkeypatch.setattr(routes, "SessionLocal", session)

        rows = _reflect_pickup_change_in_db(
            FULL_PAYLOAD, invoice_id=327497,
            order_ids=[11122, 11123], user_shops=["5064"],
        )

        assert rows == 2
        db.commit.assert_called_once()
        stmt = db.execute.call_args.args[0]
        compiled = stmt.compile()
        # New point + number land in SET …
        assert compiled.params["drop_off_point_uuid"] == "dop-uuid-new"
        assert compiled.params["invoice_number"] == "120001009487"
        # … and the WHERE stays scoped to the user's shops + the invoice's
        # own orders, coerced to str (order_id/shop_id are VARCHAR — an
        # int IN-list silently matches nothing on Postgres).
        sql = str(compiled)
        assert "shop_id IN" in sql and "order_id IN" in sql
        in_lists = [v for v in compiled.params.values() if isinstance(v, list)]
        assert ["11122", "11123"] in in_lists
        assert ["5064"] in in_lists

    def test_no_orders_skips_db_entirely(self, monkeypatch):
        session, db = _mock_session()
        monkeypatch.setattr(routes, "SessionLocal", session)
        assert _reflect_pickup_change_in_db(
            FULL_PAYLOAD, invoice_id=1, order_ids=[], user_shops=["5064"],
        ) == 0
        session.assert_not_called()

    def test_empty_payload_skips_db_entirely(self, monkeypatch):
        session, db = _mock_session()
        monkeypatch.setattr(routes, "SessionLocal", session)
        assert _reflect_pickup_change_in_db(
            {}, invoice_id=1, order_ids=[11122], user_shops=["5064"],
        ) == 0
        session.assert_not_called()

    def test_db_failure_is_swallowed(self, monkeypatch):
        # The Uzum-side move already succeeded — a local mirror failure
        # must never turn the request into an error.
        session = MagicMock(side_effect=RuntimeError("pg down"))
        monkeypatch.setattr(routes, "SessionLocal", session)
        assert _reflect_pickup_change_in_db(
            FULL_PAYLOAD, invoice_id=1, order_ids=[11122], user_shops=["5064"],
        ) == 0
