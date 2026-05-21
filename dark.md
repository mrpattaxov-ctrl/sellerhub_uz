# Dark Mode — Implementatsiya hujjati

Barcha o'zgartirishlar shu yerda. Yangi sahifa qo'shilsa yoki tuzatish kiritilsa, shu faylni yangilash shart.

---

## Asosiy printsip

Bootstrap 5.3 native dark mode ishlatiladi: `<html data-bs-theme="dark">`.  
Tema localStorage-da `sh-theme` kaliti bilan saqlanadi (`'light'` yoki `'dark'`).

**Muhim kaskad qoidasi:** `base.html`-ning inline `<style>` bloki tashqi CSS fayllardan KEYIN yuklangani uchun, `base.html`-ga tegishli overridelar albatta inline `<style>` ichida bo'lishi kerak. `styles.css`, `pos.css`, `auth.css` uchun — o'sha fayllarda yoziladi.

---

## Rang palitasi

| Maqsad            | Qiymat      |
|-------------------|-------------|
| Asosiy fon        | `#0f1117`   |
| Surface (karta)   | `#1a1d27`   |
| Elevated surface  | `#1e2130`   |
| Active/hover      | `#252a3f`   |
| Border            | `#2d3148`   |
| Border soft       | `#3a3f5c`   |
| Matn (asosiy)     | `#e2e8f0`   |
| Matn (muted)      | `#94a3b8`   |
| Matn (subtle)     | `#64748b`   |
| Ko'k accent       | `#60a5fa`   |
| Yashil accent     | `#4ade80`   |
| Qizil accent      | `#f87171`   |
| Binafsha accent   | `#a78bfa`   |

---

## 1. `templates/base.html`

### FOUC oldini olish (head-ning birinchi elementi)

```html
<script>(function(){var t=localStorage.getItem('sh-theme')||'light';document.documentElement.setAttribute('data-bs-theme',t);})();</script>
```

### Toggle tugmasi (navbar, brand yonida)

```html
<button id="theme-toggle"
  onclick="(function(){
    var h=document.documentElement,
        t=h.getAttribute('data-bs-theme')==='dark'?'light':'dark';
    h.setAttribute('data-bs-theme',t);
    localStorage.setItem('sh-theme',t);
    document.getElementById('sh-icon-moon').style.display=t==='dark'?'none':'inline';
    document.getElementById('sh-icon-sun').style.display=t==='dark'?'inline':'none';
  })();"
  style="background:none;border:none;cursor:pointer;padding:6px 8px;border-radius:8px;line-height:1;color:inherit;opacity:.75;transition:opacity .15s"
  onmouseover="this.style.opacity=1" onmouseout="this.style.opacity=.75"
  title="Темный / светлый режим">
  <svg id="sh-icon-moon" xmlns="http://www.w3.org/2000/svg" width="20" height="20"
       viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
       stroke-linecap="round" stroke-linejoin="round">
    <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>
  </svg>
  <svg id="sh-icon-sun" xmlns="http://www.w3.org/2000/svg" width="20" height="20"
       viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
       stroke-linecap="round" stroke-linejoin="round" style="display:none">
    <circle cx="12" cy="12" r="5"/>
    <line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/>
    <line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/>
    <line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/>
    <line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/>
    <line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/>
    <line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/>
  </svg>
</button>
```

### Icon holatini ishga tushirish (extra_js dan oldin)

```html
<script>
(function(){
  var t = document.documentElement.getAttribute('data-bs-theme') || 'light';
  var moon = document.getElementById('sh-icon-moon');
  var sun = document.getElementById('sh-icon-sun');
  if (moon && sun) {
    moon.style.display = t === 'dark' ? 'none' : '';
    sun.style.display = t === 'dark' ? '' : 'none';
  }
})();
</script>
```

### Inline `<style>` bloki ichidagi dark CSS

```css
/* ── Dark mode ── */
[data-bs-theme="dark"] body { background: #0f1117; }
[data-bs-theme="dark"] .table-responsive {
  background: #1a1d27; border-color: #2d3148;
  box-shadow: 0 2px 8px rgba(0,0,0,.3);
}
[data-bs-theme="dark"] .table thead th {
  background-color: #1e2130; color: #8892a4; border-bottom-color: #2d3148;
}
[data-bs-theme="dark"] .table tbody td { color: #e2e8f0; border-bottom-color: #2d3148; }
[data-bs-theme="dark"] .table-hover tbody tr:hover { background-color: #1e2130; }
[data-bs-theme="dark"] .table .form-control,
[data-bs-theme="dark"] .table .form-select { border-color: #3a3f5c; }
[data-bs-theme="dark"] .top-sub-chip { background:#1e2130; border-color:#3a3f5c; color:#94a3b8; }
[data-bs-theme="dark"] .top-sub-chip:hover { background:#252a3f; border-color:#4a5070; color:#e2e8f0; }
[data-bs-theme="dark"] .top-sub-chip--trial { background:#0f1e38; border-color:#1e4080; color:#60a5fa; }
[data-bs-theme="dark"] .top-sub-chip--active { background:#0a2318; border-color:#166534; color:#4ade80; }
[data-bs-theme="dark"] .top-sub-chip--expired { background:#2a0e0e; border-color:#7f1d1d; color:#f87171; }
[data-bs-theme="dark"] .top-sub-chip--unlimited { background:#1a1040; border-color:#4c1d95; color:#a78bfa; }
```

---

## 2. `static/styles.css`

53-qatordan boshlab dark mode bloki. Har bir sahifa alohida kommentariya bilan:

