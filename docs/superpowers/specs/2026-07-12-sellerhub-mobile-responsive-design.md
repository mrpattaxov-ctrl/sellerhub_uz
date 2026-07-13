# Sellerhub_uz — Mobile-Friendly Rework (Design)

**Date:** 2026-07-12
**Scope:** `sellerhub_uz` app only (not Outside Analytics)
**Depth:** Full rework — data tables become stacked card/list views on phones; touch-sized controls; responsive topbar.
**Strategy:** Build shared mobile machinery once, validate on representative pages (Wave 1), then convert the rest in priority waves.
**Verification:** User runs the Flask app locally; agent drives a mobile-width browser and screenshots each page.

---

## Problem

The app is a Bootstrap 5.3 Flask dashboard (~35 templates) with a sticky topbar and a **fixed 220px left sidebar** holding all primary navigation. The sidebar is **injected by `static/uzum_ui.js`** (`initUzumUI`), not present in template markup, and it forces `body { margin-left: 220px !important }` (56px when collapsed via the `sidebar-closed` body class). Responsiveness is **inconsistent**:

- **The fixed sidebar is the central mobile blocker:** on a 375px phone, `margin-left: 220px` leaves ~155px for content; even collapsed (56px) it is cramped and wastes width.
- The global stylesheet `static/styles.css` has **zero `@media` queries**.
- Some pages ship their own ad-hoc mobile rules (e.g. `sales.html` has a phone card-grid at 760px); most do not.
- Data grids are a **mix** of server-rendered `<table>` markup and JS-built DOM.
- The topbar shows a full username + plan pill that can overflow narrow screens.
- Many templates are large (fbs_orders 4455 lines, fbs_invoices 4057, group_detail 2132, postavki 1978) with inline `<style>`/`<script>` per page.

On a phone this means the sidebar eats most of the width, horizontal page scroll, cramped tables, and tap targets that are too small.

## Goals

1. No horizontal **page** scroll on any converted page down to ~340px width.
2. Wide data tables render as **stacked cards** (one card per row, label beside each value) below the phone breakpoint.
3. Topbar, forms, modals, and action bars adapt and are touch-sized (min 40–44px targets).
4. A **single reusable mechanism** so each table converts with ~one class, not bespoke markup.
5. Dark mode parity preserved (all new CSS uses existing `--tb-*` tokens).

## Non-Goals

- No visual redesign of the desktop layout — desktop stays as-is above the breakpoint.
- No framework change (stay on Bootstrap 5.3 + the existing `.pp-*` / `--tb-*` design system).
- Print templates (`print_labels`, `fbs_qr_print`, `print_queue` printout) keep their print/paper sizing; only their **trigger UI** becomes tappable.
- Outside Analytics app is out of scope.

---

## Architecture — the shared foundation

Three shared files are loaded on nearly every page and are the home for the foundation:

| File | Role | What we add |
|---|---|---|
| `static/uzum_ui.js` | global JS: injects sidebar + layout CSS, loaded broadly | **mobile off-canvas drawer** (CSS + toggle + backdrop + hamburger), `data-label` auto-stamper for tables |
| `static/styles.css` | global CSS (loaded on base + standalone pages) | breakpoint tokens, container padding, responsive topbar rules, `.rwd-card` table mode, `.rwd-cards` grid utility, touch sizing |
| `templates/_topbar.html` | shared header markup | (hamburger is injected by JS to stay co-located with drawer logic) |

### 0. Sidebar → mobile off-canvas drawer (highest priority)

The fixed sidebar and its forced `body` margin are the single biggest mobile blocker, so this is fixed first. The sidebar's layout CSS is injected by `uzum_ui.js`, so the mobile behavior is added **in the same injected stylesheet + toggle logic** (one file owns the sidebar).

Below the phone breakpoint (≤ 768px):
- `body { margin-left: 0 !important }` (override both the 220px and 56px `!important` rules) — content is full width.
- The sidebar becomes an off-canvas drawer: `position: fixed; transform: translateX(-100%)`, sliding in **over** the content (not pushing it) when opened; **default closed** on phones.
- A **hamburger button** (injected into the topbar as its first child, shown only on phones) toggles the drawer open.
- A translucent **backdrop** covers the content while the drawer is open; tapping it, tapping a nav link, or pressing Escape closes the drawer.
- Widen the drawer back to a comfortable ~260–280px in mobile mode (the 56px collapsed width is desktop-only).
- On resize back to desktop, the drawer state resets so desktop layout is unaffected. The existing `uzum_sidebar_closed` localStorage (desktop collapse memory) must not leak into mobile drawer state.

### 1. Breakpoints & container

