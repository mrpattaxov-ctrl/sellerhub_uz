"""«Новый товар» — QORALAMA yaratish tanasi (mocked, jonli chaqiruvsiz).

Nega mocked: `createProduct` HAQIQIY qoralama yaratadi. Foydalanuvchi qoidasi —
action endpoint'larni prodda sinashdan oldin mocked test yozish. Bu yerda
Uzum'ga BITTA ham so'rov ketmaydi: faqat `build_create_body()` chiqishi
tekshiriladi.

Tana shakli manbai: HAR'dagi real 201-muvaffaqiyat payload'i (client.py).
comment turlari esa portalning O'Z bandlidan olingan — tarmoq HAR'ida bu
dalil YO'Q edi (hech bir createProduct tanasi `comments` ni to'ldirmagan).
"""
from __future__ import annotations

import pytest

from noviy_tavar.client import build_create_body


def _body(**kw):
    base = dict(
        category_id=14614,
        title_uz="Aqlli soat", title_ru="Умные часы",
        short_uz="qisqa", short_ru="кратко",
        desc_uz="<p>tavsif</p>", desc_ru="<p>описание</p>",
        filter_values_sel=[],
        characteristics_sel=[],
        images=[],
        product_fields={},
    )
    base.update(kw)
    return build_create_body(**base)


# ── Ixtiyoriy bo'limlar -> comments[] ────────────────────────────────


def test_comment_types_use_api_keys_not_ui_labels():
    """⚠️ «Размерная сетка» UI yorlig'i, API kaliti «Размеры» (bandl dalili).

    Yorliqni kalit sifatida yuborsak Uzum matnni jimgina qabul qilmasdi.
    """
    body = _body(comments_sel=[
        {"commentType": "Размеры", "comment": {"uz": "S/M/L", "ru": "S/M/L"}},
        {"commentType": "Состав", "comment": {"uz": "paxta", "ru": "хлопок"}},
        {"commentType": "Инструкция", "comment": {"uz": "yuving", "ru": "стирать"}},
        {"commentType": "Сертификация", "comment": {"uz": "cert", "ru": "серт"}},
    ])
    got = {c["commentType"] for c in body["comments"]}
    assert got == {"Размеры", "Состав", "Инструкция", "Сертификация"}


def test_unknown_comment_type_is_not_sent_to_uzum():
    """Brauzer noto'g'ri tur yuborsa — Uzum'ga o'tkazmaymiz."""
    body = _body(comments_sel=[
        {"commentType": "Размерная сетка", "comment": {"uz": "x", "ru": "x"}},  # UI yorlig'i
        {"commentType": "Сертификаты", "comment": {"uz": "x", "ru": "x"}},      # UI yorlig'i
        {"commentType": "Состав", "comment": {"uz": "ok", "ru": "ok"}},
    ])
    assert [c["commentType"] for c in body["comments"]] == ["Состав"]


def test_empty_comment_is_not_sent():
    """Bo'sh bo'limni yubormaymiz — foydalanuvchi uni o'chirgan bo'lishi mumkin."""
    body = _body(comments_sel=[
        {"commentType": "Состав", "comment": {"uz": "", "ru": ""}},
        {"commentType": "Размеры", "comment": {"uz": "  ", "ru": ""}},
    ])
    assert body["comments"] == []


def test_missing_locale_is_filled_with_empty_string():
    """Uzum `fillEmptyLocaleComments` — yo'q til "" bo'lib ketadi, tushib qolmaydi."""
    body = _body(comments_sel=[
        {"commentType": "Состав", "comment": {"uz": "paxta"}},   # ru YO'Q
    ])
    assert body["comments"] == [
        {"comment": {"ru": "", "uz": "paxta"}, "commentType": "Состав"}
    ]


def test_old_care_behaviour_still_works_when_comments_not_sent():
    """`comments_sel=None` -> eski xatti-harakat buzilmasin (orqaga moslik)."""
    body = _body(care_uz="yuving", care_ru="стирать")
    assert body["comments"] == [
        {"comment": {"ru": "стирать", "uz": "yuving"}, "commentType": "Инструкция"}
    ]


# ── Filtrlar ─────────────────────────────────────────────────────────