```css
/* ── Dark mode overrides ── */
[data-bs-theme="dark"] body { background: #0f1117; }
[data-bs-theme="dark"] .product-img { background: #1a1d27; }
[data-bs-theme="dark"] .card.product-card { border-color: #2d3148; box-shadow: 0 6px 22px rgba(0,0,0,.3); }
[data-bs-theme="dark"] .skeleton {
  background: linear-gradient(90deg, rgba(255,255,255,.04) 25%, rgba(255,255,255,.08) 37%, rgba(255,255,255,.04) 63%);
  background-size: 400% 100%;
}

/* groups.html */
[data-bs-theme="dark"] .search-wrapper { background: #1a1d27 !important; border-color: #3a3f5c !important; }
[data-bs-theme="dark"] .search-wrapper input { color: #e2e8f0 !important; background: transparent !important; }
[data-bs-theme="dark"] .shop-picker-btn { background: #1a1d27 !important; border-color: #3a3f5c !important; color: #e2e8f0 !important; }
[data-bs-theme="dark"] .shop-picker-menu { background: #1a1d27 !important; border-color: #2d3148 !important; }
[data-bs-theme="dark"] .shop-picker-item { color: #94a3b8 !important; }
[data-bs-theme="dark"] .shop-picker-item:hover { background: #1e2130 !important; color: #e2e8f0 !important; }
[data-bs-theme="dark"] .shop-picker-item.selected { background: #0f1e38 !important; color: #60a5fa !important; }
[data-bs-theme="dark"] .status-toggle { background: #1e2130 !important; }
[data-bs-theme="dark"] .status-toggle-opt { color: #64748b !important; }
[data-bs-theme="dark"] .status-toggle-opt:hover { color: #94a3b8 !important; background: #252a3f !important; }
[data-bs-theme="dark"] .status-toggle-opt.active { background: #252a3f !important; color: #e2e8f0 !important; }

/* invoice_restock.html — inline style override */
[data-bs-theme="dark"] [style*="#e6e6fa"] { background-color: #1e2130 !important; color: #a78bfa !important; border-color: #3a3f5c !important; }
[data-bs-theme="dark"] [style*="#e0ffff"] { background-color: #1e2130 !important; color: #34d399 !important; border-color: #3a3f5c !important; }
[data-bs-theme="dark"] [style*="#fff3cd"] { background-color: #1e2130 !important; color: #fbbf24 !important; border-color: #3a3f5c !important; }
[data-bs-theme="dark"] thead.table-light th { background-color: #1a1d27 !important; color: #94a3b8 !important; border-color: #2d3148 !important; }

/* group_detail.html */
[data-bs-theme="dark"] .product-header-card { background: #1a1d27 !important; border-color: #2d3148 !important; }
[data-bs-theme="dark"] .product-header-card .no-img { background: #1e2130 !important; }
[data-bs-theme="dark"] .days-toggle { background: #1e2130 !important; }
[data-bs-theme="dark"] .preset-btn { color: #64748b !important; background: transparent !important; }
[data-bs-theme="dark"] .preset-btn:hover { color: #94a3b8 !important; background: #252a3f !important; }
[data-bs-theme="dark"] .preset-btn.active { background: #252a3f !important; color: #e2e8f0 !important; }
[data-bs-theme="dark"] .apply-btn { background: #e2e8f0 !important; color: #0f1117 !important; }
[data-bs-theme="dark"] .date-range-wrapper { background: #1a1d27 !important; border-color: #3a3f5c !important; }
[data-bs-theme="dark"] .date-range-wrapper input { color: #e2e8f0 !important; background: transparent !important; }
/* flatpickr calendar */
[data-bs-theme="dark"] .flatpickr-calendar { background: #1a1d27 !important; border-color: #2d3148 !important; }
[data-bs-theme="dark"] .flatpickr-month,
[data-bs-theme="dark"] .flatpickr-monthDropdown-months { color: #e2e8f0 !important; background: #1a1d27 !important; }
[data-bs-theme="dark"] .flatpickr-weekday { color: #64748b !important; background: #1a1d27 !important; }
[data-bs-theme="dark"] .flatpickr-day { color: #94a3b8 !important; }
[data-bs-theme="dark"] .flatpickr-day:hover { background: #1e2130 !important; border-color: #2d3148 !important; }
[data-bs-theme="dark"] .flatpickr-day.today { border-color: #3a3f5c !important; }
[data-bs-theme="dark"] .flatpickr-day.selected { background: #2e6da8 !important; color: #fff !important; }
[data-bs-theme="dark"] .flatpickr-day.inRange { background: #0f1e38 !important; color: #60a5fa !important; }
[data-bs-theme="dark"] .flatpickr-prev-month svg,
[data-bs-theme="dark"] .flatpickr-next-month svg { fill: #94a3b8 !important; }

/* invoice_restock.html */
[data-bs-theme="dark"] .sortable-tbody tr.sortable-selected td { background-color: #0f1e38 !important; }
[data-bs-theme="dark"] .sortable-ghost { background-color: #0f1e38 !important; }
[data-bs-theme="dark"] .sortable-chosen td { background-color: #1a2d4a !important; }
[data-bs-theme="dark"] .selection-toggle,
[data-bs-theme="dark"] .drag-handle { background: linear-gradient(180deg, #1a1d27 0%, #151820 100%) !important; }
[data-bs-theme="dark"] .footer-summary td { border-top-color: #2d3148 !important; }
[data-bs-theme="dark"] .action-btn--excel { background: linear-gradient(135deg, #1e2130 0%, #252a3f 100%) !important; }
[data-bs-theme="dark"] .invoice-card { background: #1a1d27 !important; border-color: #2d3148 !important; }
[data-bs-theme="dark"] .invoice-card .card-header,
[data-bs-theme="dark"] .invoice-card .card-header.bg-white { background: #1a1d27 !important; border-bottom-color: #2d3148 !important; color: #e2e8f0 !important; }
[data-bs-theme="dark"] .invoice-card .card-header h5 { color: #e2e8f0 !important; }
[data-bs-theme="dark"] .invoice-card .item-count.badge.bg-secondary { background: #1e2130 !important; color: #94a3b8 !important; }
[data-bs-theme="dark"] .action-btn--uzum { background: linear-gradient(135deg, #1a1040 0%, #2a1a5a 100%) !important; color: #a78bfa !important; }
[data-bs-theme="dark"] .action-btn--uzum:hover { background: linear-gradient(135deg, #2a1a5a 0%, #3a2070 100%) !important; }
[data-bs-theme="dark"] .alert.alert-secondary { background: #1a1d27 !important; border-color: #2d3148 !important; color: #94a3b8 !important; }

/* qty-pill — jadvaldagi miqdor badge'lari (uzum/sklad rangli pillalar) */
.qty-pill {
  display: inline-block;
  padding: 4px 14px;
  border-radius: 999px;
  border: 1px solid;
  font-size: 1rem;
  font-weight: 700;
  line-height: 1.2;
}
.qty-pill--uzum { background: #efe8ff; border-color: #c4b5fd; color: #5b21b6; }
.qty-pill--wh   { background: #dcfce7; border-color: #86efac; color: #166534; }
[data-bs-theme="dark"] .qty-pill--uzum { background: rgba(167,139,250,0.1) !important; border-color: #6d28d9 !important; color: #a78bfa !important; }
[data-bs-theme="dark"] .qty-pill--wh   { background: rgba(52,211,153,0.1) !important; border-color: #059669 !important; color: #34d399 !important; }

/* warehouse_data.html */
[data-bs-theme="dark"] .warehouse-hero { background: linear-gradient(160deg, #0f1117 0%, #131625 56%, #111420 100%) !important; }
[data-bs-theme="dark"] .warehouse-kicker { background: rgba(99,132,255,.15) !important; color: #93b4ff !important; }
[data-bs-theme="dark"] .warehouse-title { color: #e2e8f0 !important; }
[data-bs-theme="dark"] .warehouse-subtitle { color: #8892a4 !important; }
[data-bs-theme="dark"] .warehouse-stat { background: rgba(30,33,48,.9) !important; border-color: rgba(99,132,255,.12) !important; }
[data-bs-theme="dark"] .warehouse-stat-label { color: #64748b !important; }
[data-bs-theme="dark"] .warehouse-stat-value { color: #e2e8f0 !important; }
[data-bs-theme="dark"] .warehouse-card { background: #1a1d27 !important; border-color: #2d3148 !important; }
[data-bs-theme="dark"] .warehouse-card-title { color: #e2e8f0 !important; }
[data-bs-theme="dark"] .warehouse-card-copy { color: #8892a4 !important; }
[data-bs-theme="dark"] .warehouse-step { background: linear-gradient(180deg, #1a1d27, #161a25) !important; border-color: #2d3148 !important; }
[data-bs-theme="dark"] .warehouse-step strong { color: #e2e8f0 !important; }
[data-bs-theme="dark"] .warehouse-step span { color: #8892a4 !important; }
[data-bs-theme="dark"] .warehouse-tag { background: rgba(99,132,255,.15) !important; color: #93b4ff !important; }
[data-bs-theme="dark"] .warehouse-dropzone { background: linear-gradient(180deg, #1a1d27, #161a25) !important; border-color: #3a3f5c !important; }
[data-bs-theme="dark"] .warehouse-dropzone-copy { color: #64748b !important; }
[data-bs-theme="dark"] .warehouse-file { background: #1e2130 !important; color: #e2e8f0 !important; }
[data-bs-theme="dark"] .warehouse-side-item { background: linear-gradient(180deg, #1a1d27, #161a25) !important; border-color: #2d3148 !important; }
[data-bs-theme="dark"] .warehouse-side-item strong { color: #e2e8f0 !important; }
[data-bs-theme="dark"] .warehouse-side-item span { color: #8892a4 !important; }
[data-bs-theme="dark"] .warehouse-flash { background: linear-gradient(135deg, rgba(99,132,255,.15), rgba(99,132,255,.08)) !important; color: #93b4ff !important; }

/* settings_notifications.html */
[data-bs-theme="dark"] .notify-kicker { background: rgba(99,132,255,.15) !important; color: #93b4ff !important; }
[data-bs-theme="dark"] .notify-title { color: #e2e8f0 !important; }
[data-bs-theme="dark"] .notify-subtitle { color: #8892a4 !important; }
[data-bs-theme="dark"] .notify-flash { background: linear-gradient(135deg, rgba(99,132,255,.12), rgba(99,132,255,.06)) !important; color: #93b4ff !important; }
[data-bs-theme="dark"] .notify-summary { background: linear-gradient(160deg, #0f1117 0%, #131625 55%, #111420 100%) !important; }
[data-bs-theme="dark"] .notify-summary-title { color: #e2e8f0 !important; }
[data-bs-theme="dark"] .notify-summary-copy { color: #8892a4 !important; }
[data-bs-theme="dark"] .notify-chip { background: rgba(30,33,48,.9) !important; border-color: rgba(99,132,255,.12) !important; }
[data-bs-theme="dark"] .notify-chip-label { color: #64748b !important; }
[data-bs-theme="dark"] .notify-chip-value { color: #e2e8f0 !important; }
[data-bs-theme="dark"] .notify-card { background: #1a1d27 !important; border-color: #2d3148 !important; }
[data-bs-theme="dark"] .notify-card-title { color: #e2e8f0 !important; }
[data-bs-theme="dark"] .notify-card-copy { color: #8892a4 !important; }
[data-bs-theme="dark"] .notify-toggle { background: linear-gradient(180deg, #1a1d27, #161a25) !important; border-color: #2d3148 !important; }
[data-bs-theme="dark"] .notify-toggle-ui { background: #3a3f5c !important; }
[data-bs-theme="dark"] .notify-toggle-copy strong { color: #e2e8f0 !important; }
[data-bs-theme="dark"] .notify-toggle-copy span { color: #8892a4 !important; }
[data-bs-theme="dark"] .notify-select { background: #1a1d27 !important; border-color: #3a3f5c !important; color: #e2e8f0 !important; }
[data-bs-theme="dark"] .notify-footer-note { color: #64748b !important; }

/* subscription.html */
[data-bs-theme="dark"] .sub-alert,
[data-bs-theme="dark"] .sub-card { background: #1a1d27 !important; border-color: #2d3148 !important; }
[data-bs-theme="dark"] .sub-head h2 { color: #e2e8f0 !important; }
[data-bs-theme="dark"] .sub-head p { color: #8892a4 !important; }
[data-bs-theme="dark"] .sub-step p { color: #94a3b8 !important; }
[data-bs-theme="dark"] .sub-stat small,
[data-bs-theme="dark"] .sub-stat p,
[data-bs-theme="dark"] .sub-selected-hint,
[data-bs-theme="dark"] .sub-plan-desc { color: #8892a4 !important; }
[data-bs-theme="dark"] .sub-stat strong,
[data-bs-theme="dark"] .sub-plan-title,
[data-bs-theme="dark"] .sub-plan-price,
[data-bs-theme="dark"] .sub-selected-label { color: #e2e8f0 !important; }
[data-bs-theme="dark"] .sub-pill,
[data-bs-theme="dark"] .sub-step-num,
[data-bs-theme="dark"] .sub-chip { background: #1e2130 !important; border-color: #2d3148 !important; color: #94a3b8 !important; }
[data-bs-theme="dark"] .sub-plan { background: #1a1d27 !important; border-color: #2d3148 !important; }
[data-bs-theme="dark"] .sub-plan.selected { border-color: #2e6da8 !important; background: #0f1e38 !important; }
[data-bs-theme="dark"] .sub-plan-btn { background: #1e2d4a !important; color: #60a5fa !important; }
[data-bs-theme="dark"] .sub-pay-btn,
[data-bs-theme="dark"] .sub-code-submit { background: #e2e8f0 !important; color: #0f1117 !important; }
[data-bs-theme="dark"] .sub-selected-box { background: #1a1d27 !important; border-color: #2d3148 !important; }
[data-bs-theme="dark"] .sub-code-input { border-color: #3a3f5c !important; background: #1a1d27 !important; color: #e2e8f0 !important; }

/* subscription_expired.html */
[data-bs-theme="dark"] .expired-card { background: linear-gradient(180deg, #1a0a0a 0%, #1a1d27 100%) !important; border-color: #4a1a1a !important; }
[data-bs-theme="dark"] .expired-badge { background: #2a0e0e !important; color: #f87171 !important; }
[data-bs-theme="dark"] .expired-meta-box { background: rgba(30,10,10,.86) !important; border-color: #4a1a1a !important; }
[data-bs-theme="dark"] .expired-meta-box small { color: #f87171 !important; }
[data-bs-theme="dark"] .expired-meta-box strong { color: #e2e8f0 !important; }

/* admin_subscriptions.html */
[data-bs-theme="dark"] .admin-sub-card { background: #1a1d27 !important; border-color: #2d3148 !important; }
[data-bs-theme="dark"] .admin-sub-hero { background: linear-gradient(135deg, #0f1e38 0%, #131625 60%, #0f1117 100%) !important; }
[data-bs-theme="dark"] .admin-sub-kicker { background: #0f1e38 !important; color: #60a5fa !important; }
[data-bs-theme="dark"] .admin-sub-chip { background: #1a1d27 !important; border-color: #2d3148 !important; color: #94a3b8 !important; }
[data-bs-theme="dark"] .admin-sub-head h1,
[data-bs-theme="dark"] .admin-sub-head h2 { color: #e2e8f0 !important; }
[data-bs-theme="dark"] .admin-sub-head p { color: #8892a4 !important; }
[data-bs-theme="dark"] .admin-sub-overview-box { background: #1a1d27 !important; border-color: #2d3148 !important; }
[data-bs-theme="dark"] .admin-sub-overview-box small { color: #64748b !important; }
[data-bs-theme="dark"] .admin-sub-overview-box strong { color: #e2e8f0 !important; }
[data-bs-theme="dark"] .admin-sub-plan-card { background: #1a1d27 !important; }
[data-bs-theme="dark"] .admin-sub-alert { background: #0f1e38 !important; border-color: #1e4080 !important; }
[data-bs-theme="dark"] .admin-sub-table { border-color: #2d3148 !important; }
[data-bs-theme="dark"] .admin-code,
[data-bs-theme="dark"] .admin-sub-note { background: #1e2130 !important; border-color: #2d3148 !important; color: #94a3b8 !important; }
[data-bs-theme="dark"] .admin-sub-state--active { background: #0a2318 !important; color: #4ade80 !important; }
[data-bs-theme="dark"] .admin-sub-state--trial { background: #0f1e38 !important; color: #60a5fa !important; }
[data-bs-theme="dark"] .admin-sub-state--expired { background: #2a0e0e !important; color: #f87171 !important; }
[data-bs-theme="dark"] .admin-sub-state--admin { background: #1a1040 !important; color: #a78bfa !important; }
```

