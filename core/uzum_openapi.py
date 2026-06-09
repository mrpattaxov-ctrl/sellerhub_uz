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

import base64
import json
import os
import threading

import requests
from urllib3.util.retry import Retry

from config import HTTP_POOL_MAXSIZE
from core.http_client import _get_http_session, get_bucket_for_token
from core.fbs_locks import pace_uzum_call

OPENAPI_BASE = "https://api-seller.uzum.uz/api/seller-openapi"


# ── Bosqich A.9 (#5) — FBS-scoped fail-fast HTTP session ──────────────
# The shared core.http_client session retries 429/5xx with 60/120/180s
# additive backoff (total=3) — up to ~6 minutes INSIDE a single
# sess.request() call. That is fine for the background fbs-sync worker
# (not user-facing) but catastrophic for INTERACTIVE calls (Yangilash,
# bulk actions): the browser's AbortController frees the spinner at 15s,
# but the gunicorn worker thread keeps grinding the full backend retry to
# completion (requests has no cancel from a closed client), so N throttled
# sellers pin N worker threads for ~6 min each and can starve the pool —
# hanging the WHOLE app, not just FBS.
#
# Fix: interactive FBS calls use THIS separate per-thread session whose
# adapter mounts Retry(total=0) → zero retries, zero backoff sleep. With
# raise_on_status=False the 429/5xx response is RETURNED as the normal
# (parsed, status, text, label) tuple, so _raise_uzum_error → UzumAPIError
# fires exactly as before (error shape 100% preserved) — it just fails in
# ~1 round-trip (<2-3s) instead of 6 minutes.
#
# SCOPE: a NEW FBS-owned session on its own threading.local; it does NOT
# touch core.http_client (finance/products/POS/reports keep the patient
# shared retry). Selected per-call via the explicit ``fail_fast`` argument
# threaded from interactive callers — never thread-local, so the cancel
# Phase-4 background daemon (which re-hits the chokepoint after the
# response is sent) stays patient by passing fail_fast=False.
_fbs_fastfail_local = threading.local()


def _get_fbs_fastfail_session():
    """Per-thread requests session for INTERACTIVE FBS calls: no retry, no
    backoff (``Retry(total=0)``). A distinct object from the shared
    ``core.http_client`` session, so the patient app-wide retry policy is
    never mutated or shared.
    """
    sess = getattr(_fbs_fastfail_local, "session", None)
    if sess is None:
        sess = requests.Session()
        retry_strategy = Retry(
            total=0,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
            raise_on_status=False,
        )
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=HTTP_POOL_MAXSIZE,
            pool_maxsize=HTTP_POOL_MAXSIZE,
            max_retries=retry_strategy,
        )
        sess.mount("http://", adapter)
        sess.mount("https://", adapter)
        _fbs_fastfail_local.session = sess
    return sess


class UzumAPIError(RuntimeError):
    """Raised when Uzum returns a 4xx/5xx for an FBS action call.

    Carries the Uzum-side error code (``seller-order-NN`` style) so the
    route layer can translate it to a user-facing UZ message rather than
    leaking the raw Russian/English body. Falls back to ``None`` for code
    when Uzum's ``errors[]`` is empty or malformed (then the caller should
    show a generic "Uzum xatosi: HTTP {n}" message).

    Attributes
    ----------
    http_status : int
        The HTTP status Uzum returned (0 if request never connected).
    code : str | None
        First entry from response ``errors[].code`` (e.g. "seller-order-02").
    message : str | None
        First entry from response ``errors[].message`` (Russian text).
    raw_body : str
        Truncated raw response body for debugging.
    url : str
        The Uzum URL that was called.
    error_payload : dict | list | str | None
        First entry's ``errors[].payload`` — Uzum's structured detail for
        the error (e.g. the deadline that passed, the offending field/value,
        a limit that was exceeded). Shape varies per error code; carried
        verbatim so the route layer can enrich the UZ message and surface it
        for diagnostics. ``None`` when Uzum sent no per-error payload.
    trace : str | None
        Top-level ``trace`` request id Uzum stamps on every response — the
        single most useful thing to quote when reporting an issue to Uzum
        support, since it lets them find the exact failed request in their
        logs.
    timestamp : str | None
        Top-level ``timestamp`` (ISO-8601) of when Uzum produced the error.
    """

    def __init__(self, http_status: int, code: str | None,
                 message: str | None, raw_body: str, url: str,
                 *, error_payload=None, trace: str | None = None,
                 timestamp: str | None = None):
        self.http_status = http_status
        self.code = code
        self.message = message
        self.raw_body = raw_body[:500]
        self.url = url
        self.error_payload = error_payload
        self.trace = trace
        self.timestamp = timestamp
        super().__init__(
            f"Uzum {url} -> HTTP {http_status} code={code} msg={message} "
            f"trace={trace}"
        )


def _first_uzum_error(parsed) -> tuple[str | None, str | None]:
    """Pull ``(code, message)`` of the first entry in Uzum's ``errors[]``.

    Returns ``(None, None)`` when the response has no errors array (e.g.
    Uzum returned a non-JSON 5xx page or the array is empty).
    """
    if not isinstance(parsed, dict):
        return (None, None)
    errs = parsed.get("errors")
    if not isinstance(errs, list) or not errs:
        return (None, None)
    e = errs[0]
    if not isinstance(e, dict):
        return (None, None)
    code = e.get("code")
    msg = e.get("message")
    return (
        str(code).strip() if code else None,
        str(msg).strip() if msg else None,
    )


def _extract_uzum_error_detail(parsed) -> dict:
    """Pull the full diagnostic detail out of a Uzum error response.

    Reads BOTH the first ``errors[]`` entry (``code``, ``message``,
    ``payload``) and the top-level envelope fields (``trace``,
    ``timestamp``) that Uzum stamps on every response. The route layer
    uses these to (a) enrich the user-facing UZ message from ``payload``
    (e.g. the exact deadline that passed) and (b) surface ``trace`` /
    ``timestamp`` for diagnostics / Uzum-support reports.

    Returns a dict with keys ``code, message, payload, trace, timestamp``
    — each ``None`` when absent. Always returns a dict (never raises) so
    callers can splat it into ``UzumAPIError`` unconditionally.
    """
    out = {
        "code": None,
        "message": None,
        "payload": None,
        "trace": None,
        "timestamp": None,
    }
    if not isinstance(parsed, dict):
        return out

    # Top-level envelope — present on success AND error responses.
    trace = parsed.get("trace")
    ts = parsed.get("timestamp")
    out["trace"] = str(trace).strip() if trace else None
    out["timestamp"] = str(ts).strip() if ts else None

    errs = parsed.get("errors")
    if isinstance(errs, list) and errs:
        e = errs[0]
        if isinstance(e, dict):
            code = e.get("code")
            msg = e.get("message")
            out["code"] = str(code).strip() if code else None
            out["message"] = str(msg).strip() if msg else None
            # payload may be a dict / list / scalar — keep verbatim, but
            # drop empty containers so the route layer can treat falsy as
            # "no detail".
            pl = e.get("payload")
            if pl not in (None, {}, [], ""):
                out["payload"] = pl
    return out


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

    Uses the project's pooled session for connection reuse. Throttled by the
    process-wide ``TokenBucket`` keyed by the Uzum token, so all callers
    (backfill, hourly loop, variant seed, products sync) cooperatively
    respect Uzum's empirical 2-burst / ~2-per-sec ceiling without ever
    triggering 429s.
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

    # Cooperative rate limiting: pull a token from the per-Uzum-token bucket
    # BEFORE firing. Blocks if all tokens are in use. Auth header carries the
    # token; we strip a leading "Bearer " so different auth variants land in
    # the same bucket.
    auth_hdr = headers.get("Authorization", "")
    token_only = auth_hdr[7:].strip() if auth_hdr.lower().startswith("bearer ") else auth_hdr
    get_bucket_for_token(token_only).acquire()

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


# ─────────────────────────────────────────────────────────────────────
# FBS / DBS orders — confirmed against Uzum seller-openapi swagger
# 2026-05-21. The list endpoint is ``GET /v2/fbs/orders`` (DBS orders
# are returned by the same endpoint with ``scheme=DBS`` filter).
#
# Status enum (single value at a time, not CSV — swagger lists 11 values):
#   CREATED, PACKING, PENDING_DELIVERY, DELIVERING, DELIVERED,
#   ACCEPTED_AT_DP, DELIVERED_TO_CUSTOMER_DELIVERY_POINT, COMPLETED,
#   CANCELED, PENDING_CANCELLATION, RETURNED
#
# Response top-wrap: ``{"payload": {"orders": [...], "totalAmount": N},
# "errors": [...], "timestamp": "...", "trace": "...", "error": "null"}``
#
# Per-order key fields (verbatim Uzum names):
#   id, status, scheme, dateCreated, acceptUntil, deliverUntil, price,
#   shopId, identifierRequired, cancelReason, orderItems[], stock,
#   timeSlot, dropOffPoint, deliveryInfo {deliveryAddress,
#   customerFullname, customerPhone, deliveryComment}
# ─────────────────────────────────────────────────────────────────────


