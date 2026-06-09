"""Mocked tests for sellerId auto-detection in ``fbs.routes`` — 2026-06-02.

``POST /v1/fbs/invoice`` needs Uzum's ``sellerId`` (the ``?sId=N`` from the
cabinet URL), which NO seller/profile OpenAPI endpoint exposes — but every
``GET /v1/finance/expenses`` row carries it (verified live: sellerId=95673).
These helpers read it from the seller's OWN finance data and persist it, so
the seller never pastes it manually. Because this feeds the invoice-create
MUTATION path, we prove the logic with mocks BEFORE prod — no Uzum HTTP, no
DB.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from fbs.routes import _detect_seller_id_from_finance, _ensure_seller_id


def _expenses_body(*sellerids):
    """A ``/v1/finance/expenses`` body with one payment row per id."""
    return {"payload": {"payments": [{"sellerId": s, "amount": 1} for s in sellerids]}}


class _FakeUser:
    def __init__(self, uzum_seller_id=None, uid=7):
        self.id = uid
        self.uzum_seller_id = uzum_seller_id


class TestDetectFromFinance:
    """``_detect_seller_id_from_finance`` — reads sellerId from a live
    finance page (mocked here). Read-only; never raises out."""

    def test_reads_sellerid_from_first_row(self):
        with patch("core.uzum_openapi.fetch_finance_expenses_page",
                   return_value=_expenses_body(95673)) as m:
            sid = _detect_seller_id_from_finance("tok", [5983])
        assert sid == 95673
        assert m.call_count == 1                      # stops at first hit

    def test_empty_payments_returns_none(self):
        with patch("core.uzum_openapi.fetch_finance_expenses_page",
                   return_value={"payload": {"payments": []}}):
            assert _detect_seller_id_from_finance("tok", [5983]) is None

    def test_coerces_string_sellerid_to_int(self):
        with patch("core.uzum_openapi.fetch_finance_expenses_page",
                   return_value=_expenses_body("95673")):
            assert _detect_seller_id_from_finance("tok", [5983]) == 95673

    def test_skips_failing_shop_and_tries_next(self):
        # First shop errors (e.g. 403 / no finance scope) → try the next.
        with patch("core.uzum_openapi.fetch_finance_expenses_page",
                   side_effect=[RuntimeError("403"), _expenses_body(95673)]) as m:
            sid = _detect_seller_id_from_finance("tok", [5983, 10945])
        assert sid == 95673
        assert m.call_count == 2

    def test_no_shops_never_calls_api(self):
        with patch("core.uzum_openapi.fetch_finance_expenses_page") as m:
            assert _detect_seller_id_from_finance("tok", []) is None
        m.assert_not_called()

    def test_null_sellerid_row_is_skipped(self):
        with patch("core.uzum_openapi.fetch_finance_expenses_page",
                   return_value={"payload": {"payments": [{"sellerId": None}, {"sellerId": 95673}]}}):
            assert _detect_seller_id_from_finance("tok", [5983]) == 95673


class TestEnsureSellerId:
    """``_ensure_seller_id`` — returns the stored id, or detects+persists
    it, or None (caller then shows the manual fallback prompt)."""

    def test_returns_existing_without_fetching(self):
        user = _FakeUser(uzum_seller_id=95673)
        db = MagicMock()
        with patch("fbs.routes._detect_seller_id_from_finance") as det:
            sid = _ensure_seller_id(db, user, "tok", [5983])
        assert sid == 95673
        det.assert_not_called()                       # no needless finance call
        db.commit.assert_not_called()

    def test_detects_persists_and_returns(self):
        user = _FakeUser(uzum_seller_id=None)
        db = MagicMock()
        with patch("fbs.routes._detect_seller_id_from_finance", return_value=95673) as det:
            sid = _ensure_seller_id(db, user, "tok", [5983])
        assert sid == 95673
        assert user.uzum_seller_id == 95673           # written onto the model
        db.commit.assert_called_once()                # and persisted
        det.assert_called_once_with("tok", [5983])

    def test_none_user_returns_none(self):
        assert _ensure_seller_id(MagicMock(), None, "tok", [5983]) is None

    def test_empty_token_returns_none_without_fetch(self):
        user = _FakeUser(uzum_seller_id=None)
        db = MagicMock()
        with patch("fbs.routes._detect_seller_id_from_finance") as det:
            assert _ensure_seller_id(db, user, "", [5983]) is None
        det.assert_not_called()

    def test_no_shops_returns_none_without_fetch(self):
        user = _FakeUser(uzum_seller_id=None)
        db = MagicMock()
        with patch("fbs.routes._detect_seller_id_from_finance") as det:
            assert _ensure_seller_id(db, user, "tok", []) is None
        det.assert_not_called()

    def test_detect_failure_leaves_user_untouched(self):
        user = _FakeUser(uzum_seller_id=None)
        db = MagicMock()
        with patch("fbs.routes._detect_seller_id_from_finance", return_value=None):
            sid = _ensure_seller_id(db, user, "tok", [5983])
        assert sid is None
        assert user.uzum_seller_id is None
        db.commit.assert_not_called()
