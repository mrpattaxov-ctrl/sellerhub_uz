"""HTTP client helpers: session pooling, JSON requests, multipart uploads."""
from __future__ import annotations

import json
import os
import threading
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

import requests
from urllib3.util.retry import Retry

from config import HTTP_POOL_MAXSIZE, HTTP_USER_AGENT, HTTP_ACCEPT_LANGUAGE

_http_local = threading.local()


class AdditiveBackoffRetry(Retry):
    """urllib3 Retry subclass that waits additively: 60s, 120s, 180s.

    urllib3's built-in Retry only supports exponential backoff
    (``backoff_factor * 2**(n-1)``). For Uzum we want real breathing room
    between retries under 429/5xx, so this overrides ``get_backoff_time``
    to return ``60 * attempt`` instead.
    """

    BACKOFF_MAX = 600  # raise above urllib3's 120s default so 180s isn't clipped

    def get_backoff_time(self) -> float:
        consecutive_errors = len(
            [h for h in self.history if h.redirect_location is None]
        )
        if consecutive_errors <= 0:
            return 0
        return min(self.BACKOFF_MAX, 60.0 * consecutive_errors)


def _get_http_session():
    """Per-thread requests session with connection pooling and auto-retry."""
    sess = getattr(_http_local, "session", None)
    if sess is None:
        sess = requests.Session()
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
