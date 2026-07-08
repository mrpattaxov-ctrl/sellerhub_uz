/* ============================================================
   FBS «Soft» — ixcham raqamli pagination + «Sahifada» pastda.
   Asosiy script'ga 3 ta nuqtada ulanadi (window hook'lari orqali):
     • window.__fbsPage        — joriy sahifa (0-indeksli); loadOrders o'qiydi
     • window.__fbsGoPage(p)   — sahifaga o'tib qayta yuklaydi (asosiy script beradi)
     • window.__fbsAfterRender(total,page) — har yuklamadan keyin chaqiriladi → pager render
   «Sahifada» select toolbar'dan pastki qatorga ko'chiriladi (listener'lar saqlanadi).
   ============================================================ */
(function () {
  "use strict";
  if (!document.querySelector(".fbs-page")) return;
  if (window.__fbsPage === undefined) window.__fbsPage = 0;

  var bar = document.getElementById("fbsBottomBar");
  var sizeSel = document.getElementById("fbsSize");
  if (!bar) return;

  var SVG_PREV = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"/></svg>';
  var SVG_NEXT = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>';

  // «Sahifada» control — toolbar wrapper'ini yashirib, select'ni pastga ko'chiramiz.
  var psize = document.createElement("span");
  psize.className = "fbsx-psize";
  psize.appendChild(document.createTextNode("Sahifada"));
  if (sizeSel) {
    var wrap = sizeSel.parentElement;          // <div><label>Sahifada</label><select>…
    if (wrap) wrap.style.display = "none";
    // fxsel (fbs_select.js) selektni `.fxsel` ichiga o'rashi mumkin. Bu IIFE
    // odatda fxsel'dan OLDIN ishlaydi (script tartibi: pager → select) — u holda
    // xom selektni ko'chiramiz, fxsel keyin uni shu yerda (psize ichida) o'raydi.
    // Lekin tartib o'zgarsa ham buzilmasin: wrapper bo'lsa butun `.fxsel`ni ko'chiramiz.
    psize.appendChild(sizeSel.closest(".fxsel") || sizeSel);
    sizeSel.addEventListener("change", function () {
      if (window.__fbsGoPage) window.__fbsGoPage(0);   // o'lcham o'zgarsa 1-sahifaga
    });
  }
  bar.appendChild(psize);                       // boshida hech bo'lmasa «Sahifada» ko'rinadi

  // Ellipsis bilan sahifa indekslari (0-indeksli): 0 … c-1 c c+1 … last
  function pageWindow(cur, totalPages) {
    var last = totalPages - 1;
    var keep = {};
    [0, last, cur, cur - 1, cur + 1].forEach(function (i) {
      if (i >= 0 && i <= last) keep[i] = 1;
    });
    var arr = Object.keys(keep).map(Number).sort(function (a, b) { return a - b; });
    var out = [];
    for (var k = 0; k < arr.length; k++) {
      if (k > 0 && arr[k] - arr[k - 1] > 1) out.push("…");
      out.push(arr[k]);
    }
    return out;
  }

  function pgBtn(svg, targetIdx, disabled) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "fbsx-pgbtn";
    b.innerHTML = svg;
    if (disabled) b.disabled = true;
    else b.addEventListener("click", function () {
      if (window.__fbsGoPage) window.__fbsGoPage(targetIdx);
    });
    return b;
  }
  function pgNum(idx, cur) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "fbsx-pgn" + (idx === cur ? " on" : "");
    b.textContent = String(idx + 1);            // 1-indeksli ko'rsatamiz
    if (idx !== cur) b.addEventListener("click", function () {
      if (window.__fbsGoPage) window.__fbsGoPage(idx);
    });
    return b;
  }

  // Har yuklamadan keyin asosiy script chaqiradi.
  window.__fbsAfterRender = function (total, page) {
    var size = sizeSel ? (parseInt(sizeSel.value, 10) || 20) : 20;
    var totalPages = Math.max(1, Math.ceil((total || 0) / size));
    var cur = Math.min(Math.max(0, page | 0), totalPages - 1);
    bar.innerHTML = "";

    // Bir nechta sahifa bo'lsagina pager ko'rsatamiz; aks holda faqat «Sahifada».
    if (totalPages > 1) {
      var pager = document.createElement("span");
      pager.className = "fbsx-pager";
      pager.appendChild(pgBtn(SVG_PREV, cur - 1, cur <= 0));
      pageWindow(cur, totalPages).forEach(function (p) {
        if (p === "…") {
          var d = document.createElement("span");
          d.className = "fbsx-dots";
          d.textContent = "…";
          pager.appendChild(d);
        } else {
          pager.appendChild(pgNum(p, cur));
        }
      });
      pager.appendChild(pgBtn(SVG_NEXT, cur + 1, cur >= totalPages - 1));
      bar.appendChild(pager);
    }
    bar.appendChild(psize);
  };

  // Status / sxema / do'kon o'zgarsa → sahifa 0 ga (capture: asosiy handler'dan OLDIN).
  // «Поставка» (накладные iframe) o'z navigatsiyasiga ega → bu yerda pastki
  // «Sahifada» qatorini yashiramiz; oddiy status chipiga qaytganda ko'rsatamiz.
  function resetPage() { window.__fbsPage = 0; }
  var statusRow = document.getElementById("fbsStatusRow");
  var navChip = statusRow ? statusRow.querySelector(".fbs-status-nav") : null;
  if (statusRow) statusRow.addEventListener("click", function (e) {
    if (e.target.closest(".fbs-status-nav")) { resetPage(); bar.style.display = "none"; }
    else if (e.target.closest(".fbs-status-chip")) { resetPage(); bar.style.display = ""; }
  }, true);
  var schemeSel = document.getElementById("fbsScheme");
  if (schemeSel) schemeSel.addEventListener("change", resetPage, true);
  var shopSel = document.getElementById("fbsShop");
  if (shopSel) shopSel.addEventListener("change", resetPage, true);

  // Sahifa ?view=postavka bilan ochilgan bo'lsa — pastki qatorni darrov yashiramiz.
  if (navChip && navChip.classList.contains("is-active")) bar.style.display = "none";
})();
