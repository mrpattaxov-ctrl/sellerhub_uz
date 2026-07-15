#!/usr/bin/env python
"""uicheck — log into SellerHub and screenshot/diagnose pages from the CLI.

Why: after a UI or backend change we want to *see* the running app, not guess.
This drives a real Chromium against a real logged-in session and reports what a
user would hit: HTTP status, JS console errors, failed XHRs, plus a PNG.

Login uses the app's own Telegram-approval flow — the phone owner gets a
"Подтвердить вход / Отклонить" message in Telegram and must tap approve. The
resulting Flask session cookie is stored in .uicheck/state.json (gitignored)
and is good for 30 days (PERMANENT_SESSION_LIFETIME).

Usage:
    python tools/uicheck.py login --phone +998998110950
    python tools/uicheck.py whoami
    python tools/uicheck.py shot /fbs/orders --full-page
    python tools/uicheck.py shot /fbs/orders --click "text=Yangilash" --wait-ms 2000
    python tools/uicheck.py shot / --width 390 --height 844   # mobile viewport
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

# The Windows console defaults to cp1251 here, which cannot encode the arrows
# and check marks below — printing one kills the process mid-run.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / ".uicheck"
STATE_FILE = STATE_DIR / "state.json"
SHOTS_DIR = STATE_DIR / "shots"
DEFAULT_BASE_URL = "http://localhost:5000"

# Noise we never want to see in the "is it broken?" verdict: third-party
# widgets, favicon 404s, and the browser's own extension chatter.
IGNORED_URL_PATTERNS = (
    "favicon.ico",
    "chrome-extension://",
    "/__webpack_hmr",
)


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    if not STATE_FILE.exists():
        sys.exit(
            "No saved session. Run:  python tools/uicheck.py login --phone +998XXXXXXXXX"
        )
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def _save_state(*, base_url: str, phone: str, storage_state: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(
            {
                "base_url": base_url,
                "phone": phone,
                "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "storage_state": storage_state,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------

def cmd_login(args: argparse.Namespace) -> int:
    """Drive the real login page in a browser.

    Doing this in the browser rather than over the API on purpose: the page's own
    JS owns the send-approval + polling handshake, so we inherit whatever the
    real user gets, and the session cookie lands in the browser context where we
    need it anyway.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("Playwright missing. Run: pip install playwright && playwright install chromium")

    base_url = args.base_url.rstrip("/")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed)
        context = browser.new_context(viewport={"width": 1280, "height": 900}, locale="uz-UZ")
        page = context.new_page()
        page.goto(f"{base_url}/login", wait_until="domcontentloaded", timeout=30000)

        page.fill("#tg-phone", args.phone)
        page.click("#btn-tg-send")
        print(f"→ Approval request sent for {args.phone}.")

        # The page shows #tg-error for a rejected phone, and swaps to the bot-link
        # step when the number has no Telegram account linked yet.
        page.wait_for_timeout(2500)
        error_text = (page.text_content("#tg-error") or "").strip()
        if error_text:
            browser.close()
            print(f"✗ Login page reported: {error_text}")
            return 1
        if page.is_visible("#tg-bot-step"):
            browser.close()
            print(f"✗ {args.phone} has no Telegram account linked to it — link it in the bot first.")
            return 1

        print(
            "  Open Telegram and tap «✅ Подтвердить вход».\n"
            f"  Waiting up to {args.timeout}s …"
        )
        try:
            page.wait_for_url(
                lambda url: "/login" not in url,
                timeout=args.timeout * 1000,
            )
        except Exception:
            browser.close()
            print("\n✗ Timed out — the request was never approved (or was declined).")
            return 1

        storage_state = context.storage_state()
        browser.close()

    if not any(c["name"] == "session" for c in storage_state.get("cookies", [])):
        print("✗ Approved, but no session cookie was set.")
        return 1

    _save_state(base_url=base_url, phone=args.phone, storage_state=storage_state)

    # Never report success on a cookie we have not seen authenticate something.
    if cmd_whoami(argparse.Namespace()) != 0:
        return 1
    print(f"✓ Logged in. Session saved to {STATE_FILE.relative_to(ROOT)} (valid ~30 days).")
    print("  Now try:  python tools/uicheck.py shot /fbs/orders")
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    """Is the saved session still good? Checked in a browser, since that is the
    only client we actually replay it in."""
    from playwright.sync_api import sync_playwright

    state = _load_state()
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(storage_state=state["storage_state"])
        page = context.new_page()
        page.goto(f"{state['base_url']}/", wait_until="domcontentloaded", timeout=30000)
        landed, title = page.url, page.title()
        browser.close()

    if "/login" in landed:
        print("✗ Session is dead — run `login` again.")
        return 2
    print(f"✓ Session alive as {state['phone']} (saved {state['saved_at']})")
    print(f"  Landed on {landed} — “{title}”")
    return 0


# ---------------------------------------------------------------------------
# screenshot / diagnose
# ---------------------------------------------------------------------------

def _is_noise(url: str) -> bool:
    return any(p in url for p in IGNORED_URL_PATTERNS)