---

## 3. `static/pos.css`

### CSS o'zgaruvchilar (faylning boshida)

```css
:root {
  --pos-border: #d0d5dd;
  --pos-border-soft: #eaecf0;
  --pos-surface: #ffffff;
  --pos-muted: #667085;
  --pos-muted-soft: #98a2b3;
  --pos-bg: #f7f7f9;
}

[data-bs-theme="dark"] {
  --pos-border: #2d3148;
  --pos-border-soft: #1e2130;
  --pos-surface: #1a1d27;
  --pos-muted: #64748b;
  --pos-muted-soft: #475569;
  --pos-bg: #0f1117;
}
```

### Qattiq overridelar (faylning oxirida)

```css
/* ── Dark mode ── */
[data-bs-theme="dark"] .pos-search-bar { background: #1a1d27; }
[data-bs-theme="dark"] .pos-search-bar input { color: #e2e8f0; }
[data-bs-theme="dark"] .pos-search-bar input::placeholder { color: #475569; }
[data-bs-theme="dark"] .pos-toolbar-btn { background: #1a1d27; color: #94a3b8; border-color: #2d3148; }
[data-bs-theme="dark"] .pos-toolbar-btn:hover,
[data-bs-theme="dark"] .pos-toolbar-btn:focus-visible { background: #1e2130; color: #e2e8f0; }
[data-bs-theme="dark"] .pos-product-media { background: linear-gradient(180deg, #1a1d27 0%, #1e2130 100%); }
[data-bs-theme="dark"] .pos-product-title { color: #e2e8f0; }
[data-bs-theme="dark"] .pos-product-meta,
[data-bs-theme="dark"] .pos-product-sku { color: #64748b; }
[data-bs-theme="dark"] .pos-mode-toggle { background: #1e2130; }
[data-bs-theme="dark"] .pos-mode-toggle label { color: #64748b; }
[data-bs-theme="dark"] .pos-mode-toggle input:checked + label { background: #252a3f; color: #e2e8f0; box-shadow: 0 1px 2px rgba(0,0,0,.3); }
[data-bs-theme="dark"] .pos-cart-title h5 { color: #e2e8f0; }
[data-bs-theme="dark"] .pos-cart-list { background: #0f1117; }
[data-bs-theme="dark"] .pos-cart-item { background: #1a1d27; border-color: #2d3148; }
[data-bs-theme="dark"] .pos-cart-thumb-frame,
[data-bs-theme="dark"] .pos-cart-thumb--empty { background: #1e2130; }
[data-bs-theme="dark"] .pos-cart-name { color: #e2e8f0; }
[data-bs-theme="dark"] .pos-cart-meta { color: #64748b; }
[data-bs-theme="dark"] .pos-qty-stepper { border-color: #2d3148; background: #1a1d27; }
[data-bs-theme="dark"] .pos-qty-stepper button { background: #1e2130; color: #e2e8f0; }
[data-bs-theme="dark"] .pos-qty-stepper input { color: #e2e8f0; }
[data-bs-theme="dark"] .pos-action-footer { background: #1a1d27; box-shadow: 0 -12px 20px rgba(0,0,0,.2); }
[data-bs-theme="dark"] .pos-action-summary { color: #64748b; }
[data-bs-theme="dark"] .pos-action-summary strong { color: #e2e8f0; }
[data-bs-theme="dark"] .pos-size-picker { background: linear-gradient(180deg, #1a1d27 0%, #161a25 100%); }
[data-bs-theme="dark"] .pos-size-picker__label { color: #64748b; }
[data-bs-theme="dark"] .pos-size-picker__select { background: #1a1d27; color: #e2e8f0; }
[data-bs-theme="dark"] .pos-primary-btn { background: #e2e8f0; color: #0f1117; }
[data-bs-theme="dark"] .pos-primary-btn--danger { background: #d92d20; color: #fff; }
[data-bs-theme="dark"] .pos-primary-btn--success { background: #157347; color: #fff; }
[data-bs-theme="dark"] .pos-empty-state { color: #64748b; }
[data-bs-theme="dark"] .pos-empty-state h6 { color: #94a3b8; }
[data-bs-theme="dark"] .pos-floating-preview { background: rgba(26,29,39,0.98); border-color: #2d3148; }
[data-bs-theme="dark"] .pos-floating-preview-media { background: linear-gradient(180deg, #1a1d27 0%, #1e2130 100%); }
[data-bs-theme="dark"] .pos-floating-preview-caption { color: #94a3b8; }
```

