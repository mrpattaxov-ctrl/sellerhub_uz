"""NAZORATLI TAJRIBA — «2 razmer → 400» ni qat'iy isbotlash.
Bir xil tana; faqat razmer xususiyatlari soni farq qiladi.
Routes override CHETLAB o'tiladi (client to'g'ridan-to'g'ri). Konteyner ichida ishlaydi."""
import io
from noviy_tavar import client
from noviy_tavar.client import NoviyTavarError

SHOP = "40571"; CAT = 12434


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


def run(name, chars, image):
    body = client.build_create_body(
        category_id=CAT, title_uz="AB test", title_ru="AB test",
        short_uz="", short_ru="", desc_uz="Test", desc_ru="Test",
        filter_values_sel=[{"filterId": 6, "filterValueId": 13942}],
        characteristics_sel=chars, images=[image], product_fields={})
    n_dc = len(body.get("definedCharacteristics") or [])
    try:
        r = client.create_product(SHOP, body)
        print("  %-38s DC=%d -> 201 OK (id %s)" % (name, n_dc, r.get("id")))
    except NoviyTavarError as e:
        import json
        try:
            code = json.loads(e.body)["errors"][0]["code"]
        except Exception:
            code = "?"
        print("  %-38s DC=%d -> HTTP %d  code=%s" % (name, n_dc, e.http_status, code))


def main():
    meta = client.category_meta(SHOP, CAT)
    chars = meta["characteristics"]
    def of(ru):
        for c in chars:
            if (c.get("characteristicTitle") or {}).get("ru") == ru:
                return c
    color, ln, wr = of("Цвет"), of("Длина браслета, см"), of("Обхват запястья, см")
    image = img(); print("img:", image["key"], "\nNAZORATLI TAJRIBA (bir xil tana, faqat razmer soni farq):")
    run("T1 rang + Длина(1)", [cs(color, 1, 0), cs(ln, 1, 1)], image)
    run("T2 rang + Обхват(1)", [cs(color, 1, 0), cs(wr, 1, 1)], image)
    run("T3 rang + Длина(1) + Обхват(1)", [cs(color, 1, 0), cs(ln, 1, 1), cs(wr, 1, 2)], image)
    run("T4 rang (razmersiz)", [cs(color, 1, 0)], image)


if __name__ == "__main__":
    main()
