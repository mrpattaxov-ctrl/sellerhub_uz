/* ============================================================
   FBS «Soft» — custom <select> (fxsel).
   Native <select> ochilganda brauzer chizadigan xunuk ko'k-highlight
   ro'yxatni dizaynga mos menyu bilan almashtiradi. Native select
   DOM'da QOLADI (yashirin) — value/change mantiqi o'zgarmaydi.

   Ishlatish:  <select data-fxsel data-fxsel-variant="bare|outline"
                       data-fxsel-minw="230px">…</select>

   Menyu BODY'ga portal qilinadi va position:fixed bilan joylanadi —
   shu sabab glass header'ning backdrop-filter stacking-konteksti uni
   qamab, qidiruv paneli/tugmalar ustiga chiqib bekitmaydi (bug fix).

   Avtomatik DOMContentLoaded'da barcha [data-fxsel]'ni yaxshilaydi;
   keyin qo'shilganlar uchun window.fxsel.refresh() chaqiring.
   CSS shu fayl ichida inject qilinadi → har sahifa faqat 1 script tagi.
   ============================================================ */
(function () {
  "use strict";

  var CHEVRON =
    '<svg class="fxsel-cv" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>';
  var CHECK =
    '<svg class="fxsel-ck" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';

  var CSS =
    ".fxsel{position:relative;display:inline-flex;align-items:center;font-family:inherit;}" +
    ".fxsel-native{position:absolute!important;width:1px;height:1px;padding:0;margin:-1px;" +
    "opacity:0;pointer-events:none;clip:rect(0 0 0 0);overflow:hidden;border:0;}" +
    ".fxsel-trigger{display:inline-flex;align-items:center;gap:.5rem;cursor:pointer;font-family:inherit;" +
    "font-weight:700;font-size:.95rem;line-height:1.2;color:var(--fx-fg,#1A1A22);background:transparent;" +
    "border:0;border-radius:10px;padding:.48rem .4rem;text-align:left;" +
    "transition:background .15s ease,border-color .15s ease,box-shadow .15s ease;}" +
    ".fxsel-val{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}" +
    ".fxsel-cv{width:15px;height:15px;flex:none;color:var(--fx-muted,#9B98A5);transition:transform .22s ease;margin-left:auto;}" +
    ".fxsel.open .fxsel-cv{transform:rotate(180deg);}" +
    ".fxsel--bare .fxsel-trigger:hover{background:rgba(127,127,160,.08);}" +
    ".fxsel--outline .fxsel-trigger{border:1px solid var(--fx-bd,#E6E6EC);background:var(--fx-surface,#fff);" +
    "border-radius:12px;padding:.55rem .9rem;font-weight:600;font-size:.9rem;width:100%;}" +
    ".fxsel--outline .fxsel-trigger:hover{border-color:var(--fx-bd-hover,#cfd2dc);}" +
    ".fxsel.open.fxsel--outline .fxsel-trigger{border-color:var(--fx-accent,#1E2435);box-shadow:0 0 0 4px var(--fx-ring,rgba(30,36,53,.10));}" +
    /* Menyu — BODY'ga portal, fixed. CSS o'zgaruvchilarni o'zi belgilaydi (root'dan tashqarida). */
    ".fxsel-menu{position:fixed;z-index:99999;" +
    "--fx-surface:#fff;--fx-bd:#E6E6EC;--fx-mi:#3A3A44;--fx-hover:#F4F5F8;" +
    "--fx-accent:#1E2435;--fx-accent-tint:rgba(30,36,53,.07);" +
    "opacity:0;transform:translateY(-8px) scale(.98);pointer-events:none;transform-origin:top center;" +
    "transition:opacity .15s ease,transform .16s cubic-bezier(.16,1,.3,1);}" +
    "[data-bs-theme=\"dark\"] .fxsel-menu{--fx-surface:#1c2334;--fx-bd:#2b3346;--fx-mi:#cbd5e1;" +
    "--fx-hover:#232a3d;--fx-accent:#c4d2ff;--fx-accent-tint:rgba(120,140,210,.16);}" +
    ".fxsel-menu.fxsel-up{transform-origin:bottom center;transform:translateY(8px) scale(.98);}" +
    ".fxsel-menu.open{opacity:1;transform:none;pointer-events:auto;}" +
    ".fxsel-card{background:var(--fx-surface);border:1px solid var(--fx-bd);border-radius:14px;" +
    "box-shadow:0 22px 50px -12px rgba(20,16,40,.26),0 4px 10px rgba(20,16,40,.05);" +
    "padding:6px;max-height:340px;overflow-y:auto;}" +
    "[data-bs-theme=\"dark\"] .fxsel-card{box-shadow:0 22px 50px -12px rgba(0,0,0,.6);}" +
    ".fxsel-mi{display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:10px;" +
    "cursor:pointer;font-weight:600;font-size:.9rem;color:var(--fx-mi);white-space:nowrap;" +
    "transition:background .12s ease,color .12s ease;}" +
    ".fxsel-mi:hover,.fxsel-mi.is-hl{background:var(--fx-hover);}" +
    ".fxsel-lbl{flex:1 1 auto;}" +
    ".fxsel-ck{width:17px;height:17px;flex:none;color:var(--fx-accent);opacity:0;}" +
    ".fxsel-mi.is-sel{background:var(--fx-accent-tint);color:var(--fx-accent);font-weight:700;}" +
    ".fxsel-mi.is-sel .fxsel-ck{opacity:1;}";

  function injectCss() {
    if (document.getElementById("fxsel-css")) return;
    var st = document.createElement("style");
    st.id = "fxsel-css";
    st.textContent = CSS;
    (document.head || document.documentElement).appendChild(st);
  }

  var openInst = null; // hozir ochiq turgan yagona dropdown

  function enhance(select) {
    if (select.__fxsel) return;
    select.__fxsel = true;

    var variant = select.getAttribute("data-fxsel-variant") || "outline";
    var minw = select.getAttribute("data-fxsel-minw");
    var alignRight = select.hasAttribute("data-fxsel-right");

    var root = document.createElement("div");
    root.className = "fxsel fxsel--" + variant;

    var trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "fxsel-trigger";
    trigger.setAttribute("aria-haspopup", "listbox");
    trigger.innerHTML = '<span class="fxsel-val"></span>' + CHEVRON;
    if (minw) trigger.style.minWidth = minw;

    // Menyu — BODY'ga portal qilinadi (stacking-konteksdan qochish uchun).
    var menu = document.createElement("div");
    menu.className = "fxsel-menu";
    menu.setAttribute("role", "listbox");
    var card = document.createElement("div");
    card.className = "fxsel-card";
    menu.appendChild(card);
    document.body.appendChild(menu);

    var wrap = select.parentNode;
    wrap.insertBefore(root, select);
    root.appendChild(trigger);
    root.appendChild(select);
    select.classList.add("fxsel-native");

    // Statik karet (masalan .inv-select-caret) endi keraksiz — yashiramiz.
    var stray = wrap.querySelector(".inv-select-caret");
    if (stray) stray.style.display = "none";

    var hl = -1; // klaviatura highlight indeksi

    function valText() {
      var o = select.options[select.selectedIndex];
      return o ? o.textContent : "";
    }
    function syncVal() {
      trigger.querySelector(".fxsel-val").textContent = valText();
    }
    function render() {
      card.innerHTML = "";
      Array.prototype.forEach.call(select.options, function (opt, i) {
        var mi = document.createElement("div");
        mi.className = "fxsel-mi" + (i === select.selectedIndex ? " is-sel" : "");
        mi.setAttribute("role", "option");
        mi.innerHTML = '<span class="fxsel-lbl"></span>' + CHECK;
        mi.querySelector(".fxsel-lbl").textContent = opt.textContent;
        mi.addEventListener("click", function (e) {
          e.stopPropagation();
          choose(i);
        });
        mi.addEventListener("mousemove", function () { setHl(i); });
        card.appendChild(mi);
      });
    }
    function items() { return card.querySelectorAll(".fxsel-mi"); }
    function markSel() {
      var its = items();
      for (var i = 0; i < its.length; i++) {
        its[i].classList.toggle("is-sel", i === select.selectedIndex);
      }
    }
    function setHl(i) {
      var its = items();
      if (hl >= 0 && its[hl]) its[hl].classList.remove("is-hl");
      hl = i;
      if (its[hl]) {
        its[hl].classList.add("is-hl");
        its[hl].scrollIntoView({ block: "nearest" });
      }
    }
    function choose(i) {
      if (i < 0 || i >= select.options.length) return;
      if (select.selectedIndex !== i) {
        select.selectedIndex = i;
        select.dispatchEvent(new Event("change", { bubbles: true }));
      }
      syncVal();
      markSel();
      close();
    }

    // Menyuni trigger ostiga (yoki joy yetmasa ustiga) fixed joylash.
    function position() {
      var r = trigger.getBoundingClientRect();
      menu.style.minWidth = r.width + "px";
      var mh = menu.offsetHeight || 0;
      var below = window.innerHeight - r.bottom;
      var up = below < mh + 12 && r.top > below; // pastda joy yo'q, tepada ko'proq
      if (up) {
        menu.classList.add("fxsel-up");
        menu.style.top = Math.max(8, r.top - mh - 6) + "px";
      } else {
        menu.classList.remove("fxsel-up");
        menu.style.top = (r.bottom + 6) + "px";
      }
      if (alignRight) {
        menu.style.left = "auto";
        menu.style.right = Math.max(8, window.innerWidth - r.right) + "px";
      } else {
        // Ekrandan chiqib ketmasin.
        var left = Math.min(r.left, window.innerWidth - menu.offsetWidth - 8);
        menu.style.left = Math.max(8, left) + "px";
        menu.style.right = "auto";
      }
    }

    function open() {
      if (openInst && openInst !== inst) openInst.close();
      render();
      syncVal();
      // ko'rinmas holatda o'lchab, keyin joylaymiz
      menu.style.left = "-9999px";
      menu.style.top = "0px";
      menu.classList.add("open");
      root.classList.add("open");
      openInst = inst;
      position();
      setHl(select.selectedIndex);
      document.addEventListener("click", onDoc, true);
      document.addEventListener("keydown", onKey, true);
      window.addEventListener("resize", position, true);
      window.addEventListener("scroll", position, true);
    }
    function close() {
      menu.classList.remove("open");
      root.classList.remove("open");
      if (openInst === inst) openInst = null;
      document.removeEventListener("click", onDoc, true);
      document.removeEventListener("keydown", onKey, true);
      window.removeEventListener("resize", position, true);
      window.removeEventListener("scroll", position, true);
    }

    function onDoc(e) {
      if (!root.contains(e.target) && !menu.contains(e.target)) close();
    }
    function onKey(e) {
      if (e.key === "Escape") { close(); trigger.focus(); return; }
      if (e.key === "ArrowDown") { e.preventDefault(); setHl(Math.min((hl < 0 ? select.selectedIndex : hl) + 1, select.options.length - 1)); }
      else if (e.key === "ArrowUp") { e.preventDefault(); setHl(Math.max((hl < 0 ? select.selectedIndex : hl) - 1, 0)); }
      else if (e.key === "Enter") { e.preventDefault(); choose(hl); }
    }

    trigger.addEventListener("click", function (e) {
      e.stopPropagation();
      if (root.classList.contains("open")) close();
      else open();
    });

    // Tashqi mantiq option'larni keyin to'ldirsa (do'konlar ro'yxati) yoki
    // value'ni dasturiy o'zgartirsa — trigger'ni (va ochiq menyuni) yangilaymiz.
    new MutationObserver(function () {
      syncVal();
      if (root.classList.contains("open")) { render(); setHl(select.selectedIndex); position(); }
    }).observe(select, { childList: true, subtree: true, attributes: true, attributeFilter: ["value"] });
    select.addEventListener("change", function () { syncVal(); });

    var inst = { close: close, open: open };
    syncVal();
  }

  function refresh(scope) {
    injectCss();
    var nodes = (scope || document).querySelectorAll("select[data-fxsel]");
    Array.prototype.forEach.call(nodes, enhance);
  }

  window.fxsel = { refresh: refresh, enhance: enhance };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { refresh(); });
  } else {
    refresh();
  }
})();