---

## 4. `static/uzum_ui.js`

Sidebar JS tomonidan yaratiladi va `layoutStyle.innerHTML` orqali CSS inject qilinadi.

### Sidebar o'lchami (2026-04-21 da kichraytirilgan)

Oldin 280px ochiq / 60px yopiq edi — foydalanuvchi "joy kotta" deb aytgani uchun kichraytirildi:

```css
/* Eski qiymatlar (280/60) o'rniga: */
body { margin-left: 220px !important; background-color: #F9FAFB; transition: margin-left 0.3s ease; }
body.sidebar-closed { margin-left: 56px !important; }

.uzum-sidebar {
  position: fixed; top: 0; left: 0; height: 100vh; width: 220px;
  background: #FFFFFF; border-right: 1px solid #EAECF0;
  display: flex; flex-direction: column;
  z-index: 1050; overflow-y: auto; overflow-x: hidden;
  font-family: system-ui, -apple-system, sans-serif;
  transition: width 0.3s ease;
}
.uzum-sidebar.closed { width: 56px; }

.uzum-sidebar-header {
  padding: 16px 14px;
  border-bottom: 1px solid #EAECF0;
  display: flex; align-items: center; justify-content: space-between;
  min-height: 56px;
}
.uzum-sidebar.closed .uzum-sidebar-header {
  padding: 16px 0; justify-content: center;
}

.uzum-sidebar-content {
  padding: 12px 10px;
  display: flex; flex-direction: column; gap: 16px;
  flex: 1;
}
.uzum-sidebar.closed .uzum-sidebar-content { padding: 10px 0; gap: 12px; }

.uzum-nav-item {
  display: flex; align-items: center; gap: 10px;
  padding: 8px 10px; border-radius: 6px;
  color: #344054; text-decoration: none; font-weight: 500; font-size: 13px;
  transition: all 0.2s; white-space: nowrap; overflow: hidden;
}
```