def test_filter_values_are_sent_as_id_pairs():
    """Create body: filterValues=[{filterId, filterValueId}] (HAR shakli).

    Бренд (6) — Bosqich B fiksturasi bo'yicha 5078/5078 kategoriyada majburiy.
    """
    body = _body(filter_values_sel=[
        {"filterId": 6, "filterValueId": 13942},
        {"filterId": 8, "filterValueId": 53},
    ])
    assert body["filterValues"] == [
        {"filterId": 6, "filterValueId": 13942},
        {"filterId": 8, "filterValueId": 53},
    ]


def test_broken_filter_pair_is_dropped_not_sent_as_null():
    body = _body(filter_values_sel=[{"filterId": 6}, {"filterValueId": 5}])
    assert body["filterValues"] == []


# ── Rasmlar ──────────────────────────────────────────────────────────


def test_images_become_product_images_in_har_shape():
    body = _body(images=[{"key": "abc", "url": "https://images.uzum.uz/x/original.jpg"}])
    assert body["productImages"] == [{
        "deletable": True,
        "url": "https://images.uzum.uz/x/original.jpg",
        "key": "abc",
        "status": "ACTIVE",
    }]


def test_image_without_key_or_url_is_dropped():
    """Yuklash muvaffaqiyatsiz bo'lsa yarim-rasm Uzum'ga ketmasin."""
    body = _body(images=[{"key": "abc"}, {"url": "https://x/y.jpg"}, {}])
    assert body["productImages"] == []


# ── Sertifikatlar -> productCertificates ─────────────────────────────
#
# Shakl manbai: 235 full-flow HAR'dagi 24 ta REAL 201-tana + Uzum store'i
# `editProductCard/Certificates` (chunk-6dbbb9d8 @17150):
#   addCertificate() -> {expirationDate:"", number:"", certificateImages:[]}
#   updateImages()   -> {deletable, url, ordering, key, status}


def test_certificates_pass_through_to_product_certificates():
    """Brauzer qurgan sertifikat tanasi Uzum'ga O'ZGARISHSIZ yetib borsin."""
    cert = {
        "expirationDate": "2027-12-31",
        "number": "CERT-001",
        "certificateImages": [{
            "deletable": True,
            "url": "https://images.uzum.uz/x/t_product_540_high.jpg",
            "ordering": 0,
            "key": "x",
            "status": "ACTIVE",
        }],
    }
    body = _body(certificates=[cert])
    assert body["productCertificates"] == [cert]


def test_no_certificates_sends_empty_list_not_null():
    """Uzum `productCertificates` ni ro'yxat deb kutadi (235/235 tanada list)."""
    body = _body()
    assert body["productCertificates"] == []


def test_color_images_get_uzum_shape():
    """Brauzer `{key,url,color:{uz,ru},ordering}` beradi -> Uzum shakli chiqsin.

    Shakl manbai: Uzum store `E()` (chunk-6dbbb9d8 @43526):
        {colorImage: key, imageUrl: url, ordering, deletable, color, status}
    """
    body = _body(color_images=[{
        "key": "k1", "url": "https://images.uzum.uz/k1/original.jpg",
        "color": {"uz": "Alvon", "ru": "Алый"}, "ordering": 0,
    }])
    assert body["colorImages"] == [{
        "color": {"uz": "Alvon", "ru": "Алый"},
        "colorImage": "k1",
        "imageUrl": "https://images.uzum.uz/k1/original.jpg",
        "ordering": 0,
        "status": "ACTIVE",
        "deletable": True,
    }]


def test_color_image_without_colour_object_is_dropped():
    """Rangsiz rasm Uzum'ga ketmasin — u qaysi SKU'ga tegishli ekani noma'lum."""
    body = _body(color_images=[
        {"key": "k1", "url": "https://x/y.jpg"},              # color YO'Q
        {"key": "k2", "color": {"uz": "A", "ru": "Б"}},        # url YO'Q
        {"url": "https://x/z.jpg", "color": {"uz": "A", "ru": "Б"}},  # key YO'Q
    ])
    assert body["colorImages"] == []


def test_no_colours_sends_empty_color_images():
    body = _body()
    assert body["colorImages"] == []


