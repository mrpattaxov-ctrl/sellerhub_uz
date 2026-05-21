"""Uzum Seller OpenAPI client.

Thin server-side wrapper around https://api-seller.uzum.uz/api/seller-openapi.
The browser MUST NOT call this host directly — Uzum's CORS policy blocks
cross-origin requests from the user's browser. All OpenAPI calls go through
Flask and the user's token is read from `User.uzum_openapi_token` (or passed
explicitly for the discovery probe before persistence).

The seller-openapi subsystem uses a DIFFERENT token from the
`api-seller.uzum.uz/api/seller/...` (browser) flow. Tokens for it are
issued in the seller cabinet's "API / Integrations" section. Because the
exact auth header scheme isn't documented in the public swagger, the
client probes a small set of common variants in order and falls back if
the first returns 401/403 with "Token not found".
"""
from __future__ import annotations

import json

import requests

from core.http_client import _get_http_session

OPENAPI_BASE = "https://api-seller.uzum.uz/api/seller-openapi"


# Ordered list of (label, header builder). First scheme to return 2xx wins.
# Confirmed against /v1/shops 2026-05-20: Uzum seller-openapi expects the
# RAW token in the Authorization header (no `Bearer ` prefix). `Bearer` is
# kept as a fallback in case Uzum adds it later (it's the OpenAPI 3.0
# standard) but it currently returns `forbidden-001 / Token not found`.
_AUTH_VARIANTS: list[tuple[str, callable]] = [
    ("Authorization: <raw>",  lambda t: {"Authorization": t}),
    ("Authorization: Bearer", lambda t: {"Authorization": f"Bearer {t}"}),
]


def _clean(token: str) -> str:
    t = (token or "").strip()
    if not t:
        raise RuntimeError("OpenAPI token is empty")
    # If the user pasted "Bearer xyz", normalize back to just "xyz" so each
    # variant can prefix consistently.
    if t.lower().startswith("bearer "):
        t = t[7:].strip()
    return t


def _try_request(url: str, headers: dict, *, debug_label: str) -> tuple[int, str, dict | list | None]:
    """Single attempt with the given headers. Returns (status, body_text, parsed_or_None).

    Uses the project's pooled session for connection reuse and the
    AdditiveBackoffRetry for transient 5xx/429 only — auth failures are
    surfaced immediately so callers can pick the next auth variant.
    """
    sess = _get_http_session()
    # Minimal, programmatic-client-style headers. Some openapi gateways
    # reject browser-like UA/Accept-Language combos with a 403, so we
    # deliberately keep the request lean.
    base = {
        "Accept": "application/json",
        "User-Agent": "uzum-warehouse-app/1.0 (+openapi-client)",
    }
    base.update(headers)
    try:
        resp = sess.get(url, headers=base, timeout=30)
    except requests.RequestException as e:
        print(f"[UzumOpenAPI] network error on {debug_label}: {e}")
        return (0, str(e), None)
    text = resp.text or ""
    parsed: dict | list | None = None
    try:
        parsed = json.loads(text) if text else None
    except json.JSONDecodeError:
        parsed = None
    # Only log on non-2xx — successful calls don't need to be loud.
    if not (200 <= resp.status_code < 300):
        print(f"[UzumOpenAPI] {debug_label} -> HTTP {resp.status_code}  body[:200]={text[:200]!r}")
    return (resp.status_code, text, parsed)


def _is_token_not_found(status: int, parsed) -> bool:
    """True when Uzum tells us the token was extracted but not recognized.

    Shape Uzum returns:
      {"errors":[{"code":"forbidden-001","message":"Token not found"}], ...}
    A 401 with any body also qualifies — that's "no/bad credentials" which
    is what we want to retry with a different auth scheme.
    """
    if status == 401:
        return True
    if status == 403 and isinstance(parsed, dict):
        errs = parsed.get("errors")
        if isinstance(errs, list):
            for e in errs:
                if not isinstance(e, dict):
                    continue
                if str(e.get("code", "")).startswith("forbidden") or \
                   "token" in str(e.get("message", "")).lower():
                    return True
        if "token" in str(parsed.get("error", "")).lower():
            return True
    return False