# Legacy URL probe variants used during the swagger-blind discovery phase.
# All five proved to be 404 — kept ONLY so an ops-time env flag can replay
# them if Uzum changes the canonical path. The default code path skips
# this entirely.
_LEGACY_FBS_ORDER_URL_VARIANTS = [
    ("v1.order.shop",   "/v1/order/shop/{sid}"),
    ("v1.orders.shop",  "/v1/orders/shop/{sid}"),
    ("v1.shop.order",   "/v1/shop/{sid}/order"),
    ("v1.order.q",      "/v1/order?shopIds={sid}"),
    ("v1.orders.q",     "/v1/orders?shopIds={sid}"),
]


def _fbs_orders_request_with_auth(
    url: str, token: str, *,
    accept_language: str | None,
    debug_label: str,
    method: str = "GET",
    json_body: dict | list | None = None,
    fail_fast: bool = False,
) -> tuple[dict | list | None, int, str, str]:
    """Execute a request against `url`, trying each auth variant.

    Returns ``(parsed_or_None, status, body_text, auth_label_used)``. The
    caller decides whether a non-2xx is fatal — this helper only handles
    the auth-variant rotation when Uzum returns 401/forbidden-001.

    ``method`` defaults to GET for backward compatibility. Action callers
    (confirm/cancel/identifier) pass ``method="POST"`` plus ``json_body``;
    the helper sets ``Content-Type`` automatically.

    ``fail_fast`` (Bosqich A.9 / #5): when True the call uses the FBS
    zero-retry session (:func:`_get_fbs_fastfail_session`) so a 429/5xx
    fails in ~1 round-trip instead of the shared session's 60/120/180s
    additive backoff. Interactive callers pass True; the background
    fbs-sync worker keeps the default False (patient AdditiveBackoffRetry).
    The return-tuple shape is identical for both, so error handling is
    unchanged.
    """
    sess = _get_fbs_fastfail_session() if fail_fast else _get_http_session()
    last_status = 0
    last_text = ""
    last_label = ""
    last_parsed: dict | list | None = None
    for auth_label, builder in _AUTH_VARIANTS:
        try:
            headers = builder(token)
        except Exception as e:
            print(f"[UzumOpenAPI] header-builder error on {auth_label}: {e}")
            continue
        if accept_language:
            headers = {**headers, "Accept-Language": accept_language}

        # Same minimal lean header set as _try_request — anti-403-on-UA.
        base = {
            "Accept": "application/json",
            "User-Agent": "uzum-warehouse-app/1.0 (+openapi-client)",
        }
        base.update(headers)
        if json_body is not None:
            base["Content-Type"] = "application/json"

        try:
            resp = sess.request(
                method=method, url=url, headers=base,
                json=json_body if json_body is not None else None,
                timeout=30,
            )
        except requests.RequestException as e:
            print(f"[UzumOpenAPI] network error on {debug_label}/{auth_label}: {e}")
            last_status, last_text, last_label = 0, str(e), auth_label
            # Network error is typically transient/host-level — no point
            # rotating auth variants for it.
            break

        text = resp.text or ""
        parsed: dict | list | None = None
        try:
            parsed = json.loads(text) if text else None
        except json.JSONDecodeError:
            parsed = None

        if resp.status_code == 429:
            # Bosqich A.8 — explicit, greppable burst marker. Uzum returns
            # 429 when too many calls land on one token too fast (the
            # per-token burst penalty). On the fail-fast (interactive) path
            # this fires on the FIRST 429 (no retry); on the patient worker
            # path the shared session already retried 60/120/180s first.
            # Tail it with:  docker compose logs -f app | grep BURST
            #
            # Bosqich A.9 (#5 diagnostics) — dump the rate-limit headers so
            # we can finally tell a sub-second BURST from a rolling-WINDOW
            # quota. Retry-After (seconds or HTTP-date) is the key: if Uzum
            # sends it, that IS the throttle window; the X-RateLimit-*/
            # RateLimit-* family (if present) reveals quota size + reset.
            _h = resp.headers
            _rate_hdrs = {
                k: v for k, v in _h.items()
                if k.lower() in (
                    "retry-after", "ratelimit-limit", "ratelimit-remaining",
                    "ratelimit-reset", "x-ratelimit-limit",
                    "x-ratelimit-remaining", "x-ratelimit-reset",
                    "x-rate-limit-limit", "x-rate-limit-remaining",
                )
            }
            print(f"[UzumOpenAPI] ⚠️ BURST/429 on {debug_label}/{auth_label} — "
                  f"Uzum per-token rate-limit hit. retry_after={_h.get('Retry-After')!r} "
                  f"rate_headers={_rate_hdrs!r} body[:200]={text[:200]!r}")
        elif not (200 <= resp.status_code < 300):
            print(f"[UzumOpenAPI] {debug_label}/{auth_label} -> HTTP {resp.status_code}  body[:200]={text[:200]!r}")

        if 200 <= resp.status_code < 300:
            return (parsed, resp.status_code, text, auth_label)
        last_status, last_text, last_label = resp.status_code, text, auth_label
        last_parsed = parsed
        # Only retry with next auth variant on auth-shaped failures.
        if not _is_token_not_found(resp.status_code, parsed):
            break
    return (last_parsed, last_status, last_text, last_label)


def fetch_fbs_orders_page(
    token: str,
    shop_uzum_id: str | int | list,
    *,
    status: str = "CREATED",
    scheme: str | None = None,
    page: int = 0,
    size: int = 20,
    date_from_ms: int | None = None,
    date_to_ms: int | None = None,
    accept_language: str | None = None,
    fail_fast: bool = False,
) -> tuple[dict, str]:
    """GET /v2/fbs/orders — FBS/DBS seller orders by status.

    Returns ``(parsed_body, used_url)``. Body shape (verbatim swagger,
    confirmed 2026-05-21):

        {"payload": {"orders": [...], "totalAmount": N},
         "errors": [...], "timestamp": "...", "trace": "...",
         "error": "null"}

    Parameters
    ----------
    shop_uzum_id : single id or list of ids. Always serialized as the
        ``shopIds`` repeated query parameter (OpenAPI 3 style=form,
        explode=true). REQUIRED by Uzum.
    status : one of the 11 enum values (CREATED, PACKING, ...). Single
        value only — the endpoint does NOT accept CSV here.
    scheme : ``"FBS"`` / ``"DBS"`` / ``None`` (returns both).
    page, size : 0-based pagination. ``size`` is capped at 50 by Uzum.
    date_from_ms, date_to_ms : epoch *milliseconds* per swagger
        (``integer($int64)``). NOTE: the related ``/v1/finance/orders``
        endpoint documented ms but actually expects seconds — empirically
        verify against /v2/fbs/orders the first time these are used.

    Legacy fallback
    ---------------
    If ``UZUM_FBS_PROBE_LEGACY=1`` is set in the environment, the
    function additionally tries the five guessed URL paths from the
    pre-swagger discovery phase. Production should never need this.
    """
    token = _clean(token)
    if size > 50:
        size = 50  # Uzum caps at 50; pass-through would 400.
    elif size < 1:
        size = 1

    # Serialize shopIds as repeated form params per OpenAPI 3 default.
    if isinstance(shop_uzum_id, (list, tuple)):
        ids = [str(s).strip() for s in shop_uzum_id if str(s).strip()]
    else:
        ids = [str(shop_uzum_id).strip()]
    if not ids or not any(ids):
        raise RuntimeError("fetch_fbs_orders_page: shop_uzum_id is required")

    qs_parts = [f"shopIds={sid}" for sid in ids]
    qs_parts.append(f"status={status}")
    if scheme:
        qs_parts.append(f"scheme={scheme}")
    if date_from_ms is not None:
        qs_parts.append(f"dateFrom={int(date_from_ms)}")
    if date_to_ms is not None:
        qs_parts.append(f"dateTo={int(date_to_ms)}")
    qs_parts.append(f"page={int(page)}")
    qs_parts.append(f"size={int(size)}")
    qs = "&".join(qs_parts)

    primary_url = f"{OPENAPI_BASE}/v2/fbs/orders?{qs}"
    debug_label = f"fbs.orders[shops={','.join(ids)} status={status} scheme={scheme or '-'} p={page}]"

    parsed, status_code, text, _ = _fbs_orders_request_with_auth(
        primary_url, token,
        accept_language=accept_language,
        debug_label=debug_label,
        fail_fast=fail_fast,
    )
    if 200 <= status_code < 300 and isinstance(parsed, dict):
        return (parsed, primary_url)

    # Legacy probe — only when explicitly opted in. Useful if Uzum ever
    # renames the canonical path; lets us bring the page back without a
    # code deploy by flipping an env var.
    if os.getenv("UZUM_FBS_PROBE_LEGACY") == "1":
        for variant_label, path_tpl in _LEGACY_FBS_ORDER_URL_VARIANTS:
            # Old shapes used per-shop path templates; only retry for a
            # single-id call to keep the legacy semantics intact.
            if len(ids) != 1:
                continue
            path = path_tpl.format(sid=ids[0])
            sep = "&" if "?" in path else "?"
            url = (
                f"{OPENAPI_BASE}{path}{sep}page={int(page)}&size={int(size)}"
                f"&statuses={status}"
            )
            if scheme:
                url += f"&types={scheme}"
            legacy_parsed, legacy_status, _, _ = _fbs_orders_request_with_auth(
                url, token,
                accept_language=accept_language,
                debug_label=f"fbs.orders.legacy/{variant_label}",
                fail_fast=fail_fast,
            )
            if 200 <= legacy_status < 300 and isinstance(legacy_parsed, (dict, list)):
                body = legacy_parsed if isinstance(legacy_parsed, dict) else {"orders": legacy_parsed}
                return (body, url)

    raise RuntimeError(
        f"Uzum OpenAPI /v2/fbs/orders failed for shops={','.join(ids)} status={status} "
        f"(HTTP {status_code}). Response: {text[:300]}"
    )


