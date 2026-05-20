# Legacy `/finance/orders` retirement

This directory archives the legacy Uzum finance-fetch pipeline as it is
removed from the live codebase. Files here are **not imported** — they are
plain-text snapshots so a human can read the deleted code without doing
git surgery.

## Why this exists

The app dual-writes finance data:

- **Live:** `/api/seller/documents/v2` (SELLS_REPORT, EXPENSES_REPORT) → `sales_lines`, `expenses_ledger`
- **Legacy:** `/api/seller/finance/orders`, `/api/seller/finance/expenses` → `FinanceOrder`, `FinanceHourlySnapshot`, `WarehouseExpenseSnapshot`

Every user-facing page reads from the live tables. The legacy tables are
either unread (`WarehouseExpenseSnapshot`, `FinanceHourlySnapshot`) or
read only by `/admin/sales-diff` for parity verification. Once parity is
signed off, the entire legacy pipeline can come out.

## Retirement steps (ordered, safest first)

| # | Scope | Risk | Parity needed? | Status |
|---|---|---|---|---|
| 1 | `WarehouseExpenseSnapshot` writer + table — orphan writes, zero readers | None | No | pending |
| 2 | `FinanceHourlySnapshot` writer + table — readers removed in Phase 3 | None | No | pending |
| 3 | `auto_today` / `auto_recent` / `auto_backfill` in `_finance_auto_refresh_loop` — only feeds `/admin/sales-diff` | Low | Yes | pending |
| 4 | Manual full-sync UI + `_run_manual_sync_job` + `_finance_sync_range` | Low | Yes | pending |
| 5 | `/admin/sales-diff` page + `FinanceOrder` table (after parity sign-off) | Low | Yes | pending |
| 6 | Replace `fetch_finance_sales_map()` (Variant 30-day seed at shop add) with SELLS_REPORT 30-day call | Medium | No (smoke test) | pending |

Each step gets one commit. After each commit the Debugger agent runs the
relevant checks from `project_sales_reports_testing_plan.md`. We move to
the next step only after PASS.

## Step 1 — file/line targets (concrete brief for Fetch-Agent)

Remove writes + thread + table for `WarehouseExpenseSnapshot`.

- `app.py:16` — drop `WAREHOUSE_EXPENSE_SNAPSHOT_HOUR, WAREHOUSE_EXPENSE_SNAPSHOT_MINUTE` from config import
- `app.py:86` — drop `WarehouseExpenseSnapshot` from models import
- `app.py:297` — drop `CREATE INDEX ix_wes_day_shop` line (the live `expenses_ledger` index is independent)
- `app.py:921-?` — `_build_warehouse_expense_snapshots_for_shops`
- `app.py:977-?` — `_store_warehouse_expense_snapshot_for_day`
- `app.py:1003-?` — `_load_warehouse_expense_snapshots` (already orphan)
- `app.py:1041-?` — `_capture_warehouse_expense_snapshot_for_day`
- `app.py:1085-?` — `_warehouse_expense_snapshot_progress`
- `app.py:3435-3460` — `_warehouse_expense_snapshot_loop`
- `worker.py:39-42` — thread launch
- `background/startup.py:49` — thread launch
- `admin/routes.py:26` — drop import
- `admin/routes.py:220` — drop `delete(WarehouseExpenseSnapshot)` on shop removal
- `models.py:418-427` — `WarehouseExpenseSnapshot` class
- `config.py:60-62` — `WAREHOUSE_EXPENSE_SNAPSHOT_HOUR`/`_MINUTE`
- `.env.example:52-53` — same env vars

**New alembic migration** in `migrations/versions/`:

- `upgrade()`: `DROP INDEX IF EXISTS ix_wes_day_shop; DROP TABLE IF EXISTS warehouse_expense_snapshots;`
- `downgrade()`: full `CREATE TABLE` matching `models.py:418-427` so we can roll back if needed.

**Comment touch-ups (housekeeping, not blocking):**

- `worker.py:55-57` — drop `WarehouseExpenseSnapshot` mention from comment
- `background/startup.py:53-55` — same

## Restoration

To bring step N back:

1. `git revert <commit-sha>` of the step's commit (preferred — atomic).
2. Or copy the relevant block from `legacy/finance_orders_pipeline.py`,
   re-add the model class to `models.py`, run `alembic downgrade` for the
   drop migration.

The archive snapshots **do not import** anything, so they cannot run.
Treat them as read-only documentation. The model classes, env vars, and
function signatures captured below are pinned to the commit that removed
them — if other code has since changed (renames, signatures), updates
will be needed.

## Archive files

- `finance_orders_pipeline.py` — every function/class block removed by
  steps 1-6, in removal order, each block prefixed with the source file +
  the commit SHA that removed it.