def _call_v1_shops(token: str) -> dict | list:
    """Probe /v1/shops with each auth variant in order. Returns parsed JSON.

    Raises ``RuntimeError`` with a descriptive message if every variant
    fails. The message includes which variants were tried and the last
    response body so the caller can surface it to the UI.
    """
    url = f"{OPENAPI_BASE}/v1/shops"
    token = _clean(token)

    last_status = 0
    last_text = ""
    last_label = ""

    for label, builder in _AUTH_VARIANTS:
        try:
            headers = builder(token)
        except Exception as e:
            print(f"[UzumOpenAPI] header-builder error on {label}: {e}")
            continue
        status, text, parsed = _try_request(url, headers, debug_label=label)
        if 200 <= status < 300 and parsed is not None:
            return parsed
        last_status, last_text, last_label = status, text, label
        # Only fall through to the next variant on auth-shaped failures.
        # Anything else (5xx, network) is unlikely to be fixed by changing
        # the auth header — stop and report.
        if not _is_token_not_found(status, parsed):
            break

    raise RuntimeError(
        f"Uzum rejected every auth header variant we tried "
        f"(last={last_label}, HTTP {last_status}). Response: {last_text[:300]}"
    )


def fetch_products_page(token: str, shop_uzum_id: str | int, *,
                        page: int, size: int = 100,
                        accept_language: str | None = None,
                        sort_by: str = "ID", order: str = "DESC",
                        filter_: str = "ALL") -> dict:
    """GET /v1/product/shop/{shopId}?page=...&size=...

    Returns the parsed JSON body (``AllProducts`` schema). Raises
    ``RuntimeError`` if every auth-header variant we know about is
    rejected — same probing rules as :func:`_call_v1_shops`.

    ``accept_language`` is passed through verbatim (``"ru"`` / ``"uz"``).
    The OpenAPI swagger does not document a localization parameter, so
    whether the API actually honors it is empirical — call twice and
    diff the returned productTitle to find out.
    """
    token = _clean(token)
    qs = (f"size={int(size)}&page={int(page)}"
          f"&sortBy={sort_by}&order={order}&filter={filter_}")
    url = f"{OPENAPI_BASE}/v1/product/shop/{shop_uzum_id}?{qs}"

    last_status = 0
    last_text = ""
    last_label = ""

    for label, builder in _AUTH_VARIANTS:
        try:
            headers = builder(token)
        except Exception as e:
            print(f"[UzumOpenAPI] header-builder error on {label}: {e}")
            continue
        if accept_language:
            headers = {**headers, "Accept-Language": accept_language}
        status, text, parsed = _try_request(
            url, headers, debug_label=f"products[{shop_uzum_id} p={page} lang={accept_language or '-'}]/{label}"
        )
        if 200 <= status < 300 and isinstance(parsed, dict):
            return parsed
        last_status, last_text, last_label = status, text, label
        if not _is_token_not_found(status, parsed):
            break

    raise RuntimeError(
        f"Uzum OpenAPI rejected /v1/product/shop/{shop_uzum_id} "
        f"(last={last_label}, HTTP {last_status}). Response: {last_text[:300]}"
    )


def fetch_finance_orders_page(token: str, shop_uzum_id: str | int, *,
                              date_from_sec: int | None,
                              date_to_sec: int | None,
                              page: int = 0, size: int = 100,
                              group: bool = False) -> dict:
    """GET /v1/finance/orders — paginated per-line sales ledger.

    Returns the parsed JSON body. Shape (verified live 2026-05-20):
      {"orderItems": [ {...}, ... ], "totalElements": N}

    NOTE: there is NO ``payload`` wrapper on this endpoint, unlike most other
    OpenAPI endpoints. The bare ``FinanceOrderItemsDto`` sits at the root.

    Date filter units
    -----------------
    Uzum's swagger says ``dateFrom``/``dateTo`` are Unix epoch **milliseconds**.
    They lie — the API actually expects **seconds**. Passing ms returns
    ``totalElements=0`` cleanly with HTTP 200. Always pass seconds here;
    callers convert from Tashkent-naive datetimes via ``int(dt.timestamp())``
    after attaching tzinfo.

    Pass ``None`` for either bound to omit it (the API then returns
    unfiltered data, ordered by ``date`` desc).

    Field-name surprise: the swagger says ``sellerPrice`` for unit price but
    the real response key is ``sellPrice``. See ``_openapi_order_to_canonical``.

    Other params
    ------------
    ``group=False`` → ``SellerOrderItemDto[]`` (per-order-line; primary mode
    for ``sales_lines`` ingest).
    ``group=True``  → ``ProductGroupedSellerItem[]`` (per-product rollup
    with embedded SKU breakdown including ``skuId``, ``characteristics`` and
    ``sellerDiscountAmount`` — useful for summary reports).
    """
    token = _clean(token)
    qs_parts = [f"shopIds={int(shop_uzum_id)}",
                f"page={int(page)}", f"size={int(size)}",
                f"group={'true' if group else 'false'}"]
    if date_from_sec is not None:
        qs_parts.append(f"dateFrom={int(date_from_sec)}")
    if date_to_sec is not None:
        qs_parts.append(f"dateTo={int(date_to_sec)}")
    url = f"{OPENAPI_BASE}/v1/finance/orders?{'&'.join(qs_parts)}"

    last_status = 0
    last_text = ""
    last_label = ""

    for label, builder in _AUTH_VARIANTS:
        try:
            headers = builder(token)
        except Exception as e:
            print(f"[UzumOpenAPI] header-builder error on {label}: {e}")
            continue
        status, text, parsed = _try_request(
            url, headers,
            debug_label=f"finance.orders[{shop_uzum_id} p={page} group={group}]/{label}"
        )
        if 200 <= status < 300 and isinstance(parsed, dict):
            return parsed
        last_status, last_text, last_label = status, text, label
        if not _is_token_not_found(status, parsed):
            break

    raise RuntimeError(
        f"Uzum OpenAPI rejected /v1/finance/orders shop={shop_uzum_id} "
        f"(last={last_label}, HTTP {last_status}). Response: {last_text[:300]}"
    )