def extract_fbs_orders_list(body: dict) -> tuple[list[dict], int]:
    """Pull ``payload.orders`` + ``payload.totalAmount`` out of a
    /v2/fbs/orders response.

    Returns ``([], 0)`` for any unexpected shape. Confirmed against the
    swagger response template (2026-05-21):

        {"payload": {"orders": [{...}, ...], "totalAmount": N}, ...}

    Falls back to a bare-list response only if Uzum ever changes the
    wrap — that's the only divergence we tolerate without a code change.
    """
    if isinstance(body, list):
        return (body, len(body))
    if not isinstance(body, dict):
        return ([], 0)

    payload = body.get("payload")
    if isinstance(payload, dict):
        orders = payload.get("orders")
        if isinstance(orders, list):
            try:
                total = int(payload.get("totalAmount") or len(orders))
            except (TypeError, ValueError):
                total = len(orders)
            return (orders, total)
    return ([], 0)


def fetch_fbs_order_detail(
    token: str,
    order_id: str | int,
    *,
    accept_language: str | None = None,
    fail_fast: bool = False,
) -> tuple[dict, str]:
    """GET /v1/fbs/order/{orderId} — full info for a single order.

    Returns ``(order_dict, used_url)``. Confirmed against swagger
    2026-05-21:

        Path: GET /v1/fbs/order/{orderId}   (orderId: int64, required)
        Response: {"payload": {<order>}, "errors": [...], ...}

    The order shape inside ``payload`` is identical to one item of
    ``/v2/fbs/orders`` response's ``payload.orders[]`` — same field names
    (id, status, scheme, deliveryInfo, orderItems, stock, dropOffPoint,
    timeSlot, all the date columns, etc.). Callers can reuse list-row
    rendering logic on this dict directly.

    Note: this endpoint has no shop filter — the caller MUST perform a
    post-fetch scope check against ``payload.shopId`` for multi-tenant
    safety (fbs/routes.py already does this).
    """
    token = _clean(token)
    try:
        oid = int(order_id)
    except (TypeError, ValueError):
        raise RuntimeError(f"fetch_fbs_order_detail: orderId must be int-like, got {order_id!r}")

    url = f"{OPENAPI_BASE}/v1/fbs/order/{oid}"
    parsed, status_code, text, _ = _fbs_orders_request_with_auth(
        url, token,
        accept_language=accept_language,
        debug_label=f"fbs.order.detail[{oid}]",
        fail_fast=fail_fast,
    )
    if not (200 <= status_code < 300) or not isinstance(parsed, dict):
        raise RuntimeError(
            f"Uzum OpenAPI /v1/fbs/order/{oid} failed (HTTP {status_code}). "
            f"Response: {text[:300]}"
        )

    order = parsed.get("payload")
    if not isinstance(order, dict):
        raise RuntimeError(
            f"Uzum OpenAPI /v1/fbs/order/{oid}: unexpected payload shape "
            f"({type(order).__name__}); expected dict"
        )
    return (order, url)


def fetch_fbs_orders_count(
    token: str,
    shop_uzum_id: str | int | list,
    *,
    status: str = "CREATED",
    date_from_ms: int | None = None,
    date_to_ms: int | None = None,
    accept_language: str | None = None,
    fail_fast: bool = False,
) -> tuple[int, str]:
    """GET /v2/fbs/orders/count — number of FBS orders matching filters.

    Returns ``(count_as_int, used_url)``. Confirmed against swagger
    2026-05-21:

        Request:  GET /v2/fbs/orders/count?shopIds=...&status=...
                  &dateFrom=...&dateTo=...
        Response: {"payload": <int>, "errors": [...], ...}

    The count endpoint takes the same status enum as /v2/fbs/orders BUT
    has NO ``scheme`` parameter — meaning it returns counts for FBS only
    (per swagger description: "Возвращает количество заказов FBS").
    DBS counts must come from /v2/fbs/orders with size=1 + scheme=DBS.
    """
    token = _clean(token)

    if isinstance(shop_uzum_id, (list, tuple)):
        ids = [str(s).strip() for s in shop_uzum_id if str(s).strip()]
    else:
        ids = [str(shop_uzum_id).strip()]
    if not ids or not any(ids):
        raise RuntimeError("fetch_fbs_orders_count: shop_uzum_id is required")

    qs_parts = [f"shopIds={sid}" for sid in ids]
    qs_parts.append(f"status={status}")
    if date_from_ms is not None:
        qs_parts.append(f"dateFrom={int(date_from_ms)}")
    if date_to_ms is not None:
        qs_parts.append(f"dateTo={int(date_to_ms)}")
    url = f"{OPENAPI_BASE}/v2/fbs/orders/count?{'&'.join(qs_parts)}"

    # Bosqich A.8 — pace /count through the shared per-token gate too.
    # Uzum's burst penalty triggers on >1 call/sec on a token regardless of
    # endpoint; the old code fired 3 concurrent /count calls per Yangilash
    # press on the unverified assumption that /count is burst-exempt — a
    # prime 429 source. Now every /count is paced like every /orders call.
    pace_uzum_call(token)
    parsed, status_code, text, _ = _fbs_orders_request_with_auth(
        url, token,
        accept_language=accept_language,
        debug_label=f"fbs.count[shops={','.join(ids)} status={status}]",
        fail_fast=fail_fast,
    )
    if not (200 <= status_code < 300) or not isinstance(parsed, dict):
        raise RuntimeError(
            f"Uzum OpenAPI /v2/fbs/orders/count failed for shops={','.join(ids)} "
            f"status={status} (HTTP {status_code}). Response: {text[:300]}"
        )

    payload = parsed.get("payload")
    try:
        count = int(payload) if payload is not None else 0
    except (TypeError, ValueError):
        # Defensive: if Uzum ever changes shape (e.g. wraps in dict),
        # raise so the caller sees the issue rather than silently 0.
        raise RuntimeError(
            f"Uzum OpenAPI /v2/fbs/orders/count: unexpected payload shape "
            f"({type(payload).__name__}={payload!r}); expected integer"
        )
    return (count, url)


# ─────────────────────────────────────────────────────────────────────
# FBS action endpoints — confirm / cancel / identifier / label /
# return-reasons. Swagger snapshot 2026-05-21.
#
# All five share the same auth-variant rotation as the GET endpoints
# (because Uzum's seller-openapi still uses the raw-token-vs-Bearer
# probe in case the policy ever changes).
#
# Errors: every action raises ``UzumAPIError`` on non-2xx so the route
# layer can map Uzum's ``seller-order-NN`` codes to UZ user-friendly
# messages via FBS_ERROR_MESSAGES_UZ (in fbs/routes.py). The caller
# never has to inspect raw HTTP — the exception carries everything.
# ─────────────────────────────────────────────────────────────────────


def _raise_uzum_error(parsed, status_code: int, text: str, url: str) -> None:
    """Build and raise an ``UzumAPIError`` from a failed Uzum response.

    Defensive: if the caller passed ``parsed=None`` but a body text is
    available, re-parse it here so the error code is still extracted.
    Some helpers (e.g. ``_fbs_orders_request_with_auth`` after a non-2xx
    break) historically returned ``None`` for parsed even when the JSON
    body itself contained ``errors[]`` — the re-parse guards against that.
    """
    if parsed is None and text:
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            parsed = None
    detail = _extract_uzum_error_detail(parsed)
    raise UzumAPIError(
        status_code, detail["code"], detail["message"], text, url,
        error_payload=detail["payload"],
        trace=detail["trace"],
        timestamp=detail["timestamp"],
    )


