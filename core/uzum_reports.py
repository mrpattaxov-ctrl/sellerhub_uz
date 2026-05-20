"""Uzum Reports API wrappers (POST /api/seller/documents/v2).

Four-step async flow per project_uzum_reports_fetch.md:
  1. Create — POST /api/seller/documents/v2 with the exact working body.
  2. Poll   — GET  /api/seller/documents/v2/{requestId} until COMPLETED.
  3. Download — GET the file URL returned by the poll.
  4. Parse   — CSV by default; XLSX fallback by sniffing PK magic bytes.

All HTTP goes through ``core.http_client`` (AdditiveBackoffRetry 60/120/180s,
shared session pool). Bearer token is injected lazily via the callable passed
into ``http_json`` so this module stays decoupled from the Flask app.

SELLS_REPORT ALWAYS uses ``group=false`` — see §1 of the coder non-negotiables.
``contentType`` is ALWAYS ``"CSV"``; the XLSX branch of ``_parse_bytes`` exists
only as a defensive fallback if Uzum ever ships a mis-typed response.
"""
from __future__ import annotations

import csv
import io
import time
import uuid
from typing import Callable, Iterable, Sequence

from core.http_client import http_json as _raw_http_json

# ── Endpoint constants ───────────────────────────────────────────────
# Create is POST-only at /documents/create; poll/list live under /documents/v2.
# Posting to /documents/v2 returns HTTP 405 — seller dashboard uses /create.
_CREATE_URL = "https://api-seller.uzum.uz/api/seller/documents/create"
_BASE = "https://api-seller.uzum.uz/api/seller/documents/v2"

# Seller-portal headers expected by Uzum on create/poll.
_REPORT_HEADERS = {
    "Origin": "https://seller.uzum.uz",
    "Referer": "https://seller.uzum.uz/",
}

# Adaptive poll schedule (seconds). Past the last entry we repeat 30s until
# the wall-clock deadline is hit.
_POLL_SCHEDULE: tuple[float, ...] = (1.0, 3.0, 10.0, 30.0)

# ── RU header → canonical field map ──────────────────────────────────
_SELLS_HEADER_MAP: dict[str, str] = {
    "Статус": "status",
    "Дата создания": "created_at",
    "Дата получения": "received_at",
    "№ заказа": "order_id",
    "Штрихкод": "barcode",
    "SKU": "sku_id",
    "Наименование": "sku_title",
    "Категория": "category",
    "Количество": "qty",
    "Возвраты": "qty_returns",
    "Выручка (сумы)": "revenue",
    "Выручка с вычетом комиссии и логистики (сумы)": "seller_profit",
    "Комиссия маркетплейса (сумы)": "commission",
    "Цена (сумы)": "unit_price",
    "Промокод (сумы)": "promo_amount",
    "Себестоимость (сумы)": "purchase_price",
    "Логистический сбор": "logistics_fee",
}

_EXPENSES_HEADER_MAP: dict[str, str] = {
    "Источник": "source",
    "Услуга": "service",
    "Статус": "status",
    "ID операции": "operation_id",
    "Дата списания": "charged_at",
    "Стоимость (сумы)": "unit_cost",
    "Количество": "qty",
    "Сумма (сумы)": "amount",
    "Тип операции": "op_type",
}


# ── Internal helpers ─────────────────────────────────────────────────

def _http_json(
    url: str,
    method: str = "GET",
    body: dict | None = None,
    headers: dict | None = None,
    *,
    token_getter: Callable[[], str | None] | None = None,
) -> dict:
    """Call the shared http_json with Uzum report headers + token injection."""
    merged = dict(_REPORT_HEADERS)
    if headers:
        merged.update(headers)
    return _raw_http_json(
        url,
        method=method,
        body=body,
        headers=merged,
        _get_admin_token=token_getter,
    )


