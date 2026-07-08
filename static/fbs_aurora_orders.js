/* ============================================================
   FBS «Soft» — Buyurtmalar filter-paneli augmentatsiyasi.
   Mavjud kodga TEGMAYDI: ikkilamchi status chiplarini yashirib,
   «Boshqa holatlar» custom menyusiga ko'chiradi. Menyu elementi
   bosilganda mos (yashirin) chipni .click() qiladi — shu sabab
   barcha mavjud mantiq (setActiveStatus + loadOrders + active
   holat + count) o'zgarishsiz ishlaydi.
   Sahifa oxirida, asosiy <script>'dan KEYIN yuklanadi → chip
   click-listenerlari allaqachon ulangan bo'ladi.
   ============================================================ */
(function () {
  "use strict";
  var row = document.getElementById("fbsStatusRow");
  if (!row) return;
  // Localized «Boshqa holatlar…» label — fed from the server i18n bundle via
  // a data-attribute (this static JS can't read the Jinja list_labels dict).
  var moreLabel = row.getAttribute("data-more-label") || "Boshqa holatlar…";

  // Doim ko'rinadigan asosiy statuslar (segment-trek). Qolganlari menyuga.
  var PRIMARY = { CREATED: 1, PACKING: 1, DELIVERING: 1 };

  // Ikkilamchi statuslar uchun rang + ikonka. В пункте выдачи → Uzum belgisi.
  var MAP = {
    DELIVERED:                            { c: "green", i: "done" },
    ACCEPTED_AT_DP:                       { c: "blue",  i: "box"  },
    DELIVERED_TO_CUSTOMER_DELIVERY_POINT: { c: "uzum",  i: "uzum" },
    COMPLETED:                            { c: "slate", i: "done" },
    CANCELED:                             { c: "red",   i: "x"    },
    PENDING_CANCELLATION:                 { c: "red",   i: "x"    },
    RETURNED:                             { c: "rose",  i: "ret"  }
  };

  var SVG = {
    cv:   '<svg class="fbsx-cv" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>',
    ck:   '<svg class="fbsx-ck" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>',
    done: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>',
    box:  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/></svg>',
    x:    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>',
    ret:  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 14 4 9 9 4"/><path d="M20 20v-7a4 4 0 0 0-4-4H4"/></svg>',
    uzum: '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="8.4" fill="none" stroke="#fff" stroke-width="1.7"/><path d="M8.5 9.6 v3.0 a3.5 3.5 0 0 0 7 0 v-3.0" fill="none" stroke="#fff" stroke-width="2.15" stroke-linecap="round"/><path d="M12 7.6 v3.4" stroke="#fff" stroke-width="2.15" stroke-linecap="round"/></svg>'
  };

  // «Поставка» nav'ga PENDING_DELIVERY soni qo'shamiz (loadCounts avtomat to'ldiradi).
  var nav = row.querySelector(".fbs-status-nav");
  if (nav && !nav.querySelector("[data-status-count]")) {
    var navCt = document.createElement("span");
    navCt.className = "fbs-status-chip-count";
    navCt.setAttribute("data-status-count", "PENDING_DELIVERY");
    navCt.textContent = "·";
    nav.appendChild(navCt);
  }

  // Ikkilamchi chiplarni yig'amiz.
  var chips = Array.prototype.slice.call(row.querySelectorAll(".fbs-status-chip"));
  var secondary = chips.filter(function (ch) {
    return !PRIMARY[ch.getAttribute("data-status")];
  });
  if (!secondary.length) return;

  var dd = document.createElement("div");
  dd.className = "fbsx-dd";
  var trigger = document.createElement("button");
  trigger.type = "button";
  trigger.className = "fbsx-trigger";
  trigger.innerHTML = '<span class="fbsx-trig-label">' + moreLabel + '</span>' + SVG.cv;
  var menu = document.createElement("div");
  menu.className = "fbsx-menu";
  var card = document.createElement("div");
  card.className = "fbsx-card";

  function labelOf(ch) {
    var span = ch.querySelector("span");
    return (span ? span.textContent : ch.textContent || "").trim();
  }

  secondary.forEach(function (ch) {
    var st = ch.getAttribute("data-status");
    var label = labelOf(ch);
    var m = MAP[st] || { c: "slate", i: "done" };
    var iconHtml = (m.i === "uzum")
      ? '<span class="fbsx-uz">' + SVG.uzum + "</span>"
      : '<span class="fbsx-ic c-' + m.c + '">' + (SVG[m.i] || SVG.done) + "</span>";

    var item = document.createElement("div");
    item.className = "fbsx-mi";
    item.setAttribute("data-status", st);
    item.innerHTML =
      iconHtml +
      '<span class="fbsx-lbl">' + label + "</span>" +
      '<span class="fbsx-ct" data-status-count="' + st + '">·</span>' +
      SVG.ck;
    item.addEventListener("click", function (e) {
      e.stopPropagation();
      ch.click();                 // mavjud setActiveStatus + loadOrders
      setActiveLabel(label, st);
      dd.classList.remove("open");
    });
    card.appendChild(item);

    // Eski (yashirin) chipdagi count atributini olib tashlaymiz — aks holda
    // loadCounts() birinchi mosni (yashirin chipni) yangilab, menyuni emas.
    var oldCt = ch.querySelector("[data-status-count]");
    if (oldCt) oldCt.removeAttribute("data-status-count");
    ch.style.display = "none";     // chipni trekdan yashiramiz
  });

  menu.appendChild(card);
  dd.appendChild(trigger);
  dd.appendChild(menu);
  row.appendChild(dd);

  trigger.addEventListener("click", function (e) {
    e.stopPropagation();
    dd.classList.toggle("open");
  });
  document.addEventListener("click", function () { dd.classList.remove("open"); });

  function setActiveLabel(label, st) {
    trigger.classList.add("is-active");
    trigger.querySelector(".fbsx-trig-label").textContent = label;
    card.querySelectorAll(".fbsx-mi").forEach(function (mi) {
      mi.classList.toggle("is-sel", mi.getAttribute("data-status") === st);
    });
  }
  function resetTrigger() {
    trigger.classList.remove("is-active");
    trigger.querySelector(".fbsx-trig-label").textContent = moreLabel;
    card.querySelectorAll(".fbsx-mi").forEach(function (mi) { mi.classList.remove("is-sel"); });
  }

  // Asosiy chip yoki «Поставка» bosilsa → trigger'ni «Boshqa holatlar»ga qaytaramiz.
  chips.forEach(function (ch) {
    if (PRIMARY[ch.getAttribute("data-status")]) ch.addEventListener("click", resetTrigger);
  });
  if (nav) nav.addEventListener("click", resetTrigger);

  // Sahifa ochilishida URL'dagi status ikkilamchi bo'lsa — trigger'da aks ettiramiz.
  var activeSec = secondary.filter(function (ch) { return ch.classList.contains("active"); })[0];
  if (activeSec) setActiveLabel(labelOf(activeSec), activeSec.getAttribute("data-status"));

  // ── Sirpanuvchi gradient thumb («Segment Glide») ──────────────
  // Aktiv tab (chip / «Поставка» nav / «Boshqa holatlar» trigger) ostida
  // suzadigan plita. Pozitsiyani JS o'rnatadi, sirpanishni CSS transition
  // beradi. Qayta joylash triggerlari: klass o'zgarishi (MutationObserver),
  // count yuklanishi (chip eni o'zgaradi), resize, font load.
  var thumb = document.createElement("div");
  thumb.className = "fbsx-thumb";
  row.insertBefore(thumb, row.firstChild);

  function placeThumb() {
    var cands = row.querySelectorAll(
      ".fbs-status-chip.active, .fbs-status-nav.is-active, .fbsx-trigger.is-active");
    var el = null;
    for (var i = 0; i < cands.length; i++) {
      // offsetParent === null → yashirin (menyuga ko'chgan chip) — o'tkazamiz.
      if (cands[i].offsetParent !== null) { el = cands[i]; break; }
    }
    if (!el) { thumb.style.opacity = "0"; return; }
    // offsetLeft ISHLATMAYMIZ: «Boshqa holatlar» trigger .fbsx-dd
    // (position:relative) ichida, uning offsetLeft'i trekka emas, o'ramga
    // nisbatan (0) — thumb chap chetga uchib ketardi. Rect farqi esa har
    // doim trekning padding-box'iga nisbatan to'g'ri chiqadi.
    var rowRect = row.getBoundingClientRect();
    var r = el.getBoundingClientRect();
    thumb.style.opacity = "1";
    thumb.style.left   = (r.left - rowRect.left - row.clientLeft) + "px";
    thumb.style.top    = (r.top  - rowRect.top  - row.clientTop)  + "px";
    thumb.style.width  = r.width + "px";
    thumb.style.height = r.height + "px";
  }
  var _thumbRaf = null;
  function queuePlace() {
    if (_thumbRaf) return;
    _thumbRaf = requestAnimationFrame(function () { _thumbRaf = null; placeThumb(); });
  }
  // DIQQAT: attributeFilter'da "style" YO'Q — placeThumb o'zi thumb style'ini
  // o'zgartiradi, kuzatsak cheksiz rAF aylanish bo'lardi. Klass almashishi va
  // count textContent yangilanishi (childList) yetarli.
  new MutationObserver(queuePlace).observe(row, {
    subtree: true, childList: true, attributes: true, attributeFilter: ["class"]
  });
  window.addEventListener("resize", queuePlace);
  window.addEventListener("load", queuePlace);
  placeThumb();
})();
