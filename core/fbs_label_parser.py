"""Extract customer info from Uzum FBS label PDFs.

Uzum's Seller OpenAPI never returns ``deliveryInfo`` for FBS orders —
not via the list endpoint, not via the order-detail endpoint. The only
place the customer name + city actually shows up is on the shipping
label PDF (``/v1/fbs/order/{id}/labels/print``). This module parses
that PDF and pulls out what we can.

Observed label structure (LARGE size, 685 x 472 pt):

  Line 1:  FBS                      ← order scheme
  Line 2:  Sanayev Anvar            ← customer FULL NAME  ★
  Line 3:  Д:27.05.26                ← delivery date
  Line 4:  ID: 5112565502           ← drop-off point ID (NOT customer)
  Line 5:  Ташкент                  ← city (delivery_address)  ★
  Line 6:  FrТАШ-229 10              ← internal warehouse code
  Line 7:  10807 8116               ← order ID echo

★ marked fields are what we project into FbsOrder columns.

Limitations:
  * Phone number is NEVER on the label — Uzum hides it. ``customer_phone``
    stays NULL.
  * Address is city-level only (e.g. "Ташкент"). No street/house.
  * The label only exists for orders in PACKING+ status. CREATED orders
    will not have a label to parse from.
"""
from __future__ import annotations

import io
import re

# Lines that are NEVER customer names — used to skip header / scheme rows
# when we scan top-down. Uzum prints these in different fonts, but we go
# by text content rather than font metrics (easier, more robust).
_SCHEME_TOKENS = {"FBS", "DBS", "ФБС", "ДБС"}

# Common Uzbekistan city names (Russian + Latin spellings) — used to
# detect the address line. Not exhaustive; if a label shows a city not
# in this list, we still capture it as a fallback by position.
_CITY_PATTERNS = (
    "Ташкент", "Toshkent", "Tashkent",
    "Самарканд", "Samarqand", "Samarkand",
    "Бухара", "Buxoro", "Bukhara",
    "Андижан", "Andijon", "Andijan",
    "Наманган", "Namangan",
    "Фергана", "Farg'ona", "Fergana",
    "Кашкадарья", "Qashqadaryo",
    "Сурхандарья", "Surxondaryo",
    "Хорезм", "Xorazm", "Khorezm",
    "Каракалпакстан", "Qoraqalpog'iston",
    "Джизак", "Jizzax", "Jizzakh",
    "Сырдарья", "Sirdaryo",
    "Навои", "Navoiy",
    "Чирчик", "Chirchiq",
    "Нукус", "Nukus",
    "Алмалык", "Olmaliq",
    "Ангрен", "Angren",
)


def parse_label_pdf(pdf_bytes: bytes) -> dict:
    """Pull customer name, city, and other display fields out of a label PDF.

    Returns dict with these keys (any can be ``None`` if not found)::

        customer_fullname  "Sanayev Anvar"
        delivery_address   "Ташкент"
        dropoff_id         "5112565502"
        delivery_date      "27.05.26"
        sku                "FrTAШ-229"
        quantity           "10"
        tracking_number    "10807 8116"  (raw with space)

    Missing fields come back as ``None`` rather than raising — a single
    odd label shouldn't fail the whole download.

    Multi-page labels (rare — only when the order has multiple packages)
    are handled by reading only the first page: every page contains the
    same shipping info, just with a different package counter.
    """
    out: dict = {
        "customer_fullname": None,
        "delivery_address": None,
        "dropoff_id": None,
        "delivery_date": None,
        "sku": None,
        "quantity": None,
        "tracking_number": None,
    }
    try:
        import pdfplumber  # imported lazily so the rest of the app loads even if pdfplumber is missing
    except ImportError:
        print("[Label Parser] pdfplumber not installed — skipping parse")
        return out

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            if not pdf.pages:
                return out
            text = pdf.pages[0].extract_text() or ""
    except Exception as e:
        print(f"[Label Parser] PDF open/extract failed: {e!r}")
        return out

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return out

    # ── Customer name ───────────────────────────────────────────────
    # First non-scheme line that doesn't start with "ID:", "Д:", or a
    # digit is the customer name. In every label observed so far it
    # sits on line 2 (right after "FBS"/"DBS"), but skipping by content
    # is more robust to layout drift.
    for ln in lines:
        if ln in _SCHEME_TOKENS:
            continue
        if ln.startswith("ID:") or ln.startswith("ID :"):
            continue
        if ln.startswith("Д:") or ln.startswith("D:"):
            continue
        if ln[0].isdigit():
            continue
        # Skip city candidates — they'd otherwise consume the name slot
        # on layouts where the customer line is missing.
        if any(c in ln for c in _CITY_PATTERNS):
            continue
        # Skip internal warehouse codes like "FrТАШ-229 10" — those
        # carry a digit segment that the city skip doesn't catch.
        if re.search(r"\d", ln):
            continue
        out["customer_fullname"] = ln
        break

    # ── Delivery address (city) ─────────────────────────────────────
    # Match any known city name appearing anywhere on the label. We
    # store the matching substring, not the whole line, so labels that
    # happen to print "г. Ташкент" still normalize to "Ташкент".
    for ln in lines:
        for city in _CITY_PATTERNS:
            if city in ln:
                out["delivery_address"] = city
                break
        if out["delivery_address"]:
            break

    # ── Drop-off point ID ────────────────────────────────────────────
    # Line starting with "ID:" — Uzum's drop-off / pickup point ID,
    # NOT the customer phone. Displayed in big text on the label.
    for ln in lines:
        m = re.match(r"^ID\s*:\s*(\S+)", ln)
        if m:
            out["dropoff_id"] = m.group(1).strip()
            break

    # ── Delivery date ────────────────────────────────────────────────
    # "Д:DD.MM.YY" or "D:DD.MM.YY".
    for ln in lines:
        m = re.match(r"^[ДD]\s*:\s*(\d{1,2}\.\d{1,2}\.\d{2,4})", ln)
        if m:
            out["delivery_date"] = m.group(1).strip()
            break

    # ── SKU + quantity ──────────────────────────────────────────────
    # A line that has a non-digit-leading token followed by an integer,
    # e.g. "FrТАШ-229 10". The first token is the SKU, the trailing
    # integer is the quantity.
    for ln in lines:
        # Skip lines we've already classified.
        if ln in _SCHEME_TOKENS:
            continue
        if ln.startswith("ID:") or ln.startswith("ID :"):
            continue
        if ln.startswith("Д:") or ln.startswith("D:"):
            continue
        # Looking for "<sku> <qty>" pattern.
        m = re.match(r"^(\S+)\s+(\d{1,4})\s*$", ln)
        if m:
            sku_candidate = m.group(1)
            # SKU must not be all digits (that'd be a tracking number).
            if not sku_candidate.isdigit():
                out["sku"] = sku_candidate
                out["quantity"] = m.group(2)
                break

    # ── Tracking number ──────────────────────────────────────────────
    # The "NNNNN NNNN" pattern (5 digits + space + 4 digits) shown at
    # bottom-right of the label, also encoded in the QR. Last 4 digits
    # are visually highlighted on the label.
    for ln in lines:
        m = re.search(r"\b(\d{4,6})\s+(\d{3,5})\b", ln)
        if m:
            out["tracking_number"] = f"{m.group(1)} {m.group(2)}"
            break

    return out


__all__ = ["parse_label_pdf"]