def test_certificates_are_independent_of_certification_comment():
    """⚠️ Sertifikat FORMASI (productCertificates) va «Сертификация» MATNI
    (comments) — IKKI XIL maydon. Bittasi ikkinchisini bosib ketmasin.

    Bu xatoni qulflaydi: oldin karta faqat matn muharriri deb qurilgan edi
    (kadr 08 + bandl buni rad etdi).
    """
    body = _body(
        certificates=[{"expirationDate": "2027-01-01", "number": "N1",
                       "certificateImages": []}],
        comments_sel=[{"commentType": "Сертификация",
                       "comment": {"uz": "matn", "ru": "текст"}}],
    )
    assert body["productCertificates"][0]["number"] == "N1"
    assert body["comments"] == [
        {"comment": {"ru": "текст", "uz": "matn"}, "commentType": "Сертификация"}
    ]


# ── Umumiy shartnoma ─────────────────────────────────────────────────


def test_draft_is_created_with_empty_sku_list():
    """skuList=[] -> qoralama moderatsiyaga O'ZI ketmaydi (HAR tasdiqlagan)."""
    assert _body()["skuList"] == []


def test_body_keys_match_the_real_201_payload():
    """HAR'dagi haqiqiy 201 tanasining kalitlari — kam ham, ortiq ham bo'lmasin."""
    expected = {
        "attributes", "blockReason", "blockReasons", "blockComment", "createdByFlowB",
        "blockedImages", "categoryId", "categoryEditable", "allFiltersFilled",
        "colorImages", "colorCollectionImages", "colorVideos", "comments",
        "customCharacteristics", "dateModerated", "definedCharacteristics",
        "description", "filterValues", "filters", "imageCollection", "okpd2",
        "hasAnySkuBlocked", "photoOnPreview", "productFields", "productImages",
        "productCertificates", "ratingInfo", "shortDescription", "skuBlockReason",
        "title", "video", "skuList", "switchbackActive",
    }
    assert set(_body().keys()) == expected


# ── Video / Foto 360 (media) ─────────────────────────────────────────
#
# ⚠️ DALIL KUCHI ARALASHTIRILMAYDI:
#   · YUKLASH ENDPOINTLARI — JONLI tasdiqlangan (2026-07-18, probe).
#   · TANA KALITLARI (`video`, `imageCollection`, `colorVideos`,
#     `colorCollectionImages`) — faqat BANDL KODIDAN. 236 HAR / 6576 yozuvda
#     bu maydonlar 0/235 to'ldirilgan, ya'ni createProduct bu kalitlar bilan
#     SINALMAGAN. Shu testlar bandl semantikasini qulflaydi, «Uzum qabul
#     qiladi» degan da'voni EMAS.


def test_video_is_wrapped_in_the_bundle_shape():
    """Bandl `videoUploadHandler`: {deletable, status, url, key}.

    Brauzer faqat {key, url} yuboradi — o'ram server tomonda quriladi, aks
    holda brauzer Uzum'ning ichki shakliga bog'lanib qolardi.
    """
    body = _body(video={"key": "vk1", "url": "https://storage/original.mp4"})
    assert body["video"] == {
        "deletable": True, "status": "ACTIVE",
        "url": "https://storage/original.mp4", "key": "vk1",
    }


def test_video_absent_is_null_not_empty_dict():
    """Bandl: video yo'q -> `null`. `{}` yuborsak Uzum uni media deb o'qishi mumkin."""
    assert _body()["video"] is None


def test_half_filled_video_is_dropped_not_sent_broken():
    """key yoki url yo'q -> yubormaymiz (buzuq media qoralamani buzadi)."""
    assert _body(video={"key": "vk1"})["video"] is None
    assert _body(video={"url": "https://x/original.mp4"})["video"] is None


def test_image_collection_uses_collection_id_shape():
    """Bandl `editProductCard/Photo360` store @58913:
        updateProductCollection(id) -> {status, deletable, collectionId}
    """
    body = _body(image_collection={"collectionId": 9144})
    assert body["imageCollection"] == {
        "status": "ACTIVE", "deletable": True, "collectionId": 9144,
    }


