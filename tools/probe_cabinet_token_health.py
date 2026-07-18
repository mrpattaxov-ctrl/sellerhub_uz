"""READ-ONLY: kabinet (brauzer) tokenlarining holati — kimda bor, qachon o'ladi.

Savol (Abdulaziz 2026-07-14): FBS «qabul qilish» (confirm) ni OpenAPI o'rniga
brauzer-tokenga o'tkazsa bo'ladimi? «Акции» moduli allaqachon har user'ning
O'Z tokenini ishlatadi (_get_fresh_api_key → User.api_key).

Hal qiluvchi xavf: token muddati. _uzum_auto_login() FAQAT admin tokenini
yangilaydi (User.is_admin == True). Oddiy user tokeni o'lganda — hech kim
yangilamaydi. Confirm esa muddatli (acceptUntil) → jarima xavfi.

Bu skript: har user uchun api_key BOR-YO'Qligini, JWT `exp` bo'yicha necha
soat qolganini va telefon+parol saqlanganmi (avto-login imkoni) — ko'rsatadi.
Token qiymati HECH QACHON chop etilmaydi.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone

from sqlalchemy import select

from extensions import SessionLocal
from models import User


def jwt_exp(tok: str):
    try:
        payload = tok.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data.get("exp")
    except Exception:
        return None


now = datetime.now(timezone.utc)
with SessionLocal() as db:
    users = db.execute(select(User).order_by(User.id)).scalars().all()
    print(f"{'id':<4} {'admin':<6} {'openapi':<8} {'api_key':<8} {'exp (soat)':<14} {'tel+parol':<10}")
    print("-" * 62)
    for u in users:
        tok = (u.api_key or "").strip()
        oa = "bor" if (u.uzum_openapi_token or "").strip() else "-"
        ak = "bor" if tok else "-"
        exp = jwt_exp(tok) if tok else None
        if exp:
            left = (datetime.fromtimestamp(exp, tz=timezone.utc) - now).total_seconds() / 3600
            exp_s = f"{left:+.1f} h" + (" O'LGAN" if left < 0 else "")
        else:
            exp_s = "-" if not tok else "JWT emas"
        creds = "bor" if ((u.uzum_phone or "").strip() and (u.uzum_password_plain or "").strip()) else "-"
        print(f"{u.id:<4} {('HA' if u.is_admin else '-'):<6} {oa:<8} {ak:<8} {exp_s:<14} {creds:<10}")
