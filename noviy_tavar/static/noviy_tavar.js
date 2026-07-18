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
    shop: null,
    tipsHidden: false
  };

  try {
    var shops = JSON.parse(root.dataset.shops || '[]');
    if (shops.length) state.shop = shops[0].uzum_id;
  } catch (e) { /* shops yo'q — proxy 400 qaytaradi, UI tirik qoladi */ }

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
  }

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
    var ok = !!state.categoryId && !!(titleUz || titleRu) && !!(descUz || descRu)
             && (!needPhoto || state.images.length > 0)
             && requiredFiltersFilled()
             && certificatesValid();
    var save = document.getElementById('ntSave');
    if (save) save.disabled = !ok;
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
      var prev = saveBtn.textContent;
      saveBtn.disabled = true;
      saveBtn.textContent = tr('Saqlanmoqda...', 'Сохранение...');

      var fvals = [];
      Object.keys(state.filterValues).forEach(function (fid) {
        var v = state.filterValues[fid];
        if (v != null) fvals.push({ filterId: Number(fid), filterValueId: v });
      });

      var chars = state.rows.filter(function (r) { return r.selected.length; })
        .map(function (r, i) {
          return {
            characteristicId: r.id,
            characteristicTitle: { uz: r.opt.uz, ru: r.opt.ru },
            orderingNumber: i,
            // ⚠️ requiredType + flowA — create tanasi uchun MAJBURIY (aks holda
            // o'lcham xarakteristikasi validation-failed). client.py chiqaradi.
            requiredType: r.opt.requiredType || 'NOT_REQUIRED',
            flowA: !!r.opt.flowA,
            // Qiymatlar — portal bergan obyektlar VERBATIM (client shuni kutadi).
            values: r.selected.map(function (v) {
              return { title: { uz: v.uz, ru: v.ru }, value: v.value, skuValue: v.skuValue };
            })
          };
        });

      fetch('/noviy-tavar/api/create', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({
          shop: state.shop,
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
                   tr('Qoralama yaratilmadi', 'Не удалось создать черновик'));
            return;
          }
          notify(tr('Qoralama yaratildi: #' + res.d.id,
                    'Черновик создан: #' + res.d.id));
          // Bandl ham muvaffaqiyatdan keyin keyingi qadamga o'tadi.
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
    loading: false
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
  function s2RowError(row) {
    var full = s2Num(row.fullPrice), off = s2Num(row.off);
    if (!(full > 0)) return tr('Narxni toʻldiring', 'Заполните цену');
    if (!((off === 0 && full === 0) || off < full)) {
      return tr('Chegirma summasi tovar summasidan oshmasligi yoki unga teng boʻlmasligi kerak',
                'Сумма скидки не должна превышать или быть равной сумме товара');
    }
    // ⚠️ Xabar «1000 ga karrali» deydi, tekshiruv esa `% 10` — Uzumning O'Z
    // nomuvofiqligi (f231: `var l = f(10)`). Foydalanuvchi 1:1 taqlidni tanladi.
    if (!(off === 0 || s2Mult(off))) {
      return tr('Chegirma 1000 ga karrali boʻlishi kerak',
                'Скидка должна быть кратной тысяче');
    }
    if (!s2Mult(full)) {
      return tr('Narx 1000 ga karrali boʻlishi kerak', 'Цена должна быть кратной тысяче');
    }
    if (!String(row.ikpu || '').trim()) {
      return tr('MXIK maydonini toʻldirilishi shart', 'Поле ИКПУ обязательно для заполнения');
    }
    if (row.ikpuValid === false) return tr('Notogʻri MXIK', 'Неверный ИКПУ');
    return null;
  }

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
    inp.addEventListener('input', function () {
      row[field] = inp.value;
      if (field === 'fullPrice' || field === 'off') { s2Sync(i); s2Commission(); }
      s2Validate();
    });
    wrap.appendChild(inp);
    if (opts.unit) {
      var u = document.createElement('span');
      u.className = 'nt-td-unit';
      u.textContent = opts.unit;
      wrap.appendChild(u);
    }
    td.appendChild(wrap);
    return td;
  }

  function s2Render() {
    var tb = document.getElementById('ntSkuRows');
    var empty = document.getElementById('ntSkuEmpty');
    if (!tb) return;
    tb.textContent = '';
    var vis = s2Visible();
    if (empty) {
      empty.hidden = vis.length > 0;
      empty.textContent = tr('Bu bo‘limda SKU yo‘q', 'В этом разделе нет SKU');
    }
    vis.forEach(function (row) {
      var i = S2.rows.indexOf(row);
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
      var ib = document.createElement('button');
      ib.type = 'button';
      ib.className = 'nt-ikpu-btn' + (row.ikpu ? '' : ' is-empty');
      ib.textContent = row.ikpu || tr('Tanlang', 'Выберите');
      ib.setAttribute('aria-label', tr('IKPU tanlash', 'Выбрать ИКПУ'));
      if (row.canEdit === false) ib.disabled = true;
      ib.addEventListener('click', function (e) {
        e.stopPropagation();
        s2OpenIkpu(ib, i);
      });
      tdI.appendChild(ib);
      tr_.appendChild(tdI);

      // 6-9. O'lchovlar — HAMMASI-YOKI-HECHNARSA (bandl `lt()`: bittasi bo'sh
      //      bo'lsa `dimensions` BUTUNLAY tushadi). Buni foydalanuvchiga
      //      ko'rsatish uchun s2Validate() da ogohlantiramiz.
      var mm = tr('mm', 'мм'), gg = tr('g', 'г');
      tr_.appendChild(s2Cell(row, i, 'width', { numeric: true, unit: mm, label: tr('Eni', 'Ширина') }));
      tr_.appendChild(s2Cell(row, i, 'length', { numeric: true, unit: mm, label: tr('Uzunligi', 'Длина') }));
      tr_.appendChild(s2Cell(row, i, 'height', { numeric: true, unit: mm, label: tr('Balandligi', 'Высота') }));
      tr_.appendChild(s2Cell(row, i, 'weight', { numeric: true, unit: gg, label: tr('Ogʻirligi', 'Вес') }));

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
  function s2Validate() {
    var saveBtn2 = document.getElementById('ntSave');
    var err = null;
    var pf = document.getElementById('ntSkuPrefix');
    var pe = s2PrefixError(pf ? pf.value : '');
    var pErr = document.getElementById('ntSkuPrefixErr');
    if (pErr) { pErr.textContent = pe || ''; pErr.hidden = !pe; }
    if (pe) err = pe;

    if (!err) {
      for (var i = 0; i < S2.rows.length; i++) {
        var e = s2RowError(S2.rows[i]);
        if (e) { err = e; break; }
      }
    }
    if (saveBtn2 && state.step === 2) saveBtn2.disabled = !!err || !S2.rows.length;
    return !err && S2.rows.length > 0;
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
      S2.rows.forEach(function (r) {
        // Bandl: `canEdit` false yoki bloklangan SKU'ga o'lchov yozilmaydi.
        if (r.canEdit === false) return;
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

  function s2CloseIkpu() {
    if (ikpuPop) ikpuPop.hidden = true;
    ikpuTarget = null;
  }

  function s2OpenIkpu(anchor, i) {
    if (!ikpuPop) return;
    s2CloseBulk.call(null);
    ikpuTarget = i;
    ikpuPop.hidden = false;
    var r = anchor.getBoundingClientRect();
    ikpuPop.style.top = (window.scrollY + r.bottom + 6) + 'px';
    ikpuPop.style.left = Math.max(8, window.scrollX + r.left) + 'px';
    if (ikpuSearch) { ikpuSearch.value = ''; ikpuSearch.focus(); }
    if (ikpuList) ikpuList.textContent = '';
    s2IkpuNote(tr('Iltimos, uchtadan ortiq belgi kiriting', 'Введите более трех символов'));
  }

  function s2IkpuNote(msg) {
    if (!ikpuNote) return;
    ikpuNote.textContent = msg || '';
    ikpuNote.hidden = !msg;
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
      })
      .catch(function () { s2IkpuNote(tr('Tarmoq xatosi', 'Ошибка сети')); });
  }

  function s2PickIkpu(it) {
    var apply = function (r) {
      if (r.canEdit === false) return;
      r.ikpu = it.ikpu;
      r.ikpuValid = it.validForCategory !== false;
    };
    if (ikpuTarget === -1) S2.rows.forEach(apply);
    else if (S2.rows[ikpuTarget]) apply(S2.rows[ikpuTarget]);
    s2CloseIkpu();
    s2CloseBulk();
    s2Render();
  }

  document.addEventListener('click', function (e) {
    if (ikpuPop && !ikpuPop.hidden && !ikpuPop.contains(e.target)) s2CloseIkpu();
    if (bulkPop && !bulkPop.hidden && !bulkPop.contains(e.target)) s2CloseBulk();
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
    if (!s2Validate()) {
      // Birinchi xatoni ko'rsatamiz (bandl `nt()` ham shunday qiladi).
      var pf = document.getElementById('ntSkuPrefix');
      var m = s2PrefixError(pf ? pf.value : '');
      if (!m) {
        for (var i = 0; i < S2.rows.length; i++) {
          m = s2RowError(S2.rows[i]);
          if (m) { m = (i + 1) + '-qator: ' + m; break; }
        }
      }
      notify(m || tr('Forma to‘liq emas', 'Форма заполнена не полностью'));
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

  function s2Show(on) {
    state.step = on ? 2 : 1;
    if (step1El) step1El.hidden = on;
    if (step2El) step2El.hidden = !on;
    // Qoralama yaratilgach do'kon almashmaydi — draft do'konga qat'iy
    // bog'langan. Bir marta o'chirilsa, ortga qaytsa ham o'chiq qoladi.
    if (on) { var _ss = document.getElementById('ntShopSelect'); if (_ss) _ss.disabled = true; }
    var legend = document.querySelector('.nt-required-legend');
    if (legend) legend.hidden = on;

    // Header qadam nishonlari
    var steps = document.querySelectorAll('.nt-step');
    if (steps.length >= 2) {
      steps[0].classList.toggle('is-active', !on);
      steps[0].classList.toggle('is-done', on);
      steps[1].classList.toggle('is-active', on);
      steps[0].removeAttribute('aria-current');
      steps[1].removeAttribute('aria-current');
      (on ? steps[1] : steps[0]).setAttribute('aria-current', 'step');
    }
    window.scrollTo(0, 0);
    if (on) s2Validate(); else validate();
  }

  // Qoralama yaratilgach 2-qadamga o'tish (bandl ham shunday qiladi:
  // muvaffaqiyatdan keyin `/{shopId}/products/id/{productId}/edit/filters`).
  function s2Enter(productId) {
    s2Load(productId).then(function (ok) {
      if (ok) {
        s2Show(true);
        // Qayta yuklashga chidamli: qoralama URL'da qoladi.
        try {
          var u = new URL(window.location.href);
          u.searchParams.set('productId', String(productId));
          u.searchParams.set('shop', String(state.shop));
          window.history.replaceState({}, '', u.toString());
        } catch (e) { /* eski brauzer — URL yangilanmaydi, UI ishlaydi */ }
      }
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
    loading: false
  };

  var step3El = document.getElementById('ntStep3');
  var ENUM_SINGLE = ['enum', 'localizableEnum'];
  var ENUM_MULTI = ['enumArray', 'localizableEnumArray'];
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
    maxValues:   tr('maks. qiymatlar:', 'макс. значений:'),
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
  }

  // ── Jadval renderi ──────────────────────────────────────────────
  function s3MaxLabel(n) {
    // limits.max_values — «maks. qiymatlar: {n}» / «макс. значений: {n}»
    return (NT_S3.maxValues || '') + ' ' + n;
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

    // Sarlavha: «Товar» + har atribut.
    var ths = ['<th class="nt-th nt-th--sticky">' + esc(NT_S3.colProduct) + '</th>'];
    S3.attrs.forEach(function (a) {
      var sub = '';
      var mv = (a.dataProperties || {}).maxValues;
      if (s3IsMulti(a.valueType) && mv) {
        sub = '<span class="nt-s3-th-sub">' + esc(s3MaxLabel(mv)) + '</span>';
      }
      var req = a.required ? '<span class="nt-req" aria-hidden="true">*</span>' : '';
      ths.push('<th class="nt-th nt-s3-th">' + esc(s3AttrName(a)) + req + sub + '</th>');
    });
    head.innerHTML = ths.join('');

    // Qatorlar: har SKU.
    body.innerHTML = '';
    S3.sku.forEach(function (sk) {
      var tr_ = document.createElement('tr');
      var tds = ['<td class="nt-td nt-td--sticky">' + s3ProductCell(sk) + '</td>'];
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

  function s3ProductCell(sk) {
    // sku qatoridan rang/nom — bandl skuTitle.split('-').slice(2).
    var title = String(sk.skuTitle || '');
    var parts = title.split('-').slice(2);
    var sub = parts.join('-');
    return '<div class="nt-s3-prod"><span class="nt-s3-prod-top">' + esc(sub || title) +
           '</span></div>';
  }

  // Bitta katakni to'ldirish — valueType bo'yicha turli boshqaruv.
  function s3FillCell(td, sk, a) {
    if (!td) return;
    var vt = a.valueType;
    var raw = s3Get(sk.skuId, a.attributeCode);
    if (s3IsEnum(vt)) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'nt-s3-combo';
      s3ComboLabel(btn, a, raw);
      btn.addEventListener('click', function (e) {
        e.stopPropagation();
        s3OpenEnum(btn, sk, a);
      });
      td.appendChild(btn);
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
    } else if (vt === 'boolean') {
      var seg = document.createElement('div');
      seg.className = 'nt-s3-bool';
      [['1', NT_S3.yes, true], ['0', NT_S3.no, false]].forEach(function (opt) {
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'nt-s3-bool-btn' + (raw === opt[2] ? ' is-on' : '');
        b.textContent = opt[1];
        b.addEventListener('click', function () {
          var cur = s3Get(sk.skuId, a.attributeCode);
          var next = (cur === opt[2]) ? null : opt[2];  // qayta bossa — tozalash
          s3Set(sk.skuId, a.attributeCode, next);
          seg.querySelectorAll('.nt-s3-bool-btn').forEach(function (x) { x.classList.remove('is-on'); });
          if (next === opt[2]) b.classList.add('is-on');
        });
        seg.appendChild(b);
      });
      td.appendChild(seg);
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
  }

  function s3UnitLabel(unit) {
    // Bandl unit -> qisqartma (length_millimeter -> mm, weight_gram -> g).
    if (/millimeter|_mm/.test(unit)) return tr('mm', 'мм');
    if (/gram|_g\b/.test(unit)) return tr('g', 'г');
    return '';
  }

  // Enum combo yorlig'i — tanlanган qiymat(lar) yoki placeholder.
  function s3ComboLabel(btn, a, raw) {
    var multi = s3IsMulti(a.valueType);
    var chosen = [];
    var cache = s3EnumCache[a.attributeCode] || [];
    function titleOf(code) {
      for (var i = 0; i < cache.length; i++) if (cache[i].code === code) return s3EnumTitle(cache[i]);
      return code;
    }
    if (multi && Array.isArray(raw) && raw.length) {
      chosen = raw.map(titleOf);
    } else if (!multi && raw) {
      chosen = [titleOf(raw)];
    }
    if (chosen.length) {
      btn.classList.remove('is-placeholder');
      btn.textContent = chosen.join(', ');
    } else {
      btn.classList.add('is-placeholder');
      btn.textContent = multi ? NT_S3.chooseMulti : NT_S3.choose;
    }
  }

  // ── Enum dropdown popoveri (bitta nusxa) ────────────────────────
  var s3Pop = document.getElementById('ntS3Pop');
  var s3PopList = document.getElementById('ntS3PopList');
  var s3PopSearch = document.getElementById('ntS3Search');
  var s3PopCtx = null;   // {btn, sk, a}

  function s3OpenEnum(btn, sk, a) {
    s3PopCtx = { btn: btn, sk: sk, a: a };
    if (s3PopSearch) s3PopSearch.value = '';
    s3PositionPop(btn);
    if (s3Pop) s3Pop.hidden = false;
    if (s3PopList) s3PopList.innerHTML = '<p class="nt-s3-pop-empty">' + esc(NT_S3.search) + '</p>';
    s3FetchEnums(a.attributeCode).then(function () { s3RenderPop(''); });
    if (s3PopSearch) s3PopSearch.focus();
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
      s3RenderPop(s3PopSearch ? s3PopSearch.value : '');
    } else {
      var next = (raw === code) ? null : code;
      s3Set(sk.skuId, a.attributeCode, next);
      s3CloseEnum();
    }
    // Combo yorlig'ini yangilash.
    if (s3PopCtx && s3PopCtx.btn) s3ComboLabel(s3PopCtx.btn, a, s3Get(sk.skuId, a.attributeCode));
  }

  function s3CloseEnum() { if (s3Pop) s3Pop.hidden = true; s3PopCtx = null; }
  if (s3PopSearch) s3PopSearch.addEventListener('input', function () { s3RenderPop(s3PopSearch.value.trim()); });
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

  function s3Send() {
    if (!S3.sku.length || !S3.attrs.length) {
      notify(NT_S3.fbMain);
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
        // Bandl `properties_saved` — Uzum matni.
        notify(NT_S3.saved);
      })
      .catch(function () {
        if (btn) { btn.disabled = false; btn.textContent = prev; }
        notify(tr('Tarmoq xatosi', 'Ошибка сети'));
      });
  }

  // ── Qadam almashish ─────────────────────────────────────────────
  function s3Show(on) {
    state.step = on ? 3 : 2;
    if (step1El) step1El.hidden = true;
    if (step2El) step2El.hidden = on;
    if (step3El) step3El.hidden = !on;
    var legend = document.querySelector('.nt-required-legend');
    if (legend) legend.hidden = true;
    var steps = document.querySelectorAll('.nt-step');
    if (steps.length >= 3) {
      steps[0].classList.remove('is-active'); steps[0].classList.add('is-done');
      steps[1].classList.toggle('is-active', !on); steps[1].classList.toggle('is-done', on);
      steps[2].classList.toggle('is-active', on);
      steps[0].removeAttribute('aria-current');
      steps[1].removeAttribute('aria-current');
      steps[2].removeAttribute('aria-current');
      (on ? steps[2] : steps[1]).setAttribute('aria-current', 'step');
    }
    window.scrollTo(0, 0);
    var save = document.getElementById('ntSave');
    if (save) save.disabled = on ? !(S3.sku.length && S3.attrs.length) : false;
  }

  function s3Enter(productId) {
    s3Load(productId).then(function (ok) {
      if (ok) {
        s3Show(true);
        try {
          var u = new URL(window.location.href);
          u.searchParams.set('productId', String(productId));
          u.searchParams.set('shop', String(state.shop));
          u.searchParams.set('step', '3');
          window.history.replaceState({}, '', u.toString());
        } catch (e) { /* eski brauzer — URL yangilanmaydi, UI ishlaydi */ }
      }
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

  // ?productId=&shop=[&step=3] bilan ochilsa — to'g'ridan-to'g'ri o'sha qadam.
  // Jonli qoralamani (#3068623) tekshirish yo'li ham shu.
  (function () {
    try {
      var q = new URLSearchParams(window.location.search);
      var pid = Number(q.get('productId') || 0);
      var sh = q.get('shop');
      var st = Number(q.get('step') || 0);
      if (sh) state.shop = sh;
      if (pid > 0) { if (st === 3) s3Enter(pid); else s2Enter(pid); }
    } catch (e) { /* URLSearchParams yo'q — 1-qadam ochiladi */ }
  })();

  // ── Do'kon tanlagich (shablon faqat >1 do'konda render qiladi) ──
  // Kartochka yaratiladigan do'konni ANIQ ko'rsatadi/tanlaydi (ilgari
  // jimgina shops[0] edi). ?shop= yoki qoralama URL'i state.shop'ni
  // yuqorida o'rnatgan bo'lishi mumkin — select'ni shunga moslaymiz.
  (function () {
    var sel = document.getElementById('ntShopSelect');
    if (!sel) return;
    if (state.shop) sel.value = state.shop;
    // Agar joriy state.shop ro'yxatda bo'lmasa (masalan noto'g'ri ?shop=),
    // select birinchi variantda qoladi — state'ni shunga tortamiz.
    if (sel.value) state.shop = sel.value;
    if (state.step !== 1) sel.disabled = true;   // draft URL'i bilan ochilgan
    sel.addEventListener('change', function () {
      if (sel.disabled) return;
      state.shop = sel.value;
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
    editing: false   // «Изменить» rejimi (o'shanda «Отмена» chiqadi)
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
    if (cat.cache[key]) return Promise.resolve({ items: cat.cache[key] });
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
    var lv = cat.levels[i];
    lv.selected = { id: c.id, title: catTitle(c) };
    lv.query = '';
    lv.open = false;
    // Bu darajadan pastdagilar bekor bo'ladi.
    cat.levels = cat.levels.slice(0, i + 1);
    cat.confirmed = null;
    clearCategoryState();

    if (c.hasChildren && c.hasActiveChildren !== false) {
      // Ostiga yangi «Выбрать подкатегорию» select'i qo'shiladi.
      renderLevels();
      fetchCategories(c.id).then(function (res) {
        cat.levels.push({ items: res.items, selected: null, open: false,
                          query: '', error: res.error, parentId: c.id });
        renderLevels();
        syncAccept();
      });
    } else {
      renderLevels();   // barg — «Принять» yonadi
    }
    syncAccept();
  }

  function leafSelected() {
    if (!cat.levels.length) return null;
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

  // ── «Принять» → kategoriya meta → forma ochiladi ────────────────
  if (acceptBtn) {
    acceptBtn.addEventListener('click', function () {
      var leaf = leafSelected();
      if (!leaf || !state.shop) return;
      acceptBtn.disabled = true;
      var prev = acceptBtn.textContent;
      acceptBtn.textContent = tr('Yuklanmoqda...', 'Загрузка...');
      fetch('/noviy-tavar/api/category-meta?shop=' + encodeURIComponent(state.shop) +
            '&categoryId=' + encodeURIComponent(leaf.id),
            { credentials: 'same-origin' })
        .then(function (r) { return r.ok ? r.json() : null; })
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

  // Boshlang'ich daraja — ildizlar
  fetchCategories(null).then(function (res) {
    cat.levels = [{ items: res.items, selected: null, open: false,
                    query: '', error: res.error, parentId: null }];
    renderLevels();
    syncAccept();
  });

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
        // ⚠️ create tanasi requiredType + flowA ni MAJBURIY yuboradi (tarmoq
        // dalili: 133/133 referens). Meta'dagi xom qiymatni saqlab qolamiz.
        requiredType: c.requiredType || 'NOT_REQUIRED',
        flowA: !!c.flowA,
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
  }

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
    modalDraft = row.selected.slice();
    lastFocus = document.activeElement;
    if (modalSearch) modalSearch.value = '';
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

  function renderValueList() {
    if (!modalList || !modalRow) return;
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
      if (modalRow) modalRow.selected = modalDraft.slice();
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