def test_image_collection_preview_url_is_never_sent_to_uzum():
    """`previewUrl` — FAQAT UI uchun. Uzum uni tanada emas, alohida
    `collectionPreviewUrl` da saqlaydi -> tanaga sizib chiqmasin."""
    body = _body(image_collection={"collectionId": 9144,
                                   "previewUrl": "https://images.uzum.uz/k/360_720x960.jpg"})
    assert body["imageCollection"] == {
        "status": "ACTIVE", "deletable": True, "collectionId": 9144,
    }
    assert "previewUrl" not in body["imageCollection"]


def test_image_collection_absent_is_null():
    assert _body()["imageCollection"] is None


def test_non_int_collection_id_is_rejected():
    """`collectionId` — int (jonli probe: 9144). Satr kelsa yubormaymiz."""
    assert _body(image_collection={"collectionId": "9144"})["imageCollection"] is None


def test_color_videos_use_the_colormedia_store_shape():
    """Bandl ColorMedia store @44131 `colorVideosChange`:
        {color, status, deletable, videoUrl, videoKey}
    """
    body = _body(color_videos=[{
        "color": {"uz": "Alvon", "ru": "Алый"},
        "videoKey": "vk9", "videoUrl": "https://storage/v.mp4",
    }])
    assert body["colorVideos"] == [{
        "color": {"uz": "Alvon", "ru": "Алый"},
        "status": "ACTIVE", "deletable": True,
        "videoUrl": "https://storage/v.mp4", "videoKey": "vk9",
    }]


def test_color_collections_use_the_colormedia_store_shape():
    """Bandl `colorCollectionsChange`: {color, status, deletable, collectionId}."""
    body = _body(color_collections=[{
        "color": {"uz": "Alvon", "ru": "Алый"}, "collectionId": 77,
    }])
    assert body["colorCollectionImages"] == [{
        "color": {"uz": "Alvon", "ru": "Алый"},
        "status": "ACTIVE", "deletable": True, "collectionId": 77,
    }]


def test_color_media_color_is_the_title_dict_like_color_images():
    """`color` — xususiyat qiymatining `title` DICT'i, satr EMAS.

    Dalil: ColorMedia store `E(t, r)` -> `color: r.title` (`r` = qiymat
    obyekti). Ya'ni colorImages bilan AYNAN bir xil shakl.
    """
    body = _body(
        color_images=[{"key": "k", "url": "u",
                       "color": {"uz": "Alvon", "ru": "Алый"}, "ordering": 0}],
        color_videos=[{"color": {"uz": "Alvon", "ru": "Алый"},
                       "videoKey": "v", "videoUrl": "u"}],
        color_collections=[{"color": {"uz": "Alvon", "ru": "Алый"}, "collectionId": 5}],
    )
    for key in ("colorImages", "colorVideos", "colorCollectionImages"):
        assert body[key][0]["color"] == {"uz": "Alvon", "ru": "Алый"}


def test_color_media_color_is_normalised_not_passed_through():
    """Rang faqat `{uz, ru}` bo'lib ketsin — brauzer bergan ortiqcha kalitlar
    Uzum tanasiga sizib chiqmasin.

    ⚠️ Bu testni MUTATSION SINOV talab qildi: `color` ni xom o'tkazib yuborish
    (`"color": cv["color"]`) oldingi testlardan HECH BIRI ushlamagan edi —
    ular rangni allaqachon toza `{uz, ru}` shaklida berardi.
    """
    dirty = {"uz": "Alvon", "ru": "Алый", "value": "#F1371F", "skuValue": "АЛЫЙ"}
    body = _body(
        color_images=[{"key": "k", "url": "u", "color": dict(dirty), "ordering": 0}],
        color_videos=[{"color": dict(dirty), "videoKey": "v", "videoUrl": "u"}],
        color_collections=[{"color": dict(dirty), "collectionId": 5}],
    )
    for key in ("colorImages", "colorVideos", "colorCollectionImages"):
        assert body[key][0]["color"] == {"uz": "Alvon", "ru": "Алый"}, key
        assert set(body[key][0]["color"]) == {"uz", "ru"}, key


def test_broken_color_media_entries_are_dropped():
    """Rangsiz yoki kalitsiz yozuv Uzum'ga ketmasin."""
    body = _body(
        color_videos=[
            {"videoKey": "v", "videoUrl": "u"},                       # color YO'Q
            {"color": {"uz": "A", "ru": "А"}, "videoKey": "v"},       # videoUrl YO'Q
            {"color": "Alvon", "videoKey": "v", "videoUrl": "u"},     # color satr
        ],
        color_collections=[
            {"collectionId": 5},                                      # color YO'Q
            {"color": {"uz": "A", "ru": "А"}},                        # id YO'Q
        ],
    )
    assert body["colorVideos"] == []
    assert body["colorCollectionImages"] == []