def confirm_fbs_order(token: str, order_id: str | int, *, fail_fast: bool = False) -> tuple[dict, str]:
    """POST /v1/fbs/order/{orderId}/confirm — confirm an FBS order.

    Returns ``(payload_dict, used_url)``. ``payload`` is the full order
    JSON in the same shape as ``fetch_fbs_order_detail`` returns — Uzum
    echoes back the post-state of the order so the caller can update its
    UI without a second fetch (we still re-fetch via cache invalidation
    in fbs_data so the list page stays in sync).

    Request body: NONE. Uzum's swagger confirms this is a path-only call.

    Possible Uzum errors (lifted into ``UzumAPIError.code``):
      - seller-order-01: order not found
      - seller-order-02: wrong status for confirm (not CREATED)
      - seller-order-03: confirmation deadline passed
    """
    token = _clean(token)
    try:
        oid = int(order_id)
    except (TypeError, ValueError):
        raise RuntimeError(f"confirm_fbs_order: orderId must be int-like, got {order_id!r}")

    url = f"{OPENAPI_BASE}/v1/fbs/order/{oid}/confirm"
    # Bosqich A.8 (#4) — pace action endpoints too. Bulk confirm fans out
    # up to 5 concurrent confirm_fbs_order calls on ONE token; without the
    # per-token gate they burst Uzum (429 → 60-180s throttle). The gate
    # reserves distinct 1s-spaced slots so the fan-out interleaves safely.
    pace_uzum_call(token)
    parsed, status_code, text, _ = _fbs_orders_request_with_auth(
        url, token, method="POST",
        accept_language=None,
        debug_label=f"fbs.confirm[{oid}]",
        fail_fast=fail_fast,
    )
    if not (200 <= status_code < 300):
        _raise_uzum_error(parsed, status_code, text, url)

    payload = parsed.get("payload") if isinstance(parsed, dict) else None
    return (payload if isinstance(payload, dict) else {}, url)


def cancel_fbs_order(
    token: str,
    order_id: str | int,
    *,
    reason: str,
    comment: str | None = None,
    fail_fast: bool = False,
) -> tuple[dict, str]:
    """POST /v1/fbs/order/{orderId}/cancel — cancel an FBS order.

    Request body (application/json, REQUIRED):
        {"reason": "<enum from /v1/fbs/order/return-reasons>",
         "comment": "<optional free-text>"}

    Returns ``(parsed_body, used_url)``. Uzum's response payload is
    usually empty (``{}``) — the meaningful signal is the 200 status.

    Possible Uzum errors:
      - seller-order-01: order not found
      - seller-order-02: wrong status for cancel
      - seller-order-12: invalid reason (not in enum)
      - seller-order-13: already cancelled
    """
    token = _clean(token)
    try:
        oid = int(order_id)
    except (TypeError, ValueError):
        raise RuntimeError(f"cancel_fbs_order: orderId must be int-like, got {order_id!r}")
    if not reason or not str(reason).strip():
        raise RuntimeError("cancel_fbs_order: reason is required")

    body: dict[str, str] = {"reason": str(reason).strip()}
    if comment is not None and str(comment).strip():
        body["comment"] = str(comment).strip()

    url = f"{OPENAPI_BASE}/v1/fbs/order/{oid}/cancel"
    # Bosqich A.8 (#4) — pace cancel through the per-token gate (a cancel
    # can land in the same second as a worker tick / another action on the
    # same token and burst Uzum). See confirm_fbs_order for the rationale.
    pace_uzum_call(token)
    parsed, status_code, text, _ = _fbs_orders_request_with_auth(
        url, token, method="POST", json_body=body,
        accept_language=None,
        debug_label=f"fbs.cancel[{oid} reason={reason}]",
        fail_fast=fail_fast,
    )
    if not (200 <= status_code < 300):
        _raise_uzum_error(parsed, status_code, text, url)

    return (parsed if isinstance(parsed, dict) else {}, url)


def attach_fbs_identifiers(
    token: str,
    order_id: str | int,
    *,
    items: list[dict],
    fail_fast: bool = False,
) -> tuple[dict, str]:
    """POST /v1/fbs/order/{orderId}/identifier — attach IMEIs/serials.

    Request body shape (REQUIRED):
        {"items": [
            {"orderItemId": <int>, "values": ["IMEI1", "IMEI2", ...]},
            ...
        ]}

    Each ``values[]`` entry is one identifier string. The expected length
    must match the orderItem's ``amount`` × identifier slots — Uzum
    rejects with seller-order-06 if too many are sent.

    Returns ``(parsed_body, used_url)``. Response shape per swagger:
        {"payload": [{"type": "IMEI", "required": true, "values": [...]}],
         "errors": [...]}

    Possible Uzum errors:
      - seller-order-01: order not found
      - seller-order-02: wrong status
      - seller-order-05: order item has no identifier type defined
      - seller-order-06: too many identifiers passed
      - seller-order-07: invalid identifier value
      - seller-order-08: identifier belongs to a different SKU
      - seller-order-09: identifier type not specified
      - seller-order-10: WMS unexpected error
      - seller-order-11: identifier service unavailable
      - seller-order-36: order item not found
    """
    token = _clean(token)
    try:
        oid = int(order_id)
    except (TypeError, ValueError):
        raise RuntimeError(f"attach_fbs_identifiers: orderId must be int-like, got {order_id!r}")
    if not isinstance(items, list) or not items:
        raise RuntimeError("attach_fbs_identifiers: items list is required")

    # Normalize: strip whitespace from values, ensure orderItemId is int.
    normalized: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            oi_id = int(it.get("orderItemId"))
        except (TypeError, ValueError):
            raise RuntimeError("attach_fbs_identifiers: each item needs an int orderItemId")
        values = it.get("values") or []
        if not isinstance(values, list):
            raise RuntimeError("attach_fbs_identifiers: each item.values must be a list")
        clean_values = [str(v).strip() for v in values if str(v).strip()]
        if not clean_values:
            # Skip rows the user left blank; Uzum would 400 on empty values.
            continue
        normalized.append({"orderItemId": oi_id, "values": clean_values})
    if not normalized:
        raise RuntimeError("attach_fbs_identifiers: no non-empty identifier values provided")

    body = {"items": normalized}
    url = f"{OPENAPI_BASE}/v1/fbs/order/{oid}/identifier"
    # Bosqich A.8 (#4) — pace identifier attach through the per-token gate
    # for consistency with the other action endpoints (burst-safety).
    pace_uzum_call(token)
    parsed, status_code, text, _ = _fbs_orders_request_with_auth(
        url, token, method="POST", json_body=body,
        accept_language=None,
        debug_label=f"fbs.identifier[{oid} items={len(normalized)}]",
        fail_fast=fail_fast,
    )
    if not (200 <= status_code < 300):
        _raise_uzum_error(parsed, status_code, text, url)

    return (parsed if isinstance(parsed, dict) else {}, url)


def fetch_fbs_dropoff_points(
    token: str,
    order_ids: list[int | str],
    *,
    accept_language: str | None = None,
    fail_fast: bool = False,
) -> tuple[list[dict], str]:
    """GET /v1/fbs/invoice/dop/drop-off-points — available drop-off points.

    Returns drop-off points suitable for the given set of order IDs.
    Per swagger: "Returns available drop-off points suitable by
    dimensional groups, that have at least one timeslot earlier than the
    earliest 'deliver by' date, with remaining capacity > number of
    orders in the list."

    Args:
        token: seller OpenAPI token
        order_ids: list of FBS order IDs (PACKING+ status) to find
                   suitable points for. Uzum uses these to filter by
                   dimensional group + earliest deliver-by date + capacity.

    Returns:
        ``(points, used_url)`` where ``points`` is a list of dicts. The
        exact shape depends on Uzum's response — typically:
        ``[{"uuid": "...", "address": "...", "type": "ISSUE_POINT", ...}]``

    Failure: raises ``UzumAPIError`` on non-2xx. Caller decides whether
    to surface the error or fall back to DB-aggregated points.
    """
    token = _clean(token)
    if not order_ids:
        return ([], "")
    # Normalize and dedupe.
    ids = []
    for oid in order_ids:
        try:
            ids.append(int(oid))
        except (TypeError, ValueError):
            continue
    if not ids:
        return ([], "")
    ids = sorted(set(ids))

    # Swagger spec (2026-05-25):
    #   GET /v1/fbs/invoice/dop/drop-off-points
    #   Query: customerOrderIds (array<integer>, required) — repeated.
    #   Header: Accept-Language (ru|uz, default uz)
    #   Response 200: {"payload": {"dropOffPoints": [
    #     {"uuid", "address", "type", "latitude", "longitude",
    #      "workingHours": {"MONDAY": {"start", "end"}, ...},
    #      "dimensionalGroupIsLarge": bool}
    #   ]}, "errors": [...], "timestamp": "..."}
    qs = "&".join(f"customerOrderIds={i}" for i in ids)
    url = f"{OPENAPI_BASE}/v1/fbs/invoice/dop/drop-off-points?{qs}"
    parsed, status_code, text, _ = _fbs_orders_request_with_auth(
        url, token, method="GET",
        accept_language=accept_language,
        debug_label=f"fbs.dropoff.points[orders={len(ids)}]",
        fail_fast=fail_fast,
    )
    if not (200 <= status_code < 300):
        print(f"[UzumOpenAPI] dropoff-points HTTP {status_code}: {text[:300]}")
        _raise_uzum_error(parsed, status_code, text, url)

    points: list[dict] = []
    if isinstance(parsed, dict):
        payload = parsed.get("payload")
        if isinstance(payload, dict):
            dp = payload.get("dropOffPoints")
            if isinstance(dp, list):
                points = [p for p in dp if isinstance(p, dict)]
    if not points:
        print(f"[UzumOpenAPI] dropoff-points unexpected shape: "
              f"type={type(parsed).__name__} keys={list(parsed.keys()) if isinstance(parsed, dict) else '-'}")
    return (points, url)


