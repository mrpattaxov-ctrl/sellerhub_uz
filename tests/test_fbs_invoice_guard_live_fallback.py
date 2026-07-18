"""Invoice by-id guard — LIVE ownership fallback (2026-07-13).

The by-id endpoints (detail / akt / change-pickup) proved ownership only from
``fbs_orders``: the invoice's number in ``fbs_orders.invoice_number``, or one of
its order ids present locally. Both signals only know about orders WE synced —
so a накладная the seller built in the Uzum app (PENDING_DELIVERY is never
background-synced), or one whose local rows were lost, has NO local trace. The
seller's own invoice then showed up in the LIST (that path already consults
Uzum) but 403'd as "foreign shop" when opened.

The guard now asks Uzum before denying: one batched PENDING_DELIVERY drain for
the user's OWN shopIds yields both the invoice numbers and the order ids that
genuinely belong to them. A foreign invoice is still denied — it is absent from
that evidence precisely because it belongs to another shop.

The order-id half matters on its own: prod logs show Uzum's invoice payload can
carry ``number=None``, which leaves the number check with nothing to match.
"""
from __future__ import annotations

from unittest.mock import patch

import fbs.routes as routes
from fbs.routes import _live_owned_invoice_evidence, _user_owns_invoice

SHOPS = ["5983", "10945"]

# Uzum's PENDING_DELIVERY payload: two of the seller's orders, one from a shop
# on the same Uzum account that the seller never registered here.
LIVE_PD_ORDERS = [
    {"id": "116598999", "shopId": "10945", "invoiceNumber": "120001141434"},
    {"id": "116312451", "shopId": "5983",  "invoiceNumber": "120001136012"},
    {"id": "999999999", "shopId": "77777", "invoiceNumber": "120009999999"},
]


class TestLiveOwnedInvoiceEvidence:
    def test_projects_numbers_and_order_ids_scoped_to_own_shops(self):
        with patch("core.fbs_sync.fetch_all_pages", return_value=LIVE_PD_ORDERS):
            numbers, order_ids = _live_owned_invoice_evidence("tok", SHOPS)
        assert numbers == {"120001141434", "120001136012"}
        assert order_ids == {"116598999", "116312451"}
        # The unregistered shop's order never enters the evidence.
        assert "999999999" not in order_ids
        assert "120009999999" not in numbers

    def test_uzum_failure_returns_empty_evidence(self):
        # Best-effort: a hiccup must degrade to the DB-only decision, never to
        # an accidental ALLOW.
        with patch("core.fbs_sync.fetch_all_pages", side_effect=RuntimeError("429")):
            assert _live_owned_invoice_evidence("tok", SHOPS) == (set(), set())

    def test_no_token_or_no_shops_makes_no_call(self):
        with patch("core.fbs_sync.fetch_all_pages") as fetch:
            assert _live_owned_invoice_evidence("", SHOPS) == (set(), set())
            assert _live_owned_invoice_evidence("tok", []) == (set(), set())
        fetch.assert_not_called()


def _guard(inv_orders, *, invoice_number, db_numbers=frozenset(),
           db_order_ids=frozenset(), live=LIVE_PD_ORDERS):
    """Run the guard with the DB signals stubbed out and Uzum stubbed in."""
    with patch.object(routes, "_owned_invoice_numbers", return_value=set(db_numbers)), \
         patch.object(routes, "_live_owned_invoice_evidence") as live_ev, \
         patch.object(routes, "SessionLocal") as session:
        # DB order-id lookup → whatever db_order_ids says.
        session.return_value.__enter__.return_value.execute.return_value.all.return_value = [
            (str(i),) for i in db_order_ids
        ]
        allowed = set(SHOPS)
        live_ev.return_value = (
            {o["invoiceNumber"] for o in live if o["shopId"] in allowed},
            {o["id"] for o in live if o["shopId"] in allowed},
        )
        owns, _ = _user_owns_invoice(
            "tok", 1141434, SHOPS,
            invoice_number=invoice_number, inv_orders=inv_orders,
        )
    return owns


class TestGuardLiveFallback:
    OWN_INVOICE_ORDERS = [{"orderId": "116598999"}]
    FOREIGN_INVOICE_ORDERS = [{"orderId": "999999999"}]

    def test_own_invoice_with_no_local_trace_is_allowed(self):
        # The exact production failure: created in the Uzum app → nothing in
        # fbs_orders → both DB signals miss. Uzum says the order is ours.
        assert _guard(self.OWN_INVOICE_ORDERS, invoice_number="120001141434") is True

    def test_own_invoice_is_allowed_even_when_uzum_sends_no_number(self):
        # number=None (seen in prod logs) → the number check has nothing to
        # match; the live ORDER-ID evidence must carry the decision.
        assert _guard(self.OWN_INVOICE_ORDERS, invoice_number=None) is True

    def test_foreign_invoice_is_still_denied(self):
        # Same Uzum account, shop the seller never registered here → absent from
        # the live evidence → deny. This is the boundary the guard exists for.
        assert _guard(self.FOREIGN_INVOICE_ORDERS,
                      invoice_number="120009999999") is False

    def test_uzum_failure_keeps_the_deny(self):
        # Fail closed: no live evidence and no DB trace → deny.
        assert _guard(self.FOREIGN_INVOICE_ORDERS, invoice_number="120009999999",
                      live=[]) is False

    def test_db_hit_short_circuits_without_the_live_call(self):
        # The fast local path must not pay for a Uzum call.
        with patch.object(routes, "_owned_invoice_numbers",
                          return_value={"120001141434"}), \
             patch.object(routes, "_live_owned_invoice_evidence") as live_ev:
            owns, _ = _user_owns_invoice(
                "tok", 1141434, SHOPS,
                invoice_number="120001141434", inv_orders=self.OWN_INVOICE_ORDERS,
            )
        assert owns is True
        live_ev.assert_not_called()
