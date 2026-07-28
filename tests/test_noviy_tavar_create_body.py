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


# ── definedCharacteristics shakli: t8 etaloni (defined/fillType/isRequired) ──
#
# ⚠️ JONLI ETALON (t8.har, 2026-07-18 — Uzumda yasagan haqiqiy karta) + QAT'IY
# jonli tasdiq (2026-07-19, 12434 va 11894 → 201): createProduct
# definedCharacteristics `defined:true` yuboradi; REQUIRED (rang) uchun qo'shimcha
# `fillType:"REQUIRED"` + `isRequired:true`. `requiredType`/`flowA` YUBORILMAYDI.
# `orderingNumber` = xususiyatning O'Z tartibi (rang=0, Длина=44), qator indeksi EMAS.
# ❌ Oldingi model (requiredType+flowA MAJBURIY, defined yuborilmaydi) eski
# HAR' larга asoslangan edi — t8 va jonli 201 uni rad etdi.


def _chars(**kw):
    base = dict(
        characteristicId=-44,
        characteristicTitle={"uz": "Bilaguzuk uzunligi, sm", "ru": "Длина браслета, см"},
        orderingNumber=44,
        values=[{"title": {"uz": "15–16", "ru": "15–16"}, "value": "15–16", "skuValue": "15"}],
    )
    base.update(kw)
    return base


def test_defined_characteristic_emits_defined_true():
    """Har xarakteristika `defined:true` chiqaradi (t8 etaloni)."""
    body = _body(characteristics_sel=[
        _chars(characteristicId=-1, requiredType="REQUIRED", orderingNumber=0,
               characteristicTitle={"uz": "Rang", "ru": "Цвет"}),
        _chars(characteristicId=-44, requiredType="NOT_REQUIRED"),
    ])
    dc = body["definedCharacteristics"]
    assert len(dc) == 2
    assert dc[0]["defined"] is True and dc[1]["defined"] is True


def test_required_color_emits_fill_type_and_is_required():
    """REQUIRED (rang) → `fillType:"REQUIRED"` + `isRequired:true` (t8 etaloni)."""
    body = _body(characteristics_sel=[
        _chars(characteristicId=-1, requiredType="REQUIRED", orderingNumber=0),
    ])
    dc = body["definedCharacteristics"][0]
    assert dc["fillType"] == "REQUIRED"
    assert dc["isRequired"] is True


def test_non_required_char_omits_fill_type():
    """NOT_REQUIRED/o'lcham → fillType/isRequired YUBORILMAYDI (t8: o'lchamda yo'q)."""
    dc = _body(characteristics_sel=[_chars(requiredType="NOT_REQUIRED")])["definedCharacteristics"][0]
    assert "fillType" not in dc and "isRequired" not in dc


def test_defined_characteristic_never_sends_required_type_or_flow_a():
    """`requiredType`/`flowA` YUBORILMASLIGI shart (t8 etaloni)."""
    dc = _body(characteristics_sel=[_chars(requiredType="REQUIRED")])["definedCharacteristics"][0]
    assert "requiredType" not in dc and "flowA" not in dc


def test_ordering_number_is_characteristic_own_not_row_index():
    """`orderingNumber` = xususiyatning O'Z tartibi (44), qator indeksi (0/1) EMAS."""
    dc = _body(characteristics_sel=[_chars(characteristicId=-44, orderingNumber=44)])["definedCharacteristics"][0]
    assert dc["orderingNumber"] == 44


def test_defined_characteristic_keys_match_reference_exactly():
    """Kalitlar to'plami t8 etaloniga AYNAN teng.
    O'lcham (NOT_REQUIRED): 5 kalit; rang (REQUIRED): +fillType +isRequired = 7."""
    size = _body(characteristics_sel=[_chars(requiredType="NOT_REQUIRED")])["definedCharacteristics"][0]
    assert set(size.keys()) == {
        "characteristicId", "characteristicTitle", "characteristicValues",
        "orderingNumber", "defined",
    }
    color = _body(characteristics_sel=[_chars(requiredType="REQUIRED")])["definedCharacteristics"][0]
    assert set(color.keys()) == {
        "characteristicId", "characteristicTitle", "characteristicValues",
        "orderingNumber", "defined", "fillType", "isRequired",
    }