def cmd_shot(args: argparse.Namespace) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("Playwright missing. Run: pip install playwright && playwright install chromium")

    state = _load_state()
    base_url = args.base_url.rstrip("/") if args.base_url else state["base_url"]
    path = args.path if args.path.startswith("/") else "/" + args.path
    url = f"{base_url}{path}"

    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", path).strip("-") or "root"
    stem = args.out or f"{slug}-{time.strftime('%H%M%S')}"

    console_errors: list[str] = []
    page_errors: list[str] = []
    failed_requests: list[str] = []
    bad_responses: list[str] = []
    shots: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed)
        context = browser.new_context(
            storage_state=state["storage_state"],
            viewport={"width": args.width, "height": args.height},
            locale="uz-UZ",
            color_scheme="dark" if args.dark else "light",
        )
        if args.dark:
            # The app's own dark mode is data-bs-theme on <html>, seeded from
            # localStorage at boot — it ignores the OS color-scheme entirely, so
            # setting the context's color_scheme alone renders a light page.
            context.add_init_script("localStorage.setItem('sh-theme', 'dark');")

        page = context.new_page()

        page.on(
            "console",
            lambda m: console_errors.append(f"{m.text[:300]}")
            if m.type == "error" and not _is_noise(m.text)
            else None,
        )
        page.on("pageerror", lambda e: page_errors.append(str(e)[:300]))
        page.on(
            "requestfailed",
            lambda r: failed_requests.append(f"{r.method} {r.url} — {r.failure}")
            if not _is_noise(r.url)
            else None,
        )
        page.on(
            "response",
            lambda r: bad_responses.append(f"{r.status} {r.request.method} {r.url}")
            if r.status >= 400 and not _is_noise(r.url)
            else None,
        )

        response = page.goto(url, wait_until="domcontentloaded", timeout=args.timeout * 1000)
        http_status = response.status if response else None

        try:
            page.wait_for_load_state("networkidle", timeout=args.timeout * 1000)
        except Exception:
            pass  # a page with polling never goes idle; that is not a failure

        if args.wait_selector:
            try:
                page.wait_for_selector(args.wait_selector, timeout=args.timeout * 1000)
            except Exception:
                print(f"✗ Selector never appeared: {args.wait_selector}")

        final_url = page.url
        if "/login" in final_url and "/login" not in path:
            browser.close()
            print(f"✗ Bounced to {final_url} — session expired. Run `login` again.")
            return 2

        def snap(name: str) -> None:
            dest = SHOTS_DIR / f"{name}.png"
            # animations="disabled" — pages here run looping CSS animations
            # (spinners, shimmer), and Playwright waits for them forever otherwise.
            page.screenshot(path=str(dest), full_page=args.full_page, animations="disabled")
            shots.append(str(dest))

        if args.wait_ms:
            page.wait_for_timeout(args.wait_ms)
        snap(stem)

        # Optional interaction steps: exercise buttons and capture the result.
        for i, selector in enumerate(args.click or [], start=1):
            try:
                page.click(selector, timeout=15000)
                page.wait_for_timeout(args.wait_ms or 1500)
                snap(f"{stem}-click{i}")
                print(f"✓ Clicked: {selector}")
            except Exception as exc:
                print(f"✗ Click failed on {selector}: {str(exc).splitlines()[0]}")

        title = page.title()
        browser.close()

    ok = (
        (http_status or 0) < 400
        and not page_errors
        and not console_errors
        and not failed_requests
        and not bad_responses
    )
    report = {
        "url": url,
        "final_url": final_url,
        "http_status": http_status,
        "title": title,
        "screenshots": shots,
        "console_errors": console_errors,
        "page_errors": page_errors,
        "failed_requests": failed_requests,
        "http_errors": bad_responses,
        "ok": ok,
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="uicheck", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_login = sub.add_parser("login", help="log in via Telegram approval and save the session")
    p_login.add_argument("--phone", required=True, help="phone of a Telegram-linked account, e.g. +998998110950")
    p_login.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p_login.add_argument("--timeout", type=int, default=180, help="seconds to wait for the tap")
    p_login.add_argument("--headed", action="store_true", help="show the browser window")
    p_login.set_defaults(func=cmd_login)

    p_who = sub.add_parser("whoami", help="check whether the saved session still works")
    p_who.set_defaults(func=cmd_whoami)

    p_shot = sub.add_parser("shot", help="screenshot a page and report console/network errors")
    p_shot.add_argument("path", help="app path, e.g. /fbs/orders")
    p_shot.add_argument("--out", help="screenshot filename stem")
    p_shot.add_argument("--base-url")
    p_shot.add_argument("--full-page", action="store_true")
    p_shot.add_argument("--width", type=int, default=1440)
    p_shot.add_argument("--height", type=int, default=900)
    p_shot.add_argument("--dark", action="store_true")
    p_shot.add_argument("--headed", action="store_true", help="show the browser window")
    p_shot.add_argument("--wait-selector", help="wait for this selector before shooting")
    p_shot.add_argument("--wait-ms", type=int, default=0, help="extra settle time before shooting")
    p_shot.add_argument("--click", action="append", help="click this selector, then shoot again (repeatable)")
    p_shot.add_argument("--timeout", type=int, default=30, help="per-step timeout in seconds")
    p_shot.set_defaults(func=cmd_shot)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