def fetch_fbs_time_slots(
    token: str,
    dop_id: str,
    order_ids: list[int | str],
    *,
    accept_language: str | None = None,
    fail_fast: bool = False,
) -> tuple[list[dict], str]:
    """GET /v1/fbs/invoice/dop/time-slot — available delivery time-slots.

    Swagger spec (2026-05-25):
      Query: dopId (uuid, required) + sellerOrderIds (array<int>, required)
      Header: Accept-Language (ru|uz, default uz)
      Response 200: {"payload": {"timeSlots": [
        {"timeFrom": ISO8601, "timeTo": ISO8601}
      ]}}
      400 errors:
        seller-order-14 — Drop-off point not found
        fbs-20-incompatible-dimensional-groups — points doesn't fit orders
        fbs-19-time-is-up — deliver-by deadline already passed

    The slot set is the intersection of:
      * point's capacity ≥ number of orders in list
      * slot time falls between NOW and the earliest deliver-by date.
    """
    token = _clean(token)
    if not dop_id:
        raise RuntimeError("fetch_fbs_time_slots: dopId required")
    if not order_ids:
        return ([], "")
    ids = []
    for oid in order_ids:
        try:
            ids.append(int(oid))
        except (TypeError, ValueError):
            continue
    if not ids:
        return ([], "")
    ids = sorted(set(ids))

    qs_parts = [f"dopId={dop_id}"] + [f"sellerOrderIds={i}" for i in ids]
    url = f"{OPENAPI_BASE}/v1/fbs/invoice/dop/time-slot?{'&'.join(qs_parts)}"
    parsed, status_code, text, _ = _fbs_orders_request_with_auth(
        url, token, method="GET",
        accept_language=accept_language,
        debug_label=f"fbs.dropoff.timeslots[dop={dop_id[:8]} orders={len(ids)}]",
        fail_fast=fail_fast,
    )
    if not (200 <= status_code < 300):
        print(f"[UzumOpenAPI] time-slot HTTP {status_code}: {text[:300]}")
        _raise_uzum_error(parsed, status_code, text, url)

    slots: list[dict] = []
    if isinstance(parsed, dict):
        payload = parsed.get("payload")
        if isinstance(payload, dict):
            ts = payload.get("timeSlots")
            if isinstance(ts, list):
                slots = [s for s in ts if isinstance(s, dict)]
    return (slots, url)


def create_fbs_invoice(
    token: str,
    *,
    order_ids: list[int | str],
    drop_off_point_uuid: str,
    time_slot_uuid: str,
    seller_id: int,
    idempotency_key: str | None = None,
    update_only: bool = False,
    accept_language: str | None = None,
    fail_fast: bool = False,
) -> tuple[dict, str]:
    """POST /v1/fbs/invoice (or .../dop/time-slot when ``update_only=True``).

    ``fail_fast`` (default False): the whole invoice family is reached only
    from interactive page/button handlers — never the background worker — so
    every route passes ``fail_fast=True``. That swaps the shared session's
    patient 60/120/180s additive backoff (right for the worker) for a single
    round-trip, so a per-token burst-429 surfaces a fast error instead of
    freezing the UI for minutes (e.g. change-pickup → immediate list reload).

    Both endpoints accept the same body and return the same invoice shape
    — the difference is intent:
      * ``POST /v1/fbs/invoice`` creates a brand-new invoice.
      * ``POST /v1/fbs/invoice/dop/time-slot`` updates the point/slot of
        an EXISTING invoice that already covers those order IDs.

    Body (InvoiceRequest schema, verified 2026-05-25)::

        {
          "orderIds":         [<int>, ...],   *required
          "dropOffPointUuid": "<uuid>",       *required
          "timeSlotUuid":     "<uuid>",       *required
          "sellerId":         <int>,          *required
          "idempotencyKey":   "<string>"      (optional)
        }

    Response 200 (both)::

        {"payload": {
          "id": <invoice id>, "number": <invoice number>,
          "status": {"value": "CREATED", "text": "...", "color": "..."},
          "stock": {...}, "timeSlot": {...}, "dropOffPoint": {...},
          "ettn": {"ettnId": "...", "status": "CREATED", ...}
        }}

    Error codes:
      Create:  seller-order-14 (point not found),
               seller-order-23 (slot unavailable)
      Update:  seller-order-04 (invoice position not found),
               seller-order-19 (invoice not found),
               seller-order-23 (slot unavailable)
      Both:    fbs-2-seller-access-denied (403)

    Args:
        update_only: if True, route to the .../dop/time-slot variant.
    """
    token = _clean(token)
    if not order_ids:
        raise RuntimeError("create_fbs_invoice: order_ids required")
    if not drop_off_point_uuid:
        raise RuntimeError("create_fbs_invoice: drop_off_point_uuid required")
    if not time_slot_uuid:
        raise RuntimeError("create_fbs_invoice: time_slot_uuid required")
    if seller_id is None:
        raise RuntimeError("create_fbs_invoice: seller_id required")
    try:
        seller_id_int = int(seller_id)
    except (TypeError, ValueError):
        raise RuntimeError(
            f"create_fbs_invoice: seller_id must be int-like, got {seller_id!r}"
        )

    ids = []
    for oid in order_ids:
        try:
            ids.append(int(oid))
        except (TypeError, ValueError):
            continue
    if not ids:
        raise RuntimeError("create_fbs_invoice: no valid order IDs after coercion")

    body: dict = {
        "orderIds": ids,
        "dropOffPointUuid": drop_off_point_uuid,
        "timeSlotUuid": time_slot_uuid,
        "sellerId": seller_id_int,
    }
    # Idempotency key — Uzum uses this to deduplicate retries. If caller
    # didn't supply one, mint a stable hash of the args so retries within
    # the same minute collapse into a single invoice.
    if not idempotency_key:
        import hashlib
        sig = f"{seller_id_int}-{drop_off_point_uuid}-{time_slot_uuid}-{sorted(ids)}"
        idempotency_key = hashlib.sha256(sig.encode()).hexdigest()[:32]
    body["idempotencyKey"] = idempotency_key

    path = "/v1/fbs/invoice/dop/time-slot" if update_only else "/v1/fbs/invoice"
    url = f"{OPENAPI_BASE}{path}"
    parsed, status_code, text, _ = _fbs_orders_request_with_auth(
        url, token, method="POST", json_body=body,
        accept_language=accept_language,
        debug_label=f"fbs.invoice.{'update' if update_only else 'create'}"
                    f"[orders={len(ids)} dop={drop_off_point_uuid[:8]}]",
        fail_fast=fail_fast,
    )
    if not (200 <= status_code < 300):
        print(f"[UzumOpenAPI] invoice {'update' if update_only else 'create'} "
              f"HTTP {status_code}: {text[:400]}")
        _raise_uzum_error(parsed, status_code, text, url)

    payload = parsed.get("payload") if isinstance(parsed, dict) else None
    if not isinstance(payload, dict):
        raise RuntimeError(
            f"create_fbs_invoice: unexpected response shape "
            f"({type(parsed).__name__})"
        )
    return (payload, url)


# Invoice statuses (verified live 2026-05-25 against /v1/fbs/invoice
# swagger). Order matters for tab rendering in the UI — sellers act
# on CREATED first; ACCEPTED and CANCELLED are terminal-ish.
FBS_INVOICE_STATUSES: tuple[str, ...] = (
    "CREATED",
    "ACCEPTANCE_IN_PROGRESS",
    "ACCEPTED",
    "CANCELLED",
)