Eski → yangi jadval:

| Element | Eski | Yangi |
|---------|------|-------|
| Sidebar width (open) | 280px | 220px |
| Sidebar width (closed) | 60px | 56px |
| `body` margin-left | 280px | 220px |
| `body.sidebar-closed` margin-left | 60px | 56px |
| Header padding | 24px 20px | 16px 14px |
| Header min-height | 73px | 56px |
| Closed header padding | 24px 0 | 16px 0 |
| Content padding | 20px | 12px 10px |
| Content gap | 24px | 16px |
| Closed content padding | 12px 0 | 10px 0 |
| Closed content gap | 16px | 12px |
| Nav-item padding | 10px 12px | 8px 10px |
| Nav-item gap | 12px | 10px |
| Nav-item font-size | 14px | 13px |

### Dark overridelar (layoutStyle string ichida)

```css
[data-bs-theme="dark"] body { background-color: #0f1117 !important; }
[data-bs-theme="dark"] .uzum-sidebar { background: #1a1d27; border-right-color: #2d3148; }
[data-bs-theme="dark"] .uzum-sidebar-header { border-bottom-color: #2d3148; }
[data-bs-theme="dark"] .uzum-brand { color: #e2e8f0; }
[data-bs-theme="dark"] .uzum-brand svg path { stroke: #94a3b8; }
[data-bs-theme="dark"] .uzum-toggle-btn { color: #64748b; }
[data-bs-theme="dark"] .uzum-toggle-btn:hover { color: #e2e8f0; }
[data-bs-theme="dark"] .uzum-nav-item { color: #94a3b8; }
[data-bs-theme="dark"] .uzum-nav-item svg { stroke: #64748b; }
[data-bs-theme="dark"] .uzum-nav-item:hover { background: #1e2130; color: #e2e8f0; }
[data-bs-theme="dark"] .uzum-nav-item:hover svg { stroke: #94a3b8; }
[data-bs-theme="dark"] .uzum-nav-item.active { background: #252a3f; color: #e2e8f0; }
[data-bs-theme="dark"] .uzum-nav-item.active svg { stroke: #94a3b8; }
[data-bs-theme="dark"] .uzum-section-label { color: #475569; }
[data-bs-theme="dark"] .uzum-sidebar input.form-control { background: #1e2130; border-color: #3a3f5c; color: #e2e8f0; }
[data-bs-theme="dark"] .uzum-segment { background: #1e2130; }
[data-bs-theme="dark"] .uzum-segment-opt { color: #64748b; }
[data-bs-theme="dark"] .uzum-segment-opt.active { background: #252a3f; color: #e2e8f0; box-shadow: 0 1px 2px rgba(0,0,0,.3); }
```

