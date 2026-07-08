/* ============================================================
   FBS «Soft» — bo'lim tablari «Glass Slide».
   .fbs-section-tabs (Buyurtmalar / Ombor) ichiga sirpanuvchi navy
   gradient plita (.seg-thumb) qo'shadi va aktiv tab ostiga joylaydi.
   Tablar haqiqiy <a> link'lar — bosilganda sahifa almashadi, lekin
   plitani darrov sirpatib «jonli» his beramiz. Markup'ga tegmaydi:
   thumb DOM'ga shu yerda qo'shiladi.
   ============================================================ */
(function () {
  "use strict";

  function place(nav) {
    var a = nav.querySelector("a.active");
    var th = nav.querySelector(".seg-thumb");
    if (!a || !th) return;
    th.style.left = a.offsetLeft + "px";
    th.style.width = a.offsetWidth + "px";
    th.style.opacity = "1";
  }

  function init() {
    var navs = Array.prototype.slice.call(document.querySelectorAll(".fbs-section-tabs"));
    if (!navs.length) return;

    navs.forEach(function (nav) {
      if (!nav.querySelector(".seg-thumb")) {
        var th = document.createElement("span");
        th.className = "seg-thumb";
        nav.insertBefore(th, nav.firstChild);
      }
      place(nav);
      // Bosilganda plitani darrov sirpatamiz (sahifa yuklanguncha feedback).
      nav.querySelectorAll("a").forEach(function (a) {
        a.addEventListener("click", function () {
          nav.querySelectorAll("a").forEach(function (x) { x.classList.remove("active"); });
          a.classList.add("active");
          place(nav);
        });
      });
    });

    window.addEventListener("resize", function () { navs.forEach(place); });
    window.addEventListener("load", function () { navs.forEach(place); });
    // Shrift kech yuklansa eni o'zgaradi — qayta joylash.
    if (document.fonts && document.fonts.ready) {
      document.fonts.ready.then(function () { navs.forEach(place); });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