# ── «≤2 xususiyat» darvozasi: filled_characteristic_count ────────────
#
# ⚠️ JONLI dalil (QAT'IY probe 2026-07-19, kat. 12811/12434):
#   2 razmer (rangsiz)          → 201 ✓   ·  3 razmer (rangsiz)   → 400 ✗
#   rang + 1 razmer             → 201 ✓   ·  rang + 2 razmer      → 400 ✗
#   12434 rang+Длина+Обхват     → 400 ✗   (ikkalasi NOT_REQUIRED!)
#   1 xususiyat × ko'p qiymat   → 201 ✓   (bitta char ichida ko'p qiymat NORMAL)
# Qoida sof SON (≤2) — TUR (rang/razmer) AHAMIYATSIZ, rang ham sanaladi.
# Route (nt_create) bu funksiya >2 qaytarsa 400 beradi — brauzer cap'iga
# ishonmasdan. [[project_noviy_tavar_size_constraint]]

from noviy_tavar.client import filled_characteristic_count


def _size(vals=1):
    return {"characteristicId": -21, "requiredType": "REQUIRED_ONE_OF_SIZE",
            "values": [{"skuValue": str(i)} for i in range(vals)]}


def test_two_characteristics_are_allowed():
    """Rang + 1 razmer = 2 → darvoza o'tkazadi (jonli 201)."""
    chars = [{"characteristicId": -1, "requiredType": "REQUIRED", "values": [{"skuValue": "К"}]},
             _size()]
    assert filled_characteristic_count(chars) == 2


def test_three_characteristics_are_counted_as_three():
    """3 qiymatli char → 3 → route 400 beradi (jonli validation-failed-001).
    12434 Braslet: rang + Длина + Обхват (ikkalasi NOT_REQUIRED!) → 400."""
    color = {"characteristicId": -1, "requiredType": "REQUIRED", "values": [{"skuValue": "К"}]}
    a = {"characteristicId": -30, "requiredType": "NOT_REQUIRED", "values": [{"skuValue": "1"}]}
    b = {"characteristicId": -31, "requiredType": "NOT_REQUIRED", "values": [{"skuValue": "2"}]}
    assert filled_characteristic_count([color, a, b]) == 3


def test_color_is_counted_too():
    """Rang MAXSUS emas — u ham qiymatli xususiyat sifatida sanaladi."""
    color = {"characteristicId": -1, "requiredType": "REQUIRED", "values": [{"skuValue": "К"}]}
    assert filled_characteristic_count([color]) == 1


def test_multiple_values_in_one_characteristic_still_count_as_one():
    """⚠️ Bitta char ICHIDA ko'p qiymat NORMAL (poyabzal 36/37/38 → 201) —
    QIYMAT emas, XUSUSIYAT sanaladi."""
    assert filled_characteristic_count([_size(vals=8)]) == 1


def test_empty_characteristic_is_not_counted():
    """Qatori bor, lekin qiymatsiz xususiyat sanalmaydi."""
    assert filled_characteristic_count([_size(vals=0)]) == 0
    assert filled_characteristic_count([{"requiredType": "REQUIRED_ONE_OF_SIZE"}]) == 0


def test_none_and_junk_are_safe():
    """None / buzuq element yiqilmasin."""
    assert filled_characteristic_count(None) == 0
    assert filled_characteristic_count([None, "x", {"requiredType": "NOT_REQUIRED",
                                                    "values": [{"skuValue": "1"}]}]) == 1


# ── createProduct 400 → tushunarli xabar: explain_create_error ───────
#
# ⚠️ JONLI dalil (2026-07-18, scripts/nt_forbidden.py + nt_codes.py):
#   forbidden = razmer kategoriyaga to'g'ri kelmaydi (ayollar босоножкаsiда
#   «bolalar»/«erkaklar» razmeri) · missed = rang/razmer majburiy, yo'q ·
#   bad-request-001 = Бренд filtri yo'q. Read-only signal YO'Q — faqat shu 400.
# [[project_noviy_tavar_createproduct_rules]]

