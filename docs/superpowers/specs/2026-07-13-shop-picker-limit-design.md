# Enforce the shop limit in the OpenAPI shop picker

**Date:** 2026-07-13
**Status:** approved

## Problem

The shop picker (`/fetch` → «Выберите магазины») lets the user tick every shop the
token found. The per-user shop limit is enforced only on the server: `attach_shops_via_openapi`
computes `headroom = limit - current_count` and silently drops the overflow with skip
reason `limit_reached`.

So a user with 5 free slots can tick 6 shops, press «Добавить выбранные», and watch
one of them vanish with no say in *which* one. The limit is invisible until it bites.

## Goal

Make over-selection impossible. If the user has N slots left, the picker lets them tick
at most N shops. `limit_reached` becomes unreachable through the UI.

This is a **UI guard, not a replacement** for the server gate. The server-side `headroom`
check stays exactly as it is — a stale page must still be refused.

## Design

### Backend

`discover_shops_via_openapi` (`admin/routes.py`) returns `{"shops": [...]}` today. It will
also return `shop_count` and `shop_limit`, computed with the existing `_can_user_add_shop`
helper — the same call `attach` already makes, and the same two fields the attach response
already returns.

Semantics carry over unchanged:

- `shop_limit <= 0` means **unlimited** (matches `_can_user_add_shop`).
- Admins report unlimited.

No new endpoint, no new helper, no schema change.

### Frontend (`templates/fetch.html`)

The modal derives a slot budget:

```js
slotsLeft = shop_limit > 0 ? Math.max(0, shop_limit - shop_count) : Infinity
```

Three behaviours follow:

1. **Rows disable dynamically once the budget is spent.** `_renderDiscoveredShops` already
   greys rows for `already_added` / `owned_by_other`. Those two are *static* — known at
   render time. "No slots left" is a **third, dynamic reason**, so it is evaluated in
   `_updateOpiPickerUi` on every tick, not baked into the row at render. A blocked row gets
   the same grey treatment plus a `лимит` chip, visually distinct from `занят`. Unticking
   any shop re-enables the rest.

2. **«Выбрать все» ticks the first `slotsLeft` free shops**, not all of them.

3. **The counter reads `selected / slotsLeft`** when a limit applies, falling back to
   today's `selected / pickable` when unlimited.

### Zero slots

The modal still opens, so the user can see what their token found. Every row is greyed,
a banner reads «Лимит исчерпан: 5 из 5 магазинов», and the add button stays disabled.

### After a successful add

The attach response's `shop_count` / `shop_limit` refresh the budget in place, so adding 2
of 5 and reopening the picker correctly offers 3 — no page reload needed.

## Strings

Four new keys (ru + uz) in `translations.py`:

| key | purpose |
|---|---|
| `fetch_openapi_limit_chip` | the `лимит` chip on a blocked row |
| `fetch_openapi_limit_exhausted` | zero-slots banner |
| `fetch_openapi_limit_hint` | «Осталось слотов: N» hint under the title |
| `fetch_openapi_limit_of` | «N из M магазинов» fragment |

## Testing

The headless harness drives the real modal. Verify:

- cap at `shop_limit` 5 and 6 with 6 shops discovered → cannot tick more than the budget
- `shops_left = 0` → all rows greyed, banner shown, add button disabled
- unlimited (`shop_limit = 0`) → no cap, today's behaviour
- «Выбрать все» respects the budget
- light + dark at 375 / 768 / 1440
