# 📝 My Notes — How the App Works

> A plain-language map of three things I've studied:
> **1) Admin login · 2) Database structure & connections · 3) User registration + Telegram login**
>
> Every section links to the real function in the code. Use `Ctrl+F` with the 🔎 keywords.

---

## 🗂️ Quick Find (search these)

| Topic | 🔎 Search for | Jump to |
|---|---|---|
| Admin secret link | `ADMIN-SECRET` | [§1.1](#sec-1-1) |
| Admin password check | `ADMIN-CHECK` | [§1.3](#sec-1-3) |
| User table / columns | `DB-USER-TABLE` | [§2.1](#sec-2-1) |
| DB connection (SessionLocal/engine) | `DB-ENGINE` | [§2.2](#sec-2-2) |
| PgBouncer pooling | `DB-PGBOUNCER` | [§2.3](#sec-2-3) |
| Workers vs threads vs cores | `DB-CONCURRENCY` | [§2.4](#sec-2-4) |
| Telegram login (known user) | `TG-LOGIN` | [§3.2](#sec-3-2) |
| Telegram login (new user) | `TG-NEWUSER` | [§3.3](#sec-3-3) |
| The bot itself | `TG-BOT` | [§3.4](#sec-3-4) |
| The shared mailbox (TelegramPending) | `TG-MAILBOX` | [§3.5](#sec-3-5) |
| Subscription system | `SUB-ADMIN` | [§4](#subscriptions) |
| Recurring patterns | `PAT-` | [§5](#patterns) |
| Uzum API token / `/settings/api-key` | `TOK-` | [§6](#apikey) |
| Background loops & workers | `BG-` | [§7](#background) |
| My Shops — add & delete | `SHOP-` | [§8](#shops) |
| Fetch & data layer (how data comes in / read back) | `FETCH-` | [§9](#fetch) |
| Concurrency & locking (shop_lock, deadlocks) | `LOCK-` | [§10](#concurrency) |
| Infra — Docker stack & Redis | `INF-` | [§11](#infra) |
| Product save loop (fetch → DB) | `SAVE-` | [§12](#save) |

---

# 1. 🔐 Admin Login  <a id="admin-login"></a>

**Big idea:** admin logs in with **username + password**. Entry is hidden behind a **secret link** in the URL.

<a id="sec-1-1"></a>
### 1.1 The secret link — the "knock"  `ADMIN-SECRET`
- The secret comes from the env var **`ADMIN_SECRET_PATH`** → read at [app.py:224](app.py#L224).
  - If it's empty, each worker invents a *random* link (4 workers = 4 different links!). Set the env var so all workers agree.
- Route: [`admin_login`](auth/routes.py#L135) at [auth/routes.py:135](auth/routes.py#L135)
  - Checks the secret → if correct, sets a session flag `BACKSTAGE_LOGIN_SESSION_KEY = True` → **redirects** to `/backstage/login`.

### 1.2 The backstage login page  `ADMIN-BACKSTAGE`
- Route: [`backstage_login`](auth/routes.py#L107) at [auth/routes.py:107](auth/routes.py#L107)
  - If you didn't pass through the secret first → **404** (page is invisible to the public).
  - If you did → shows the login form via `_finish_admin_login(...)`.

<a id="sec-1-3"></a>
### 1.3 The credential check  `ADMIN-CHECK`
- Function: [`_finish_admin_login`](auth/routes.py#L72) at [auth/routes.py:72](auth/routes.py#L72)
- **GET** → shows the form ([admin_login.html](templates/admin_login.html)). **POST** → checks credentials.
- The 3 gates that must ALL pass ([auth/routes.py:81](auth/routes.py#L81)):
  1. user **exists** (looked up by username)
  2. **password matches** → `check_password_hash(user.password_hash, password)`
  3. **`is_admin` is True**
- On success → `login_user(user)` → you're in.

### 1.4 Where the admin account comes from  `ADMIN-BOOTSTRAP`
- Function: [`_bootstrap_default_admin`](app.py#L116) at [app.py:116](app.py#L116)
- On an **empty** database, creates `username="admin"`, password from `ADMIN_DEFAULT_PASSWORD` (default `"admin"`), `is_admin=True`.
- Password is **hashed** with `generate_password_hash` before saving — never stored in plain text.

**Admin flow in one line:** secret link → set session flag → `/backstage/login` → form → check (exists + password + is_admin) → `login_user`.

---

# 2. 🗄️ Database Structure & Connections  <a id="database"></a>

<a id="sec-2-1"></a>
### 2.1 Where data lives — the `users` table  `DB-USER-TABLE`
- Model: [`User`](models.py#L161) at [models.py:161](models.py#L161). It's a Python class that maps to the `users` table in **PostgreSQL**.
- Key columns for login:
  - `username` (unique) · `password_hash` (**hashed**, never plain) · `is_admin` · `telegram_id` · `phone` · `trial_started_at`
- Passwords are **one-way hashed**: `generate_password_hash` (write) ↔ `check_password_hash` (verify).

<a id="sec-2-2"></a>
### 2.2 How the app connects to the database  `DB-ENGINE`
- All wiring is in [extensions.py](extensions.py).
- **`DATABASE_URL`** (the address) comes from compose → env var → read in [config.py:19](config.py#L19).
  - Chain: [docker-compose.yml:114](docker-compose.yml#L114) → `os.getenv` in config → imported by extensions.
- **`engine`** = the connection pool, created **once** → [extensions.py:36](extensions.py#L36).
- **`SessionLocal`** = the session factory → [extensions.py:82](extensions.py#L82).
- **Usage pattern everywhere:**
  ```python
  with SessionLocal() as db:     # borrow a connection
      db.execute(select(User)...) # run SQL
  # block ends → connection returned to the pool
  ```
- Pool settings (per worker): `DB_POOL_SIZE` (20), `DB_MAX_OVERFLOW` (30), `DB_POOL_RECYCLE_SECONDS` → [config.py:23-25](config.py#L23).

<a id="sec-2-3"></a>
### 2.3 PgBouncer — the connection funnel  `DB-PGBOUNCER`
- **Why:** real Postgres connections are expensive & capped. PgBouncer lets **many cheap app connections** share **few real DB connections**.
- The app points at **PgBouncer**, not Postgres directly: `DATABASE_URL → pgbouncer:6432` ([docker-compose.yml:114](docker-compose.yml#L114)).
- PgBouncer config ([docker-compose.yml:49-92](docker-compose.yml#L49)):
  - `MAX_CLIENT_CONN: 2000` = waiting room (cheap app connections)
  - `DEFAULT_POOL_SIZE: 25` = real Postgres connections (the funnel output)
  - `POOL_MODE: transaction` = a real connection is lent **only for one transaction**, then reused.
- **2000 → 25 works** because DB work is brief & most connections are *waiting*, not querying. Time-sharing.
- Trade-off: no prepared statements / LISTEN — handled in [extensions.py:64-79](extensions.py#L64).

**The two stacked pools:**
```
app worker (DB_POOL_SIZE=20)  →  PgBouncer (25 real)  →  Postgres (max 500)
```

<a id="sec-2-4"></a>
### 2.4 Workers, threads & cores — how many users at once  `DB-CONCURRENCY`
- gunicorn: **4 workers × 16 threads = 64** requests in flight → [docker-compose.yml:130-133](docker-compose.yml#L130).
- **Worker** = separate process (own memory, real parallelism across CPU cores).
- **Thread** = lane inside a worker (concurrent, time-sliced; great for *waiting* on Uzum/DB).
- **My PC:** 4 cores / 8 logical processors → only **8 things truly run at once**.
- Key insight — three meanings of "at once":
  - **64** = requests *juggled* (concurrency) — most are *waiting*, using no CPU.
  - **8** = can *execute* at the same instant (parallelism).
  - **~25/30** = transactions that can hit Postgres at once (PgBouncer cap).
- A request only needs a CPU while **computing**; while **waiting on Uzum/DB** it uses no local CPU (Postgres may use a core for *its* query).
- Threads cap concurrency; a bigger DB pool past thread count = wasted. Scale **threads + pool together**.

**Chain of limits (smallest wins):** `threads (64) → app pool (80) → PgBouncer (25) → Postgres (500)`.

---

# 3. 📱 User Registration + Telegram Login  <a id="telegram"></a>

**Big idea:** regular users have **no password**. They log in with **phone + a Telegram tap** (passwordless).

### 3.1 The login page  `TG-PAGE`
- Route: [`login`](auth/routes.py#L100) at [auth/routes.py:100](auth/routes.py#L100) → renders [login.html](templates/login.html) (GET only).
- The page's JavaScript drives everything via background `fetch()` calls (no password field).

<a id="sec-3-2"></a>
### 3.2 Login flow — known user (approval)  `TG-LOGIN`
1. **Browser** sends phone → `POST /api/telegram/send-approval` ([login.html:196](templates/login.html#L196)).
2. **Server** [`api_tg_send_approval`](telegram/routes.py#L86) at [telegram/routes.py:86](telegram/routes.py#L86):
   - finds the user by **phone**, creates a **token**, sends a Telegram message with ✅/❌ buttons.
3. **Browser** polls `GET /api/telegram/check-approval/<token>` every 2s ([login.html:240](templates/login.html#L240)).
4. **User taps ✅** in Telegram → bot handler [`handle_approval_callback`](app.py#L1247) at [app.py:1247](app.py#L1247) → calls `_tg_confirm(token)`.
5. **The confirmation write:** [`_tg_confirm`](app.py#L696) at [app.py:696](app.py#L696) sets `confirmed = True` + `db.commit()`.
6. **Next poll** [`api_tg_check_approval`](telegram/routes.py#L148) at [telegram/routes.py:148](telegram/routes.py#L148) sees confirmed → `login_user` → returns redirect → browser navigates in.

<a id="sec-3-3"></a>
### 3.3 Registration flow — new / unknown user  `TG-NEWUSER`
- Detection: phone not found OR no `telegram_id` → "not linked" branch ([telegram/routes.py:104](telegram/routes.py#L104)).
  - No message sent (can't message an unknown user). Instead returns `not_linked: true` + bot link.
- Browser shows "open the bot" step ([`showBotStep`](templates/login.html#L227)) and keeps polling.
- User opens bot & **shares contact** → bot handler [`handle_contact`](app.py#L746) at [app.py:746](app.py#L746):
  - **Write A — create/link user:** make a `User` with `telegram_id`, `phone`, random password hash, `is_admin=False`, and **start a trial** (`_ensure_user_trial_started`) → `db.commit()` ([app.py:777-790](app.py#L777)).
  - **Write B — confirm token:** find the pending `contact_link` row (matched by phone digits) → `confirmed=True` ([app.py:792-804](app.py#L792)).
- Browser's poll sees confirmed → `login_user` → redirect. Same finish as known users.
- Trial granter: [`_ensure_user_trial_started`](core/subscriptions.py#L220) at [core/subscriptions.py:220](core/subscriptions.py#L220) (sets `trial_started_at = now`).

<a id="sec-3-4"></a>
### 3.4 The bot — where it connects to Telegram  `TG-BOT`
- Started in [`_start_tg_bot`](app.py#L713) at [app.py:713](app.py#L713), runs in **one** worker's background thread.
- Connects via **long-polling** (`bot.infinity_polling`, [app.py:1283](app.py#L1283)) — the app reaches *out* to Telegram; no public webhook needed.
- Token from **`TELEGRAM_BOT_TOKEN`** env var → [`_tg_config`](app.py#L639) at [app.py:639](app.py#L639) (file fallback `telegram_config.json` is unused in my setup).
- **Who talks to Telegram:** `app.py` bot **receives** messages/taps · `routes.py` **sends** one message · `login.html` talks only to Flask.

<a id="sec-3-5"></a>
### 3.5 The glue — `TelegramPending` table  `TG-MAILBOX`
- The browser-side (routes.py) and bot-side (app.py) **never call each other** — they coordinate through a DB table (`TelegramPending`).
- Helper functions in app.py: [`_tg_set`](app.py#L677) (create), [`_tg_get`](app.py#L665) (read), [`_tg_confirm`](app.py#L696) (mark confirmed), [`_tg_delete`](app.py#L707) (remove).
- **Why a DB table (not memory)?** With 4 workers, the bot (worker 8) and the browser's request (worker 9) can't share memory — Postgres is the one place they both see.
- **Expiry:** pending entries are cleaned after **5 minutes** by [`_tg_clean_expired`](app.py#L658) at [app.py:658](app.py#L658) (lazy — runs at the top of login endpoints).

**Telegram login in one line:** browser sends phone → server messages the user (or shows bot link) → user taps/shares contact → bot flips `confirmed=True` in DB → browser's poll sees it → `login_user` → redirect.

---

## 🔑 Cross-cutting things I learned

- **Routing:** a `@route` decorator registers `URL + method → function` in a lookup table; `register_blueprint` ([app.py:580+](app.py#L580)) copies all routes into the app's master table at startup. A request is matched against that table.
- **Translations:** `{{ t.key }}` in templates = translated text from [translations.py](translations.py), injected by the context processor [app.py:355](app.py#L355). Edit **both** `ru` and `uz`.
- **Page links vs API links:** `GET /login` returns an **HTML page** (for humans); `POST /api/...` returns **JSON** (for JavaScript, in the background).
- **Subscription gate:** [`_enforce_active_subscription`](app.py#L276) at [app.py:276](app.py#L276) runs on every request — Redis flags → signed-session fast path → Postgres slow path; allow if `effective_end_at > now`.
- **Both logins converge on `login_user(user)`** — admin proves identity by password, users by Telegram ownership.

---

# 4. 💳 Subscription System (admin + user)  <a id="subscriptions"></a>

> 🔎 Search keywords: `SUB-ADMIN`, `SUB-DATA`, `SUB-USER`, `SUB-LINK`, `SUB-PRICE`, `SUB-BUG`

## 4.1 The admin subscription page — what admin can do  `SUB-ADMIN`
Route: [`admin_subscriptions_page`](admin/routes.py#L592) at [admin/routes.py:592](admin/routes.py#L592) (admin-only). It's a **form-POST page** (not fetch/JSON). One hidden field `action` decides which of 6 jobs runs.

| Admin action | DB effect | Web effect |
|---|---|---|
| **Change settings** (trial days, price, shop limit) | update `subscription_settings` row + clear cache | flash + form shows new values; users feel new trial/price |
| **Create code** | insert `subscription_codes` row | code appears in list; user can redeem it |
| **Edit code** | update code + recompute affected users | code shows new duration |
| **Delete code** | delete code + activations + recompute users | code disappears |
| **Grant user a period** | set `subscription_expires_at` + `unrevoke_user` (Redis) | user unblocked instantly |
| **Cancel user** | null `subscription_expires_at` + revoke logic (Redis) | user blocked (if no trial left) |

- POST part = **do** the action, then `redirect`. GET part = **read everything + render** ([admin/routes.py:767](admin/routes.py#L767)).
- Every action ends with `redirect` → browser does a fresh GET → the GET part re-reads the DB → page shows the update. (See pattern `PAT-PRG`.)

## 4.2 The subscription data model  `SUB-DATA`
3 tables + 3 columns on `users`:
- [`SubscriptionSettings`](models.py#L198) — **one** global row (trial_days=7, monthly_price=100000, max_shops=5).
- [`SubscriptionCode`](models.py#L228) — one row per redeemable code.
- [`SubscriptionCodeActivation`](models.py#L262) — junction table "who used which code" (`code_id=NULL` = manual admin grant).
- On `users`: `trial_started_at`, `subscription_expires_at`, `subscription_is_unlimited` = the **live truth** the gate reads.

**Helpers** (in [core/subscriptions.py](core/subscriptions.py)): [`_admin_set_user_subscription`](core/subscriptions.py#L464) (grant), [`_admin_clear_user_subscription`](core/subscriptions.py#L505) (cancel), [`_generate_subscription_code`](core/subscriptions.py#L516) (random code), [`_recalculate_subscription_for_user`](core/subscriptions.py#L381) (keep users in sync with codes).

## 4.3 The user's own subscription page  `SUB-USER`
Route: [`subscription_page`](auth/routes.py#L340) at [auth/routes.py:340](auth/routes.py#L340).
- **GET** = reads current settings + computes status via [`_subscription_status_for_user`](core/subscriptions.py#L228) → renders [subscription.html](templates/subscription.html).
- **POST** = user redeems a code → [`_activate_subscription_code`](core/subscriptions.py#L525) → extends their access.
- Blocked users land on [`subscription_expired_page`](auth/routes.py#L399).

## 4.4 How admin & user pages are interconnected  `SUB-LINK`
- They **never call each other**. They share the **database + Redis**. Admin **writes**, user side **reads** — through the **same function** `_subscription_status_for_user`.
- **Trial is COMPUTED, not stored:** `trial_end = trial_started_at + settings.trial_days` ([core/subscriptions.py:270](core/subscriptions.py#L270)). So changing the one global `trial_days` instantly shifts every user's trial end — no per-user update. (See pattern `PAT-STORE-COMPUTE`.)

## 4.5 Pricing — computed from ONE value  `SUB-PRICE`
- Admin sets only `monthly_price_sum`. The 1/3/6/12-month plans are **hardcoded** in [`SUBSCRIPTION_PLAN_OPTIONS`](core/subscriptions.py#L110) (months + discount %).
- [`_subscription_plan_rows`](core/subscriptions.py#L176) computes each price = `monthly × months × (1 − discount)`. Both pages call this → identical prices.
- `max_shops_per_user` = a stored value shown as-is (not computed); enforced when a user adds a shop.

## 4.6 Code activation vs admin grant — the difference  `SUB-PRICE`
- **Code redeem** ([core/subscriptions.py:555](core/subscriptions.py#L555)): starts from `max(now, effective_end_at)` → **stacks on top** of remaining trial ("give me 30 *more* days").
- **Admin grant** ([core/subscriptions.py:485](core/subscriptions.py#L485)): starts from `now` → flat 30 days, trial absorbed ("set access to 30 days from today"). Intentional difference.

## 4.7 The redirect-loop bug we found & fixed  `SUB-BUG`
**Symptom:** after admin cancels, user gets `ERR_TOO_MANY_REDIRECTS` on `/economics`.
**Cause:** two checks disagreed — the **gate** trusted the Redis revoke flag (blocked) while the **expired page** trusted the DB (trial still active → bounced back). Ping-pong.
**Fixes applied:**
1. **Expired page respects the revoke flag** ([auth/routes.py:414](auth/routes.py#L414)): `if status["active"] and not is_user_revoked(user_id):` — a revoked user stays put (no loop).
2. **Cancel keeps the trial** ([admin/routes.py:758-770](admin/routes.py#L758)): after clearing the paid sub, recompute status — if still active (trial), `unrevoke + mark_user_for_recheck` (keep access); only `revoke_user` if truly expired.
**Lesson:** redirect loops = two checks using different data sources that disagree → make them agree. (See pattern `PAT-TWO-TRUTHS`.)

---

# 5. ♻️ Recurring Patterns (the ones that repeat everywhere)  <a id="patterns"></a>

> These show up again and again. Learn these and the codebase gets easy.

### `PAT-STORE-COMPUTE` — Store fixed values, compute derived ones
- **Store** a value when it's fixed/independent (e.g. `subscription_expires_at`, `monthly_price_sum`).
- **Compute** a value when it depends on something that can change (e.g. `trial_end = trial_started + trial_days`; plan prices = monthly × months × discount).
- Why: computed values update automatically when their inputs change — no stale data, no mass updates.

### `PAT-GET-POST` — GET reads, POST writes
- **GET** = "show me" → route **reads** DB (`select`) and renders. **POST** = "do/save" → route **writes** DB (`commit`).
- HTTP method (GET/POST) ≠ DB op (read/commit) — but they usually pair up like this.

### `PAT-PRG` — Post / Redirect / Get
- A save action does: **POST → `db.commit()` → `redirect` → browser GET → re-read DB → show result**.
- The POST and the reload are **two separate requests**; the **database is the bridge** (not memory).
- Why the redirect: refreshing the page won't re-submit the form.

### `PAT-COMMIT-OWNER` — Who opens the session commits
- Whoever opens `with SessionLocal() as db:` **owns the transaction** and calls `db.commit()`.
- **Action helpers** (part of a bigger op, e.g. [`_admin_set_user_subscription`](core/subscriptions.py#L464)) do `db.add` but **don't commit** — the caller commits once (all-or-nothing).
- **Get-or-create / ensure-exists helpers** (e.g. [`_get_or_create_subscription_settings`](core/subscriptions.py#L135)) **do commit** — self-contained setup that should be durable immediately.

### `PAT-DB-PARAM` — The `db` parameter IS the connection
- A helper doesn't open its own DB connection — it **receives** the session as the `db` argument from the route.
- **Has a `db` param** → it can query/commit (uses the route's session). **No `db` param, only objects** (`settings`, `user`) → pure compute, can't touch the DB.

### `PAT-IDENTIFY` — Tell what a function touches
- **Redis** → uses `swr_get` / `swr_invalidate` / `redis_client.` / `mark_user_for_recheck` / cache-key constants.
- **Database** → has a `db` param + `db.get/execute/add/commit`.
- **Cookie** → uses `session_obj`.
- **Pure compute** → only objects in, math/format out.

### `PAT-CACHE-INVALIDATE` — Write DB, then clear the cache
- Hot data is cached in **Redis** for speed (e.g. settings, 30-min TTL).
- After changing it, **delete the stale cache** so the next read refreshes from the DB: [`_invalidate_settings_cache`](core/subscriptions.py#L40) is called right after the settings `db.commit()` ([admin/routes.py:618](admin/routes.py#L618)).
- "Invalidate" = **delete** the cached copy (not re-cache). Re-caching happens on the next read.
- ⚠️ The settings cache lives in **Redis (server)**, NOT the cookie. The cookie is a *separate* per-user cache the gate uses.

### `PAT-TWO-TRUTHS` — Two checks must agree
- When two places decide "is this allowed?" from **different data** (e.g. gate=Redis flag, page=DB status), they can **disagree** → bugs like redirect loops.
- Fix: make them read consistent data / honor the same flags.

### `PAT-RENDER` — Server-side rendering (Jinja)
- Route passes Python data → template fills it in → finished HTML to browser.
- Three tools: `{{ value }}` = print · `{% for x in list %}` = repeat (lists → table rows) · `{% if %}` = choose what to show.
- Each generated row can carry a **form with that row's id** → its button posts the right action back.

### `PAT-SHARED-DATA` — Pages connect through shared storage, not each other
- Separate pages/processes (admin↔user, browser↔bot) **don't call each other**. They coordinate through the **database (or Redis)**.
- One side **writes**, the other **reads** — often via the **same function** so results always agree.

---

# 6. 🔑 Uzum API Token — `/settings/api-key`  <a id="apikey"></a>

> 🔎 Search keywords: `TOK-PAGE`, `TOK-LOGIN`, `TOK-READ`, `TOK-EXPIRY`, `TOK-SCHED`, `TOK-TWO`

**Big idea:** every Uzum API call needs a **JWT bearer token**. There is **ONE shared admin token** for the whole platform, kept fresh automatically.

## 6.1 The page  `TOK-PAGE`
- Route: [`settings_api_key`](auth/routes.py#L237) at [auth/routes.py:237](auth/routes.py#L237) — **admin-only**, **form-POST** (like the subscription page).
- Two save paths (two `<form>`s, same route):
  1. **Auto-login** — save `uzum_phone` + `uzum_password` → app logs in for you forever.
  2. **Manual token** — paste a raw bearer token directly into `api_key`.
- Each field saved **only if non-empty** (partial update). Saving creds spawns a one-shot `_uzum_auto_login` thread ([auth/routes.py:206](auth/routes.py#L206)).

## 6.2 How it GETS the token — OAuth2 password grant  `TOK-LOGIN`
- Function: [`_uzum_auto_login`](core/auth_helpers.py#L62) at [core/auth_helpers.py:62](core/auth_helpers.py#L62).
- `POST https://api-seller.uzum.uz/api/oauth/token` with `grant_type=password` + phone/password, plus a **Basic** auth header (`b2b-front:clientSecret` = the cabinet's public client id).
- Uzum returns `access_token` → prepends `"Bearer "` → saves to **`admin.api_key`**. The manual paste path writes the **same column** — both converge there.

## 6.3 How it's READ — one shared admin token  `TOK-READ`
- [`_get_admin_token`](core/auth_helpers.py#L55) at [core/auth_helpers.py:55](core/auth_helpers.py#L55) → returns the **admin** user's `api_key`. **All** Uzum calls use this one token. (That's why the page is admin-only and the expiry banner is global.)

## 6.4 Expiry — read from the JWT itself  `TOK-EXPIRY`
- [`_jwt_expires_in_seconds`](core/auth_helpers.py#L143) at [core/auth_helpers.py:143](core/auth_helpers.py#L143): base64-decode the JWT's middle part (payload), read the `exp` claim, return `exp − now`. **Signature is NOT verified** (we only want to read expiry, not trust it).
- Status API [`api_user_api_key_status`](auth/routes.py#L450) at [auth/routes.py:450](auth/routes.py#L450) → drives the red banner on every page ([base.html:170](templates/base.html#L170)).
- ⚠️ `expires_in` (in `_uzum_auto_login`) comes from **Uzum's response**, NOT the DB — it's only logged, never stored. Expiry is re-derived from the token's `exp` later.

## 6.5 The 90-minute refresh  `TOK-SCHED`
- [`_start_auto_login_scheduler`](app.py#L386) at [app.py:386](app.py#L386) — APScheduler `add_job(_uzum_auto_login, "interval", minutes=90)` + an immediate login at boot.
- **Started by** [background/startup.py:68](background/startup.py#L68) (inside gunicorn — the live path). See §7.

## 6.6 Two different tokens — don't confuse them  `TOK-TWO`
- **Admin cabinet token** = `users.api_key` (from `_uzum_auto_login`). Used by legacy/cabinet calls + cost-price `/sku-list`.
- **Per-user OpenAPI token** = `users.uzum_openapi_token` (pasted in My Shops). Used by products + finance fetches. **This is the one the data loops use.**

**Token in one line:** admin saves phone+password → `_uzum_auto_login` OAuth → `admin.api_key` → every call reads `_get_admin_token` → a 90-min scheduler keeps it fresh → banner watches the JWT `exp`.

---

# 7. ⚙️ Background Runtime — loops & workers  <a id="background"></a>

> 🔎 Search keywords: `BG-START`, `BG-ORDER`, `BG-LOOPVS1SHOT`, `BG-RATE`, `BG-WORKERPY`

## 7.1 What starts the loops  `BG-START`
- [app.py:4401](app.py#L4401) calls `start_background_threads()` → [background/startup.py:18](background/startup.py#L18).
- A **file lock** ([startup.py:32](background/startup.py#L32)) means **only ONE** gunicorn worker runs the loops (prevents 4 duplicate bots). That worker **still serves web too** — the lock only de-duplicates, it doesn't isolate.

## 7.2 Startup order  `BG-ORDER`
- Launched in order: **Telegram bot → finance loops → products sync → auto-login scheduler** ([startup.py:49-68](background/startup.py#L49)).
- But all are `Thread().start()` → they run **concurrently**. Order = which launches first, not a sequence. Finance loops **sleep until a clock time** before first work.

## 7.3 Loops vs one-shot threads  `BG-LOOPVS1SHOT`
- **Persistent loops** (`while True`) come **only** from `startup.py`.
- **Pages start one-shot threads** (run once, exit): auto-login on save ([auth/routes.py:206](auth/routes.py#L206)), backfill on add-shop (`_fire_finance_seed`), SWR cache refresh.

## 7.4 Rate limiting  `BG-RATE`
- [`TokenBucket`](core/http_client.py#L19) at [core/http_client.py:19](core/http_client.py#L19): **~1 request/sec PER token**, **Redis-backed** so all workers share one budget per token.
- 100 users = 100 tokens = 100 independent budgets (no 429s).
- ⚠️ **Bottleneck:** the hourly finance loop is **sequential per shop** ([app.py](app.py)) — it can't use the parallel per-token budgets. Fix = parallelize by token.

## 7.5 `worker.py` — the dormant alternative  `BG-WORKERPY`
- `worker.py` runs the **same loops in a SEPARATE process** (true CPU isolation). **Not auto-started** — needs systemd or `docker compose exec ... python worker.py`.
- In this setup it's **NOT running**; `startup.py` (inside gunicorn) handles everything. Keep it for production scale; ignore for now.

---

# 8. 🏪 My Shops — Add & Delete a Shop  <a id="shops"></a>

> 🔎 Search keywords: `SHOP-DISCOVER`, `SHOP-ATTACH`, `SHOP-BURST`, `SHOP-SAVE`, `SHOP-DELETE`, `SHOP-FK`, `SHOP-DEADLOCK`

The page `/my-shops` just **redirects to `fetch.html`** ([admin/routes.py:514](admin/routes.py#L514)). Adding is driven by the **OpenAPI token**.

## 8.1 DISCOVER — "what shops does this token own?"  `SHOP-DISCOVER`
- Route [`discover_shops_via_openapi`](admin/routes.py#L245) at [admin/routes.py:245](admin/routes.py#L245)
  - → [`list_owned_shops`](core/uzum_openapi.py#L322) (normalizes Uzum's messy JSON)
  - → [`_call_v1_shops`](core/uzum_openapi.py#L117) (tries multiple auth-header formats — Uzum wants the **raw** token, no `Bearer`)
  - → [`_try_request`](core/uzum_openapi.py#L49) (one GET, rate-limited)
- **The token IS proof of ownership** — Uzum returns only the shops that token can see.
- Front-end: [`_renderDiscoveredShops`](templates/fetch.html#L845) draws a checkbox list; server tags each shop `already_added` / `owned_by_other`.

## 8.2 ATTACH — create the Shop rows  `SHOP-ATTACH`
- Attach handler ([fetch.html:936](templates/fetch.html#L936)) → [`attach_shops_via_openapi`](admin/routes.py#L309) at [admin/routes.py:309](admin/routes.py#L309).
- Creates/claims a `Shop` row per pick, sets `owner_id = you`, enforces the **shop limit** (default 5).
- Single-shop sibling: [`add_shop`](admin/routes.py#L191) (`POST /api/shops`) — also the **rename** endpoint (used by `editShop`).

## 8.3 BURST — background backfill  `SHOP-BURST`
- Per new shop → [`_fire_finance_seed`](admin/routes.py#L63) at [admin/routes.py:63](admin/routes.py#L63) → [`_orchestrate`](admin/routes.py#L120) spawns **3 parallel daemon threads**:
  1. [`_run_full_backfill_for_shop`](app.py#L2844) → `finance_orders` (sales history, quarter-chunked)
  2. `_run_full_expenses_backfill_for_shop` → `expenses_ledger`
  3. [`_run_products_burst`](admin/routes.py#L93) → products + SKU images
- `.start()` all 3, then `.join()` all 3 (**wait for them**), then `_send_post_backfill_summary` (Telegram).
- **`.start()` = run in parallel · `.join()` = wait for it to finish.**

## 8.4 How products SAVE to the DB  `SHOP-SAVE`
- `_run_products_burst` only **reads** the token; it **delegates** the save to [`_sync_products_via_openapi`](app.py#L3807) → [`_sync_products_via_openapi_impl`](app.py#L3896).
- The impl opens its **own** session and writes: `Shop` (ensure), then loops Uzum pages building **`ProductGroup`** (→ `product_groups`) + **`Variant`** (→ `variants`), then `db.commit()`.
- "Which table" = the model's `__tablename__`. "Which shop" = the `shop_id` / `group_id` foreign keys.
- Then [`_sync_finance_for_shop`](app.py#L4152) at [app.py:4152](app.py#L4152) **reads** 30-day sales from `finance_orders` and **stamps** `sales_30d_finance` + `avg_daily_sales` onto each variant (UPDATE, not insert). ⚠️ Can race the backfill → shows 0 until "Sync Finance" re-stamps.

## 8.5 DELETE a shop  `SHOP-DELETE`
- Front-end [`deleteShop`](templates/fetch.html#L759) → `DELETE /api/shops/<dbId>` (uses `data-*` attrs read via `e.target.dataset`).
- Backend [`delete_shop`](admin/routes.py#L446) at [admin/routes.py:446](admin/routes.py#L446) — **hand-rolled cascade**, in order:
  `variants → product_groups → pos_action_log → finance tables (by uzum_id) → the shop row → db.commit()`.
- `db.delete(shop)` (one loaded object) vs `db.execute(delete(Variant).where(...))` (bulk, by condition). **Both need `db.commit()`** to be permanent.

## 8.6 FK vs soft-link — why finance cleanup is manual  `SHOP-FK`
- **Catalog** (shop→group→variant) = **real foreign keys** (numbers, DB-enforced, can cascade). Shop id stored **once** (on the group); variant derives it via the chain (= normalization).
- **Finance** (`finance_orders`, etc.) = **soft link**: stores `shop_id` = `shops.uzum_id` **as a string**, **NO foreign key**. The DB doesn't know it means a shop.
- ⚠️ **No DB cascade** → `delete_shop` must delete finance rows **by hand**. **Add a new `shop_id` table? You MUST add it to `delete_shop` or shop-delete 500s / leaves orphans.**
- The `uzum_id_int` vs `uzum_id_str` juggling exists because finance tables inconsistently store `shop_id` as int (Expenses/SyncState) vs string (FinanceOrder/Snapshot).

## 8.7 Deadlocks & locking  `SHOP-DEADLOCK`
- **Deadlock** = two transactions each hold a row the other needs → stuck. Postgres kills one ("victim") with an error.
- [`delete_shop`](admin/routes.py#L462) wraps the cascade in a **retry loop** (up to 4×, backoff `0.2 * attempt`): retry **only** on deadlock (transient, since it collides with finance loops), fail fast on anything else.
- Three concurrency tools in the app:
  - **retry-on-deadlock** → `delete_shop` only
  - **`with_for_update()` + consistent lock order** (PREVENT deadlocks) → POS ([pos/routes.py:174](pos/routes.py#L174)), warehouse
  - **`shop_lock`** (Redis, app-level "one process per shop") → [core/shop_lock.py:28](core/shop_lock.py#L28), used by all finance/products loops

**Add-shop in one line:** paste token → discover → pick → attach (create Shop) → `_fire_finance_seed` bursts 3 parallel backfills → products/finance fill in → page refreshes.
**Delete-shop in one line:** `deleteShop` confirms → `delete_shop` hand-deletes every child table (no DB cascade) → deletes shop row → retries on deadlock.

---

## ♻️ New patterns (from sections 6–8)

### `PAT-DELEGATE` — Orchestrator reads, worker saves
- An orchestrator function (e.g. [`_run_products_burst`](admin/routes.py#L93)) only **reads/sets up**, then **delegates** the actual DB writes to a deeper function that opens its **own** session and commits.
- To find where data is saved, follow the call chain down — the `db.add/commit` lives where the data is **built**, not in the orchestrator.

### `PAT-FK-VS-SOFTLINK` — Two ways to link tables
- **Foreign key** (number, DB-enforced, can cascade) for the app's **own nested data** (catalog).
- **Soft link** (matching `uzum_id` string, app-enforced, no cascade) for **flat, externally-sourced** data (finance).
- Soft links cost you: **manual cleanup on delete** + type juggling. New `shop_id` table → update `delete_shop`.

### `PAT-NORMALIZE` — Store a fact once, derive the rest
- Don't store `shop_id` on the variant — its group already knows the shop. Walk the chain (variant→group→shop). One source of truth, no out-of-sync duplicates.

### `PAT-DEADLOCK-RETRY` — Retry transient conflicts, fail fast on real errors
- Wrap a contended transaction in a loop; on a **deadlock** error, wait (backoff) and retry; on any other error, raise immediately.
- Prevent deadlocks elsewhere by **locking rows in a consistent order** (`with_for_update`).

### `PAT-START-JOIN` — Parallel then barrier
- `thread.start()` × N = run jobs in parallel. `thread.join()` × N = **wait for all** before the next step (e.g. send a summary only after all backfills finish).

### `PAT-DATASET` — HTML carries data, JS reads it back
- Render `data-db-id` / `data-name` on a button → on click, `e.target.dataset.dbId` / `.name` read them back (kebab-case → camelCase). That's how the right row's id reaches the handler.

---

# 9. 🔌 Fetch & Data Layer — how data comes in and is read back  <a id="fetch"></a>

> 🔎 Search keywords: `FETCH-MAP`, `FETCH-SHARED`, `FETCH-PARSE`, `FETCH-VS-SAVE`, `FETCH-READ`, `FETCH-LIVE`

## 9.1 The fetch file-map — fetches are split by domain  `FETCH-MAP`
| Fetches | File | Key functions |
|---|---|---|
| **Products + shop discovery** | [core/uzum_openapi.py](core/uzum_openapi.py) | `fetch_products_page` ([:139](core/uzum_openapi.py#L139)), `list_owned_shops` ([:322](core/uzum_openapi.py#L322)) |
| **Finance** (orders, daily aggregates, expenses) | [core/uzum_finance_openapi.py](core/uzum_finance_openapi.py) | `fetch_daily_aggregates_for_shop_day` ([:391](core/uzum_finance_openapi.py#L391)), `fetch_finance_orders_page`, `fetch_finance_expenses_for_shop_window` |
| **Reports CSV (LEGACY fallback)** | [core/uzum_reports.py](core/uzum_reports.py) | 4-step async: **Create → Poll → Download → Parse CSV**. Only runs if OpenAPI fails ([app.py:3413](app.py#L3413)) |
| **Cabinet /sku-list** (cost+images) | [core/uzum_skulist.py](core/uzum_skulist.py) | uses admin cabinet token, not OpenAPI |

- **OpenAPI = synchronous** (data in the JSON response). **Reports = asynchronous** (request a file → poll until ready → download CSV). OpenAPI is PRIMARY; Reports is the dormant backup.

## 9.2 The shared layer — everything goes through it  `FETCH-SHARED`
- [core/http_client.py](core/http_client.py) holds the **pooled HTTP session** + the **`TokenBucket`** ([:19](core/http_client.py#L19)). Every fetch (products/finance/reports) passes through here → that's why they all respect the same ~1/sec-per-token limit. Rate-limiting isn't duplicated per file; it's in this one place.

## 9.3 `parsers.py` — clean Uzum's messy JSON  `FETCH-PARSE`
- [core/parsers.py](core/parsers.py): Uzum returns the same field under different keys/shapes across endpoints. These helpers **try many keys** to reliably extract values.
  - `_extract_uzum_qty` ([:44](core/parsers.py#L44)) — stock qty (tries ~25 keys, nested containers, then a **scored recursive scan**)
  - `_safe_qty` ([:10](core/parsers.py#L10)) · `_extract_sku` ([:141](core/parsers.py#L141)) · `_collect_variant_rows` ([:172](core/parsers.py#L172)) · `_safe_status_text`
- Used mostly in the **product sync** — cleans raw Uzum product data before saving to `variants`.

## 9.4 Fetch vs Save are SEPARATE  `FETCH-VS-SAVE`
- **Fetch** (`uzum_openapi.py`) = "the phone": calls Uzum, returns raw JSON, **knows nothing about the DB**.
- **Save** (`_sync_products_via_openapi_impl`, [app.py:3896](app.py#L3896)) = "the filing clerk": loops pages, builds `ProductGroup`/`Variant` rows (via `parsers.py`), `db.commit()`. **Knows nothing about Uzum's HTTP.**
- They meet at ONE call: `fetch_products_page(...)` inside the save loop. (See pattern `PAT-FETCH-VS-SAVE`.)
- ⚠️ Note: `fetch_products_page` was imported into app.py under an alias; aliases are just `import X as Y` — same function, local nickname. (Now imported with its plain name.)

## 9.5 `read_sales_aggregated` — the central money reader  `FETCH-READ`
- [core/sales_reads.py:131](core/sales_reads.py#L131): ONE `GROUP BY` query over **`finance_orders`** that sums units/revenue/profit/commission/cost over a date window, grouped by **day / month / sku**.
- The actual DB hit is [line 248](core/sales_reads.py#L248) (`sess.execute(stmt)`). It reads `finance_orders` via the **`FinanceOrder`** model.
- Used EVERYWHERE: economics (month/day), restock (sku), group page, charts, and the `_sync_finance_for_shop` stamp. One source of truth → all pages' numbers agree.
- Session handling: `_resolve_session` ([:122](core/sales_reads.py#L122)) **reuses a passed `session=db`** or **makes its own** (`owns` flag decides whether to close it). (See pattern `PAT-SESSION-REUSE`.)

## 9.6 Live read vs stored snapshot — `sales_30d_finance`  `FETCH-LIVE`
- **Restock page** reads 30-day sales **LIVE** from `finance_orders` via `_restock_sales_maps` ([products/routes.py:2263](products/routes.py#L2263)) → `read_sales_aggregated`, matched per-variant by `_restock_match` (sku → uzum_sku_id → title/barcode). Always current. ✅
- **`Variant.sales_30d_finance`** is a STORED snapshot stamped by `_sync_finance_for_shop`. It's **half-retired**: the restock page bypasses it, but **still read by** `get_group_variants_api` ([products/routes.py:2176](products/routes.py#L2176)) and the **Telegram bot** ([app.py:957](app.py#L957)+).
- ⚠️ **The 0-bug:** the stamp runs in parallel with the finance backfill it depends on → reads empty data → writes 0. It's a **timing race on a derived value** (not corruption). Fix = run the stamp AFTER the backfill, OR migrate the 2 remaining consumers to read live, then drop the column. (See pattern `PAT-LIVE-VS-SNAPSHOT`.)

---

# 10. 🔒 Concurrency & Locking (deep)  <a id="concurrency"></a>

> 🔎 Search keywords: `LOCK-SIGN`, `LOCK-VS-RATE`, `LOCK-GRAIN`, `LOCK-DEADLOCK`

## 10.1 `shop_lock` = a "busy sign" for a shop  `LOCK-SIGN`
- [core/shop_lock.py:28](core/shop_lock.py#L28): before working on a shop, a process flips a **"busy" sign**; if it's already flipped, it **skips**. Stops two processes updating the SAME shop at once (e.g. hourly sync + a manual "Sync now").
- Mechanism: Redis `SET shop_lock:<id> 1 NX EX 300` — `NX` = "only if not already set" (True=got it, False=taken); `EX 300` = **auto-unlock after 5 min** so a crashed worker can't lock a shop forever.
- Lives in **Redis** so ALL processes see the same signs (cross-process coordination).

## 10.2 `shop_lock` vs `TokenBucket` — DIFFERENT jobs  `LOCK-VS-RATE`
| | `TokenBucket` (rate limit) | `shop_lock` (mutex) |
|---|---|---|
| Controls | how **fast** you call Uzum | **who** works on a shop |
| Protects | **Uzum's API** (avoid 429) | **your database** (no two writers collide) |
| Scope | per **token** | per **shop** |
- You need **both**. Rate-limiting two writers on the same shop still corrupts data; the lock fixes that. The lock doesn't stop 429s; the bucket does.

## 10.3 Granularity — per-shop lock, per-token rate  `LOCK-GRAIN`
- Lock key = **shop id** (NOT user). Different shops = different locks → **fully parallel**; same shop = serialized (what you want).
- Rate = per **token**; one user's shops **share one token** → their requests trickle ~1/sec **interleaved** (effectively sequential). **Different users = different tokens = real parallelism.**
- So for 100 users × 5 shops: **100-wide parallel** (across users), each user's 5 shops paced by their one bucket. The lock never serializes different shops; the **token bucket** is the per-user choke point.
- The shop holds its lock for the **whole** fetch (acquire → all pages → save → release in `finally`), so a 20-page fetch keeps the shop "busy" ~20s — but other shops fetch freely.

## 10.4 Deadlocks  `LOCK-DEADLOCK`
- See §8.7 `SHOP-DEADLOCK`. Summary: deadlock = two transactions each holding what the other needs; Postgres kills one. `delete_shop` **retries** on deadlock; POS/warehouse **prevent** them with `with_for_update()` + consistent lock order.

---

# 11. 🐳 Infra — Docker stack & Redis  <a id="infra"></a>

> 🔎 Search keywords: `INF-SERVICES`, `INF-REDIS`, `INF-BOOT`

## 11.1 The 4 Docker services  `INF-SERVICES`
| Service | Is | Analogy |
|---|---|---|
| **db** (Postgres) | the real database, on disk | the warehouse (permanent) |
| **pgbouncer** | connection pooler (funnel many app conns → ~25 real) | the receptionist |
| **redis** | in-memory shared scratchpad (fast, temporary) | the sticky-note board |
| **app** (gunicorn 4×16) | the Flask web server | the staff |
- App connects via `DATABASE_URL → pgbouncer:6432` and `REDIS_URL → redis:6379`. Docker resolves service names as hostnames on a private network. (pgbouncer details: §2.3)

## 11.2 What Redis stores (why it's needed)  `INF-REDIS`
- Redis = RAM-only, super fast, **shared across all processes** (that's the point). Holds: **TokenBucket** rate buckets, **subscription cache**, **`shop_lock`** signs, **SWR** cache.
- Why not Postgres for these? They're hot, short-lived, and need cross-process speed. Redis is the fast shared scratchpad; Postgres is the permanent store.

## 11.3 Why all 4 boot together  `INF-BOOT`
- The app can't run without DB (via pgbouncer) + Redis — they're **one system**. `docker compose up` starts all; **`depends_on` + `healthcheck`** enforce order:
  `db ready → pgbouncer + redis ready → app starts`. Starting the app first would crash (DB not up).

---

## ♻️ New patterns (from sections 9–11)

### `PAT-FETCH-VS-SAVE` — Fetch and save live in different layers
- The fetch function returns raw data and knows nothing about the DB; a separate save function loops + writes rows. They meet at one call. Find the save by following the chain down to where `db.commit()` is.

### `PAT-LIVE-VS-SNAPSHOT` — Read live vs store a derived value
- **Live read** (compute from the source table each load) = always current, no race, but recomputed each time. **Stored snapshot** (a derived column) = fast but can go **stale / race its inputs**. Prefer live unless it's a measured perf problem; if stored, sequence the stamp AFTER its inputs.

### `PAT-SESSION-REUSE` — Reuse a passed session or make your own
- A read helper takes `session=None`; if given one it **reuses** it (shared connection, `owns=False`, don't close), else it **creates** one (`owns=True`, close in `finally`). Lets callers batch many reads on one connection.

### `PAT-BUSY-SIGN` — One worker per resource via a shared flag
- Put a short-lived flag in Redis (`SET key NX EX ttl`) before working on a resource; skip if it exists. TTL auto-frees it if the worker crashes. Coordinates across processes/machines (vs an in-process set, which can't).

---

# 12. 💾 How Products Save to the DB — the save loop  <a id="save"></a>

> 🔎 Search keywords: `SAVE-STEP0`, `SAVE-PAGE`, `SAVE-GROUP`, `SAVE-VARIANT`, `SAVE-FLUSH`, `SAVE-ARCHIVE`, `SAVE-SORT`

**Big idea:** [`_sync_products_via_openapi_impl`](app.py#L3706) mirrors a shop's Uzum catalog into our DB. It **pages** through Uzum, **upserts** a `ProductGroup` per product + a `Variant` per SKU, **commits once per page**, then **archives** whatever Uzum stopped sending. `fetch_products_page` returns raw JSON; this function does all the DB work. (Public entry is the lock-wrapped [`_sync_products_via_openapi`](app.py#L3634); see `LOCK-SIGN` §10.)

## 12.1 Step 0 — make sure the Shop row exists  `SAVE-STEP0`
- [app.py:3713-3724](app.py#L3713). Look up `Shop` by `uzum_id`; if missing, create a stub and **commit immediately** (every `ProductGroup` needs a real `shop_id` FK). Keep `current_shop_pk`.
- **Safety net only** — the real shop is normally created earlier in [`add_shop`](admin/routes.py#L191). This branch fires if a background loop syncs a shop with no row yet. The stub it makes is minimal (`owner_id=None`) — the tell that it's not the "normal" birth.

## 12.2 Pass 1 — the paging loop (P1–P7)  `SAVE-PAGE`
One `SessionLocal()` wraps a `while True:` ([app.py:3748](app.py#L3748)). Each loop = one page:
- **P1 Fetch** — `fetch_products_page(..., accept_language="ru")` → `raw` (Uzum JSON as dict). Throws → `break`.
- **P2 Read** — `products = raw.get("productList")` ([:3759](app.py#L3759)) — Uzum's key. Empty → break. (`.get` + `or []` so a malformed page ends cleanly, not a crash.)
- **P3 Loop + count** — `for p in products`, skip junk ids, `product_counter += 1`.
- **P4 Extract** — title, image (+`/t_product_540_high.jpg` fixup), category, archived-boolean, commission.
- **P5 Upsert group** (12.3).
- **P6 Variants** (12.4) + `active_group_ids.add(group.id)` if not archived ([:3823](app.py#L3823)).
- **P7 Commit** — `db.commit()` once per page ([:4010](app.py#L4010)). Then: short page (`len(products) < size`) or `page >= max_pages` → break; else `page += 1`.

## 12.3 ProductGroup upsert — find or create  `SAVE-GROUP`
- [app.py:3792](app.py#L3792): `SELECT ProductGroup WHERE uzum_product_id AND shop_id` (scoped to *this* shop via `current_shop_pk`).
  - `None` → new `ProductGroup`, `db.add`, **`db.flush`** ([:3808](app.py#L3808)) so `group.id` exists for the variants.
  - else → update fields in place (no add/flush — SQLAlchemy auto-tracks the loaded row).
- `if image:` guards the image (don't wipe a good one with an empty); `name` is overwritten unconditionally.
- **Saved cols:** uzum_product_id, name, image_url, shop_id, is_archived, uzum_sort_order, category, commission. **NOT written** (OpenAPI lacks them; kept from the old browser sync): viewers, conversion, roi, rating, feedback_quantity, rank.

## 12.4 Variant save — per SKU (chunks 1–8)  `SAVE-VARIANT`
- **Pre-load + index** ([:3826](app.py#L3826)): load this group's existing variants once, build 3 lookup dicts — `_v_by_uzum_id`, `_v_by_barcode`, `_v_by_sku` (one SELECT, then in-memory matching — no per-SKU query).
- For each `s` in `p["skuList"]`:
  1. extract raw fields; skip if no `skuFullTitle`/`skuTitle`.
  2. **Upsert** ([:3883](app.py#L3883)): match by `uzum_sku_id → barcode → sku_title` (strongest key first); all miss → new `Variant(group_id=..., sku=...)` + `db.add`.
  3. **Stamp identity** (`uzum_sku_id`, `barcode`) — runs for new *and* existing (so a new variant is findable next sync; a missing barcode gets backfilled).
  4. **Guarded assignments** fill ~25 columns — each `if value is not None: try: v.col = int(value) except: pass` (missing → keep old value; bad type → skip; never crash).
  5. `image_url` deliberately **not** written (owned by the sku-list image fetcher). `status` derived from blocked/archived booleans.
  6. **`db.flush()` + `total_variants += 1`** ([:4007](app.py#L4007)).

## 12.5 add / flush / commit — the three steps  `SAVE-FLUSH`
- **`db.add(obj)`** = stage a NEW object (its `id` is still `None`).
- **`db.flush()`** = send the INSERT inside the transaction → DB assigns the auto-id → `group.id`/`v.id` now readable so **child rows can attach**. Saved-but-not-permanent (a rollback still undoes it; other connections can't see it yet).
- **`db.commit()`** = make the whole page permanent, once per page (atomic batch).
- Existing (loaded) rows need **no add/flush** — SQLAlchemy tracks attribute changes and emits UPDATEs at commit. *Whiteboard (flush) vs photograph (commit).*

## 12.6 Pass 3 — archive reconciliation  `SAVE-ARCHIVE`
- [app.py:4018](app.py#L4018). Two bulk UPDATEs (this shop only): ids **in** `active_group_ids` → `is_archived=False`; ids **NOT in** it (`~...in_`) → `is_archived=True`.
- A product the seller **deleted** never enters `active_group_ids` (absent from every page's `productList`) → flipped archived. No explicit delete-detection needed — inferred from absence.
- **Empty-guard:** if `active_group_ids` is empty (API hiccup → 0 products), **skip** — never archive the whole catalog over a blip.

## 12.7 Identity vs position  `SAVE-SORT`
- **Identity (fixed):** `uzum_product_id` (Uzum's, stable — the upsert match key, this is *why* re-syncing never duplicates) and our PK `id` (assigned once, never changes). 
- **Position (fluid):** `uzum_sort_order = product_counter` — re-numbered every sync from Uzum's current order (new/edited products auto-float to the top). The groups + POS pages `ORDER BY` it ([products/routes.py:147](products/routes.py#L147)). Stored as **Integer** so it sorts numerically; the PK can't substitute because a newly-added product gets the **highest** PK yet shows at the **top**.

**Save loop in one line:** ensure Shop → page Uzum → upsert each group (flush for its id) → upsert its variants (guarded fill, flush) → commit per page → archive whatever Uzum stopped sending.

---

## ♻️ New patterns (from §12)

### `PAT-UPSERT` — Look up, update if found, insert if missing
- `SELECT by a stable key` → found: edit in place; not found: `db.add(new)`. Used for Shop, ProductGroup, Variant. Re-syncing never duplicates. Match on the **most stable** key first (Uzum id > barcode > title) — matching title-first would turn a rename into a duplicate.

### `PAT-ADD-FLUSH-COMMIT` — Stage, get the id, persist
- `add` stages a new row; `flush` sends it so the DB assigns the auto-id (needed so **child rows can reference the parent**); `commit` makes it durable. Loaded rows skip add/flush — SQLAlchemy auto-UPDATEs them.

### `PAT-GUARDED-ASSIGN` — Never overwrite good data with nothing
- `if value is not None: try: col = cast(value) except: pass`. Missing field → keep the old value; bad type → skip that field; one bad field never kills the whole sync. (Same instinct as `if image:` on the group.)

### `PAT-RECONCILE-BY-ABSENCE` — Detect deletions by what's missing
- Collect the ids you saw this run; afterward flag everything NOT in that set (scoped to the owner) as archived/gone. **Guard against an empty set** — don't nuke everything on a fetch failure.
