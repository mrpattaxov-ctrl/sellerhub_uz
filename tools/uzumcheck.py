#!/usr/bin/env python
"""uzumcheck — screenshot/diagnose the REAL Uzum seller portal (seller.uzum.uz).

Why: `tools/uicheck.py` does this for our own SellerHub. This is the same idea
pointed at Uzum's portal, so we can compare our «Новый товар» screens against
the original pixel-for-pixel instead of relying on captured HAR files.

HOW AUTH WORKS (evidence, not guesswork — from the captured bundle + HARs):
  · Every portal/API call carries `Authorization: Bearer <token>`; the portal
    sets NO cookies at all (full-flow HAR: 2401 requests to seller.uzum.uz,
    zero cookies).
  · The bundle builds that header from the persisted Vuex store:
        e.headers.Authorization = "Bearer ".concat(a["a"].state.user.tokens.access)
    and `state` is one of the localStorage keys the app persists.
  => So a logged-in browser session can be recreated by writing the admin
     token into localStorage["state"].user.tokens.access. No password needed
     and none is ever read by this tool.

TOKEN SOURCE: the admin user's `api_key` in our own DB — the very token the
app already uses for every portal call (core.auth_helpers._get_admin_token).
Read it out of the app container, never hardcode it:

    docker exec sellerhub_uz-app-1 python -c "import json;\
from core.auth_helpers import _get_admin_token;\
open('/tmp/uztok.json','w').write(json.dumps({'access': _get_admin_token().strip()}))"
    docker cp sellerhub_uz-app-1:/tmp/uztok.json <somewhere>/uztok.json

Usage:
    python tools/uzumcheck.py shot /product/create --token-file <path>/uztok.json
    python tools/uzumcheck.py shot / --token-file ... --full-page --headed

⚠️ READ-ONLY BY DESIGN. This drives a real seller account: never point it at a
flow that submits (createProduct, save-filters, ...). It only navigates and
screenshots. Clicks are allowed but you own what you click.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

# Windows console is cp1251 here — printing an arrow kills the process midway.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parent.parent
SHOTS_DIR = ROOT / ".uicheck" / "uzum-shots"
STATE_FILE = ROOT / ".uicheck" / "uzum-state.json"
BASE_URL = "https://seller.uzum.uz"

# Uzum sits behind Yandex SmartCaptcha. Headless Chromium is refused every
# time (3/3 live attempts, 2026-07-21) — it never even reaches auth.
#
# ⚠️ DELIBERATE NON-GOAL: we do NOT try to look like a human to the anti-bot
# check. An earlier revision of this file spoofed navigator.webdriver, the
# user agent, plugins and languages, and disabled Blink's automation flag.
# That defeated SmartCaptcha — and that is exactly why it was removed.
# SmartCaptcha is Uzum's control on Uzum's infrastructure; owning the seller
# account does not make circumventing it ours to decide.
#
# The supported path is `login`: a REAL browser window opens, a HUMAN clears
# the captcha (which is precisely what the control asks for), and the cookies
# that follow are reused by `shot`. Using the installed Chrome instead of the
# bundled headless shell is not evasion — it is just a normal browser.
def _launch(pw, headed: bool):
    """Real Chrome when available, bundled Chromium as a fallback."""
    for channel in ("chrome", None):
        try:
            return pw.chromium.launch(headless=not headed, channel=channel)
        except Exception:
            continue
    return pw.chromium.launch(headless=not headed)

# Third-party noise we never want in the "is it broken?" verdict.
IGNORED_URL_PATTERNS = (
    "favicon.ico",
    "chrome-extension://",
    "growthbook.io",
    "helpdeskeddy.com",
    "sentry",
    "google-analytics",
    "googletagmanager",
    "mc.yandex",
)


def _ignored(url: str) -> bool:
    return any(p in url for p in IGNORED_URL_PATTERNS)


def _read_token(path: str) -> str:
    p = Path(path)
    if not p.exists():
        sys.exit(f"Token file not found: {p}\nSee the docstring for how to export it.")
    raw = p.read_text(encoding="utf-8").strip()
    try:
        tok = (json.loads(raw).get("access") or "").strip()
    except json.JSONDecodeError:
        tok = raw  # allow a bare token file too
    if not tok:
        sys.exit("Token file is empty — is the admin api_key set?")
    # Our DB stores the api_key WITH the "Bearer " prefix (verified: the exported
    # token starts with it), but the bundle builds the header itself as
    # "Bearer ".concat(state.user.tokens.access). Leaving the prefix in would
    # produce "Bearer Bearer <tok>" and every portal call would 403.
    if tok.lower().startswith("bearer "):
        tok = tok[7:].strip()
    return tok


def _shop_id_from_path(path: str) -> int | None:
    """`/seller/7138/products/...` -> 7138."""
    m = re.match(r"/?seller/(\d+)(?:/|$)", path.lstrip("/") and "/" + path.lstrip("/"))
    return int(m.group(1)) if m else None


def _storage_state(token: str, lang: str, shop_id: int | None = None) -> dict:
    """Playwright storage_state that makes the SPA think it is logged in.

    Shape mirrors what the bundle persists (read back off a live session):
      state.user.tokens.access   — the bearer the SPA rebuilds every header from
      state.shop.currentShopId   — WHICH SHOP the cabinet opens
      state.product.showHints    — the hint cards toggle
    The rest is re-fetched with the token, so we keep this minimal.
    """
    persisted: dict = {
        "user": {"tokens": {"access": token, "refresh": ""}},
        # The hint cards are ON in the reference frames; keep them so screenshots
        # match. NOTE: this lives INSIDE `state`, not as its own localStorage key.
        "product": {"showHints": True},
    }
    # Without this the SPA just picks the first shop of the account and ignores
    # the shopId in the URL — and if that shop's profile is incomplete it
    # redirects every page to /seller/profile?type=fill.
    if shop_id:
        persisted["shop"] = {"currentShopId": shop_id}
    state = {
        "cookies": [],
        "origins": [
            {
                "origin": BASE_URL,
                "localStorage": [
                    {"name": "state", "value": json.dumps(persisted, ensure_ascii=False)},
                    # JSON-encoded, not raw: the cabinet SPA JSON.parse()s this key
                    # and a bare `ru` crashes the whole app at boot
                    # (pageerror: Unexpected token 'r', "ru" is not valid JSON)
                    # leaving a blank white page with zero API calls.
                    {"name": "language", "value": json.dumps(lang)},
                ],
            }
        ],
    }
    # Reuse the SmartCaptcha clearance cookie from a previous `login` run, if any.
    # The portal itself needs no cookies — this is purely the anti-bot pass.
    if STATE_FILE.exists():
        try:
            saved = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            state["cookies"] = saved.get("cookies") or []
        except (json.JSONDecodeError, OSError):
            pass
    return state


def cmd_login(args: argparse.Namespace) -> int:
    """Open a REAL browser window so a human can clear the captcha once.

    Nothing is typed for you and no password is read: the portal session comes
    from the injected token, and this step exists only to satisfy the anti-bot
    check. The resulting cookies are saved and reused by `shot`.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("Playwright missing. Run: pip install playwright && playwright install chromium")

    token = _read_token(args.token_file)
    with sync_playwright() as pw:
        browser = _launch(pw, headed=True)
        context = browser.new_context(
            viewport={"width": args.width, "height": args.height},
            locale="ru-RU",
            storage_state=_storage_state(token, args.lang),
        )
        page = context.new_page()
        page.goto(BASE_URL + "/", wait_until="domcontentloaded", timeout=60000)
        print("+ Browser window is open.")
        print("  Solve the captcha if one is shown, then wait — this exits by itself.")
        deadline = time.time() + args.wait_login
        while time.time() < deadline:
            if "showcaptcha" not in page.url and "/login" not in page.url:
                break
            page.wait_for_timeout(1000)
        page.wait_for_timeout(2500)
        final_url, title = page.url, page.title()
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(STATE_FILE))
        browser.close()

    passed = "showcaptcha" not in final_url
    print(json.dumps({"final_url": final_url[:160], "title": title,
                      "captcha_cleared": passed,
                      "state_saved": str(STATE_FILE)}, indent=2, ensure_ascii=False))
    return 0 if passed else 1


