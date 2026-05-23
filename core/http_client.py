"""HTTP client helpers: session pooling, JSON requests, multipart uploads."""
from __future__ import annotations

import json
import os
import threading
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

import requests
from urllib3.util.retry import Retry

from config import HTTP_POOL_MAXSIZE, HTTP_USER_AGENT, HTTP_ACCEPT_LANGUAGE

_http_local = threading.local()


class TokenBucket:
    """Process-wide rate limiter for Uzum API calls keyed by token.

    Uzum's OpenAPI enforces (empirically) capacity=2 burst tokens replenished
    at ~2/sec per token. When the backfill / hourly loop / variant seed all
    use the same per-user token concurrently, naive parallelism instantly
    blows past the burst → 429 storm. This bucket serializes them at the
    application layer so we never overrun.

    Threads call ``acquire()`` before issuing a request; the call blocks until
    a token is available, then decrements the pool.
    """

    def __init__(self, capacity: int = 2, refill_per_sec: float = 2.0):
        self.capacity = float(capacity)
        self.refill_per_sec = float(refill_per_sec)
        self.tokens = float(capacity)
        self.last_refill = time.monotonic()
        self._cond = threading.Condition()

    def acquire(self) -> None:
        with self._cond:
            while True:
                now = time.monotonic()
                elapsed = now - self.last_refill
                if elapsed > 0:
                    self.tokens = min(
                        self.capacity, self.tokens + elapsed * self.refill_per_sec
                    )
                    self.last_refill = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                wait = max(0.01, (1.0 - self.tokens) / self.refill_per_sec)
                self._cond.wait(timeout=wait)


# One bucket per OpenAPI token. Threads sharing a token share a bucket;
# different tokens get independent buckets (and thus independent rate budgets).
_buckets_lock = threading.Lock()
_buckets: dict[str, TokenBucket] = {}

# Conservative defaults: 1 token capacity, 1/sec refill.
# Empirical Uzum refill is ~1.2-2/sec but capacity=1 ensures STRICTLY
# sequential pacing — no concurrent in-flight requests at all. This is
# slower than the bucket-burst design but guarantees zero 429s regardless
# of how many concurrent callers (products + finance + backfill) compete.
# Can be raised via env vars if higher throughput is needed and Uzum's
# rate limit allows.
_BUCKET_CAPACITY = int(os.getenv("UZUM_OPENAPI_BUCKET_CAPACITY", "1"))
_BUCKET_REFILL_PER_SEC = float(os.getenv("UZUM_OPENAPI_BUCKET_REFILL_PER_SEC", "1.0"))


def get_bucket_for_token(token: str) -> TokenBucket:
    """Return (and create if needed) the shared TokenBucket for ``token``.

    Same token across threads → same bucket → cooperative rate limiting.
    Tokens use a short key (first 16 chars) so the dict doesn't bloat with
    long secrets in memory dumps.
    """
    if not token:
        # Fall back to a sentinel bucket so callers without a token still pace.
        key = "_no_token_"
    else:
        key = token.strip()[:16]
    with _buckets_lock:
        b = _buckets.get(key)
        if b is None:
            b = TokenBucket(
                capacity=_BUCKET_CAPACITY, refill_per_sec=_BUCKET_REFILL_PER_SEC
            )
            _buckets[key] = b
        return b


class AdditiveBackoffRetry(Retry):
    """urllib3 Retry subclass with short additive backoff: 1s, 2s, 3s.

    Original design used 60s/120s/180s waits, which made backfill thrash
    for minutes when 429s hit. With the application-layer TokenBucket
    preventing most 429s upstream, this retry only needs to catch the
    rare race-condition slip (e.g. a sibling thread that started right
    before bucket-pacing kicked in). A few seconds of backoff is enough.
    """

    BACKOFF_MAX = 60

    def get_backoff_time(self) -> float:
        consecutive_errors = len(
            [h for h in self.history if h.redirect_location is None]
        )
        if consecutive_errors <= 0:
            return 0
        # 1s for first retry, 2s for second — short enough that products
        # sync doesn't visibly hang on startup, long enough for Uzum's
        # bucket to refill at least one token.
        return min(self.BACKOFF_MAX, 1.0 * consecutive_errors)


def _get_http_session():
    """Per-thread requests session with connection pooling and auto-retry."""
    sess = getattr(_http_local, "session", None)
    if sess is None:
        sess = requests.Session()
        # 429 IS in the forcelist as a safety net — the bucket should
        # prevent 429s, but at startup multiple concurrent callers (products
        # sync + variant seed + 2 backfill threads) can briefly out-pace
        # Uzum's refill. Two short retries (1s + 2s) catch those without
        # the user-visible 60+120+180s stalls of the old design.
        retry_strategy = AdditiveBackoffRetry(
            total=3,
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
        _http_local.session = sess
    return sess


def http_json(url: str, method: str = "GET", body: dict | None = None, headers: dict | None = None, *, _get_admin_token=None) -> dict:
    """Make a JSON HTTP request. Pass _get_admin_token callable to auto-inject auth."""
    req_headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": HTTP_USER_AGENT,
        "Accept-Language": HTTP_ACCEPT_LANGUAGE,
    }

    # Optional extra headers (JSON)
    try:
        extra = os.getenv("HTTP_EXTRA_HEADERS_JSON", "").strip()
        if extra:
            req_headers.update(json.loads(extra))
    except Exception:
        pass

    if headers:
        req_headers.update(headers)

    # Always use the admin's Uzum Bearer token for direct API calls.
    try:
        if "Authorization" not in req_headers and _get_admin_token is not None:
            k = _get_admin_token()
            if k:
                req_headers["Authorization"] = k if k.startswith("Bearer ") else f"Bearer {k}"
    except Exception:
        pass

    try:
        sess = _get_http_session()
        if body is not None:
            req_headers["Content-Type"] = "application/json"
        resp = sess.request(
            method=method,
            url=url,
            json=body if body is not None else None,
            headers=req_headers,
            timeout=60,
        )
        raw = resp.text or ""
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}: {raw[:200]}")
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise RuntimeError(f"Non-JSON response from server: {raw[:200]}") from None
    except requests.RequestException as e:
        raise RuntimeError(f"Network error: {e}") from e


def http_post_multipart(url: str, file_name: str, file_bytes: bytes, headers: dict | None = None) -> dict:
    """Helper to send a multipart/form-data POST request for file uploads."""
    import uuid
    boundary = uuid.uuid4().hex
    body = bytearray()

    body.extend(f"--{boundary}\r\n".encode("utf-8"))
    body.extend(f'Content-Disposition: form-data; name="file"; filename="{file_name}"\r\n'.encode("utf-8"))
    body.extend(b'Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n')
    body.extend(file_bytes)
    body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode("utf-8"))

    req_headers = {
        "User-Agent": HTTP_USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(body)),
        "Origin": "https://seller.uzum.uz",
        "Referer": "https://seller.uzum.uz/",
        "Connection": "close"
    }
    try:
        extra = os.getenv("HTTP_EXTRA_HEADERS_JSON", "").strip()
        if extra:
            req_headers.update(json.loads(extra))
    except Exception:
        pass

    if headers:
        req_headers.update(headers)

    req = Request(url=url, method="POST", data=bytes(body), headers=req_headers)
    try:
        with urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except HTTPError as e:
        msg = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {msg}") from e
    except URLError as e:
        raise RuntimeError(f"Network error: {e}") from e