def test_media_keys_stay_inside_the_known_201_key_set():
    """Media qo'shilishi tananing KALIT TO'PLAMINI o'zgartirmasin — 235 ta
    haqiqiy 201-javob shu to'plamni qulflagan."""
    full = _body(
        video={"key": "v", "url": "u"},
        image_collection={"collectionId": 1},
        color_videos=[{"color": {"uz": "A", "ru": "А"}, "videoKey": "v", "videoUrl": "u"}],
        color_collections=[{"color": {"uz": "A", "ru": "А"}, "collectionId": 2}],
    )
    assert set(full.keys()) == set(_body().keys())


# ══════════════════════════════════════════════════════════════════════
#  2-QADAM «Цены и SKU» — sendSkuData tanasi (mocked, jonli chaqiruvsiz)
#
#  Nega mocked: `sendSkuData` qoralamaga HAQIQIY SKU qo'shadi (YOZUVCHI).
#  Foydalanuvchi qoidasi — action endpoint'ni prodda sinashdan oldin mocked
#  test. Bu yerda Uzum'ga BITTA ham so'rov ketmaydi.
#
#  Tana shakli manbai: HAR'dagi 230 ta real 201-tana + bandl `lt()`
#  (chunk-2619e7c4 @88800 — «undefined» qoidalari HAR'da KO'RINMAYDI).
# ══════════════════════════════════════════════════════════════════════

from noviy_tavar.client import build_sku_body

# Jonli qoralamadan (3068623) olingan shakl — 1 xususiyat, 3 rang.
_DEFINED = [{
    "characteristicId": None,
    "characteristicTitle": {"ru": "Цвет", "uz": "Rang"},
    "characteristicValues": [
        {"skuValue": "БЕЖЕВ", "title": {"ru": "Бежевый", "uz": "Sargʻish"}, "value": "#dfc29a"},
        {"skuValue": "БЕЛЫЙ", "title": {"ru": "Белый", "uz": "Oq"}, "value": "#ffffff"},
    ],
    "flowA": True,
    "orderingNumber": 0,
    "requiredType": "NOT_REQUIRED",
}]


def _row(**kw):
    base = dict(skuTitle="BLUMMY-SOCH01-БЕЖЕВ", ikpu="02106999028438013",
                fullPrice=99000, sellPrice=59000, barcode=None,
                sellerItemCode="", id=None,
                width=45, height=73, length=193, weight=95)
    base.update(kw)
    return base


def _sku(rows=None, **kw):
    base = dict(product_id=3068623, sku_for_product="SOCH01",
                rows=rows if rows is not None else [_row()],
                defined_characteristics=_DEFINED)
    base.update(kw)
    return build_sku_body(**base)


# ── Bandl `lt()` ning «undefined» qoidalari (HAR'da ko'rinmaydi) ─────


def test_dimensions_are_all_or_nothing():
    """⚠️ Bandl: `dimensions: u ? undefined : {...}` — w/h/l/weight dan
    BITTASI bo'sh bo'lsa, `dimensions` BUTUNLAY tushadi.

    Yarim o'lchov yuborilsa Uzum uni qabul qilib, tovar noto'g'ri
    o'lchov bilan qolardi.
    """
    full = _sku()["skuList"][0]
    assert full["dimensions"] == {"width": 45, "height": 73, "length": 193, "weight": 95}

    for missing in ("width", "height", "length", "weight"):
        body = _sku(rows=[_row(**{missing: None})])
        assert "dimensions" not in body["skuList"][0], missing


def test_zero_dimension_also_drops_the_whole_block():
    """0 ham «bo'sh» — bandl `!t.width.value` ni tekshiradi, 0 falsy."""
    body = _sku(rows=[_row(width=0)])
    assert "dimensions" not in body["skuList"][0]


