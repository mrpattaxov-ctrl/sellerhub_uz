# Mobile-Friendly Wave 1 (Foundation + Proof) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the reusable mobile machinery for sellerhub_uz — an off-canvas sidebar drawer, a table-to-cards mechanism, a responsive topbar, and touch sizing — and prove it on two representative pages.

**Architecture:** All shared chrome lives in three files loaded on nearly every page: `static/uzum_ui.js` (injects the sidebar + layout CSS, and will own the mobile drawer + the `.rwd-card` auto-labeler), `static/styles.css` (global CSS: `.rwd-card`/`.rwd-cards`, responsive topbar, touch sizing), and `templates/_topbar.html` (the hamburger is injected into it by JS). Desktop layout (≥ 1024px) must remain visually unchanged; all new behavior is gated behind `@media (max-width: 768px)`.

**Tech Stack:** Flask + Jinja templates, Bootstrap 5.3, vanilla JS (no build step), existing `--tb-*` design tokens and `.pp-*` topbar classes.

## Global Constraints

- **Phone breakpoint:** `max-width: 768px`. **Small-phone breakpoint:** `max-width: 480px`. Use these exact values.
- **Dark-mode parity:** every new visual rule that sets a color must use existing `--tb-*` tokens or have a `[data-bs-theme="dark"]` counterpart.
- **No desktop regression:** nothing above 768px may change. Verify one desktop width (≥ 1024px) per proof page.
- **Not a git repo:** there are no commits. Each task ends with a **visual verification checkpoint** instead. Do not run `git`.
- **No test framework for this work:** "verification" = load the page in a browser at the given widths and confirm the listed observations (screenshots).
- **Cache busting:** `styles.css` and `uzum_ui.js` are referenced with `?v=` query strings (e.g. `styles.css?v=5`, `uzum_ui.js?v=20260613c`) that differ per page. After editing these files, bump the query string on any page under test and hard-refresh, or the browser serves stale assets.
- **Touch targets:** interactive controls ≥ 40px tall on phones.

---

### Task 1: Sidebar → mobile off-canvas drawer

Turn the fixed 220px sidebar into a hidden off-canvas drawer on phones, with a hamburger toggle and a backdrop. This is the single most important fix — without it, content has ~155px of width on a phone.

**Files:**
- Modify: `static/uzum_ui.js` — append mobile rules to the injected `layoutStyle.innerHTML` template literal (currently ends ~line 359, just before the closing `` ` ``), and add drawer JS inside `initUzumUI` after the sidebar is injected and its toggle wired (~after line 481).

**Interfaces:**
- Consumes: existing `sidebar` element (`.uzum-sidebar`), the `.pp-topbar` element, `document.body`.
- Produces: `.uzum-drawer-backdrop` element, `.uzum-hamburger` button (first child of `.pp-topbar`), and the `drawer-open` class on `.uzum-sidebar`. No global functions exported.

- [ ] **Step 1: Append the mobile drawer CSS** to the end of the `layoutStyle.innerHTML` template literal in `initUzumUI` (immediately before the closing `` `; `` at ~line 359):

```css
      /* ── Hamburger (mobile only) ── */
      .uzum-hamburger { display: none; }

      /* ── Mobile: sidebar becomes an off-canvas drawer ── */
      @media (max-width: 768px) {
        body, body.sidebar-closed { margin-left: 0 !important; }
        .uzum-sidebar, .uzum-sidebar.closed {
          width: 272px;
          transform: translateX(-100%);
          transition: transform 0.28s ease;
        }
        .uzum-sidebar.drawer-open {
          transform: translateX(0);
          box-shadow: 0 12px 40px rgba(16,24,40,.28);
        }
        /* keep labels + header full even if desktop-collapsed state persisted */
        .uzum-sidebar.closed .uzum-nav-item { justify-content: flex-start; padding: 8px 10px; border-radius: 6px; }
        .uzum-sidebar.closed .uzum-nav-item .nav-label { opacity: 1; width: auto; }
        .uzum-sidebar.closed .uzum-sidebar-header { padding: 16px 14px; justify-content: space-between; }
        .uzum-sidebar.closed .uzum-brand #uzumBrandLabel { display: inline; }
        .uzum-sidebar .uzum-toggle-btn { display: none; }
        .uzum-drawer-backdrop {
          position: fixed; inset: 0; z-index: 1049;
          background: rgba(16,24,40,.45);
          opacity: 0; visibility: hidden;
          transition: opacity .28s ease, visibility .28s ease;
        }
        .uzum-drawer-backdrop.show { opacity: 1; visibility: visible; }
        .uzum-hamburger { display: inline-flex !important; }
      }
      [data-bs-theme="dark"] .uzum-drawer-backdrop { background: rgba(0,0,0,.6); }
```