Define two breakpoints as documentation constants (Bootstrap-aligned so we don't fight the grid):

- **Phone:** `max-width: 768px` (primary — tables → cards, topbar collapse, forms stack)
- **Small phone:** `max-width: 480px` (secondary — tighter padding, hide non-essential chrome)

Reduce `main.container-fluid` / `.container` horizontal padding on phones (e.g. `px-3` → ~12px) and ensure `body { overflow-x: hidden }` is **not** used as a crutch — the fix is that nothing overflows.

### 2. `.rwd-card` — the table-to-cards mechanism

The core reusable primitive. Applying `class="table rwd-card"` to any `<table>` makes it:

- **Desktop (> 768px):** render exactly as today (no change).
- **Phone (≤ 768px):** each `<tr>` becomes a bordered card; each `<td>` becomes a row inside the card with its **column label on the left** and the value on the right.

**How labels appear without editing every cell:** a JS helper in `uzum_ui.js` runs on load, finds every `table.rwd-card`, reads the `<thead> th` text, and stamps `data-label="<header>"` on each body `<td>` by column index. CSS then renders `td::before { content: attr(data-label) }` on phones. Result: **most server-rendered tables convert with just the class** — no markup churn.

The helper must also re-run for tables inserted/re-rendered by page JS. Expose it as `window.rwdCards.refresh(rootEl?)` so pages that rebuild a table can call it after render. It is idempotent (skips cells already stamped unless forced).

**Edge cases the helper/CSS handle explicitly:**
- **Action/blank-header columns** (buttons, checkboxes): a `<th>` with empty text → the `<td>` gets no label and spans full width.
- **`colspan` cells** (e.g. "no data" rows, group headers): skip labeling, span full card width.
- **Sticky headers:** `thead` is visually hidden in card mode (labels come from `::before`), so the existing sticky-header rules only apply on desktop.
- Opt-out: a table without `.rwd-card` is untouched (used for tables that must stay tabular, or handled bespoke).

### 3. `.rwd-cards` — utility for JS-rendered grids

Pages that build their own grid DOM (sales, warehouse, groups, etc.) don't have `<table>` markup. For those, a CSS utility class turns a container into a responsive card list: multi-column on desktop, single-column stacked on phone. Pages opt in by adding the class to their grid container; where a page already has its own working media queries (like `sales.html`), we **leave them** and only patch what breaks.

### 4. Responsive topbar

In `styles.css`, below 768px:
- Username label hides (`.pp-tb-avatar-trigger span` with the name → `display:none`), leaving avatar + chevron.
- Plan pill: shrink font / allow it to be the first thing to drop below ~420px if space is tight (keep it if it fits).
- Reduce topbar horizontal padding (24px → 12px) and gaps.
- Ensure the language + notifications ghost buttons remain 40px+ tap targets.
- Verify the avatar dropdown (already JS-toggled, `position:absolute`) stays within the viewport (right-anchored, `max-width: calc(100vw - 24px)`).

### 5. Touch sizing

Global phone rules: form controls, `.btn`, `.pp-tb-ghost`, and dropdown items get a min height of ~40px and adequate spacing. Full-width primary buttons on phones where they currently sit inline.

---

## Wave 1 — Foundation + Proof (this spec's build target)

Deliverables:

1. **Mobile drawer** in `static/uzum_ui.js`: injected mobile CSS (body margin reset, off-canvas transform, backdrop, widened drawer), hamburger injected into the topbar, open/close + backdrop + close-on-nav-click + Escape + resize-reset logic.
2. **Foundation CSS** in `static/styles.css`: breakpoint rules, container padding, responsive topbar, `.rwd-card`, `.rwd-cards`, touch sizing — all with dark-mode parity.
3. **Auto-labeler JS** in `static/uzum_ui.js`: `window.rwdCards` with `refresh()`, auto-run inside `initUzumUI` / on `DOMContentLoaded`.
4. **Proof on 2 representative pages**, each a different rendering pattern (both also exercise the drawer + topbar):
   - **Server-rendered table:** `admin_users.html` — a clean Bootstrap `.table` (proper `<thead>`, simple cells). Add `.rwd-card`, verify auto-labels stack correctly. (Swapped from `expenses.html`, which is a colspan-heavy custom `.we-table` with its own responsive scheme — a hard edge case deferred to Wave 2 rather than a clean first proof.)
   - **Form page:** `settings_api_key.html` — verify stacking, full-width inputs/buttons, tap sizing.

**Exit criteria for Wave 1:** on both proof pages at 375px and 340px — no horizontal page scroll; the sidebar is hidden by default and opens as a drawer over content via the hamburger, with a working backdrop; the `.table.rwd-card` renders as labeled cards; topbar fits and its avatar dropdown works; dark mode intact — all confirmed by screenshots. Desktop (≥ 1024px) is visually unchanged from today.

## Later waves (outlined; each becomes its own spec/plan cycle)

- **Wave 2 — core daily seller flows:** fetch (dashboard), sales, fbs_orders, fbs_order_detail, fbs_stock, fbs_invoices, pos, postavki, groups, group_detail, invoice_restock.
- **Wave 3 — money & settings:** economics, expenses (beyond proof), calculator, subscription, subscription_expired, settings_notifications, change_password.
- **Wave 4 — admin, auth, print:** admin_autoslot, admin_users, admin_subscriptions, login, not_found; print pages get tappable trigger UI only.

Each later wave reuses the Wave 1 machinery: add `.rwd-card` / `.rwd-cards`, call `rwdCards.refresh()` after JS renders, patch page-specific overflow, screenshot-verify.

---

## Risks & mitigations

- **Complex tables** (colspans, nested controls, action columns) may not auto-label cleanly → the helper handles empty-header and colspan cells; genuinely irregular tables opt out of `.rwd-card` and get bespoke treatment (flagged per page in its wave).
- **Pages with existing media queries** (sales) → don't fight them; patch only what breaks.
- **Cache busting:** `styles.css` is referenced with mixed/absent `?v=` query strings across pages. Bump the version consistently (or note it) so the new CSS actually loads. Track as an implementation step.
- **JS-rebuilt tables** losing labels → any page that re-renders a `.rwd-card` table must call `rwdCards.refresh()`; audited per page.
- **Not a git repo:** design doc cannot be committed; kept as a file under `docs/superpowers/specs/`.

## Verification plan

Per page: user runs the Flask app; agent opens the page at 375px and 340px (and one desktop width to confirm no regression), screenshots, checks: no horizontal page scroll, tables are labeled cards, controls are tappable (≥40px), topbar + dropdown work, dark mode parity.