def test_empty_seller_item_code_key_is_absent_not_null():
    """⚠️ Bandl: `sellerItemCode: ... || undefined` — bo'sh bo'lsa kalit
    BUTUNLAY yo'q. `None` yuborish BOSHQA narsa.

    Shuning uchun HAR tanalarida bu kalit ko'rinmaydi (230/230 da yo'q) —
    HAR'ga qarab yozgan odam bu maydonni butunlay o'tkazib yuborardi.
    """
    assert "sellerItemCode" not in _sku()["skuList"][0]
    assert "sellerItemCode" not in _sku(rows=[_row(sellerItemCode="   ")])["skuList"][0]
    assert _sku(rows=[_row(sellerItemCode=" A-1 ")])["skuList"][0]["sellerItemCode"] == "A-1"


def test_id_key_is_absent_for_new_skus():
    """Bandl: `id: t.id || undefined`. HAR: `id` atigi 5/235 tanada —
    ular TAHRIRLASH yo'li, yaratish emas."""
    assert "id" not in _sku()["skuList"][0]
    assert _sku(rows=[_row(id=718371)])["skuList"][0]["id"] == 718371


def test_status_is_always_null():
    """HAR: 235/235 tanada `status: null`."""
    assert _sku()["skuList"][0]["status"] is None


def test_body_keys_match_the_231_real_201_bodies():
    """Tana kalit to'plami HAR'dagi 230 ta 201-tana bilan bir xil."""
    assert set(_sku().keys()) == {
        "productId", "skuForProduct", "skuList", "skuTitlesForCustomCharacteristics",
    }


# ── skuCharacteristicList — rang shu yerdan keladi ───────────────────


def test_sku_characteristics_are_rebuilt_from_sku_title():
    """Bandl `ot()`: skuTitle.split("-").slice(2) -> qiymat bo'laklari.

    ⚠️ Birinchi IKKI bo'lak (do'kon + prefiks) REZERV.
    """
    got = _sku()["skuList"][0]["skuCharacteristicList"]
    assert got == [{
        "characteristicTitle": {"ru": "Цвет", "uz": "Rang"},
        "definedType": True,
        "characteristicValue": {"ru": "Бежевый", "uz": "Sargʻish"},
    }]


def test_without_defined_characteristics_the_sku_goes_out_colourless():
    """⚠️ `definedCharacteristicList` uzatilmasa SKU RANGSIZ ketadi.

    Bu jim buziladigan xato: 201 keladi, lekin SKU'da rang bo'lmaydi.
    UI uni /api/sku-step javobidan VERBATIM olib o'tishi SHART.
    """
    body = build_sku_body(product_id=1, sku_for_product="X",
                          rows=[_row()], defined_characteristics=None)
    assert body["skuList"][0]["skuCharacteristicList"] == []


def test_unknown_sku_value_yields_no_characteristic():
    """skuTitle'dagi qiymat xususiyatlar ro'yxatida yo'q bo'lsa — qo'shmaymiz
    (o'ylab topilgan rang Uzum'ga ketmasin)."""
    body = _sku(rows=[_row(skuTitle="BLUMMY-SOCH01-YOQ")])
    assert body["skuList"][0]["skuCharacteristicList"] == []


def test_characteristics_follow_ordering_number_not_list_order():
    """Bandl `d` indeksi — `orderingNumber` bo'yicha TARTIB.

    Ro'yxat teskari kelsa ham, skuTitle bo'laklari orderingNumber tartibida
    o'qilishi kerak.
    """
    defined = [
        {"characteristicTitle": {"ru": "Размер", "uz": "Oʻlcham"},
         "orderingNumber": 1,
         "characteristicValues": [{"skuValue": "XL", "title": {"ru": "XL", "uz": "XL"}}]},
        {"characteristicTitle": {"ru": "Цвет", "uz": "Rang"},
         "orderingNumber": 0,
         "characteristicValues": [{"skuValue": "БЕЖЕВ",
                                   "title": {"ru": "Бежевый", "uz": "Sargʻish"}}]},
    ]
    body = build_sku_body(product_id=1, sku_for_product="P",
                          rows=[_row(skuTitle="SHOP-P-БЕЖЕВ-XL")],
                          defined_characteristics=defined)
    got = [c["characteristicTitle"]["ru"] for c in body["skuList"][0]["skuCharacteristicList"]]
    assert got == ["Цвет", "Размер"]


