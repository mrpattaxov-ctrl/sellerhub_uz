"""185ms qayerga ketadi — TARMOQ (masofa) vs UZUM SERVER (o'ylash) ajratmasi.

- TCP-connect vaqti = 1 marta borib-kelish (SOF tarmoq masofasi, server o'ylashi YO'Q).
- TLS handshake = xavfsiz ulanish (yana bir-necha borib-kelish).
- To'liq so'rov (issiq) = tarmoq + Uzum'ning javob hisoblashi.

Agar TCP-connect KATTA (~150ms) → masofa aybdor → Tashkentga yaqin server juda
foyda. Agar TCP-connect KICHIK (~20ms) → 185ms'ning ko'pi Uzum o'ylashi →
yaqinroq server yordam bermaydi. READ-ONLY, faqat ulanish o'lchaydi.
"""
from __future__ import annotations

import socket
import ssl
import statistics
import time

HOST = "api-seller.uzum.uz"
PORT = 443
N = 15


def _median(xs):
    return statistics.median(xs) if xs else 0.0


def main():
    print(f"[probe] {HOST}:{PORT} — tarmoq vs server ajratmasi ({N} o'lchov)\n")

    tcp_times, tls_times = [], []
    ip = socket.gethostbyname(HOST)
    print(f"   DNS: {HOST} → {ip}\n")

    ctx = ssl.create_default_context()
    for i in range(N):
        # 1) SOF TCP-connect = 1 borib-kelish (tarmoq masofasi).
        t0 = time.monotonic()
        s = socket.create_connection((HOST, PORT), timeout=10)
        t_tcp = (time.monotonic() - t0) * 1000
        tcp_times.append(t_tcp)
        # 2) TLS handshake (TCP ustidan).
        t1 = time.monotonic()
        ss = ctx.wrap_socket(s, server_hostname=HOST)
        t_tls = (time.monotonic() - t1) * 1000
        tls_times.append(t_tls)
        ss.close()
        time.sleep(0.1)

    tcp_med = _median(tcp_times)
    tls_med = _median(tls_times)
    print(f"   TCP-connect (SOF tarmoq, 1 borib-kelish):")
    print(f"     min={min(tcp_times):.0f}ms  median={tcp_med:.0f}ms  max={max(tcp_times):.0f}ms")
    print(f"   TLS handshake (ulanish o'rnatish):")
    print(f"     min={min(tls_times):.0f}ms  median={tls_med:.0f}ms  max={max(tls_times):.0f}ms")

    print(f"\n   ── TALQIN ──")
    print(f"   • Bitta borib-kelish (RTT) ≈ TCP-connect ≈ {tcp_med:.0f}ms — bu SOF masofa.")
    full = 185
    thinking = max(0, full - tcp_med)   # to'liq so'rov − 1 RTT ≈ Uzum o'ylashi (taxminiy)
    print(f"   • Issiq to'liq so'rov ~{full}ms edi. Undan 1 RTT ({tcp_med:.0f}ms) tarmoq;")
    print(f"     qolgan ~{thinking:.0f}ms ≈ Uzum server o'ylashi + javob uzatish.")
    if tcp_med >= 80:
        print(f"   → TCP-connect KATTA ({tcp_med:.0f}ms): masofa jiddiy ulush. "
              f"Uzum'ga YAQIN server (Tashkent) sezilarli tezlashtiradi.")
    else:
        print(f"   → TCP-connect KICHIK ({tcp_med:.0f}ms): masofa kichik ulush. "
              f"185ms'ning ko'pi Uzum o'ylashi — yaqinroq server kam yordam beradi.")
    print(f"\n[probe] tugadi.")


if __name__ == "__main__":
    main()