def fetch_finance_expenses_page(token: str, shop_uzum_id: str | int, *,
                                date_from_sec: int | None,
                                date_to_sec: int | None,
                                page: int = 0, size: int = 100) -> dict:
    """GET /v1/finance/expenses — paginated per-operation expenses ledger.

    Returns the parsed JSON body. Shape (verified live 2026-05-20):
      {"payload": {"payments": [ {...}, ... ], "totalElements": N},
       "timestamp": "...", "trace": "..."}

    Unlike ``/v1/finance/orders`` this endpoint DOES use the ``payload``
    wrapper. Caller reads ``parsed["payload"]["payments"]``.

    Same seconds-not-ms quirk applies — pass epoch seconds.
    """
    token = _clean(token)
    qs_parts = [f"shopIds={int(shop_uzum_id)}",
                f"page={int(page)}", f"size={int(size)}"]
    if date_from_sec is not None:
        qs_parts.append(f"dateFrom={int(date_from_sec)}")
    if date_to_sec is not None:
        qs_parts.append(f"dateTo={int(date_to_sec)}")
    url = f"{OPENAPI_BASE}/v1/finance/expenses?{'&'.join(qs_parts)}"

    last_status = 0
    last_text = ""
    last_label = ""

    for label, builder in _AUTH_VARIANTS:
        try:
            headers = builder(token)
        except Exception as e:
            print(f"[UzumOpenAPI] header-builder error on {label}: {e}")
            continue
        status, text, parsed = _try_request(
            url, headers,
            debug_label=f"finance.expenses[{shop_uzum_id} p={page}]/{label}"
        )
        if 200 <= status < 300 and isinstance(parsed, dict):
            return parsed
        last_status, last_text, last_label = status, text, label
        if not _is_token_not_found(status, parsed):
            break

    raise RuntimeError(
        f"Uzum OpenAPI rejected /v1/finance/expenses shop={shop_uzum_id} "
        f"(last={last_label}, HTTP {last_status}). Response: {last_text[:300]}"
    )


def list_owned_shops(token: str) -> list[dict]:
    """Call /v1/shops and return a normalized list of owned shops.

    Each returned dict has at least:
      - uzum_id: str   (Uzum's shop identifier as a string)
      - name: str | None
    Other fields the API returns are passed through under `raw`.
    """
    resp = _call_v1_shops(token)

    # Uzum responses vary; accept the common shapes:
    #   { "payload": [ {...}, ... ] }
    #   { "payload": { "shops": [...] } }
    #   { "shops":   [ {...}, ... ] }
    #   { "data":    [ {...}, ... ] }
    #   [ {...}, ... ]
    items: list[dict] | None = None
    if isinstance(resp, list):
        items = resp
    elif isinstance(resp, dict):
        for key in ("payload", "shops", "data", "items", "result"):
            v = resp.get(key)
            if isinstance(v, list):
                items = v
                break
            if isinstance(v, dict):
                for inner in ("shops", "data", "items"):
                    iv = v.get(inner)
                    if isinstance(iv, list):
                        items = iv
                        break
                if items is not None:
                    break
    if items is None:
        raise RuntimeError(f"Unexpected response shape from Uzum OpenAPI /v1/shops: {str(resp)[:300]}")

    out: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        sid = (
            it.get("id")
            or it.get("shopId")
            or it.get("shop_id")
            or it.get("uzumId")
            or it.get("uzum_id")
            or it.get("merchantId")
        )
        if sid is None:
            continue
        name = (
            it.get("title")
            or it.get("name")
            or it.get("shopTitle")
            or it.get("shopName")
            or it.get("merchantName")
            or None
        )
        out.append({
            "uzum_id": str(sid).strip(),
            "name": (str(name).strip() if name else None),
            "raw": it,
        })
    return out