def cmd_shot(args: argparse.Namespace) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("Playwright missing. Run: pip install playwright && playwright install chromium")

    token = _read_token(args.token_file)
    url = BASE_URL + ("/" + args.path.lstrip("/") if args.path != "/" else "/")
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)

    console_errors: list[str] = []
    page_errors: list[str] = []
    failed: list[str] = []
    http_errors: list[str] = []
    shots: list[str] = []

    with sync_playwright() as pw:
        browser = _launch(pw, headed=args.headed)
        ctx_kw = {
            "viewport": {"width": args.width, "height": args.height},
            "locale": "ru-RU" if args.lang == "ru" else "uz-UZ",
            "storage_state": _storage_state(
                token, args.lang, args.shop or _shop_id_from_path(args.path)),
        }
        # HAR = the same thing DevTools writes with "Save all as HAR": every
        # request/response of the session. `content="embed"` inlines response
        # bodies, which is what makes the capture useful for reverse-reading
        # the portal's own API shapes later.
        if args.har:
            har_path = Path(args.har)
            har_path.parent.mkdir(parents=True, exist_ok=True)
            ctx_kw["record_har_path"] = str(har_path)
            ctx_kw["record_har_content"] = "embed"
        def _open():
            """One attempt: fresh context, wired listeners, navigated page."""
            ctx = browser.new_context(**ctx_kw)
            pg = ctx.new_page()

            pg.on("console", lambda m: (
                console_errors.append(m.text[:300])
                if m.type == "error" and not _ignored(m.text) else None))
            pg.on("pageerror", lambda e: page_errors.append(str(e)[:300]))
            pg.on("requestfailed", lambda r: (
                failed.append(f"{r.method} {r.url[:140]}") if not _ignored(r.url) else None))

            def _on_response(r):
                if r.status >= 400 and not _ignored(r.url):
                    http_errors.append(f"{r.status} {r.request.method} {r.url[:140]}")
            pg.on("response", _on_response)

            # ⚠️ `goto` ni YIQITMAYMIZ: kabinet ~2200 chunk so'ragani uchun
            # `domcontentloaded` ba'zan belgilangan vaqtga ulgurmaydi, lekin
            # sahifa keyin baribir chiziladi. Timeout — qayta urinish sababi,
            # dastur o'limi emas.
            resp = None
            try:
                resp = pg.goto(url, wait_until="domcontentloaded",
                               timeout=args.timeout * 1000)
            except Exception as e:
                print(f"- goto kutish vaqti tugadi ({str(e).splitlines()[0][:60]}) — davom")
            return ctx, pg, resp

        # The cabinet SPA loses a race against its own check_token call maybe half
        # the time and bounces to /signin?reloaded=true — with a perfectly valid
        # token. Retrying works, but ONLY in a fresh context: on the way out the
        # SPA clears localStorage, so a retry in the same context is guaranteed
        # to fail. Each attempt therefore rebuilds the context from scratch.
        # Ikki xil nosozlik bor va IKKALASIDA ham yangi kontekst kerak:
        #   1) SPA `signin?reloaded=true` ga sakraydi (check_token poygasi);
        #   2) sahifa OQ qoladi — SPA ~2200 ta route-chunk'ni bir vaqtda
        #      so'raydi va brauzer ularning bir qismini `ERR_ABORTED` qiladi;
        #      kerakli chunk shu qurbonlar orasiga tushsa, ilova hech qachon
        #      render bo'lmaydi (HAR: 879 ta abort). Bu TASODIFIY — qayta
        #      urinish yordam beradi, kutish vaqtini oshirish esa YO'Q.
        # Shuning uchun `--wait-selector` berilganda uni ham qayta urinish
        # sharti sifatida ishlatamiz.
        def _rendered() -> bool:
            if "signin" in page.url:
                return False
            if not args.wait_selector:
                return True
            try:
                page.wait_for_selector(args.wait_selector,
                                       timeout=min(args.timeout, 25) * 1000)
                return True
            except Exception:
                return False

        context, page, response = _open()
        for attempt in range(2, args.retries + 1):
            if _rendered():
                break
            print(f"- attempt {attempt - 1}: {page.url[:60]} — sahifa chiqmadi, qayta")
            del console_errors[:], page_errors[:], failed[:], http_errors[:]
            context.close()
            context, page, response = _open()
        else:
            _rendered()
        page.wait_for_timeout(args.wait_ms)

        out = SHOTS_DIR / f"{args.out}.png"
        page.screenshot(path=str(out), full_page=args.full_page)
        shots.append(str(out))

        for i, sel in enumerate(args.click or [], start=1):
            try:
                page.click(sel, timeout=15000)
                print(f"+ Clicked: {sel}")
            except Exception as e:
                print(f"x Click failed on {sel}: {str(e).splitlines()[0]}")
            page.wait_for_timeout(args.wait_ms)
            shot = SHOTS_DIR / f"{args.out}-click{i}.png"
            page.screenshot(path=str(shot), full_page=args.full_page)
            shots.append(str(shot))

        final_url = page.url
        title = page.title()
        # Landing on the sign-in page means the injected session was not accepted.
        # `/seller/signin` is the cabinet's own path and was missing here, so a
        # bounced run used to report logged_in=true.
        logged_in = not any(m in final_url for m in
                            ("/login", "/auth", "/signin", "showcaptcha"))
        captcha = "showcaptcha" in final_url
        # HAR is only flushed to disk when the context closes — close it, not
        # just the browser, or the file comes out empty/truncated.
        context.close()
        browser.close()

    print(json.dumps({
        "url": url,
        "final_url": final_url,
        "http_status": response.status if response else None,
        "title": title,
        "captcha_blocked": captcha,
        "logged_in": logged_in,
        "har": args.har or None,
        "screenshots": shots,
        "console_errors": console_errors[:10],
        "page_errors": page_errors[:10],
        "failed_requests": failed[:10],
        "http_errors": http_errors[:10],
        "ok": bool(logged_in and not page_errors),
    }, indent=2, ensure_ascii=False))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="uzumcheck")
    sub = ap.add_subparsers(dest="cmd", required=True)

    lg = sub.add_parser("login", help="open a real window once to clear the captcha")
    lg.add_argument("--token-file", required=True)
    lg.add_argument("--width", type=int, default=1440)
    lg.add_argument("--height", type=int, default=900)
    lg.add_argument("--lang", default="ru", choices=["ru", "uz"])
    lg.add_argument("--wait-login", type=int, default=180,
                    help="seconds to wait for the captcha to be solved")
    lg.set_defaults(func=cmd_login)

    s = sub.add_parser("shot", help="screenshot a seller.uzum.uz path")
    s.add_argument("path", help="portal path, e.g. /product/create")
    s.add_argument("--token-file", required=True, help="JSON {'access': '<token>'} or bare token")
    s.add_argument("--out", default="uzum")
    s.add_argument("--full-page", action="store_true")
    s.add_argument("--width", type=int, default=1440)
    s.add_argument("--height", type=int, default=900)
    s.add_argument("--headed", action="store_true")
    s.add_argument("--lang", default="ru", choices=["ru", "uz"])
    s.add_argument("--wait-selector")
    s.add_argument("--wait-ms", type=int, default=2500)
    s.add_argument("--click", action="append")
    s.add_argument("--har", help="write a full HAR of the session to this path")
    s.add_argument("--shop", type=int,
                   help="active shopId; defaults to the one in the path (/seller/<id>/...)")
    s.add_argument("--retries", type=int, default=5,
                   help="attempts when the SPA bounces to /signin (fresh context each)")
    s.add_argument("--timeout", type=int, default=45)
    s.set_defaults(func=cmd_shot)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