- [ ] **Step 2: Add the drawer JS** inside `initUzumUI`, after the existing sidebar toggle logic (after the `if (localStorage.getItem("uzum_sidebar_closed") === "true") toggleSidebar();` line, ~line 481):

```javascript
    // ── Mobile off-canvas drawer ──
    const _mq = window.matchMedia("(max-width: 768px)");

    const backdrop = document.createElement("div");
    backdrop.className = "uzum-drawer-backdrop";
    document.body.appendChild(backdrop);

    let hamburger = null;
    const topbar = document.querySelector(".pp-topbar");
    if (topbar) {
      hamburger = document.createElement("button");
      hamburger.className = "uzum-hamburger pp-tb-ghost";
      hamburger.setAttribute("aria-label", "Menu");
      hamburger.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></svg>';
      topbar.insertBefore(hamburger, topbar.firstChild);
    }

    const openDrawer = () => {
      sidebar.classList.add("drawer-open");
      backdrop.classList.add("show");
      document.body.style.overflow = "hidden";
    };
    const closeDrawer = () => {
      sidebar.classList.remove("drawer-open");
      backdrop.classList.remove("show");
      document.body.style.overflow = "";
    };

    if (hamburger) hamburger.addEventListener("click", (e) => { e.stopPropagation(); openDrawer(); });
    backdrop.addEventListener("click", closeDrawer);
    sidebar.querySelectorAll(".uzum-nav-item").forEach((a) =>
      a.addEventListener("click", () => { if (_mq.matches) closeDrawer(); })
    );
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });
    _mq.addEventListener("change", (e) => { if (!e.matches) closeDrawer(); });
```

- [ ] **Step 3: Bump the cache-bust version** so the edited JS loads. In `templates/base.html` change `uzum_ui.js?v=20260613c` → `uzum_ui.js?v=20260712a`. (Standalone pages under test are handled in their own tasks.)

- [ ] **Step 4: Verify — desktop unchanged.** Load `/admin/users` (or any base page) at 1280px wide. Confirm: sidebar visible at 220px on the left, content shifted right as before, no hamburger visible, desktop collapse toggle still works. Screenshot.

- [ ] **Step 5: Verify — mobile drawer.** Load the same page at 375px wide. Confirm: sidebar hidden, content full-width (no ~155px squeeze), a hamburger appears at the far left of the topbar. Tap the hamburger → drawer slides in over content (~272px) with a dark backdrop; nav labels are readable. Tap the backdrop → drawer closes. Open again, tap a nav link → drawer closes and navigates. Press Escape → closes. Screenshot open + closed states.

- [ ] **Step 6: Verify — dark mode.** Toggle dark theme, repeat the 375px open/close check. Backdrop is darker, drawer surfaces use dark tokens. Screenshot.

- [ ] **Checkpoint:** Drawer works on phone, desktop untouched, dark mode intact.

---

### Task 2: `.rwd-card` and `.rwd-cards` CSS

Add the table-to-cards mechanism and the grid utility to the global stylesheet.

**Files:**
- Modify: `static/styles.css` — append a new responsive section at the end of the file.

