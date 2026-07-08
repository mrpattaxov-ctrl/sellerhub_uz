"""ACTUAL Contabo-tarmog'idan Uzum'ga ping — server sotib olmasdan.

Globalping probe'ni AYNAN Contabo tarmog'ida (AS51167) tanlaydi → chinakam
Contabo→Uzum ping. Solishtirish uchun Moscow tarmoqlaridan ham so'raymiz.
Contabo probe topilmasa — buni aytadi va muqobil yo'llarni ko'rsatadi.
"""
from __future__ import annotations

import json
import time
from urllib.request import Request, urlopen

TARGET = "130.193.53.216"   # api-seller.uzum.uz (Moscow, Yandex.Cloud)
API = "https://api.globalping.io/v1/measurements"

LOCATIONS = [
    {"magic": "contabo", "limit": 3},      # AYNAN Contabo tarmog'i
    {"magic": "AS51167", "limit": 2},      # Contabo ASN (ehtiyot nusxa)
    {"magic": "moscow", "limit": 2},       # taqqoslash — Uzum yonida
    {"magic": "germany", "limit": 2},      # umumiy Germaniya
]


def _post():
    body = json.dumps({
        "type": "ping", "target": TARGET,
        "locations": LOCATIONS,
        "measurementOptions": {"packets": 8},
    }).encode()
    req = Request(API, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def _get(mid):
    with urlopen(f"{API}/{mid}", timeout=20) as r:
        return json.loads(r.read().decode())


def main():
    print(f"[probe] Contabo tarmog'idan (AS51167) Uzum {TARGET} ga ping...\n")
    try:
        created = _post()
    except Exception as e:
        print(f"[probe] xato: {e!r}"); return
    mid = created.get("id")
    print(f"   id={mid}, {created.get('probesCount')} probe topildi — kutilmoqda...\n")
    res = None
    for _ in range(30):
        time.sleep(2)
        res = _get(mid)
        if res.get("status") != "in-progress":
            break

    print(f"   {'tarmoq (network)':<28}{'shahar':<14}{'davlat':<8}{'ping':>8}{'loss':>7}")
    print("   " + "-" * 70)
    any_contabo = False
    for r in (res.get("results") or []):
        p = r.get("probe", {})
        stats = ((r.get("result") or {}).get("stats") or {})
        avg = stats.get("avg"); loss = stats.get("loss")
        net = str(p.get("network") or "")
        if "contabo" in net.lower():
            any_contabo = True
        avg_s = f"{avg:.0f}ms" if avg is not None else "—"
        loss_s = f"{loss:.0f}%" if loss is not None else "—"
        print(f"   {net[:27]:<28}{str(p.get('city'))[:13]:<14}{str(p.get('country')):<8}{avg_s:>8}{loss_s:>7}")

    print()
    if not any_contabo:
        print("   ⚠️ Contabo tarmog'ida jonli probe topilmadi (globalpingда yo'q).")
        print("      → globalping.io saytida 'From: Contabo' ni sinab ko'ring, yoki")
        print("        soatbay VPS (Hetzner/Vultr) olib o'zingiz ping qiling (pastga qara).")
    print(f"\n   Taqqoslash: Tashkent (hozirgi) ~71ms.")
    print(f"[probe] tugadi.")


if __name__ == "__main__":
    main()