def fetch_fbs_invoices_list(
    token: str,
    *,
    statuses: list[str] | None = None,
    page: int = 0,
    size: int = 20,
    accept_language: str | None = None,
    fail_fast: bool = False,
) -> tuple[list[dict], str]:
    """GET /v1/fbs/invoice — list invoices created by this seller.

    Swagger spec (2026-05-25):
      Query: statuses (array<string>, REQUIRED) — repeated, from
             FBS_INVOICE_STATUSES enum.
             size (int32, default 20) — page size.
             page (int32, default 0) — 0-based page index.
      Header: Accept-Language (ru|uz, default uz)
      Response 200: {"payload": [<invoice>, ...], "errors": [...], ...}

    Per-invoice shape (verified live against #943194):
      {"id", "number", "status": {"value", "text", "color"},
       "fullPrice", "acceptedPrice", "numberOrders",
       "numberAcceptedOrders",
       "stock": {"id", "title", "address", ...},
       "dateCreated" (epoch ms), "dateUpdated" (epoch ms),
       "acceptanceStartedDate", "acceptedDate",
       "timeSlot": {"uuid", "timeFrom" (epoch ms), "timeTo"},
       "dropOffPoint": {"uuid", "address", "type"},
       "ettn": null | {"ettnId", "isEditable", "status", ...}}

    Note: NO ``sellerId`` field in the response. Uzum filters by the
    token's owner internally and the field isn't echoed back.
    """
    token = _clean(token)
    if not statuses:
        statuses = list(FBS_INVOICE_STATUSES)
    # IMPORTANT (verified live 2026-06-08 via scripts/_probe_inv_page):
    # Uzum's /v1/fbs/invoice behaviour, confirmed against the real token:
    #   • ``statuses`` is REQUIRED and may repeat (multi-value works fine).
    #   • ``size`` → ALWAYS HTTP 400 ``bad-request-001`` (cannot change it).
    #   • ``page`` ALONE (no ``size``) → accepted (HTTP 200). Page size is
    #     FIXED at 20 server-side; a full 20-row page means more may follow.
    # TRUE server-side pagination: we fetch ONE page per call and let the
    # caller drive Prev/Next. This is O(1) per view — a seller with 20 or
    # 20 000 invoices pays the same single call — instead of sweeping the
    # whole history on every open. ``size`` is accepted for call-site
    # compatibility but never forwarded (Uzum would 400).
    page = max(0, int(page or 0))
    base_qs = "&".join(f"statuses={s}" for s in statuses)
    url = f"{OPENAPI_BASE}/v1/fbs/invoice?{base_qs}&page={page}"
    # Bosqich A.8 — this call MUST go through the shared per-token gate like
    # every other FBS call, or a list load fired next to a worker/count call
    # on the same token trips Uzum's per-token burst penalty (429). Reserving
    # a 1s slot interleaves it safely.
    pace_uzum_call(token)
    parsed, status_code, text, _ = _fbs_orders_request_with_auth(
        url, token, method="GET",
        accept_language=accept_language,
        debug_label=f"fbs.invoice.list[statuses={','.join(statuses)} p={page}]",
        fail_fast=fail_fast,
    )
    if not (200 <= status_code < 300):
        _raise_uzum_error(parsed, status_code, text, url)

    invoices: list[dict] = []
    if isinstance(parsed, dict):
        payload = parsed.get("payload")
        if isinstance(payload, list):
            invoices = [i for i in payload if isinstance(i, dict)]
    return (invoices, url)


def fetch_fbs_invoice_detail(
    token: str,
    invoice_id: str | int,
    *,
    accept_language: str | None = None,
    fail_fast: bool = False,
) -> tuple[dict, str]:
    """GET /v1/fbs/invoice/{invoiceId} — single invoice detail.

    Swagger spec (2026-05-25):
      Path: invoiceId (int64, required)
      Header: Accept-Language (ru|uz, default uz)
      Response 200: {"payload": <invoice>, ...}
      403: fbs-2-seller-access-denied
      404: fbs-invoice-01

    Returns the same shape as one entry of :func:`fetch_fbs_invoices_list`.
    """
    token = _clean(token)
    try:
        iid = int(invoice_id)
    except (TypeError, ValueError):
        raise RuntimeError(f"fetch_fbs_invoice_detail: invoiceId must be int-like, got {invoice_id!r}")

    url = f"{OPENAPI_BASE}/v1/fbs/invoice/{iid}"
    # Bosqich A.8 — pace through the shared per-token gate (see
    # fetch_fbs_invoices_list). Expanding an invoice card fires this right
    # after the list load, so without pacing the two collide on the token.
    pace_uzum_call(token)
    parsed, status_code, text, _ = _fbs_orders_request_with_auth(
        url, token, method="GET",
        accept_language=accept_language,
        debug_label=f"fbs.invoice.detail[{iid}]",
        fail_fast=fail_fast,
    )
    if not (200 <= status_code < 300):
        _raise_uzum_error(parsed, status_code, text, url)

    payload = parsed.get("payload") if isinstance(parsed, dict) else None
    if not isinstance(payload, dict):
        raise RuntimeError(
            f"fetch_fbs_invoice_detail: unexpected payload shape "
            f"({type(payload).__name__})"
        )
    return (payload, url)


def fetch_fbs_invoice_orders(
    token: str,
    invoice_id: str | int,
    *,
    accept_language: str | None = None,
    fail_fast: bool = False,
) -> tuple[list[dict], str]:
    """GET /v1/fbs/invoice/{invoiceId}/orders — orders attached to an invoice.

    This is the AUTHORITATIVE membership of an invoice: Uzum returns the
    exact orders it has linked, so callers no longer guess from the local
    cache. The old joins — ``raw_json->>'invoiceNumber'`` (detail view) and
    ``invoice_number LIKE '%suffix'`` (change-pickup) — fired before the
    sync worker had populated those columns, leaving freshly-created
    invoices looking empty, and the LIKE suffix could collide.

    Path: ``invoiceId`` (int64, required) — the SHORT id (e.g. 327497),
          NOT the 12-digit ``number``.
    Header: Accept-Language (ru|uz, default uz)
    Response 200: ``{"payload": [{"orderId", "fullPrice", "items": [...]}]}``
      403: fbs-2-seller-access-denied (token doesn't own the invoice)

    Per-order shape (verified live 2026-06-01 against invoice 327497)::

        {"orderId": <int>, "fullPrice": <int>,
         "items": [{"orderId", "barcode", "skuTitle", "title", "amount",
                    "price", "skuId", "photo", "status", "deliverUntil"}]}

    Returns ``(orders_list, used_url)`` — empty list for any unexpected
    payload shape.
    """
    token = _clean(token)
    try:
        iid = int(invoice_id)
    except (TypeError, ValueError):
        raise RuntimeError(
            f"fetch_fbs_invoice_orders: invoiceId must be int-like, got {invoice_id!r}"
        )

    url = f"{OPENAPI_BASE}/v1/fbs/invoice/{iid}/orders"
    # Bosqich A.8 — pace through the shared per-token gate. The detail route
    # fires this right after fetch_fbs_invoice_detail on the same token; the
    # gate serialises the two ~1s apart so the pair never bursts Uzum (429).
    pace_uzum_call(token)
    parsed, status_code, text, _ = _fbs_orders_request_with_auth(
        url, token, method="GET",
        accept_language=accept_language,
        debug_label=f"fbs.invoice.orders[{iid}]",
        fail_fast=fail_fast,
    )
    if not (200 <= status_code < 300):
        _raise_uzum_error(parsed, status_code, text, url)

    orders: list[dict] = []
    if isinstance(parsed, dict):
        payload = parsed.get("payload")
        if isinstance(payload, list):
            orders = [o for o in payload if isinstance(o, dict)]
    return (orders, url)


def change_invoice_pickup(
    token: str,
    *,
    invoice_id: int | str,
    seller_id: int,
    drop_off_point_uuid: str,
    time_slot_uuid: str,
    accept_language: str | None = None,
    fail_fast: bool = False,
) -> tuple[dict, str]:
    """Move an existing invoice to a new drop-off point + time slot.

    The orders that ride on the invoice are read from Uzum first
    (:func:`fetch_fbs_invoice_orders` — the authoritative membership), then
    passed verbatim to :func:`create_fbs_invoice` with ``update_only=True``
    (``POST /v1/fbs/invoice/dop/time-slot``). This replaces the old fragile
    ``invoice_number LIKE '%suffix'`` DB guess, which could collide on the
    suffix and was empty before the sync worker had populated the column.

    Returns ``(invoice_payload, used_url)``. Raises :class:`ValueError`
    when Uzum reports no orders on the invoice — there is nothing to move,
    and firing the mutation with an empty ``orderIds`` would 400 anyway.
    """
    orders, _ = fetch_fbs_invoice_orders(
        token, invoice_id, accept_language=accept_language, fail_fast=fail_fast
    )
    order_ids = [
        o.get("orderId") for o in orders
        if isinstance(o, dict) and o.get("orderId") is not None
    ]
    if not order_ids:
        raise ValueError(
            f"change_invoice_pickup: invoice {invoice_id} has no linked orders"
        )
    return create_fbs_invoice(
        token,
        order_ids=order_ids,
        drop_off_point_uuid=drop_off_point_uuid,
        time_slot_uuid=time_slot_uuid,
        seller_id=seller_id,
        update_only=True,
        accept_language=accept_language,
        fail_fast=fail_fast,
    )