---

## 5. `templates/economics.html` (standalone — base.html dan kelmaydi)

`<head>` boshiga FOUC skript + `<style>` tegi ichiga:

```html
<!-- head boshiga -->
<script>(function(){var t=localStorage.getItem('sh-theme')||'light';document.documentElement.setAttribute('data-bs-theme',t);})();</script>
```

```css
/* style tegi ichida */
[data-bs-theme="dark"] body { background-color:#0f1117 !important; color:#e2e8f0 !important; }
[data-bs-theme="dark"] .card-stat { background:#1a1d27 !important; border-color:#2d3148 !important; }
[data-bs-theme="dark"] .stat-label { color:#64748b !important; }
[data-bs-theme="dark"] .stat-val { color:#e2e8f0 !important; }
[data-bs-theme="dark"] .stat-subtitle { color:#475569 !important; }
[data-bs-theme="dark"] .info-btn { border-color:#3a3f5c !important; background:#1a1d27 !important; color:#64748b !important; }
[data-bs-theme="dark"] .info-btn:hover { color:#e2e8f0 !important; background:#1e2130 !important; }
[data-bs-theme="dark"] .info-popover { background:#e2e8f0 !important; color:#0f1117 !important; }
[data-bs-theme="dark"] .table-card { background:#1a1d27 !important; border-color:#2d3148 !important; }
[data-bs-theme="dark"] .table thead th { background:#1e2130 !important; color:#64748b !important; border-bottom-color:#2d3148 !important; }
[data-bs-theme="dark"] .table tbody td { color:#e2e8f0 !important; border-bottom-color:#2d3148 !important; }
[data-bs-theme="dark"] .table tbody tr:hover { background:#1e2130 !important; }
[data-bs-theme="dark"] .badge-roi.positive { background:#0a2318 !important; color:#4ade80 !important; }
[data-bs-theme="dark"] .badge-roi.neutral  { background:#1e2130 !important; color:#94a3b8 !important; }
[data-bs-theme="dark"] .badge-roi.negative { background:#2a0e0e !important; color:#f87171 !important; }
[data-bs-theme="dark"] .skeleton { background:linear-gradient(90deg,#1e2130 25%,#252a3f 50%,#1e2130 75%) !important; }
[data-bs-theme="dark"] #statusBar { background:#0f1e38 !important; border-color:#1e4080 !important; color:#60a5fa !important; }
[data-bs-theme="dark"] #statusBar.error { background:#2a0e0e !important; border-color:#7f1d1d !important; color:#f87171 !important; }
[data-bs-theme="dark"] #cacheBar { background:#1a1d27 !important; border-color:#2d3148 !important; color:#94a3b8 !important; }
[data-bs-theme="dark"] #cacheBar span { color:#64748b !important; }
[data-bs-theme="dark"] #btnThisMonth { background:#1e2130 !important; color:#94a3b8 !important; border:1px solid #3a3f5c !important; }
[data-bs-theme="dark"] #btnApply { background:#e2e8f0 !important; color:#0f1117 !important; }
[data-bs-theme="dark"] .date-range-wrapper { background:#1a1d27 !important; border-color:#3a3f5c !important; }
[data-bs-theme="dark"] .date-range-wrapper input { color:#e2e8f0 !important; }
[data-bs-theme="dark"] .flatpickr-calendar { background:#1a1d27 !important; border-color:#2d3148 !important; }
[data-bs-theme="dark"] .flatpickr-month,
[data-bs-theme="dark"] .flatpickr-monthDropdown-months { color:#e2e8f0 !important; background:#1a1d27 !important; }
[data-bs-theme="dark"] .flatpickr-weekday { color:#64748b !important; background:#1a1d27 !important; }
[data-bs-theme="dark"] .flatpickr-day { color:#94a3b8 !important; }
[data-bs-theme="dark"] .flatpickr-day:hover { background:#1e2130 !important; border-color:#2d3148 !important; }
[data-bs-theme="dark"] .flatpickr-day.today { border-color:#3a3f5c !important; }
[data-bs-theme="dark"] .flatpickr-day.selected { background:#2e6da8 !important; color:#fff !important; }
[data-bs-theme="dark"] .flatpickr-day.inRange { background:#0f1e38 !important; color:#60a5fa !important; }
[data-bs-theme="dark"] .flatpickr-prev-month svg,
[data-bs-theme="dark"] .flatpickr-next-month svg { fill:#94a3b8 !important; }
```

