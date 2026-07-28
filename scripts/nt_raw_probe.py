"""Noyob strukturalar auditi — o'zimdan hech narsa qo'shmasdan, jonli meta bilan.
A) 3-razmer defolt (Зеркала 12811), B) razmer-emas o'lcham (14655), C) 0-xususiyat (12498).
Konteyner ichida, routes override CHETLAB."""
import io, json
from noviy_tavar import client
from noviy_tavar.client import NoviyTavarError

SHOP = "40571"


def img():
    from PIL import Image
    b = io.BytesIO(); Image.new("RGB", (1080, 1440), (240, 240, 240)).save(b, "JPEG", quality=85)
    up = client.upload_image(b.getvalue(), "p.jpg", "image/jpeg")
    p = up.get("payload") or {}
    return {"key": p.get("key"), "url": p.get("originalUrl")}


def cs(ch, n, o):
    return {"characteristicId": ch.get("characteristicId"),
            "characteristicTitle": ch.get("characteristicTitle") or {},
            "orderingNumber": o, "requiredType": ch.get("requiredType"),
            "flowA": bool(ch.get("flowA", False)),
            "values": (ch.get("characteristicValues") or [])[:n]}


def req_fv(meta):
    for f in (meta.get("filters") or []):
        if f.get("required"):
            return {"filterId": f["id"], "filterValueId": (f.get("emptyValue") or {}).get("id")}
    return None


def create(cid, chars, image, fv):
    body = client.build_create_body(
        category_id=cid, title_uz="AUD", title_ru="AUD", short_uz="", short_ru="",
        desc_uz="Test", desc_ru="Test",
        filter_values_sel=[fv] if fv else [], characteristics_sel=chars,
        images=[image], product_fields={})
    try:
        r = client.create_product(SHOP, body)
        # SKU jadvalini ham o'qiymiz (nechta SKU qatori chiqdi)
        pid = r.get("id")
        try:
            dr = client.product_description_response(SHOP, pid)
            nsku = len(dr.get("skuList") or [])
            dc = [(c.get("characteristicTitle") or {}).get("ru") for c in (dr.get("definedCharacteristicList") or [])]
        except Exception:
            nsku, dc = "?", "?"
        return "201 id=%s | SKU=%s | keptDC=%s" % (pid, nsku, dc)
    except NoviyTavarError as e:
        try:
            code = json.loads(e.body)["errors"][0]["code"]
        except Exception:
            code = e.body[:70]
        return "HTTP %d code=%s" % (e.http_status, code)


def meta(cid):
    return client.category_meta(SHOP, cid)


def by(chs, ru):
    for c in chs:
        if (c.get("characteristicTitle") or {}).get("ru") == ru:
            return c


def main():
    image = img(); print("img:", image["key"], "\n")

    # A) Зеркала 12811 — 3 generic razmer defolt
    m = meta(12811); chs = m["characteristics"]; fv = req_fv(m)
    color = by(chs, "Цвет"); s1 = by(chs, "Размер колец"); s2 = by(chs, "Размер ремня")
    print("A) 12811 Зеркала (3 generic razmer):")
    print("   rang+1razmer :", create(12811, [cs(color, 1, 0), cs(s1, 1, 1)], image, fv))
    print("   rang+2razmer :", create(12811, [cs(color, 1, 0), cs(s1, 1, 1), cs(s2, 1, 2)], image, fv))

    # B) 14655 — razmer-EMAS o'lcham (Тип электротранспорта) + rang, ikkovi NOT_REQUIRED
    m = meta(14655); chs = m["characteristics"]; fv = req_fv(m)
    color = by(chs, "Цвет"); typ = by(chs, "Тип электротранспорта")
    print("\nB) 14655 Электротранспорт (razmer-emas o'lcham):")
    print("   rang+Тип     :", create(14655, [cs(color, 1, 0), cs(typ, 1, 1)], image, fv))
    print("   faqat Тип    :", create(14655, [cs(typ, 1, 0)], image, fv))

    # C) 12498 Хлебопечки — 0 xususiyat
    m = meta(12498); chs = m["characteristics"]; fv = req_fv(m)
    print("\nC) 12498 Хлебопечки (0 xususiyat), filters:", [(f.get("id"), f.get("required")) for f in (m.get("filters") or [])])
    print("   xususiyatsiz :", create(12498, [], image, fv))


if __name__ == "__main__":
    main()
