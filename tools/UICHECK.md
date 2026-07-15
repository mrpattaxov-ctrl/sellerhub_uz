# uicheck — ko'rib tekshirish vositasi (screenshot + diagnostika)

Dizayn yoki backend o'zgargandan keyin «ishlayaptimi?» degan savolga **taxmin qilmasdan**
javob berish uchun. Haqiqiy Chromium'ni haqiqiy login qilingan sessiya bilan yuritadi va
foydalanuvchi ko'radigan narsani qaytaradi: HTTP status, JS konsol xatolari, uzilgan
so'rovlar va PNG surat.

## O'rnatish (bir marta)

```bash
pip install playwright
python -m playwright install chromium
```

## 1. Login (Telegram tasdig'i bilan)

```bash
python tools/uicheck.py login --phone +998998110950
```

Saytning **o'z** login formasi brauzerda to'ldiriladi → raqam egasiga Telegram'da
«✅ Подтвердить вход / ❌ Отклонить» keladi → egasi tasdiqlaydi. Ya'ni inson ruxsatisiz
sessiya ochilmaydi.

Sessiya `.uicheck/state.json` ichida saqlanadi (gitignore'da) va **~30 kun** yashaydi
(`PERMANENT_SESSION_LIFETIME`), demak login kamdan-kam kerak bo'ladi.

Tirikligini tekshirish:

```bash
python tools/uicheck.py whoami
```

## 2. Sahifani suratga olish va tekshirish

```bash
python tools/uicheck.py shot /fbs
python tools/uicheck.py shot /fbs/invoices --full-page
python tools/uicheck.py shot /fbs --click "[data-status='PACKING']" --wait-ms 2500
python tools/uicheck.py shot /economics --width 390 --height 844   # mobil
python tools/uicheck.py shot /fbs --dark                            # tungi rejim
```

Suratlar `.uicheck/shots/` ichiga tushadi. Har bir `--click` dan keyin qo'shimcha surat
olinadi (`<nom>-click1.png`), shuning uchun tugma haqiqatan ishlaganini ko'rish mumkin.

Chiqish — JSON hisobot; `"ok": false` bo'lsa quyidagilardan biri bor:
`console_errors`, `page_errors`, `failed_requests`, `http_errors`, yoki HTTP ≥ 400.
Sessiya o'lgan bo'lsa `/login`ga uloqtiradi va 2-kod bilan chiqadi.

## Tuzoqlar

- **Git Bash yo'lni buzadi**: `shot /fbs` → `C:/Program Files/Git/fbs`. PowerShell'dan
  chaqiring, yoki boshidagi `/` ni tashlang: `shot fbs`.
- **`--full-page`** uzun jadvallarda 20 000+ px surat beradi — kichraytirilganda o'qib
  bo'lmaydi. Dizaynni ko'rish uchun oddiy viewport surati yaxshiroq.
- **Lokalda 502**: `/postavki/api/grab-plan/list` — avto-slot mikroservisi lokalda
  ishlamayapti (`AUTOSLOT_URL` yoqilgan, konteyner yo'q). Kod bug'i emas.