---

## 6. `templates/fetch.html` (standalone)

```html
<!-- head boshiga -->
<script>(function(){var t=localStorage.getItem('sh-theme')||'light';document.documentElement.setAttribute('data-bs-theme',t);})();</script>
```

```css
[data-bs-theme="dark"] body { background:#0f1117 !important; color:#e2e8f0 !important; }
[data-bs-theme="dark"] .page-header { background:#1a1d27 !important; border-color:#2d3148 !important; }
[data-bs-theme="dark"] .form-shell { background:#1a1d27 !important; border-color:#2d3148 !important; }
[data-bs-theme="dark"] .fetch-btn--back { color:#94a3b8 !important; background:#1a1d27 !important; border-color:#2d3148 !important; }
[data-bs-theme="dark"] .fetch-btn--primary { background:#e2e8f0 !important; color:#0f1117 !important; }
[data-bs-theme="dark"] .shop-limit-pill { background:#1a1d27 !important; border-color:#2d3148 !important; color:#94a3b8 !important; }
[data-bs-theme="dark"] .shop-limit-pill--full { background:#2a0e0e !important; border-color:#7f1d1d !important; color:#f87171 !important; }
[data-bs-theme="dark"] .shop-card { background:#1a1d27 !important; border-color:#2d3148 !important; }
[data-bs-theme="dark"] .shop-action-btn--sync { color:#a78bfa !important; background:#1a1040 !important; }
[data-bs-theme="dark"] .shop-action-btn--edit { color:#94a3b8 !important; background:#1e2130 !important; }
[data-bs-theme="dark"] .shop-action-btn--delete { color:#f87171 !important; background:#2a0e0e !important; }
```

---

## 7. `static/auth.css`

Login/register sahifalari (faylning oxirida):

```css
[data-bs-theme="dark"] body.auth-body { background: #0f1117; color: #e2e8f0; }
[data-bs-theme="dark"] .auth-panel { background: linear-gradient(180deg, #0f1117 0%, #131625 100%); }
[data-bs-theme="dark"] .auth-card,
[data-bs-theme="dark"] .auth-admin-card { background: #1a1d27; border-color: #2d3148; box-shadow: 0 18px 40px rgba(0,0,0,.3); }
[data-bs-theme="dark"] .auth-card-title { color: #e2e8f0; }
[data-bs-theme="dark"] .auth-card-subtitle { color: #8892a4; }
[data-bs-theme="dark"] .auth-label { color: #94a3b8; }
[data-bs-theme="dark"] .auth-input { background: #1e2130; border-color: #3a3f5c; color: #e2e8f0; }
[data-bs-theme="dark"] .auth-input:focus { border-color: #2e90fa; }
[data-bs-theme="dark"] .auth-btn--dark { background: #e2e8f0; color: #0f1117; }
[data-bs-theme="dark"] .auth-btn--outline { background: #1e2130; border-color: #3a3f5c; color: #94a3b8; }
[data-bs-theme="dark"] .auth-wait-box { background: #1e2130; border-color: #2d3148; color: #94a3b8; }
[data-bs-theme="dark"] .auth-inline-note { border-top-color: #2d3148; color: #64748b; }
[data-bs-theme="dark"] .auth-inline-note a { color: #94a3b8; }
[data-bs-theme="dark"] .auth-muted-link { color: #64748b; }
[data-bs-theme="dark"] .auth-muted-link:hover { color: #94a3b8; }
[data-bs-theme="dark"] .auth-admin-shell { background: #0f1117; }
```

---

## Yangi sahifa qo'shilganda nima qilish kerak

1. **`base.html` extend qiladigan sahifa** — CSS klasslari `styles.css` ga qo'shiladi (sahifa nomi kommentariya bilan).
2. **Standalone sahifa** (base.html dan kelmaydi) — FOUC skripti `<head>` boshiga + dark CSS o'sha faylning `<style>` tegiga yoziladi.
3. **Yangi CSS fayl** — `pos.css` kabi: CSS o'zgaruvchilar bloki boshiga, qattiq overridelar faylning oxiriga.
4. **Inline `style=` atributlari** — `[data-bs-theme="dark"] [style*="#rrggbb"]` selektor bilan override qilinadi (`styles.css` da).
5. **Shu faylni yangilash** — yuqoridagi qoidalarga mos bo'lim qo'shiladi.

---

## O'zgartirishlar tarixi

