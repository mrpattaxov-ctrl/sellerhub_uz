"""Throwaway HAR analyzer for the competitor (seller.uzum.plus) capture.
Extracts: hosts, FBS/order endpoints, rate-limit headers, 429s, call timing.
"""
import json
import sys
from collections import Counter
from urllib.parse import urlparse

p = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\Abdulaziz\Downloads\seller.uzum.plus++++++++.har"
with open(p, encoding="utf-8") as f:
    har = json.load(f)
ents = har["log"]["entries"]
print("Jami so'rovlar:", len(ents))

hosts = Counter(urlparse(e["request"]["url"]).netloc for e in ents)
print("\n=== Hostlar ===")
for h, c in hosts.most_common():
    print(f"  {c:4d}  {h}")

# Rate-limit headers (any response that carries them)
RK = ("ratelimit-limit", "ratelimit-remaining", "ratelimit-reset",
      "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset",
      "x-rate-limit-limit", "x-rate-limit-remaining", "retry-after")
print("\n=== Rate-limit header ko'rinishlari (namuna) ===")
seen = 0
for e in ents:
    hdrs = {h["name"].lower(): h["value"] for h in e["response"].get("headers", [])}
    rl = {k: v for k, v in hdrs.items() if k in RK}
    if rl:
        seen += 1
        if seen <= 8:
            u = e["request"]["url"]
            path = urlparse(u).path
            print(f"  HTTP {e['response']['status']}  {path[:60]}  {rl}")
print(f"  (jami {seen} ta so'rovda rate-limit header bor)")

# 429s
print("\n=== 429 javoblar ===")
n429 = [e for e in ents if e["response"]["status"] == 429]
print(f"  {len(n429)} ta 429")
for e in n429[:5]:
    hdrs = {h["name"].lower(): h["value"] for h in e["response"].get("headers", [])}
    rl = {k: v for k, v in hdrs.items() if k in RK}
    print(f"    {urlparse(e['request']['url']).path[:60]}  {rl}")

# FBS/order-related endpoints
print("\n=== FBS/order/count endpoint'lar (path + method + necha marta) ===")
fbs = Counter()
for e in ents:
    path = urlparse(e["request"]["url"]).path
    if any(k in path.lower() for k in ("fbs", "order", "count", "invoice", "label")):
        fbs[(e["request"]["method"], path)] += 1
for (m, path), c in fbs.most_common(30):
    print(f"  {c:3d}  {m:5s} {path[:75]}")
