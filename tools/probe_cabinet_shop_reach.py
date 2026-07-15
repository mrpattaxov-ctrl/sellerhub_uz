"""READ-ONLY: admin kabinet (brauzer) tokeni QAYSI do'konlarga kira oladi?

Savol: FBS «qabul qilish»ni brauzer-tokenga o'tkazsak, u HAMMA user'ning
buyurtmasini tasdiqlay oladimi? Bu faqat do'konlar bitta Uzum akkauntiga
tegishli bo'lsa ishlaydi. Begona do'konda Uzum 403 beradi.

Har do'kon uchun ENG YENGIL read-only portal GET yuboramiz:
    GET /api/seller/shop/{id}/marketing/sales?saleType=ALL&page=0&size=1
200 → token bu do'konni ko'radi;  403 → begona akkaunt.

SAFETY: GET only, hech narsa o'zgarmaydi. Token chop etilmaydi.
"""
from __future__ import annotations

import requests
from sqlalchemy import select

from extensions import SessionLocal
from models import Shop, User
from core.auth_helpers import _get_admin_token

BASE = "https://api-seller.uzum.uz/api/seller/shop"

tok = (_get_admin_token() or "").strip()
if not tok:
    raise SystemExit("Admin kabinet tokeni yo'q.")

hdrs = {
    "Authorization": tok if tok.startswith("Bearer ") else f"Bearer {tok}",
    "Accept": "application/json",
    "Accept-Language": "ru-RU",
    "Origin": "https://seller.uzum.uz",
    "Referer": "https://seller.uzum.uz/",
}

with SessionLocal() as db:
    rows = db.execute(
        select(Shop.uzum_id, Shop.name, Shop.owner_id, User.is_admin)
        .join(User, User.id == Shop.owner_id)
        .order_by(Shop.owner_id, Shop.uzum_id)
    ).all()

print(f"{'shop':<8} {'egasi':<7} {'nom':<22} {'admin-token kirishi'}")
print("-" * 60)
for uzum_id, name, owner_id, is_admin in rows:
    if not uzum_id:
        continue
    url = f"{BASE}/{uzum_id}/marketing/sales?saleType=ALL&page=0&size=1"
    try:
        r = requests.get(url, headers=hdrs, timeout=15)
        verdict = "✅ 200 KIRADI" if r.status_code == 200 else f"❌ {r.status_code} {r.text[:60]}"
    except Exception as e:
        verdict = f"XATO {e!r}"
    owner = f"user{owner_id}" + ("*" if is_admin else "")
    print(f"{uzum_id:<8} {owner:<7} {(name or '-')[:20]:<22} {verdict}")

print("\n* = admin")
print("Hammasi 200 -> confirm'ni brauzer-tokenga o'tkazish TEXNIK MUMKIN.")
print("Biror 403  -> u do'kon boshqa Uzum akkauntida; brauzer-token yo'li YOPIQ.")
