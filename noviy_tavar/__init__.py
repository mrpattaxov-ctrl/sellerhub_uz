# «Новый товар» — Uzum'da yangi mahsulot kartasi yaratish moduli.
# Butun funksiya shu paket ichida izolyatsiyalangan (postavki/ naqshi):
#   client.py  — Uzum ichki portal API o'rami (browser-token auth)
#   routes.py  — Flask blueprint (sahifa + proxy JSON API'lar)
#   templates/ — wizard sahifasi
# Tizimning boshqa qismlariga tegmaydi — faqat app.py'da bir marta
# blueprint ro'yxatdan o'tadi va groups.html'dagi tugma shu sahifaga olib
# keladi.
#
# Manba: tavarsozdat.har (2026-07-11) — portal «Создать карточку» oqimining
# to'liq yozuvi. createProduct → 201 real muvaffaqiyat bilan tasdiqlangan.
# DIQQAT: createProduct QORALAMA yaratadi (moderatsiyaga o'zi ketmaydi);
# SKU/narx + moderatsiyaga yuborish bosqichi alohida HAR kutmoqda.
from noviy_tavar.routes import noviy_tavar_bp  # noqa: F401