from noviy_tavar.client import explain_create_error


def test_known_create_error_codes_map_to_uzbek_messages():
    for code in ("category-defined-characteristics-forbidden",
                 "category-defined-characteristics-missed",
                 "bad-request-001", "validation-failed-001"):
        body = '{"payload":null,"errors":[{"code":"%s","message":"x"}]}' % code
        got_code, msg = explain_create_error(body)
        assert got_code == code
        assert msg and isinstance(msg, str), code


def test_forbidden_message_tells_user_to_pick_another_size():
    _, msg = explain_create_error('{"errors":[{"code":"category-defined-characteristics-forbidden"}]}')
    assert "razmer" in msg.lower()


def test_unknown_code_returns_empty_message_for_fallback():
    """Noma'lum kod → '' (chaqiruvchi eski _portal_error 502 ga tushadi)."""
    code, msg = explain_create_error('{"errors":[{"code":"some-new-code"}]}')
    assert code == "some-new-code"
    assert msg == ""


def test_code_extracted_from_TRUNCATED_body():
    """⚠️ NoviyTavorError tanani 500 belgiga kesadi — uzun ko'p-xatoli tanada
    json.loads YIQILADI. Regex `errors[0].code` ni baribir topishi shart."""
    # Tana o'rtasida kesilgan (yopuvchi } yo'q) — json.loads bu yerda yiqiladi.
    truncated = ('{"payload":null,"errors":[{"code":'
                 '"category-defined-characteristics-missed","message":'
                 '"Bosonojkalar toifasi uchun majburiy xususiyat')
    code, msg = explain_create_error(truncated)
    assert code == "category-defined-characteristics-missed"
    assert msg


def test_empty_or_garbage_body_is_safe():
    assert explain_create_error("") == ("", "")
    assert explain_create_error("not json at all") == ("", "")
    assert explain_create_error(None) == ("", "")


def test_sendsku_dimension_error_is_mapped():
    """2-BOSQICH (JONLI 2026-07-18): o'lchovsiz SKU → 400
    `weight-and-size-characteristics-required-error` (createProduct bilan bir xil
    `{errors:[{code}]}` shakl → explain_create_error tutadi)."""
    body = ('{"payload":null,"errors":[{"code":'
            '"weight-and-size-characteristics-required-error","message":"..."}]}')
    code, msg = explain_create_error(body)
    assert code == "weight-and-size-characteristics-required-error"
    assert "o'lcham" in msg.lower() or "vazn" in msg.lower()


# ── 3-BOSQICH (save-filters) xato shakli: explain_filter_error ───────
#
# ⚠️ Shakl createProduct'nikidan BOSHQA (JONLI 2026-07-18):
#   {"payload":[{"in":"body","path":"skus.<id>.attributes.<code>",
#                "msg":"Qiymatni to'ldiring"}]}  ← "msg" (createProduct "message")

from noviy_tavar.client import explain_filter_error


def test_filter_error_maps_missing_required_attributes():
    body = ('{"payload":[{"in":"body","path":"skus.11.attributes.gender",'
            '"msg":"Qiymatni to\'ldiring"},{"in":"body",'
            '"path":"skus.11.attributes.ring_material","msg":"Qiymatni to\'ldiring"}]}')
    msg = explain_filter_error(body)
    assert msg and "Свойства" in msg


def test_filter_error_works_on_truncated_body():
    """NoviyTavorError tanani 500 belgiga kesadi — `"msg"` boshida, topiladi."""
    truncated = '{"payload":[{"in":"body","path":"skus.11.attributes.gender","msg":"Qiymatni'
    assert explain_filter_error(truncated)


def test_filter_error_empty_for_non_filter_bodies():
    """createProduct shakli (`"message"`, `"msg"` yo'q) → '' (bu parser tegmaydi)."""
    assert explain_filter_error('{"errors":[{"code":"x","message":"y"}]}') == ""
    assert explain_filter_error("") == ""
    assert explain_filter_error(None) == ""