def _extract_download_link(resp: dict | None) -> str | None:
    """Pick out the file URL returned by the Uzum poll endpoint.

    Uzum has returned the link under a handful of field names over time; accept
    any of them so we're resilient to harmless API shape drift.
    """
    if not isinstance(resp, dict):
        return None
    payload = resp.get("payload") if isinstance(resp.get("payload"), dict) else resp
    if not isinstance(payload, dict):
        return None
    for key in ("link", "fileUrl", "url", "downloadUrl", "downloadLink", "resultUrl"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    # Nested file object shape.
    for key in ("file", "result"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            lnk = _extract_download_link({"payload": nested})
            if lnk:
                return lnk
    return None


def _decode_csv_bytes(raw: bytes) -> str:
    """Decode CSV bytes trying BOM, UTF-8, CP1251 (common for RU exports)."""
    for enc in ("utf-8-sig", "utf-8", "cp1251", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _rows_with_mapped_headers(
    rows: Iterable[dict[str, str]],
    header_map: dict[str, str],
) -> list[dict]:
    """Re-key each CSV row using the Russian→canonical header map.

    Columns present in the CSV but not in the map are dropped — they'd break
    consumers that typecheck fields, and we only care about the canonical
    business fields documented in plan §1.
    """
    out: list[dict] = []
    for row in rows:
        mapped: dict = {}
        for ru_key, canon_key in header_map.items():
            val = row.get(ru_key)
            if val is None:
                continue
            if isinstance(val, str):
                val = val.strip()
            mapped[canon_key] = val
        if mapped:
            out.append(mapped)
    return out


def _parse_csv_text(text: str) -> list[dict[str, str]]:
    """Parse CSV text, auto-detecting ; vs , delimiter from the first line."""
    if not text:
        return []
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    first_line = text.split("\n", 1)[0]
    delimiter = ";" if ";" in first_line else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    return [dict(r) for r in reader]


def _parse_xlsx_bytes(raw: bytes) -> list[dict[str, str]]:
    """Defensive XLSX fallback — production always requests CSV."""
    import openpyxl  # local import so openpyxl stays optional at import time

    wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    ws = wb.active
    all_rows = list(ws.iter_rows(values_only=True))
    if not all_rows:
        return []
    headers = [str(c) if c is not None else "" for c in all_rows[0]]
    out: list[dict[str, str]] = []
    for r in all_rows[1:]:
        cells = [str(c) if c is not None else "" for c in r]
        out.append(dict(zip(headers, cells)))
    return out


def _parse_bytes_with_map(raw: bytes, header_map: dict[str, str]) -> list[dict]:
    """Decide CSV vs XLSX from magic bytes, then re-key rows."""
    if raw[:2] == b"PK":  # XLSX / zip magic — defensive fallback only.
        rows = _parse_xlsx_bytes(raw)
    else:
        rows = _parse_csv_text(_decode_csv_bytes(raw))
    return _rows_with_mapped_headers(rows, header_map)


# ── Public API ───────────────────────────────────────────────────────

def create_report(
    shop_ids: Sequence[int | str],
    job_type: str,
    date_from_ms: int,
    date_to_ms: int,
    *,
    token_getter: Callable[[], str | None] | None = None,
) -> str:
    """POST /documents/v2 — return the ``requestId`` Uzum assigns.

    Path C bulk-create: ``shop_ids`` is a sequence (one or many). All shops
    must live under the same admin token. Concurrent same-token creates are
    race-prone (cross-wired ``fileUrl`` — see project_uzum_race_bug_fix.md);
    callers must serialize / chunk creates rather than fan out per-shop.

    SELLS_REPORT hard-wires ``group=false`` (per coder rule §1). EXPENSES_REPORT
    also passes ``group=false`` to match the captured browser payload.
    """
    params = {
        "returns": False,
        "group": False,  # <-- NEVER True for sales, for any range, any condition.
        "shopIds": [int(x) for x in shop_ids],
        "dateFrom": int(date_from_ms),
        "dateTo": int(date_to_ms),
    }
    body = {
        "idempotencyKey": str(uuid.uuid4()),
        "jobType": job_type,
        "contentType": "CSV",
        "params": params,
    }
    resp = _http_json(_CREATE_URL, method="POST", body=body, token_getter=token_getter)
    payload = resp.get("payload") if isinstance(resp, dict) else None
    request_id = None
    if isinstance(payload, dict):
        request_id = payload.get("requestId") or payload.get("id")
    if not request_id and isinstance(resp, dict):
        request_id = resp.get("requestId")
    if not request_id:
        raise RuntimeError(f"create_report: no requestId in response ({resp!r:.200})")
    return str(request_id)


def wait_for_report(
    request_id: str,
    *,
    max_wait_s: int = 600,
    token_getter: Callable[[], str | None] | None = None,
) -> str:
    """Poll /documents/v2/{requestId} with adaptive 1→3→10→30s backoff.

    Raises ``TimeoutError`` if the wall-clock deadline is hit before COMPLETED.
    """
    poll_url = f"{_BASE}/{request_id}"
    deadline = time.monotonic() + max_wait_s
    attempt = 0
    status = "CREATED"
    last_resp: dict | None = None

    while time.monotonic() < deadline:
        sleep_s = _POLL_SCHEDULE[min(attempt, len(_POLL_SCHEDULE) - 1)]
        time.sleep(sleep_s)
        attempt += 1

        try:
            last_resp = _http_json(poll_url, token_getter=token_getter)
        except Exception:
            # Transient poll failures — retry schedule already dampens load.
            continue

        payload = last_resp.get("payload") if isinstance(last_resp, dict) else None
        if isinstance(payload, dict):
            status = payload.get("status") or payload.get("jobStatus") or status
        link = _extract_download_link(last_resp)
        if link:
            return link
        if status == "COMPLETED":
            # COMPLETED but no link in this payload — one more direct call.
            try:
                last_resp = _http_json(poll_url, token_getter=token_getter)
                link = _extract_download_link(last_resp)
                if link:
                    return link
            except Exception:
                pass

    raise TimeoutError(
        f"wait_for_report: request_id={request_id} status={status} "
        f"not COMPLETED within {max_wait_s}s"
    )


def download_csv(
    url: str,
    *,
    token_getter: Callable[[], str | None] | None = None,
    timeout: int = 120,
) -> bytes:
    """GET the report file URL, returning raw bytes.

    Uzum's file URL is already signed, so we do NOT inject the Bearer token
    by default (and must not — some CDN-backed links reject Authorization).
    A token_getter is still accepted so callers can override if needed.
    """
    from core.http_client import _get_http_session  # local import to stay lightweight

    sess = _get_http_session()
    headers = {
        "Accept": "application/octet-stream,application/csv,text/csv,*/*",
    }
    if token_getter is not None:
        try:
            tok = token_getter()
            if tok:
                headers["Authorization"] = tok if tok.startswith("Bearer ") else f"Bearer {tok}"
        except Exception:
            pass
    resp = sess.get(url, headers=headers, timeout=timeout)
    if resp.status_code >= 400:
        raise RuntimeError(f"download_csv HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.content


def parse_sells_csv(raw: bytes) -> list[dict]:
    """Parse SELLS_REPORT bytes (CSV primary, XLSX defensive fallback).

    Returns canonical-keyed dicts per ``_SELLS_HEADER_MAP``. Cancelled-row
    filtering (``status == "Отменен"``) is NOT done here — callers do that
    at ingest time right before INSERT (see coder rule §2).
    """
    return _parse_bytes_with_map(raw, _SELLS_HEADER_MAP)


def parse_expenses_csv(raw: bytes) -> list[dict]:
    """Parse EXPENSES_REPORT bytes to canonical-keyed dicts.

    ``amount`` values are already positive in the CSV — direction is carried
    by ``op_type`` (``Оплата`` / ``Возврат``). Callers must NOT flip the sign.
    """
    return _parse_bytes_with_map(raw, _EXPENSES_HEADER_MAP)
