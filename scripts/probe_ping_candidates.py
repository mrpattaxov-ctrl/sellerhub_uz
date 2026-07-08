"""Xarid OLDIDAN: turli shaharlardan Uzum serveriga (Moscow) ping — qaysi VPS
joyi eng yaqin. Globalping (bepul, butun dunyo probe'lari) orqali — biz hech
qanday server sotib olmasdan, o'sha shaharlardagi mashinalar Uzum'ga ping yuboradi.

Target = Uzum api-seller.uzum.uz (130.193.53.216, Moscow/Yandex).
Solishtirish uchun bizning Tashkent RTT ~71ms.
"""
from __future__ import annotations

import json
import time
from urllib.request import Request, urlopen

TARGET = "130.193.53.216"   # api-seller.uzum.uz (Moscow, Yandex.Cloud)
API = "https://api.globalping.io/v1/measurements"

# Nomzod joylar (Contabo EU=Germaniya; Moscow=ideal; boshqalar taqqoslash uchun).
LOCATIONS = [
    {"magic": "Nuremberg"},     # Contabo asosiy EU DC
    {"magic": "Germany"},
    {"magic": "Frankfurt"},     # yirik EU tarmoq tuguni
    {"magic": "Amsterdam"},
    {"magic": "Moscow"},        # Uzum yonida — ideal
    {"magic": "Helsinki"},      # Moskvaga yaqin, past latency bilan mashhur
    {"magic": "Warsaw"},
    {"magic": "Istanbul"},
]


def _post():
    body = json.dumps({
        "type": "ping",
        "target": TARGET,
        "locations": LOCATIONS,
        "measurementOptions": {"packets": 6},
    }).encode()
    req = Request(API, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def _get(mid):
    with urlopen(f"{API}/{mid}", timeout=20) as r:
        return json.loads(r.read().decode())


def main():
    print(f"[probe] Uzum {TARGET} (Moscow) ga turli shaharlardan ping...\n")
    try:
        created = _post()
    except Exception as e:
        print(f"[probe] globalping so'rovi xato: {e!r}")
        print("       (konteynerdan chiqish bo'lmasa, globalping.io saytida qo'lda qiling)")
        return
    mid = created.get("id")
    print(f"   measurement id={mid}, {created.get('probesCount')} probe — kutilmoqda...\n")

    res = None
    for _ in range(30):
        time.sleep(2)
        res = _get(mid)
        if res.get("status") != "in-progress":
            break

    rows = []
    for r in (res.get("results") or []):
        p = r.get("probe", {})
        stats = ((r.get("result") or {}).get("stats") or {})
        avg = stats.get("avg")
        loss = stats.get("loss")
        city = p.get("city"); country = p.get("country"); net = p.get("network")
        rows.append((avg if avg is not None else 9999, city, country, net, avg, loss))

    rows.sort(key=lambda x: x[0])
    print(f"   {'shahar':<16}{'davlat':<10}{'ping(avg)':>10}{'loss':>7}   tarmoq")
    print("   " + "-" * 70)
    for _, city, country, net, avg, loss in rows:
        avg_s = f"{avg:.0f}ms" if avg is not None else "—"
        loss_s = f"{loss:.0f}%" if loss is not None else "—"
        print(f"   {str(city):<16}{str(country):<10}{avg_s:>10}{loss_s:>7}   {str(net)[:34]}")

    print(f"\n   ── Solishtirish ──")
    print(f"   Sizning hozirgi Tashkent serveringiz → Uzum: ~71ms")
    best = rows[0] if rows else None
    if best and best[4] is not None:
        print(f"   Eng yaqin nomzod: {best[1]} ~{best[4]:.0f}ms "
              f"→ Tashkentdan ~{71-best[4]:.0f}ms tez (har round-trip).")
        print(f"   Grab = 2 round-trip → ~{2*(71-best[4]):.0f}ms tejash.")
    print(f"\n[probe] eslatma: probe'lar o'sha shahardagi jamoat mashinalari — "
          f"aniq Contabo DC emas, lekin shu shahar/tarmoq uchun yaxshi taxmin.")
    print(f"[probe] tugadi.")


if __name__ == "__main__":
    main()