| Sana       | Nima o'zgardi |
|------------|--------------|
| 2026-04-20 | Dastlabki implementatsiya — barcha sahifalar |
| 2026-04-20 | subscription sub-head h2/p, economics cacheBar/#btnThisMonth, invoice_restock jadval ranglari tuzatildi |
| 2026-04-20 | invoice_restock: oq `card-header` stripe (bg-white) qorong'i qilindi, `action-btn--uzum` qorong'i, `Итого` row footer-cell'lari `.qty-pill` badge'iga aylantirildi (templates/invoice_restock.html + styles.css) |
| 2026-04-21 | Docker rebuild uchun `.env` yaratildi (`.env.example` nusxasi), eski `sellerhub-*` konteynerlari to'xtatildi — port 5432 bo'shatildi. `docker compose up -d --build` qayta ishga tushirildi. |
| 2026-04-21 | Sidebar o'lchami kichraytirildi: 280→220px (ochiq), 60→56px (yopiq), header padding 24/20→16/14, content padding 20→12/10, nav-item font 14→13px. Detallar "4. uzum_ui.js" bo'limida. |
| 2026-04-21 | Yangi sahifa: `templates/product_create.html` («Создать карточка», MVP-1 cascade kategoriya tanlash). Inline `<style>` ichida `.pc-*` klasslari uchun dark overridelar qo'shildi (palitra: surface `#1a1d27`, border `#2d3148`, accent `#a78bfa`/`#7c3aed`). Detallar `Создать карточка.md` da. |

---

## Dark mode tegadigan barcha fayllar ro'yxati

Yangi kontekst uchun aniq adreslar:

```
sellerhub_uz-codex-payme-in-progress/
├── dark.md                            ← shu hujjat (har doim yangilanadi)
├── templates/
│   ├── base.html                      ← FOUC skript + toggle tugmasi + inline dark CSS
│   ├── economics.html                 ← standalone — o'zi head-da FOUC va dark CSS saqlaydi
│   ├── fetch.html                     ← standalone — o'zi head-da FOUC va dark CSS saqlaydi
│   └── product_create.html            ← «Создать карточка» — `.pc-*` klasslar uchun inline dark overridelar
└── static/
    ├── styles.css                     ← `[data-bs-theme="dark"]` bloki 54-qatordan boshlanadi
    ├── pos.css                        ← POS sahifalari: CSS o'zgaruvchilar + qattiq overridelar
    ├── auth.css                       ← login/register uchun
    └── uzum_ui.js                     ← sidebar JS, `layoutStyle.innerHTML` ichida 325-qator
```

Dark mode TEGMAYDIGAN (tekshirilmagan yoki kerak emas):
- `templates/*.html` dan base.html extend qiladiganlar: ular styles.css orqali boshqariladi.
- `static/fonts/`, `static/img/`, `*.min.css` — o'zgartirilmaydi.

---

## Sessiya summary (2026-04-20 ish holati)

Ushbu chat davomida nima qilindi:

1. **Boshlang'ich holat**: SellerHub (Flask + Bootstrap 5.3.3) loyihasida dark mode yo'q edi.
2. **Core infra**:
   - `base.html` ga FOUC-oldini-olish skripti (`localStorage.sh-theme` → `data-bs-theme`) qo'shildi.
   - Navbar-da oy/quyosh icon-li toggle tugmasi.
   - Bootstrap 5.3 native dark rejimidan foydalanildi.
3. **Har bir sahifa uchun dark CSS yozildi** (yuqoridagi palitra asosida).
4. **Jonli tuzatishlar** (foydalanuvchi screenshot orqali ko'rsatgan xatolar):
   - **Subscription** sahifasidagi sarlavhalar qora matn bilan edi → `.sub-head h2/p` ga `#e2e8f0` berildi.
   - **Economics** sahifasidagi `#cacheBar` (oq polosa) va `#btnThisMonth` (oq tugma) qora mode-ga moslashtirildi.
   - **invoice_restock**: jadvalning rangli hujayralari (`#e6e6fa`, `#e0ffff`, `#fff3cd`) neytral `#1e2130` ga, matn uchun accent rang.
   - **invoice_restock card-header**: `bg-white` yuqoridagi oq polosa `#1a1d27` ga.
   - **invoice_restock "Итого" qatori**: ikkita g'aliz rangli hujayra (`0`, `0`) `.qty-pill` pill-badge'iga aylantirildi (binafsha/yashil).
   - **"Отправить в Uzum" tugmasi** qorong'i gradient (`#1a1040 → #2a1a5a`) + binafsha matn.
5. **`dark.md` hujjati** yozildi va har tuzatishda yangilandi.
6. **Docker** qayta build qilindi (`docker compose up --build -d`).

---

## Sessiya summary (2026-04-21 ish holati)

Ushbu chat davomida nima qilindi:

1. **Docker setup muammolari hal qilindi**:
   - `.env` fayl yo'q edi — `.env.example` dan nusxa olindi (`cp .env.example .env`).
   - Port 5432 band edi (eski `sellerhub-app-1` / `sellerhub-db-1` konteynerlari). Ularni to'xtatib olib tashlandi:
     ```bash
     docker stop sellerhub-app-1 sellerhub-db-1
     docker rm sellerhub-app-1 sellerhub-db-1
     ```
   - `app` container `db` host nomini hal qila olmadi (network stale) — `docker compose down && docker compose up -d` bilan tuzatildi.
2. **Admin credentials** topildi (`app.py:119`): default `admin/admin` (`ADMIN_DEFAULT_PASSWORD` env var).
3. **Sidebar kichiklashtirildi** (`static/uzum_ui.js`):
   - Foydalanuvchi "joy kotta bo'pketkan" (joy katta bo'lib ketgan) deb screenshot yuborgan.
   - 280px→220px, header 73px→56px, nav item 14px→13px font.
4. **Docker rebuild**: `docker compose up -d --build` — static fayllar image ichiga `COPY . .` bilan ko'chiriladi, shuning uchun har CSS/JS o'zgarishdan keyin rebuild kerak.

**Muhim eslatma**: `docker-compose.yml` da app source code uchun volume mount yo'q — faqat `app_data:/app/data`. Har bir static/template o'zgarishi rebuild talab qiladi.

---