# ── Narx/tur ────────────────────────────────────────────────────────


def test_prices_are_ints_not_strings():
    """Brauzer input'i satr beradi — Uzum'ga raqam ketishi kerak."""
    row = _sku(rows=[_row(fullPrice="99000", sellPrice="59000")])["skuList"][0]
    assert row["fullPrice"] == 99000 and isinstance(row["fullPrice"], int)
    assert row["sellPrice"] == 59000 and isinstance(row["sellPrice"], int)


def test_barcode_empty_becomes_null_not_empty_string():
    """HAR: 230/230 da `barcode: null`."""
    assert _sku(rows=[_row(barcode="")])["skuList"][0]["barcode"] is None
    assert _sku(rows=[_row(barcode="1000035686209")])["skuList"][0]["barcode"] == "1000035686209"


def test_multiple_rows_keep_their_own_characteristics():
    """Har rang o'z qiymatini olsin — indekslar aralashib ketmasin."""
    body = _sku(rows=[
        _row(skuTitle="BLUMMY-SOCH01-БЕЖЕВ"),
        _row(skuTitle="BLUMMY-SOCH01-БЕЛЫЙ"),
    ])
    got = [r["skuCharacteristicList"][0]["characteristicValue"]["ru"] for r in body["skuList"]]
    assert got == ["Бежевый", "Белый"]


# ── definedCharacteristics shakli: requiredType + flowA MAJBURIY ──────
#
# ⚠️ Ildiz sabab (jonli, 2026-07-18): o'lcham xarakteristikali mahsulot
# createProduct'da `validation-failed` berardi. TARMOQ DALILI: 230
# muvaffaqiyatli referens tanasi / 133 xarakteristika — HAMMASI `requiredType`
# + `flowA` yuboradi, `defined` kalitini HECH QAYSI yubormaydi (0/133).
# Oldin build_create_body teskarisini qilardi (defined:true, requiredType yo'q).


def _chars(**kw):
    base = dict(
        characteristicId=-44,
        characteristicTitle={"uz": "Bilaguzuk uzunligi, sm", "ru": "Длина браслета, см"},
        orderingNumber=1,
        values=[{"title": {"uz": "15–16", "ru": "15–16"}, "value": "15–16", "skuValue": "15"}],
    )
    base.update(kw)
    return base


def test_defined_characteristic_emits_required_type_and_flow_a():
    """Har xarakteristika `requiredType` + `flowA` chiqarishi shart."""
    body = _body(characteristics_sel=[
        _chars(characteristicId=-1, requiredType="REQUIRED", flowA=False,
               characteristicTitle={"uz": "Rang", "ru": "Цвет"}),
        _chars(characteristicId=-44, requiredType="REQUIRED_ONE_OF_SIZE", flowA=False),
    ])
    dc = body["definedCharacteristics"]
    assert len(dc) == 2
    assert dc[0]["requiredType"] == "REQUIRED"
    assert dc[1]["requiredType"] == "REQUIRED_ONE_OF_SIZE"
    assert dc[0]["flowA"] is False and dc[1]["flowA"] is False


def test_defined_characteristic_never_sends_defined_key():
    """`defined` kaliti referensда 0/133 — YUBORILMASLIGI shart."""
    body = _body(characteristics_sel=[_chars(requiredType="REQUIRED_ONE_OF_SIZE")])
    assert "defined" not in body["definedCharacteristics"][0]


def test_defined_characteristic_keys_match_reference_exactly():
    """Kalitlar to'plami referens 201-tanasinikiga AYNAN teng (6 ta)."""
    body = _body(characteristics_sel=[_chars(requiredType="REQUIRED")])
    keys = set(body["definedCharacteristics"][0].keys())
    assert keys == {
        "characteristicId", "characteristicTitle", "characteristicValues",
        "orderingNumber", "requiredType", "flowA",
    }


def test_missing_required_type_falls_back_to_not_required():
    """Frontend requiredType bermasa — xavfsiz `NOT_REQUIRED` (valid enum)."""
    body = _body(characteristics_sel=[_chars()])   # requiredType YO'Q
    assert body["definedCharacteristics"][0]["requiredType"] == "NOT_REQUIRED"
    assert body["definedCharacteristics"][0]["flowA"] is False
