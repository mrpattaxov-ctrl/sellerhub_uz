"""READ-ONLY nazorat sinovi: /v2/fbs/orders/count — ko'p status bitta so'rovda?

probe_fbs_count_multistatus.py hamma variantda payload=0 (INT) qaytardi, lekin
0 chalg'itishi mumkin (bu do'konlarda CREATED/PACKING haqiqatan 0). Shu sabab
NOL BO'LMAGAN statuslar (COMPLETED/CANCELED) bilan qayta so'raymiz: agar ko'p
status yuborilganda son QO'SHILSA — parametr o'qilyapti; o'zgarmasa — e'tiborga
olinmayapti.
"""
from __future__ import annotations

from sqlalchemy import func, select

from extensions import SessionLocal
from models import Shop, User
from core.uzum_openapi import OPENAPI_BASE, _fbs_orders_request_with_auth

with SessionLocal() as db:
    u = db.execute(
        select(User)
        .where(func.length(func.coalesce(User.uzum_openapi_token, "")) > 0)
        .order_by(User.id)
    ).scalars().first()
    shops = [str(s) for s in db.execute(
        select(Shop.uzum_id).where(Shop.owner_id == u.id)
    ).scalars().all() if s]
    tok = (u.uzum_openapi_token or "").strip()

q = "&".join(f"shopIds={s}" for s in shops)
base = f"{OPENAPI_BASE}/v2/fbs/orders/count?{q}"

cases = [
    ("COMPLETED yakka",                base + "&status=COMPLETED"),
    ("CANCELED  yakka",                base + "&status=CANCELED"),
    ("COMPLETED&CANCELED (takroriy)",  base + "&status=COMPLETED&status=CANCELED"),
    ("statuses=COMPLETED,CANCELED",    base + "&statuses=COMPLETED,CANCELED"),
    ("status YO'Q (hammasi?)",         base),
]

for label, url in cases:
    parsed, http, text, _ = _fbs_orders_request_with_auth(
        url, tok, accept_language=None, debug_label="ctl", fail_fast=True,
    )
    pl = parsed.get("payload") if isinstance(parsed, dict) else None
    print(f"{label:<32} HTTP {http}  payload={pl!r}  turi={type(pl).__name__}")