def fetch_fbs_invoice_akt_pdf(
    token: str,
    invoice_id: str | int,
    *,
    accept_language: str | None = None,
    fail_fast: bool = False,
) -> tuple[bytes, str]:
    """GET /v1/fbs/invoice/{invoiceId}/print — official "Акт поставки" PDF.

    Uzum returns ``{"payload": {"document": "<base64 PDF>"}, ...}`` — we
    decode the base64 and hand back raw PDF bytes. The PDF already has
    all komitent fields (FIO, PINFL, INN, contract №, etc.) filled in
    from Uzum's own seller registration, so we don't need any of that
    data locally.

    Returns ``(pdf_bytes, used_url)``.
    """
    import base64 as _base64

    token = _clean(token)
    try:
        iid = int(invoice_id)
    except (TypeError, ValueError):
        raise RuntimeError(f"fetch_fbs_invoice_akt_pdf: invoiceId must be int-like, got {invoice_id!r}")

    url = f"{OPENAPI_BASE}/v1/fbs/invoice/{iid}/print"
    parsed, status_code, text, _ = _fbs_orders_request_with_auth(
        url, token, method="GET",
        accept_language=accept_language,
        debug_label=f"fbs.invoice.print[{iid}]",
        fail_fast=fail_fast,
    )
    if not (200 <= status_code < 300):
        _raise_uzum_error(parsed, status_code, text, url)

    payload = parsed.get("payload") if isinstance(parsed, dict) else None
    document = payload.get("document") if isinstance(payload, dict) else None
    if not isinstance(document, str) or not document:
        raise RuntimeError(
            f"fetch_fbs_invoice_akt_pdf: missing payload.document in response"
        )
    try:
        pdf_bytes = _base64.b64decode(document)
    except Exception as exc:
        raise RuntimeError(f"fetch_fbs_invoice_akt_pdf: base64 decode failed: {exc}")
    if not pdf_bytes.startswith(b"%PDF"):
        raise RuntimeError("fetch_fbs_invoice_akt_pdf: decoded payload is not a PDF")
    return (pdf_bytes, url)


# ─────────────────────────────────────────────────────────────────────────
# O6 — FBS/DBS SKU stock management (GET read + POST update)
# Official swagger (2026-06-02): GET/POST /v2/fbs/sku/stocks. Both are
# interactive-only (the «Ombor» page) → callers pass fail_fast=True.
# ─────────────────────────────────────────────────────────────────────────
def fetch_fbs_sku_stocks(
    token: str,
    *,
    accept_language: str | None = None,
    fail_fast: bool = False,
) -> tuple[list[dict], str]:
    """GET /v2/fbs/sku/stocks — SKU stocks available for FBS/DBS update.

    Swagger (2026-06-02): NO parameters. Returns every SKU the token's
    seller may update (FBS + DBS eligible), one row each::

        {"skuId", "skuTitle", "productTitle", "barcode", "amount",
         "fbsAllowed", "dbsAllowed", "fbsLinked", "dbsLinked",
         "sellerSkuCode"}

    Needs the ``SKU_READ`` permission on the token (else 403
    fbs-2-seller-access-denied). No image is returned — the route enriches
    each row from our local ``Variant`` table by skuId.

    Returns ``(sku_list, used_url)``.
    """
    token = _clean(token)
    url = f"{OPENAPI_BASE}/v2/fbs/sku/stocks"
    parsed, status_code, text, _ = _fbs_orders_request_with_auth(
        url, token, method="GET",
        accept_language=accept_language,
        debug_label="fbs.sku.stocks.list",
        fail_fast=fail_fast,
    )
    if not (200 <= status_code < 300):
        _raise_uzum_error(parsed, status_code, text, url)
    skus: list[dict] = []
    if isinstance(parsed, dict):
        payload = parsed.get("payload")
        if isinstance(payload, dict):
            lst = payload.get("skuAmountList")
            if isinstance(lst, list):
                skus = [s for s in lst if isinstance(s, dict)]
    return (skus, url)


def update_fbs_sku_stocks(
    token: str,
    *,
    sku_amounts: list[dict],
    accept_language: str | None = None,
    fail_fast: bool = False,
) -> tuple[dict, str]:
    """POST /v2/fbs/sku/stocks — update FBS/DBS stock for the given SKUs.

    Body (verified 2026-06-02)::

        {"skuAmountList": [
          {"skuId", "skuTitle", "productTitle", "barcode",
           "amount", "fbsLinked", "dbsLinked"}
        ]}

    We round-trip the exact descriptive fields the GET returned
    (skuTitle/productTitle/barcode) so Uzum's validation matches, changing
    only ``amount`` (plus whatever fbsLinked/dbsLinked the caller passes).
    Needs the ``SKU_UPDATE`` permission (else 403).

    ``sku_amounts`` — list of dicts; each MUST carry an int-like ``skuId``
    (``id`` accepted as an alias) and ``amount``. Rows without a usable
    skuId are dropped; ``amount`` is coerced to a non-negative int. Raises
    :class:`ValueError` when nothing valid remains, so we never fire an
    empty or malformed mutation at Uzum.

    Error codes: storage-service-05 (validation), -09 (update failed),
    storage-service-04 (seller blocked), fbs-2-seller-access-denied
    (no SKU_UPDATE rights).

    Returns ``(response_payload, used_url)``.
    """
    token = _clean(token)
    body_list: list[dict] = []
    for it in (sku_amounts or []):
        if not isinstance(it, dict):
            continue
        sku_id = it.get("skuId", it.get("id"))
        if sku_id is None:
            continue
        try:
            sku_id_int = int(sku_id)
        except (TypeError, ValueError):
            continue
        try:
            amount_int = int(it.get("amount") or 0)
        except (TypeError, ValueError):
            amount_int = 0
        if amount_int < 0:
            amount_int = 0
        body_list.append({
            "skuId": sku_id_int,
            "skuTitle": it.get("skuTitle") or "",
            "productTitle": it.get("productTitle") or "",
            "barcode": str(it.get("barcode") or ""),
            "amount": amount_int,
            "fbsLinked": bool(it.get("fbsLinked")),
            "dbsLinked": bool(it.get("dbsLinked")),
        })
    if not body_list:
        raise ValueError("update_fbs_sku_stocks: no valid SKU rows to update")
    body = {"skuAmountList": body_list}
    url = f"{OPENAPI_BASE}/v2/fbs/sku/stocks"
    parsed, status_code, text, _ = _fbs_orders_request_with_auth(
        url, token, method="POST", json_body=body,
        accept_language=accept_language,
        debug_label=f"fbs.sku.stocks.update[n={len(body_list)}]",
        fail_fast=fail_fast,
    )
    if not (200 <= status_code < 300):
        _raise_uzum_error(parsed, status_code, text, url)
    payload = parsed.get("payload") if isinstance(parsed, dict) else None
    return (payload if isinstance(payload, dict) else {}, url)


def download_fbs_label(
    token: str,
    order_id: str | int,
    *,
    size: str = "LARGE",
    fail_fast: bool = False,
) -> tuple[list[bytes], str]:
    """GET /v1/fbs/order/{orderId}/labels/print?size=LARGE|BIG

    Returns ``(pdf_bytes_list, used_url)`` — each entry is a fully
    decoded PDF (Uzum may return more than one for multi-package orders).
    The caller is responsible for streaming them to the browser; we don't
    cache because each print should be a fresh barcode.

    Possible Uzum errors:
      - seller-order-01: order not found
      - seller-order-14: label service temporarily unavailable
      - seller-order-15: customer order identifiers missing
    """
    token = _clean(token)
    try:
        oid = int(order_id)
    except (TypeError, ValueError):
        raise RuntimeError(f"download_fbs_label: orderId must be int-like, got {order_id!r}")
    size_norm = (size or "LARGE").strip().upper()
    if size_norm not in ("LARGE", "BIG"):
        size_norm = "LARGE"

    url = f"{OPENAPI_BASE}/v1/fbs/order/{oid}/labels/print?size={size_norm}"
    # Bosqich A.8 (#4) — pace label download. Bulk print fans out up to 5
    # concurrent download_fbs_label calls on ONE token; the 5-min label
    # cache absorbs repeat clicks, but a first-time bulk print of N fresh
    # orders still bursts without this gate. See confirm_fbs_order.
    pace_uzum_call(token)
    parsed, status_code, text, _ = _fbs_orders_request_with_auth(
        url, token, method="GET",
        accept_language=None,
        debug_label=f"fbs.label[{oid} size={size_norm}]",
        fail_fast=fail_fast,
    )
    if not (200 <= status_code < 300):
        _raise_uzum_error(parsed, status_code, text, url)

    payload = parsed.get("payload") if isinstance(parsed, dict) else None
    docs_b64: list[str] = []
    if isinstance(payload, dict):
        docs = payload.get("document")
        # Uzum's swagger documented this as a list, but the live endpoint
        # was observed 2026-05-24 returning a single base64 string when
        # the order has just one package (typical for FBS). Accept both
        # shapes: list → one PDF per entry, string → single PDF.
        if isinstance(docs, list):
            docs_b64 = [str(d).strip() for d in docs if d and str(d).strip()]
        elif isinstance(docs, str) and docs.strip():
            docs_b64 = [docs.strip()]
    if not docs_b64:
        raise UzumAPIError(
            status_code, None,
            "Uzum returned no label PDFs in payload.document",
            text, url,
        )

    decoded: list[bytes] = []
    for b64 in docs_b64:
        try:
            decoded.append(base64.b64decode(b64))
        except (ValueError, base64.binascii.Error) as e:
            raise UzumAPIError(
                status_code, None,
                f"Failed to decode base64 PDF: {e}",
                text, url,
            )
    return (decoded, url)