# ── MAXSUS («custom») xususiyatlar -> customCharacteristics[] ────────
#
# DALIL (Uzum bandli, editProductCard/Characteristics + submit yig'uvchisi `it()`):
#   definedCharacteristics = tanlanganlarning `defined` bo'lganlari
#   customCharacteristics  = qolgani, `orderingNumber: 100 + indeks` bilan
#   `ne()` maxsus xususiyatga characteristicId BERMAYDI — faqat
#   `custom:true` + characteristicTitle + characteristicValues.
# Chegara: >3 bo'lsa Uzum saqlashda rad etadi (limiting_number_of_custom_...).


def _char(custom=False, **kw):
    base = dict(
        characteristicId=44,
        characteristicTitle={"uz": "Uzunligi", "ru": "Длина"},
        orderingNumber=44,
        values=[{"title": {"uz": "18 sm", "ru": "18 см"}, "value": "18 см",
                 "skuValue": ""}],
    )
    if custom:
        base["custom"] = True
    base.update(kw)
    return base


def test_custom_characteristic_goes_to_custom_bucket_not_defined():
    body = _body(characteristics_sel=[_char(custom=True)])
    assert body["definedCharacteristics"] == []
    assert len(body["customCharacteristics"]) == 1
    entry = body["customCharacteristics"][0]
    assert entry["custom"] is True
    assert entry["characteristicTitle"] == {"uz": "Uzunligi", "ru": "Длина"}
    # Bandl: maxsus xususiyatda characteristicId YO'Q (`defined` ham yo'q).
    assert "characteristicId" not in entry
    assert "defined" not in entry


def test_custom_characteristics_are_renumbered_from_100():
    body = _body(characteristics_sel=[
        _char(custom=True, characteristicTitle={"uz": "A", "ru": "А"}, orderingNumber=7),
        _char(custom=True, characteristicTitle={"uz": "B", "ru": "Б"}, orderingNumber=9),
    ])
    assert [c["orderingNumber"] for c in body["customCharacteristics"]] == [100, 101]


def test_defined_and_custom_are_split_correctly():
    body = _body(characteristics_sel=[
        _char(characteristicId=-1, characteristicTitle={"uz": "Rang", "ru": "Цвет"},
              orderingNumber=0, requiredType="REQUIRED"),
        _char(custom=True, characteristicTitle={"uz": "Naqsh", "ru": "Узор"}),
    ])
    assert len(body["definedCharacteristics"]) == 1
    assert len(body["customCharacteristics"]) == 1
    # Rang REQUIRED -> fillType/isRequired qo'shiladi (mavjud xulq buzilmadi).
    assert body["definedCharacteristics"][0]["fillType"] == "REQUIRED"
    assert body["definedCharacteristics"][0]["defined"] is True


def test_custom_characteristic_without_values_is_dropped():
    """Qiymatsiz xususiyat — bandl ham `characteristicValues.length > 0` filtri."""
    body = _body(characteristics_sel=[_char(custom=True, values=[])])
    assert body["customCharacteristics"] == []
    assert body["definedCharacteristics"] == []


# ── Гарантия (WARRANTY) -> productFields ─────────────────────────────
#
# DALIL (jonli field-descriptions: fieldName WARRANTY, INTEGER, required false;
# bandl ProductFieldsDescriptions `i()`/`o()`): create body `productFields`
# INTEGER kutadi; bo'sh/string tozalanadi; <6 UI+server rad etadi.


def test_warranty_passed_as_integer():
    body = _body(product_fields={"WARRANTY": 12})
    assert body["productFields"] == {"WARRANTY": 12}
    assert isinstance(body["productFields"]["WARRANTY"], int)


def test_warranty_string_is_coerced_to_int():
    body = _body(product_fields={"WARRANTY": "6"})
    assert body["productFields"] == {"WARRANTY": 6}


def test_empty_warranty_is_dropped():
    for empty in ("", None):
        body = _body(product_fields={"WARRANTY": empty})
        assert body["productFields"] == {}


def test_no_product_fields_gives_empty_dict():
    body = _body(product_fields={})
    assert body["productFields"] == {}


def test_garbage_warranty_is_dropped_not_crashed():
    body = _body(product_fields={"WARRANTY": "abc"})
    assert body["productFields"] == {}
