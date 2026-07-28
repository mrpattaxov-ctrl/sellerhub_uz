/* «Новый товар» — feature-local behavior (1-bosqich, bo'sh holat).
 *
 * Doira: FAQAT [data-page="noviy-tavar"] ildizi ichida ishlaydi. Global
 * obyektlarga yozmaydi, boshqa sahifalarga hodisa bog'lamaydi.
 *
 * Xavfsizlik: Uzum'ga TO'G'RIDAN-TO'G'RI murojaat yo'q — hamma chaqiruv
 * /noviy-tavar/api/* Flask proxy'lari orqali (egalik guard'i serverda).
 */
(function () {
  'use strict';

  var root = document.querySelector('[data-page="noviy-tavar"]');
  if (!root) return;

  var lang = root.dataset.lang || 'uz';
  function tr(uz, ru) { return lang === 'uz' ? uz : ru; }

  // ── Holat ────────────────────────────────────────────────────────
  var state = {
    categoryId: null,   // TASDIQLANGAN kategoriya («Qabul qilish» bosilgach)
    meta: null,         // /api/category-meta javobi
    rows: [],           // tanlangan xususiyat qatorlari
    images: [],         // [{key, url}] — umumiy rasmlar (create body: productImages)
    // create body: productCertificates. Element shakli AYNAN Uzum store'idagidek
    // (chunk-6dbbb9d8 @17150): {expirationDate, number, certificateImages[]}
    certificates: [],
    // create body: colorImages. client.py `{key,url,color:{uz,ru},ordering}` ni
    // kutadi va Uzum shakliga (colorImage/imageUrl/status/deletable) o'giradi.
    colorImages: [],
    // create body: video — {key,url}; client.py `{deletable,status,url,key}` ga o'giradi.
    // ⚠️ Video RASMDAN boshqa proxy'da: /noviy-tavar/api/upload-video
    // (server tomonda auth sxemasi ham boshqa — client.py izohiga qarang).
    video: null,
    // create body: imageCollection — {collectionId, previewUrl}. previewUrl faqat
    // UI uchun (Uzum `collectionPreviewUrl` ni ALOHIDA saqlaydi, tanaga qo'shmaydi).
    imageCollection: null,
    // create body: colorVideos / colorCollectionImages
    colorVideos: [],       // [{color:{uz,ru}, videoKey, videoUrl}]
    colorCollections: [],  // [{color:{uz,ru}, collectionId, previewUrl}]
    filterValues: {},   // {filterId: valueId} — create body: filterValues[]
    // create body: productFields — {WARRANTY: <oy>} kabi. field-descriptions'dan
    // (hozircha faqat WARRANTY qo'llab-quvvatlanadi). Bo'sh {} bo'lsa yuborilmaydi.
    productFields: {},
    shop: null,
    tipsHidden: false,
    // Yaratilgan/ochilgan qoralama id'si — qadam nishonlari va «Saqlash»
    // tugmasining yaratish/yangilash tanlovi shunga qarab ishlaydi.
    productId: null,
    // Kartochka formasi SHU sahifada to'ldirilganmi. ?productId= bilan
    // yangidan ochilganda forma bo'sh bo'ladi — u holda 1-qadam qulflanadi
    // (bo'sh formani mavjud karta ustiga saqlab yubormaslik uchun).
    cardFilled: false
  };

  try {
    var shops = JSON.parse(root.dataset.shops || '[]');
    if (shops.length) state.shop = shops[0].uzum_id;
  } catch (e) { /* shops yo'q — proxy 400 qaytaradi, UI tirik qoladi */ }


  /* ══════════════════════════════════════════════════════════════
   *  SAQLANMAGAN MEHNAT QO'RIQCHISI (F5 / «orqaga» / oynani yopish)
   *
   *  Brauzerning O'Z tasdiq oynasini chiqaramiz (`beforeunload`). Maxsus
   *  matn 2018-dan beri taqiqlangan — brauzer o'z standart matnini
   *  ko'rsatadi; bizning ishimiz FAQAT «yo'qotadigan narsa bormi?» ga
   *  to'g'ri javob berish, aks holda har chiqishda bezor qiladigan oyna.
   *
   *  · 1-QADAM — imzo (signature) taqqoslash: forma holati oxirgi XAVFSIZ
   *    nuqtadan (bo'sh forma / tiklangan qoralama / Uzumga saqlangan karta)
   *    farq qilsa «iflos». `collectDraft()` butun holatni qamrab olgani
   *    uchun bu KLIK bilan bo'ladigan o'zgarishlarni ham (rasm, xususiyat,
   *    kategoriya, sertifikat) ushlaydi — ular `input`/`change` bermaydi.
   *  · 2/3-QADAM — oddiy bayroq. Bu qadamlarda localStorage qoralamasi
   *    YO'Q (narx/SKU/xususiyat faqat Uzumda), ya'ni har qanday tahrir
   *    yangilashda BUTUNLAY yo'qoladi — ogohlantirish shu yerda eng zarur.
   *
   *  ⚠️ In-app qadam nishonlari sahifani tark etmaydi — u yerda oyna
   *  chiqmaydi va chiqmasligi kerak (ma'lumot DOM'da qoladi).
   * ══════════════════════════════════════════════════════════════ */
  var ntDirty = { 2: false, 3: false };
  var ntCleanSig = null;        // 1-qadamning oxirgi XAVFSIZ holati imzosi

  function ntSig() {
    // `collectDraft` pastda e'lon qilingan (funksiya deklaratsiyasi — ko'tariladi).
    try { var d = collectDraft(); d.ts = 0; return JSON.stringify(d); }
    catch (e) { return null; }         // holat hali qurilmagan
  }
  function ntSnapshot() { ntCleanSig = ntSig(); }

  function ntHasUnsaved() {
    if (state.step !== 1) return !!ntDirty[state.step];
    var s = ntSig();
    if (s === null) return false;
    // Xavfsiz nuqta hali belgilanmagan (ildizlar kelmadi/xato) — shubhada
    // FOYDALANUVCHI foydasiga: o'zgargan deb hisoblaymiz.
    var changed = (ntCleanSig === null) || (s !== ntCleanSig);
    if (!changed) return false;
    // ⚠️ MAZMUN DARVOZASI. O'zgarish o'zi yetarli emas: bo'sh formada do'kon
    // almashtirish ham imzoni o'zgartiradi, lekin yo'qotadigan MEHNAT yo'q —
    // u yerda oyna chiqarish shunchaki bezor qilish bo'lardi (jonli uchradi
    // 2026-07-22: do'kon tanlash ogohlantirish chiqarib yubordi).
    try { return draftHasContent(collectDraft()); } catch (e) { return false; }
  }


  // ── Maslahatlarni yashirish/ko'rsatish ──────────────────────────
  var tipsBtn = document.getElementById('ntTipsToggle');
  var tipsLabel = document.getElementById('ntTipsLabel');

  if (tipsBtn) {
    // sahifa yangilangach holat saqlanadi
    try {
      if (localStorage.getItem('nt.tipsHidden') === '1') setTips(true);
    } catch (e) { /* localStorage o'chiq — jim o'tamiz */ }

    tipsBtn.addEventListener('click', function () { setTips(!state.tipsHidden); });
  }

  function setTips(hidden) {
    state.tipsHidden = hidden;
    root.classList.toggle('nt-tips-hidden', hidden);
    tipsBtn.setAttribute('aria-pressed', hidden ? 'true' : 'false');
    tipsLabel.textContent = hidden
      ? tr("Maslahatlarni ko'rsatish", 'Показать подсказки')
      : tr('Maslahatlarni yashirish', 'Скрыть подсказки');
    try { localStorage.setItem('nt.tipsHidden', hidden ? '1' : '0'); } catch (e) {}
  }

  // ── Jonli hisoblagichlar (current/max) ──────────────────────────
  root.querySelectorAll('[data-nt-counter]').forEach(function (el) {
    var out = document.getElementById(el.dataset.ntCounter);
    if (!out) return;
    var max = el.getAttribute('maxlength') || 0;
    function upd() { out.textContent = el.value.length + '/' + max; }
    el.addEventListener('input', function () { upd(); validate(); });
    upd();
  });

  // ── Boy matn muharrirlari ───────────────────────────────────────
  root.querySelectorAll('[data-nt-editor]').forEach(function (ed) {
    var area = ed.querySelector('.nt-editor-area');
    var bar = ed.querySelector('.nt-editor-bar');
    if (!area || !bar) return;

    bar.addEventListener('click', function (e) {
      var btn = e.target.closest('[data-cmd]');
      if (!btn || btn.tagName === 'SELECT') return;
      var cmd = btn.dataset.cmd;
      area.focus();
      if (cmd === 'bold' || cmd === 'italic') {
        document.execCommand(cmd, false, null);
        syncBar();
      } else if (cmd === 'undo' || cmd === 'redo') {
        document.execCommand(cmd, false, null);
      } else if (cmd === 'image') {
        // Rasm — proxy orqali yuklanadi (Uzum'ga to'g'ridan-to'g'ri EMAS).
        // Yuklash oqimi rang xususiyati bosqichida ulanadi.
        notify(tr('Rasm yuklash kategoriya tanlangach ochiladi',
                  'Загрузка изображения станет доступна после выбора категории'));
      }
    });

    var sel = bar.querySelector('[data-cmd="block"]');
    if (sel) {
      sel.addEventListener('change', function () {
        area.focus();
        var v = sel.value;
        document.execCommand('formatBlock', false, v === 'p' ? 'P' : v.toUpperCase());
      });
    }

    // faol formatlash holatini panelga aks ettirish
    function syncBar() {
      ['bold', 'italic'].forEach(function (c) {
        var b = bar.querySelector('[data-cmd="' + c + '"]');
        if (!b) return;
        var on = false;
        try { on = document.queryCommandState(c); } catch (e) {}
        b.setAttribute('aria-pressed', on ? 'true' : 'false');
      });
    }

    area.addEventListener('keyup', syncBar);
    area.addEventListener('mouseup', syncBar);
    area.addEventListener('input', validate);
  });

  // ── Xususiyat tanlagichi: kategoriyaga bog'liq (frame 06) ───────
  var charHint = document.getElementById('ntCharHint');

  function syncCategoryDependent() {
    // Referens dalili (001_category-baseline-viewport.png): kategoriya
    // tasdiqlangach dropdown YOQILADI va sariq eslatma YO'QOLADI.
    var has = !!state.categoryId;
    var btn = document.getElementById('ntCharBtn');
    if (btn) btn.disabled = !has;
    if (!has) closeCharPanel();
    if (charHint) charHint.hidden = has;

    // «Общие фотографии товара» — bandl sharti: requiredMediaType !== NOT_DEFINED.
    // Qiymatni META'dan o'qiymiz (qotirmaymiz): /category/{id}/fields javobi
    // {fields:{...}} shaklida, shuning uchun meta.fields.fields.
    var card = document.getElementById('ntCardPhoto');
    if (card) card.hidden = !has || requiredMediaType() === 'NOT_DEFINED';

    // Sertifikat kartasi `fillType` ga bog'liq (`*`, sariq banner, «Добавить»).
    // Karta O'ZI hech qachon yashirilmaydi — kadr 08: kategoriyasiz ham turadi.
    syncCertGate();

    // Rang-media darvozasining ikkinchi sharti — requiredMediaType (kategoriyadan).
    syncColorMedia();

    // Гарантия — meta.fieldDescriptions'da WARRANTY bo'lsa ko'rsatiladi.
    syncWarranty();
  }

  // ── Гарантия (в месяцах) ────────────────────────────────────────
  // DALIL (jonli field-descriptions + bandl ProductFieldsDescriptions):
  //   javob = [{fieldName:"WARRANTY", fieldType:"INTEGER", required:false, ...}]
  //   bandl `i()`: number → >3 belgi bo'lsa 999, aks holda raqamlarni ajratib son
  //   bandl `l()`: qiymat bo'lsa productFields[WARRANTY]=son, bo'lmasa o'chiradi
  //   bandl `o()`: 0 → value_cannot_be_zero; <6 → warranty_min_months
  //   yuklashda: productFields[WARRANTY] bo'sh bo'lsa default 6 qo'yiladi
  function warrantyField() {
    var list = state.meta && state.meta.fieldDescriptions;
    if (!Array.isArray(list)) return null;
    for (var i = 0; i < list.length; i++) {
      if (list[i] && list[i].fieldName === 'WARRANTY') return list[i];
    }
    return null;
  }

  function syncWarranty() {
    var card = document.getElementById('ntCardWarranty');
    var input = document.getElementById('ntWarranty');
    if (!card) return;
    var wf = warrantyField();
    if (!state.categoryId || !wf) {
      card.hidden = true;
      // Kategoriya WARRANTY'siz bo'lsa — eski qiymatni tashlaymiz.
      delete state.productFields.WARRANTY;
      return;
    }
    card.hidden = false;
    // Bandl: qiymat yo'q bo'lsa qonuniy default 6 qo'yiladi.
    if (state.productFields.WARRANTY == null || state.productFields.WARRANTY === '') {
      state.productFields.WARRANTY = 6;
    }
    if (input) input.value = String(state.productFields.WARRANTY);
    syncWarrantyClear();
  }

  function syncWarrantyClear() {
    var input = document.getElementById('ntWarranty');
    var clr = document.getElementById('ntWarrantyClear');
    if (clr && input) clr.hidden = !input.value;
  }

  // Bo'sh emas va <6 bo'lsa false qaytaradi + inline xato ko'rsatadi.
  function warrantyValid() {
    var err = document.getElementById('ntWarrantyErr');
    var card = document.getElementById('ntCardWarranty');
    var hide = function () { if (err) err.hidden = true; };
    if (!card || card.hidden) { hide(); return true; }
    var raw = state.productFields.WARRANTY;
    // Bo'sh — ixtiyoriy (qonun bo'yicha 6 bo'ladi), xato yo'q.
    if (raw == null || raw === '') { hide(); return true; }
    var n = Number(raw);
    var msg = '';
    if (n === 0) msg = tr('Maydon qiymati 0 ga teng boʻlmasligi kerak',
                          'Значение поля не может быть равно 0');
    else if (n < 6) msg = tr('Kafolat muddati kamida 6 oy boʻlishi kerak',
                             'Минимальный срок гарантии — 6 месяцев');
    if (err) { err.textContent = msg; err.hidden = !msg; }
    return !msg;
  }

  (function wireWarranty() {
    var input = document.getElementById('ntWarranty');
    var clr = document.getElementById('ntWarrantyClear');
    if (input) {
      input.addEventListener('input', function () {
        // Bandl `i()`: 3 belgidan uzun → 999, aks holda faqat raqamlar.
        var digits = input.value.replace(/[^0-9]/g, '');
        var n = digits.length > 3 ? 999 : (digits === '' ? '' : Number(digits));
        input.value = n === '' ? '' : String(n);
        if (n === '') delete state.productFields.WARRANTY;
        else state.productFields.WARRANTY = n;
        syncWarrantyClear();
        warrantyValid();
      });
    }
    if (clr) {
      clr.addEventListener('click', function () {
        if (input) input.value = '';
        delete state.productFields.WARRANTY;
        syncWarrantyClear();
        warrantyValid();
      });
    }
  })();

  function requiredMediaType() {
    var f = state.meta && state.meta.fields && state.meta.fields.fields;
    return (f && f.requiredMediaType) || 'NOT_DEFINED';
  }

  function requiredFiltersFilled() {
    // Bosqich B fiksturasi: har kategoriyada «Бренд» MAJBURIY (5078/5078).
    // Majburiylik meta'dan o'qiladi, qotirilmaydi.
    var list = (state.meta && state.meta.filters) || [];
    if (!Array.isArray(list)) return true;
    return list.every(function (f) {
      return !f.required || state.filterValues[f.id] != null;
    });
  }

  function requiredCharsSelected() {
    // «Цвет» kabi REQUIRED xususiyat qatori MAVJUD va QIYMATLI bo'lishi shart.
    // JONLI dalil (2026-07-18): REQUIRED cat.da rang qiymatsiz → createProduct
    // 400 `category-defined-characteristics-missed`. Signal meta'dan (o.required).
    return (charOptions || []).filter(function (o) { return o.required; })
      .every(function (o) {
        var row = state.rows.filter(function (r) { return r.id === o.id; })[0];
        return row && row.selected.length > 0;
      });
  }

  function sizeRequirementMet() {
    // REQUIRED_ONE_OF_SIZE bor kategoriyada AYNAN bitta razmer-tizim qiymatli
    // bo'lishi shart (radio ≤1 ni, bu ≥1 ni ta'minlaydi). JONLI: razmersiz →
    // 400 `...-missed`. Razmer bo'lmasa (12811 kabi) ortiqcha bloklamaymiz.
    var hasSize = (charOptions || []).some(function (o) {
      return o.requiredType === 'REQUIRED_ONE_OF_SIZE';
    });
    if (!hasSize) return true;
    return state.rows.filter(isSizeRow).some(function (r) { return r.selected.length > 0; });
  }

  // ⚠️ Qizil xato qutisi DARROV chiqmaydi — faqat foydalanuvchi «Saqlash»ga
  // urinib majburiy xususiyatni to'ldirmagan bo'lsa (Uzum kabi). saveBtn handler
  // uni `true` qiladi; to'ldirilgach qayta hidden.
  var charReqAttempted = false;

  function syncCharReq() {
    // Majburiy xususiyat to'ldirilmasa Uzum'dagi AYNAN inline xato qutisi.
    // Matn Uzum bundle i18n'idan (t9 + category-interactions HAR):
    //   required_characteristics_without_values / required_one_of_size_characteristics
    // Karta ('ntCardChars') qizil ramka oladi (is-invalid). Faqat 1-qadamda.
    var el = document.getElementById('ntCharReq');
    var card = document.getElementById('ntCardChars');
    if (!el) return;
    var hide = function () {
      el.hidden = true;
      if (card) card.classList.remove('is-invalid');
    };
    // Urinish bo'lmaguncha — hech qachon ko'rsatmaymiz.
    if (!charReqAttempted) { hide(); return; }
    if (state.step === 2 || !state.categoryId || !charOptions || !charOptions.length) {
      hide(); return;
    }
    var msgs = [];
    // Majburiy (REQUIRED, masalan «Цвет») qiymatsiz xususiyatlar NOMI bilan.
    var missNames = (charOptions || []).filter(function (o) { return o.required; })
      .filter(function (o) {
        var row = state.rows.filter(function (r) { return r.id === o.id; })[0];
        return !(row && row.selected.length > 0);
      })
      .map(function (o) { return tr(o.uz, o.ru); });
    if (missNames.length) {
      msgs.push(tr(
        "Iltimos, xususiyat uchun kamida bitta qadrlikni tanlang va to'ldiring: ",
        "Пожалуйста, выберите и заполните хотя бы одно значение для характеристики: "
      ) + missNames.join(', '));
    }
    if (!sizeRequirementMet()) {
      msgs.push(tr(
        "Iltimos, kamida bitta o'lcham xususiyatini tanlang va to'ldiring",
        "Пожалуйста, выберите и заполните хотя бы одну размерную характеристику"
      ));
    }
    if (!msgs.length) { hide(); return; }
    el.textContent = '';
    msgs.forEach(function (m, i) {
      if (i) el.appendChild(document.createElement('br'));
      el.appendChild(document.createTextNode(m));
    });
    el.hidden = false;
    if (card) card.classList.add('is-invalid');
  }

  // ── Validatsiya + asosiy tugma holati ───────────────────────────
  // Referens: bo'sh formada «Сохранить и продолжить» O'CHIQ.
  function validate() {
    // 2-qadamda saqlash tugmasini s2Validate() boshqaradi — bu yerdan
    // tegmaymiz, aks holda ikkalasi bir-birini o'chiradi.
    if (state.step === 2) return true;
    var titleUz = val('ntTitleUz');
    var titleRu = val('ntTitleRu');
    var descUz = txt('ntDescUz');
    var descRu = txt('ntDescRu');
    // Rasm MAJBURIY: bandlda karta `required`, serverimiz ham kamida bitta
    // rasmsiz qoralama yaratmaydi (routes.nt_create). Karta ko'rinmasa
    // (NOT_DEFINED) rasm talab qilinmaydi.
    var needPhoto = !!state.categoryId && requiredMediaType() !== 'NOT_DEFINED';
    // ⚠️ Majburiy XUSUSIYAT (rang/razmer) bu yerda tugmani O'CHIRMAYDI — Uzum
    // kabi tugma bosilsin, keyin urinishda qizil xato chiqsin (saveBtn handler +
    // charReqAttempted). Aks holda foydalanuvchi hech narsa qilmasdan turib
    // qizil ogohlantirish darrov chiqib turardi (user shikoyati 2026-07-19).
    var ok = !!state.categoryId && !!(titleUz || titleRu) && !!(descUz || descRu)
             && (!needPhoto || state.images.length > 0)
             && requiredFiltersFilled()
             && certificatesValid();
    var save = document.getElementById('ntSave');
    if (save) save.disabled = !ok;
    syncCharReq();
    // validate() forma HAR o'zgarganda chaqiriladi (rasm, xususiyat,
    // kategoriya, sertifikat) — qoralamani saqlash uchun ishonchli ilgak.
    // `input`/`change` hodisalari faqat matn maydonlarini qamrab oladi.
    if (root.ntSaveDraft) root.ntSaveDraft();
    return ok;
  }

  function val(id) { var e = document.getElementById(id); return e ? e.value.trim() : ''; }
  function txt(id) { var e = document.getElementById(id); return e ? e.textContent.trim() : ''; }

  function prefersReduced() {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  // ── Ixtiyoriy bo'limlar: Qo'shish -> ikki tilli muharrir -> O'chirish ──
  //
  // DALIL — Uzum portalining O'Z kodi (chunk-3ca9d890, editProductCard/Comments):
  //   addComment(tur)         -> {comment:{ru:"",uz:""}, commentType:tur} qo'shadi,
  //                              agar shu turdagi comment ALLAQACHON bo'lsa — hech nima
  //   removeComment(tur, til) -> faqat o'sha tilni o'chiradi; ikkala til ketsa,
  //                              comment butunlay splice qilinadi -> «Qo'shish» qaytadi
  // commentType kalitlari HTML'da (data-nt-opt) — «Размеры»/«Сертификация» kabi
  // tuzoqli nomlar shu yerda bir marta yozilgan, JS ularni o'ylab topmaydi.
  var comments = {};   // {commentType: {uz: bool, ru: bool}} — MAVJUD tillar

  function optBox(ctype) {
    return root.querySelector('[data-nt-opt="' + ctype + '"]');
  }

  function optBlock(box, code) {
    return box.querySelector('.nt-optsec-block[data-opt-lang="' + code + '"]');
  }

  function renderOpt(ctype) {
    var box = optBox(ctype);
    var addWrap = root.querySelector('[data-opt-add="' + ctype + '"]');
    if (!box || !addWrap) return;
    var st = comments[ctype];
    box.hidden = !st;
    addWrap.hidden = !!st;          // «Qo'shish» faqat comment YO'Q bo'lganda
    ['uz', 'ru'].forEach(function (code) {
      var b = optBlock(box, code);
      if (b) b.hidden = !(st && st[code]);
    });
  }

  function addComment(ctype) {
    if (comments[ctype]) return;    // Uzum: mavjudini qayta qo'shmaydi
    comments[ctype] = { uz: true, ru: true };
    renderOpt(ctype);
    var box = optBox(ctype);
    var first = box && box.querySelector('.nt-editor-area');
    if (first) first.focus();
  }

  function removeComment(ctype, code) {
    var st = comments[ctype];
    if (!st) return;
    st[code] = false;
    var box = optBox(ctype);
    var blk = box && optBlock(box, code);
    var area = blk && blk.querySelector('.nt-editor-area');
    if (area) area.textContent = '';           // o'chirilgan tilning matni ham ketadi
    if (!st.uz && !st.ru) delete comments[ctype];   // ikkalasi ketsa — butunlay
    renderOpt(ctype);
  }

  function collectComments() {
    // Uzum `fillEmptyLocaleComments`: yo'q locale "" bilan to'ldiriladi.
    var out = [];
    Object.keys(comments).forEach(function (ctype) {
      var box = optBox(ctype);
      if (!box) return;
      var c = { uz: '', ru: '' };
      ['uz', 'ru'].forEach(function (code) {
        if (!comments[ctype][code]) return;
        var blk = optBlock(box, code);
        var area = blk && blk.querySelector('.nt-editor-area');
        if (area) c[code] = area.innerHTML.trim();
      });
      if (c.uz || c.ru) out.push({ commentType: ctype, comment: c });
    });
    return out;
  }

  root.querySelectorAll('[data-nt-optional]').forEach(function (btn) {
    btn.addEventListener('click', function () { addComment(btn.dataset.ntOptional); });
  });

  root.querySelectorAll('.nt-optsec').forEach(function (box) {
    var ctype = box.dataset.ntOpt;
    box.querySelectorAll('[data-opt-del]').forEach(function (b) {
      b.addEventListener('click', function () { removeComment(ctype, b.dataset.optDel); });
    });
    renderOpt(ctype);
  });

  // ── Filtrlar: Бренд / Модель / Страна производства ───────────────
  //
  // DALIL (bandl, edit-product-card-filters.ce): har filtr O'Z KARTASI,
  // `v-for` API TARTIBIDA; ichida qidiruvli+sahifalangan tanlagich va
  // «Отсутствует <nom>» katakchasi (belgilansa tanlagich o'chadi va qiymat
  // sifatida filtr `emptyValue.id` ketadi).
  // Filtr ID'lari (bandl enum): BRAND=6, MODEL=7, COUNTRY=8.
  var FILTER_BRAND = 6, FILTER_MODEL = 7, FILTER_COUNTRY = 8;
  var filtersEl = document.getElementById('ntFilters');

  function filterPlaceholder(id) {
    if (id === FILTER_BRAND) return tr('Brendni tanlang', 'Выберите бренд');
    if (id === FILTER_MODEL) return tr('Modelni tanlang', 'Выберите модель');
    if (id === FILTER_COUNTRY) return tr('Mamlakatni tanlang', 'Выберите страну');
    return '';
  }

  function fetchFilterValues(filterId, search, page) {
    return fetch('/noviy-tavar/api/filter-values?shop=' + encodeURIComponent(state.shop) +
                 '&filterId=' + encodeURIComponent(filterId) +
                 '&categoryId=' + encodeURIComponent(state.categoryId) +
                 '&search=' + encodeURIComponent(search || '') +
                 '&page=' + encodeURIComponent(page || 0),
                 { credentials: 'same-origin' })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function (d) { return { items: (d && d.values) || [] }; })
      // ⚠️ Xato JIM YUTILMAYDI — bo'sh ro'yxat «brend yo'q» degan yolg'on xabar bo'lardi.
      .catch(function () { return { items: [], error: true }; });
  }

  function buildFilters(meta) {
    if (!filtersEl) return;
    filtersEl.textContent = '';
    state.filterValues = {};   // {filterId: valueId} -> create body: filterValues[]
    var list = (meta && meta.filters) || [];
    if (!Array.isArray(list)) return;
    list.forEach(function (f) { filtersEl.appendChild(filterCard(f)); });
  }

  function filterCard(f) {
    // ⚠️ Bandl brend sarlavhasini i18n'dan QAYTA YOZADI va buni katakcha
    // yorlig'i hisoblanishidan OLDIN qiladi (`o && (e.title = o)`), ya'ni
    // katakcha ham shu nomni ishlatadi. Faqat sarlavhani almashtirsak,
    // API'ning uz tarjimasi chiqib qolardi: karta «Brend», katakcha esa
    // «tovar belgisi mavjud emas» — jonli kadr aynan shu xatoni ko'rsatdi.
    if (f.id === FILTER_BRAND) {
      f = Object.assign({}, f, { title: tr('Brend', 'Бренд') });
    }

    var sec = document.createElement('section');
    sec.className = 'nt-card';
    sec.setAttribute('data-nt-filter', String(f.id));

    var h = document.createElement('h2');
    h.className = 'nt-card-title';
    h.textContent = f.title || '';
    if (f.required) {
      var req = document.createElement('span');
      req.className = 'nt-req';
      req.setAttribute('aria-hidden', 'true');
      req.textContent = '*';
      h.appendChild(document.createTextNode(' '));
      h.appendChild(req);
    }
    sec.appendChild(h);

    // Qidiruvli tanlagich
    var combo = document.createElement('div');
    combo.className = 'nt-select-wrap nt-combo';
    var input = document.createElement('input');
    input.className = 'nt-input';
    input.type = 'text';
    input.autocomplete = 'off';
    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-expanded', 'false');
    input.placeholder = filterPlaceholder(f.id);
    input.setAttribute('aria-label', h.textContent);
    var panel = document.createElement('div');
    panel.className = 'nt-panel';
    panel.setAttribute('role', 'listbox');
    panel.hidden = true;
    combo.appendChild(input);
    combo.appendChild(panel);
    sec.appendChild(combo);

    var page = 0, query = '', loading = false, done = false, timer = null;

    function addItems(items) {
      items.forEach(function (v) {
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'nt-panel-item';
        b.setAttribute('role', 'option');
        b.textContent = v.title || v.value;
        b.addEventListener('click', function (e) {
          e.stopPropagation();
          state.filterValues[f.id] = v.id;
          input.value = v.title || v.value;
          panel.hidden = true;
          input.setAttribute('aria-expanded', 'false');
          validate();
        });
        panel.appendChild(b);
      });
    }

    function load(reset) {
      if (loading) return;
      loading = true;
      if (reset) { page = 0; done = false; panel.textContent = ''; }
      fetchFilterValues(f.id, query, page).then(function (res) {
        loading = false;
        if (res.error) {
          panel.textContent = '';
          var er = document.createElement('div');
          er.className = 'nt-panel-error';
          er.textContent = tr('Ro’yxatni olib bo’lmadi', 'Не удалось загрузить список');
          panel.appendChild(er);
          return;
        }
        if (!res.items.length && page === 0) {
          var em = document.createElement('div');
          em.className = 'nt-panel-empty';
          em.textContent = tr('Topilmadi', 'Ничего не найдено');
          panel.appendChild(em);
          return;
        }
        if (res.items.length < 24) done = true;   // sahifa hajmi 24 (HAR)
        addItems(res.items);
        page += 1;
      });
    }

    input.addEventListener('focus', function () {
      panel.hidden = false;
      input.setAttribute('aria-expanded', 'true');
      if (!panel.children.length) load(true);
    });
    input.addEventListener('input', function () {
      query = input.value.trim();
      state.filterValues[f.id] = null;   // terildi -> tanlov bekor
      clearTimeout(timer);
      timer = setTimeout(function () { load(true); }, 250);   // debounce
      validate();
    });
    // Sahifalash: pastga yetganda keyingi sahifa (bandl: loadMore)
    panel.addEventListener('scroll', function () {
      if (done || loading) return;
      if (panel.scrollTop + panel.clientHeight >= panel.scrollHeight - 8) load(false);
    });

    // «Отсутствует <nom>» — bandl: label = filter_missing + " " + title.toLowerCase()
    if (f.emptyValue && f.emptyValue.id != null) {
      var lab = document.createElement('label');
      lab.className = 'nt-check';
      var cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.id = 'filter-' + f.id + '-emptyValue';
      var name = (f.title || '').toLowerCase();
      // ⚠️ Uzum uz-lokalida shu formula «Mavjud emas brend» beradi (grammatik
      // xato). Biz o'zbekchada tabiiy tartibni qo'yamiz; ruschada Uzum bilan
      // aynan bir xil.
      lab.appendChild(cb);
      lab.appendChild(document.createTextNode(
        ' ' + tr(name + ' mavjud emas', 'Отсутствует ' + name)));
      cb.addEventListener('change', function () {
        input.disabled = cb.checked;
        if (cb.checked) {
          state.filterValues[f.id] = f.emptyValue.id;
          input.value = '';
          panel.hidden = true;
        } else if (state.filterValues[f.id] === f.emptyValue.id) {
          state.filterValues[f.id] = null;
        }
        validate();
      });
      sec.appendChild(lab);
    }

    return sec;
  }

  // ── Umumiy rasmlar: yuklash /noviy-tavar/api/upload-image orqali ──
  //
  // DALIL (Uzum bandli): karta sharti `requiredMediaType !== NOT_DEFINED`,
  // yuklagich chegarasi `max-size: 5` (MB), `upload-type: "multipart"`.
  // Token brauzerga CHIQMAYDI — fayl Flask proxy'siga ketadi, u Uzum'ga uzatadi.
  var MAX_PHOTO_BYTES = 5 * 1024 * 1024;   // bandl: max-size 5
  var photosEl = document.getElementById('ntPhotos');
  var photosHint = document.getElementById('ntPhotosHint');
  var photoInput = document.getElementById('ntPhotoInput');
  var photoMsg = document.getElementById('ntPhotoMsg');
  document.addEventListener('click', function () { closePhotoMenus(); });

  function photoNote(msg) {
    if (!photoMsg) return;
    photoMsg.textContent = msg || '';
    photoMsg.hidden = !msg;
  }

  // Sudrab tartiblash uchun boshlang'ich indeks
  var dragFrom = -1;

  function closePhotoMenus() {
    photosEl.querySelectorAll('.nt-photo-actions').forEach(function (m) {
      m.hidden = true;
      var b = m.previousElementSibling;
      if (b) b.setAttribute('aria-expanded', 'false');
    });
  }

  function renderPhotos() {
    if (!photosEl) return;
    photosEl.textContent = '';

    // Referens (foydalanuvchi kadri + bandl): matn faqat rasm BOR bo'lganda.
    if (photosHint) photosHint.hidden = !state.images.length;

    state.images.forEach(function (im, i) {
      var fig = document.createElement('div');
      fig.className = 'nt-photo';
      fig.draggable = true;
      fig.addEventListener('dragstart', function () { dragFrom = i; });
      fig.addEventListener('dragover', function (e) { e.preventDefault(); });
      fig.addEventListener('drop', function (e) {
        e.preventDefault();
        if (dragFrom < 0 || dragFrom === i) return;
        var moved = state.images.splice(dragFrom, 1)[0];
        state.images.splice(i, 0, moved);
        dragFrom = -1;
        renderPhotos();
      });

      var img = document.createElement('img');
      img.src = im.url;
      img.alt = tr('Tovar rasmi ', 'Фото товара ') + (i + 1);
      fig.appendChild(img);

      // Tartib raqami — referens kadrda chap yuqorida
      var num = document.createElement('span');
      num.className = 'nt-photo-num';
      num.textContent = String(i + 1);
      fig.appendChild(num);

      // ⋮ menyusi. Bandl: [Удалить, Скрыть, Вернуть видимость] — oxirgi ikkitasi
      // `can-hide` (hasMainUploadedStudio) ga bog'liq, yangi tovarda u false,
      // shuning uchun yaratish oqimida faqat «O'chirish» qoladi.
      var menuBtn = document.createElement('button');
      menuBtn.type = 'button';
      menuBtn.className = 'nt-photo-menu';
      menuBtn.setAttribute('aria-haspopup', 'menu');
      menuBtn.setAttribute('aria-expanded', 'false');
      menuBtn.setAttribute('aria-label', tr('Rasm amallari', 'Действия с фото'));
      menuBtn.textContent = '⋮';
      fig.appendChild(menuBtn);

      var menu = document.createElement('div');
      menu.className = 'nt-photo-actions';
      menu.setAttribute('role', 'menu');
      menu.hidden = true;

      var del = document.createElement('button');
      del.type = 'button';
      del.setAttribute('role', 'menuitem');
      del.textContent = tr('O’chirish', 'Удалить');
      del.addEventListener('click', function () {
        state.images.splice(i, 1);
        renderPhotos();
        validate();
      });
      menu.appendChild(del);
      fig.appendChild(menu);

      menuBtn.addEventListener('click', function (e) {
        e.stopPropagation();
        var open = menu.hidden;
        closePhotoMenus();
        menu.hidden = !open;
        menuBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
      });

      photosEl.appendChild(fig);
    });

    var add = document.createElement('button');
    add.type = 'button';
    add.className = 'nt-tile';
    add.id = 'ntAddPhoto';
    add.innerHTML = '<span class="nt-plus" aria-hidden="true">+</span><span>' +
      tr('Rasm<br>qo’shish', 'Добавить<br>фото') + '</span>';
    add.addEventListener('click', function () { if (photoInput) photoInput.click(); });
    photosEl.appendChild(add);
  }

  function uploadPhoto(file) {
    if (file.size > MAX_PHOTO_BYTES) {
      photoNote(tr('«' + file.name + '» 5 Mb dan katta',
                   '«' + file.name + '» больше 5 Мб'));
      return Promise.resolve(null);
    }
    var fd = new FormData();
    fd.append('shop', state.shop || '');
    fd.append('file', file);
    return fetch('/noviy-tavar/api/upload-image',
                 { method: 'POST', body: fd, credentials: 'same-origin' })
      .then(function (r) {
        return r.json().then(function (d) { return { ok: r.ok, d: d }; });
      })
      .then(function (res) {
        // ⚠️ Xatoni JIM YUTMAYMIZ — aks holda foydalanuvchi rasm yuklandi deb o'ylaydi.
        if (!res.ok || !res.d || !res.d.key) {
          photoNote((res.d && res.d.error) ||
                    tr('Rasm yuklanmadi', 'Не удалось загрузить фото'));
          return null;
        }
        // Uzum sifat ogohlantirishi (masalan «1080×1440 dan kichik») — proxy
        // uni qaytaradi, biz KO'RSATAMIZ: aks holda moderatsiyada rad etiladi.
        return { key: res.d.key, url: res.d.url, recs: res.d.recommendations || [] };
      })
      .catch(function () {
        photoNote(tr('Tarmoq xatosi', 'Ошибка сети'));
        return null;
      });
  }

  if (photoInput) {
    photoInput.addEventListener('change', function () {
      var files = Array.prototype.slice.call(photoInput.files || []);
      if (!files.length) return;
      photoNote(tr('Yuklanmoqda...', 'Загрузка...'));
      Promise.all(files.map(uploadPhoto)).then(function (out) {
        var ok = out.filter(Boolean);
        state.images = state.images.concat(ok);
        if (ok.length === files.length) {
          // Hammasi yuklandi — endi Uzum tavsiyalari bo'lsa o'shani ko'rsatamiz.
          var recs = [];
          ok.forEach(function (im) { recs = recs.concat(im.recs || []); });
          photoNote(recs.length ? recs.join(' ') : '');
        }
        renderPhotos();
        validate();
      });
      photoInput.value = '';   // bir xil faylni qayta tanlash ishlasin
    });
    renderPhotos();
  }

  // ── Har bir rang uchun mediafayllar (colorImages) ───────────────
  //
  // DARVOZA (bandl ota-render @498913):  productColors && requiredMediaType===ANY
  // productColors (store @43526):
  //     We(e) => Object.values(e.characteristicTitle).includes("Цвет")
  //     s = () => selectedCharacteristics.find(We)?.characteristicValues
  // Ya'ni «Цвет» sarlavhali xususiyat qatoridagi TANLANGAN qiymatlar.
  // ⚠️ «Цвет» so'zma-so'z solishtiriladi (bandlda ham shunday) — id emas.
  //
  // Yuklagich chegaralari bandldan: `max-size: 5` (MB), `max-count: 10`
  // (maxColorImages prop default), `upload-type: "multipart"`.
  var MAX_COLOR_IMAGES = 10;
  var colorMediaCard = document.getElementById('ntCardColorMedia');
  var colorMediaEl = document.getElementById('ntColorMedia');
  var colorInput = document.getElementById('ntColorInput');
  var colorVideoInput = document.getElementById('ntColorVideoInput');
  var color360Input = document.getElementById('ntColor360Input');
  var colorMsg = document.getElementById('ntColorMsg');
  var colorUploadKey = null;   // qaysi rangga yuklanmoqda (title obyekti)
  var colorDragFrom = -1;      // sudrab tartiblash boshlang'ich indeksi

  function isColorRow(row) {
    var o = row.opt || {};
    return o.uz === 'Цвет' || o.ru === 'Цвет';
  }

  // Tanlangan rang qiymatlari yoki null (bandl: productColors)
  function productColors() {
    var row = state.rows.filter(isColorRow)[0];
    return row ? row.selected : null;
  }

  // Rang identifikatori — bandl `cb(title)` ni ham KALIT, ham KO'RSATILADIGAN
  // matn sifatida ishlatadi. Bizda `rowLabel` aynan «uz / ru» beradi va kadr
  // 009 dagi chip ham «Alvon / Алый» ko'rinishida — shuning uchun ikkisiga ham
  // shu ishlatiladi. (Bu XULOSA: `cb` ning minifikatsiyalangan ta'rifi
  // ochilmadi, lekin u bir vaqtda kalit ham, sarlavha ham bo'lishi shart.)
  function colorKey(v) { return rowLabel(v); }

  function colorNote(msg) {
    if (!colorMsg) return;
    colorMsg.textContent = msg || '';
    colorMsg.hidden = !msg;
  }

  function imagesOfColor(v) {
    var k = colorKey(v);
    return state.colorImages.filter(function (im) { return colorKey(im.color) === k; });
  }

  function syncColorMedia() {
    if (!colorMediaCard) return;
    var colors = productColors();
    var on = !!colors && requiredMediaType() === 'ANY';
    colorMediaCard.hidden = !on;
    // Rang qatori olib tashlansa yoki qiymat yechilsa — o'sha rangning
    // rasmlari ham tushib qolsin (bandl `resetColorImages()` shuni qiladi).
    if (!on) {
      state.colorImages = [];
      state.colorVideos = [];
      state.colorCollections = [];
    } else {
      var keys = colors.map(colorKey);
      state.colorImages = state.colorImages.filter(function (im) {
        return keys.indexOf(colorKey(im.color)) >= 0;
      });
      // Video/360 ham rasm bilan bir xil qoidaga bo'ysunadi — rang yechilsa
      // uning MEDIASI ham ketadi (bandl `resetColorImages()` semantikasi).
      state.colorVideos = state.colorVideos.filter(function (m) {
        return keys.indexOf(colorKey(m.color)) >= 0;
      });
      state.colorCollections = state.colorCollections.filter(function (m) {
        return keys.indexOf(colorKey(m.color)) >= 0;
      });
    }
    renderColorMedia();
  }

  function renderColorMedia() {
    if (!colorMediaEl) return;
    colorMediaEl.textContent = '';
    var colors = productColors() || [];

    colors.forEach(function (v) {
      var block = document.createElement('div');
      block.className = 'nt-cm-block';

      var head = document.createElement('div');
      head.className = 'nt-cm-color';
      var sw = document.createElement('span');
      sw.className = 'nt-cm-swatch';
      // Rang HEX'i xususiyat qiymatining `value` maydonida (masalan «#F1371F»)
      if (isHex(v.value)) sw.style.background = v.value;
      head.appendChild(sw);
      var title = document.createElement('span');
      title.className = 'nt-cm-title';
      title.textContent = colorKey(v);
      head.appendChild(title);
      block.appendChild(head);

      var mine = imagesOfColor(v);

      // Bandl: sudrash matni faqat shu rangda rasm bo'lsa
      if (mine.length) {
        var drag = document.createElement('p');
        drag.className = 'nt-cm-drag';
        drag.textContent = tr(
          'Fotosuratlar tartibini oʻzgartirish uchun ularni kerakli joyga tortib qoʻying',
          'Вы можете легко изменять порядок фотографий, перетаскивая их в нужное место');
        block.appendChild(drag);
      }

      var view = document.createElement('div');
      view.className = 'nt-cm-view';

      mine.forEach(function (im, i) {
        var fig = document.createElement('div');
        fig.className = 'nt-photo';
        // Sudrab tartiblash — yuqoridagi matn shuni va'da qiladi, demak
        // HAQIQATAN ishlashi shart (bandl: uploader `onSortImage`).
        fig.draggable = true;
        fig.addEventListener('dragstart', function () { colorDragFrom = i; });
        fig.addEventListener('dragover', function (e) { e.preventDefault(); });
        fig.addEventListener('drop', function (e) {
          e.preventDefault();
          moveColorImage(v, colorDragFrom, i);
          colorDragFrom = -1;
          renderColorMedia();
        });

        var img = document.createElement('img');
        img.src = im.url;
        img.alt = colorKey(v);
        fig.appendChild(img);

        var del = document.createElement('button');
        del.type = 'button';
        del.className = 'nt-photo-menu';
        del.setAttribute('aria-label', tr('Rasmni o’chirish', 'Удалить фото'));
        del.textContent = '×';
        del.addEventListener('click', function () {
          var i = state.colorImages.indexOf(im);
          if (i >= 0) state.colorImages.splice(i, 1);
          reorderColor(v);
          renderColorMedia();
          validate();
        });
        fig.appendChild(del);
        view.appendChild(fig);
      });

      // Bandl: `max-count` ga yetganda yuklash plitkasi ko'rsatilmaydi
      if (mine.length < MAX_COLOR_IMAGES) {
        var add = document.createElement('button');
        add.type = 'button';
        add.className = 'nt-tile';
        add.innerHTML = '<span class="nt-plus" aria-hidden="true">+</span><span>' +
          tr('Rasm<br>qo’shish', 'Добавить<br>фото') + '</span>';
        add.addEventListener('click', function () {
          colorUploadKey = v;
          if (colorInput) colorInput.click();
        });
        view.appendChild(add);
      }

      block.appendChild(view);

      // Per-rang video va 360 — kartaning maslahatlari ikkalasini ham va'da
      // qiladi, demak ikkalasi ham ISHLASHI shart (dars: va'da qilgan matn
      // implement qilinmagan bo'lsa — ko'rsatma).
      block.appendChild(colorMediaSlot(v, 'video'));
      block.appendChild(colorMediaSlot(v, '360'));

      colorMediaEl.appendChild(block);
    });
  }

  // Bitta rang uchun video yoki 360 uyasi. Uzum store'ida ikkalasi ham
  // rang boshiga BITTA (colorVideosChange/colorCollectionsChange `findIndex`
  // bilan almashtiradi, ro'yxatga qo'shmaydi).
  function colorMediaSlot(v, kind) {
    var isVideo = kind === 'video';
    var list = isVideo ? state.colorVideos : state.colorCollections;
    var k = colorKey(v);
    var cur = list.filter(function (m) { return colorKey(m.color) === k; })[0];

    var wrap = document.createElement('div');
    wrap.className = 'nt-cm-slot';

    var lbl = document.createElement('p');
    lbl.className = 'nt-cm-slot-h';
    lbl.textContent = isVideo ? tr('Video', 'Видео') : tr('Foto 360', 'Фото 360');
    wrap.appendChild(lbl);

    var view = document.createElement('div');
    view.className = 'nt-cm-view';

    if (cur) {
      var fig = document.createElement('div');
      fig.className = 'nt-photo' + (isVideo ? ' nt-photo--video' : '');
      if (isVideo) {
        var vid = document.createElement('video');
        vid.src = cur.videoUrl;
        vid.controls = true;
        vid.preload = 'metadata';
        fig.appendChild(vid);
      } else {
        var img = document.createElement('img');
        img.src = cur.previewUrl;
        img.alt = k;
        fig.appendChild(img);
      }
      var del = document.createElement('button');
      del.type = 'button';
      del.className = 'nt-photo-menu';
      del.setAttribute('aria-label', tr('O‘chirish', 'Удалить'));
      del.textContent = '×';
      del.addEventListener('click', function () {
        var i = list.indexOf(cur);
        if (i >= 0) list.splice(i, 1);
        renderColorMedia();
      });
      fig.appendChild(del);
      view.appendChild(fig);
    } else {
      var add = document.createElement('button');
      add.type = 'button';
      add.className = 'nt-tile';
      add.innerHTML = '<span class="nt-plus" aria-hidden="true">+</span><span>' +
        (isVideo ? tr('Video<br>qo‘shish', 'Добавить<br>видео')
                 : tr('Foto 360<br>qo‘shish', 'Добавить<br>фото 360')) + '</span>';
      add.addEventListener('click', function () {
        colorUploadKey = v;
        var inp = isVideo ? colorVideoInput : color360Input;
        if (inp) inp.click();
      });
      view.appendChild(add);
    }

    wrap.appendChild(view);
    return wrap;
  }

  // Bandl `j()`: bitta rang ichida ordering 0..n-1 qilib qayta raqamlanadi
  function reorderColor(v) {
    imagesOfColor(v).forEach(function (im, i) { im.ordering = i; });
  }

  // Bitta rang ichida rasmni `from` dan `to` ga ko'chirish. state.colorImages
  // aralash ro'yxat (hamma ranglar) — shu rangga TEGISHLI pozitsiyalarni topib,
  // ularni yangi tartib bilan qayta to'ldiramiz; boshqa ranglar joyida qoladi.
  function moveColorImage(v, from, to) {
    var mine = imagesOfColor(v);
    if (from < 0 || to < 0 || from === to ||
        from >= mine.length || to >= mine.length) return;
    mine.splice(to, 0, mine.splice(from, 1)[0]);

    var k = colorKey(v);
    var slots = [];
    state.colorImages.forEach(function (im, idx) {
      if (colorKey(im.color) === k) slots.push(idx);
    });
    slots.forEach(function (idx, n) { state.colorImages[idx] = mine[n]; });
    reorderColor(v);
  }

  if (colorInput) {
    colorInput.addEventListener('change', function () {
      var files = Array.prototype.slice.call(colorInput.files || []);
      colorInput.value = '';
      var v = colorUploadKey;
      if (!files.length || !v) return;

      // ⚠️ Hajm tekshiruvi SHU YERDA: `uploadPhoto` o'z xatosini UMUMIY rasmlar
      // kartasining satriga (photoNote) yozadi — rang-media uchun u NOTO'G'RI
      // karta bo'lardi. Chegara bandldan: `max-size: 5` (MB).
      var warn = [];
      var big = files.filter(function (f) { return f.size > MAX_PHOTO_BYTES; });
      files = files.filter(function (f) { return f.size <= MAX_PHOTO_BYTES; });
      if (big.length) {
        warn.push(tr('«' + big[0].name + '» 5 Mb dan katta',
                     '«' + big[0].name + '» больше 5 Мб'));
      }

      var room = MAX_COLOR_IMAGES - imagesOfColor(v).length;
      if (files.length > room) {
        warn.push(tr('Har rang uchun maksimum ' + MAX_COLOR_IMAGES + ' ta rasm',
                     'Максимум ' + MAX_COLOR_IMAGES + ' фото на цвет'));
        files = files.slice(0, room);
      }
      if (!files.length) { colorNote(warn.join(' ')); return; }

      colorNote(tr('Yuklanmoqda...', 'Загрузка...'));
      Promise.all(files.map(uploadPhoto)).then(function (out) {
        var ok = out.filter(Boolean);
        // ⚠️ Xato JIM YUTILMAYDI — ogohlantirishlar saqlanadi, «Yuklanmoqda»
        // ularni bosib ketmasin.
        if (ok.length !== files.length) {
          warn.push(tr('Ba’zi rasmlar yuklanmadi', 'Некоторые фото не загрузились'));
        }
        if (!ok.length) { colorNote(warn.join(' ')); return; }
        colorNote(warn.join(' '));
        ok.forEach(function (im) {
          state.colorImages.push({
            key: im.key,
            url: im.url,
            color: { uz: v.uz, ru: v.ru },
            ordering: 0
          });
        });
        reorderColor(v);
        renderColorMedia();
        validate();
      });
    });
  }

  // Per-rang video / 360. ⚠️ Hajm xatosi SHU kartaning satriga (colorNote)
  // yoziladi — umumiy kartanikiga EMAS (o'tgan sessiyada aynan shu xato bo'lgan).
  if (colorVideoInput) {
    colorVideoInput.addEventListener('change', function () {
      var file = (colorVideoInput.files || [])[0];
      colorVideoInput.value = '';
      var v = colorUploadKey;
      if (!file || !v) return;
      if (file.size > MAX_VIDEO_BYTES) {
        colorNote(tr('«' + file.name + '» 10 Mb dan katta',
                     '«' + file.name + '» больше 10 Мб'));
        return;
      }
      colorNote(tr('Yuklanmoqda...', 'Загрузка...'));
      uploadVideoFile(file).then(function (out) {
        if (out.err) { colorNote(out.err); return; }
        colorNote('');
        // Rang boshiga BITTA — eskisini almashtiramiz (bandl `findIndex`).
        var k = colorKey(v);
        state.colorVideos = state.colorVideos.filter(function (m) {
          return colorKey(m.color) !== k;
        });
        state.colorVideos.push({
          color: { uz: v.uz, ru: v.ru },
          videoKey: out.key,
          videoUrl: out.url
        });
        renderColorMedia();
      });
    });
  }

  if (color360Input) {
    color360Input.addEventListener('change', function () {
      var file = (color360Input.files || [])[0];
      color360Input.value = '';
      var v = colorUploadKey;
      if (!file || !v) return;
      if (file.size > MAX_360_BYTES) {
        colorNote(tr('«' + file.name + '» 10 Mb dan katta',
                     '«' + file.name + '» больше 10 Мб'));
        return;
      }
      colorNote(tr('Yuklanmoqda...', 'Загрузка...'));
      upload360File(file).then(function (out) {
        if (out.err) { colorNote(out.err); return; }
        colorNote('');
        var k = colorKey(v);
        state.colorCollections = state.colorCollections.filter(function (m) {
          return colorKey(m.color) !== k;
        });
        state.colorCollections.push({
          color: { uz: v.uz, ru: v.ru },
          collectionId: out.collectionId,
          previewUrl: out.previewUrl
        });
        renderColorMedia();
      });
    });
  }

  // ── Sertifikatlar formasi (productCertificates) ─────────────────
  //
  // DALIL — Uzum store'i `editProductCard/Certificates` (chunk-6dbbb9d8 @17150),
  // so'zma-so'z ko'chirilgan semantika:
  //   addCertificate()          -> push({expirationDate:"", number:"", certificateImages:[]})
  //   updateImages(cert, imgs)  -> cert.certificateImages = imgs.map((im,i) =>
  //        ({deletable:true, url:im.toString(), ordering:i, key:im.key, status:"ACTIVE"}))
  //   removeCertificate(i)      -> splice(i,1)
  //   validate()                -> quyidagi certificatesValid() ga qara
  // Karta va tugma ko'rinishi `fillType` ga bog'liq — u meta.certification dan
  // keladi (GET product-certification-filltype, javob O'RAMSIZ: {"fillType":...}).
  //
  // ⚠️ ZIDLIK MEROSI (HANDOFF_2026_07_17 §2, foydalanuvchi «keyin» dedi):
  // yuklash proxy'si `url` sifatida `originalUrl` qaytaradi, haqiqiy 24 ta
  // tanada esa `t_product_540_high.jpg`. §2 tuzatilsa — umumiy rasm ham,
  // sertifikat rasmi ham birdan to'g'rilanadi (ikkalasi bir proxy'dan oladi).
  var MAX_CERT_BYTES = 5 * 1024 * 1024;   // karta maslahati: «Не больше 5 Мб»
  var certListEl = document.getElementById('ntCertList');
  var certAddBtn = document.getElementById('ntCertAdd');
  var certInput = document.getElementById('ntCertInput');
  var certMsg = document.getElementById('ntCertMsg');
  var certReq = document.getElementById('ntCertReq');
  var certBanner = document.getElementById('ntCertBanner');
  var certUploadIdx = -1;   // qaysi sertifikatga yuklanmoqda
  var certDelIdx = null;    // o'chirish tasdig'i kutayotgan indeks

  function certFillType() {
    var c = state.meta && state.meta.certification;
    return (c && c.fillType) || null;
  }

  // Bandl `validate()` ning AYNAN o'zi:
  //   OPTIONAL && 0 ta            -> valid
  //   REQUIRED && 0 ta            -> INVALID
  //   aks holda: har sertifikatda number && rasm && expirationDate bo'lsin
  // Kategoriya tanlanmaganda fillType=null -> bo'sh ro'yxat valid (Uzum ham shunday).
  function certificatesValid() {
    var list = state.certificates;
    var ft = certFillType();
    if (ft === 'OPTIONAL' && !list.length) return true;
    if (ft === 'REQUIRED' && !list.length) return false;
    return list.every(function (c) {
      return !!c.number && c.certificateImages.length > 0 && !!c.expirationDate;
    });
  }

  function certNote(msg) {
    if (!certMsg) return;
    certMsg.textContent = msg || '';
    certMsg.hidden = !msg;
  }

  function certBannerDismissed() {
    try { return localStorage.getItem('nt.certHintHidden') === '1'; } catch (e) { return false; }
  }

  // Bandl: sarlavha `*` <-> REQUIRED; banner <-> REQUIRED && ko'rinuvchi;
  // «Добавить» <-> fillType !== DISABLED (235 HAR'da DISABLED 0 marta).
  function syncCertGate() {
    var ft = certFillType();
    if (certReq) certReq.hidden = ft !== 'REQUIRED';
    if (certBanner) certBanner.hidden = !(ft === 'REQUIRED' && !certBannerDismissed());
    if (certAddBtn) certAddBtn.hidden = ft === 'DISABLED';
  }

  function certToday() {
    // Bandl sana tanlagichi: modelType "yyyy-MM-dd", minDate: new Date
    var d = new Date();
    var m = String(d.getMonth() + 1);
    var day = String(d.getDate());
    return d.getFullYear() + '-' + (m.length < 2 ? '0' + m : m) +
           '-' + (day.length < 2 ? '0' + day : day);
  }

  function certField(labelText, node) {
    var wrap = document.createElement('div');
    wrap.className = 'nt-cert-field';
    var lab = document.createElement('label');
    lab.className = 'nt-label';
    lab.textContent = labelText;
    var req = document.createElement('span');
    req.className = 'nt-req';
    req.setAttribute('aria-hidden', 'true');
    req.textContent = '*';
    lab.appendChild(req);
    lab.setAttribute('for', node.id);
    wrap.appendChild(lab);
    wrap.appendChild(node);
    return wrap;
  }

  function certItem(cert, idx) {
    var box = document.createElement('div');
    box.className = 'nt-cert';
    box.id = 'certificate-' + idx;   // bandl: id="certificate-{index}"

    // Bandl: × faqat `fillType===DISABLED && certificates.length>0` bo'lsa YASHIRINADI
    if (!(certFillType() === 'DISABLED' && state.certificates.length > 0)) {
      var x = document.createElement('button');
      x.type = 'button';
      x.className = 'nt-cert-x';
      x.setAttribute('aria-label', tr('O’chirish', 'Удалить'));
      x.textContent = '×';
      x.addEventListener('click', function () { openCertDelete(idx); });
      box.appendChild(x);
    }

    // Bandl: `certificates.item` + " " + (index+1)
    var h = document.createElement('h4');
    h.className = 'nt-cert-title';
    h.textContent = tr('Sertifikat', 'Сертификат') + ' ' + (idx + 1);
    box.appendChild(h);

    var grid = document.createElement('div');
    grid.className = 'nt-cert-photos';
    cert.certificateImages.forEach(function (im, i) {
      var fig = document.createElement('div');
      fig.className = 'nt-photo';
      var img = document.createElement('img');
      img.src = im.url;
      img.alt = tr('Sertifikat rasmi ', 'Фото сертификата ') + (i + 1);
      fig.appendChild(img);

      var del = document.createElement('button');
      del.type = 'button';
      del.className = 'nt-photo-menu';
      del.setAttribute('aria-label', tr('Rasmni o’chirish', 'Удалить фото'));
      del.textContent = '×';
      del.addEventListener('click', function () {
        cert.certificateImages.splice(i, 1);
        // ordering qayta hisoblanadi — bandl updateImages() ham shunday qiladi
        cert.certificateImages.forEach(function (c, n) { c.ordering = n; });
        renderCerts();
        validate();
      });
      fig.appendChild(del);
      grid.appendChild(fig);
    });

    var addPhoto = document.createElement('button');
    addPhoto.type = 'button';
    addPhoto.className = 'nt-tile';
    addPhoto.innerHTML = '<span class="nt-plus" aria-hidden="true">+</span><span>' +
      tr('Rasm<br>qo’shish', 'Добавить<br>фото') + '</span>';
    addPhoto.addEventListener('click', function () {
      certUploadIdx = idx;
      if (certInput) certInput.click();
    });
    grid.appendChild(addPhoto);
    box.appendChild(grid);

    var fields = document.createElement('div');
    fields.className = 'nt-cert-fields';

    var num = document.createElement('input');
    num.type = 'text';
    num.className = 'nt-input';
    num.id = 'ntCertNum' + idx;
    num.value = cert.number || '';
    num.addEventListener('input', function () {
      cert.number = num.value.trim();
      validate();
    });
    fields.appendChild(certField(tr('Sertifikat raqami', 'Номер сертификата'), num));

    var exp = document.createElement('input');
    exp.type = 'date';
    exp.className = 'nt-input';
    exp.id = 'ntCertExp' + idx;
    exp.min = certToday();          // bandl: minDate: new Date
    exp.value = cert.expirationDate || '';
    exp.addEventListener('change', function () {
      cert.expirationDate = exp.value;   // native qiymat allaqachon yyyy-MM-dd
      validate();
    });
    fields.appendChild(certField(tr('Amal qilishining tugashi', 'Окончание действия'), exp));

    box.appendChild(fields);
    return box;
  }

  function renderCerts() {
    if (!certListEl) return;
    certListEl.textContent = '';
    state.certificates.forEach(function (c, i) {
      certListEl.appendChild(certItem(c, i));
    });
    syncCertGate();
  }

  if (certAddBtn) {
    certAddBtn.addEventListener('click', function () {
      state.certificates.push({ expirationDate: '', number: '', certificateImages: [] });
      renderCerts();
      validate();
    });
  }

  var certBannerX = document.getElementById('ntCertBannerX');
  if (certBannerX) {
    certBannerX.addEventListener('click', function () {
      // Bandl: yopilgach IS_CERTIFICATE_REQUIRED_VISIBLE=false saqlanadi.
      try { localStorage.setItem('nt.certHintHidden', '1'); } catch (e) {}
      syncCertGate();
    });
  }

  if (certInput) {
    certInput.addEventListener('change', function () {
      var file = (certInput.files || [])[0];
      certInput.value = '';
      var cert = state.certificates[certUploadIdx];
      if (!file || !cert) return;
      if (file.size > MAX_CERT_BYTES) {
        certNote(tr('«' + file.name + '» 5 Mb dan katta',
                    '«' + file.name + '» больше 5 Мб'));
        return;
      }
      certNote(tr('Yuklanmoqda...', 'Загрузка...'));
      uploadPhoto(file).then(function (im) {
        // ⚠️ Xato JIM YUTILMAYDI — uploadPhoto o'zi photoNote'ga yozadi, biz
        // sertifikat kartasining o'z satriga ham yozamiz.
        if (!im) {
          certNote(tr('Rasm yuklanmadi', 'Не удалось загрузить фото'));
          return;
        }
        certNote('');
        // Bandl updateImages(): ordering qayta raqamlanadi, status ACTIVE.
        cert.certificateImages.push({
          deletable: true,
          url: im.url,
          ordering: cert.certificateImages.length,
          key: im.key,
          status: 'ACTIVE'
        });
        renderCerts();
        validate();
      });
    });
  }

  // O'chirish tasdig'i (bandl: certificates.delete_dialog)
  var certDelModal = document.getElementById('ntCertDelModal');
  var certDelOk = document.getElementById('ntCertDelOk');
  var certDelCancel = document.getElementById('ntCertDelCancel');

  function openCertDelete(idx) {
    certDelIdx = idx;
    if (certDelModal) certDelModal.hidden = false;
    if (certDelOk) certDelOk.focus();
  }

  function closeCertDelete() {
    certDelIdx = null;
    if (certDelModal) certDelModal.hidden = true;
  }

  if (certDelCancel) certDelCancel.addEventListener('click', closeCertDelete);
  if (certDelOk) {
    certDelOk.addEventListener('click', function () {
      if (certDelIdx === null) return;
      state.certificates.splice(certDelIdx, 1);
      closeCertDelete();
      renderCerts();
      validate();
    });
  }
  if (certDelModal) {
    certDelModal.addEventListener('click', function (e) {
      if (e.target === certDelModal) closeCertDelete();
    });
  }
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && certDelModal && !certDelModal.hidden) closeCertDelete();
  });

  renderCerts();

  // ── Video va Foto 360 ───────────────────────────────────────────
  //
  // Video va 360 ning AUTH sxemasi bir-biriga TESKARI — lekin bu FAQAT
  // server tomonning ishi. Tafsilot va jonli probe dalili `client.py` da.
  // ⚠️ Bu yerga Uzum host nomlarini yoki kalit nomlarini YOZMA: brauzer JS'i
  // Uzum bilan to'g'ridan-to'g'ri gaplashmasligi kerak va testlar ularni JS
  // MANBASIDA qidiradi — izohdagi nomdan ham yiqiladi (ataylab shunday).
  //
  // Chegaralar bandldan O'LCHANGAN (MediaUploader.ce `K = 10`):
  // video 10 Mb, arxiv 10 Mb. ⚠️ Ekrandagi maslahat «3 Mb» deydi — Uzum'ning
  // O'ZIDA ham shunday (matn 3, tekshiruv 10). Referensga sodiq qolamiz.
  var MAX_VIDEO_BYTES = 10 * 1024 * 1024;
  var MAX_360_BYTES = 10 * 1024 * 1024;
  // Bandl: 360 status'ini 5000ms interval bilan poll qiladi.
  var POLL_MS = 5000;
  var POLL_TRIES = 24;   // ~2 daqiqa — keyin taslim bo'lamiz (jim osilib qolmaydi)

  function mediaNote(el, msg) {
    if (!el) return;
    el.textContent = msg || '';
    el.hidden = !msg;
  }

  // Bitta media faylni proxy'ga yuborish. `path` — /noviy-tavar/api/... .
  function postMedia(path, file) {
    var fd = new FormData();
    fd.append('shop', state.shop || '');
    fd.append('file', file);
    return fetch(path, { method: 'POST', body: fd, credentials: 'same-origin' })
      .then(function (r) {
        return r.json().then(function (d) { return { ok: r.ok, d: d || {} }; },
                             function () { return { ok: false, d: {} }; });
      });
  }

  function uploadVideoFile(file) {
    return postMedia('/noviy-tavar/api/upload-video', file).then(function (res) {
      // ⚠️ Xatoni JIM YUTMAYMIZ — aks holda foydalanuvchi yuklandi deb o'ylaydi.
      if (!res.ok || !res.d.key) {
        return { err: res.d.error || tr('Video yuklanmadi', 'Не удалось загрузить видео') };
      }
      return { key: res.d.key, url: res.d.url };
    }, function () {
      return { err: tr('Tarmoq xatosi', 'Ошибка сети') };
    });
  }

  // 360 ASINXRON: yuklash faqat collectionId beradi, kadrlar keyin tayyor
  // bo'ladi -> status'ni poll qilamiz (bandl `baseCollectionUploadHandler`
  // ham aynan shunday qiladi — komponent faqat yakuniy natijani ko'radi).
  function upload360File(file) {
    return postMedia('/noviy-tavar/api/upload-collection', file).then(function (res) {
      if (!res.ok || !res.d.collectionId) {
        return { err: res.d.error || tr('360 yuklanmadi', 'Не удалось загрузить фото 360') };
      }
      return poll360(res.d.collectionId, POLL_TRIES);
    }, function () {
      return { err: tr('Tarmoq xatosi', 'Ошибка сети') };
    });
  }

  function poll360(cid, left) {
    var url = '/noviy-tavar/api/collection-status?shop=' +
              encodeURIComponent(state.shop || '') + '&id=' + encodeURIComponent(cid);
    return fetch(url, { credentials: 'same-origin' })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d || {} }; }); })
      .then(function (res) {
        if (!res.ok) {
          return { err: res.d.error || tr('360 holati olinmadi', 'Не удалось получить статус 360') };
        }
        var imgs = res.d.images || [];
        // `process_job_status` tayyorlik belgisi (jonli probe: "succeeded").
        if (res.d.status === 'succeeded' && imgs.length) {
          return { collectionId: cid, previewUrl: imgs[0].url, frames: imgs.length };
        }
        if (res.d.status === 'failed') {
          return { err: tr('Uzum arxivni qayta ishlay olmadi',
                           'Uzum не смог обработать архив') };
        }
        if (left <= 1) {
          // ⚠️ Cheksiz kutmaymiz — jim osilib qolish xatodan battar.
          return { err: tr('360 tayyor bo‘lmadi — keyinroq urinib ko‘ring',
                           'Фото 360 не готово — попробуйте позже') };
        }
        return new Promise(function (ok) {
          setTimeout(function () { ok(poll360(cid, left - 1)); }, POLL_MS);
        });
      }, function () {
        return { err: tr('Tarmoq xatosi', 'Ошибка сети') };
      });
  }

  // ── Umumiy video kartasi ────────────────────────────────────────
  var videoView = document.getElementById('ntVideoView');
  var videoInput = document.getElementById('ntVideoInput');
  var videoMsg = document.getElementById('ntVideoMsg');

  function renderVideo() {
    if (!videoView) return;
    videoView.textContent = '';
    if (state.video) {
      var fig = document.createElement('div');
      fig.className = 'nt-photo nt-photo--video';
      var vid = document.createElement('video');
      vid.src = state.video.url;
      vid.controls = true;
      vid.preload = 'metadata';
      fig.appendChild(vid);
      var del = document.createElement('button');
      del.type = 'button';
      del.className = 'nt-photo-menu';
      del.setAttribute('aria-label', tr('Videoni o‘chirish', 'Удалить видео'));
      del.textContent = '×';
      del.addEventListener('click', function () {
        state.video = null;
        mediaNote(videoMsg, '');
        renderVideo();
      });
      fig.appendChild(del);
      videoView.appendChild(fig);
      return;
    }
    var add = document.createElement('button');
    add.type = 'button';
    add.className = 'nt-tile';
    add.id = 'ntAddVideo';
    add.innerHTML = '<span class="nt-plus" aria-hidden="true">+</span><span>' +
      tr('Video<br>qo‘shish', 'Добавить<br>видео') + '</span>';
    add.addEventListener('click', function () { if (videoInput) videoInput.click(); });
    videoView.appendChild(add);
  }

  if (videoInput) {
    videoInput.addEventListener('change', function () {
      var file = (videoInput.files || [])[0];
      videoInput.value = '';
      if (!file) return;
      if (file.size > MAX_VIDEO_BYTES) {
        mediaNote(videoMsg, tr('«' + file.name + '» 10 Mb dan katta',
                               '«' + file.name + '» больше 10 Мб'));
        return;
      }
      mediaNote(videoMsg, tr('Yuklanmoqda...', 'Загрузка...'));
      uploadVideoFile(file).then(function (out) {
        if (out.err) { mediaNote(videoMsg, out.err); return; }
        state.video = { key: out.key, url: out.url };
        mediaNote(videoMsg, '');
        renderVideo();
      });
    });
  }

  renderVideo();

  // ── Umumiy 360 kartasi ──────────────────────────────────────────
  var v360View = document.getElementById('nt360View');
  var v360Input = document.getElementById('nt360Input');
  var v360Msg = document.getElementById('nt360Msg');

  function render360() {
    if (!v360View) return;
    v360View.textContent = '';
    if (state.imageCollection) {
      var fig = document.createElement('div');
      fig.className = 'nt-photo';
      var img = document.createElement('img');
      // Uzum `collectionPreviewUrl` ni ALOHIDA saqlaydi — birinchi kadr.
      img.src = state.imageCollection.previewUrl;
      img.alt = tr('Foto 360', 'Фото 360');
      fig.appendChild(img);
      var del = document.createElement('button');
      del.type = 'button';
      del.className = 'nt-photo-menu';
      del.setAttribute('aria-label', tr('Foto 360 ni o‘chirish', 'Удалить фото 360'));
      del.textContent = '×';
      del.addEventListener('click', function () {
        state.imageCollection = null;
        mediaNote(v360Msg, '');
        render360();
      });
      fig.appendChild(del);
      v360View.appendChild(fig);
      return;
    }
    var add = document.createElement('button');
    add.type = 'button';
    add.className = 'nt-tile';
    add.id = 'ntAdd360';
    add.innerHTML = '<span class="nt-plus" aria-hidden="true">+</span><span>' +
      tr('Foto 360<br>qo‘shish', 'Добавить<br>фото 360') + '</span>';
    add.addEventListener('click', function () { if (v360Input) v360Input.click(); });
    v360View.appendChild(add);
  }

  if (v360Input) {
    v360Input.addEventListener('change', function () {
      var file = (v360Input.files || [])[0];
      v360Input.value = '';
      if (!file) return;
      if (file.size > MAX_360_BYTES) {
        mediaNote(v360Msg, tr('«' + file.name + '» 10 Mb dan katta',
                              '«' + file.name + '» больше 10 Мб'));
        return;
      }
      mediaNote(v360Msg, tr('Yuklanmoqda...', 'Загрузка...'));
      upload360File(file).then(function (out) {
        if (out.err) { mediaNote(v360Msg, out.err); return; }
        state.imageCollection = { collectionId: out.collectionId,
                                  previewUrl: out.previewUrl };
        mediaNote(v360Msg, '');
        render360();
      });
    });
  }

  render360();

  // ── Saqlash: QORALAMA yaratish ──────────────────────────────────
  //
  // createProduct QORALAMA yaratadi — moderatsiyaga O'ZI KETMAYDI (HAR'da
  // tasdiqlangan: skuList=[] bilan 201, karta kabinetda «to'ldirilmagan»
  // holatda turadi). Token brauzerda YO'Q — hamma narsa proxy orqali.
  var saveBtn = document.getElementById('ntSave');
  if (saveBtn) {
    saveBtn.addEventListener('click', function () {
      // Har qadamda bu tugma BOSHQA ish qiladi.
      if (state.step === 3) { s3Send(); return; }
      // 2-qadamda SKU/narxlarni saqlaydi.
      if (state.step === 2) { s2Send(); return; }
      if (!validate()) return;
      // Гарантия <6 bo'lsa — bandldagi kabi to'xtaymiz (warranty_min_months).
      if (!warrantyValid()) {
        var wc = document.getElementById('ntCardWarranty');
        if (wc && wc.scrollIntoView) wc.scrollIntoView({ behavior: 'smooth', block: 'center' });
        return;
      }
      // Majburiy xususiyat darvozasi — faqat SHU URINISHDA (Uzum kabi):
      // to'ldirilmagan bo'lsa qizil xato qutisini ko'rsatamiz va to'xtaymiz.
      if (!requiredCharsSelected() || !sizeRequirementMet()) {
        charReqAttempted = true;
        syncCharReq();
        var cc = document.getElementById('ntCardChars');
        if (cc && cc.scrollIntoView) cc.scrollIntoView({ behavior: 'smooth', block: 'center' });
        return;
      }
      var prev = saveBtn.textContent;
      saveBtn.disabled = true;
      saveBtn.textContent = tr('Saqlanmoqda...', 'Сохранение...');

      var fvals = [];
      Object.keys(state.filterValues).forEach(function (fid) {
        var v = state.filterValues[fid];
        if (v != null) fvals.push({ filterId: Number(fid), filterValueId: v });
      });

      var chars = state.rows.filter(function (r) { return r.selected.length; })
        .map(function (r) {
          return {
            characteristicId: r.id,
            characteristicTitle: { uz: r.opt.uz, ru: r.opt.ru },
            // ⚠️ Xususiyatning O'Z tartibi (meta'dan) — QATOR INDEKSI EMAS.
            // Etalon (t8): rang=0, Длина=44. client.py `defined:true` +
            // (REQUIRED bo'lsa) fillType/isRequired qo'shadi.
            orderingNumber: r.opt.orderingNumber,
            // requiredType — server REQUIRED (rang) ni aniqlashi uchun.
            requiredType: r.opt.requiredType || 'NOT_REQUIRED',
            flowA: !!r.opt.flowA,
            // MAXSUS xususiyat — server uni `customCharacteristics` ga ajratadi
            // (bandl: definedCharacteristics = defined, custom = qolgani,
            //  orderingNumber 100+indeks bilan qayta raqamlanadi).
            custom: !!r.opt.custom,
            // Qiymatlar — portal bergan obyektlar VERBATIM (client shuni kutadi).
            values: r.selected.map(function (v) {
              return { title: { uz: v.uz, ru: v.ru }, value: v.value, skuValue: v.skuValue };
            })
          };
        });

      // ⚠️ Qoralama ALLAQACHON yaratilgan bo'lsa (foydalanuvchi qadam nishoni
      // orqali 1-qadamga qaytgan) — createProduct DUBLIKAT karta yasardi.
      // Uzum bandli ham shu joyda ikkiga bo'linadi (chunk-6dbbb9d8 @79125):
      //   isEdit ? editProduct(...) : createProduct(...)
      var isEdit = !!(state.productId && state.cardFilled);
      fetch(isEdit ? '/noviy-tavar/api/update' : '/noviy-tavar/api/create', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({
          shop: state.shop,
          // create'da bu kalit yo'q — JSON.stringify `undefined` ni tashlaydi.
          productId: isEdit ? state.productId : undefined,
          categoryId: state.categoryId,
          titleUz: val('ntTitleUz'), titleRu: val('ntTitleRu'),
          shortUz: val('ntShortUz'), shortRu: val('ntShortRu'),
          descUz: html('ntDescUz'), descRu: html('ntDescRu'),
          descIsHtml: true,
          images: state.images.map(function (im) { return { key: im.key, url: im.url }; }),
          // routes.nt_create -> client.build_create_body(certificates=...) ->
          // body.productCertificates (pass-through). Shakl bandldan olingan.
          certificates: state.certificates,
          // client.py {key,url,color:{uz,ru},ordering} ni Uzum shakliga o'giradi
          // (colorImage/imageUrl/status/deletable) — biz xom holda beramiz.
          colorImages: state.colorImages,
          // Media — client.py Uzum shakliga o'giradi:
          //   video            -> {deletable,status,url,key}
          //   imageCollection  -> {status,deletable,collectionId}
          //   colorVideos      -> {color,status,deletable,videoUrl,videoKey}
          //   colorCollections -> colorCollectionImages{color,status,deletable,collectionId}
          // ⚠️ `previewUrl` ATAYLAB yuborilmaydi — u faqat UI uchun; Uzum uni
          // tanada emas, alohida `collectionPreviewUrl` da saqlaydi.
          video: state.video,
          imageCollection: state.imageCollection
            ? { collectionId: state.imageCollection.collectionId } : null,
          colorVideos: state.colorVideos,
          colorCollections: state.colorCollections.map(function (c) {
            return { color: c.color, collectionId: c.collectionId };
          }),
          filterValues: fvals,
          characteristics: chars,
          // Гарантия va shu kabi qo'shimcha maydonlar — {WARRANTY: <oy>}.
          // Bo'sh {} bo'lsa client.py uni oddiygina tashlaydi.
          productFields: state.productFields,
          comments: collectComments()
        })
      })
        .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
        .then(function (res) {
          saveBtn.textContent = prev;
          saveBtn.disabled = false;
          // ⚠️ Xato JIM YUTILMAYDI — foydalanuvchi saqlandi deb o'ylamasin.
          if (!res.ok || !res.d || !res.d.id) {
            notify((res.d && res.d.error) ||
                   (isEdit ? tr('Kartochka yangilanmadi', 'Не удалось обновить карточку')
                           : tr('Qoralama yaratilmadi', 'Не удалось создать черновик')));
            return;
          }
          notify(isEdit
            ? tr('Kartochka saqlandi', 'Карточка сохранена')
            : tr('Qoralama yaratildi: #' + res.d.id,
                 'Черновик создан: #' + res.d.id));
          // Uzumda yaratildi — endi lokal qoralama KERAKMAS. Tozalamasak,
          // keyingi «Yangi tovar» ochilishida eski forma tirilib chiqardi.
          if (root.ntClearDraft) root.ntClearDraft();
          // Karta endi Uzumda bor — keyingi «Saqlash» YANGILASH bo'ladi.
          state.productId = res.d.id;
          state.cardFilled = true;
          // 1-qadam Uzumga tushdi — shu holat endi XAVFSIZ nuqta.
          ntSnapshot();
          // Bandl ham muvaffaqiyatdan keyin 2-qadamga o'tadi (yaratishda ham,
          // yangilashda ham: `push(/{shop}/products/id/{id}/edit/sku/all)`).
          s2Enter(res.d.id);
        })
        .catch(function () {
          saveBtn.textContent = prev;
          saveBtn.disabled = false;
          notify(tr('Tarmoq xatosi', 'Ошибка сети'));
        });
    });
  }

  function html(id) { var e = document.getElementById(id); return e ? e.innerHTML.trim() : ''; }

  /* ════════════════════════════════════════════════════════════════
   *  2-QADAM «Цены и SKU»
   *
   *  MA'LUMOT MANBAI: /noviy-tavar/api/sku-step (description-response).
   *  `get_product` EMAS — unda `shopSkuTitle` yo'q (jonli o'lchangan).
   *  Uzum har rang qiymati uchun qatorni O'ZI oldindan quradi.
   *
   *  Bu blokdagi qoidalar Uzum bandlidan SO'ZMA-SO'Z ko'chirilgan (3-daraja
   *  dalil: «kod shunday qiladi»). Aniq joylari har qoidaning tepasida.
   * ════════════════════════════════════════════════════════════════ */

  var S2 = {
    productId: null,
    shopSkuTitle: '',     // Uzum O'ZI beradi (BLUMMY) — biz sozlamaymiz
    defined: [],          // definedCharacteristicList — send-sku'ga VERBATIM ketadi
    rows: [],
    tab: 'all',
    loading: false,
    // Bo'sh MAJBURIY kataklar. Uzum belgini FAQAT «Saqlash va davom etish»
    // bosilgach ko'rsatadi (errShown), katak to'ldirilishi bilan o'chiradi.
    errs: {},        // {rowIndex: {field: true}}
    errShown: false,
    // Mahsulot SKU'si saqlangan bo'lsa o'zgartirilmaydi — s2LockPrefix() qo'yadi.
    skuLocked: false
  };

  var step1El = document.getElementById('ntStep1');
  var step2El = document.getElementById('ntStep2');

  // Bandl `it(e) = e.skuValue || De(cb(e.title))` — skuValue ustun.
  function s2Label(row) {
    var titles = row.values.map(function (v) { return v.title; }).filter(Boolean);
    return { top: titles.join(' / '), sub: row.values.map(function (v) { return v.skuValue; }).join('-') };
  }

  // skuTitle = {shopSkuTitle}-{prefix}-{skuValue1}[-{skuValue2}...]
  // Bandl `ot()`: skuTitle.split("-").slice(2) bilan qiymatlarni ajratadi ->
  // birinchi IKKI bo'lak REZERV. Shuning uchun prefiksda «-» bo'lmasligi kerak.
  function s2SkuTitle(row) {
    var prefix = (document.getElementById('ntSkuPrefix') || {}).value || '';
    var vals = row.values.map(function (v) { return v.skuValue; });
    return [S2.shopSkuTitle, prefix.trim()].concat(vals).join('-');
  }

  function s2Num(v) {
    var s = String(v == null ? '' : v).replace(/\s/g, '').replace(',', '.');
    if (s === '') return 0;
    var n = Number(s);
    return isFinite(n) ? n : NaN;
  }

  // ⚠️ Uzumning O'Z nomuvofiqligi: xabar «кратной тысяче» deydi, KOD esa
  // `% 10` tekshiradi (chunk-7aca3fd6, modul f231: `var l = f(10)`,
  // `f(t) = e => Number(e) % Number(t) === 0`). Biz Uzumga 1:1 taqlid
  // qilamiz — tekshiruv 10 ga, xabar esa Uzumniki (foydalanuvchi qarori).
  function s2Mult(v) { return Number(v) % 10 === 0; }

  // sellPrice = fullPrice - off  (bandl `at()`; manfiy bo'lsa 0)
  function s2SellPrice(row) {
    var full = s2Num(row.fullPrice), off = s2Num(row.off);
    if (!(full >= 0 && off >= 0)) return 0;
    var c = full - off;
    return c > 0 ? c : 0;
  }

  // Bandl `nt()` — xato tartibi AYNAN shunday (birinchi topilgani ko'rsatiladi).
  //
  // ⚠️ HAMMA MATN Uzum bandlining O'Z i18n'idan (ru @356800.., uz @419100..).
  // O'zbekchasini O'YLAB TOPMA — Uzumniki bor va farq qiladi.
  // Bo'sh MAJBURIY katak maydonlari — Uzum «Saqlash va davom etish» da bularni
  // qizil «!» bilan belgilaydi. Format/mantiq xatolari (s2RowFormatError) ALOHIDA
  // — ular toast bilan chiqadi, katak qizarmaydi (Uzum ko'rinishi 2026-07-23).
  //   · Narx (fullPrice) — >0 bo'lishi shart
  //   · MXIK (ikpu) — bo'sh bo'lmasligi kerak
  //   · O'lchovlar — JONLI dalil (2026-07-18): o'lchovsiz sendSkuData 400
  //     `weight-and-size-characteristics-required-error`; build_sku_body yarim
  //     o'lchovni tashlaydi, shuning uchun 4 tasi ham kerak. Signal:
  //     meta.fields.fields.DIMENSIONAL_GROUP (har kategoriyada bor).
  function s2EmptyRequired(row) {
    var out = [];
    if (!(s2Num(row.fullPrice) > 0)) out.push('fullPrice');
    if (!String(row.ikpu || '').trim()) out.push('ikpu');
    // Omborda o'lchangan SKU — o'lchov tekshirilmaydi (bandl `y(e,t)`:
    // `if (!(status in [ARCHIVED, ARCHIVED_BLOCKED, BLOCKED] || e.updatedFromWms))`
    // — ya'ni bayroq bor bo'lsa validatsiya BUTUNLAY o'tkazib yuboriladi).
    // Aks holda qulflangan katakni foydalanuvchi to'ldira olmay qolardi.
    if (!row.updatedFromWms && s2DimsRequired()) {
      ['width', 'length', 'height', 'weight'].forEach(function (k) {
        if (!(s2Num(row[k]) > 0)) out.push(k);
      });
    }
    return out;
  }

  // Bo'sh bo'lmagan kataklardagi format/mantiq xatolari (toast, qizil emas).
  function s2RowFormatError(row) {
    var full = s2Num(row.fullPrice), off = s2Num(row.off);
    if (full > 0) {
      if (!(off < full)) {
        return tr('Chegirma summasi tovar summasidan oshmasligi yoki unga teng boʻlmasligi kerak',
                  'Сумма скидки не должна превышать или быть равной сумме товара');
      }
      // ⚠️ Xabar «1000 ga karrali» deydi, tekshiruv esa `% 10` — Uzumning O'Z
      // nomuvofiqligi (f231: `var l = f(10)`). Foydalanuvchi 1:1 taqlidni tanladi.
      if (!s2Mult(full)) {
        return tr('Narx 1000 ga karrali boʻlishi kerak', 'Цена должна быть кратной тысяче');
      }
    }
    if (off > 0 && !s2Mult(off)) {
      return tr('Chegirma 1000 ga karrali boʻlishi kerak',
                'Скидка должна быть кратной тысяче');
    }
    if (String(row.ikpu || '').trim() && row.ikpuValid === false) {
      return tr('Notogʻri MXIK', 'Неверный ИКПУ');
    }
    return null;
  }

  function s2DimsRequired() {
    var f = state.meta && state.meta.fields && state.meta.fields.fields;
    return !!(f && f.DIMENSIONAL_GROUP);
  }

  // ВГХ (o'lchov-og'irlik) ustunlari — bandl `["width","length","height","weight"]`.
  // Omborda o'lchangan SKU'da AYNAN shu to'rttasi qulflanadi.
  var S2_DIMS = ['width', 'length', 'height', 'weight'];

  // Bandl `sku_restriction`: butunlay lotin+raqam YOKI butunlay kirill+raqam.
  function s2PrefixError(v) {
    var s = String(v || '').trim();
    if (!s) {
      return tr('SKU nomini kiriting',
                'Пожалуйста, введите название для SKU в поля сверху страницы');
    }
    var latin = /^[A-Za-z0-9]+$/.test(s), cyr = /^[А-Яа-яЁё0-9]+$/.test(s);
    if (!latin && !cyr) {
      return tr('SKU ' + s + ' faqat lotin alifbosidagi harflardan va raqamlardan yoki ' +
                'kirill alifbosidagi harflardan va raqamlardan iborat boʻlishi kerak!',
                'SKU ' + s + ' должно быть целиком из латиницы и цифр либо кириллицы и цифр!');
    }
    return null;
  }

  function s2Money(n) {
    if (n == null || n === '' || isNaN(Number(n))) return '';
    return String(Math.round(Number(n))).replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
  }

  // hex -> rgba (status.color'ni Uzumdek och fon + to'q matn qilish uchun).
  function s2HexA(hex, a) {
    var h = String(hex || '').replace('#', '');
    if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
    var n = parseInt(h, 16);
    if (!isFinite(n) || h.length !== 6) return hex;
    return 'rgba(' + ((n >> 16) & 255) + ',' + ((n >> 8) & 255) + ',' + (n & 255) + ',' + a + ')';
  }

  // ── sku-step yuklash ────────────────────────────────────────────
  function s2Load(productId) {
    S2.loading = true;
    S2.productId = productId;
    var url = '/noviy-tavar/api/sku-step?shop=' + encodeURIComponent(state.shop) +
              '&productId=' + encodeURIComponent(productId);
    return fetch(url, { credentials: 'same-origin' })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d || {} }; }); })
      .then(function (res) {
        S2.loading = false;
        if (!res.ok) {
          notify(res.d.error || tr('2-qadam yuklanmadi', 'Не удалось загрузить шаг 2'));
          return false;
        }
        var d = res.d;
        S2.shopSkuTitle = d.shopSkuTitle || '';
        S2.defined = d.definedCharacteristicList || [];

        // skuValue -> title xaritasi (xususiyat tartibida)
        var ordered = S2.defined.slice().sort(function (a, b) {
          return (a.orderingNumber || 0) - (b.orderingNumber || 0);
        });
        var byVal = {};
        ordered.forEach(function (ch) {
          (ch.characteristicValues || []).forEach(function (v) {
            if (v && v.skuValue) {
              byVal[v.skuValue] = (v.title && (lang === 'uz' ? v.title.uz : v.title.ru)) ||
                                  (v.title && v.title.ru) || v.skuValue;
            }
          });
        });

        S2.rows = (d.skuList || []).map(function (sk) {
          // Uzum qatorni oldindan quradi: skuTitle = "BLUMMY-null-БЕЖЕВ".
          // ⚠️ literal "null" — Uzumning O'Z bug'i (prefiks bo'sh bo'lgani uchun).
          // Qiymat bo'laklari HAR DOIM 3-bo'lakdan boshlanadi (bandl `slice(2)`).
          var parts = String(sk.skuTitle || '').split('-').slice(2);
          var dims = sk.dimensions || {};
          return {
            id: sk.id || null,
            values: parts.map(function (p) { return { skuValue: p, title: byVal[p] || p }; }),
            status: sk.status || null,
            isActive: sk.isActive,
            canEdit: sk.canEdit !== false,
            // ⚠️ OMBORDA O'LCHANGAN SKU (ВГХ qulfi). Uzum tovar omborga
            // kelgach o'zi o'lchaydi va shundan keyin sotuvchiga o'lchov
            // yozishga RUXSAT BERMAYDI. Bayroq — description-response'ning
            // O'ZIDA, `dimensions` bilan bir qatorda.
            // JONLI DALIL (2026-07-28, do'kon 5983, mahsulot 2907838):
            //   11/11 SKU -> updatedFromWms:true, canEdit:true, IN_STOCK.
            // Ya'ni `canEdit` BU HOLATNI BILDIRMAYDI — alohida bayroq kerak.
            updatedFromWms: !!sk.updatedFromWms,
            sellerItemCode: sk.sellerItemCode || '',
            barcode: sk.barcode || null,
            ikpu: sk.ikpu || '',
            ikpuValid: null,
            width: dims.width || '', length: dims.length || '',
            height: dims.height || '', weight: dims.weight || '',
            fullPrice: sk.fullPrice == null ? '' : sk.fullPrice,
            off: (sk.fullPrice != null && sk.sellPrice != null)
                   ? (sk.fullPrice - sk.sellPrice) : '',
            marketPrice: sk.marketPrice,
            comm: null
          };
        });

        var t = document.getElementById('ntTblTitle');
        if (t) {
          var nm = d.title && (lang === 'uz' ? d.title.uz : d.title.ru);
          t.textContent = tr('Tovar jadvali', 'Таблица товара') +
                          (nm ? ' «' + nm + '»' : '');
        }
        var pf = document.getElementById('ntSkuPrefix');
        if (pf && d.productSkuTitle) { pf.value = d.productSkuTitle; s2CountPrefix(); }
        s2LockPrefix(pf, !!d.productSkuTitle);

        s2Render();
        s2Commission();
        return true;
      })
      .catch(function () {
        S2.loading = false;
        notify(tr('Tarmoq xatosi', 'Ошибка сети'));
        return false;
      });
  }

  // ── Mahsulot SKU'si — SAQLANGANDAN KEYIN O'ZGARMAYDI ─────────────
  //
  // BANDL (mf-products, «SKU для названия товара» inputi @15691746):
  //     readonly: O.isEditing && !!O.original.productSkuTitle
  //               && !O.isProductInActiveInvoice
  // Uchinchi shart — o'sha chunk'da `ref(!1)` bo'lib e'lon qilingan va HECH
  // QAYERDA o'zlashtirilmagan (`Z.value=` yo'q) => amalda DOIM false, ya'ni
  // qoida `isEditing && saqlangan productSkuTitle bor` ga qisqaradi.
  //
  // Bizda bayroq KERAK EMAS: yangi qoralamada `productSkuTitle` BO'SH keladi
  // (routes.py da qayd: qoralama 3068623 -> productSkuTitle=""), mavjud
  // kartada esa to'la. Ya'ni bo'sh-emaslikning O'ZI «tahrirlanmoqda» degani.
  // JONLI DALIL (2026-07-28): mahsulot 2907838 -> productSkuTitle="BRELOK1".
  //
  // NEGA umuman qulf: prefiks har bir SKU'ning `skuTitle` ichiga pishirilgan
  // ("LUXUZ-BRELOK1-КОРИЧН"), keyin o'zgartirilsa mavjud SKU'lar uziladi.
  //
  // ⚠️ `disabled` EMAS, `readonly`: Uzum inputni kulrang qilmaydi — matn
  // o'qiladi va nusxa olinadi, faqat yozib bo'lmaydi. Bandlda bu holat uchun
  // uslub qoidasi UMUMAN yo'q, shuning uchun bizda ham CSS qo'shilmagan
  // (izohi noviy_tavar.css da).
  function s2LockPrefix(pf, locked) {
    S2.skuLocked = !!locked;
    if (!pf) return;
    // Qayta yuklashda TIKLANADI — bir xil DOM elementi qayta ishlatiladi.
    pf.readOnly = !!locked;
  }

  // ── Komissiya (O'QISH) ──────────────────────────────────────────
  // ⚠️ Uzum `skuPrices` uchun `skuId` TALAB QILADI. Yangi qoralamada SKU hali
  // yo'q -> ro'yxat BO'SH qaytadi (jonli 2 marta tasdiqlangan: NO_SKU
  // qoralamada ham, SKU'si bor mahsulotda `skuId`siz ham). Uzumning O'Z
  // portali ham aynan shu bo'sh javobni oladi — bu XATO EMAS.
  var s2CommTimer = null;
  function s2Commission() {
    if (s2CommTimer) clearTimeout(s2CommTimer);
    // Bandl: 300ms debounce (`b.n.debounce(..., 300)`).
    s2CommTimer = setTimeout(function () {
      var prices = S2.rows.filter(function (r) { return r.id; }).map(function (r) {
        return { newPrice: Math.round(s2SellPrice(r)), skuId: r.id };
      });
      if (!prices.length) { s2RenderComm(); return; }
      fetch('/noviy-tavar/api/commission', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ shop: state.shop, productId: S2.productId, prices: prices })
      })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
          if (!d) return;
          var by = {};
          (d.items || []).forEach(function (it) { by[it.skuId] = it; });
          S2.rows.forEach(function (r) { if (r.id && by[r.id]) r.comm = by[r.id]; });
          s2RenderComm();
        })
        .catch(function () { /* komissiya — ikkilamchi, UI tirik qoladi */ });
    }, 300);
  }

  function s2RenderComm() {
    S2.rows.forEach(function (r, i) {
      var tr_ = document.querySelector('#ntSkuRows tr[data-i="' + i + '"]');
      if (!tr_) return;
      ['logistics', 'comm', 'withdraw'].forEach(function (k) {
        var c = tr_.querySelector('[data-comm="' + k + '"]');
        if (!c) return;
        if (!r.comm) { c.textContent = '—'; return; }
        var v = k === 'logistics' ? r.comm.logisticDeliveryFee
              : k === 'comm' ? r.comm.marketplaceCommission
              : r.comm.toWithdraw;
        c.textContent = v == null ? '—' : s2Money(v);
        if (k === 'withdraw') c.classList.toggle('is-neg', Number(v) < 0);
      });
    });
  }

  // ── Jadval ──────────────────────────────────────────────────────
  function s2Visible() {
    if (S2.tab === 'all') return S2.rows;
    // Bandl status enum: ARCHIVED / ARCHIVED_BLOCKED / BLOCKED / RUN_OUT / IN_STOCK
    return S2.rows.filter(function (r) {
      var v = r.status && r.status.value;
      var arch = v === 'ARCHIVED' || v === 'ARCHIVED_BLOCKED';
      return S2.tab === 'archived' ? arch : !arch;
    });
  }

  function s2Cell(row, i, field, opts) {
    opts = opts || {};
    var td = document.createElement('td');
    td.className = 'nt-td';
    var wrap = document.createElement('span');
    wrap.className = 'nt-td-input' + (opts.unit ? ' has-unit' : '');
    var inp = document.createElement('input');
    inp.className = 'nt-input nt-input--cell';
    inp.type = 'text';
    inp.inputMode = opts.numeric ? 'numeric' : 'text';
    inp.value = row[field] == null ? '' : row[field];
    if (opts.maxlength) inp.maxLength = opts.maxlength;
    if (opts.placeholder) inp.placeholder = opts.placeholder;
    inp.setAttribute('aria-label', opts.label || field);
    if (row.canEdit === false) inp.disabled = true;
    // Omborda o'lchangan SKU — ВГХ katagi QULF (bandl `edit-product-sku-table`:
    // `disabled: f(n) || n.updatedFromWms`, bu yerda `f` = status ARCHIVED/
    // ARCHIVED_BLOCKED/BLOCKED). Bayroq faqat 4 ta o'lchov ustuniga beriladi.
    if (opts.wms) inp.disabled = true;
    inp.addEventListener('input', function () {
      row[field] = inp.value;
      if (field === 'fullPrice' || field === 'off') { s2Sync(i); s2Commission(); }
      if (S2.errShown) s2ClearCellError(inp, i, field, inp.value);
      s2Validate();
    });
    wrap.appendChild(inp);
    if (opts.unit) {
      var u = document.createElement('span');
      u.className = 'nt-td-unit';
      u.textContent = opts.unit;
      wrap.appendChild(u);
    }
    // Ko'k ⓘ — inputning O'NG chekkasidan tashqarida (bandl:
    // `.validation-icon{position:absolute;right:-20px}`), shuning uchun
    // `wrap` ichida turadi, `td` da emas.
    if (opts.wms) wrap.appendChild(s2WmsIcon(opts.tipTop));
    td.appendChild(wrap);
    // Bo'sh majburiy katak («Saqlash va davom etish» dan keyin) — qizil + «!».
    td.dataset.field = field;
    if (s2HasErr(i, field)) { td.classList.add('has-error'); td.appendChild(s2ErrMark()); }
    return td;
  }

  // Qizil «!» doira belgisi — Uzum bo'sh majburiy katagining o'ng chekkasida.
  function s2ErrMark() {
    var m = document.createElement('span');
    m.className = 'nt-cell-err-mark';
    m.setAttribute('aria-label', tr('Maydon toʻldirish majburiy', 'Обязательное поле'));
    m.textContent = '!';
    return m;
  }

  function s2HasErr(i, field) {
    return !!(S2.errShown && S2.errs[i] && S2.errs[i][field]);
  }

  // ── Omborda o'lchangan SKU: ko'k ⓘ + qora maslahat ────────────────
  //
  // Matn Uzum bandlining O'Z i18n bloki (`create_sku.updated_from_wms`) —
  // ru @48970064, uz @49035417. So'zma-so'z, tarjima QILINMAGAN.
  function s2WmsText() {
    return tr('Ushbu SKU omborda o‘lchab bo‘lingan. VGT o‘zgartirish uchun ' +
              'biznes qo‘llab-quvvatlash xizmatiga murojaat qiling',
              'Данный SKU был замерен на складе. Для изменения ВГХ ' +
              'пожалуйста обратитесь в бизнес-поддержку');
  }

  // Yagona maslahat elementi — jadvaldan TASHQARIDA, sahifa ILDIZIDA.
  // Jadval `overflow-x: auto` bo'lgani uchun katak ichidagi maslahat
  // kesilardi; Uzum ham aynan shu sababdan `teleport` qiladi
  // (bandl: `drop-down-props:{teleport:...}`).
  // ⚠️ `document.body` EMAS — modul doirasi qoidasi (test_noviy_tavar_scope)
  // har bir uslub selektori `[data-page="noviy-tavar"]` dan boshlanishini
  // talab qiladi, ya'ni element ildiz ICHIDA turishi shart.
  var wmsTipEl = null;
  function s2WmsTipEl() {
    if (wmsTipEl && wmsTipEl.parentNode) return wmsTipEl;
    wmsTipEl = document.createElement('div');
    wmsTipEl.className = 'nt-wms-tip';
    wmsTipEl.setAttribute('role', 'tooltip');
    wmsTipEl.hidden = true;
    (root || document.body).appendChild(wmsTipEl);
    return wmsTipEl;
  }

  function s2WmsTipShow(anchor, top) {
    var el = s2WmsTipEl();
    el.textContent = s2WmsText();
    el.hidden = false;
    var a = anchor.getBoundingClientRect();
    var b = el.getBoundingClientRect();
    // Bandl `v(a)`: OXIRGI qator -> "top-center", qolganlari -> "bottom-center".
    var y = top ? (a.top - b.height - 8) : (a.bottom + 8);
    var x = a.left + (a.width / 2) - (b.width / 2);
    // Ekrandan chiqib ketmasin (bandl: `max-width: calc(100vw - 16px)`).
    x = Math.max(8, Math.min(x, window.innerWidth - b.width - 8));
    el.style.left = Math.round(x) + 'px';
    el.style.top = Math.round(y) + 'px';
  }

  function s2WmsTipHide() {
    if (wmsTipEl) wmsTipEl.hidden = true;
  }

  function s2WmsIcon(top) {
    var s = document.createElement('span');
    s.className = 'nt-wms';
    s.setAttribute('tabindex', '0');
    s.setAttribute('role', 'img');
    s.setAttribute('aria-label', s2WmsText());
    // Uzum nishoni: to'ldirilgan ko'k doira + oq «i» (16px).
    // Rang — bandlning O'Z dizayn tokeni: `IconInfo: "#478eff"` (yorug' mavzu).
    s.innerHTML =
      '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" focusable="false">' +
      '<circle cx="8" cy="8" r="8" fill="#478eff"></circle>' +
      '<rect x="7" y="6.8" width="2" height="5.4" rx="1" fill="#fff"></rect>' +
      '<circle cx="8" cy="4.6" r="1.1" fill="#fff"></circle></svg>';
    s.addEventListener('mouseenter', function () { s2WmsTipShow(s, top); });
    s.addEventListener('mouseleave', s2WmsTipHide);
    s.addEventListener('focus', function () { s2WmsTipShow(s, top); });
    s.addEventListener('blur', s2WmsTipHide);
    return s;
  }

  // Katak to'ldirilishi bilan qizil belgini o'chiradi (qatorni QAYTA QURMASDAN —
  // aks holda yozish o'rtasida fokus yo'qoladi).
  function s2ClearCellError(inp, i, field, val) {
    if (!(S2.errs[i] && S2.errs[i][field])) return;
    var filled = (s2Num(val) > 0);
    if (!filled) return;
    delete S2.errs[i][field];
    var td = inp.closest ? inp.closest('td') : null;
    if (!td) return;
    td.classList.remove('has-error');
    var mk = td.querySelector('.nt-cell-err-mark');
    if (mk && mk.parentNode) mk.parentNode.removeChild(mk);
  }

  function s2Render() {
    var tb = document.getElementById('ntSkuRows');
    var empty = document.getElementById('ntSkuEmpty');
    if (!tb) return;
    // Qatorlar qayta quriladi — ochiq maslahat «yetim» nishonni ko'rsatmasin.
    s2WmsTipHide();
    tb.textContent = '';
    var vis = s2Visible();
    if (empty) {
      empty.hidden = vis.length > 0;
      empty.textContent = tr('Bu bo‘limda SKU yo‘q', 'В этом разделе нет SKU');
    }
    vis.forEach(function (row, vi) {
      var i = S2.rows.indexOf(row);
      // Bandl `v(a)`: OXIRGI qatorning maslahati YUQORIDA ochiladi (aks holda
      // jadval ostidan chiqib ketardi), qolganlariniki pastda.
      var tipTop = vi === vis.length - 1;
      var tr_ = document.createElement('tr');
      tr_.className = 'nt-tr';
      tr_.dataset.i = String(i);

      // 1. Артикул — title / skuValue (referens: «Розовый / РОЗОВ»)
      var lab = s2Label(row);
      var tdA = document.createElement('td');
      tdA.className = 'nt-td nt-td--sticky';
      var t1 = document.createElement('div');
      t1.className = 'nt-art-top';
      t1.textContent = lab.top;
      var t2 = document.createElement('div');
      t2.className = 'nt-art-sub';
      t2.textContent = lab.sub;
      tdA.appendChild(t1); tdA.appendChild(t2);
      tr_.appendChild(tdA);

      // 2. Статус
      var tdS = document.createElement('td');
      tdS.className = 'nt-td';
      if (row.status && row.status.title) {
        var b = document.createElement('span');
        b.className = 'nt-sku-badge';
        b.textContent = row.status.title;
        // Rang Uzumdan keladi (status.color: IN_STOCK #51AE42, RUN_OUT #E4981B,
        // ...) — o'zimdan hech narsa qo'shmayman, faqat och fon + to'q matn.
        var sc = row.status.color;
        if (sc) {
          b.style.color = sc;
          b.style.background = s2HexA(sc, 0.12);
          b.style.borderColor = s2HexA(sc, 0.32);
        }
        tdS.appendChild(b);
      } else {
        tdS.textContent = '—';
      }
      tr_.appendChild(tdS);

      // 3. Идентификатор SKU -> sellerItemCode
      //    Bandl `lt()`: `sellerItemCode: t.sellerItemCode.value || undefined`
      //    -> bo'sh bo'lsa tanaga QO'SHILMAYDI (shuning uchun HAR'da ko'rinmaydi).
      tr_.appendChild(s2Cell(row, i, 'sellerItemCode', {
        label: tr('SKU identifikatori', 'Идентификатор SKU'), maxlength: 100
      }));

      // 4. Штрихкод — Uzum beradi, biz o'qiymiz (yangi SKU'da yo'q)
      var tdB = document.createElement('td');
      tdB.className = 'nt-td nt-td--ro';
      tdB.textContent = row.barcode || '—';
      tr_.appendChild(tdB);

      // 5. ИКПУ — tanlagich (bandl: ro'yxatdan tanlanadi, erkin matn emas)
      var tdI = document.createElement('td');
      tdI.className = 'nt-td';
      tdI.dataset.field = 'ikpu';
      if (s2HasErr(i, 'ikpu')) { tdI.classList.add('has-error'); }
      var ib = document.createElement('button');
      ib.type = 'button';
      ib.className = 'nt-ikpu-btn' + (row.ikpu ? '' : ' is-empty');
      // Uzum ko'rinishi: kod (kesiluvchi) + chevron ▼.
      ib.innerHTML = '<span class="nt-ikpu-btn-t">' +
        esc(row.ikpu || tr('Tanlang', 'Выберите')) + '</span>' +
        '<span class="nt-ikpu-btn-caret" aria-hidden="true">' + S3_CARET + '</span>';
      ib.setAttribute('aria-label', tr('IKPU tanlash', 'Выбрать ИКПУ'));
      if (row.canEdit === false) ib.disabled = true;
      ib.addEventListener('click', function (e) {
        e.stopPropagation();
        s2OpenIkpu(ib, i);
      });
      tdI.appendChild(ib);
      if (s2HasErr(i, 'ikpu')) tdI.appendChild(s2ErrMark());
      tr_.appendChild(tdI);

      // 6-9. O'lchovlar — HAMMASI-YOKI-HECHNARSA (bandl `lt()`: bittasi bo'sh
      //      bo'lsa `dimensions` BUTUNLAY tushadi). Buni foydalanuvchiga
      //      ko'rsatish uchun s2Validate() da ogohlantiramiz.
      //      Omborda o'lchangan bo'lsa — TO'RTALASI ham qulf + ko'k ⓘ.
      var mm = tr('mm', 'мм'), gg = tr('g', 'г');
      var w = !!row.updatedFromWms;
      tr_.appendChild(s2Cell(row, i, 'width', { numeric: true, unit: mm, label: tr('Eni', 'Ширина'), wms: w, tipTop: tipTop }));
      tr_.appendChild(s2Cell(row, i, 'length', { numeric: true, unit: mm, label: tr('Uzunligi', 'Длина'), wms: w, tipTop: tipTop }));
      tr_.appendChild(s2Cell(row, i, 'height', { numeric: true, unit: mm, label: tr('Balandligi', 'Высота'), wms: w, tipTop: tipTop }));
      tr_.appendChild(s2Cell(row, i, 'weight', { numeric: true, unit: gg, label: tr('Ogʻirligi', 'Вес'), wms: w, tipTop: tipTop }));

      // 10. Рекомендуемая цена -> marketPrice (bo'sh bo'lsa Uzum matni)
      var tdM = document.createElement('td');
      tdM.className = 'nt-td nt-td--ro';
      tdM.textContent = row.marketPrice
        ? s2Money(row.marketPrice)
        : tr('Narx hali topilmadi', 'Пока не нашли цену');
      if (!row.marketPrice) tdM.classList.add('is-muted');
      tr_.appendChild(tdM);

      // 11. Цена, сум -> fullPrice  (bandl: `fullPrice: Number(t.fullPrice.value)`)
      tr_.appendChild(s2Cell(row, i, 'fullPrice', {
        numeric: true, unit: tr('soʻm', 'сум'), label: tr('Narx', 'Цена')
      }));
      // 12. Скидка, сум -> off
      tr_.appendChild(s2Cell(row, i, 'off', {
        numeric: true, unit: tr('soʻm', 'сум'), label: tr('Chegirma', 'Скидка')
      }));

      // 13. Цена продажи -> HISOBLANADI (bandl `at()`: fullPrice - off).
      //     Bandl uni ham inputga yozmaydi — `sellPrice` da `.value` YO'Q.
      var tdSell = document.createElement('td');
      tdSell.className = 'nt-td nt-td--ro nt-td--sell';
      tdSell.dataset.sell = '1';
      tdSell.textContent = s2Money(s2SellPrice(row));
      tr_.appendChild(tdSell);

      // 14-16. Komissiya — skuId talab qiladi -> yangi qoralamada BO'SH
      ['logistics', 'comm', 'withdraw'].forEach(function (k) {
        var td = document.createElement('td');
        td.className = 'nt-td nt-td--ro';
        td.dataset.comm = k;
        td.textContent = '—';
        tr_.appendChild(td);
      });

      tb.appendChild(tr_);
    });
    s2RenderComm();
    s2Validate();
  }

  // Bitta qatorning hisoblangan kataklarini yangilash (to'liq re-render emas —
  // aks holda fokus yo'qoladi va yozib bo'lmaydi).
  function s2Sync(i) {
    var tr_ = document.querySelector('#ntSkuRows tr[data-i="' + i + '"]');
    if (!tr_) return;
    var c = tr_.querySelector('[data-sell]');
    if (c) c.textContent = s2Money(s2SellPrice(S2.rows[i]));
  }

  // ── Validatsiya ─────────────────────────────────────────────────
  // ⚠️ UZUMDEK: «Saqlash va davom etish» bo'sh majburiy kataklar uchun
  // O'CHIRILMAYDI. U bosiladi, keyin s2MarkRequiredErrors() kataklarni qizartadi
  // (step 3'dagi «Yakunlash» bilan bir xil xatti-harakat). Tugma faqat SKU
  // umuman bo'lmasa o'chiq. SKU-nomi prefiksi esa jonli qizil matn beradi.
  function s2Validate() {
    var saveBtn2 = document.getElementById('ntSave');
    var pf = document.getElementById('ntSkuPrefix');
    // Qulflangan prefiks TEKSHIRILMAYDI: qiymat Uzumning O'ZIDAN keldi va
    // foydalanuvchi uni tuzata olmaydi — aks holda tuzatilmas qizil xato
    // «Saqlash» ni butunlay to'sib qo'yardi (eski kartalarda real xavf).
    var pe = S2.skuLocked ? null : s2PrefixError(pf ? pf.value : '');
    var pErr = document.getElementById('ntSkuPrefixErr');
    if (pErr) { pErr.textContent = pe || ''; pErr.hidden = !pe; }
    if (saveBtn2 && state.step === 2) saveBtn2.disabled = !S2.rows.length;
    return !pe && S2.rows.length > 0;
  }

  // «Saqlash va davom etish» bosilganda — bo'sh MAJBURIY kataklarni qizil
  // «!» bilan belgilaydi. Qaytaradi: belgilangan kataklar soni.
  function s2MarkRequiredErrors() {
    S2.errs = {};
    var n = 0;
    S2.rows.forEach(function (row, i) {
      var empty = s2EmptyRequired(row);
      if (!empty.length) return;
      S2.errs[i] = {};
      empty.forEach(function (f) { S2.errs[i][f] = true; n++; });
    });
    S2.errShown = true;
    s2Render();
    return n;
  }

  function s2CountPrefix() {
    var pf = document.getElementById('ntSkuPrefix');
    var c = document.getElementById('ntSkuPrefixCount');
    if (pf && c) c.textContent = pf.value.length + '/100';
  }

  var prefixEl = document.getElementById('ntSkuPrefix');
  if (prefixEl) {
    prefixEl.addEventListener('input', function () {
      s2CountPrefix();
      s2Validate();
    });
  }

  // ── Tablar ──────────────────────────────────────────────────────
  Array.prototype.forEach.call(document.querySelectorAll('[data-nt-tab]'), function (b) {
    b.addEventListener('click', function () {
      S2.tab = b.dataset.ntTab;
      Array.prototype.forEach.call(document.querySelectorAll('[data-nt-tab]'), function (o) {
        var on = o === b;
        o.classList.toggle('is-active', on);
        o.setAttribute('aria-selected', on ? 'true' : 'false');
      });
      s2Render();
    });
  });

  // ── Bulk-fill (⚡) ──────────────────────────────────────────────
  // Bandl `forAllValues` ANIQ 7 maydon: fullPrice, off, ikpu, width, length,
  // height, weight. Boshqa ustunda ⚡ YO'Q — shuning uchun bu ro'yxat qat'iy.
  var bulkPop = document.getElementById('ntBulkPop');
  var bulkField = document.getElementById('ntBulkField');
  var bulkOpenFor = null;

  function s2CloseBulk() {
    if (bulkPop) bulkPop.hidden = true;
    bulkOpenFor = null;
    Array.prototype.forEach.call(document.querySelectorAll('[data-nt-bulk]'), function (b) {
      b.setAttribute('aria-expanded', 'false');
    });
  }

  Array.prototype.forEach.call(document.querySelectorAll('[data-nt-bulk]'), function (btn) {
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      var f = btn.dataset.ntBulk;
      if (bulkOpenFor === f) { s2CloseBulk(); return; }
      s2CloseBulk();
      s2CloseIkpu();
      bulkOpenFor = f;
      btn.setAttribute('aria-expanded', 'true');
      bulkField.textContent = '';
      if (f === 'ikpu') {
        var ib = document.createElement('button');
        ib.type = 'button';
        ib.className = 'nt-ikpu-btn is-empty';
        ib.textContent = tr('Tanlang', 'Выберите');
        ib.addEventListener('click', function (ev) {
          ev.stopPropagation();
          s2OpenIkpu(ib, -1);   // -1 = hamma qatorga
        });
        bulkField.appendChild(ib);
      } else {
        var inp = document.createElement('input');
        inp.className = 'nt-input';
        inp.type = 'text';
        inp.inputMode = 'numeric';
        inp.id = 'ntBulkInput';
        inp.setAttribute('aria-label', tr('Qiymat', 'Значение'));
        bulkField.appendChild(inp);
        setTimeout(function () { inp.focus(); }, 0);
      }
      var r = btn.getBoundingClientRect();
      bulkPop.hidden = false;
      bulkPop.style.top = (window.scrollY + r.bottom + 6) + 'px';
      bulkPop.style.left = Math.max(8, window.scrollX + r.left - 100) + 'px';
    });
  });

  var bulkApply = document.getElementById('ntBulkApply');
  if (bulkApply) {
    bulkApply.addEventListener('click', function () {
      if (!bulkOpenFor || bulkOpenFor === 'ikpu') { s2CloseBulk(); return; }
      var inp = document.getElementById('ntBulkInput');
      if (!inp) return;
      var v = inp.value.trim();
      var isDim = S2_DIMS.indexOf(bulkOpenFor) >= 0;
      S2.rows.forEach(function (r) {
        // Bandl: `canEdit` false yoki bloklangan SKU'ga o'lchov yozilmaydi.
        if (r.canEdit === false) return;
        // Bandl `R()`: `n.updatedFromWms && l || (...)` — l = maydon o'lchov
        // ustunlaridan biri. Ya'ni ⚡ omborda o'lchangan qatorning ВГХ sini
        // CHETLAB O'TADI, lekin narx/MXIK ni baribir to'ldiradi.
        if (isDim && r.updatedFromWms) return;
        r[bulkOpenFor] = v;
      });
      s2CloseBulk();
      s2Render();
      s2Commission();
    });
  }
  var bulkCancel = document.getElementById('ntBulkCancel');
  if (bulkCancel) bulkCancel.addEventListener('click', s2CloseBulk);

  // ── IKPU tanlagich ──────────────────────────────────────────────
  var ikpuPop = document.getElementById('ntIkpuPop');
  var ikpuSearch = document.getElementById('ntIkpuSearch');
  var ikpuList = document.getElementById('ntIkpuList');
  var ikpuNote = document.getElementById('ntIkpuNote');
  var ikpuTarget = null;   // qator indeksi; -1 = hammasi
  var ikpuTimer = null;
  var ikpuAnchorBox = null;   // ochilgan tugma o'rni (hujjat koordinatalarida)

  function s2CloseIkpu() {
    if (ikpuPop) ikpuPop.hidden = true;
    ikpuTarget = null;
    ikpuAnchorBox = null;
  }

  function s2OpenIkpu(anchor, i) {
    if (!ikpuPop) return;
    // ⚠️ Anchor bulk popover ICHIDA bo'lishi mumkin (⚡ -> «Выберите»). Avval
    // s2CloseBulk() chaqirilardi: popover display:none bo'lgach anchor'ning
    // getBoundingClientRect() i NOL qaytarardi va panel sahifa chap-yuqorisiga
    // uchib ketardi (bag 2026-07-22). Bulk ochiq bo'lsa — yopmaymiz.
    var inBulk = !!(bulkPop && !bulkPop.hidden && bulkPop.contains(anchor));
    if (!inBulk) s2CloseBulk();
    ikpuTarget = i;
    ikpuPop.hidden = false;
    var r = anchor.getBoundingClientRect();
    ikpuAnchorBox = {                       // hujjat koordinatalarida saqlaymiz
      top: r.top + window.scrollY,
      bottom: r.bottom + window.scrollY,
      left: r.left + window.scrollX
    };
    if (ikpuList) ikpuList.textContent = '';
    s2IkpuNote(tr('Iltimos, uchtadan ortiq belgi kiriting', 'Введите более трех символов'));
    s2PlaceIkpu();
    // preventScroll: fokus sahifani sakratmasin.
    if (ikpuSearch) {
      ikpuSearch.value = '';
      try { ikpuSearch.focus({ preventScroll: true }); } catch (e) { ikpuSearch.focus(); }
    }
  }

  // Panelni anchor'ga nisbatan joylash. Ro'yxat to'lgach balandlik o'zgaradi,
  // shuning uchun HAR renderdan keyin qayta chaqiriladi (aks holda «tepaga
  // ochilgan» panel bilan tugma orasida bo'sh joy qoladi).
  function s2PlaceIkpu() {
    if (!ikpuPop || ikpuPop.hidden || !ikpuAnchorBox) return;
    var b = ikpuAnchorBox;
    var h = ikpuPop.offsetHeight;
    var vTop = window.scrollY;
    var vBot = vTop + document.documentElement.clientHeight;
    var top = b.bottom + 6;
    // Pastda joy yo'q, tepada bor bo'lsa — tugmaning USTIDA ochamiz.
    if (top + h > vBot - 8 && b.top - 6 - h > vTop + 8) top = b.top - 6 - h;
    ikpuPop.style.top = top + 'px';
    // O'ng chekkadan chiqib ketmasin (panel 460px, bulk popover esa 260px).
    var maxLeft = window.scrollX + document.documentElement.clientWidth -
                  ikpuPop.offsetWidth - 8;
    ikpuPop.style.left = Math.max(8, Math.min(b.left, maxLeft)) + 'px';
  }

  function s2IkpuNote(msg) {
    if (!ikpuNote) return;
    ikpuNote.textContent = msg || '';
    ikpuNote.hidden = !msg;
    s2PlaceIkpu();   // balandlik o'zgardi — anchor'ga qayta yopishtiramiz
  }

  if (ikpuSearch) {
    ikpuSearch.addEventListener('input', function () {
      if (ikpuTimer) clearTimeout(ikpuTimer);
      var q = ikpuSearch.value.trim();
      // Bandl `ikpu_min_length`: «Введите более трех символов» -> 3 dan ORTIQ.
      if (q.length < 3) {
        if (ikpuList) ikpuList.textContent = '';
        s2IkpuNote(tr('Iltimos, uchtadan ortiq belgi kiriting', 'Введите более трех символов'));
        return;
      }
      ikpuTimer = setTimeout(function () { s2IkpuSearch(q); }, 300);
    });
  }

  function s2IkpuSearch(q) {
    if (!state.categoryId) {
      s2IkpuNote(tr('Avval kategoriya tanlang', 'Сначала выберите категорию'));
      return;
    }
    s2IkpuNote(tr('Qidirilmoqda...', 'Поиск...'));
    var url = '/noviy-tavar/api/ikpu-search?shop=' + encodeURIComponent(state.shop) +
              '&categoryId=' + encodeURIComponent(state.categoryId) +
              '&q=' + encodeURIComponent(q);
    fetch(url, { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!ikpuList) return;
        ikpuList.textContent = '';
        var items = (d && d.items) || [];
        if (!items.length) {
          s2IkpuNote(tr('Hech narsa topilmadi', 'Ничего не найдено'));
          return;
        }
        s2IkpuNote('');
        items.forEach(function (it) {
          var b = document.createElement('button');
          b.type = 'button';
          b.className = 'nt-ikpu-item';
          var code = document.createElement('span');
          code.className = 'nt-ikpu-code';
          code.textContent = it.ikpu;
          var nm = document.createElement('span');
          nm.className = 'nt-ikpu-name';
          nm.textContent = it.name || '';
          b.appendChild(code); b.appendChild(nm);
          // Uzum aytadi: kod shu kategoriyaga mos keladimi. KO'RSATAMIZ —
          // aks holda foydalanuvchi moderatsiyada qoladi.
          if (it.validForCategory === false) {
            var w = document.createElement('span');
            w.className = 'nt-ikpu-warn';
            w.textContent = tr('kategoriyaga mos emas', 'не подходит категории');
            b.appendChild(w);
          }
          b.addEventListener('click', function () { s2PickIkpu(it); });
          ikpuList.appendChild(b);
        });
        s2PlaceIkpu();   // ro'yxat to'ldi — joyni qayta hisoblaymiz
      })
      .catch(function () { s2IkpuNote(tr('Tarmoq xatosi', 'Ошибка сети')); });
  }

  function s2PickIkpu(it) {
    var apply = function (r) {
      if (r.canEdit === false) return;
      r.ikpu = it.ikpu;
      r.ikpuValid = it.validForCategory !== false;
    };
    if (ikpuTarget === -1) {
      S2.rows.forEach(function (r, i) { apply(r); if (S2.errs[i]) delete S2.errs[i].ikpu; });
    } else if (S2.rows[ikpuTarget]) {
      apply(S2.rows[ikpuTarget]);
      if (S2.errs[ikpuTarget]) delete S2.errs[ikpuTarget].ikpu;
    }
    s2CloseIkpu();
    s2CloseBulk();
    s2Render();
  }

  document.addEventListener('click', function (e) {
    // IKPU paneli bulk popover'dan ochilgan bo'lishi mumkin — panel ichidagi
    // klik bulk'ni YOPMASIN (aks holda «Выберите» tugmasi ostidan yo'qoladi).
    var inIkpu = !!(ikpuPop && ikpuPop.contains(e.target));
    if (ikpuPop && !ikpuPop.hidden && !inIkpu) s2CloseIkpu();
    if (bulkPop && !bulkPop.hidden && !inIkpu && !bulkPop.contains(e.target)) s2CloseBulk();
  });
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    s2CloseIkpu();
    s2CloseBulk();
  });

  // ── Saqlash (sendSkuData) — YOZUVCHI ────────────────────────────
  // ⚠️ Bu chaqiruv qoralamaga HAQIQIY SKU qo'shadi. Server ham (routes.py
  // nt_send_sku) validatsiya qiladi — brauzerga ishonmaymiz. Bu yerdagi
  // tekshiruvlar server ro'yxatining NUSXASI: maqsad — foydalanuvchi 400
  // ko'rmasin, xatoni shu yerda ko'rsin.
  function s2Send() {
    if (!S2.rows.length) {
      notify(tr('Forma to‘liq emas', 'Форма заполнена не полностью'));
      return;
    }
    // Uzumdek: bosilganda tekshiramiz. Bo'sh majburiy kataklar → qizil «!»,
    // format/mantiq xatolari → toast, SKU-nomi → qizil matn maydonda.
    var pf = document.getElementById('ntSkuPrefix');
    // Qulflangan prefiks — s2Validate() dagi bilan bir xil sabab.
    var pe = S2.skuLocked ? null : s2PrefixError(pf ? pf.value : '');
    var pErr = document.getElementById('ntSkuPrefixErr');
    if (pErr) { pErr.textContent = pe || ''; pErr.hidden = !pe; }

    var nEmpty = s2MarkRequiredErrors();

    var fmt = null;
    for (var i = 0; i < S2.rows.length; i++) {
      var e = s2RowFormatError(S2.rows[i]);
      if (e) { fmt = (i + 1) + '-qator: ' + e; break; }
    }

    if (pe || nEmpty || fmt) {
      notify(pe || fmt ||
        tr('Majburiy maydonlarni toʻldiring', 'Заполните обязательные поля'));
      return;
    }

    var btn = document.getElementById('ntSave');
    var prev = btn ? btn.textContent : '';
    if (btn) {
      btn.disabled = true;
      btn.textContent = tr('Saqlanmoqda...', 'Сохранение...');
    }

    var rows = S2.rows.map(function (r) {
      return {
        skuTitle: s2SkuTitle(r),
        ikpu: String(r.ikpu || '').trim(),
        fullPrice: Math.round(s2Num(r.fullPrice)),
        // ⚠️ sellPrice INPUT EMAS — hisoblanadi (bandl `at()`: fullPrice-off).
        // Serverga aynan shu hisoblangan qiymat ketadi.
        sellPrice: Math.round(s2SellPrice(r)),
        barcode: r.barcode || null,
        sellerItemCode: String(r.sellerItemCode || '').trim(),
        id: r.id || null,
        width: s2Num(r.width) || 0, height: s2Num(r.height) || 0,
        length: s2Num(r.length) || 0, weight: s2Num(r.weight) || 0
      };
    });

    fetch('/noviy-tavar/api/send-sku', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({
        shop: state.shop,
        productId: S2.productId,
        skuForProduct: (document.getElementById('ntSkuPrefix') || {}).value.trim(),
        // ⚠️ VERBATIM uzatiladi — usiz build_sku_body `skuCharacteristicList` ni
        // BO'SH quradi va SKU RANGSIZ ketadi (`_sku_chars_for` shunga tayanadi).
        definedCharacteristicList: S2.defined,
        rows: rows
      })
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d || {} }; }); })
      .then(function (res) {
        if (btn) { btn.disabled = false; btn.textContent = prev; }
        // Xato JIM YUTILMAYDI — foydalanuvchi saqlandi deb o'ylamasin.
        if (!res.ok) {
          notify(res.d.error || tr('SKU saqlanmadi', 'Не удалось сохранить SKU'));
          return;
        }
        ntDirty[2] = false;        // SKU/narxlar Uzumda — yo'qotadigan narsa yo'q
        var n = (res.d.skus || []).length;
        // Bandl `sku_created_successfully` (ru/uz) — Uzum matni.
        notify(tr('SKU muvaffaqiyatli yaratildi! (' + n + ')',
                  'SKU успешно созданы (' + n + ')'));
        // Bandl «Saqlash va davom etish» dan keyin 3-qadamga o'tadi
        // (/{shopId}/products/id/{productId}/edit/filters). SKU endi mavjud —
        // 3-qadam jadvali «taqiq» emas, atributlar bilan ochiladi.
        s3Enter(S2.productId);
      })
      .catch(function () {
        if (btn) { btn.disabled = false; btn.textContent = prev; }
        notify(tr('Tarmoq xatosi', 'Ошибка сети'));
      });
  }

  // ── Qadamlar orasida o'tish ─────────────────────────────────────
  state.step = 1;

  // «Chuqur tahrir» yuklagichini yashiradi (⋮-menyudan 2/3-qadam ochilganda
  // 1-qadam o'rniga ko'rsatilgan spinner). Kerakli qadam render bo'lgach yoki
  // 1-qadamга qaytilganda chaqiriladi.
  function ntHideEditLoader() {
    var el = document.getElementById('ntEditLoader');
    if (el) el.hidden = true;
  }
  function ntShowEditLoader() {
    var el = document.getElementById('ntEditLoader');
    if (el) el.hidden = false;
  }

  function s2Show(on) {
    state.step = on ? 2 : 1;
    ntHideEditLoader();
    if (step1El) step1El.hidden = on;
    if (step2El) step2El.hidden = !on;
    // Qoralama yaratilgach do'kon almashmaydi — draft do'konga qat'iy
    // bog'langan. Bir marta o'chirilsa, ortga qaytsa ham o'chiq qoladi.
    if (on) { var _ss = document.getElementById('ntShopSelect'); if (_ss) _ss.disabled = true; }
    var legend = document.querySelector('.nt-required-legend');
    if (legend) legend.hidden = on;

    if (step3El) step3El.hidden = true;   // 3-qadamdan qaytilgan bo'lishi mumkin
    paintSteps();                          // header nishonlari — yagona joyda
    window.scrollTo(0, 0);
    if (on) s2Validate(); else validate();
  }

  // Qoralama yaratilgach 2-qadamga o'tish (bandl ham shunday qiladi:
  // muvaffaqiyatdan keyin `/{shopId}/products/id/{productId}/edit/filters`).
  function s2Enter(productId) {
    state.productId = Number(productId) || null;
    return s2Load(productId).then(function (ok) {
      if (ok) {
        ntDirty[2] = false;   // serverdan yangi tortildi — toza holat
        s2Show(true);
        // Qayta yuklashga chidamli: qoralama URL'da qoladi.
        try {
          var u = new URL(window.location.href);
          u.searchParams.set('productId', String(productId));
          u.searchParams.set('shop', String(state.shop));
          window.history.replaceState({}, '', u.toString());
        } catch (e) { /* eski brauzer — URL yangilanmaydi, UI ishlaydi */ }
      } else {
        // Yuklab bo'lmadi — «chuqur tahrir» yuklagichida qotib qolmasin.
        showStep1();
      }
      return ok;
    });
  }
  root.ntEnterStep2 = s2Enter;   // 1-qadam saqlashi shu orqali chaqiradi

  /* ════════════════════════════════════════════════════════════════
   *  3-QADAM «Свойства» (xususiyatlar / filtrlar)
   *
   *  MA'LUMOT MANBAI (jonli tasdiqlangan 2026-07-18):
   *    · Jadval  — /noviy-tavar/api/step3       (filters/product/{id})
   *    · Qiymat  — /noviy-tavar/api/attr-enums  (assortment /attributes/enums)
   *    · Saqlash — /noviy-tavar/api/save-filters (filters/product POST, YOZUVCHI)
   *
   *  Bandl: chunk-1527337c (3-qadam komponenti). Qiymat serializatsiyasi
   *  bandl `RE()` (mf-products @2011646) + bo'shlik tekshiruvi `$T()`
   *  (@605568) dan SO'ZMA-SO'Z. i18n `create_filters` blokidan.
   *
   *  ⚠️ SKU yo'q bo'lsa Uzum «taqiq» ekranini ko'rsatadi (jonli i18n
   *  `filters_forbidden` tasdiqladi) — bu XATO EMAS, Uzumning O'Z xatti-harakati.
   * ════════════════════════════════════════════════════════════════ */
  var S3 = {
    productId: null,
    attrs: [],     // ustunlar (skuAttributes[0].attributes)
    sku: [],       // qatorlar (filters/product javobidagi `sku`)
    values: {},    // {skuId: {attributeCode: <raw>}}
    // Katak xatolari — {skuId: {attributeCode: true}}. Uzumdek FAQAT
    // «Yakunlash» bosilgandan keyin to'ldiriladi (oldindan qizartirilmaydi).
    errs: {},
    errShown: false,
    loading: false
  };
  // Ommaviy tahrirlash modalidagi boshqaruv ham AYNAN katak boshqaruvi —
  // shu sabab u ham «SKU» sifatida yashaydi, faqat soxta id bilan.
  var S3_BULK_SKU = { skuId: '__bulk__' };

  var step3El = document.getElementById('ntStep3');
  var ENUM_SINGLE = ['enum', 'localizableEnum'];
  var ENUM_MULTI = ['enumArray', 'localizableEnumArray'];
  // Uzum dropdowni chevroni + chip ✕ (ichki SVG — tashqi ikonka darkormas).
  var S3_CARET = '<svg width="16" height="16" viewBox="0 0 16 16" fill="none">' +
    '<path d="M4 6l4 4 4-4" stroke="currentColor" stroke-width="1.5" ' +
    'stroke-linecap="round" stroke-linejoin="round"/></svg>';
  var S3_CHIP_X = '<svg width="10" height="10" viewBox="0 0 10 10" fill="none">' +
    '<path d="M2 2l6 6M8 2l-6 6" stroke="currentColor" stroke-width="1.5" ' +
    'stroke-linecap="round"/></svg>';
  // Ustun sarlavhasidagi nishon (↕ + 3 chiziq) — referens kadrdan o'lchangan:
  // ink #ACACAC, strelka chapda, chiziqlar o'ngda qisqarib boradi.
  // ⚠️ O'LCHAMI 18px — bandl `.attribute-header__icon{width:18px;height:18px}`.
  // ⚠️ BU SARALASH EMAS: bandl `data-v-12137e6d` da shu tugma
  // `attribute-bulk-modal` ni ochadi (i18n `bulkEditTitle`). Ilgari bizda
  // saralash osilgan edi — Uzumda ustunni saralash imkoniyati YO'Q.
  var S3_BULK_ICON = '<svg width="18" height="18" viewBox="0 0 16 16" fill="none">' +
    '<path d="M4 3v10M2.2 4.9L4 3l1.8 1.9M2.2 11.1L4 13l1.8-1.9" ' +
    'stroke="currentColor" stroke-width="1.5" stroke-linecap="round" ' +
    'stroke-linejoin="round"/>' +
    '<path d="M8.5 4.5h6M8.5 8h4.5M8.5 11.5h3" stroke="currentColor" ' +
    'stroke-width="1.5" stroke-linecap="round"/></svg>';
  function s3IsEnum(vt) { return ENUM_SINGLE.indexOf(vt) >= 0 || ENUM_MULTI.indexOf(vt) >= 0; }
  function s3IsMulti(vt) { return ENUM_MULTI.indexOf(vt) >= 0; }

  // HTML'ga qo'yiladigan matnni xavfsizlash (skuTitle foydalanuvchi prefiksini
  // o'z ichiga oladi). Mavjud kod ko'pincha textContent ishlatadi; jadval
  // innerHTML bilan qurilgani uchun bu yerda kerak.
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  // ⚠️ 3-QADAM MATNLARI — HAMMASI BANDLDAN (o'zim tarjima qilmadim).
  // create_filters (ru @363208 / uz @426280) + katak matnlari QE (@2017666,
  // ru+uz bitta obyektda). L lug'atidagi s3_* bilan bir xil manba.
  var NT_S3 = {
    colProduct:  tr('Tovar', 'Товар'),
    choose:      tr('Qiymatni tanlang', 'Выберите значение'),
    chooseMulti: tr('Qiymatlarni tanlang', 'Выберите значения'),
    enterValue:  tr('Qiymatni kiriting', 'Введите значение'),
    enterNumber: tr('Raqamni kiriting', 'Введите число'),
    search:      tr('Qidirish...', 'Поиск...'),
    yes:         tr('Ha', 'Да'),
    no:          tr('Yoʻq', 'Нет'),
    // Bandl BooleanField uchinchi varianti — bo'sh qiymatning YORLIG'I.
    dash:        '—',
    // Bandl i18n `value` — ochiq dropdown filtri placeholder'i.
    valueWord:   tr('Qiymat', 'Значение'),
    // ⚠️ 3-QADAMDA TUGMA MATNI BOSHQA: bandl umumiy i18n `finish`
    // («Yakunlash» / «Завершить») — referens kadrda ham shunday. 1- va
    // 2-qadamda esa `save_and_continue` qoladi.
    finish:      tr('Yakunlash', 'Завершить'),
    maxValues:   tr('maks. qiymatlar:', 'макс. значений:'),
    // ── Ommaviy tahrirlash (ustun sarlavhasidagi nishon) ──────────
    // Bandl `assortment-kit` i18n (ru @2019442 / uz @2021501):
    //   bulkEditTitle · applyValueToAllSku {name} · applyValueToAllSkuGeneric
    bulkTitle:   tr('Ommaviy tahrirlash', 'Массовое редактирование'),
    bulkAllGen:  tr('Qiymatni barcha SKUlarga qoʻllash',
                    'Применить значение ко всем SKU'),
    // ── Xato matnlari ─────────────────────────────────────────────
    // `requiredField` (ru @2019115 / uz @2021175) — katak ostidagi qizil matn.
    requiredField: tr('Maydon toʻldirish majburiy', 'Обязательное поле'),
    // `create_filters.validation_errors` — «Yakunlash» bosilgandagi bildirishnoma.
    validationErrors: tr('Maʼlumotlar notoʻgʻri toʻldirilgan. Kiriting qiymatlarni tekshiring.',
                         'Данные заполнены некорректно. Проверьте введённые значения.'),
    fbMain:      tr('SKU kodsiz mahsulot xususiyatlarini qoʻsha olmaysiz',
                    'Вы не можете добавить свойства товаров без SKU'),
    saved:       tr('Xususiyatlar saqlangan', 'Свойства сохранены'),
    saveFail:    tr("Xususiyatlarni saqlab bo'lmadi", 'Не удалось сохранить свойства')
  };

  // Atribut/qiymat nomi — ikki til inline (assortment {uz,ru} beradi).
  function s3AttrName(a) {
    var n = a.attributeName || {};
    return tr(n.uz || n.ru || a.attributeCode, n.ru || n.uz || a.attributeCode);
  }
  function s3EnumTitle(item) {
    var v = item.value || {};
    return tr(v.uz || v.ru || item.code, v.ru || v.uz || item.code);
  }

  // Enum qiymatlar keshi (dropdown ochilganda lazy) — bandl `fetchEnumValues`.
  var s3EnumCache = {};
  function s3FetchEnums(attrCode) {
    if (s3EnumCache[attrCode]) return Promise.resolve(s3EnumCache[attrCode]);
    var url = '/noviy-tavar/api/attr-enums?shop=' + encodeURIComponent(state.shop) +
              '&attrCode=' + encodeURIComponent(attrCode);
    return fetch(url, { credentials: 'same-origin' })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d || {} }; }); })
      .then(function (res) {
        var items = (res.ok && res.d.items) ? res.d.items : [];
        s3EnumCache[attrCode] = items;
        return items;
      })
      .catch(function () { return []; });
  }

  function s3Load(productId) {
    S3.loading = true;
    S3.productId = productId;
    var url = '/noviy-tavar/api/step3?shop=' + encodeURIComponent(state.shop) +
              '&productId=' + encodeURIComponent(productId);
    return fetch(url, { credentials: 'same-origin' })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d || {} }; }); })
      .then(function (res) {
        S3.loading = false;
        if (!res.ok) {
          notify(res.d.error || tr('Xususiyatlarni yuklab boʻlmadi',
                                   'Не удалось загрузить свойства'));
          return false;
        }
        var sa = res.d.skuAttributes || [];
        S3.sku = res.d.sku || [];
        // Ustunlar hamma SKU uchun bir xil — birinchi qatordan olamiz.
        S3.attrs = (sa[0] && sa[0].attributes) ? sa[0].attributes : [];
        // Mavjud qiymatlarni tiklash (bandl: har sku qatoridagi attributeValues).
        S3.values = {};
        sa.forEach(function (rowObj) {
          var m = {};
          (rowObj.attributeValues || []).forEach(function (av) {
            if (av && av.attributeCode) m[av.attributeCode] = s3RawFromValue(av.attributeValue);
          });
          S3.values[rowObj.skuId] = m;
        });
        // Saqlangan qiymatli ENUM atributlar uchun nomlarni oldindan tortamiz —
        // aks holda chip/matn kodni (masalan «gender_female») ko'rsatib qoladi.
        var enumMap = {};
        S3.attrs.forEach(function (a) { if (s3IsEnum(a.valueType)) enumMap[a.attributeCode] = 1; });
        var need = {};
        Object.keys(S3.values).forEach(function (skuId) {
          Object.keys(S3.values[skuId] || {}).forEach(function (code) {
            if (enumMap[code] && !s3EnumCache[code]) need[code] = 1;
          });
        });
        var codes = Object.keys(need);
        if (codes.length) {
          return Promise.all(codes.map(function (c) { return s3FetchEnums(c); }))
            .then(function () { s3Render(); return true; });
        }
        s3Render();
        return true;
      })
      .catch(function () {
        S3.loading = false;
        notify(tr('Tarmoq xatosi', 'Ошибка сети'));
        return false;
      });
  }

  // Saqlangan {valueType,value} -> ichki xom qiymat (tahrirlash yo'li uchun).
  function s3RawFromValue(av) {
    if (!av || typeof av !== 'object') return null;
    if (ENUM_MULTI.indexOf(av.valueType) >= 0) return Array.isArray(av.value) ? av.value : [];
    return av.value;
  }

  // ── Katak qiymati (ichki xom) ───────────────────────────────────
  function s3Get(skuId, code) {
    return (S3.values[skuId] || (S3.values[skuId] = {}))[code];
  }
  function s3Set(skuId, code, val) {
    (S3.values[skuId] || (S3.values[skuId] = {}))[code] = val;
    // 3-qadam tahriri — faqat shu yo'l orqali o'tadi (dropdown/chip/matn),
    // s3Load esa `S3.values` ni TO'G'RIDAN-TO'G'RI to'ldiradi. Shu sababli
    // bu yerdagi bayroq «foydalanuvchi o'zgartirdi» degani.
    ntDirty[3] = true;
    // Katak to'ldirilgan bo'lsa — qizil belgi darhol ketadi (Uzumdek).
    var a = s3AttrByCode(code);
    if (a && !s3IsEmpty(a.valueType, val)) s3ClearCellError(skuId, code);
    // Qiymat o'zgardi — majburiy-atribut gate + hint qayta hisoblanadi.
    s3SyncSave();
  }

  function s3AttrByCode(code) {
    for (var i = 0; i < S3.attrs.length; i++) {
      if (S3.attrs[i].attributeCode === code) return S3.attrs[i];
    }
    return null;
  }

  // ── Katak xatosi («Обязательное поле») ──────────────────────────
  // Uzum bu belgini FAQAT «Yakunlash» bosilgandan keyin ko'rsatadi va katak
  // to'ldirilishi bilan o'chiradi (bandl `setAttributeErrors`/`clearAttributeErrors`).
  function s3HasErr(skuId, code) {
    return !!(S3.errs[skuId] && S3.errs[skuId][code]);
  }

  function s3ClearCellError(skuId, code) {
    if (!s3HasErr(skuId, code)) return;
    delete S3.errs[skuId][code];
    // Katakni QAYTA QURMAYMIZ — ochiq dropdown shu boshqaruvga bog'langan,
    // qayta qurilsa tanlov o'rtasida uzilib qolardi.
    var td = document.querySelector('#ntS3Rows .nt-s3-td[data-sku="' + skuId +
                                    '"][data-code="' + code + '"]');
    if (!td) return;
    td.classList.remove('has-error');
    var msg = td.querySelector('.nt-s3-cell-err');
    if (msg && msg.parentNode) msg.parentNode.removeChild(msg);
  }

  function s3PaintCellError(td, sk, a) {
    if (!td || !td.classList) return;
    var on = s3HasErr(sk.skuId, a.attributeCode);
    td.classList.toggle('has-error', on);
    var msg = td.querySelector('.nt-s3-cell-err');
    if (!on) {
      if (msg && msg.parentNode) msg.parentNode.removeChild(msg);
      return;
    }
    if (msg) return;
    var m = document.createElement('span');
    m.className = 'nt-s3-cell-err';
    m.textContent = NT_S3.requiredField;
    td.appendChild(m);
  }

  // «Yakunlash» bosilganda — bo'sh MAJBURIY kataklarni belgilaydi.
  // Qaytaradi: belgilangan kataklar soni.
  function s3MarkRequiredErrors() {
    S3.errs = {};
    var n = 0;
    S3.attrs.forEach(function (a) {
      if (!a.required) return;
      S3.sku.forEach(function (sk) {
        if (!s3IsEmpty(a.valueType, s3Get(sk.skuId, a.attributeCode))) return;
        (S3.errs[sk.skuId] || (S3.errs[sk.skuId] = {}))[a.attributeCode] = true;
        n++;
      });
    });
    S3.errShown = true;
    s3Render();
    return n;
  }

  // ── Jadval renderi ──────────────────────────────────────────────
  function s3MaxLabel(n) {
    // limits.max_values — «maks. qiymatlar: {n}» / «макс. значений: {n}»
    return (NT_S3.maxValues || '') + ' ' + n;
  }

  // ── Ommaviy tahrirlash (ustun sarlavhasidagi nishon) ────────────
  // Bandl `data-v-12137e6d`: nishon -> `attribute-bulk-modal`; modal ichidagi
  // boshqaruv KATAKNIKI bilan AYNAN bir xil, «Применить» esa qiymatni BARCHA
  // SKU'ga yozadi. Modal soxta «__bulk__» SKU'da ishlaydi — shu tufayli
  // s3FillCell/s3OpenEnum ni qayta yozmasdan ishlatamiz.
  var s3BulkModal = document.getElementById('ntS3BulkModal');
  var s3BulkAttr = null;

  function s3OpenBulk(ai) {
    var a = S3.attrs[ai];
    if (!a || !s3BulkModal) return;
    s3BulkAttr = a;
    // Oldingi qiymat qolib ketmasin.
    S3.values[S3_BULK_SKU.skuId] = {};

    var sub = document.getElementById('ntS3BulkSub');
    if (sub) {
      var nm = s3AttrName(a);
      // `applyValueToAllSku`: «{name}» qiymatini barcha SKUlarga qoʻllash /
      // Применить значение «{name}» ко всем SKU
      sub.textContent = nm
        ? tr('«' + nm + '» qiymatini barcha SKUlarga qoʻllash',
             'Применить значение «' + nm + '» ко всем SKU')
        : NT_S3.bulkAllGen;
    }
    var lim = document.getElementById('ntS3BulkLimits');
    if (lim) {
      var mv = (a.dataProperties || {}).maxValues;
      var show = s3IsMulti(a.valueType) && mv;
      lim.textContent = show ? s3MaxLabel(mv) : '';
      lim.hidden = !show;
    }
    var field = document.getElementById('ntS3BulkField');
    if (field) {
      field.innerHTML = '';
      s3FillCell(field, S3_BULK_SKU, a);
    }
    s3BulkModal.hidden = false;
  }

  function s3CloseBulk() {
    if (s3BulkModal) s3BulkModal.hidden = true;
    s3CloseEnum();
    s3BulkAttr = null;
    delete S3.values[S3_BULK_SKU.skuId];
  }

  function s3ApplyBulk() {
    var a = s3BulkAttr;
    if (!a) { s3CloseBulk(); return; }
    var val = s3Get(S3_BULK_SKU.skuId, a.attributeCode);
    S3.sku.forEach(function (sk) {
      // Massiv — HAR SKU'ga nusxa (bitta havolani bo'lishsa, bitta chip
      // o'chirilganda hamma qatordan yo'qolardi).
      s3Set(sk.skuId, a.attributeCode, Array.isArray(val) ? val.slice() : val);
    });
    s3CloseBulk();
    s3Render();
    s3SyncSave();
  }

  if (s3BulkModal) {
    var bCancel = document.getElementById('ntS3BulkCancel');
    var bClose = document.getElementById('ntS3BulkClose');
    var bApply = document.getElementById('ntS3BulkApply');
    if (bCancel) bCancel.addEventListener('click', s3CloseBulk);
    if (bClose) bClose.addEventListener('click', s3CloseBulk);
    if (bApply) bApply.addEventListener('click', s3ApplyBulk);
    s3BulkModal.addEventListener('click', function (e) {
      if (e.target === s3BulkModal) s3CloseBulk();   // fon bosilsa yopiladi
    });
  }

  function s3Render() {
    var head = document.getElementById('ntS3Head');
    var body = document.getElementById('ntS3Rows');
    var wrap = document.getElementById('ntS3TblWrap');
    var forb = document.getElementById('ntS3Forbidden');
    if (!head || !body) return;

    // SKU yo'q -> «taqiq» ekrani (Uzumdek).
    var hasSku = S3.sku.length > 0 && S3.attrs.length > 0;
    if (forb) forb.hidden = hasSku;
    if (wrap) wrap.hidden = !hasSku;
    var save = document.getElementById('ntSave');
    if (save && state.step === 3) save.disabled = !hasSku;
    if (!hasSku) return;

    // Sarlavha: «Товar» + har atribut (+ Uzumdek ↕ saralash nishoni).
    var ths = ['<th class="nt-th nt-th--sticky">' + esc(NT_S3.colProduct) + '</th>'];
    S3.attrs.forEach(function (a, ai) {
      var sub = '';
      var mv = (a.dataProperties || {}).maxValues;
      if (s3IsMulti(a.valueType) && mv) {
        sub = '<span class="nt-s3-th-sub">' + esc(s3MaxLabel(mv)) + '</span>';
      }
      var req = a.required ? '<span class="nt-req" aria-hidden="true">*</span>' : '';
      ths.push('<th class="nt-th nt-s3-th"><span class="nt-s3-th-in">' +
        '<span class="nt-s3-th-lab">' + esc(s3AttrName(a)) + req + sub + '</span>' +
        '<button type="button" class="nt-s3-bulk-btn" data-ai="' + ai + '" ' +
        'aria-label="' + esc(NT_S3.bulkTitle) + '" title="' + esc(NT_S3.bulkTitle) + '">' +
        S3_BULK_ICON + '</button></span></th>');
    });
    head.innerHTML = ths.join('');
    head.querySelectorAll('.nt-s3-bulk-btn').forEach(function (btn) {
      btn.addEventListener('click', function (e) {
        e.stopPropagation();
        s3OpenBulk(Number(btn.getAttribute('data-ai')));
      });
    });

    // Qatorlar: har SKU.
    body.innerHTML = '';
    S3.sku.forEach(function (sk) {
      var tr_ = document.createElement('tr');
      var tds = ['<td class="nt-td nt-td--sticky" data-prod="' + esc(String(sk.skuId)) +
                 '">' + s3ProductCell(sk) + '</td>'];
      S3.attrs.forEach(function (a) {
        tds.push('<td class="nt-td nt-s3-td" data-sku="' + esc(String(sk.skuId)) +
                 '" data-code="' + esc(a.attributeCode) + '"></td>');
      });
      tr_.innerHTML = tds.join('');
      body.appendChild(tr_);
      // Kataklarni to'ldirish (DOM element sifatida — hodisa bog'lash uchun).
      S3.attrs.forEach(function (a, ci) {
        s3FillCell(tr_.children[ci + 1], sk, a);
      });
    });
  }

  // rang nomi — `characteristics` "[uz: Alvon, ru: Алый]" dan tanlangan tilda.
  function s3ColorName(sk) {
    var c = String(sk.characteristics || '');
    var re = (lang === 'ru') ? /ru:\s*([^,\]]+)/ : /uz:\s*([^,\]]+)/;
    var m = c.match(re);
    if (m && m[1]) return m[1].trim();
    // Fallback — skuTitle oxirgi bo'lagi (rang odatda oxirida).
    var parts = String(sk.skuTitle || '').split('-');
    return parts.length ? parts[parts.length - 1] : String(sk.skuTitle || '');
  }

  // Shu QATORDA to'ldirilmagan majburiy atributlar (qizil «!» nishoni uchun).
  // Bandl `sku-errors-hint`: nishon qator ichida turadi va tooltip'da qaysi
  // atribut xato ekani sanaladi. Referens kadrda har qatorda ko'rinadi.
  function s3RowMissing(sk) {
    var out = [];
    S3.attrs.forEach(function (a) {
      if (!a.required) return;
      if (s3IsEmpty(a.valueType, s3Get(sk.skuId, a.attributeCode))) out.push(s3AttrName(a));
    });
    return out;
  }

  function s3ProductCell(sk) {
    // Uzum ko'rinishi: rasm + rang nomi (tepa) + to'liq skuTitle (ost) + «!».
    var color = s3ColorName(sk);
    var skuT = String(sk.skuTitle || '');
    var img = sk.imageLow || sk.imageHigh || '';
    var imgHtml = img
      ? '<img class="nt-s3-prod-img" src="' + esc(img) + '" alt="" loading="lazy">'
      : '<span class="nt-s3-prod-img is-empty" aria-hidden="true"></span>';
    var miss = s3RowMissing(sk);
    var err = miss.length
      ? '<span class="nt-s3-err" data-sku="' + esc(String(sk.skuId)) + '" role="img" ' +
        'aria-label="' + esc(tr('Toʻldirilmagan majburiy xususiyatlar',
                                'Незаполненные обязательные свойства')) + '" ' +
        'title="' + esc(tr('Toʻldiring: ', 'Заполните: ') + miss.join(', ')) + '">!</span>'
      : '';
    return '<div class="nt-s3-prod">' + imgHtml +
      '<span class="nt-s3-prod-txt">' +
        '<span class="nt-s3-prod-top">' + esc(color || skuT) + '</span>' +
        (skuT ? '<span class="nt-s3-prod-sub">' + esc(skuT) + '</span>' : '') +
      '</span>' + err + '</div>';
  }

  // Bitta katakni to'ldirish — valueType bo'yicha turli boshqaruv.
  function s3FillCell(td, sk, a) {
    if (!td) return;
    var vt = a.valueType;
    var raw = s3Get(sk.skuId, a.attributeCode);
    // ⚠️ MANTIQIY (boolean) atribut ham DROPDOWN — segment tugma EMAS.
    // JONLI DALIL (bandl `BooleanField`, mf-products @2022149):
    //   u-select type:"single", input-variant:"outlined", size:"medium",
    //   values: [{yes,true},{no,false},{"—",null}], showClearIcon:false
    // Ilgari bu yerda binafsha «Ha|Yoʻq» segmenti chizilardi — Uzumda bunday
    // boshqaruv YO'Q, jadvaldagi hamma katak bir xil ko'rinishli dropdown.
    if (s3IsEnum(vt) || vt === 'boolean') {
      // Uzum kombosi: div konteyner (chip'lar ichkaridan bo'ladi — <button>
      // ichida <button> yaroqsiz) + chevron. Enter/Space bilan ham ochiladi.
      var combo = document.createElement('div');
      combo.className = 'nt-s3-combo';
      combo.setAttribute('role', 'button');
      combo.tabIndex = 0;
      var body = document.createElement('div');
      body.className = 'nt-s3-combo-body';
      var caret = document.createElement('span');
      caret.className = 'nt-s3-combo-caret';
      caret.setAttribute('aria-hidden', 'true');
      caret.innerHTML = S3_CARET;
      combo.appendChild(body);
      combo.appendChild(caret);
      s3ComboLabel(combo, sk, a);
      combo.addEventListener('click', function (e) {
        // chip ✕ bosilsa dropdown ochilmaydi (faqat o'chirish).
        if (e.target.closest && e.target.closest('.nt-s3-chip-x')) return;
        e.stopPropagation();
        s3OpenEnum(combo, sk, a);
      });
      combo.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); s3OpenEnum(combo, sk, a); }
      });
      td.appendChild(combo);
    } else if (vt === 'numeric') {
      var dp = a.dataProperties || {};
      var wrapN = document.createElement('div');
      wrapN.className = 'nt-s3-num';
      var inp = document.createElement('input');
      inp.className = 'nt-input';
      inp.type = 'text';
      inp.inputMode = dp.decimal ? 'decimal' : 'numeric';
      inp.placeholder = NT_S3.enterNumber;
      if (raw != null) inp.value = String(raw);
      inp.addEventListener('input', function () {
        s3Set(sk.skuId, a.attributeCode, inp.value.trim() === '' ? null : inp.value.trim());
      });
      wrapN.appendChild(inp);
      var unit = ((dp.unit || {}).primaryUnit) || '';
      if (unit) {
        var u = document.createElement('span');
        u.className = 'nt-s3-unit';
        u.textContent = s3UnitLabel(unit);
        wrapN.appendChild(u);
      }
      td.appendChild(wrapN);
    } else {
      // string / localizableString — oddiy matn kiritish.
      var t = document.createElement('input');
      t.className = 'nt-input';
      t.type = 'text';
      t.placeholder = NT_S3.enterValue;
      if (raw != null) t.value = (typeof raw === 'object') ? (raw.ru || raw.uz || '') : String(raw);
      t.addEventListener('input', function () {
        s3Set(sk.skuId, a.attributeCode, t.value.trim() === '' ? null : t.value.trim());
      });
      td.appendChild(t);
    }
    // Bo'sh majburiy katak «Yakunlash» dan keyin qizil ramka + matn oladi.
    s3PaintCellError(td, sk, a);
  }

  function s3UnitLabel(unit) {
    // Bandl unit -> qisqartma (length_millimeter -> mm, weight_gram -> g).
    if (/millimeter|_mm/.test(unit)) return tr('mm', 'мм');
    if (/gram|_g\b/.test(unit)) return tr('g', 'г');
    return '';
  }

  // Enum combo yorlig'i — Uzum ko'rinishi: multi → o'chiriladigan chip'lar
  // (Женский ✕), single → matn, bo'sh → placeholder. Konteyner `.nt-s3-combo`.
  function s3ComboLabel(combo, sk, a) {
    var body = combo.querySelector && combo.querySelector('.nt-s3-combo-body');
    if (!body) return;
    var multi = s3IsMulti(a.valueType);
    var raw = s3Get(sk.skuId, a.attributeCode);
    var cache = s3EnumCache[a.attributeCode] || [];
    function titleOf(code) {
      for (var i = 0; i < cache.length; i++) if (cache[i].code === code) return s3EnumTitle(cache[i]);
      return code;
    }
    body.innerHTML = '';
    // Mantiqiy atribut — bandl `BooleanField`: bo'sh qiymat ham TANLANGAN
    // variant («—») sifatida ko'rsatiladi, placeholder emas
    // (`get: () => options.find(...) ?? options[2]`, options[2] = {"—", null}).
    if (a.valueType === 'boolean') {
      combo.classList.remove('is-placeholder');
      var bv = document.createElement('span');
      bv.className = 'nt-s3-combo-val';
      bv.textContent = (raw === true) ? NT_S3.yes : (raw === false) ? NT_S3.no : NT_S3.dash;
      body.appendChild(bv);
      return;
    }
    if (multi && Array.isArray(raw) && raw.length) {
      combo.classList.remove('is-placeholder');
      raw.forEach(function (code) {
        var chip = document.createElement('span');
        chip.className = 'nt-s3-chip';
        var t = document.createElement('span');
        t.className = 'nt-s3-chip-t';
        t.textContent = titleOf(code);
        var x = document.createElement('button');
        x.type = 'button';
        x.className = 'nt-s3-chip-x';
        x.setAttribute('aria-label', tr('Oʻchirish', 'Удалить'));
        x.innerHTML = S3_CHIP_X;
        x.addEventListener('click', function (e) {
          e.stopPropagation();
          s3RemoveMulti(sk, a, code, combo);
        });
        chip.appendChild(t); chip.appendChild(x);
        body.appendChild(chip);
      });
      return;
    }
    if (!multi && raw) {
      combo.classList.remove('is-placeholder');
      var v = document.createElement('span');
      v.className = 'nt-s3-combo-val';
      v.textContent = titleOf(raw);
      body.appendChild(v);
      return;
    }
    combo.classList.add('is-placeholder');
    var ph = document.createElement('span');
    ph.className = 'nt-s3-combo-ph';
    ph.textContent = multi ? NT_S3.chooseMulti : NT_S3.choose;
    body.appendChild(ph);
  }

  // Chip ✕ — bitta multi qiymatini olib tashlaydi (dropdown ochilmaydi).
  function s3RemoveMulti(sk, a, code, combo) {
    var raw = s3Get(sk.skuId, a.attributeCode);
    var arr = Array.isArray(raw) ? raw.slice() : [];
    var i = arr.indexOf(code);
    if (i >= 0) arr.splice(i, 1);
    s3Set(sk.skuId, a.attributeCode, arr.length ? arr : null);
    s3RefreshCombo(combo, sk, a);
    // Shu atribut dropdowni ochiq bo'lsa — belgilangan holatni yangilaymiz.
    if (s3PopCtx && s3PopCtx.btn === combo && s3Pop && !s3Pop.hidden) {
      s3RenderPop(s3FilterValue());
    }
  }

  // ── Enum dropdown popoveri (bitta nusxa) ────────────────────────
  var s3Pop = document.getElementById('ntS3Pop');
  var s3PopList = document.getElementById('ntS3PopList');
  var s3PopSearch = document.getElementById('ntS3Search');
  var s3PopCtx = null;   // {btn, sk, a}

  function s3OpenEnum(btn, sk, a) {
    if (s3PopCtx && s3PopCtx.btn && s3PopCtx.btn !== btn) s3CloseEnum();
    s3PopCtx = { btn: btn, sk: sk, a: a };
    if (s3PopSearch) s3PopSearch.value = '';
    btn.classList.add('is-open');          // chevron teskari + fokus halqasi
    s3PositionPop(btn);
    if (s3Pop) s3Pop.hidden = false;
    // ⚠️ QIDIRUV MAYDONI PANELDA EMAS, TUGMANING ICHIDA.
    // JONLI DALIL: sacvoyage-step3-operating-system-dropdown.png — dropdown
    // ochilganda tugma matn maydoniga aylanadi («Значение…» placeholder'i
    // bilan), ro'yxatda esa alohida qidiruv qatori YO'Q. Bandl ham shunday:
    // `EnumField` u-select'ga `filter:{placeholder: t("search")}` beradi
    // (u-select filtrni O'Z inputiga qo'yadi), `BooleanField` esa bermaydi.
    var isBool = a.valueType === 'boolean';
    if (isBool) { s3RenderPop(''); return; }
    s3MountFilter(btn, sk, a);
    if (s3PopList) s3PopList.innerHTML = '<p class="nt-s3-pop-empty">' + esc(NT_S3.search) + '</p>';
    s3FetchEnums(a.attributeCode).then(function () {
      if (s3PopCtx && s3PopCtx.btn === btn) s3RenderPop(s3FilterValue());
    });
  }

  // Tugma ichidagi filtr inputi (Uzum u-select'ining ochiq holati).
  function s3MountFilter(combo, sk, a) {
    var body = combo.querySelector('.nt-s3-combo-body');
    if (!body || body.querySelector('.nt-s3-combo-filter')) return;
    // Bitta tanlovli maydonda ochilganda joriy MATN o'rniga input turadi;
    // ko'p tanlovlida chip'lar qoladi va input ular yonига qo'shiladi.
    if (!s3IsMulti(a.valueType)) {
      body.innerHTML = '';
    } else {
      // ⚠️ Ko'p tanlovda chip'lar qoladi, LEKIN placeholder span'ini olib
      // tashlaymiz. Aks holda bo'sh multi ochilganda «Qiymatlarni tanlang»
      // yorlig'i input placeholder'i «Qiymat» bilan yonma-yon chiqib
      // ikkilanardi (flex-wrap body). Uzumda ochilganda faqat input turadi.
      var phEl = body.querySelector('.nt-s3-combo-ph');
      if (phEl) phEl.parentNode.removeChild(phEl);
    }
    var inp = document.createElement('input');
    inp.className = 'nt-s3-combo-filter';
    inp.type = 'text';
    inp.autocomplete = 'off';
    // Ochiq holatdagi placeholder — «Qiymat» / «Значение» (bandl i18n `value`).
    // Referens kadrda tugma ochilganda aynan shu matn turadi, «Qiymatni
    // tanlang» EMAS (u yopiq holatning yorlig'i).
    inp.placeholder = NT_S3.valueWord;
    inp.addEventListener('click', function (e) { e.stopPropagation(); });
    inp.addEventListener('input', function () { s3RenderPop(inp.value.trim()); });
    inp.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { e.stopPropagation(); s3CloseEnum(); }
    });
    body.appendChild(inp);
    inp.focus();
  }

  function s3FilterValue() {
    var inp = s3PopCtx && s3PopCtx.btn
      ? s3PopCtx.btn.querySelector('.nt-s3-combo-filter') : null;
    return inp ? inp.value.trim() : '';
  }

  // Yorliqni qayta chizadi va dropdown OCHIQ bo'lsa filtr inputini tiklaydi
  // (ko'p tanlovda chip qo'shilgach input yo'qolib qolmasin — Uzumda ochiq
  // dropdown chip qo'shgandan keyin ham yozishda davom etadi).
  function s3RefreshCombo(combo, sk, a) {
    var open = combo.classList.contains('is-open');
    var keep = open ? s3FilterValue() : '';
    s3ComboLabel(combo, sk, a);
    if (open && a.valueType !== 'boolean') {
      s3MountFilter(combo, sk, a);
      var inp = combo.querySelector('.nt-s3-combo-filter');
      if (inp && keep) inp.value = keep;
    }
  }

  function s3PositionPop(btn) {
    if (!s3Pop) return;
    var r = btn.getBoundingClientRect();
    s3Pop.style.top = (window.scrollY + r.bottom + 4) + 'px';
    s3Pop.style.left = (window.scrollX + r.left) + 'px';
    s3Pop.style.minWidth = Math.max(220, r.width) + 'px';
  }

  function s3RenderPop(q) {
    if (!s3PopCtx || !s3PopList) return;
    var a = s3PopCtx.a, sk = s3PopCtx.sk;
    // Mantiqiy atribut — bandl BooleanField'ning AYNAN uchta varianti:
    //   [{yes,true}, {no,false}, {"—", null}]
    if (a.valueType === 'boolean') {
      var cur = s3Get(sk.skuId, a.attributeCode);
      s3PopList.innerHTML = '';
      [[NT_S3.yes, true], [NT_S3.no, false], [NT_S3.dash, null]].forEach(function (o) {
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'nt-s3-opt' + ((cur === o[1] || (cur == null && o[1] === null)) ? ' is-on' : '');
        b.textContent = o[0];
        b.addEventListener('click', function () {
          s3Set(sk.skuId, a.attributeCode, o[1]);
          if (s3PopCtx && s3PopCtx.btn) s3ComboLabel(s3PopCtx.btn, sk, a);
          s3CloseEnum();
        });
        s3PopList.appendChild(b);
      });
      return;
    }
    var multi = s3IsMulti(a.valueType);
    var maxV = (a.dataProperties || {}).maxValues || 0;
    var items = (s3EnumCache[a.attributeCode] || []).filter(function (it) {
      if (!q) return true;
      return s3EnumTitle(it).toLowerCase().indexOf(q.toLowerCase()) >= 0;
    });
    if (!items.length) {
      s3PopList.innerHTML = '<p class="nt-s3-pop-empty">' +
        esc(tr('Hech narsa topilmadi', 'Ничего не найдено')) + '</p>';
      return;
    }
    var raw = s3Get(sk.skuId, a.attributeCode);
    var sel = multi ? (Array.isArray(raw) ? raw : []) : (raw ? [raw] : []);
    s3PopList.innerHTML = '';
    items.forEach(function (it) {
      var on = sel.indexOf(it.code) >= 0;
      var opt = document.createElement('button');
      opt.type = 'button';
      opt.className = 'nt-s3-opt' + (on ? ' is-on' : '');
      opt.textContent = s3EnumTitle(it);
      opt.addEventListener('click', function () {
        s3ToggleValue(sk, a, it.code, multi, maxV);
      });
      s3PopList.appendChild(opt);
    });
  }

  function s3ToggleValue(sk, a, code, multi, maxV) {
    var raw = s3Get(sk.skuId, a.attributeCode);
    if (multi) {
      var arr = Array.isArray(raw) ? raw.slice() : [];
      var i = arr.indexOf(code);
      if (i >= 0) arr.splice(i, 1);
      else {
        if (maxV && arr.length >= maxV) {
          notify(s3MaxLabel(maxV));
          return;
        }
        arr.push(code);
      }
      s3Set(sk.skuId, a.attributeCode, arr.length ? arr : null);
      // Combo yorlig'i + filtr inputi (ochiq qoladi — Uzumdek).
      if (s3PopCtx && s3PopCtx.btn) s3RefreshCombo(s3PopCtx.btn, sk, a);
      s3RenderPop(s3FilterValue());
      return;
    }
    var next = (raw === code) ? null : code;
    s3Set(sk.skuId, a.attributeCode, next);
    s3CloseEnum();   // yopilishda yorliq o'zi tiklanadi
  }

  function s3CloseEnum() {
    if (s3Pop) s3Pop.hidden = true;
    // Filtr inputini olib tashlab, yorliqni (chip/qiymat/placeholder) tiklaymiz.
    if (s3PopCtx && s3PopCtx.btn) {
      s3PopCtx.btn.classList.remove('is-open');
      if (s3PopCtx.a.valueType !== 'boolean') {
        s3ComboLabel(s3PopCtx.btn, s3PopCtx.sk, s3PopCtx.a);
      }
    }
    s3PopCtx = null;
  }
  // (Paneldagi eski qidiruv qatori olib tashlandi — filtr endi tugma ichida.)
  //
  // ⚠️ Panel ichidagi bosishlar hujjatga CHIQMASLIGI kerak. Aks holda ko'p
  // tanlovli atributda qiymat bosilgach ro'yxat qayta chiziladi, bosilgan
  // tugma DOM'dan chiqib ketadi va pastdagi tekshiruv (`s3Pop.contains`)
  // uni «tashqarida» deb topib, dropdown'ni yopib qo'yardi (jonli ushlandi:
  // chip qo'shilgach panel yopilib ketardi, Uzumda esa ochiq qoladi).
  if (s3Pop) {
    s3Pop.addEventListener('click', function (e) { e.stopPropagation(); });
  }
  document.addEventListener('click', function (e) {
    if (s3Pop && !s3Pop.hidden && !s3Pop.contains(e.target)) s3CloseEnum();
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && s3Pop && !s3Pop.hidden) s3CloseEnum();
  });

  // ── Saqlash tanasi — bandl `RE()` serializatori + `$T()` bo'shlik ─
  function s3IsEmpty(vt, raw) {
    if (raw == null) return true;
    if (ENUM_MULTI.indexOf(vt) >= 0) return !Array.isArray(raw) || raw.length === 0;
    if (vt === 'string' || vt === 'html') return String(raw).trim() === '';
    if (vt === 'numeric') return String(raw).trim() === '';
    return false;   // boolean false — bo'sh EMAS
  }

  function s3Serialize(a, raw) {
    var vt = a.valueType;
    if (s3IsEmpty(vt, raw)) return null;   // bandl $T -> null
    if (vt === 'numeric') {
      var dp = a.dataProperties || {};
      var num = Number(String(raw).replace(',', '.'));
      var out = { valueType: vt, value: isFinite(num) ? num : null };
      if (dp.unit) { out.unitType = dp.unit.unitType; out.unit = dp.unit.primaryUnit; }
      return out;
    }
    if (vt === 'boolean') return { valueType: vt, value: !!raw };
    // enum / enumArray / string — value xom holda (enum: code; array: [codes]).
    return { valueType: vt, value: raw };
  }

  // ⚠️ s3MissingRequired() OLIB TASHLANDI — u «qaysi ustunlar bo'sh» ro'yxatini
  // qaytarib, tugmani o'chirish + hint qatori uchun ishlatilardi. Uzumda gate
  // KATAK darajasida: `s3MarkRequiredErrors()` har (SKU × majburiy atribut)
  // katakni belgilaydi. JONLI dalil (2026-07-18) o'z kuchida qoladi: bo'sh
  // majburiy atribut → save-filters 400 «Qiymatni to'ldiring», shuning uchun
  // POST oldidan tekshiruv SAQLANDI, faqat ko'rinishi Uzumnikiga o'tdi.

  // Qatorlardagi qizil «!» nishonini qayta chizadi (qiymat o'zgargach).
  function s3PaintRowErrors() {
    var cells = document.querySelectorAll('#ntS3Rows .nt-td--sticky[data-prod]');
    for (var i = 0; i < cells.length; i++) {
      var id = cells[i].getAttribute('data-prod');
      var sk = null;
      for (var j = 0; j < S3.sku.length; j++) {
        if (String(S3.sku[j].skuId) === id) { sk = S3.sku[j]; break; }
      }
      if (sk) cells[i].innerHTML = s3ProductCell(sk);
    }
  }

  function s3SyncSave() {
    // ⚠️ UZUMDEK: «Yakunlash» bo'sh majburiy maydonlar uchun O'CHIRILMAYDI —
    // u bosiladi, keyin kataklar qizaradi (s3MarkRequiredErrors) va
    // `validation_errors` bildirishnomasi chiqadi. Ilgari bu yerda tugma
    // o'chirilib, ustiga «Majburiy — toʻldiring: …» hint qatori chizilardi;
    // Uzumda bunday qator YO'Q.
    if (state.step !== 3) return;
    s3PaintRowErrors();
    var save = document.getElementById('ntSave');
    if (save) save.disabled = !(S3.sku.length && S3.attrs.length);
  }

  function s3Send() {
    if (!S3.sku.length || !S3.attrs.length) {
      notify(NT_S3.fbMain);
      return;
    }
    // MAJBURIY atributlar gate — chala yuborilса Uzum 400 beradi.
    // UZUMDEK: bo'sh kataklar qizaradi + ostida «Обязательное поле», tepada
    // esa `create_filters.validation_errors` bildirishnomasi.
    if (s3MarkRequiredErrors()) {
      notify(NT_S3.validationErrors);
      return;
    }
    var skuAttributeValues = S3.sku.map(function (sk) {
      return {
        skuId: sk.skuId,
        attributes: S3.attrs.map(function (a) {
          return {
            attributeCode: a.attributeCode,
            // Bandl attributeName -> faqat ru.
            attributeName: (a.attributeName || {}).ru || a.attributeCode,
            attributeValue: s3Serialize(a, s3Get(sk.skuId, a.attributeCode))
          };
        })
      };
    });

    var btn = document.getElementById('ntSave');
    var prev = btn ? btn.textContent : '';
    if (btn) { btn.disabled = true; btn.textContent = tr('Saqlanmoqda...', 'Сохранение...'); }

    fetch('/noviy-tavar/api/save-filters', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({
        shop: state.shop,
        productId: S3.productId,
        skuFilters: [],
        skuAttributeValues: skuAttributeValues
      })
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d || {} }; }); })
      .then(function (res) {
        if (btn) { btn.disabled = false; btn.textContent = prev; }
        if (!res.ok) {
          notify(res.d.error || NT_S3.saveFail);
          return;
        }
        ntDirty[3] = false;        // xususiyatlar Uzumda — ogohlantirish shart emas
        // Bandl `properties_saved` — Uzum matni.
        notify(NT_S3.saved);
        // ── YAKUN: karta to'liq yaratildi. Endi «Mahsulotlar» (/groups) ni
        // AYNAN shu do'kon tanlangan holda ochamiz (foydalanuvchi talabi
        // 2026-07-25 — ilgari sahifada qolib ketardi). Do'kon tanlovi barcha
        // sahifalar o'rtasida `sh_shop` cookie orqali ulashiladi (uzum_id
        // saqlaydi; /groups shundan o'qiydi — products/routes.py). Uni
        // sales.html'dagi `saveShopCookie` bilan BIR XIL yozamiz.
        try {
          document.cookie = 'sh_shop=' + encodeURIComponent(state.shop || '') +
                            '; path=/; max-age=31536000; SameSite=Lax';
        } catch (e) { /* cookie o'chiq — baribir o'tamiz, /groups fallback qiladi */ }
        ntBypassGuard = true;      // qo'riqchi jim (holat allaqachon toza, ishonch uchun)
        // Qisqa kechikish — «Xususiyatlar saqlangan» toast'i ko'rinib ulgursin.
        setTimeout(function () { window.location.assign('/groups'); }, 900);
      })
      .catch(function () {
        if (btn) { btn.disabled = false; btn.textContent = prev; }
        notify(tr('Tarmoq xatosi', 'Ошибка сети'));
      });
  }

  // ── Qadam almashish ─────────────────────────────────────────────
  function s3Show(on) {
    state.step = on ? 3 : 2;
    ntHideEditLoader();
    if (step1El) step1El.hidden = true;
    if (step2El) step2El.hidden = on;
    if (step3El) step3El.hidden = !on;
    var legend = document.querySelector('.nt-required-legend');
    if (legend) legend.hidden = true;
    paintSteps();
    window.scrollTo(0, 0);
    var save = document.getElementById('ntSave');
    if (save) save.disabled = on ? !(S3.sku.length && S3.attrs.length) : false;
    // 3-qadamda majburiy-atribut gate + hint (Save'ni yana o'chirishi mumkin).
    if (on) s3SyncSave();
  }

  function s3Enter(productId) {
    state.productId = Number(productId) || null;
    return s3Load(productId).then(function (ok) {
      if (ok) {
        ntDirty[3] = false;   // serverdan yangi tortildi — toza holat
        s3Show(true);
        try {
          var u = new URL(window.location.href);
          u.searchParams.set('productId', String(productId));
          u.searchParams.set('shop', String(state.shop));
          u.searchParams.set('step', '3');
          window.history.replaceState({}, '', u.toString());
        } catch (e) { /* eski brauzer — URL yangilanmaydi, UI ishlaydi */ }
      } else {
        // Yuklab bo'lmadi — «chuqur tahrir» yuklagichida qotib qolmasin
        // (s3Load xato toast'ini allaqachon ko'rsatdi).
        showStep1();
      }
      return ok;
    });
  }

  // «Narxlar va SKU»ga qaytish (taqiq ekranidagi tugma).
  var s3Back = document.getElementById('ntS3Back');
  if (s3Back) {
    s3Back.addEventListener('click', function () {
      if (step3El) step3El.hidden = true;
      s2Show(true);
      try {
        var u = new URL(window.location.href);
        u.searchParams.delete('step');
        window.history.replaceState({}, '', u.toString());
      } catch (e) { /* jim o'tamiz */ }
    });
  }

  /* ════════════════════════════════════════════════════════════════
   *  HEADER QADAM NISHONLARI — bosiladi (Uzumdagidek)
   *
   *  DALIL (bandl `chunk-2e85dd89` @52129, t12.har bilan tasdiqlangan):
   *    [{card, /edit}, {prices, /edit/sku/all}, {property, /edit/filters}]
   *      .forEach((e, t) => {
   *         (t === joriy || yuklanmoqda) && (e.link = "")
   *         !isEdit && t > joriy       && (e.link = "")
   *         isEdit && !editable.isEditable && t === 0 && (e.link = "RESTRICTED")
   *      })
   *  Ya'ni: ORQAGA qaytish doim ochiq, OLDINGA sakrash yo'q (faqat o'tib
   *  bo'lingan qadamga), joriy qadam esa umuman bosilmaydi.
   *
   *  t12.har (195 yozuv, 6+ marta qadam almashtirilgan): har bosishda faqat
   *  O'QISH so'rovlari ketadi, hech narsa saqlanmaydi. Bizda ham shunday —
   *  s2Enter/s3Enter serverdan qayta yuklaydi.
   * ════════════════════════════════════════════════════════════════ */

  // Qaysi qadamgacha borilgan (nishonlarni «bajarildi» qilish uchun ham).
  state.maxStep = 1;

  function stepReachable(n) {
    if (n === state.step) return false;             // joriy qadam — bosilmaydi
    if (n === 1) {
      // 1-qadam ochiq bo'ladi, agar:
      //   · yangi yaratish (productId yo'q) — forma o'sha yerda; YOKI
      //   · karta MA'LUMOTI bu sahifada bor (cardFilled — yangi yaratishda
      //     saqlangan forma); YOKI
      //   · MAVJUD kartani tahrirlayapmiz (editingProduct) — bosilganda
      //     `goStep(1)` s1Load bilan YUKLAB beradi, bo'sh forma ko'rsatilmaydi
      //     (shuning uchun «ustidan saqlab o'chirish» xavfi yo'q).
      // Ilgari tahrirда (productId bor, cardFilled yo'q) 1-qadam QULF edi —
      // foydalanuvchi 3-qadamdan 1-ga qayta olmasdi (shikoyat 2026-07-26).
      return !state.productId || !!state.cardFilled || !!state.editingProduct;
    }
    // Bandl: `!isEdit && t > joriy -> link=""`, bunda `isEdit = !!productId`.
    // Ya'ni karta MAVJUD bo'lsa oldinga ham o'tiladi; karta yo'q ekan
    // 2/3-qadamda ko'rsatadigan narsaning O'ZI yo'q — shu shart yetarli.
    // (3-qadam SKU'siz ochilsa Uzum «taqiq» ekranini ko'rsatadi — bizda ham.)
    return !!state.productId;
  }

  // «Saqlash va davom etish» → 3-qadamda «Yakunlash» (bandl i18n `finish`;
  // referens kadrda ham 3-qadamda «Завершить» turadi).
  var ntSaveLabelDefault = null;
  function syncSaveLabel() {
    var b = document.getElementById('ntSave');
    if (!b) return;
    if (ntSaveLabelDefault === null) ntSaveLabelDefault = b.textContent.trim();
    b.textContent = (state.step === 3) ? NT_S3.finish : ntSaveLabelDefault;
  }

  function paintSteps() {
    if (state.step > state.maxStep) state.maxStep = state.step;
    syncSaveLabel();
    var btns = document.querySelectorAll('.nt-step');
    for (var i = 0; i < btns.length; i++) {
      var n = i + 1;
      var b = btns[i];
      var open = stepReachable(n);
      b.classList.toggle('is-active', n === state.step);
      // «Bajarilgan» (yashil ✓) — referens kadrlardan chiqarilgan qoida:
      //   · o'tib bo'lingan qadam DOIM yashil
      //   · JORIY qadam ham yashil, agar karta allaqachon mavjud bo'lsa
      //     (tahrirlash oqimi: 2- va 3-kadrda joriy qadam yashil turibdi);
      //     bo'sh yaratishda esa 1-qadam KO'K raqamli qoladi (1-kadr).
      b.classList.toggle('is-done',
        n < state.step || (n === state.step && !!state.productId));
      b.classList.toggle('is-locked', !open && n !== state.step);
      b.disabled = !open;
      if (n === state.step) b.setAttribute('aria-current', 'step');
      else b.removeAttribute('aria-current');
      if (open) b.removeAttribute('title');
      else if (n !== state.step && n > 1 && !state.productId) {
        b.title = tr('Avval tovar kartasini saqlang',
                     'Сначала сохраните карточку товара');
      } else if (n === 1 && state.productId && !state.cardFilled) {
        b.title = tr('Kartochka maʼlumotlari bu sahifada yuklanmagan',
                     'Данные карточки не загружены на этой странице');
      } else {
        b.removeAttribute('title');
      }
    }
  }

  // 1-qadamga qaytish (nishon orqali). Forma DOM'da turgani uchun
  // foydalanuvchi kiritgan hamma narsa joyida qoladi.
  function showStep1() {
    state.step = 1;
    ntHideEditLoader();
    if (step1El) step1El.hidden = false;
    if (step2El) step2El.hidden = true;
    if (step3El) step3El.hidden = true;
    var legend = document.querySelector('.nt-required-legend');
    if (legend) legend.hidden = false;
    paintSteps();
    window.scrollTo(0, 0);
    validate();          // «Saqlash» tugmasi holatini 1-qadam qoidasiga qaytaradi
    try {
      var u = new URL(window.location.href);
      u.searchParams.delete('step');
      window.history.replaceState({}, '', u.toString());
    } catch (e) { /* eski brauzer — UI baribir ishlaydi */ }
  }

  function goStep(n) {
    if (!stepReachable(n)) return;
    if (n === 1) {
      // Mavjud karta hali bu sahifada yuklanmagan bo'lsa (masalan ⋮→3-qadamdan
      // kelib, 1-qadamга qaytish) — bo'sh forma emas, SERVERDAN yuklaymiz.
      if (state.productId && !state.cardFilled) {
        if (step2El) step2El.hidden = true;
        if (step3El) step3El.hidden = true;
        ntShowEditLoader();           // yuklaguncha spinner (s1Load→showStep1 yopadi)
        s1Load(state.productId);
      } else {
        showStep1();
      }
      return;
    }
    if (n === 2) { s2Enter(state.productId); return; }
    if (n === 3) { s3Enter(state.productId); return; }
  }

  (function () {
    var nav = document.querySelector('.nt-steps');
    if (!nav) return;
    nav.addEventListener('click', function (ev) {
      var b = ev.target.closest ? ev.target.closest('.nt-step') : null;
      if (!b || b.disabled) return;
      goStep(Number(b.dataset.step || 0));
    });
    paintSteps();     // boshlang'ich holat (1-qadam faol, qolgani qulfda)
  })();

  // ?productId=&shop=[&step=3] bilan ochilsa — to'g'ridan-to'g'ri o'sha qadam.
  // Jonli qoralamani (#3068623) tekshirish yo'li ham shu.
  (function () {
    try {
      var q = new URLSearchParams(window.location.search);
      var pid = Number(q.get('productId') || 0);
      var sh = q.get('shop');
      var st = Number(q.get('step') || 0);
      if (sh) state.shop = sh;
      if (pid > 0) {
        state.cardFilled = false;
        // Har tugma O'Z qadamiga: 1 → umumiy ta'rif (get_product yuklab),
        // 2 → SKU, 3 → xususiyatlar. Ilgari step=1 ham `else` orqali 2-qadamga
        // tushib ketardi (foydalanuvchi shikoyati 2026-07-25).
        if (st === 1) {
          s1Enter(pid);
        } else {
          // «Chuqur tahrir»: 1-qadam SERVER-TOMON yashiritilgan (deep_edit) va
          // yuklagich ko'rinib turibdi. Nav nishonini ham darhol kerakli
          // qadamga qo'yamiz — yuklanayotgan bosqich header'da to'g'ri porlasin
          // (aks holda spinner davomida «1» faol ko'rinardi).
          // MAVJUD kartani tahrirlayapmiz — 1-qadam nishoni ochiq bo'lsin
          // (bosilganda goStep→s1Load YUKLAB beradi).
          state.editingProduct = pid;
          state.step = (st === 3) ? 3 : 2;
          try { paintSteps(); } catch (e) { /* paintSteps hali yo'q — s2/s3Show qiladi */ }
          if (st === 3) s3Enter(pid);
          else s2Enter(pid);
        }
      }
    } catch (e) { /* URLSearchParams yo'q — 1-qadam ochiladi */ }
  })();

  // ── Do'kon tanlagich (shablon faqat >1 do'konda render qiladi) ──
  // Kartochka yaratiladigan do'konni ANIQ ko'rsatadi/tanlaydi (ilgari
  // jimgina shops[0] edi). ?shop= yoki qoralama URL'i state.shop'ni
  // yuqorida o'rnatgan bo'lishi mumkin — select'ni shunga moslaymiz.
  (function () {
    var sel = document.getElementById('ntShopSelect');
    if (!sel) return;

    /* ⚠️ TANLANGAN DO'KONNI ESLAB QOLAMIZ — qoralama tiklanishi shunga
     *  bog'liq. Qoralama kaliti do'kon bo'yicha (`nt.draft.<shop>`), sahifa
     *  esa yangilanganda `state.shop` ni jimgina `shops[0]` ga qaytarardi.
     *  Oqibat: BIRINCHIDAN BOSHQA do'konda (mixbox) F5 bosilsa, tiklash
     *  YO'Q do'konning kalitiga qarab bo'sh forma ko'rsatardi — mehnat
     *  «yo'qolgan»dek tuyulardi. Jonli uchradi 2026-07-22.
     *  URL (`?shop=`) va qoralama URL'i ustunroq — ularga tegmaymiz.
     */
    var fromUrl = false;
    try {
      var q0 = new URLSearchParams(window.location.search);
      fromUrl = !!q0.get('shop') || Number(q0.get('productId') || 0) > 0;
    } catch (e) { /* eski brauzer — eslab qolgan do'kondan foydalanamiz */ }
    if (!fromUrl) {
      try {
        var last = localStorage.getItem('nt.shop');
        if (last && sel.querySelector('option[value="' + last + '"]')) state.shop = last;
      } catch (e) { /* localStorage o'chiq — jim o'tamiz */ }
    }

    if (state.shop) sel.value = state.shop;
    // Agar joriy state.shop ro'yxatda bo'lmasa (masalan noto'g'ri ?shop=),
    // select birinchi variantda qoladi — state'ni shunga tortamiz.
    if (sel.value) state.shop = sel.value;
    if (state.step !== 1) sel.disabled = true;   // draft URL'i bilan ochilgan
    sel.addEventListener('change', function () {
      if (sel.disabled) return;
      state.shop = sel.value;
      try { localStorage.setItem('nt.shop', String(sel.value)); } catch (e) {}
    });
  })();

  // ── Oddiy bildirishnoma (Escape bilan yopiladi) ─────────────────
  function notify(msg) {
    var n = document.createElement('div');
    n.setAttribute('role', 'status');
    n.textContent = msg;
    n.style.cssText = 'position:fixed;left:50%;bottom:24px;transform:translateX(-50%);' +
      'background:#1f2025;color:#fff;padding:12px 18px;border-radius:8px;' +
      'font-size:14px;z-index:9999;max-width:90vw;';
    document.body.appendChild(n);
    var kill = function () { n.remove(); document.removeEventListener('keydown', esc); };
    var esc = function (e) { if (e.key === 'Escape') kill(); };
    document.addEventListener('keydown', esc);
    setTimeout(kill, 3200);
  }

  /* ═══════════════════════════════════════════════════════════════
     BOSQICH A — kategoriya kaskadi + xususiyat qatorlari + qiymat modali

     DALIL (taxmin emas):
       - Ikkala HAR (categories/001 va 2712-yozuvli sacvoyage to'liq oqimi)
         dasturiy skanerlandi: kategoriya QIDIRUV endpointi YO'Q. Faqat
         rootCategories + childCategories → Uzum'da bu KASKAD, qidiruv emas.
         Shuning uchun terilgan matn faqat KO'RINIB turgan darajani filtrlaydi.
       - «+ Добавить» dropdown emas, MODAL ochadi:
         screenshots/category-interactions/001_.../005_-1_Цвет_value-dropdown-open.png
       - Rang uchun modalda 30px rang doirasi, o'lcham uchun doirasiz
         (005 vs 019) — modalning o'zi bir xil.

     Xavfsizlik: Uzum'ga to'g'ridan-to'g'ri murojaat YO'Q — hamma chaqiruv
     /noviy-tavar/api/* proxy'lari orqali, egalik guard'i serverda.
     ═══════════════════════════════════════════════════════════════ */

  // ── Kategoriya: USTMA-UST select'lar (Uzum oqimi) ───────────────
  //
  // ⚠️ Birinchi urinishda drill-down panel qilingan edi — XATO. Uzum'da
  // har daraja O'Z SELECT'ini oladi; tanlangach ostiga «Выбрать
  // подкатегорию» qo'shiladi; barg tanlangach «Принять» yonadi; tasdiqlangach
  // select'lar breadcrumb + «Изменить» ga almashadi.
  //
  // DALIL: foydalanuvchi bergan jonli Uzum kadrlari (6 ta holat) +
  //   blank-step1-category-typed.png (bitta select, «Принять» o'chiq) +
  //   sacvoyage-filled-step1-product-card.png (breadcrumb «A › B › C › D»).
  // HAR dalili: rootCategories + childCategories, qidiruv endpointi YO'Q ->
  //   terilgan matn faqat SHU darajani filtrlaydi.

  var cat = {
    levels: [],      // [{items:[], selected:{id,title}|null, open:bool}]
    cache: {},       // parentId -> items
    confirmed: null, // {id, path:[...]} — «Принять» bosilgach
    editing: false,  // «Изменить» rejimi (o'shanda «Отмена» chiqadi)
    // Quyi darajalar tortilyaptimi. TRUE bo'lsa «Qabul qilish» O'CHIQ —
    // tanlov barg ekani hali noma'lum (pickLevel izohiga qarang).
    pendingChildren: false,
    // Avlod qulfi (postavki `S.rstGen` naqshi): tanlov o'zgarsa raqam oshadi
    // va kech kelgan eski javob TASHLAB YUBORILADI. Usiz: foydalanuvchi
    // tortish paytida matn tersa, eskirgan javob yot daraja qo'shar va
    // `pendingChildren` abadiy yoqiq qolib «Qabul qilish»ni o'ldirardi.
    pickGen: 0
  };

  var catLevelsEl = document.getElementById('ntCatLevels');
  var catPick = document.getElementById('ntCatPick');
  var catDone = document.getElementById('ntCatDone');
  var crumbsEl = document.getElementById('ntCrumbs');
  var catCancel = document.getElementById('ntCatCancel');
  var catChange = document.getElementById('ntCatChange');
  var catNote = document.getElementById('ntCatNote');
  var acceptBtn = document.getElementById('ntAccept');

  function catTitle(c) {
    // Portal `title` ni tekis string qaytaradi; ehtiyot uchun {uz,ru} ni ham qo'llaymiz.
    if (c && typeof c.title === 'object' && c.title) return c.title[lang] || c.title.ru || c.title.uz || '';
    return (c && c.title) || '';
  }

  // ⚠️ XATONI JIMGINA YUTMAYMIZ. Avval xato holatida bo'sh massiv qaytarilardi
  // va panel «Нет подходящей категории» ko'rsatardi — ya'ni token o'lganda
  // (Uzum 401 -> proxy 502) foydalanuvchi «kategoriya yo'q ekan» deb o'ylardi.
  // Bu YOLG'ON xabar. Endi bo'sh ro'yxat va xato FARQLANADI.
  function fetchCategories(parentId) {
    var key = parentId == null ? 'root' : String(parentId);
    // ⚠️ BO'SH massiv `[]` JS'da truthy — `.length` sharti bo'lmasa o'tkinchi
    // bo'sh javob kesh-hit deb sanalib, tarmoqqa qayta chiqmasdik.
    if (cat.cache[key] && cat.cache[key].length) {
      return Promise.resolve({ items: cat.cache[key] });
    }
    if (!state.shop) return Promise.resolve({ items: [], error: 'noshop' });
    var url = '/noviy-tavar/api/categories?shop=' + encodeURIComponent(state.shop) +
      (parentId == null ? '' : '&parentId=' + encodeURIComponent(parentId));
    return fetch(url, { credentials: 'same-origin' })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function (d) {
        // Uzum ro'yxatni alifbo bo'yicha ko'rsatadi (kadr: Автотовары,
        // Аксессуары, Бытовая техника...) — API tartibi saralanmagan.
        var list = ((d && d.categories) || []).slice().sort(function (a, b) {
          return catTitle(a).localeCompare(catTitle(b), lang === 'uz' ? 'uz' : 'ru');
        });
        cat.cache[key] = list;   // faqat MUVAFFAQIYAT keshlanadi
        return { items: list };
      })
      .catch(function () {
        // Kiritilgan ma'lumot saqlanadi; foydalanuvchi qayta urinishi mumkin.
        return { items: [], error: 'fetch', parentId: parentId };
      });
  }

  function levelPlaceholder(i) {
    return i === 0
      ? tr('Kategoriya nomi yoki tovar', 'Название категории или товара')
      : tr('Quyi kategoriyani tanlang', 'Выбрать подкатегорию');
  }

  function renderLevels() {
    if (!catLevelsEl) return;
    catLevelsEl.textContent = '';
    cat.levels.forEach(function (lv, i) {
      var wrap = document.createElement('div');
      wrap.className = 'nt-select-wrap nt-combo';

      var input = document.createElement('input');
      input.className = 'nt-input';
      input.type = 'text';
      input.autocomplete = 'off';
      input.setAttribute('role', 'combobox');
      input.setAttribute('aria-expanded', lv.open ? 'true' : 'false');
      input.setAttribute('aria-autocomplete', 'list');
      input.setAttribute('aria-label', levelPlaceholder(i));
      input.placeholder = levelPlaceholder(i);
      input.value = lv.selected ? lv.selected.title : (lv.query || '');

      var panel = document.createElement('div');
      panel.className = 'nt-panel';
      panel.setAttribute('role', 'listbox');
      panel.hidden = !lv.open;

      // DIQQAT: `click` ham ochmasin — click fokusdan KEYIN keladi va panelni
      // darrov qayta yopardi. Ochish faqat `focus` orqali; yopish — tashqi
      // klik yoki Escape.
      input.addEventListener('click', function (e) { e.stopPropagation(); });
      input.addEventListener('focus', function () { openLevel(i); });
      input.addEventListener('input', function () {
        // Terish shu darajani filtrlaydi va tanlovni bekor qiladi
        // (pastdagi darajalar ham tushadi).
        lv.query = input.value;
        lv.selected = null;
        cat.levels = cat.levels.slice(0, i + 1);
        // Kutilayotgan bola-tortishni BEKOR qilamiz: aks holda kech kelgan
        // javob yot daraja qo'shar, `pendingChildren` esa yoqiq qolardi.
        cat.pickGen++;
        cat.pendingChildren = false;
        cat.confirmed = null;
        clearCategoryState();
        lv.open = true;
        fillPanel(panel, lv, i);
        panel.hidden = false;
        syncAccept();
      });

      fillPanel(panel, lv, i);
      wrap.appendChild(input);
      wrap.appendChild(panel);
      catLevelsEl.appendChild(wrap);
    });
  }

  function fillPanel(panel, lv, i) {
    panel.textContent = '';
    // DIQQAT: `active` bo'yicha FILTRLAMAYMIZ. Jonli javob (shop=10945, 22 ildiz)
    // hammasini `active:false` bilan qaytaradi — bu «o'chirilgan» degani EMAS,
    // shunchaki ildiz barg emas (`canUse:true`, `hasChildren:true`). Bir marta
    // shunga aldanib butun ro'yxat bo'sh chiqqan edi.
    var q = (lv.query || '').trim().toLowerCase();
    var list = lv.items.filter(function (c) {
      if (!q) return true;
      return catTitle(c).toLowerCase().indexOf(q) !== -1;
    });
    // XATO va BO'SH RO'YXAT boshqa-boshqa xabar beradi (avval ikkalasi ham
    // «kategoriya yo'q» derdi — token o'lganda yolg'on xulosa chiqardi).
    if (lv.error) {
      var er = document.createElement('div');
      er.className = 'nt-panel-empty nt-panel-error';
      er.appendChild(document.createTextNode(lv.error === 'noshop'
        ? tr('Do’kon topilmadi', 'Магазин не найден')
        : tr('Kategoriyalarni yuklab bo’lmadi', 'Не удалось загрузить категории')));
      if (lv.error === 'fetch') {
        var rb = document.createElement('button');
        rb.type = 'button';
        rb.className = 'nt-panel-retry';
        rb.textContent = tr('Qayta urinish', 'Повторить');
        rb.addEventListener('click', function (e) {
          e.stopPropagation();
          fetchCategories(lv.parentId).then(function (res) {
            lv.items = res.items; lv.error = res.error;
            renderLevels();
          });
        });
        er.appendChild(rb);
      }
      panel.appendChild(er);
      return;
    }
    if (!list.length) {
      var em = document.createElement('div');
      em.className = 'nt-panel-empty';
      em.textContent = tr('Mos kategoriya yo’q', 'Нет подходящей категории');
      panel.appendChild(em);
      return;
    }
    list.forEach(function (c) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'nt-panel-item';
      b.setAttribute('role', 'option');
      b.textContent = catTitle(c);
      b.addEventListener('click', function (e) {
        e.stopPropagation();
        pickLevel(i, c);
      });
      panel.appendChild(b);
    });
  }

  function openLevel(i) {
    // ⚠️ REKURSIYA QULFI: renderLevels() yangi input yaratadi va unga fokus
    // beradi -> focus handleri yana openLevel() ni chaqiradi. Agar bu daraja
    // allaqachon ochiq bo'lsa darrov qaytamiz, aks holda «Maximum call stack
    // size exceeded» bo'ladi (aynan shu sodir bo'lgan).
    if (!cat.levels[i] || cat.levels[i].open) return;
    cat.levels.forEach(function (lv, j) { lv.open = (j === i); });
    renderLevels();
    var inp = catLevelsEl.querySelectorAll('input')[i];
    if (inp && document.activeElement !== inp) inp.focus();
  }

  function closeAllLevels() {
    var any = cat.levels.some(function (lv) { return lv.open; });
    if (!any) return;
    cat.levels.forEach(function (lv) { lv.open = false; });
    renderLevels();
  }

  function pickLevel(i, c) {
    var gen = ++cat.pickGen;   // bu tanlovning avlodi
    var lv = cat.levels[i];
    lv.selected = { id: c.id, title: catTitle(c) };
    lv.query = '';
    lv.open = false;
    // Bu darajadan pastdagilar bekor bo'ladi.
    cat.levels = cat.levels.slice(0, i + 1);
    cat.confirmed = null;
    clearCategoryState();

    if (c.hasChildren && c.hasActiveChildren !== false) {
      // ⚠️ POYGA: yangi daraja ASINXRON qo'shiladi. Shu oraliqda oxirgi
      // daraja «tanlangan» ko'rinadi va leafSelected() uni BARG deb hisoblab
      // «Qabul qilish»ni yoqib yuborardi — foydalanuvchi barg BO'LMAGAN
      // (masalan ildiz «Aksessuarlar») kategoriyani tasdiqlab, mahsulotni
      // noto'g'ri joyga qo'yishi mumkin edi. Jonli o'lchov bilan uchradi
      // (2026-07-21): sovuq yuklashda 400ms lik oyna ochiq qolardi.
      cat.pendingChildren = true;
      renderLevels();
      fetchCategories(c.id).then(function (res) {
        if (gen !== cat.pickGen) return;   // eskirgan tanlov — tashlab yuboramiz
        cat.pendingChildren = false;
        cat.levels.push({ items: res.items, selected: null, open: false,
                          query: '', error: res.error, parentId: c.id });
        renderLevels();
        syncAccept();
      }).catch(function () {
        if (gen !== cat.pickGen) return;
        // Tortib bo'lmadi — baribir ochib qo'ymaymiz (barg emasligi ANIQ).
        cat.pendingChildren = false;
        syncAccept();
      });
    } else {
      cat.pendingChildren = false;
      renderLevels();   // barg — «Принять» yonadi
    }
    syncAccept();
  }

  function leafSelected() {
    if (!cat.levels.length) return null;
    // Quyi darajalar hali yuklanyapti — bu tanlov BARG ekani hali NOMA'LUM.
    if (cat.pendingChildren) return null;
    var last = cat.levels[cat.levels.length - 1];
    return last.selected || null;   // oxirgi daraja tanlangan = barg
  }

  function syncAccept() {
    if (acceptBtn) acceptBtn.disabled = !leafSelected();
  }

  function clearCategoryState() {
    // Kategoriya o'zgardi — sariq eslatma aytganidek, xususiyatlar saqlanmaydi.
    state.categoryId = null;
    state.meta = null;
    state.rows = [];
    charOptions = [];
    charReqAttempted = false;   // yangi kategoriyaга xato holatini olib o'tmaymiz
    renderRows();
    syncCategoryDependent();
    validate();
  }

  function catPath() {
    return cat.levels.filter(function (lv) { return lv.selected; })
                     .map(function (lv) { return lv.selected.title; });
  }

  function showPickMode() {
    if (catPick) catPick.hidden = false;
    if (catDone) catDone.hidden = true;
    if (catNote) catNote.hidden = false;
    if (catCancel) catCancel.hidden = !cat.editing;
    syncAccept();
  }

  function showDoneMode() {
    if (!crumbsEl) return;
    crumbsEl.textContent = '';
    cat.confirmed.path.forEach(function (t, i) {
      if (i) {
        var sep = document.createElement('span');
        sep.className = 'nt-crumb-sep';
        sep.setAttribute('aria-hidden', 'true');
        sep.textContent = '›';   // referens: «A › B › C»
        crumbsEl.appendChild(sep);
      }
      var s = document.createElement('span');
      s.textContent = t;
      crumbsEl.appendChild(s);
    });
    if (catPick) catPick.hidden = true;
    if (catDone) catDone.hidden = false;
  }

  if (catChange) {
    catChange.addEventListener('click', function () {
      cat.editing = true;      // tahrir rejimi -> «Отмена» chiqadi
      showPickMode();
    });
  }

  if (catCancel) {
    catCancel.addEventListener('click', function () {
      // Tasdiqlangan holatga qaytamiz — hech narsa o'zgarmaydi.
      if (!cat.confirmed) return;
      cat.editing = false;
      restoreConfirmed();
    });
  }

  function restoreConfirmed() {
    state.categoryId = cat.confirmed.id;
    state.meta = cat.confirmed.meta;
    state.rows = cat.confirmed.rows;
    charOptions = cat.confirmed.charOptions;
    renderRows();
    syncCategoryDependent();
    validate();
    showDoneMode();
  }

  // Kategoriya meta'si — «Qabul qilish» va qoralama tiklash IKKALASI ham
  // shu orqali oladi (bitta joy — ikki chaqiruvchi bir xil ishlasin).
  function fetchCategoryMeta(categoryId) {
    return fetch('/noviy-tavar/api/category-meta?shop=' + encodeURIComponent(state.shop) +
                 '&categoryId=' + encodeURIComponent(categoryId),
                 { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; });
  }

  // ── «Принять» → kategoriya meta → forma ochiladi ────────────────
  if (acceptBtn) {
    acceptBtn.addEventListener('click', function () {
      var leaf = leafSelected();
      if (!leaf || !state.shop) return;
      acceptBtn.disabled = true;
      var prev = acceptBtn.textContent;
      acceptBtn.textContent = tr('Yuklanmoqda...', 'Загрузка...');
      fetchCategoryMeta(leaf.id)
        .then(function (meta) {
          acceptBtn.textContent = prev;
          if (!meta) {
            acceptBtn.disabled = false;
            notify(tr('Kategoriya ma’lumotini olib bo’lmadi',
                      'Не удалось получить данные категории'));
            return;
          }
          state.categoryId = leaf.id;
          state.meta = meta;
          state.rows = [];
          buildCharOptions(meta);
          buildFilters(meta);   // Бренд/Модель/Страна — kategoriyaga bog'liq
          renderRows();
          syncCategoryDependent();
          validate();
          cat.editing = false;
          cat.confirmed = {
            id: leaf.id, path: catPath(), meta: meta,
            rows: state.rows, charOptions: charOptions
          };
          showDoneMode();
        })
        .catch(function () {
          acceptBtn.textContent = prev;
          acceptBtn.disabled = false;
          notify(tr('Tarmoq xatosi', 'Ошибка сети'));
        });
    });
  }

  /* ══════════════════════════════════════════════════════════════
   *  QORALAMA (draft) — 1-bosqich holati
   *
   *  MAQSAD: F5 / brauzer «orqaga» tugmasi / brauzerni yopib qayta ochish —
   *  bularning HECH BIRIDA to'ldirilgan forma yo'qolmasin. Uzumda karta
   *  yaratish uzoq ish (rasm, xususiyat, sertifikat) — bir tasodifiy
   *  yangilash butun mehnatni yo'q qilardi.
   *
   *  Naqsh postavki.html:3153 (`saveCreateDraftNow`/`restoreCreateDraft`)
   *  dan olingan: do'kon bo'yicha kalit, 250ms debounce, navigatsiyadan
   *  OLDIN majburiy yozish, har localStorage murojaati try/catch ichida.
   *
   *  ⚠️ Bu KESH EMAS — tezlik uchun emas, foydalanuvchi MEHNATI uchun.
   *  O'z prefiksi bor (`nt.draft.*`), UI bayroqlaridan (`nt.tipsHidden`)
   *  ajratilgan.
   * ══════════════════════════════════════════════════════════════ */

  var NT_DRAFT_VER = 1;          // shakl o'zgarsa +1 (eski qoralama tashlanadi)
  var NT_DRAFT_PREFIX = 'nt.draft.';
  var NT_DRAFT_TTL = 7 * 24 * 3600e3;   // 7 kun — juda eski qoralama tirilmasin

  function draftKey() { return NT_DRAFT_PREFIX + (state.shop || '?'); }

  function setVal(id, v) {
    var e = document.getElementById(id);
    if (e && v != null) e.value = v;
  }
  function setHtml(id, v) {
    var e = document.getElementById(id);
    if (e && v != null) e.innerHTML = v;
  }

  // Filtr (Brend/Model/Davlat): `state.filterValues` faqat ID saqlaydi,
  // ko'rinadigan matn esa input'da — tiklashda IKKALASI ham kerak.
  function collectFilterTexts() {
    var out = [];
    if (!filtersEl) return out;
    filtersEl.querySelectorAll('[data-nt-filter]').forEach(function (sec) {
      var fid = sec.getAttribute('data-nt-filter');
      var inp = sec.querySelector('input');
      if (fid && inp && inp.value) {
        out.push({ fid: fid, text: inp.value, id: state.filterValues[fid] });
      }
    });
    return out;
  }

  function collectDraft() {
    return {
      v: NT_DRAFT_VER,
      ts: Date.now(),
      shop: state.shop,
      categoryId: state.categoryId,
      catPath: cat.confirmed ? cat.confirmed.path : null,
      titleUz: val('ntTitleUz'), titleRu: val('ntTitleRu'),
      shortUz: val('ntShortUz'), shortRu: val('ntShortRu'),
      descUz: html('ntDescUz'), descRu: html('ntDescRu'),
      warranty: (document.getElementById('ntWarranty') || {}).value || '',
      productFields: state.productFields,
      // Media — Uzumga ALLAQACHON yuklangan, {key,url} bardoshli.
      images: state.images,
      certificates: state.certificates,
      colorImages: state.colorImages,
      video: state.video,
      imageCollection: state.imageCollection,
      colorVideos: state.colorVideos,
      colorCollections: state.colorCollections,
      filters: collectFilterTexts(),
      // Xususiyat qatorlari: `opt` meta'dan qayta quriladi, shuning uchun
      // faqat id + tanlangan qiymatlar saqlanadi. MAXSUS (custom) xususiyat
      // meta'da YO'Q — uni nomi bilan saqlab, tiklashda qayta yaratamiz.
      rows: state.rows.map(function (r) {
        return {
          id: r.id,
          selected: r.selected || [],
          custom: (r.opt && r.opt.custom)
            ? { uz: r.opt.uz, ru: r.opt.ru, values: r.opt.values || [] } : null
        };
      })
    };
  }

  // Bo'sh formani saqlamaymiz — aks holda har ochilishda keraksiz yozuv.
  function draftHasContent(d) {
    var desc = (d.descUz || '').replace(/<br\s*\/?>|&nbsp;|\s/gi, '');
    var descRu = (d.descRu || '').replace(/<br\s*\/?>|&nbsp;|\s/gi, '');
    return !!(d.categoryId || d.titleUz || d.titleRu || d.shortUz || d.shortRu ||
              desc || descRu || (d.images && d.images.length) ||
              (d.rows && d.rows.length) || (d.filters && d.filters.length));
  }

  // ⚠️ TARTIB QULFI. Sahifa ochilganda forma BO'SH, tiklash esa ildizlar
  // yuklangach (~400ms) boshlanadi. Bu qulfsiz: bo'sh formaning 250ms
  // debounce'i tiklashdan OLDIN ishga tushib, `draftHasContent` false
  // bo'lgani uchun qoralamani O'CHIRIB yuborardi. Jonli uchradi (2026-07-21)
  // — ilgari brauzer keshi ildizlarni oniy bergani uchun xato YASHIRIN edi.
  var draftReady = false;
  function markDraftReady() { draftReady = true; }

  function saveDraftNow() {
    if (!draftReady) return;          // tiklash tugamaguncha YOZMAYMIZ
    // Mavjud kartani tahrirlash (s1Load) — «yangi tovar» qoralamasini
    // IFLOSLAMAYMIZ (aks holda keyingi yangi-tovar ochilishida eski karta
    // tirilib qolardi).
    if (state.editingProduct) return;
    // 2-qadamda qoralama Uzumda yaratilgan — endi localStorage'da saqlash
    // ma'nosiz (va zararli: eski 1-qadam holati tirilib qolardi).
    if (state.step !== 1) return;
    try {
      var d = collectDraft();
      if (draftHasContent(d)) localStorage.setItem(draftKey(), JSON.stringify(d));
      else localStorage.removeItem(draftKey());
    } catch (e) { /* kvota/private rejim — jim o'tamiz, UI tirik qoladi */ }
  }

  var _draftT = null;
  function saveDraft() {
    clearTimeout(_draftT);
    _draftT = setTimeout(function () { _draftT = null; saveDraftNow(); }, 250);
  }
  function saveDraftFlush() {
    clearTimeout(_draftT); _draftT = null; saveDraftNow();
  }
  function clearDraft() {
    clearTimeout(_draftT); _draftT = null;
    try { localStorage.removeItem(draftKey()); } catch (e) {}
  }
  root.ntSaveDraft = saveDraft;        // boshqa bloklar chaqiradi
  root.ntClearDraft = clearDraft;

  function restoreDraft() {
    // HAR chiqish yo'lida `markDraftReady()` chaqirilishi SHART — aks holda
    // saqlash butunlay o'chib qoladi (yuqoridagi tartib qulfi izohiga qara).
    var raw;
    try { raw = localStorage.getItem(draftKey()); } catch (e) { markDraftReady(); return false; }
    if (!raw) { markDraftReady(); return false; }
    var d;
    try { d = JSON.parse(raw); } catch (e) { markDraftReady(); return false; }
    if (!d || d.v !== NT_DRAFT_VER) { markDraftReady(); return false; }
    if (!d.ts || (Date.now() - d.ts) > NT_DRAFT_TTL) {
      draftReady = true; clearDraft(); return false;
    }

    applyDraftObject(d, { notify: tr('Qoralama tiklandi — davom etishingiz mumkin',
                                     'Черновик восстановлен — можно продолжить') });
    return true;
  }

  // Draft (localStorage) YOKI get_product (server) obyektini 1-qadam formasiga
  // qo'llaydi. `restoreDraft` va `s1Load` (mavjud kartani tahrirlash) — ikkalasi
  // shu bir mashinani ishlatadi.
  //   opts.notify     — tugagach ko'rsatiladigan xabar (yoki null)
  //   opts.cardFilled — true → holat «Uzumga saqlangan» deb belgilanadi
  //                     (tahrirlash: yuklangan holat TOZA baza; keyingi edit=iflos)
  function applyDraftObject(d, opts) {
    opts = opts || {};
    var doneMsg = opts.notify || null;
    var markFilled = !!opts.cardFilled;

    // ── Matn maydonlari (kategoriyaga bog'liq emas) ──────────────
    setVal('ntTitleUz', d.titleUz); setVal('ntTitleRu', d.titleRu);
    setVal('ntShortUz', d.shortUz); setVal('ntShortRu', d.shortRu);
    setHtml('ntDescUz', d.descUz);  setHtml('ntDescRu', d.descRu);
    setVal('ntWarranty', d.warranty);
    state.productFields = d.productFields || {};

    // ── Media (URL'lar Uzumda, qayta yuklash shart emas) ─────────
    state.images = d.images || [];
    state.certificates = d.certificates || [];
    state.colorImages = d.colorImages || [];
    state.video = d.video || null;
    state.imageCollection = d.imageCollection || null;
    state.colorVideos = d.colorVideos || [];
    state.colorCollections = d.colorCollections || [];

    function finish() {
      // Tahrirlash: yuklangan holat toza baza (edit qilinsa ogohlantirish
      // chiqadi). Draft-tiklash: ATAYLAB snapshot YO'Q (2026-07-24) — to'la
      // forma chiqishda baribir ogohlantirsin.
      if (markFilled) { state.cardFilled = true; ntSnapshot(); }
      if (doneMsg) notify(doneMsg);
    }

    // ── Kategoriya: meta'siz xususiyat/filtrlarni qura olmaymiz ──
    if (d.categoryId && d.catPath && d.catPath.length) {
      fetchCategoryMeta(d.categoryId).then(function (meta) {
        if (!meta) { markDraftReady(); return; }  // meta yo'q — matnlar baribir tiklandi
        state.categoryId = d.categoryId;
        state.meta = meta;
        state.rows = [];
        buildCharOptions(meta);                  // REQUIRED qatorlar avto-qo'shiladi
        buildFilters(meta);                      // ⚠️ state.filterValues ni TOZALAYDI

        // Filtrlarni tiklash — buildFilters'dan KEYIN (u tozalab ketadi).
        (d.filters || []).forEach(function (f) {
          if (f.id != null) state.filterValues[f.fid] = f.id;
          var sec = filtersEl && filtersEl.querySelector('[data-nt-filter="' + f.fid + '"]');
          var inp = sec && sec.querySelector('input');
          if (inp) inp.value = f.text || '';
        });

        // Xususiyat qatorlari
        state.rows = [];
        (d.rows || []).forEach(function (sr) {
          var opt = charOptions.filter(function (o) { return o.id === sr.id; })[0];
          if (!opt && sr.custom) {
            // Maxsus xususiyat meta'da yo'q — qayta yaratamiz.
            opt = { id: sr.id, uz: sr.custom.uz, ru: sr.custom.ru, custom: true,
                    required: false, requiredType: 'NOT_REQUIRED', flowA: false,
                    orderingNumber: 0, values: sr.custom.values || [] };
            charOptions.push(opt);
          }
          if (!opt) return;                      // sxema o'zgargan — o'sha qatorni tashlaymiz
          state.rows.push({ id: sr.id, opt: opt, selected: sr.selected || [] });
        });

        renderRows();
        syncCategoryDependent();
        cat.confirmed = { id: d.categoryId, path: d.catPath, meta: meta,
                          rows: state.rows, charOptions: charOptions };
        showDoneMode();
        repaintMedia();
        markDraftReady();     // ⚠️ validate() dan OLDIN — u saqlashni chaqiradi
        validate();
        finish();
      }).catch(function () {
        markDraftReady();     // meta olinmadi — matnlar tiklandi, saqlash tiklansin
      });
    } else {
      repaintMedia();
      markDraftReady();
      validate();
      finish();
    }
  }

  // ── 1-QADAM: mavjud kartani tahrirlash uchun yuklash ─────────────
  // «Umumiy ta'rifni o'zgartirish» (karta ⋮-menyusi) → ?productId=&step=1.
  function s1Enter(productId) {
    state.productId = Number(productId) || null;
    s1Load(productId);
  }
  function s1Load(productId) {
    var url = '/noviy-tavar/api/load-product?shop=' + encodeURIComponent(state.shop) +
              '&productId=' + encodeURIComponent(productId);
    fetch(url, { credentials: 'same-origin' })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d || {} }; }); })
      .then(function (res) {
        if (!res.ok) {
          notify(res.d.error || tr('Kartani yuklab bo\'lmadi', 'Не удалось загрузить карточку'));
          showStep1();
          return;
        }
        // Tahrirlash sessiyasi: localStorage «yangi tovar» qoralamasini
        // IFLOSLAMAYMIZ (u boshqa oqim). saveDraftNow shu bayroqni tekshiradi.
        state.editingProduct = Number(productId) || null;
        applyDraftObject(res.d, { cardFilled: true });
        showStep1();
      })
      .catch(function () {
        notify(tr('Tarmoq xatosi', 'Ошибка сети'));
        showStep1();
      });
  }
  root.ntEnterStep1 = s1Enter;

  // Media bo'limlarini qayta chizish (funksiyalar yuqorida e'lon qilingan).
  function repaintMedia() {
    try { renderPhotos(); } catch (e) {}
    try { renderCerts(); } catch (e) {}
    try { renderVideo(); } catch (e) {}
    try { render360(); } catch (e) {}
    try { syncColorMedia(); renderColorMedia(); } catch (e) {}
    try { syncCertGate(); } catch (e) {}
    try { syncWarranty(); } catch (e) {}
  }

  // Har o'zgarishda saqlaymiz. `input`+`change` — matn, tanlov va
  // contenteditable tavsif uchun yetarli; media/xususiyat o'zgarishlari
  // validate() ni chaqiradi, uni ham ilib qo'yamiz (pastda).
  ['input', 'change'].forEach(function (ev) {
    root.addEventListener(ev, function () {
      if (state.step === 1) saveDraft();
      // 2/3-qadamda qoralama YO'Q — faqat «saqlanmagan» bayrog'ini ko'taramiz.
      else ntDirty[state.step] = true;
    }, true);
  });
  // Sahifadan chiqishdan OLDIN kutib turgan yozuvni diskka tushiramiz.
  window.addEventListener('pagehide', saveDraftFlush);

  /* ══════════════════════════════════════════════════════════════════
   *  SAHIFADAN CHIQISH OGOHLANTIRISHI
   *
   *  ⚠️ HALOL CHEKLOV: brauzerning NATIV `beforeunload` oynasini
   *  («Закрыть сайт? / Изменения могут не сохраниться») dizaynga moslab
   *  BO'LMAYDI — Chrome/Firefox/Edge buni ataylab bloklaydi (matn ham,
   *  ko'rinish ham). U faqat F5 / tab yopish / manzil satri uchun qoladi.
   *
   *  Lekin aksariyat tasodifiy chiqish ilova ICHIDAGI o'tishdan bo'ladi
   *  (chap menyu, logotip, «orqaga»). AYNAN shu joylarni ushlab, o'z
   *  dizaynli modalimizni ko'rsatamiz; nativ oyna zaxira bo'lib qoladi.
   * ══════════════════════════════════════════════════════════════════ */
  var ntBypassGuard = false;        // «Chiqish» tasdiqlangach — qo'riqchini o'chirar
  var ntPendingNav = null;          // {type:'href'|'back', url?}
  var leaveModal = document.getElementById('ntLeaveModal');
  var leaveOk = document.getElementById('ntLeaveOk');
  var leaveCancel = document.getElementById('ntLeaveCancel');

  function showLeaveModal(pending) {
    ntPendingNav = pending;
    if (leaveModal) {
      leaveModal.hidden = false;
      // Fokusni «Qolish» ga — xavfsiz standart (Enter tasodifan chiqarmasin).
      if (leaveCancel) { try { leaveCancel.focus({ preventScroll: true }); } catch (e) { leaveCancel.focus(); } }
    }
  }
  function hideLeaveModal() {
    if (leaveModal) leaveModal.hidden = true;
    ntPendingNav = null;
  }
  function doLeave() {
    ntBypassGuard = true;           // endi nativ beforeunload ham jim o'tadi
    var p = ntPendingNav;
    hideLeaveModal();
    if (!p) return;
    if (p.type === 'back') history.go(-2);   // qayta qo'yilgan sentinel + joriy sahifadan o'tib ketamiz
    else if (p.url) window.location.href = p.url;
  }
  if (leaveOk) leaveOk.addEventListener('click', doLeave);
  if (leaveCancel) leaveCancel.addEventListener('click', hideLeaveModal);
  if (leaveModal) {
    leaveModal.addEventListener('click', function (e) {
      if (e.target === leaveModal) hideLeaveModal();   // fon bosilsa yopiladi
    });
  }
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && leaveModal && !leaveModal.hidden) hideLeaveModal();
  });

  // ── 1. Ichki navigatsiya bosilishi (chap menyu, logotip, breadcrumb) ──
  // Havolalar IKKI xil bo'ladi:
  //   · oddiy `<a href>` — profil menyusi, hujjat havolalari;
  //   · yon-panelning `[data-href]` elementlari — `uzum_ui.js` ularning
  //     `href`'ini olib tashlab, klikda `location.assign()` bilan JS orqali
  //     o'tadi. AGAR ularni ushlamasak, o'sha JS o'tishi NATIV oynani chiqarib
  //     yuboradi (foydalanuvchi shikoyati 2026-07-24). Shuning uchun ikkalasini
  //     ham ushlaymiz.
  // ⚠️ `e.stopPropagation()` SHART: biz capture-fazada ishlaymiz, uzum_ui.js
  //   esa bubble-fazada `[data-href]` ni tinglaydi. To'xtatmasak, u baribir
  //   navigatsiya qilib, nativ oynani chiqaradi.
  document.addEventListener('click', function (e) {
    if (ntBypassGuard) return;
    var a = e.target.closest && e.target.closest('a[href], [data-href]');
    if (!a) return;
    var raw = a.getAttribute('href') || a.getAttribute('data-href');
    if (!raw || raw.charAt(0) === '#' || /^(javascript:|mailto:|tel:)/i.test(raw)) return;
    if (a.hasAttribute('download')) return;
    if (a.target && a.target !== '_self') return;          // yangi tab — tegmaymiz
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey ||
        e.shiftKey || e.altKey) return;                    // Ctrl+klik va h.k. — yangi tab
    var dest;
    try { dest = new URL(raw, location.href); } catch (_) { return; }
    // Ayni sahifa (faqat hash) — o'tish emas.
    if (dest.origin === location.origin &&
        dest.pathname === location.pathname && dest.search === location.search) return;
    if (!ntHasUnsaved()) return;                            // yo'qotadigan narsa yo'q
    e.preventDefault();
    e.stopPropagation();       // uzum_ui.js `data-href` ishlovchisi navigatsiya qilmasin
    showLeaveModal({ type: 'href', url: dest.href });
  }, true);

  // ── 2. «Orqaga» tugmasi — history sentinel bilan ushlanadi ───────
  // Sentinel: yuklashda bitta soxta yozuv qo'shamiz. «Orqaga» bosilsa u
  // iste'mol qilinadi va URL o'zgarmaydi — shunda ushlab qolamiz.
  if (window.history && history.pushState) {
    try { history.pushState(null, '', location.href); } catch (e) {}
    window.addEventListener('popstate', function () {
      if (ntBypassGuard) return;                 // «Chiqish» tasdiqlangan — o'tadi
      if (!ntHasUnsaved()) { history.back(); return; }  // toza — orqaga davom etsin
      try { history.pushState(null, '', location.href); } catch (e) {}  // sahifada qolamiz
      showLeaveModal({ type: 'back' });
    });
  }

  // ── 3. Qattiq unload (F5 / tab yopish / manzil satri) — NATIV oyna ──
  // Bularni brauzer boshqacha ko'rsatishga ruxsat bermaydi; zaxira sifatida
  // qoldiramiz. Faqat haqiqatan saqlanmagan mehnat bo'lsa chiqadi.
  window.addEventListener('beforeunload', function (e) {
    saveDraftFlush();                      // qoralama HAR HOLDA diskka tushsin
    if (ntBypassGuard || !ntHasUnsaved()) return;
    e.preventDefault();
    e.returnValue = '';
    return '';
  });

  // Boshlang'ich daraja — ildizlar
  fetchCategories(null).then(function (res) {
    cat.levels = [{ items: res.items, selected: null, open: false,
                    query: '', error: res.error, parentId: null }];
    renderLevels();
    syncAccept();
    // Ildizlar kelgach qoralamani tiklaymiz (kategoriya tanlagichi tayyor).
    // ⚠️ URL'da productId bo'lsa — foydalanuvchi ALLAQACHON 2-qadamda,
    // 1-qadam qoralamasini tiklash noto'g'ri bo'lardi.
    try {
      var q = new URLSearchParams(window.location.search);
      // `restoreDraft()` false qaytarsa — tiklanadigan narsa yo'q edi, ya'ni
      // forma BO'SH: aynan shu «xavfsiz nuqta». Tiklangan holatda esa imzo
      // tiklash tugagach (kategoriya meta'si ham) ichkarida olinadi.
      if (!Number(q.get('productId') || 0)) { if (!restoreDraft()) ntSnapshot(); }
      else { markDraftReady(); ntSnapshot(); }   // 2-qadam: tiklamaymiz, qulf ochilsin
    } catch (e) { restoreDraft(); }
  });

  // Zaxira: ildizlar tortilishi qotib qolsa ham saqlash abadiy o'chib
  // qolmasin — 8 soniyadan keyin qulfni majburan ochamiz.
  setTimeout(markDraftReady, 8000);

  var pickLink = document.getElementById('ntPickCategoryLink');
  if (pickLink) {
    pickLink.addEventListener('click', function (e) {
      e.preventDefault();
      if (cat.confirmed && catChange) { catChange.click(); }
      var first = catLevelsEl && catLevelsEl.querySelector('input');
      if (first) {
        first.focus();
        first.scrollIntoView({ behavior: prefersReduced() ? 'auto' : 'smooth', block: 'center' });
      }
    });
  }

  // ── Xususiyatlar: meta'dan ro'yxat + REQUIRED avto-render ───────
  var charBtn = document.getElementById('ntCharBtn');
  var charPanel = document.getElementById('ntCharPanel');
  var charRows = document.getElementById('ntCharRows');
  // (yuqoridagi kategoriya bloki ham shu ro'yxatni tozalaydi)
  var charOptions = [];   // [{id, uz, ru, values, required}]

  function ttl(t, which) {
    if (t && typeof t === 'object') return t[which] || t.ru || t.uz || '';
    return t || '';
  }

  function buildCharOptions(meta) {
    var src = (meta && meta.characteristics) || [];
    charOptions = src.map(function (c) {
      return {
        id: c.characteristicId,
        uz: ttl(c.characteristicTitle, 'uz'),
        ru: ttl(c.characteristicTitle, 'ru'),
        // DALIL (HANDOFF §2.3, jonli tasdiqlangan): REQUIRED → qator avto-render;
        // REQUIRED_ONE_OF_SIZE → render BO'LMAYDI (cheklov, majburiyat emas).
        required: c.requiredType === 'REQUIRED',
        // requiredType — REQUIRED (rang) aniqlash uchun (server fillType/isRequired
        // qo'yadi). ⚠️ orderingNumber — xususiyatning O'Z tartibi (rang=0, Длина=44);
        // create tanasida QATOR INDEKSI emas SHU yuboriladi (t8 etaloni, 2026-07-18).
        requiredType: c.requiredType || 'NOT_REQUIRED',
        flowA: !!c.flowA,
        orderingNumber: (c.orderingNumber != null ? c.orderingNumber : 0),
        values: (c.characteristicValues || []).map(function (v) {
          return {
            skuValue: v.skuValue,
            uz: ttl(v.title, 'uz'),
            ru: ttl(v.title, 'ru'),
            value: v.value
          };
        })
      };
    }).sort(function (a, b) { return 0; });

    // REQUIRED bo'lganlar darrov qator bo'lib chiqadi (referens: «Rang / Цвет»)
    charOptions.forEach(function (o) { if (o.required) addRow(o.id, true); });
  }

  function addRow(id, silent) {
    if (state.rows.some(function (r) { return r.id === id; })) return;
    if (state.rows.length >= 5) {           // referens sarlavhasi: «Максимум 5»
      notify(tr('Maksimum 5 ta xususiyat', 'Максимум 5 характеристик'));
      return;
    }
    var o = charOptions.filter(function (c) { return c.id === id; })[0];
    if (!o) return;
    state.rows.push({ id: id, opt: o, selected: [] });
    if (!silent) renderRows();
  }

  function removeRow(id) {
    state.rows = state.rows.filter(function (r) { return r.id !== id; });
    // MAXSUS xususiyat o'chirilsa — butunlay yo'qoladi (bandl `ne()` uni faqat
    // TANLANGANLAR ro'yxatiga qo'shadi, kategoriya sxemasiga emas). Aks holda u
    // dropdownda «ruxsat berilgan» xususiyatdek qolib ketardi.
    charOptions = charOptions.filter(function (o) {
      return !(o.custom && o.id === id);
    });
    renderRows();
    validate();
  }

  function rowLabel(o) {
    // Referens: «Rang / Цвет» — uz / ru bitta satrda (uzun bo'lsa o'raladi).
    if (o.uz && o.ru && o.uz !== o.ru) return o.uz + ' / ' + o.ru;
    return o.uz || o.ru;
  }

  function renderRows() {
    if (!charRows) return;
    charRows.textContent = '';
    state.rows.forEach(function (row) {
      var el = document.createElement('div');
      el.className = 'nt-char-row';

      var name = document.createElement('div');
      name.className = 'nt-char-name';
      name.textContent = rowLabel(row.opt);
      el.appendChild(name);

      var mid = document.createElement('div');
      mid.className = 'nt-char-mid';
      var add = document.createElement('button');
      add.type = 'button';
      add.className = 'nt-btn nt-btn--add';
      var plus = document.createElement('span');
      plus.setAttribute('aria-hidden', 'true');
      plus.textContent = '+';
      add.appendChild(plus);
      add.appendChild(document.createTextNode(' ' + tr('Qo’shish', 'Добавить')));
      add.addEventListener('click', function () { openValueModal(row); });
      mid.appendChild(add);

      if (row.selected.length) {
        var chips = document.createElement('div');
        chips.className = 'nt-chips';
        row.selected.forEach(function (v) {
          var chip = document.createElement('span');
          chip.className = 'nt-chip';
          if (isHex(v.value)) {
            var dot = document.createElement('span');
            dot.className = 'nt-chip-dot';
            dot.style.background = v.value;
            chip.appendChild(dot);
          }
          chip.appendChild(document.createTextNode(tr(v.uz, v.ru) || v.ru || v.uz));
          chips.appendChild(chip);
        });
        mid.appendChild(chips);
      }
      el.appendChild(mid);

      var del = document.createElement('button');
      del.type = 'button';
      del.className = 'nt-btn nt-btn--del';
      del.textContent = tr('O’chirish', 'Удалить');
      del.addEventListener('click', function () { removeRow(row.id); });
      el.appendChild(del);

      charRows.appendChild(el);
    });
    renderCharPanel();
    // Rang-media darvozasi qatorlarga bog'liq (productColors) — qator qo'shilsa,
    // o'chirilsa yoki qiymat tanlansa qayta hisoblanadi.
    syncColorMedia();
  }

  function isHex(v) { return typeof v === 'string' && /^#[0-9a-f]{3,8}$/i.test(v); }

  // Razmer-tizim qatori — «one-of-size» radio guruhi a'zosi (Uzum «Размер»
  // guruhi: barcha REQUIRED_ONE_OF_SIZE = bitta yashirin guruh). JONLI dalil
  // (2026-07-18): 2+ razmer-tizim tanlansa createProduct 400 validation-failed-001.
  function isSizeRow(row) {
    return !!(row && row.opt && row.opt.requiredType === 'REQUIRED_ONE_OF_SIZE');
  }

  // «≤2 xususiyat» cap — qiymatli defined-char soni ≤2 (JONLI 2026-07-19: 3+ →
  // validation-failed-001; rang MAXSUS emas, sof son). justRow'ga yangi qiymat
  // tanlangach son 2 dan oshsa, eng ESKI boshqa xususiyatlar tozalanadi.
  // Himoya: hozir tanlangan qator + rang (id -1) — odatda ikkalasi ham kerak.
  // state.rows tartibi = qo'shilish tartibi → eng eskisi birinchi.
  function s1EnforceCharCap(justRow) {
    var withVals = function () {
      return state.rows.filter(function (r) { return r.selected.length; }).length;
    };
    var cleared = false;
    for (var i = 0; i < state.rows.length; i++) {
      if (withVals() <= 2) break;
      var r = state.rows[i];
      if (r === justRow || r.id === -1 || !r.selected.length) continue;
      r.selected = [];
      cleared = true;
    }
    return cleared;
  }

  // ── Xususiyat dropdown (O'LCHANGAN: 012 — qidiruvsiz ro'yxat) ───
  function renderCharPanel() {
    if (!charPanel) return;
    charPanel.textContent = '';
    var free = charOptions.filter(function (o) {
      return !state.rows.some(function (r) { return r.id === o.id; });
    });
    if (!free.length) {
      var em = document.createElement('div');
      em.className = 'nt-panel-empty';
      em.textContent = tr('Boshqa xususiyat yo’q', 'Других характеристик нет');
      charPanel.appendChild(em);
      // «Yangi xususiyat» bandlda HAR DOIM oxirida turadi — ro'yxat bo'sh
      // bo'lganda ham (bu qator sxemadan emas, mijozdan qo'shiladi).
      appendNewCharItem();
      return;
    }
    free.forEach(function (o) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'nt-panel-item';
      b.setAttribute('role', 'option');
      b.textContent = tr(o.uz, o.ru) || o.ru || o.uz;
      b.addEventListener('click', function (e) {
        e.stopPropagation();
        addRow(o.id);
        closeCharPanel();
      });
      charPanel.appendChild(b);
    });
    appendNewCharItem();
  }

  // ── MAXSUS («custom») xususiyat ─────────────────────────────────
  // DALIL (Uzum bandli, editProductCard/Characteristics store):
  //   K = bo'sh xususiyatlar ro'yxati; oxiriga MIJOZ tomonda qo'shiladi:
  //     { displayTitle: t("add_new_characteristic_with_limit"),
  //       characteristicValues: [], orderingNumber: -1 }
  //   H(e,t): orderingNumber === -1 bo'lsa qator qo'shilmaydi, modal ochiladi.
  // Ya'ni bu qator API sxemasidan KELMAYDI — bizda chiqmagani shundan.
  var NT_CUSTOM_MAX = 3;      // bandl: customCharacteristics.length > 3 -> xato
  var customSeq = 0;          // maxsus qatorlarga sun'iy id (manfiy, -1 dan uzoq)

  function customRows() {
    return state.rows.filter(function (r) { return r.opt && r.opt.custom; });
  }

  function appendNewCharItem() {
    if (!charPanel) return;
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'nt-panel-item nt-panel-item--new';
    b.setAttribute('role', 'option');
    b.textContent = tr('Yangi xususiyat qoʻshish (maks. 3)',
                       'Добавить новую характеристику (макс. 3)');
    b.addEventListener('click', function (e) {
      e.stopPropagation();
      closeCharPanel();
      openNewCharModal();
    });
    charPanel.appendChild(b);
  }

  // Modal — Uzum `new-char-popup` komponentining aynan ekvivalenti.
  var ncModal = document.getElementById('ntNewCharModal');
  var ncUz = document.getElementById('ntNewCharUz');
  var ncRu = document.getElementById('ntNewCharRu');
  var ncUzErr = document.getElementById('ntNewCharUzErr');
  var ncRuErr = document.getElementById('ntNewCharRuErr');
  var ncLastFocus = null;

  function ncSetErr(el, msg) {
    if (!el) return;
    el.textContent = msg || '';
    el.hidden = !msg;
  }

  function ncClearErrors() {
    ncSetErr(ncUzErr, '');
    ncSetErr(ncRuErr, '');
    if (ncUz) ncUz.classList.remove('is-invalid');
    if (ncRu) ncRu.classList.remove('is-invalid');
  }

  function openNewCharModal() {
    if (!ncModal) return;
    // «maks. 3» — bandlda yakuniy saqlashda tekshiriladi
    // (customCharacteristics.length > 3 -> errors.limiting_number_of_...).
    // Biz DARROV to'sib, o'sha matnni ko'rsatamiz — 3 tadan keyin yaratish
    // behuda bo'lardi.
    if (customRows().length >= NT_CUSTOM_MAX) {
      notify(tr('Foydalanuvchi xususiyatlarining maksimal soni 3 ta boʻlishi mumkin',
                'Допустимо не более 3-х пользовательских характеристик'));
      return;
    }
    // Qatorlar umumiy chegarasi ham amal qiladi («Максимум 5»).
    if (state.rows.length >= 5) {
      notify(tr('Maksimum 5 ta xususiyat', 'Максимум 5 характеристик'));
      return;
    }
    ncLastFocus = document.activeElement;
    if (ncUz) ncUz.value = '';
    if (ncRu) ncRu.value = '';
    ncClearErrors();
    ncModal.hidden = false;
    document.addEventListener('keydown', ncKeys);
    if (ncUz) ncUz.focus();
  }

  function closeNewCharModal() {
    if (!ncModal) return;
    ncModal.hidden = true;
    ncClearErrors();
    document.removeEventListener('keydown', ncKeys);
    if (ncLastFocus && ncLastFocus.focus) ncLastFocus.focus();
  }

  function ncKeys(e) {
    if (e.key === 'Escape') { closeNewCharModal(); return; }
    if (e.key !== 'Tab' || ncModal.hidden) return;
    var f = ncModal.querySelectorAll('button, input');
    if (!f.length) return;
    var first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }

  // Bandl `ne()` 1:1: ikkala til MAJBURIY; ortiqcha bo'shliqlar siqiladi;
  // nom mavjud xususiyatlar bilan (registrga qaramay) to'qnashmasligi kerak.
  function ncNorm(s) { return String(s || '').replace(/ {2,}/g, ' ').trim(); }

  function ncTaken(name) {
    var low = name.toLowerCase();
    var hit = function (uz, ru) {
      return String(uz || '').toLowerCase() === low ||
             String(ru || '').toLowerCase() === low;
    };
    // a.value (kategoriyaning barcha xususiyatlari) + n.value (tanlanganlar)
    return charOptions.some(function (o) { return hit(o.uz, o.ru); }) ||
           state.rows.some(function (r) { return hit(r.opt.uz, r.opt.ru); });
  }

  function saveNewChar() {
    var uz = ncNorm(ncUz && ncUz.value);
    var ru = ncNorm(ncRu && ncRu.value);
    if (ncUz) ncUz.value = uz;
    if (ncRu) ncRu.value = ru;
    ncClearErrors();

    var reqMsg = tr('Toʻldirilishi shart boʻlgan maydon', 'Обязательное поле');
    var dupMsg = tr('Bunday xususiyat allaqachon mavjud!',
                    'Такая характеристика уже существует!');
    var bad = false;
    [[uz, ncUz, ncUzErr], [ru, ncRu, ncRuErr]].forEach(function (p) {
      var val = p[0], input = p[1], err = p[2];
      if (!val) { ncSetErr(err, reqMsg); bad = true; }
      else if (ncTaken(val)) { ncSetErr(err, dupMsg); bad = true; }
      if (err && !err.hidden && input) input.classList.add('is-invalid');
    });
    if (bad) return;

    customSeq += 1;
    var opt = {
      // Sun'iy manfiy id — haqiqiy characteristicId bilan to'qnashmaydi va
      // rang uchun ajratilgan -1 dan ham uzoq.
      id: -1000 - customSeq,
      uz: uz, ru: ru,
      custom: true,
      required: false,
      requiredType: 'NOT_REQUIRED',
      flowA: false,
      // Bandl: yangi xususiyat orderingNumber = tanlanganlar soni; yakuniy
      // yuborishda baribir 100+indeks bilan qayta raqamlanadi.
      orderingNumber: state.rows.length,
      values: []          // oldindan qiymat yo'q — foydalanuvchi o'zi kiritadi
    };
    charOptions.push(opt);
    state.rows.push({ id: opt.id, opt: opt, selected: [] });
    closeNewCharModal();
    renderRows();
    validate();
  }

  if (ncModal) {
    ncModal.addEventListener('click', function (e) {
      if (e.target === ncModal) closeNewCharModal();
    });
  }
  ['ntNewCharClose', 'ntNewCharCancel'].forEach(function (id) {
    var b = document.getElementById(id);
    if (b) b.addEventListener('click', closeNewCharModal);
  });
  var ncSave = document.getElementById('ntNewCharSave');
  if (ncSave) ncSave.addEventListener('click', saveNewChar);
  [ncUz, ncRu].forEach(function (i) {
    if (!i) return;
    i.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); saveNewChar(); }
    });
  });

  function openCharPanel() {
    if (!charPanel || charBtn.disabled) return;
    renderCharPanel();
    charPanel.hidden = false;
    charBtn.setAttribute('aria-expanded', 'true');
  }

  function closeCharPanel() {
    if (!charPanel) return;
    charPanel.hidden = true;
    charBtn.setAttribute('aria-expanded', 'false');
  }

  if (charBtn) {
    charBtn.addEventListener('click', function (e) {
      e.stopPropagation();
      if (charPanel.hidden) openCharPanel(); else closeCharPanel();
    });
  }

  // ── Qiymat modali «Выбрать характеристики» (O'LCHANGAN: 005/019) ─
  var modal = document.getElementById('ntValueModal');
  var modalList = document.getElementById('ntValueList');
  var modalSearch = document.getElementById('ntValueSearch');
  var modalSave = document.getElementById('ntValueSave');
  var modalRow = null;      // qaysi qator uchun ochilgan
  var modalDraft = [];      // vaqtinchalik tanlov — «Сохранить» bosilmaguncha qo'llanmaydi
  var lastFocus = null;

  function openValueModal(row) {
    if (!modal) return;
    modalRow = row;
    // Maxsus qatorda qiymatlar TAHRIRLANADI — nusxa olamiz (bekor qilinsa
    // asl ro'yxat tegilmaydi).
    modalDraft = row.selected.map(function (v) {
      return row.opt && row.opt.custom
        ? { uz: v.uz, ru: v.ru, value: v.value, skuValue: v.skuValue }
        : v;
    });
    lastFocus = document.activeElement;
    if (modalSearch) modalSearch.value = '';
    // Qidiruv faqat tayyor qiymatlar ro'yxati uchun mantiqiy.
    var searchWrap = document.getElementById('ntValueSearchWrap');
    if (searchWrap) searchWrap.hidden = !!(row.opt && row.opt.custom);
    var mTitle = document.getElementById('ntValueTitle');
    if (mTitle) {
      mTitle.textContent = (row.opt && row.opt.custom)
        ? tr('Xususiyatlar qiymati', 'Значения характеристики')
        : tr('Xususiyatlarni tanlang', 'Выбрать характеристики');
    }
    renderValueList();
    modal.hidden = false;
    document.addEventListener('keydown', modalKeys);
    if (modalSearch) modalSearch.focus();
  }

  function closeValueModal() {
    if (!modal) return;
    modal.hidden = true;
    modalRow = null;
    document.removeEventListener('keydown', modalKeys);
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }

  function modalKeys(e) {
    if (e.key === 'Escape') { closeValueModal(); return; }
    if (e.key !== 'Tab' || modal.hidden) return;
    // Fokus tuzog'i — modal ichida qoladi (a11y)
    var f = modal.querySelectorAll('button, input, [tabindex]:not([tabindex="-1"])');
    if (!f.length) return;
    var first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }

  // MAXSUS xususiyatda tayyor qiymat yo'q — Uzumdagidek uz/ru juftliklari
  // kiritiladi (bandl `X()`: {skuValue:"", title:{ru,uz}, value, isNew:true};
  // `te()`: IKKALA til majburiy, bir xil qiymat takrorlanmasin — same_fields).
  function renderCustomValueEditor() {
    modalList.textContent = '';
    if (!modalDraft.length) modalDraft.push({ uz: '', ru: '', value: '', skuValue: '' });

    modalDraft.forEach(function (v, idx) {
      var row = document.createElement('div');
      row.className = 'nt-cval';

      [['uz', tr('Oʻzbek tilida nom', 'Название на узбекском')],
       ['ru', tr('Rus tilida nom', 'Название на русском')]].forEach(function (p) {
        var key = p[0];
        var wrap = document.createElement('div');
        wrap.className = 'nt-cval-f';
        var lab = document.createElement('label');
        lab.className = 'nt-label';
        lab.textContent = p[1];
        var inp = document.createElement('input');
        inp.className = 'nt-input';
        inp.type = 'text';
        inp.maxLength = 100;
        inp.autocomplete = 'off';
        inp.value = v[key] || '';
        inp.addEventListener('input', function () { v[key] = inp.value; });
        wrap.appendChild(lab);
        wrap.appendChild(inp);
        row.appendChild(wrap);
      });

      // Oxirgi qatordan boshqasida — o'chirish.
      if (modalDraft.length > 1) {
        var del = document.createElement('button');
        del.type = 'button';
        del.className = 'nt-btn nt-btn--del nt-cval-del';
        del.textContent = tr('O’chirish', 'Удалить');
        del.addEventListener('click', function () {
          modalDraft.splice(idx, 1);
          renderCustomValueEditor();
        });
        row.appendChild(del);
      }

      var err = document.createElement('p');
      err.className = 'nt-err';
      err.hidden = true;
      err.setAttribute('data-cval-err', String(idx));
      row.appendChild(err);

      modalList.appendChild(row);
    });

    var add = document.createElement('button');
    add.type = 'button';
    add.className = 'nt-btn nt-btn--soft';
    add.textContent = tr('Qiymat qoʻshish', 'Добавить значение');
    add.addEventListener('click', function () {
      modalDraft.push({ uz: '', ru: '', value: '', skuValue: '' });
      renderCustomValueEditor();
    });
    modalList.appendChild(add);
  }

  // `te()` ekvivalenti — tozalash + tekshirish. Yaroqli bo'lsa qiymatlar
  // ro'yxatini qaytaradi, aks holda null (xatolar joyida ko'rsatiladi).
  function collectCustomValues() {
    var out = [];
    var bad = false;
    var seen = {};
    var reqMsg = tr('Toʻldirilishi shart boʻlgan maydon', 'Обязательное поле');
    var dupMsg = tr('Maydonlar bir xil', 'Поля совпадают');

    modalDraft.forEach(function (v, idx) {
      var uz = ncNorm(v.uz), ru = ncNorm(v.ru);
      var err = modalList.querySelector('[data-cval-err="' + idx + '"]');
      var show = function (m) { if (err) { err.textContent = m; err.hidden = false; } bad = true; };
      if (err) { err.hidden = true; err.textContent = ''; }
      // Butunlay bo'sh qator — e'tiborsiz qoldiriladi (bandl ham shunday).
      if (!uz && !ru) return;
      if (!uz || !ru) { show(reqMsg); return; }
      var key = (uz + ' ' + ru).toLowerCase();
      if (seen[key]) { show(dupMsg); return; }
      seen[key] = true;
      out.push({
        uz: uz, ru: ru,
        // Bandl: value = joriy tildagi sarlavha, skuValue = "" (yangi qiymat).
        value: tr(uz, ru),
        skuValue: ''
      });
    });
    return bad ? null : out;
  }

  function renderValueList() {
    if (!modalList || !modalRow) return;
    if (modalRow.opt && modalRow.opt.custom) { renderCustomValueEditor(); return; }
    modalList.textContent = '';
    var q = (modalSearch && modalSearch.value || '').trim().toLowerCase();
    var vals = modalRow.opt.values.filter(function (v) {
      if (!q) return true;
      return ((v.uz || '') + ' ' + (v.ru || '')).toLowerCase().indexOf(q) !== -1;
    });
    if (!vals.length) {
      var em = document.createElement('div');
      em.className = 'nt-modal-empty';
      em.textContent = tr('Hech narsa topilmadi', 'Ничего не найдено');
      modalList.appendChild(em);
      return;
    }
    vals.forEach(function (v) {
      var lab = document.createElement('label');
      lab.className = 'nt-opt';
      var cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = modalDraft.some(function (d) { return d.skuValue === v.skuValue; });
      cb.addEventListener('change', function () {
        if (cb.checked) modalDraft.push(v);
        else modalDraft = modalDraft.filter(function (d) { return d.skuValue !== v.skuValue; });
      });
      lab.appendChild(cb);
      // Rang doirasi FAQAT qiymat HEX bo'lsa (005 = rangli, 019 = doirasiz)
      if (isHex(v.value)) {
        var sw = document.createElement('span');
        sw.className = 'nt-opt-swatch';
        sw.style.background = v.value;
        sw.setAttribute('aria-hidden', 'true');
        lab.appendChild(sw);
      }
      lab.appendChild(document.createTextNode(tr(v.uz, v.ru) || v.ru || v.uz));
      modalList.appendChild(lab);
    });
  }

  if (modalSearch) modalSearch.addEventListener('input', renderValueList);
  if (modalSave) {
    modalSave.addEventListener('click', function () {
      if (modalRow) {
        if (modalRow.opt && modalRow.opt.custom) {
          var vals = collectCustomValues();
          if (vals === null) return;      // xato bor — modal ochiq qoladi
          modalDraft = vals;
        }
        modalRow.selected = modalDraft.slice();
        // «≤2 xususiyat» cap: qiymatli defined-char soni ≤2 bo'lishi shart.
        // JONLI (2026-07-19): 3+ qiymatli char → createProduct 400
        // validation-failed-001 (Uzum SKU = 2-o'lchovli matritsa; TUR
        // ahamiyatsiz — rang ham sanaladi). Yangi qiymat tanlanganda son 2 dan
        // oshsa — eng ESKI boshqa xususiyat(lar) tozalanadi. Himoya: hozir
        // tanlangan qator + rang (id -1, odatda kerak). Bitta char ICHIDA ko'p
        // qiymat NORMAL (poyabzal 36/37/38 → 201).
        var cleared = s1EnforceCharCap(modalRow);
        if (cleared) {
          notify(tr('Ko‘pi bilan 2 ta xususiyat tanlanadi',
                    'Можно выбрать не более 2 характеристик'));
        }
      }
      closeValueModal();
      renderRows();
      validate();
    });
  }
  ['ntValueClose', 'ntValueCancel'].forEach(function (id) {
    var b = document.getElementById(id);
    if (b) b.addEventListener('click', closeValueModal);
  });
  if (modal) {
    modal.addEventListener('click', function (e) {
      if (e.target === modal) closeValueModal();   // fon bosilsa yopiladi
    });
  }

  // ── Panellarni tashqi klik bilan yopish ─────────────────────────
  document.addEventListener('click', function (e) {
    if (catLevelsEl && !catLevelsEl.contains(e.target)) closeAllLevels();
    if (charPanel && !charPanel.hidden && charBtn && !charBtn.contains(e.target) &&
        !charPanel.contains(e.target)) closeCharPanel();
  });

  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    closeAllLevels();
    if (charPanel && !charPanel.hidden) closeCharPanel();
  });

  // ── Boshlang'ich holat ──────────────────────────────────────────
  syncCategoryDependent();
  validate();
})();
