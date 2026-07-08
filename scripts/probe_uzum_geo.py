"""Uzum serveri QAYERDA + BIZning server qayerda — masofani aniqlash.

- Uzum IP (api-seller.uzum.uz) reverse-DNS + geo (ip-api.com).
- Bizning egress IP + geo — shundan Uzum'gacha masofa/RTT ma'noli bo'ladi.

Faqat OMMAVIY IP joylashuvini so'raydi (ip-api.com) — maxfiy ma'lumot yo'q.
"""
from __future__ import annotations

import json
import socket
import time
from urllib.request import urlopen

HOST = "api-seller.uzum.uz"


def _geo(ip_or_empty: str) -> dict:
    # ip-api.com — bepul, kalit shart emas; bo'sh IP = so'rovchi (biz).
    url = f"http://ip-api.com/json/{ip_or_empty}?fields=status,country,regionName,city,lat,lon,isp,org,as,query,reverse"
    try:
        with urlopen(url, timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"status": "fail", "error": repr(e)}


def _rdns(ip: str) -> str:
    try:
        return socket.gethostbyaddr(ip)[0]
    except Exception:
        return "(reverse-DNS yo'q)"


def _show(label: str, g: dict):
    print(f"   {label}")
    if g.get("status") != "success":
        print(f"     xato: {g.get('error') or g}")
        return
    print(f"     IP      : {g.get('query')}")
    print(f"     joylashuv: {g.get('city')}, {g.get('regionName')}, {g.get('country')}")
    print(f"     koordinat: {g.get('lat')}, {g.get('lon')}")
    print(f"     ISP/org  : {g.get('isp')} / {g.get('org')}")
    print(f"     AS       : {g.get('as')}")


def _haversine(a, b) -> float:
    from math import radians, sin, cos, asin, sqrt
    lat1, lon1 = a; lat2, lon2 = b
    dlat = radians(lat2 - lat1); dlon = radians(lon2 - lon1)
    h = sin(dlat/2)**2 + cos(radians(lat1))*cos(radians(lat2))*sin(dlon/2)**2
    return 2 * 6371 * asin(sqrt(h))


def main():
    uzum_ip = socket.gethostbyname(HOST)
    print(f"[probe] {HOST} → {uzum_ip}   rDNS: {_rdns(uzum_ip)}\n")

    uz = _geo(uzum_ip)
    time.sleep(1.5)   # ip-api bepul limit (~45/daq) — ehtiyot
    me = _geo("")

    print("── UZUM serveri ──")
    _show("(api-seller.uzum.uz)", uz)
    print("\n── BIZning server (egress) ──")
    _show("(shu konteyner chiqish IP'si)", me)

    if uz.get("status") == "success" and me.get("status") == "success":
        try:
            km = _haversine((me["lat"], me["lon"]), (uz["lat"], uz["lon"]))
            print(f"\n   ── MASOFA ──")
            print(f"   Biz → Uzum: ~{km:.0f} km (to'g'ri chiziq)")
            print(f"   Yorug'lik shu masofani borib-kelishi: ~{2*km/200:.0f}ms (fiber ~200km/ms)")
            print(f"   (o'lchagan RTT ~71ms — bunga yo'l egriligi + tarmoq hoplar qo'shiladi)")
            if me.get("country") != uz.get("country"):
                print(f"   → Turli davlat ({me.get('country')} vs {uz.get('country')}). "
                      f"Uzum davlatiga/regioniga ko'chsak travel qismini kamaytiramiz.")
            else:
                print(f"   → Bir davlat — travel allaqachon kichik; ko'p yutuq yo'q.")
        except Exception as e:
            print(f"   masofa hisoblanmadi: {e!r}")
    print(f"\n[probe] tugadi.")


if __name__ == "__main__":
    main()
