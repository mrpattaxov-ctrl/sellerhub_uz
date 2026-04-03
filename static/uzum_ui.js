(function () {
  // Inject CSS to fix image aspect ratio globally (3:4)
  const style = document.createElement("style");
  style.innerHTML = `
    img.card-img-top, .product-img, .card img {
      width: 100% !important;
      height: auto !important;
      aspect-ratio: 3/4 !important;
      object-fit: cover !important;
    }
    /* Small table image with 3:4 aspect ratio */
    .table-img {
      width: 48px !important;
      height: auto !important;
      aspect-ratio: 3/4 !important;
      object-fit: cover !important;
      cursor: zoom-in;
    }
    /* Popup zoom image style */
    #uzum-zoom-popup {
      position: fixed;
      z-index: 10000;
      width: 240px;
      height: auto;
      aspect-ratio: 3/4;
      object-fit: cover;
      border-radius: 8px;
      border: 1px solid #e0e0e0;
      box-shadow: 0 12px 24px rgba(0,0,0,0.25);
      pointer-events: none; /* Let mouse events pass through to original img */
      animation: fadeIn 0.1s ease-out;
      background: #fff;
    }
    @keyframes fadeIn { from { opacity: 0; transform: scale(0.8); } to { opacity: 1; transform: scale(1); } }

    /* Increase table cell spacing to separate columns (e.g. Stock vs Barcode) */
    .table th, .table td {
      padding-left: 12px !important;
      padding-right: 12px !important;
    }

    /* Custom Dropdown (Figma Style) */
    .uzum-dd-container {
      position: relative;
      display: inline-block;
      font-family: system-ui, -apple-system, sans-serif;
    }
    .uzum-dd-btn {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      background: #FFFFFF;
      border: 1px solid #D0D5DD;
      box-shadow: 0px 1px 2px rgba(16, 24, 40, 0.05);
      border-radius: 8px;
      padding: 8px 14px;
      font-size: 14px;
      line-height: 20px;
      font-weight: 500;
      color: #344054;
      cursor: pointer;
      transition: all 0.2s ease;
      min-width: 180px;
      user-select: none;
    }
    .uzum-dd-btn:hover {
      background: #F9FAFB;
      border-color: #B9C0D4;
    }
    .uzum-dd-btn:active, .uzum-dd-container.open .uzum-dd-btn {
      border-color: #84CAFF;
      box-shadow: 0px 0px 0px 4px #F2F4F7;
    }
    .uzum-dd-menu {
      position: absolute;
      top: calc(100% + 4px);
      left: 0;
      width: 100%;
      background: #FFFFFF;
      border: 1px solid #EAECF0;
      box-shadow: 0px 12px 16px -4px rgba(16, 24, 40, 0.08), 0px 4px 6px -2px rgba(16, 24, 40, 0.03);
      border-radius: 8px;
      padding: 4px;
      z-index: 1060;
      display: none;
      max-height: 300px;
      overflow-y: auto;
      animation: ddFadeIn 0.15s ease-out;
    }
    .uzum-dd-container.open .uzum-dd-menu {
      display: block;
    }
    .uzum-dd-item {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 10px 10px;
      font-size: 14px;
      color: #101828;
      border-radius: 6px;
      cursor: pointer;
      transition: background 0.15s;
    }
    .uzum-dd-item:hover {
      background: #F9FAFB;
    }
    .uzum-dd-item.selected {
      background: #F9F5FF;
      color: #6941C6;
    }
    .uzum-dd-item.selected::after {
      content: "✓";
      color: #7F56D9;
      font-weight: bold;
    }
    .uzum-dd-chevron {
      transition: transform 0.2s;
      color: #667085;
    }
    .uzum-dd-container.open .uzum-dd-chevron {
      transform: rotate(180deg);
    }
    @keyframes ddFadeIn { from { opacity: 0; transform: translateY(-5px); } to { opacity: 1; transform: translateY(0); } }
  `;
  document.head.appendChild(style);

  const $ = (id) => document.getElementById(id);

  // Global hover listener for table images to show popup
  document.addEventListener("mouseover", (e) => {
    if (e.target && e.target.classList.contains("table-img")) {
      const src = e.target.src;
      if (!src) return;

      // Remove existing if any
      const existing = document.getElementById("uzum-zoom-popup");
      if (existing) existing.remove();

      const popup = document.createElement("img");
      popup.id = "uzum-zoom-popup";
      popup.src = src;
      
      // Calculate position to center over the original image
      const rect = e.target.getBoundingClientRect();
      const w = 240;
      const h = 320;
      
      let top = rect.top + (rect.height / 2) - (h / 2);
      let left = rect.left + (rect.width / 2) - (w / 2);
      
      // Keep within viewport logic
      if (top < 10) top = 10;
      if (left < 10) left = 10;
      if (left + w > window.innerWidth - 10) left = window.innerWidth - w - 10;
      if (top + h > window.innerHeight - 10) top = window.innerHeight - h - 10;
      
      popup.style.top = top + "px";
      popup.style.left = left + "px";
      
      document.body.appendChild(popup);

      const cleanup = () => {
        if (popup.parentNode) popup.parentNode.removeChild(popup);
        e.target.removeEventListener("mouseout", cleanup);
      };
      e.target.addEventListener("mouseout", cleanup);
    }
  });

  function initUzumUI() {
    // 1. Create Sidebar
    const sidebar = document.createElement("div");
    sidebar.className = "uzum-sidebar";
    
    // Inject Layout CSS
    const layoutStyle = document.createElement("style");
    layoutStyle.innerHTML = `
      body { margin-left: 280px !important; background-color: #F9FAFB; transition: margin-left 0.3s ease; }
      body.sidebar-closed { margin-left: 60px !important; }

      .uzum-sidebar {
        position: fixed; top: 0; left: 0; height: 100vh; width: 280px;
        background: #FFFFFF; border-right: 1px solid #EAECF0;
        display: flex; flex-direction: column;
        z-index: 1050; overflow-y: auto; overflow-x: hidden;
        font-family: system-ui, -apple-system, sans-serif;
        transition: width 0.3s ease;
      }
      .uzum-sidebar.closed { width: 60px; }

      .uzum-sidebar-header {
        padding: 24px 20px;
        border-bottom: 1px solid #EAECF0;
        display: flex; align-items: center; justify-content: space-between;
        min-height: 73px;
      }
      .uzum-sidebar.closed .uzum-sidebar-header {
        padding: 24px 0; justify-content: center;
      }
      .uzum-sidebar.closed .uzum-brand span { display: none; }

      .uzum-sidebar-content {
        padding: 20px;
        display: flex; flex-direction: column; gap: 24px;
        flex: 1;
      }
      .uzum-sidebar.closed .uzum-sidebar-content { padding: 12px 0; gap: 16px; }

      .uzum-brand {
        font-size: 18px; font-weight: 600; color: #101828;
        display: flex; align-items: center; gap: 10px;
        white-space: nowrap;
      }
      .uzum-toggle-btn {
        cursor: pointer;
        background: none;
        border: none;
        padding: 4px;
        display: flex; align-items: center; justify-content: center;
        color: #98A2B3;
        transition: color 0.18s;
        flex-shrink: 0;
      }
      .uzum-toggle-btn:hover { color: #101828; }
      .uzum-sidebar.closed .uzum-toggle-btn { display: none; }

      .uzum-floating-toggle { display: none; }

      .uzum-nav-item {
        display: flex; align-items: center; gap: 12px;
        padding: 10px 12px; border-radius: 6px;
        color: #344054; text-decoration: none; font-weight: 500; font-size: 14px;
        transition: all 0.2s; white-space: nowrap; overflow: hidden;
      }
      .uzum-nav-item:hover { background: #F9FAFB; color: #101828; }
      .uzum-nav-item.active { background: #F2F4F7; color: #101828; }
      .uzum-nav-item .nav-icon { flex-shrink: 0; width: 20px; height: 20px; display: flex; align-items: center; justify-content: center; }
      .uzum-nav-item .nav-label { transition: opacity 0.2s; }
      .uzum-sidebar.closed .uzum-nav-item { justify-content: center; padding: 10px 0; border-radius: 0; }
      .uzum-sidebar.closed .uzum-nav-item .nav-label { opacity: 0; width: 0; overflow: hidden; }

      .uzum-sidebar.closed .uzum-section-label,
      .uzum-sidebar.closed .uzum-dd-container,
      .uzum-sidebar.closed .uzum-segment,
      .uzum-sidebar.closed .w-100 { display: none; }
      
      .uzum-section-label {
        font-size: 12px; font-weight: 600; color: #667085;
        text-transform: uppercase; letter-spacing: 0.04em;
        margin-bottom: 8px;
      }
      
      /* Inputs & Buttons in Sidebar */
      .uzum-sidebar input.form-control {
        font-size: 14px; padding: 10px 12px;
        border: 1px solid #D0D5DD; border-radius: 8px;
        box-shadow: 0px 1px 2px rgba(16, 24, 40, 0.05);
      }
      .uzum-sidebar .btn {
        width: 100%; justify-content: center; border-radius: 8px;
        font-weight: 500; padding: 10px;
      }
      
      /* Segmented Control for Status */
      .uzum-segment {
        background: #F2F4F7; padding: 4px; border-radius: 8px;
        display: flex; gap: 4px;
      }
      .uzum-segment-opt {
        flex: 1; text-align: center; padding: 6px;
        font-size: 13px; font-weight: 500; color: #667085;
        border-radius: 6px; cursor: pointer; text-decoration: none;
        transition: all 0.2s;
      }
      .uzum-segment-opt.active {
        background: #FFFFFF; color: #101828;
        box-shadow: 0px 1px 2px rgba(16, 24, 40, 0.06);
      }
      .uzum-segment-opt:hover:not(.active) {
        color: #344054;
      }
      
      .uzum-dd-container { width: 100%; display: block; }
      .uzum-dd-btn { width: 100%; justify-content: space-between; }
    `;
    document.head.appendChild(layoutStyle);

    // Header
    const header = document.createElement("div");
    header.className = "uzum-sidebar-header";
    header.innerHTML = `
      <div class="uzum-brand">
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
          <path d="M12 2L2 7L12 12L22 7L12 2Z" stroke="#667085" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
          <path d="M2 17L12 22L22 17" stroke="#667085" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
          <path d="M2 12L12 17L22 12" stroke="#667085" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
        <span>SellerHub</span>
      </div>
      <button class="uzum-toggle-btn" id="uzumSidebarToggle" title="Свернуть">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 3v18"/><path d="m16 15-3-3 3-3"/></svg>
      </button>
    `;
    sidebar.appendChild(header);

    const content = document.createElement("div");
    content.className = "uzum-sidebar-content";
    sidebar.appendChild(content);

    // Navigation
    const nav = document.createElement("nav");
    nav.style.display = "flex";
    nav.style.flexDirection = "column";
    nav.style.gap = "4px";

    const svgIcon = (paths) => `<svg class="nav-icon" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#667085" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${paths}</svg>`;
    const links = [
      { label: "Товары", href: "/groups", icon: svgIcon('<path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/>') },
      { label: "Юнит Экономика", href: "/economics", icon: svgIcon('<line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/>') },
      { label: "Калькулятор", href: "/calculator", icon: svgIcon('<rect x="4" y="2" width="16" height="20" rx="2"/><line x1="8" y1="6" x2="16" y2="6"/><line x1="8" y1="10" x2="10" y2="10"/><line x1="14" y1="10" x2="16" y2="10"/><line x1="8" y1="14" x2="10" y2="14"/><line x1="14" y1="14" x2="16" y2="14"/><line x1="8" y1="18" x2="16" y2="18"/>') },
      { label: "POS Терминал", href: "/pos", icon: svgIcon('<rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/>') },
      { label: "Создать Накладную", href: "/invoice/restock", icon: svgIcon('<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/>') },
      { label: "Импорт / Экспорт", href: "/warehouse/import", icon: svgIcon('<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="8" y1="15" x2="16" y2="15"/><polyline points="11 12 8 15 11 18"/><polyline points="13 12 16 15 13 18"/>') },
      { label: "Печать ценников", href: "/print/queue", icon: svgIcon('<polyline points="6 9 6 2 18 2 18 9"/><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/><rect x="6" y="14" width="12" height="8"/>') },
      { label: "Мои магазины", href: "/fetch", icon: svgIcon('<path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/>') },
    ];

    links.forEach(l => {
      const a = document.createElement("a");
      a.className = "uzum-nav-item";
      if (window.location.pathname === l.href || (l.href === "/groups" && window.location.pathname === "/")) {
        a.classList.add("active");
      }
      a.innerHTML = `${l.icon}<span class="nav-label">${l.label}</span>`;
      a.href = l.href;
      a.title = l.label;
      nav.appendChild(a);
    });

    // Logout Link
    const logoutLink = document.createElement("a");
    logoutLink.className = "uzum-nav-item mt-auto";
    logoutLink.style.color = "#F04438";
    logoutLink.innerHTML = `${svgIcon('<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/>').replace('#667085','#F04438')}<span class="nav-label">Выйти</span>`;
    logoutLink.href = "/logout";
    logoutLink.title = "Выйти";
    nav.appendChild(logoutLink);
    
    content.appendChild(nav);

    // Shop picker moved to groups page top bar

    // Search & Filters — kept in the top of the page, not moved to sidebar

    // Actions
    const actionIds = ["btnAdd", "btnRefresh", "btnSummary"];
    const foundActions = [];
    actionIds.forEach(id => {
      const el = $(id);
      if (el) foundActions.push(el);
    });

    if (foundActions.length > 0) {
      const sectionTitle = document.createElement("div");
      sectionTitle.className = "uzum-section-label";
      sectionTitle.textContent = "Действия";
      content.appendChild(sectionTitle);
      
      const actionsGroup = document.createElement("div");
      actionsGroup.className = "d-flex flex-column gap-2";
      content.appendChild(actionsGroup);

      foundActions.forEach(btn => {
        btn.classList.remove("btn-sm", "float-end", "ms-2");
        btn.classList.add("w-100", "mb-0");
        if (btn.id === "btnAdd") btn.classList.add("btn", "btn-success");
        else btn.classList.add("btn", "btn-light", "border");
        actionsGroup.appendChild(btn);
      });
    }

    // Hide legacy sync controls if present on this page
    ["syncShopId", "btnDoSync"].forEach(id => {
      const el = $(id);
      if (el) el.style.display = "none";
    });

    // 5. Inject Sidebar
    document.body.prepend(sidebar);

    // 6. Sidebar Toggle Logic
    const toggleSidebar = () => {
      const isClosed = document.body.classList.toggle("sidebar-closed");
      sidebar.classList.toggle("closed", isClosed);
      localStorage.setItem("uzum_sidebar_closed", isClosed);
    };

    // Click brand icon to toggle both ways
    header.querySelector(".uzum-brand svg").style.cursor = "pointer";
    header.querySelector(".uzum-brand svg").addEventListener("click", (e) => {
      e.preventDefault(); e.stopPropagation(); toggleSidebar();
    });
    header.querySelector("#uzumSidebarToggle").addEventListener("click", (e) => {
      e.preventDefault(); e.stopPropagation(); toggleSidebar();
    });
    if (localStorage.getItem("uzum_sidebar_closed") === "true") toggleSidebar();

    // 5. Hide inputs that are now hardcoded (AFTER moving Shop ID)
    ["syncWorkerBase", "syncSize"].forEach(id => {
      const el = $(id);
      if (el) {
        const p = el.closest(".mb-3") || el.parentElement;
        if (p) p.style.display = "none";
      }
    });

    // 6. Attach Sync Event Listener
    const btn = $("btnDoSync");
    if (btn) {
      btn.addEventListener("click", async () => {
        const urlParams = new URLSearchParams(window.location.search);
        const selectedShopId = urlParams.get("shop_id") || "";
        const size = 100;
        const sync_all = $("syncAll")?.checked || false;

        const out = $("syncResult");
        if (out) {
          out.style.display = "block";
          out.className = "alert alert-secondary mb-0 small";
          out.textContent = "Синхронизация… пожалуйста, подождите (это может занять время).";
        }

        try {
          let shopIds = [];
          if (selectedShopId) {
            shopIds = [selectedShopId];
          } else {
            const shopsRes = await fetch("/api/shops");
            const shopsData = await shopsRes.json();
            shopIds = (shopsData.shops || []).map(s => s.uzum_id);
          }

          let totalPages = 0, totalFetched = 0;
          for (const shop_id of shopIds) {
            const data = await postJson("/api/uzum/sync", { shop_id, size, sync_all, max_pages: 500 });
            totalPages += data.pages_synced || 0;
            totalFetched += data.fetched || 0;
          }

          if (out) {
            out.className = "alert alert-success mb-0 small";
            out.textContent = `Готово. Страниц: ${totalPages}, товаров: ${totalFetched}. Перезагрузка…`;
          }
          setTimeout(() => location.reload(), 800);
        } catch (e) {
          if (out) {
            out.className = "alert alert-danger mb-0 small";
            out.textContent = "Ошибка: " + e.message;
          }
        }
      });
    }

    // Inject images into group detail table (server-rendered)
    const match = window.location.pathname.match(/\/groups\/(\d+)$/);
    if (match) {
      const groupId = match[1];
      fetch(`/api/groups/${groupId}/variants?_=${Date.now()}`)
        .then(r => r.json())
        .then(data => {
          const imgs = (data.variants || []).map(v => v.image_url);
          const sales = (data.variants || []).map(v => v.sales_30d);
          const needs = (data.variants || []).map(v => v.need_60d);
          const ids = (data.variants || []).map(v => v.id);
          const table = document.querySelector("table");
          if (!table) return;

          const theadRow = table.querySelector("thead tr");
          if (theadRow) {
              if (theadRow.querySelector(".th-img-col")) return; // Prevent double injection

              // Remove existing duplicate "Продажи (30д)" columns if any, plus "Цвет" and "Размер"
              const headers = Array.from(theadRow.children);
              for (let i = headers.length - 1; i >= 0; i--) {
                  const txt = headers[i].textContent.trim();
                  if (txt.includes("Продажи (30д)") || txt === "Цвет" || txt === "Размер") {
                      headers[i].remove();
                      const bodyRows = table.querySelectorAll("tbody tr");
                      bodyRows.forEach(r => {
                          if (r.children[i]) r.children[i].remove();
                      });
                  }
              }

              const th = document.createElement("th");
              th.textContent = "Фото";
              th.className = "th-img-col";
              th.style.width = "70px";
              theadRow.insertBefore(th, theadRow.firstElementChild);

              const thNeed = document.createElement("th");
              thNeed.textContent = "Нужно (60д)";
              thNeed.className = "text-center";
              theadRow.appendChild(thNeed);

              const thPrint = document.createElement("th");
              thPrint.innerHTML = "&#128438;"; // Printer icon
              thPrint.className = "text-center";
              thPrint.style.width = "40px";
              theadRow.appendChild(thPrint);
          }

          const rows = table.querySelectorAll("tbody tr");
          rows.forEach((row, i) => {
              let url = imgs[i];
              const s30 = sales[i] || 0;
              const n60 = needs[i] || 0;
              const vid = ids[i];
              
              // Safety check: if URL is a JSON string (legacy bad data), parse it
              if (url && url.startsWith("{")) {
                try {
                  const parsed = JSON.parse(url);
                  url = parsed.url || parsed.link || parsed.src || null;
                } catch (e) {}
              }

              const td = document.createElement("td");
              if (url) {
                const img = document.createElement("img");
                img.src = url;
                img.className = "table-img";
                Object.assign(img.style, { width: "48px", height: "64px", objectFit: "cover", borderRadius: "4px" });
                img.onerror = function() {
                    this.onerror = null;
                    this.style.display = "none";
                };
                td.appendChild(img);
              } else {
                td.innerHTML = `<span class="text-secondary small" style="font-size:0.7rem">-</span>`;
              }
              row.insertBefore(td, row.firstElementChild);

              const tdNeed = document.createElement("td");
              tdNeed.className = "text-center fw-bold";
              tdNeed.textContent = n60;
              if (n60 > 0) {
                tdNeed.style.color = "#d32f2f";
                tdNeed.style.backgroundColor = "#fff5f5";
              } else {
                tdNeed.style.color = "#2e7d32";
              }
              row.appendChild(tdNeed);

              const tdPrint = document.createElement("td");
              tdPrint.className = "text-center";
              const aPrint = document.createElement("a");
              aPrint.href = `/print/labels?ids=${vid}`;
              aPrint.target = "_blank";
              aPrint.className = "btn btn-sm btn-outline-secondary p-0 px-1";
              aPrint.innerHTML = "&#128438;";
              aPrint.title = "Печать этикетки";
              tdPrint.appendChild(aPrint);
              row.appendChild(tdPrint);
          });
        });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initUzumUI);
  } else {
    initUzumUI();
  }

  async function postJson(url, payload) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const text = await res.text();
    let data;
    try { data = JSON.parse(text); } catch { data = { raw: text }; }
    if (!res.ok) {
      const msg = data?.error ? data.error : (typeof data === "string" ? data : JSON.stringify(data));
      throw new Error(msg);
    }
    return data;
  }

  // --- Auto Refresh Logic (Every 10 Minutes) ---
  setInterval(async () => {
    console.log("Auto-refreshing data...");
    try {
      const shopsRes = await fetch("/api/shops");
      const shopsData = await shopsRes.json();
      if (shopsData.shops) {
        for (const shop of shopsData.shops) {
          await postJson("/api/uzum/sync", { shop_id: shop.uzum_id, size: 100, sync_all: true });
        }
        window.location.reload();
      }
    } catch (e) { console.error("Auto-sync failed", e); }
  }, 10 * 60 * 1000); // 10 minutes
})();