**Interfaces:**
- Consumes: `data-label` attributes on `<td>` (stamped by Task 3's JS).
- Produces: the `.rwd-card` (on `<table>`) and `.rwd-cards` (on a container) class contracts used by all later tasks and waves.

- [ ] **Step 1: Append to `static/styles.css`:**

```css
/* ═══════════ Responsive: tables → cards, grids → single column ═══════════ */
.rwd-cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 16px;
}

@media (max-width: 768px) {
  .rwd-cards { grid-template-columns: 1fr; }

  table.rwd-card { display: block; width: 100%; }
  table.rwd-card thead {
    position: absolute; width: 1px; height: 1px;
    padding: 0; margin: -1px; overflow: hidden;
    clip: rect(0 0 0 0); white-space: nowrap; border: 0;
  }
  table.rwd-card tbody { display: block; }
  table.rwd-card tr {
    display: block;
    margin-bottom: 12px;
    padding: 4px 2px;
    border: 1px solid var(--tb-border);
    border-radius: 14px;
    background: var(--tb-surface);
    box-shadow: 0 1px 2px rgba(16,24,40,.05);
  }
  table.rwd-card td {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    text-align: right;
    white-space: normal !important;
    padding: 9px 14px !important;
    border: 0 !important;
    border-bottom: 1px solid var(--tb-border) !important;
  }
  table.rwd-card tr td:last-child { border-bottom: 0 !important; }
  table.rwd-card td::before {
    content: attr(data-label);
    flex: 0 0 auto;
    margin-right: auto;
    text-align: left;
    font-size: .74rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: .03em;
    color: var(--tb-text-muted);
  }
  /* action / checkbox columns (empty header) — full width, no label */
  table.rwd-card td[data-label=""],
  table.rwd-card td:not([data-label]) { justify-content: flex-start; }
  table.rwd-card td[data-label=""]::before,
  table.rwd-card td:not([data-label])::before { content: none; }
  /* colspan cells (empty-state / group rows) — centered, no label */
  table.rwd-card td[colspan] { justify-content: center; text-align: center; }
  table.rwd-card td[colspan]::before { content: none; }
}
```

- [ ] **Step 2: Verify it parses.** Reload any page and confirm no CSS errors in the console and the desktop tables are unchanged (these rules only bite ≤768px and via `.rwd-card`, which no table has yet). Screenshot a desktop table page.

- [ ] **Checkpoint:** CSS added, desktop unaffected. (Card rendering is proven in Task 5 once a table opts in and Task 3's labels exist.)

---

### Task 3: `.rwd-card` auto-labeler JS

Stamp `data-label` on every `<td>` from its column's `<thead>` header, so tables convert to cards with just a class — no per-cell markup.

**Files:**
- Modify: `static/uzum_ui.js` — add `stampRwdLabels` at IIFE top level (near the other helpers, before `initUzumUI`), expose `window.rwdCards`, and call it at the end of `initUzumUI`.

**Interfaces:**
- Consumes: any `table.rwd-card` with a `<thead><th>` row.
- Produces: `window.rwdCards.refresh(rootEl?)` — idempotent; pages that re-render a `.rwd-card` table call this afterward.

- [ ] **Step 1: Add the helper** near the top of the IIFE in `static/uzum_ui.js` (after the existing top-level helpers, before `function initUzumUI()`):

```javascript
  // ── Responsive tables: stamp data-label on <td> from <thead> headers ──
  function stampRwdLabels(root) {
    const scope = root || document;
    scope.querySelectorAll("table.rwd-card").forEach((table) => {
      const headCells = table.querySelectorAll("thead th");
      if (!headCells.length) return;
      const labels = Array.from(headCells).map((th) => (th.textContent || "").trim());
      table.querySelectorAll("tbody tr").forEach((tr) => {
        let col = 0;
        Array.from(tr.children).forEach((td) => {
          if (td.tagName !== "TD") return;
          if (td.hasAttribute("colspan")) {
            col += parseInt(td.getAttribute("colspan"), 10) || 1;
            return;
          }
          if (!td.hasAttribute("data-label")) {
            td.setAttribute("data-label", labels[col] || "");
          }
          col += 1;
        });
      });
    });
  }
  window.rwdCards = { refresh: stampRwdLabels };
```

- [ ] **Step 2: Call it at the end of `initUzumUI`** (last line inside the function, before its closing brace):

```javascript
    stampRwdLabels();
```

- [ ] **Step 3: Bump cache-bust** if not already done in Task 1 Step 3 (base.html `uzum_ui.js?v=20260712a`).

- [ ] **Step 4: Verify the helper runs.** Load `/admin/users`, open the console, run `document.querySelector('table')?.classList` (no `.rwd-card` yet, so labels won't stamp — expected). Then temporarily run in console: `document.querySelector('table').classList.add('rwd-card'); rwdCards.refresh();` and inspect a `<td>` — it should now have a `data-label` matching its column header. Screenshot the inspected element / DOM.

- [ ] **Checkpoint:** `window.rwdCards.refresh()` exists and stamps labels correctly. (Real page opt-in happens in Task 5.)

---

### Task 4: Responsive topbar, container padding, touch sizing

Make the topbar fit narrow screens and enlarge tap targets.

**Files:**
- Modify: `static/styles.css` — append another `@media` block after the Task 2 section.

**Interfaces:**
- Consumes: existing `.pp-topbar`, `.pp-tb-avatar-trigger`, `.pp-tb-plan`, `.pp-tb-ghost`, `.pp-tb-dropdown` classes.
- Produces: no new classes.

- [ ] **Step 1: Append to `static/styles.css`:**

```css
/* ═══════════ Responsive: topbar, spacing, touch targets ═══════════ */
@media (max-width: 768px) {
  .pp-topbar { padding: 0 12px; gap: 6px; }
  /* hide the username label, keep the avatar circle + chevron */
  .pp-tb-avatar-trigger > span:not(.pp-tb-avatar) { display: none; }
  .pp-tb-plan { font-size: 12px; padding: 0 10px; }
  .pp-tb-dropdown { max-width: calc(100vw - 24px); }

  main.container-fluid { padding-left: 12px !important; padding-right: 12px !important; }

  /* touch sizing */
  .btn, .form-control, .form-select { min-height: 42px; }
  .pp-tb-ghost { min-width: 42px; height: 42px; }
}
@media (max-width: 480px) {
  /* drop the plan pill on very small screens to save room */
  .pp-tb-plan { display: none; }
}
```

- [ ] **Step 2: Verify topbar at 375px.** Load a base page. Confirm: hamburger + theme + lang + bell + avatar (no username text) all fit on one row with no overflow/wrap; avatar dropdown opens within the viewport. Screenshot.

- [ ] **Step 3: Verify at 340px and 480px.** At ≤480px the plan pill is gone; everything still fits at 340px. Screenshot.

- [ ] **Step 4: Verify desktop unchanged** at 1280px: username visible, plan pill visible, normal padding. Screenshot.

- [ ] **Checkpoint:** Topbar responsive, tap targets ≥42px, desktop intact.

---

### Task 5: Proof — server table page (`admin_users.html`)

Prove the whole stack on a clean Bootstrap table.

**Files:**
- Modify: `templates/admin_users.html` — add the `rwd-card` class to its main `.table` (line ~11: `<table class="table table-hover align-middle">`).

**Interfaces:**
- Consumes: Tasks 2 + 3 (`.rwd-card` CSS + auto-labeler).
- Produces: nothing; validation only.

- [ ] **Step 1: Add the class.** Change `templates/admin_users.html:11` from `<table class="table table-hover align-middle">` to `<table class="table table-hover align-middle rwd-card">`.

- [ ] **Step 2: Verify cards at 375px.** Load `/admin/users` at 375px (log in as admin first). Confirm: each user row is a bordered card; each value has its column label (ID / Login / Role / Shops / Actions) on the left; the header row is visually hidden; the Actions cell (empty header) spans full width without a stray label; no horizontal page scroll. Screenshot.

- [ ] **Step 3: Verify the action controls** (buttons/links in the Actions cell, and the shop checkboxes) are tappable (≥40px) and usable inside the card. Screenshot.

- [ ] **Step 4: Verify desktop unchanged** at 1280px: normal table with sticky header, no card styling. Screenshot.

- [ ] **Step 5: Verify dark mode** at 375px: cards use dark surfaces/borders, labels legible. Screenshot.

- [ ] **Checkpoint:** A real server-rendered table converts to labeled cards with one class + zero markup churn. This validates the mechanism for all later server tables (admin_subscriptions, admin_autoslot, invoice_restock, fbs tables, …).

---

### Task 6: Proof — form page (`settings_api_key.html`)

Prove forms stack, inputs/buttons go full-width and tappable, and the page has no overflow.

**Files:**
- Modify: `templates/settings_api_key.html` — only if the verification finds overflow or non-tappable controls; otherwise no change (the global rules from Task 4 may already cover it). Any change is page-local (e.g. adding `w-100` to submit buttons, or a small `@media` in the page's own style block if it has one).

**Interfaces:**
- Consumes: Task 4 (touch sizing) + Task 1 (drawer).
- Produces: nothing; validation only.

- [ ] **Step 1: Verify at 375px.** Load `/settings/api-key` at 375px. Confirm: the `.col-12 col-lg-7` column is full width; the two card forms stack; inputs are full-width and ≥42px tall; the "Save" buttons are comfortably tappable; the top row (`h4` title + "Back" button) does not overflow — if it does, note it. Screenshot.

- [ ] **Step 2: Apply page-local fixes only if needed.** If the header row overflows, wrap it to allow the button to drop below the title on phones (e.g. add `flex-wrap gap-2` to the `d-flex` at line ~5). If the save buttons feel cramped, add `w-100` / stack the `d-flex gap-2` button row at line ~52 on phones. Make the smallest change that fixes the observed problem; re-screenshot.

- [ ] **Step 3: Verify the drawer + topbar** work on this page too (open drawer, navigate, close). Screenshot.

- [ ] **Step 4: Verify desktop unchanged** at 1280px. Screenshot.

- [ ] **Step 5: Verify dark mode** at 375px. Screenshot.

- [ ] **Checkpoint:** Forms are usable on phones; the global touch/spacing rules hold. Wave 1 complete.

---

## Wave 1 exit review

- [ ] Both proof pages: no horizontal page scroll at 375px and 340px.
- [ ] Sidebar hidden by default on phones; hamburger opens it as a drawer over content; backdrop + nav-link + Escape all close it.
- [ ] `admin_users` table renders as labeled cards; action/empty-header cells handled correctly.
- [ ] Topbar fits (username hidden, plan pill dropped ≤480px), avatar dropdown works.
- [ ] Tap targets ≥42px on phones.
- [ ] Dark mode parity on every checked screen.
- [ ] Desktop (≥1024px) visually identical to before on both pages.
- [ ] Reusable primitives confirmed for later waves: mobile drawer (automatic on every uzum_ui.js page), `.rwd-card` + `rwdCards.refresh()`, `.rwd-cards`, global topbar/touch rules.
