"""Time-slot 30-DAQIQALIK STRESS-TEST — 100 parallel/sekund, 403 + timeout hisobi.

Abdulaziz so'rovi (2026-07-07): 100 so'rov/sekund BARAVAR (parallel), 30 daqiqa
davomida — nechta 403 va timeout chiqishini KO'RISH uchun. Har DAQIQA jonli
xulosa (200/403/429/timeout/other + latency), 403 javob tanasi ushlanadi, oxirida
to'liq summary.

⚠️ JONLI PROD token/egress'da ~180 000 so'rov. Agar sustained-403 blok chiqsa,
BUTUN prod Uzum trafigiga tegishi mumkin. Abdulaziz to'liq 30 daqiqani (erta
to'xtatmasdan) tanladi va riskni qabul qildi. READ-ONLY (band qilmaydi).

    docker compose cp scripts/probe_timeslot_stress30.py app:/app/scripts/probe_timeslot_stress30.py
    docker compose exec app python scripts/probe_timeslot_stress30.py --minutes 30 --per-sec 100
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from requests.adapters import HTTPAdapter

_BASE = "https://api-seller.uzum.uz/api/seller/shop"
_SLOT_WINDOW_MS = 31_532_400_000
_TIMEFROM_LEAD_MS = 5 * 60 * 1000

_INTERESTING_HEADERS = (
    "www-authenticate", "retry-after", "date", "x-request-id", "x-trace-id",
    "cf-ray", "server", "content-type", "x-ratelimit-remaining",
    "x-ratelimit-burst-capacity", "x-ratelimit-replenish-rate",
)


def _load_context() -> tuple[str, str, dict, str]:
    from app import app as flask_app
    from models import Shop
    from extensions import SessionLocal
    from sqlalchemy import select
    from core.auth_helpers import _get_admin_token
    from postavki.slot_watch import _get_sku_context
    with flask_app.app_context():
        tok = (_get_admin_token() or "").strip()
        with SessionLocal() as db:
            shop_id = db.execute(
                select(Shop.uzum_id).where(Shop.uzum_id.isnot(None)).limit(1)
            ).scalar_one_or_none()
        shop_id = str(shop_id) if shop_id else ""
        base = pool = None
        if shop_id:
            base, _dim, pool = _get_sku_context(shop_id)
    if not base:
        return tok, shop_id, {}, ""
    return tok, shop_id, {**base, "quantityToStock": 150}, (pool or "FULLFILMENT")


def _make_session(pool_size: int) -> requests.Session:
    s = requests.Session()
    ad = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size, max_retries=0)
    s.mount("https://", ad); s.mount("http://", ad)
    return s


def _classify(exc: Exception) -> str:
    s = repr(exc)
    if "ConnectTimeout" in s:
        return "connect-timeout"
    if "ReadTimeout" in s or "Read timed out" in s:
        return "read-timeout"
    if "reset by peer" in s or "RemoteDisconnected" in s or "ConnectionResetError" in s:
        return "conn-reset"
    if "Connection refused" in s:
        return "conn-refused"
    if "Max retries" in s:
        return "max-retries"
    return "other-error"


class Stats:
    """Thread-safe yig'gich — global + per-daqiqa."""
    def __init__(self, minutes: int):
        self.lock = threading.Lock()
        self.g = {"200": 0, "403": 0, "429": 0, "timeout": 0, "other": 0, "dropped": 0}
        self.per_min = [dict(m200=0, m403=0, m429=0, mto=0, moth=0)
                        for _ in range(minutes + 1)]
        self.ok_latency: list[float] = []       # 200 latency (percentile uchun)
        self.first_403_sec: int | None = None
        self.first_timeout_sec: int | None = None
        self.captured: list[dict] = []          # namuna non-200 javoblar
        self.status_zoo: dict[int, int] = {}     # kutilmagan status kodlar

    def record(self, kind: str, elapsed: float, minute: int, sec: int,
               status: int | None, body: str | None, headers: dict | None):
        with self.lock:
            b = self.per_min[min(minute, len(self.per_min) - 1)]
            if kind == "200":
                self.g["200"] += 1; b["m200"] += 1
                self.ok_latency.append(elapsed)
            elif kind == "403":
                self.g["403"] += 1; b["m403"] += 1
                if self.first_403_sec is None:
                    self.first_403_sec = sec
                if len(self.captured) < 20 and body is not None:
                    self.captured.append({"sec": sec, "status": status,
                                          "body": body[:300], "headers": headers or {}})
            elif kind == "429":
                self.g["429"] += 1; b["m429"] += 1
            elif "timeout" in kind:
                self.g["timeout"] += 1; b["mto"] += 1
                if self.first_timeout_sec is None:
                    self.first_timeout_sec = sec
            else:
                self.g["other"] += 1; b["moth"] += 1
                if status is not None:
                    self.status_zoo[status] = self.status_zoo.get(status, 0) + 1
                elif len(self.captured) < 20 and body is not None:
                    self.captured.append({"sec": sec, "status": status,
                                          "body": body[:300], "headers": headers or {},
                                          "kind": kind})

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.g)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, default=30)
    ap.add_argument("--per-sec", type=int, default=100)
    ap.add_argument("--connect-timeout", type=float, default=5.0)
    ap.add_argument("--read-timeout", type=float, default=8.0)
    ap.add_argument("--max-inflight", type=int, default=1200)
    ap.add_argument("--workers", type=int, default=400)
    args = ap.parse_args()

    tok, shop_id, line, pool = _load_context()
    if not (tok and shop_id and line):
        print("[probe] kontekst topilmadi (token/shop/sku)"); return

    url = f"{_BASE}/{shop_id}/v2/invoice/time-slot"
    # ⚠️ timeFrom KELAJAKDA bo'lishi shart — HAR so'rovда qayta hisoblanadi.
    # Muzlatib qo'ysang 5 daqiqадан keyin o'tmishga tushib 400 validation-failed
    # beradi (Uzum bloki EMAS — probe artefakti).
    def _fresh_body():
        now = int(time.time() * 1000)
        return {"skuList": [line], "poolSource": pool,
                "timeFrom": now + _TIMEFROM_LEAD_MS, "timeTo": now + _SLOT_WINDOW_MS}
    h = {"Authorization": tok if tok.startswith("Bearer ") else f"Bearer {tok}",
         "Accept": "application/json", "Content-Type": "application/json",
         "Origin": "https://seller.uzum.uz", "Referer": "https://seller.uzum.uz/"}

    total_secs = args.minutes * 60
    st = Stats(args.minutes)
    sess = _make_session(pool_size=args.per_sec + 40)
    inflight = 0
    infl_lock = threading.Lock()

    def _task(sec: int):
        nonlocal inflight
        minute = sec // 60
        t0 = time.monotonic()
        status = bodytext = None; hdrs = None
        try:
            r = sess.post(url, headers=h, json=_fresh_body(),
                          timeout=(args.connect_timeout, args.read_timeout))
            el = time.monotonic() - t0
            status = r.status_code
            if status == 200:
                kind = "200"
            elif status == 403:
                kind = "403"; bodytext = r.text or ""
                hdrs = {k: v for k, v in r.headers.items() if k.lower() in _INTERESTING_HEADERS}
            elif status == 429:
                kind = "429"
                hdrs = {k: v for k, v in r.headers.items() if k.lower() in _INTERESTING_HEADERS}
            else:
                kind = f"http-{status}"; bodytext = r.text or ""
                hdrs = {k: v for k, v in r.headers.items() if k.lower() in _INTERESTING_HEADERS}
        except Exception as e:
            el = time.monotonic() - t0
            kind = _classify(e); bodytext = repr(e)[:200]
        st.record(kind, el, minute, sec, status, bodytext, hdrs)
        with infl_lock:
            inflight -= 1

    print(f"[probe] 30-DAQIQALIK STRESS: {args.per_sec}/sek × {args.minutes} daq "
          f"(~{args.per_sec*total_secs} so'rov), pooled Session")
    print(f"[probe] timeout connect={args.connect_timeout}s read={args.read_timeout}s, "
          f"max-inflight={args.max_inflight}, workers={args.workers}")
    print(f"[probe] shop={shop_id} sku={line.get('skuId')} pool={pool} token={tok[:10]}..")
    print(f"[probe] endpoint=POST .../v2/invoice/time-slot (read-only)\n")
    print(f"   {'daq':>3} | {'200':>6} {'403':>5} {'429':>4} {'t/out':>5} {'oth':>4} {'drop':>4}"
          f" | {'p50':>5} {'p95':>5} {'jami':>7}")
    print("   " + "-" * 74)
    sys.stdout.flush()

    t_start = time.monotonic()
    last_min_printed = -1
    prev_cum = {"200": 0, "403": 0, "429": 0, "timeout": 0, "other": 0, "dropped": 0}
    ok_lat_marker = 0

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for sec in range(total_secs):
            target = t_start + sec
            dt = target - time.monotonic()
            if dt > 0:
                time.sleep(dt)
            for _ in range(args.per_sec):
                with infl_lock:
                    if inflight >= args.max_inflight:
                        st.g["dropped"] += 1  # backpressure — 100/s ushlab turolmadi
                        continue
                    inflight += 1
                ex.submit(_task, sec)

            # Har daqiqa boshida (yangi daqiqaga o'tganda) oldingi daqiqa xulosasi.
            cur_min = sec // 60
            if cur_min != last_min_printed and sec > 0:
                last_min_printed = cur_min
                cum = st.snapshot()
                d = {k: cum[k] - prev_cum[k] for k in cum}
                prev_cum = cum
                with st.lock:
                    lat = st.ok_latency[ok_lat_marker:]
                    ok_lat_marker = len(st.ok_latency)
                p50 = (statistics.median(lat) if lat else 0.0)
                p95 = (sorted(lat)[int(len(lat) * 0.95)] if lat else 0.0)
                total_done = sum(cum[k] for k in ("200", "403", "429", "timeout", "other"))
                print(f"   {cur_min:>3} | {d['200']:>6} {d['403']:>5} {d['429']:>4} "
                      f"{d['timeout']:>5} {d['other']:>4} {d['dropped']:>4}"
                      f" | {p50:>5.2f} {p95:>5.2f} {total_done:>7}")
                sys.stdout.flush()

        print("\n[probe] barcha yuborildi — qolgan javoblar yig'ilmoqda...")
        sys.stdout.flush()
        # Qolgan in-flight'lar tugashini kutamiz (max read-timeout + zaxira).
        deadline = time.monotonic() + args.read_timeout + 10
        while time.monotonic() < deadline:
            with infl_lock:
                if inflight <= 0:
                    break
            time.sleep(0.5)

    total_span = time.monotonic() - t_start
    cum = st.snapshot()
    total_req = sum(cum[k] for k in ("200", "403", "429", "timeout", "other")) + cum["dropped"]
    done = total_req - cum["dropped"]

    print(f"\n{'='*74}")
    print(f"  YAKUNIY SUMMARY — {args.minutes} daqiqa, {total_span/60:.1f} daq real")
    print(f"{'='*74}")
    print(f"  Yuborilgan (target)   : {args.per_sec*total_secs}")
    print(f"  Bajarilgan (javob keldi): {done}")
    print(f"  Drop (backpressure)   : {cum['dropped']}  (100/s ushlab turolmagan payt)")
    print(f"  Haqiqiy temp          : {done/total_span:.0f} javob/sek")
    print()
    print(f"  ✅ 200 OK       : {cum['200']:>7}  ({100*cum['200']/done:.2f}%)" if done else "")
    print(f"  ⛔ 403          : {cum['403']:>7}  ({100*cum['403']/done:.2f}%)" if done else "")
    print(f"  🔵 429          : {cum['429']:>7}")
    print(f"  ⏱  TIMEOUT      : {cum['timeout']:>7}  ({100*cum['timeout']/done:.2f}%)" if done else "")
    print(f"  ❓ boshqa xato  : {cum['other']:>7}")
    if st.status_zoo:
        print(f"     boshqa status kodlar: {dict(st.status_zoo)}")
    print()
    if st.first_403_sec is not None:
        print(f"  Birinchi 403: {st.first_403_sec}s da (daqiqa {st.first_403_sec//60})")
    else:
        print(f"  Birinchi 403: YO'Q — 30 daqiqa davomida bironta 403 chiqmadi")
    if st.first_timeout_sec is not None:
        print(f"  Birinchi timeout: {st.first_timeout_sec}s da (daqiqa {st.first_timeout_sec//60})")
    else:
        print(f"  Birinchi timeout: YO'Q")

    if st.ok_latency:
        xs = sorted(st.ok_latency)
        def _p(p): return xs[min(len(xs) - 1, int(len(xs) * p))]
        print(f"\n  200 latency: p50={_p(.5):.2f}s p90={_p(.9):.2f}s p95={_p(.95):.2f}s "
              f"p99={_p(.99):.2f}s max={xs[-1]:.2f}s")

    # Per-daqiqa 403/timeout rampasi (faqat nolga teng bo'lmaganlar qiziq).
    ramp = [(i, m["m403"], m["mto"]) for i, m in enumerate(st.per_min)
            if m["m403"] or m["mto"]]
    if ramp:
        print(f"\n  403/timeout bo'lgan daqiqalar (daq: 403 / timeout):")
        for i, e403, eto in ramp:
            print(f"     daq {i:>2}: 403×{e403}  timeout×{eto}")

    if st.captured:
        print(f"\n  ── Ushlangan non-200 javoblar namunasi ({len(st.captured)}) ──")
        for c in st.captured[:10]:
            print(f"   • sek {c['sec']} HTTP {c.get('status')}: headers={c['headers']}")
            print(f"     body: {c['body']!r}")
    else:
        print(f"\n  (bironta non-200 javob ushlanmadi)")

    print(f"\n[probe] tugadi — read-only, hech narsa band qilinmadi.")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