def fetch_fbs_return_reasons(token: str, *, fail_fast: bool = False) -> tuple[list[dict], str]:
    """GET /v1/fbs/order/return-reasons — enum used by the cancel modal.

    Returns ``(reasons_list, used_url)``. Each entry is the raw dict
    Uzum returned (usually ``{"code": "OUT_OF_STOCK", "title": "..."}``,
    field names not fully documented — we pass through and let the
    template handle whatever keys are present).

    The endpoint contents change rarely (it's an enum) so the data
    layer caches this with a 1-hour soft TTL.
    """
    token = _clean(token)
    url = f"{OPENAPI_BASE}/v1/fbs/order/return-reasons"
    parsed, status_code, text, _ = _fbs_orders_request_with_auth(
        url, token, method="GET",
        accept_language=None,
        debug_label="fbs.return-reasons",
        fail_fast=fail_fast,
    )
    if not (200 <= status_code < 300):
        _raise_uzum_error(parsed, status_code, text, url)

    payload = parsed.get("payload") if isinstance(parsed, dict) else parsed
    if isinstance(payload, list):
        return (payload, url)
    if isinstance(payload, dict):
        for k in ("reasons", "items", "data"):
            v = payload.get(k)
            if isinstance(v, list):
                return (v, url)
    # Unknown shape — return empty list rather than raise, the cancel
    # modal will fall back to a hard-coded enum.
    return ([], url)


# ─────────────────────────────────────────────────────────────────────
# DBS (Delivery by Seller) action endpoints — confirmed against Uzum
# seller-openapi swagger 2026-05-21.
#
# Workflow (DBS-only; FBS uses the /v1/fbs/* action set):
#   PACKING   → /delivering → DELIVERING
#   DELIVERING → /completed   → COMPLETED
#   COMPLETED  → /refund      → RETURNED (refund initiated)
#
# All three accept the orderId in the path and DO NOT take a request
# body. ``/completed`` is the only one with a query parameter
# (``issueCode`` — the SMS verification code some customers must read
# to the courier at handover).
#
# DBS-specific error code beyond the FBS set:
#   fbs-18-invalid-order-type — DBS endpoint called on a FBS order
# Code numbers 37/38/39 appear in the FBS error table for completeness
# but are only raised by /completed.
# ─────────────────────────────────────────────────────────────────────


def mark_dbs_delivering(token: str, order_id: str | int, *, fail_fast: bool = False) -> tuple[dict, str]:
    """POST /v1/dbs/order/{orderId}/delivering — seller picks up the
    order from the warehouse and starts delivering it.

    Body: NONE. Returns ``(payload_dict, used_url)`` — payload is the
    full order JSON with status flipped to DELIVERING.

    Possible Uzum errors:
      - seller-order-01: order not found
      - seller-order-02: wrong status (only PACKING is valid)
      - seller-order-03: delivery deadline passed
      - seller-order-15: IMEIs not attached (identifierRequired orders)
      - fbs-18-invalid-order-type: called on a non-DBS order
    """
    token = _clean(token)
    try:
        oid = int(order_id)
    except (TypeError, ValueError):
        raise RuntimeError(f"mark_dbs_delivering: orderId must be int-like, got {order_id!r}")

    url = f"{OPENAPI_BASE}/v1/dbs/order/{oid}/delivering"
    parsed, status_code, text, _ = _fbs_orders_request_with_auth(
        url, token, method="POST",
        accept_language=None,
        debug_label=f"dbs.delivering[{oid}]",
        fail_fast=fail_fast,
    )
    if not (200 <= status_code < 300):
        _raise_uzum_error(parsed, status_code, text, url)

    payload = parsed.get("payload") if isinstance(parsed, dict) else None
    return (payload if isinstance(payload, dict) else {}, url)


def mark_dbs_completed(
    token: str,
    order_id: str | int,
    *,
    issue_code: int | None = None,
    fail_fast: bool = False,
) -> tuple[dict, str]:
    """POST /v1/dbs/order/{orderId}/completed[?issueCode=N] — customer
    accepted the package, order complete.

    ``issue_code`` is optional. Some Uzum customers receive an SMS code
    that they read back to the courier — passing it proves the seller
    actually met the customer. Other orders don't require this and the
    call succeeds without the param.

    Body: NONE. Returns ``(payload_dict, used_url)``.

    Possible Uzum errors:
      - seller-order-01: order not found
      - seller-order-02: wrong status (only DELIVERING is valid)
      - seller-order-03: delivery deadline passed
      - fbs-18-invalid-order-type: called on a non-DBS order
      - seller-order-37: code was required but missing
      - seller-order-38: code wrong
      - seller-order-39: code wrong + rate-limited (retry later)
    """
    token = _clean(token)
    try:
        oid = int(order_id)
    except (TypeError, ValueError):
        raise RuntimeError(f"mark_dbs_completed: orderId must be int-like, got {order_id!r}")

    url = f"{OPENAPI_BASE}/v1/dbs/order/{oid}/completed"
    if issue_code is not None:
        try:
            url += f"?issueCode={int(issue_code)}"
        except (TypeError, ValueError):
            raise RuntimeError(
                f"mark_dbs_completed: issue_code must be int-like, got {issue_code!r}"
            )

    parsed, status_code, text, _ = _fbs_orders_request_with_auth(
        url, token, method="POST",
        accept_language=None,
        debug_label=f"dbs.completed[{oid} code={issue_code if issue_code is not None else '-'}]",
        fail_fast=fail_fast,
    )
    if not (200 <= status_code < 300):
        _raise_uzum_error(parsed, status_code, text, url)

    payload = parsed.get("payload") if isinstance(parsed, dict) else None
    return (payload if isinstance(payload, dict) else {}, url)


def refund_dbs_order(token: str, order_id: str | int, *, fail_fast: bool = False) -> tuple[dict, str]:
    """POST /v1/dbs/order/{orderId}/refund — start a refund for a
    completed DBS order.

    Body: NONE (Uzum's swagger confirms this — no items list, no reason
    enum — single-button action). Returns ``(parsed_body, used_url)``;
    payload is usually ``{}`` and the meaningful signal is HTTP 200.

    Possible Uzum errors:
      - seller-order-01: order not found
      - seller-order-02: wrong status (only COMPLETED is valid)
      - seller-order-13: order is already cancelled or in refund flow
    """
    token = _clean(token)
    try:
        oid = int(order_id)
    except (TypeError, ValueError):
        raise RuntimeError(f"refund_dbs_order: orderId must be int-like, got {order_id!r}")

    url = f"{OPENAPI_BASE}/v1/dbs/order/{oid}/refund"
    parsed, status_code, text, _ = _fbs_orders_request_with_auth(
        url, token, method="POST",
        accept_language=None,
        debug_label=f"dbs.refund[{oid}]",
        fail_fast=fail_fast,
    )
    if not (200 <= status_code < 300):
        _raise_uzum_error(parsed, status_code, text, url)

    return (parsed if isinstance(parsed, dict) else {}, url)


# ── Order statuses (canonical enum from /v2/fbs/orders swagger) ────────
# Single source of truth so routes/templates can import instead of
# duplicating the list. Order matters: this is the lifecycle.
FBS_ORDER_STATUSES = (
    "CREATED",
    "PACKING",
    "PENDING_DELIVERY",
    "DELIVERING",
    "DELIVERED",
    "ACCEPTED_AT_DP",
    "DELIVERED_TO_CUSTOMER_DELIVERY_POINT",
    "COMPLETED",
    "CANCELED",
    "PENDING_CANCELLATION",
    "RETURNED",
)
FBS_ORDER_SCHEMES = ("FBS", "DBS")


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
