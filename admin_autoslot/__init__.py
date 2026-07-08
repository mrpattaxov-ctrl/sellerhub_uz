"""Admin «Авто-слот» paneli — IZOLYATSIYALANGAN modul.

Butun kod shu papkada; boshqa blueprint/modullarga tegmaydi. Faqat
SellerHub operatori (is_admin) global avto-slot navbatini ko'radi va
navbat prioritetini boshqaradi.
"""
from admin_autoslot.routes import admin_autoslot_bp

__all__ = ["admin_autoslot_bp"]
