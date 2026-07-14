/* Reusable "Add product to a sale" modal.
 *
 * Launch for ONE product: shows which joinable Uzum sales it's eligible for
 * (and which it's already in). When a sale is selected, it fetches Uzum's
 * per-SKU price limits for THAT sale (currentSellPrice + recommendedPrice = the
 * «Не больше X» fair-discount ceiling) and renders an editable per-SKU price
 * table that caps each price at its limit, so the add never gets rejected.
 * Each SKU also shows its warehouse-storage expense.
 *
 * Usage:
 *   UzumSalePicker.open({ shopId:"5983", productId:637136,
 *                         productName:"…", highlightSku:"LUXUZ-…" });
 *
 * Endpoints:
 *   GET  /api/product/<productId>/eligible-sales
 *   GET  /api/sales/<saleId>/product/<productId>/sku-limits
 *   POST /api/sales/<saleId>/add   (products:[{product_id, skus:[{sku_id,new_price}]}])
 */
(function () {
  if (window.UzumSalePicker) return;

  var LANG = (document.body && document.body.dataset.lang) || "ru";
  var UZ = LANG === "uz";
  var cur = UZ ? "so'm" : "сум";
  var T = {
    title:      UZ ? "Aksiyaga qo'shish" : "Добавить в акцию",
    inSales:    UZ ? "Mahsulot allaqachon aksiyalarda:" : "Товар уже участвует в акциях:",
    chooseSale: UZ ? "Aksiyani tanlang:" : "Выберите акцию:",
    discAll:    UZ ? "Hammaga chegirma" : "Скидка на всё",
    apply:      UZ ? "Qo'llash" : "Применить",
    orPerSku:   UZ ? "yoki har bir SKU narxini quyida o'zgartiring" : "или измените цену каждого SKU вручную ниже",
    pickSale:   UZ ? "Narxlarni ko'rish uchun aksiyani tanlang" : "Выберите акцию, чтобы увидеть цены",
    storage:    UZ ? "Saqlash" : "Хранение",
    cost:       UZ ? "Tannarx" : "Себестоимость",
    noMore:     UZ ? "Ko'pi bilan" : "Не больше",
    add:        UZ ? "Aksiyaga qo'shish" : "Добавить в акцию",
    update:     UZ ? "Narxni yangilash" : "Обновить цену",
    cancel:     UZ ? "Bekor qilish" : "Отмена",
    loading:    UZ ? "Yuklanmoqda…" : "Загрузка…",
    none:       UZ ? "Bu mahsulotni hozir hech qaysi aksiyaga qo'shib bo'lmaydi." : "Этот товар сейчас нельзя добавить ни в одну акцию.",
    noLimits:   UZ ? "Bu mahsulot uchun narx limitlari topilmadi." : "Лимиты цен для этого товара не найдены.",
    inSale:     UZ ? "Aksiyada" : "В акции",
    active:     UZ ? "Faol" : "Идёт",
    created:    UZ ? "Tez orada" : "Скоро",
    minDisc:    UZ ? "min." : "мин.",
    totalNow:   UZ ? "Jami hozir" : "Итого сейчас",
    totalNew:   UZ ? "Jami yangi" : "Итого новая",
    over:       UZ ? "limitdan yuqori" : "выше лимита",
    payout:     UZ ? "Olishga" : "К выводу",
    // Jadval ustunlari — «Акции» sahifasidagi nomlar bilan bir xil.
    colCur:     UZ ? "Joriy narx" : "Текущая цена",
    colNew:     UZ ? "Yangi narx" : "Новая цена",
    colDisc:    UZ ? "Chegirma" : "Скидка",
  };

  function esc(s){ var d=document.createElement("div"); d.textContent = (s==null?"":String(s)); return d.innerHTML; }
  function fmt(n){ if(n==null||n==="") return "—"; return Number(n).toLocaleString("ru-RU").replace(/,/g," "); }
  function floor10(x){ return Math.max(0, Math.floor((Number(x)||0)/10)*10); }
  function pct(c, n){ c=Number(c)||0; n=Number(n)||0; if(c<=0) return 0; return Math.round((c-n)/c*100); }

  var css = ""
    + ".sp-back{position:fixed;inset:0;background:rgba(10,8,20,.55);display:none;align-items:flex-start;justify-content:center;z-index:2000;padding:max(3vh,18px) 20px 18px;overflow-y:auto;}"
    + ".sp-back.open{display:flex;}"
    + ".sp-modal{background:var(--sp-surface,#fff);color:var(--sp-text,#1A1A22);border:1px solid var(--sp-border,#ECECF0);border-radius:18px;box-shadow:0 24px 60px rgba(20,16,40,.28);width:min(1120px,100%);max-height:94vh;display:flex;flex-direction:column;overflow:hidden;font-family:'Inter',ui-sans-serif,system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;}"
    + ".sp-head{padding:20px 26px;border-bottom:1px solid var(--sp-border,#ECECF0);display:flex;align-items:flex-start;gap:12px;}"
    + ".sp-head h3{margin:0;font-size:18px;font-weight:700;}"
    + ".sp-head .sp-sub{font-size:13px;color:var(--sp-muted,#8E8B97);margin-top:4px;}"
    + ".sp-x{margin-left:auto;background:none;border:0;color:var(--sp-muted,#8E8B97);cursor:pointer;font-size:22px;line-height:1;padding:0 4px;}"
    /* padding-top:0 — ATAYLAB. Bu scroll konteyner; agar tepasida padding
       bo'lsa, sticky sarlavha o'sha padding chetiga yopishadi va uning
       USTIDAGI 16px yo'lakda qatorlar ko'rinib o'tadi. Tepa bo'shliq o'rniga
       birinchi bolaga margin beriladi — u qatorlar bilan birga suriladi. */
    + ".sp-body{padding:0 26px 22px;overflow-y:auto;}"
    + ".sp-body>:first-child{margin-top:16px;}"
    + ".sp-sectlabel{font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--sp-muted,#8E8B97);margin:16px 0 8px;}"
    + ".sp-inbanner{display:flex;flex-wrap:wrap;gap:8px;align-items:center;background:var(--sp-hl,#FFF7E6);color:var(--sp-warnfg,#B7791F);border-radius:11px;padding:10px 13px;font-size:13px;font-weight:600;}"
    + ".sp-inbanner .sp-chip{background:rgba(0,0,0,.06);border-radius:999px;padding:2px 9px;font-weight:700;}"
    + ".sp-sales{display:flex;flex-direction:column;gap:8px;}"
    + ".sp-sale{display:flex;align-items:center;gap:14px;border:1px solid var(--sp-border,#ECECF0);border-radius:14px;padding:12px 14px;cursor:pointer;transition:border-color .12s,background .12s;}"
    + ".sp-sale:hover{background:var(--sp-hover,#F8F8FA);}"
    + ".sp-sale.sel{border-color:var(--sp-accent,#4169E1);box-shadow:0 0 0 3px rgba(65,105,225,.12);}"
    + ".sp-sale-radio{width:18px;height:18px;flex-shrink:0;accent-color:var(--sp-accent,#4169E1);}"
    + ".sp-sale-banner{width:104px;height:58px;border-radius:10px;overflow:hidden;background:var(--sp-active,#EFEEF2);flex-shrink:0;}.sp-sale-banner img{width:100%;height:100%;object-fit:cover;}"
    + ".sp-sale-main{min-width:0;flex:1 1 auto;}.sp-sale-title{font-size:15px;font-weight:600;line-height:1.3;}.sp-sale-meta{font-size:12.5px;color:var(--sp-muted,#8E8B97);margin-top:3px;}"
    + ".sp-badge{font-size:10px;font-weight:700;letter-spacing:.03em;padding:3px 8px;border-radius:999px;text-transform:uppercase;margin-left:auto;flex-shrink:0;}"
    + ".sp-badge.active{background:#ECFDF3;color:#0F9A6A;}.sp-badge.created{background:#EAF0FF;color:#4169E1;}.sp-badge.in{background:#FFF7E6;color:#B7791F;}"
    + ".sp-disc-global{display:flex;align-items:center;gap:10px;flex-wrap:wrap;background:var(--sp-active,#EFEEF2);border-radius:12px;padding:10px 13px;}"
    + ".sp-disc-global label{font-size:13px;font-weight:600;display:flex;align-items:center;gap:6px;}"
    + ".sp-disc-global input{width:74px;height:36px;border:1px solid var(--sp-border,#ECECF0);border-radius:9px;background:var(--sp-surface,#fff);color:var(--sp-text,#1A1A22);text-align:center;font-size:14px;font-weight:700;}"
    + ".sp-disc-apply{height:36px;padding:0 14px;border:0;border-radius:9px;background:var(--sp-accent,#4169E1);color:#fff;font-size:13px;font-weight:600;cursor:pointer;}"
    + ".sp-hint{font-size:12px;color:var(--sp-muted,#8E8B97);}"
    /* SKU JADVALI (Ulug'bek 2026-07-14). Ustunlar tartibi:
         SKU · Хранение · [bo'sh] · Текущая цена · Новая цена · Скидка · К выводу · Себестоимость
       SAQLASH ustuni SKU'ga yaqin turadi — jadval o'sha xarajat bo'yicha
       saralanadi, shuning uchun ikkalasi yonma-yon o'qiladi. Ular bilan narx
       bloki orasida CHO'ZILUVCHAN bo'sh yo'lak (3-track) bor: ortiqcha joy
       SKU ustuniga emas, o'sha yo'lakka ketadi — shunda «Хранение» SKU'ni
       quvib yuboradi, narxlar esa aniq ajralib turadi. Bo'sh yo'lak uchun
       alohida element YO'Q: 3-bola 4-track'ga majburan qo'yiladi.
       «Не больше X» — narx maydoni OSTIDA qoladi. */
    + ".sp-vtable{display:flex;flex-direction:column;border:1px solid var(--sp-border,#ECECF0);border-radius:14px;}"
    /* SKU ustuni max-content: ortiqcha joyni O'ZIGA olmaydi, 3-track'ka beradi —
       shundagina «Хранение» SKU'ning yoniga keladi (aks holda 145px uzoqda qolardi). */
    + ".sp-thead,.sp-vrow{display:grid;grid-template-columns:minmax(150px,max-content) 80px minmax(16px,1fr) 100px 134px 74px 92px 104px;gap:12px;align-items:center;}"
    + ".sp-thead>:nth-child(3),.sp-vrow>:nth-child(3){grid-column:4;}"   /* 3-track'ni bo'sh qoldiradi */
    /* Sarlavha qatori scroll'да yopishib turadi. Ikki shart: (1) .sp-vtable'да
       overflow YO'Q; (2) .sp-body'да padding-top YO'Q — aks holda sticky o'sha
       padding chetiga yopishadi va tepasida qatorlar ko'rinib o'tadi (aynan shu
       nuqson tuzatildi). Burchaklar qo'lда yumaloqlanadi. */
    + ".sp-thead{position:sticky;top:0;z-index:3;padding:11px 16px;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:var(--sp-muted,#8E8B97);background:var(--sp-thbg,#F2F2F6);border-bottom:1px solid var(--sp-border,#ECECF0);border-radius:13px 13px 0 0;}"
    + ".sp-thead .sp-r{text-align:right;}"
    + ".sp-vrow{padding:12px 16px;border-bottom:1px solid var(--sp-border,#ECECF0);transition:background .12s;}"
    + ".sp-vrow:last-child{border-bottom:0;border-radius:0 0 13px 13px;}"
    + ".sp-vrow:hover{background:var(--sp-hover,#F8F8FA);}"
    + ".sp-vrow.hl{background:var(--sp-hlsoft,#F5F8FF);box-shadow:inset 3px 0 0 var(--sp-accent,#4169E1);}"
    + ".sp-vprod{display:flex;align-items:center;gap:10px;min-width:0;}"
    + ".sp-vthumb{width:44px;height:44px;border-radius:9px;overflow:hidden;background:var(--sp-active,#EFEEF2);border:1px solid var(--sp-border,#ECECF0);flex-shrink:0;}"
    + ".sp-vthumb img{width:100%;height:100%;object-fit:cover;}"
    + ".sp-vskuwrap{min-width:0;}"
    + ".sp-vsku{font-family:'JetBrains Mono','SF Mono',Menlo,monospace;font-size:12px;font-weight:700;color:var(--sp-text,#1A1A22);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}"
    + ".sp-vchar{font-size:11px;color:var(--sp-muted,#8E8B97);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}"
    + ".sp-num{text-align:right;font-variant-numeric:tabular-nums;font-weight:600;font-size:13px;}"
    + ".sp-vcur{color:var(--sp-text,#1A1A22);}"
    + ".sp-vcost{color:var(--sp-text2,#5A5762);}"
    + ".sp-vexp{color:var(--sp-expfg,#E84747);}"          /* saqlash xarajati — qizil */
    + ".sp-vexp.zero{color:var(--sp-muted,#8E8B97);font-weight:500;}"
    + ".sp-vnew{text-align:right;}"
    + ".sp-price-input{width:118px;height:38px;border:1px solid var(--sp-border,#ECECF0);border-radius:9px;background:var(--sp-surface,#fff);color:var(--sp-text,#1A1A22);text-align:right;font-size:14px;font-weight:700;font-variant-numeric:tabular-nums;}"
    + ".sp-price-input:focus{outline:0;border-color:var(--sp-accent,#4169E1);box-shadow:0 0 0 3px rgba(65,105,225,.12);}"
    + ".sp-limit{display:block;font-size:11px;color:var(--sp-muted,#8E8B97);margin-top:4px;}"
    + ".sp-vdisc{text-align:right;}"
    + ".sp-vdisc b{font-size:13px;font-weight:700;color:var(--sp-text,#1A1A22);font-variant-numeric:tabular-nums;}"
    + ".sp-vdisc small{display:block;margin-top:2px;font-size:11px;font-weight:700;color:#0F9A6A;}"
    + ".sp-vpayout{text-align:right;font-variant-numeric:tabular-nums;font-weight:600;font-size:13px;color:var(--sp-text,#1A1A22);}"
    + ".sp-vrow.warn .sp-price-input{border-color:#F97066;}"
    + ".sp-vrow.warn .sp-limit{color:#B42318;font-weight:700;}"
    + ".sp-vrow.warn .sp-vdisc small{color:#B42318;}"
    + ".sp-foot{padding:14px 22px;border-top:1px solid var(--sp-border,#ECECF0);display:flex;align-items:center;gap:14px;flex-wrap:wrap;}"
    + ".sp-foot-sum{font-size:13px;color:var(--sp-text2,#5A5762);}.sp-foot-sum b{color:var(--sp-text,#1A1A22);font-variant-numeric:tabular-nums;}"
    + ".sp-btn{height:40px;padding:0 18px;border-radius:11px;border:0;font-size:14px;font-weight:600;cursor:pointer;}"
    + ".sp-btn-primary{background:var(--sp-accent,#4169E1);color:#fff;margin-left:auto;}.sp-btn-primary:disabled{opacity:.4;cursor:not-allowed;}"
    + ".sp-btn-ghost{background:var(--sp-surface,#fff);border:1px solid var(--sp-border,#ECECF0);color:var(--sp-text,#1A1A22);}"
    + ".sp-msg{padding:24px 8px;text-align:center;color:var(--sp-muted,#8E8B97);font-size:14px;}"
    + ".sp-err{padding:12px 14px;border-radius:10px;background:#FEF3F2;color:#B42318;font-size:14px;margin-bottom:10px;}"
    + ".sp-toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(20px);background:#1A1A22;color:#fff;padding:10px 18px;border-radius:12px;font-size:13px;font-weight:500;box-shadow:0 12px 36px rgba(0,0,0,.3);opacity:0;pointer-events:none;transition:opacity .2s,transform .2s;z-index:2100;max-width:90vw;text-align:center;}"
    + ".sp-toast.show{opacity:1;transform:translateX(-50%) translateY(0);}"
    /* 7 ustun ~940px'dan tor oynaga SIG'MAYDI (o'lchandi: 768px'да qator 46px
       toshib ketadi, 900px'да SKU ustuni 16px'gacha siqiladi). Shuning uchun
       shu chegaradan pastда jadval 2 ustunli kartaga yig'iladi — Акции
       sahifasidagi kabi, ammo har bir katak ustida ustun NOMI bilan, aks holda
       yalang'och raqamlar nimani anglatishi bilinmaydi. */
    + "@media (max-width:980px){"
      + ".sp-thead{display:none;}"
      /* align-items:start — «Новая цена» kataki (input + «Не больше») baland,
         markazga tekislansa yonidagi kataklar atrofida bo'sh joy qoladi. */
      + ".sp-vrow{grid-template-columns:1fr 1fr;gap:10px 12px;align-items:start;}"
      + ".sp-vrow>:nth-child(3){grid-column:auto;}"   /* keng ekrandagi bo'sh yo'lak bekor qilinadi */
      + ".sp-vprod{grid-column:1 / -1;}"
      + ".sp-num,.sp-vnew,.sp-vdisc,.sp-vpayout{text-align:left;}"
      + ".sp-vrow [data-l]::before{content:attr(data-l);display:block;margin-bottom:3px;font-size:10px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;color:var(--sp-muted,#8E8B97);}"
      + ".sp-price-input{width:100%;}"
    + "}"
    + "[data-bs-theme='dark'] .sp-modal{--sp-surface:#18181F;--sp-thbg:#20202A;--sp-text:#F0EDF5;--sp-text2:#B6B4C2;--sp-border:#25252F;--sp-muted:#8A8896;--sp-active:#22222D;--sp-hover:#1C1C26;--sp-accent:#6E8FE8;--sp-hl:rgba(251,191,36,.12);--sp-warnfg:#FBBF24;--sp-hlsoft:rgba(110,143,232,.12);--sp-expbg:rgba(252,165,165,.16);--sp-expfg:#FCA5A5;}"
    + "[data-bs-theme='dark'] .sp-toast{background:#F0EDF5;color:#0E0E14;}";
  var st = document.createElement("style"); st.textContent = css; document.head.appendChild(st);

  var back = document.createElement("div"); back.className = "sp-back";
  back.innerHTML =
    "<div class='sp-modal' role='dialog' aria-modal='true'>"
    + "<div class='sp-head'><div><h3 id='spTitle'></h3><div class='sp-sub' id='spSub'></div></div><button class='sp-x' id='spX'>&times;</button></div>"
    + "<div class='sp-body' id='spBody'></div>"
    + "<div class='sp-foot' id='spFoot' style='display:none'>"
    + "<div class='sp-foot-sum' id='spSum'></div>"
    + "<button class='sp-btn sp-btn-ghost' id='spCancel'></button>"
    + "<button class='sp-btn sp-btn-primary' id='spAdd'></button>"
    + "</div></div>";
  document.body.appendChild(back);
  var toast = document.createElement("div"); toast.className = "sp-toast"; document.body.appendChild(toast);
  var toastTimer;
  function showToast(m){ toast.textContent = m; toast.classList.add("show"); clearTimeout(toastTimer); toastTimer=setTimeout(function(){toast.classList.remove("show");},4500); }

  var $ = function(id){ return document.getElementById(id); };
  var state = { shopId:null, productId:null, data:null, selected:null, storageBySku:{}, limitsBySale:{} };

  // Cross-open cache so prefetch (on hover/page-load) and the actual modal open
  // share results — the first click then pops instantly. In-flight promises are
  // de-duped so a hover + click don't fire the same request twice.
  var CACHE = { eligible:{}, limits:{}, inflight:{} };
  function ekey(shop, pid){ return shop + ":" + pid; }
  function lkey(shop, sale, pid){ return shop + ":" + sale + ":" + pid; }

  function fetchEligible(shop, pid){
    var k = ekey(shop, pid);
    if (CACHE.eligible[k]) return Promise.resolve(CACHE.eligible[k]);
    if (CACHE.inflight[k]) return CACHE.inflight[k];
    var p = fetch("/api/product/" + pid + "/eligible-sales?shop_id=" + encodeURIComponent(shop))
      .then(function(r){ return r.json().then(function(j){ if(!r.ok) throw new Error(j.error||"Error"); return j; }); })
      .then(function(j){ CACHE.eligible[k] = j; delete CACHE.inflight[k]; return j; })
      .catch(function(e){ delete CACHE.inflight[k]; throw e; });
    CACHE.inflight[k] = p; return p;
  }

  function fetchLimits(shop, sale, pid){
    var k = lkey(shop, sale, pid);
    if (CACHE.limits[k]) return Promise.resolve(CACHE.limits[k]);
    if (CACHE.inflight[k]) return CACHE.inflight[k];
    var p = fetch("/api/sales/" + sale + "/product/" + pid + "/sku-limits?shop_id=" + encodeURIComponent(shop))
      .then(function(r){ return r.json().then(function(j){ if(!r.ok) throw new Error(j.error||"Error"); return j.skus||[]; }); })
      .then(function(skus){ CACHE.limits[k] = skus; delete CACHE.inflight[k]; return skus; })
      .catch(function(e){ delete CACHE.inflight[k]; throw e; });
    CACHE.inflight[k] = p; return p;
  }

  function dropProductCache(shop, pid){
    delete CACHE.eligible[ekey(shop, pid)];
    var suf = ":" + pid;
    Object.keys(CACHE.limits).forEach(function(k){
      if (k.indexOf(shop + ":") === 0 && k.lastIndexOf(suf) === k.length - suf.length) delete CACHE.limits[k];
    });
  }

  // Warm a product's modal data in the background (eligible sales + the
  // default sale's SKU limits) so a subsequent open is instant.
  function prefetch(opts){
    opts = opts || {};
    var shop = String(opts.shopId || ""), pid = opts.productId;
    if (!shop || !pid) return;
    fetchEligible(shop, pid).then(function(j){
      var sales = (j && j.eligible_sales) || [];
      if (!sales.length) return;
      var inIdx = sales.findIndex(function(s){ return s.already_in; });
      var s = sales[inIdx >= 0 ? inIdx : 0];
      if (s) fetchLimits(shop, s.id, pid).catch(function(){});
    }).catch(function(){});
  }

  function close(){ back.classList.remove("open"); }
  $("spX").addEventListener("click", close);
  $("spCancel").addEventListener("click", close);
  back.addEventListener("click", function(e){ if(e.target===back) close(); });
  document.addEventListener("keydown", function(e){ if(e.key==="Escape") close(); });

  function saleTitle(s){ return (UZ ? (s.title.uz||s.title.ru) : (s.title.ru||s.title.uz)) || ""; }
  function currentSale(){ return state.selected==null ? null : (state.data.eligible_sales||[])[state.selected]; }

  function render(){
    var body = $("spBody"); body.innerHTML = "";
    var data = state.data || {};
    var p = data.product || {};
    // storage lookup by skuId (from our DB)
    state.storageBySku = {};
    (p.variants||[]).forEach(function(v){ if(v.sku_id!=null) state.storageBySku[String(v.sku_id)] = v; });
    var sales = data.eligible_sales || [];

    var inSales = sales.filter(function(s){ return s.already_in; });
    if (inSales.length) {
      var b = document.createElement("div"); b.className="sp-inbanner";
      b.innerHTML = "<span>"+T.inSales+"</span>" + inSales.map(function(s){ return "<span class='sp-chip'>"+esc(saleTitle(s))+"</span>"; }).join("");
      body.appendChild(b);
    }

    var lab = document.createElement("div"); lab.className="sp-sectlabel"; lab.textContent=T.chooseSale; body.appendChild(lab);
    if (!sales.length) {
      var m=document.createElement("div"); m.className="sp-msg"; m.textContent=T.none; body.appendChild(m);
      $("spFoot").style.display="none"; return;
    }
    var sbox=document.createElement("div"); sbox.className="sp-sales";
    sales.forEach(function(s,i){
      var st2=(s.status||"").toLowerCase();
      var stLabel = s.already_in ? T.inSale : (s.status==="ACTIVE"?T.active:T.created);
      var badgeCls = s.already_in ? "in" : st2;
      var banner=(s.image_url && (UZ ? (s.image_url.uz||s.image_url.ru) : (s.image_url.ru||s.image_url.uz))) || "";
      var row=document.createElement("div"); row.className="sp-sale"; row.dataset.idx=i;
      row.innerHTML="<input type='radio' name='spSale' class='sp-sale-radio'>"
        +(banner?"<div class='sp-sale-banner'><img src='"+esc(banner)+"' alt='' loading='lazy'></div>":"")
        +"<div class='sp-sale-main'><div class='sp-sale-title'>"+esc(saleTitle(s))+"</div>"
        +"<div class='sp-sale-meta'>"+esc(s.start_date||"")+" → "+esc(s.finish_date||"")+" · "+T.minDisc+" "+s.min_discount+"%</div></div>"
        +"<span class='sp-badge "+badgeCls+"'>"+esc(stLabel)+"</span>";
      row.addEventListener("click", function(){ selectSale(i); });
      sbox.appendChild(row);
    });
    body.appendChild(sbox);

    var dc=document.createElement("div"); dc.className="sp-disc-global";
    dc.innerHTML="<label>"+T.discAll+": <input type='number' id='spDiscAll' min='0' max='99' value='0'> %</label>"
      +"<button type='button' class='sp-disc-apply' id='spApplyDisc'>"+T.apply+"</button>"
      +"<span class='sp-hint'>"+T.orPerSku+"</span>";
    var lab2=document.createElement("div"); lab2.className="sp-sectlabel"; lab2.textContent="SKU"; body.appendChild(lab2); body.appendChild(dc);

    var vt=document.createElement("div"); vt.className="sp-vtable"; vt.id="spVtable";
    vt.innerHTML="<div class='sp-msg'>"+T.pickSale+"</div>";
    body.appendChild(vt);

    $("spApplyDisc").addEventListener("click", applyDiscountAll);
    $("spDiscAll").addEventListener("input", applyDiscountAll);
    $("spFoot").style.display="";

    var inIdx = sales.findIndex(function(s){ return s.already_in; });
    selectSale(inIdx>=0?inIdx:0);
  }

  function selectSale(i){
    state.selected = i;
    var sales = state.data.eligible_sales || [];
    var s = sales[i]; if(!s) return;
    Array.prototype.forEach.call(document.querySelectorAll(".sp-sale"), function(elm){
      var on=Number(elm.dataset.idx)===i; elm.classList.toggle("sel",on);
      var r=elm.querySelector(".sp-sale-radio"); if(r) r.checked=on;
    });
    $("spAdd").textContent = s.already_in ? T.update : T.add;
    $("spAdd").disabled = true;
    var da=$("spDiscAll"); if(da) da.value = s.min_discount||0;
    loadLimits(s.id);
  }

  function loadLimits(saleId){
    var vt=$("spVtable"); if(!vt) return;
    if (state.limitsBySale[saleId]) { renderTable(state.limitsBySale[saleId]); return; }
    var lk = lkey(state.shopId, saleId, state.productId);
    if (CACHE.limits[lk]) { state.limitsBySale[saleId] = CACHE.limits[lk]; renderTable(CACHE.limits[lk]); return; }
    vt.innerHTML="<div class='sp-msg'>"+T.loading+"</div>";
    fetchLimits(state.shopId, saleId, state.productId)
      .then(function(skus){
        state.limitsBySale[saleId] = skus;
        renderTable(skus);
      })
      .catch(function(err){ vt.innerHTML="<div class='sp-err'>"+esc(String(err.message||err))+"</div>"; });
  }

  // Saqlash xarajati bo'yicha kamayish tartibi — sahifaning o'zi ham shunday
  // saralangan: eng ko'p pul yeyayotgan SKU tepada turadi, chunki chegirma eng
  // avvalo o'shalarga kerak. Sort BARQAROR: xarajati teng SKU'lar Uzum bergan
  // tartibda qoladi.
  function byStorageDesc(skus){
    return skus.slice().sort(function(a,b){
      var sa=Number((state.storageBySku[String(a.sku_id)]||{}).storage)||0;
      var sb=Number((state.storageBySku[String(b.sku_id)]||{}).storage)||0;
      return sb-sa;
    });
  }

  function renderTable(skus){
    var vt=$("spVtable"); vt.innerHTML="";
    if(!skus.length){ vt.innerHTML="<div class='sp-msg'>"+T.noLimits+"</div>"; $("spAdd").disabled=true; return; }
    var head=document.createElement("div"); head.className="sp-thead";
    head.innerHTML="<span>SKU</span><span class='sp-r'>"+T.storage+"</span>"
      +"<span class='sp-r'>"+T.colCur+"</span><span class='sp-r'>"+T.colNew+"</span>"
      +"<span class='sp-r'>"+T.colDisc+"</span><span class='sp-r'>"+T.payout+"</span>"
      +"<span class='sp-r'>"+T.cost+"</span>";
    vt.appendChild(head);
    byStorageDesc(skus).forEach(function(sk){
      var sid=sk.sku_id;
      var ours = state.storageBySku[String(sid)] || {};
      var cur0 = sk.current_price || 0;
      var maxp = sk.max_price || 0;
      var def = sk.already_in && sk.sale_price ? sk.sale_price : maxp; // default to existing sale price or the max
      var skuName = (ours.sku || sk.sku_title || ("SKU "+sid));
      var color = ours.color || sk.characteristics || "";
      var stor = Number(ours.storage)||0;
      var hl = state.highlightSku && skuName && skuName.toLowerCase()===state.highlightSku.toLowerCase();
      var row=document.createElement("div"); row.className="sp-vrow"+(hl?" hl":""); row.dataset.sku=sid;
      row.innerHTML =
        "<div class='sp-vprod'><div class='sp-vthumb'>"+(ours.image_url?"<img src='"+esc(ours.image_url)+"'>":"")+"</div>"
          +"<div class='sp-vskuwrap'><div class='sp-vsku'>"+esc(skuName)+"</div>"
          +(color?"<div class='sp-vchar'>"+esc(color)+"</div>":"")+"</div></div>"
        +"<div class='sp-num sp-vexp"+(stor>0?"":" zero")+"' data-l='"+T.storage+"'>"+(stor>0?fmt(stor):"—")+"</div>"
        +"<div class='sp-num sp-vcur' data-l='"+T.colCur+"'>"+fmt(cur0)+"</div>"
        +"<div class='sp-vnew' data-l='"+T.colNew+"'>"
          +"<input type='number' class='sp-price-input' data-sku='"+sid+"' data-cur='"+cur0+"' data-max='"+maxp+"' data-comm='"+(sk.commission_rate!=null?sk.commission_rate:"")+"' data-logi='"+(sk.logistics_per_unit!=null?sk.logistics_per_unit:"")+"' value='"+def+"' min='0' step='10'>"
          +"<span class='sp-limit'>"+T.noMore+" "+fmt(maxp)+" "+cur+"</span></div>"
        +"<div class='sp-vdisc' data-l='"+T.colDisc+"'><b data-amt='"+sid+"'>0</b><small data-pct='"+sid+"'>0%</small></div>"
        +"<div class='sp-vpayout' data-payout='"+sid+"' data-l='"+T.payout+"'>—</div>"
        +"<div class='sp-num sp-vcost' data-l='"+T.cost+"'>"+fmt((ours.cost_price||0)>0?ours.cost_price:null)+"</div>";
      vt.appendChild(row);
    });
    vt.querySelectorAll(".sp-price-input").forEach(function(inp){
      inp.addEventListener("input", function(){ clampInput(inp); updateRowPct(inp.dataset.sku); updateSummary(); });
      inp.addEventListener("blur", function(){ clampInput(inp); updateRowPct(inp.dataset.sku); updateSummary(); });
    });
    refreshAll();
    $("spAdd").disabled = false;
  }

  function clampInput(inp){
    var max=Number(inp.dataset.max)||0; var v=Math.max(0, Math.round(Number(inp.value)||0));
    if (max>0 && v>max) v=max;     // never allow above Uzum's «Не больше» ceiling
    inp.value=v;
  }

  function applyDiscountAll(){
    var d=Number(($("spDiscAll")||{}).value)||0;
    document.querySelectorAll(".sp-price-input").forEach(function(inp){
      var c=Number(inp.dataset.cur)||0, max=Number(inp.dataset.max)||0;
      var np=floor10(c*(1-d/100));
      if(max>0 && np>max) np=max;
      inp.value=np; updateRowPct(inp.dataset.sku);
    });
    updateSummary();
  }

  function updateRowPct(sid){
    var inp=document.querySelector(".sp-price-input[data-sku='"+sid+"']");
    var lbl=document.querySelector("[data-pct='"+sid+"']");
    if(!inp||!lbl) return;
    var c=Number(inp.dataset.cur)||0, max=Number(inp.dataset.max)||0, np=Number(inp.value)||0;
    var d=pct(c,np); lbl.textContent=(d>0?"−":"")+d+"%";
    // «Скидка» ustuni: tepada — necha so'm arzonlashdi, ostida — foizi.
    var amt=document.querySelector("[data-amt='"+sid+"']");
    if(amt) amt.textContent=fmt(Math.max(0, c-np));
    var row=inp.closest(".sp-vrow"); if(row) row.classList.toggle("warn", max>0 && np>max);
    var pay=document.querySelector("[data-payout='"+sid+"']");
    if(pay){
      var comm=inp.dataset.comm, logi=inp.dataset.logi;
      if(comm!=="" && comm!=null && logi!=="" && logi!=null){
        var po=Math.max(0, Math.round(np*(1-parseFloat(comm)) - parseFloat(logi)));
        pay.textContent=fmt(po);
      } else { pay.textContent="—"; }
    }
  }
  function refreshAll(){ document.querySelectorAll(".sp-price-input").forEach(function(inp){ updateRowPct(inp.dataset.sku); }); updateSummary(); }

  function selectedSkus(){
    var out=[];
    document.querySelectorAll(".sp-price-input").forEach(function(inp){
      var sid=Number(inp.dataset.sku), np=Math.max(0,Math.round(Number(inp.value)||0));
      var max=Number(inp.dataset.max)||0; if(max>0 && np>max) np=max;
      if(sid&&np>0) out.push({sku_id:sid, new_price:np});
    });
    return out;
  }
  function updateSummary(){
    var tc=0,tn=0;
    document.querySelectorAll(".sp-price-input").forEach(function(inp){ tc+=Number(inp.dataset.cur)||0; tn+=Number(inp.value)||0; });
    var el=$("spSum"); if(el) el.innerHTML=T.totalNow+": <b>"+fmt(tc)+"</b> "+cur+" → "+T.totalNew+": <b>"+fmt(tn)+"</b> "+cur;
  }

  $("spAdd").addEventListener("click", function(){
    var s=currentSale(); if(!s) return;
    var skus=selectedSkus(); if(!skus.length){ showToast(UZ?"Narx kiriting":"Укажите цену"); return; }
    var payload={ shop_id:state.shopId, products:[{ product_id:state.productId, skus:skus }] };
    $("spAdd").disabled=true;
    fetch("/api/sales/"+s.id+"/add",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)})
      .then(function(r){ return r.json().then(function(j){ return {status:r.status,j:j}; }); })
      .then(function(res){
        $("spAdd").disabled=false;
        if(res.status>=200&&res.status<300){
          showToast(s.already_in?(UZ?"Narx yangilandi":"Цена обновлена"):(UZ?"Aksiyaga qo'shildi":"Добавлено в акцию"));
          state.limitsBySale={}; dropProductCache(state.shopId, state.productId);
          // Tell the host page (e.g. expenses table) this product is now enrolled,
          // so it can flip its button to "Участвует в акции" without a reload.
          try {
            document.dispatchEvent(new CustomEvent("uzum-sale-added", {
              detail: { shopId: state.shopId, productId: state.productId, saleTitle: saleTitle(s) }
            }));
          } catch(_){}
          load();
        }
        else { showToast(res.j.error||"Error"); }
      })
      .catch(function(err){ $("spAdd").disabled=false; showToast(String(err)); });
  });

  function load(){
    state.selected=null; state.limitsBySale={};
    // If a prefetch already warmed this product, render instantly.
    var k = ekey(state.shopId, state.productId);
    if (CACHE.eligible[k]) { state.data = CACHE.eligible[k]; render(); return; }
    $("spBody").innerHTML="<div class='sp-msg'>"+T.loading+"</div>"; $("spFoot").style.display="none";
    fetchEligible(state.shopId, state.productId)
      .then(function(j){ state.data=j; render(); })
      .catch(function(err){ $("spBody").innerHTML="<div class='sp-err'>"+esc(String(err.message||err))+"</div>"; });
  }

  function open(opts){
    opts=opts||{};
    state.shopId=String(opts.shopId||""); state.productId=opts.productId; state.highlightSku=opts.highlightSku||"";
    $("spTitle").textContent=T.title; $("spSub").textContent=opts.productName||"";
    $("spCancel").textContent=T.cancel; $("spAdd").textContent=T.add; $("spAdd").disabled=true;
    back.classList.add("open");
    if(!state.productId||!state.shopId){ $("spBody").innerHTML="<div class='sp-err'>"+(UZ?"Mahsulot Uzum bilan bog'lanmagan.":"Товар не связан с Uzum.")+"</div>"; $("spFoot").style.display="none"; return; }
    load();
  }

  window.UzumSalePicker = { open: open, prefetch: prefetch };
})();
