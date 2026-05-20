"""
Archive of the legacy /finance/orders + /finance/expenses pipeline.

NOT IMPORTED. NOT EXECUTABLE. This file is a plain-text snapshot of code
that has been removed from the live codebase. See ./README.md for the
retirement plan and restoration instructions.

Each block below is prefixed with:
  # ── STEP N — <description> (removed in commit <sha>) ──────────────
  # FROM <relative path>:<line range at removal>

Blocks are appended in removal order. Do not edit existing blocks; only
append. To restore code, prefer `git revert <sha>` over copying from
here, since dependencies (imports, related models) may have shifted.
"""

# (empty — entries appended as steps land)


# ─────────────────────────────────────────────────────────────────────
# STEP 1 — WarehouseExpenseSnapshot pipeline (removed: <commit-pending>)
#
# This was the daily 23:30 background loop that fetched
# /api/seller/finance/expenses for every shop and stored a per-shop
# rollup row in `warehouse_expense_snapshots`. The table had no readers
# (orphan writes); the live expenses path uses `expenses_ledger` via
# core/sales_reads.py:355-498.
# ─────────────────────────────────────────────────────────────────────


# ── FROM models.py:418-427 ──────────────────────────────────────────
class WarehouseExpenseSnapshot(Base):
    """Daily warehouse-expense snapshot per shop for Telegram summaries."""
    __tablename__ = "warehouse_expense_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    expense_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    shop_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    items_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    total_amount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)


# ── FROM config.py:60-62 ────────────────────────────────────────────
# ── Warehouse expense snapshots ───────────────────────────────────────
WAREHOUSE_EXPENSE_SNAPSHOT_HOUR = min(23, max(0, int(os.getenv("WAREHOUSE_EXPENSE_SNAPSHOT_HOUR", "23"))))
WAREHOUSE_EXPENSE_SNAPSHOT_MINUTE = min(59, max(0, int(os.getenv("WAREHOUSE_EXPENSE_SNAPSHOT_MINUTE", "30"))))


# ── FROM app.py:977-1001 ────────────────────────────────────────────
def _store_warehouse_expense_snapshot_for_day(expense_day: date, snapshots_by_shop: dict[str, dict]) -> int:
    captured_at = _app_naive(_now_app_tz())
    shop_ids = list(snapshots_by_shop.keys())
    with SessionLocal() as db:
        if shop_ids:
            db.execute(
                delete(WarehouseExpenseSnapshot).where(
                    WarehouseExpenseSnapshot.expense_date == expense_day,
                    WarehouseExpenseSnapshot.shop_id.in_(shop_ids),
                )
            )
        rows = []
        for shop_id, payload in snapshots_by_shop.items():
            rows.append({
                "expense_date": expense_day,
                "shop_id": shop_id,
                "items_json": json.dumps(payload.get("items", []), ensure_ascii=False),
                "total_amount": int(payload.get("total", 0) or 0),
                "captured_at": captured_at,
            })
        if rows:
            db.execute(insert(WarehouseExpenseSnapshot), rows)
        db.commit()
    return len(rows)


# ── FROM app.py:1003-1039 ───────────────────────────────────────────
def _load_warehouse_expense_snapshots(expense_day: date, shop_ids: list[str]) -> dict[str, dict]:
    if not shop_ids:
        return {}
    with SessionLocal() as db:
        rows = db.execute(
            select(WarehouseExpenseSnapshot).where(
                WarehouseExpenseSnapshot.expense_date == expense_day,
                WarehouseExpenseSnapshot.shop_id.in_(shop_ids),
            )
        ).scalars().all()

    snapshots: dict[str, dict] = {}
    for row in rows:
        try:
            items = json.loads(row.items_json or "[]")
        except Exception:
            items = []
        if not isinstance(items, list):
            items = []
        normalized_items = []
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            try:
                amount = int(item.get("amount", 0) or 0)
            except (ValueError, TypeError):
                amount = 0
            if not name or amount <= 0:
                continue
            normalized_items.append({"name": name, "amount": amount})
        snapshots[str(row.shop_id)] = {
            "items": normalized_items,
            "total": int(row.total_amount or 0),
        }
    return snapshots


# ── FROM app.py:1041-1083 ───────────────────────────────────────────
def _capture_warehouse_expense_snapshot_for_day(
    expense_day: date,
    *,
    allow_latest_fallback: bool = False,
) -> dict:
    api_key = _get_admin_token()
    if not api_key:
        print(f"[ExpensesSnapshot] No admin token, skipping snapshot for {expense_day.isoformat()}.")
        return {"ok": False, "reason": "no_token", "expense_day": expense_day.isoformat()}

    with SessionLocal() as db:
        shops = db.execute(select(Shop)).scalars().all()
        shop_ids = [str(shop.uzum_id).strip() for shop in shops if shop.uzum_id]

    if not shop_ids:
        print(f"[ExpensesSnapshot] No shops found, skipping snapshot for {expense_day.isoformat()}.")
        return {"ok": False, "reason": "no_shops", "expense_day": expense_day.isoformat()}

    started_at = time.monotonic()
    payments = _fetch_warehouse_expense_payments(shop_ids, api_key=api_key)
    filtered = _filter_warehouse_expense_payments_for_day(
        payments,
        expense_day,
        allow_latest_fallback=allow_latest_fallback,
    )
    snapshots_by_shop = _build_warehouse_expense_snapshots_for_shops(filtered, shop_ids)
    row_count = _store_warehouse_expense_snapshot_for_day(expense_day, snapshots_by_shop)
    total_amount = sum(int(payload.get("total", 0) or 0) for payload in snapshots_by_shop.values())
    elapsed = time.monotonic() - started_at
    print(
        f"[ExpensesSnapshot] day={expense_day.isoformat()} shops={len(shop_ids)} "
        f"payments={len(filtered)} rows={row_count} total={total_amount} elapsed={elapsed:.2f}s"
    )
    return {
        "ok": True,
        "expense_day": expense_day.isoformat(),
        "shop_count": len(shop_ids),
        "payment_count": len(filtered),
        "row_count": row_count,
        "total_amount": total_amount,
        "elapsed_seconds": round(elapsed, 2),
    }


# ── FROM app.py:1085-1095 ───────────────────────────────────────────
def _warehouse_expense_snapshot_progress(expense_day: date) -> tuple[int, int]:
    with SessionLocal() as db:
        expected_rows = int(db.execute(
            select(func.count()).select_from(Shop).where(Shop.uzum_id != None)
        ).scalar() or 0)
        actual_rows = int(db.execute(
            select(func.count()).select_from(WarehouseExpenseSnapshot).where(
                WarehouseExpenseSnapshot.expense_date == expense_day
            )
        ).scalar() or 0)
    return expected_rows, actual_rows


# ── FROM app.py:3435-3460 ───────────────────────────────────────────
def _warehouse_expense_snapshot_loop():
    """Background thread: capture warehouse-expense snapshots once daily around 23:30."""
    import time as _t

    while True:
        try:
            now = _now_app_tz()
            if (
                now.hour == WAREHOUSE_EXPENSE_SNAPSHOT_HOUR
                and now.minute >= WAREHOUSE_EXPENSE_SNAPSHOT_MINUTE
            ):
                expense_day = now.date()
                expected_rows, actual_rows = _warehouse_expense_snapshot_progress(expense_day)
                if expected_rows > 0 and actual_rows < expected_rows:
                    print(
                        f"[ExpensesSnapshot] Capturing day={expense_day.isoformat()} "
                        f"progress={actual_rows}/{expected_rows}"
                    )
                    _capture_warehouse_expense_snapshot_for_day(
                        expense_day,
                        allow_latest_fallback=False,
                    )
            _t.sleep(60)
        except Exception as e:
            print(f"[ExpensesSnapshot] Unexpected error: {e}")
            _t.sleep(60)


# ─────────────────────────────────────────────────────────────────────
# STEP 2 — FinanceHourlySnapshot writer + table (removed: <commit-pending>)
#
# Per-hour rollup of FinanceOrder daily aggregates. Reads were removed
# in Phase 3 (sales_lines took over for "За час" / "С 00:00" Telegram
# numbers); only the writer remained. Step 2 of the legacy retirement.
# ─────────────────────────────────────────────────────────────────────


# ── FROM models.py:355-372 ──────────────────────────────────────────
class FinanceHourlySnapshot(Base):
    """Snapshot of daily sales totals taken each hour.

    Hourly delta = current finance_orders totals - previous snapshot.
    Zero API calls needed — reads entirely from PostgreSQL.
    """
    __tablename__ = "finance_hourly_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shop_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    sku_title: Mapped[str] = mapped_column(String(300), nullable=False)
    snapshot_hour: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    amount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sell_price: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    purchase_price: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    seller_profit: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    commission: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    logistics_fee: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


# ── FROM app.py:2694-2733 ───────────────────────────────────────────
def _save_hourly_snapshots(snap_hour: datetime) -> None:
    import datetime as _dt

    snap_hour_db = _app_naive(snap_hour)
    snapshot_day = (snap_hour - _dt.timedelta(seconds=1)).date()

    with SessionLocal() as db:
        snapshot_rows = db.execute(
            select(FinanceOrder).where(
                FinanceOrder.period_from == snapshot_day,
                FinanceOrder.period_to == snapshot_day,
            )
        ).scalars().all()

        cutoff = snap_hour_db - _dt.timedelta(hours=25)
        db.execute(
            delete(FinanceHourlySnapshot)
            .where(FinanceHourlySnapshot.snapshot_hour < cutoff)
        )
        db.execute(
            delete(FinanceHourlySnapshot)
            .where(FinanceHourlySnapshot.snapshot_hour == snap_hour_db)
        )

        payload = []
        for row in snapshot_rows:
            payload.append({
                "shop_id": row.shop_id,
                "sku_title": row.sku_title,
                "snapshot_hour": snap_hour_db,
                "amount": row.amount,
                "sell_price": row.sell_price,
                "purchase_price": row.purchase_price,
                "seller_profit": row.seller_profit,
                "commission": row.commission,
                "logistics_fee": row.logistics_fee,
            })
        if payload:
            db.execute(insert(FinanceHourlySnapshot), payload)
        db.commit()


# ─────────────────────────────────────────────────────────────────────
# STEP 6 (partial) — _sync_finance_for_shop migrated to SELLS_REPORT
# (no-regression fix; removed: <commit-pending>)
#
# Old body called legacy fetch_finance_sales_map() (paginated
# /finance/orders for last 30 days) and only populated Variant. The new
# version uses the live SELLS_REPORT pipeline and ALSO writes to
# sales_lines so the Finance page works within seconds of shop-add.
# fetch_finance_sales_map() itself is still in app.py — used by
# products/routes.py and debug_routes.py; full retirement is a
# follow-up.
# ─────────────────────────────────────────────────────────────────────


# ── FROM app.py:5270-5291 ───────────────────────────────────────────
def _sync_finance_for_shop(shop_uzum_id: str, shop_pk: int) -> dict:
    """Standalone finance-only sync. Returns {updated}."""
    api_key = _get_admin_token()
    sales_map = fetch_finance_sales_map(shop_uzum_id, api_key=api_key)
    if sales_map is None:
        raise RuntimeError("Не удалось получить данные о продажах.")
    updated = 0
    with SessionLocal() as db:
        variants = db.execute(
            select(Variant).join(ProductGroup).where(ProductGroup.shop_id == shop_pk)
        ).scalars().all()
        for v in variants:
            sku_key = (v.sku or "").strip()
            data = sales_map.get(sku_key) or sales_map.get(sku_key.upper())
            if data is None and v.barcode:
                bc_key = v.barcode.strip()
                data = sales_map.get(bc_key) or sales_map.get(bc_key.upper())
            v.sales_30d_finance = data["qty"] if data else 0
            v.avg_daily_sales = v.sales_30d_finance / 30.0
            updated += 1
        db.commit()
    return {"updated": updated}


# ─────────────────────────────────────────────────────────────────────
# STEP 6 (cont.) — products/routes.py sync trigger migrated to
# SELLS_REPORT pipeline (removed: <commit-pending>)
#
# Old code called legacy fetch_finance_sales_map(shop_id) (paginated
# /finance/orders for last 30 days) and read 5 fields per SKU
# (qty / price / sell_price / commission / logistics). Replaced with a
# single SELLS_REPORT call + per-SKU aggregation that yields the same
# {qty, price, sell_price, commission, logistics} map shape, plus
# writes to sales_lines so the Finance page is populated immediately.
# fetch_finance_sales_map() itself remains in app.py — used by
# debug_routes.py and the orphan burst-fetch path. Final removal is a
# separate coda cleanup.
# ─────────────────────────────────────────────────────────────────────


# ── FROM products/routes.py:463-467 (OLD fetch) ─────────────────────
            # Fetch 30-day sales stats for the shop
            sales_map = _app.fetch_finance_sales_map(shop_id, api_key=api_key)

            if sales_map is None:
                return _json_response({"error": "Не удалось получить данные о продажах (ошибка API). Проверьте ID магазина."}, 500)


# ── FROM products/routes.py:482-499 (variant loop — unchanged, kept ─
#    for reference of the {qty, price, sell_price, commission,
#    logistics} contract the new aggregator must satisfy) ───────────
            for v in variants:
                sku_key = (v.sku or "").strip()
                data = sales_map.get(sku_key) or sales_map.get(sku_key.upper())
                if data is None and v.barcode:
                    bc_key = v.barcode.strip()
                    data = sales_map.get(bc_key) or sales_map.get(bc_key.upper())

                qty_val = data["qty"] if data else 0
                v.sales_30d_finance = qty_val
                v.avg_daily_sales = qty_val / 30.0
                if data and data.get("price", 0) > 0:
                    v.purchase_price = data["price"]
                if data and data.get("sell_price", 0) > 0:
                    v.sell_price_uzum = int(data["sell_price"])
                if data and data.get("commission", 0) > 0:
                    v.commission_per_unit = int(data["commission"])
                if data and data.get("logistics", 0) > 0:
                    v.logistics_per_unit = int(data["logistics"])


# ─────────────────────────────────────────────────────────────────────
# STEP 3 — _finance_auto_refresh_loop + queues + dashboard
# (removed: <commit-pending>)
#
# Was the HH:00 background loop that hit /api/seller/finance/orders for
# each shop's auto_today / auto_recent / auto_backfill window and wrote
# results into FinanceOrder. The only readers were /admin/sales-diff
# (parity verification, removed in step 5). Loop stopped; manual sync
# helpers (_try_begin_finance_shop_sync etc.) kept for step 4.
# ─────────────────────────────────────────────────────────────────────


# ── FROM app.py:3237-3263 ───────────────────────────────────────────
def _finance_auto_refresh_loop():
    """Background thread: queue hourly finance refresh at HH:00 and drain it safely."""
    import time as _t

    next_hour = _now_app_tz().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

    while True:
        try:
            now = _now_app_tz()
            if not FINANCE_AUTO_REFRESH_ENABLED:
                _t.sleep(60)
                continue

            while now >= next_hour:
                _dispatch_finance_jobs(now_dt=next_hour)
                next_hour += timedelta(hours=1)

            _dispatch_finance_jobs(now_dt=now.replace(second=0, microsecond=0))

            sleep_seconds = min(
                FINANCE_AUTO_QUEUE_TICK_SECONDS,
                max(1.0, (next_hour - now).total_seconds()),
            )
            _t.sleep(sleep_seconds)
        except Exception as e:
            print(f"[FinanceAutoRefresh] Unexpected error: {e}")
            _t.sleep(60)


# ── FROM app.py:4194-4207 ───────────────────────────────────────────
def _dispatch_finance_jobs(*, now_dt: datetime | None = None) -> bool:
    """Queue hourly finance work at HH:00 and drain it safely with bounded concurrency."""
    if not FINANCE_AUTO_REFRESH_ENABLED:
        return False
    if not _get_admin_token():
        print("[FinanceAutoRefresh] Skipped: no admin Uzum token configured.")
        return False

    now_dt = now_dt or _now_app_tz().replace(second=0, microsecond=0)
    if now_dt.minute == 0:
        _enqueue_finance_jobs_for_hour(now_dt)

    _drain_finance_job_queue()
    return True


# ── FROM app.py:3989-4052 ───────────────────────────────────────────
def _enqueue_finance_jobs_for_hour(run_at: datetime) -> int:
    """Queue all finance refresh jobs that officially become due at HH:00."""
    global _finance_last_enqueue_hour

    hour_start = run_at.replace(minute=0, second=0, microsecond=0)
    hour_key = _finance_queue_hour_key(hour_start)

    with _finance_queue_lock:
        if _finance_last_enqueue_hour == hour_key:
            return 0
        _finance_last_enqueue_hour = hour_key

    shop_ids = _finance_rotated_shop_ids(_finance_auto_refresh_shop_ids(), hour_start)
    if not shop_ids:
        with _finance_stats_lock:
            _finance_stats["last_enqueue_time"] = hour_start.isoformat()
            _finance_stats["last_enqueue_hour"] = hour_key
            _finance_stats["last_enqueue_count"] = 0
            _finance_stats["pending_queue_size"] = len(_finance_pending_shop_ids)
        return 0

    due_counts = {"auto_today": 0, "auto_recent": 0, "auto_backfill": 0}
    enqueued = 0

    with _finance_queue_lock:
        for shop_id in shop_ids:
            windows = _scheduled_auto_finance_windows(shop_id, hour_start)
            if not windows:
                continue

            for sync_type, _, _ in windows:
                due_counts[sync_type] = due_counts.get(sync_type, 0) + 1

            entry = _finance_pending_jobs.get(shop_id)
            if entry:
                entry["windows"] = windows
                entry["queued_hour"] = hour_key
                entry["queued_at"] = hour_start.isoformat()
                continue

            _finance_pending_jobs[shop_id] = {
                "shop_id": shop_id,
                "windows": windows,
                "queued_hour": hour_key,
                "queued_at": hour_start.isoformat(),
            }
            _finance_pending_shop_ids.append(shop_id)
            enqueued += 1

        pending_size = len(_finance_pending_shop_ids)

    print(
        f"[FinanceAutoRefresh] {hour_start:%Y-%m-%d %H:%M}: queued {enqueued} shops at hour start — "
        f"today={due_counts['auto_today']}, recent={due_counts['auto_recent']}, backfill={due_counts['auto_backfill']}, "
        f"pending={pending_size}"
    )

    with _finance_stats_lock:
        _finance_stats["last_enqueue_time"] = hour_start.isoformat()
        _finance_stats["last_enqueue_hour"] = hour_key
        _finance_stats["last_enqueue_count"] = enqueued
        _finance_stats["pending_queue_size"] = pending_size

    return enqueued


# ── FROM app.py:4078-4149 ───────────────────────────────────────────
def _drain_finance_job_queue() -> int:
    """Submit queued finance jobs without overfilling executor capacity."""
    # Snapshot active shops once via a single SCAN instead of re-querying
    # Redis per pending job below.
    active_snapshot = set(shop_lock.active_shops())

    capacity = max(0, FINANCE_AUTO_REFRESH_WORKERS - len(active_snapshot))
    if capacity <= 0:
        with _finance_stats_lock:
            _finance_stats["last_drain_time"] = _now_app_tz().isoformat()
        return 0

    submissions: list[tuple[str, list[tuple[str, date, date]]]] = []
    with _finance_queue_lock:
        scan_limit = len(_finance_pending_shop_ids)
        while scan_limit > 0 and len(submissions) < capacity:
            shop_id = _finance_pending_shop_ids.popleft()
            entry = _finance_pending_jobs.get(shop_id)
            scan_limit -= 1

            if not entry:
                continue

            if shop_id in active_snapshot:
                _finance_pending_shop_ids.append(shop_id)
                continue

            submissions.append((shop_id, entry["windows"]))
            del _finance_pending_jobs[shop_id]
            active_snapshot.add(shop_id)

        pending_size = len(_finance_pending_shop_ids)

    if not submissions:
        with _finance_stats_lock:
            _finance_stats["last_drain_time"] = _now_app_tz().isoformat()
            _finance_stats["pending_queue_size"] = pending_size
        return 0

    print(
        f"[FinanceAutoRefresh] {_now_app_tz():%Y-%m-%d %H:%M:%S}: submitting {len(submissions)} queued shops "
        f"(pending={pending_size}, active={len(active_snapshot)})"
    )

    def _wrapped_shop_sync(shop_id, windows):
        """Run shop sync and update global stats."""
        try:
            results = _run_auto_finance_refresh_for_shop(shop_id, windows=windows)
            if results == [] and windows:
                _requeue_finance_job(shop_id, windows)
                return None

            with _finance_stats_lock:
                _finance_stats["total_completed"] += 1
            return results
        except Exception as e:
            print(f"[FinanceAutoRefresh] Shop {shop_id} failed: {e}")
            with _finance_stats_lock:
                _finance_stats["total_failed"] += 1
            return None

    for shop_id, windows in submissions:
        _finance_executor.submit(_wrapped_shop_sync, shop_id, windows)

    with _finance_stats_lock:
        _finance_stats["total_dispatched"] += len(submissions)
        _finance_stats["last_dispatch_time"] = _now_app_tz().isoformat()
        _finance_stats["last_dispatch_count"] = len(submissions)
        _finance_stats["last_drain_time"] = _now_app_tz().isoformat()
        _finance_stats["pending_queue_size"] = pending_size

    return len(submissions)


# ── FROM app.py:4152-4191 ───────────────────────────────────────────
def _run_auto_finance_refresh_for_shop(shop_id: str, *, windows: list[tuple[str, date, date]]) -> list[dict]:
    """Refresh the due finance windows for one shop."""
    if not windows:
        return []
    if not _try_begin_finance_shop_sync(shop_id):
        return []

    try:
        state = _prepare_finance_sync_state(shop_id)
        today = _today_app_tz()
        results: list[dict] = []

        # Auto-refresh seeds only the rolling window; full history stays manual.
        if state["needs_full_rebuild"] or state["existing"] <= 0:
            d_from, d_to = _recent_finance_refresh_window(today)
            total_fetched = _finance_sync_range(shop_id, d_from, d_to, "auto_seed")
            results.append({
                "shop_id": shop_id,
                "sync_type": "auto_seed",
                "date_from": d_from,
                "date_to": d_to,
                "records_fetched": total_fetched,
                "upgraded_to_daily": state["upgraded_to_daily"],
            })
            return results

        for sync_type, d_from, d_to in windows:
            total_fetched = _finance_sync_range(shop_id, d_from, d_to, sync_type)
            results.append({
                "shop_id": shop_id,
                "sync_type": sync_type,
                "date_from": d_from,
                "date_to": d_to,
                "records_fetched": total_fetched,
                "upgraded_to_daily": state["upgraded_to_daily"],
            })

        return results
    finally:
        _finish_finance_shop_sync(shop_id)


# ── FROM app.py:3946-3972 ───────────────────────────────────────────
def _scheduled_auto_finance_windows(shop_id: str, now_dt: datetime) -> list[tuple[str, date, date]]:
    """Return the finance windows due for this shop at the top of the hour."""
    if now_dt.minute != 0:
        return []

    today = now_dt.date()
    windows: list[tuple[str, date, date]] = []

    recent_due = (
        FINANCE_RECENT_DAYS > 1
        and (now_dt.hour % FINANCE_RECENT_REFRESH_HOURS) == _stable_slot(shop_id, FINANCE_RECENT_REFRESH_HOURS, "finance-recent-hour")
    )
    if recent_due:
        recent_start = today - timedelta(days=FINANCE_RECENT_DAYS - 1)
        windows.append(("auto_recent", recent_start, today))
    else:
        windows.append(("auto_today", today, today))

    if FINANCE_REFRESH_DAYS > FINANCE_RECENT_DAYS:
        backfill_hour = _stable_slot(shop_id, 24, "finance-backfill-hour")
        if now_dt.hour == backfill_hour:
            backfill_start = today - timedelta(days=FINANCE_REFRESH_DAYS - 1)
            backfill_end = today - timedelta(days=FINANCE_RECENT_DAYS)
            if backfill_start <= backfill_end:
                windows.append(("auto_backfill", backfill_start, backfill_end))

    return windows


# ── FROM app.py:3936-3943 ───────────────────────────────────────────
def _finance_auto_refresh_shop_ids() -> list[str]:
    """Auto-refresh every active shop that has an Uzum shop ID."""
    with SessionLocal() as db:
        return sorted({
            str(shop_id).strip()
            for shop_id in db.execute(select(Shop.uzum_id)).scalars().all()
            if str(shop_id or "").strip()
        })


# ── FROM app.py:3979-3986 ───────────────────────────────────────────
def _finance_rotated_shop_ids(shop_ids: list[str], run_at: datetime) -> list[str]:
    """Rotate queue order each hour so the same shops are not always first."""
    if not shop_ids:
        return []

    ordered = sorted(shop_ids)
    offset = int(run_at.timestamp() // 3600) % len(ordered)
    return ordered[offset:] + ordered[:offset]


# ── FROM app.py:3975-3976 ───────────────────────────────────────────
def _finance_queue_hour_key(run_at: datetime) -> str:
    return run_at.replace(minute=0, second=0, microsecond=0).isoformat()


# ── FROM app.py:4055-4075 ───────────────────────────────────────────
def _requeue_finance_job(shop_id: str, windows: list[tuple[str, date, date]]) -> None:
    """Return a shop to the pending queue if it lost the race to an overlapping sync."""
    if not windows:
        return

    with _finance_queue_lock:
        entry = _finance_pending_jobs.get(shop_id)
        if entry:
            entry["windows"] = windows
        else:
            _finance_pending_jobs[shop_id] = {
                "shop_id": shop_id,
                "windows": windows,
                "queued_hour": _finance_last_enqueue_hour,
                "queued_at": _now_app_tz().isoformat(),
            }
            _finance_pending_shop_ids.append(shop_id)
        pending_size = len(_finance_pending_shop_ids)

    with _finance_stats_lock:
        _finance_stats["pending_queue_size"] = pending_size


# ─────────────────────────────────────────────────────────────────────
# STEP 4 — Manual full-sync pipeline (removed: <commit-pending>)
#
# Was the legacy "Sync" / "Full sync" UI plus the 3 shop-add seed calls
# that ran _run_manual_sync_job → _finance_sync_range → daily-chunked
# /api/seller/finance/orders fetch → FinanceOrder. Replaced by the new
# pipeline: _onboarding_backfill_loop fills sales_lines from 2022→today,
# while _sync_finance_for_shop seeds the last 30d directly from
# SELLS_REPORT into both Variant and sales_lines.
# After step 4 nothing writes to FinanceOrder; step 5 drops the table.
# ─────────────────────────────────────────────────────────────────────


# ── FROM app.py:3936-3976 ───────────────────────────────────────────
def _run_manual_sync_job(job_id: str, shop_id: str, force_full: bool = False):
    """Background worker for manual / onboarding sync jobs."""
    _clean_old_sync_jobs()
    if not _try_begin_finance_shop_sync(shop_id):
        _update_sync_job(job_id, status="failed", error="sync already running for this shop",
                         finished_at=datetime.utcnow())
        return
    try:
        _update_sync_job(job_id, status="running")
        state = _prepare_finance_sync_state(shop_id)
        today = _today_app_tz()

        if force_full:
            with SessionLocal() as db:
                db.execute(delete(FinanceOrder).where(FinanceOrder.shop_id == shop_id))
                db.execute(delete(FinanceSyncLog).where(FinanceSyncLog.shop_id == shop_id))
                db.commit()
            sync_type = "full"
            d_from = date(2022, 1, 1)
            d_to = today
        elif state["existing"] > 0 and not state["needs_full_rebuild"]:
            sync_type = "refresh"
            d_from, d_to = _recent_finance_refresh_window(today)
        else:
            sync_type = "full"
            d_from = date(2022, 1, 1)
            d_to = today

        total_days = (d_to - d_from).days + 1
        _update_sync_job(job_id, sync_type=sync_type,
                         date_from=d_from.isoformat(), date_to=d_to.isoformat(),
                         total_days=total_days)

        total_fetched = _finance_sync_range(shop_id, d_from, d_to, sync_type, job_id=job_id)
        _update_sync_job(job_id, status="completed", records_fetched=total_fetched,
                         progress_days=total_days, finished_at=datetime.utcnow())
    except Exception as exc:
        _update_sync_job(job_id, status="failed", error=str(exc)[:500],
                         finished_at=datetime.utcnow())
    finally:
        _finish_finance_shop_sync(shop_id)


# ── FROM app.py:3982-4255 ───────────────────────────────────────────
def _finance_sync_range(shop_id: str, d_from: date, d_to: date, sync_type: str, job_id: str | None = None) -> int:
    """Fetch grouped finance data from Uzum API and save to DB.
    Uses group=true to get per-SKU aggregates.
    Fetches in daily chunks so cached rows can power day-by-day analytics.
    """
    from_ts_fn = _app_day_start_ts
    to_ts_fn = _app_day_end_ts
    auto_sync = sync_type.startswith("auto_")
    verbose_sync = str(os.environ.get("FINANCE_SYNC_VERBOSE", "0")).strip().lower() in ("1", "true", "yes")
    fetch_ru_titles = (
        str(os.environ.get("FINANCE_FETCH_RU_TITLES", "1")).strip().lower() in ("1", "true", "yes")
        and not auto_sync
    )

    total_fetched = 0

    # Build daily chunks so each stored record represents exactly one day.
    chunks = []
    current = d_from
    while current <= d_to:
        chunks.append((current, current))
        current += timedelta(days=1)

    def _load_cached_title_map() -> dict[str, dict]:
        with SessionLocal() as db:
            rows = db.execute(
                select(
                    FinanceOrder.product_id,
                    func.max(FinanceOrder.product_title_ru),
                    func.max(FinanceOrder.product_title),
                    func.max(FinanceOrder.image_url),
                )
                .where(
                    FinanceOrder.shop_id == shop_id,
                    FinanceOrder.product_id.is_not(None),
                )
                .group_by(FinanceOrder.product_id)
            ).all()
        return {
            str(product_id): {
                "product_title_ru": product_title_ru or "",
                "product_title": product_title or "",
                "image_url": image_url or "",
            }
            for product_id, product_title_ru, product_title, image_url in rows
            if product_id is not None
        }

    def _fetch_shop_ru_titles() -> dict[str, str]:
        """Fetch Russian product titles once per shop sync, not once per day."""
        if not fetch_ru_titles:
            return {}

        ru_titles: dict[str, str] = {}
        page = 0
        total_products = None
        collected = 0
        headers = {"Accept-Language": "ru-RU,ru;q=0.9", "x-language": "ru"}

        while True:
            url = f"https://api-seller.uzum.uz/api/seller/product?page={page}&size=100"
            try:
                raw = http_json(url, headers=headers)
            except Exception as e:
                print(f"[FinanceSync] RU title fetch page {page} failed for shop {shop_id}: {e}")
                break

            payload = raw.get("payload") or {}
            products = payload.get("products") or []
            if total_products is None:
                total_products = int(payload.get("totalProductAmount") or 0)

            for p in products:
                if str(p.get("shopId") or "").strip() != shop_id:
                    continue
                pid = str(p.get("id") or "").strip()
                title = str(p.get("title") or "").strip()
                if pid and title:
                    ru_titles[pid] = title

            collected += len(products)
            if not products or (total_products and collected >= total_products):
                break
            page += 1

        if verbose_sync:
            print(f"[FinanceSync] {shop_id}: loaded {len(ru_titles)} Russian product titles")
        return ru_titles

    cached_titles = _load_cached_title_map()
    shop_ru_titles = _fetch_shop_ru_titles()

    def _extract_order_items(resp):
        """Extract orderItems list from API response."""
        items = []
        if isinstance(resp, dict):
            for k in ["orderItems", "content", "items", "data", "orders"]:
                v = resp.get(k)
                if isinstance(v, list):
                    items = v
                    break
            if not items:
                p = resp.get("payload")
                if isinstance(p, list):
                    items = p
                elif isinstance(p, dict):
                    for k in ["orderItems", "content", "items", "data", "orders"]:
                        v = p.get(k)
                        if isinstance(v, list):
                            items = v
                            break
        elif isinstance(resp, list):
            items = resp
        return items

    def _fetch_chunk(chunk_start, chunk_end):
        """Fetch all pages for one daily chunk. Returns list of record dicts."""
        from_ts = from_ts_fn(chunk_start)
        to_ts = to_ts_fn(chunk_end)

        ru_titles = shop_ru_titles

        # Second pass: fetch with default (Uzbek) language — this is the main data
        records = []
        page = 0
        while True:
            url = (
                f"https://api-seller.uzum.uz/api/seller/finance/orders"
                f"?shopIds={shop_id}&dateFrom={from_ts}&dateTo={to_ts}"
                f"&group=true&page={page}&size=500"
            )
            try:
                resp = http_json(url)
            except Exception as e:
                print(f"[FinanceSync] Error page {page} for {chunk_start}-{chunk_end}: {e}")
                break

            items = _extract_order_items(resp)
            if not items:
                break

            for product_group in items:
                if not isinstance(product_group, dict):
                    continue
                group_pid = product_group.get("productId")
                group_title = product_group.get("productTitle", "")
                sku_items = product_group.get("items") or []
                if not sku_items:
                    sku_items = [product_group]
                for item in sku_items:
                    if not isinstance(item, dict):
                        continue
                    img_url = None
                    img = item.get("image") or item.get("productImage")
                    if isinstance(img, dict):
                        pk = img.get("photoKey", "")
                        if pk:
                            img_url = f"https://images.uzum.uz/{pk}/t_product_240_high.jpg"
                    chars = item.get("characteristics")
                    chars_str = ", ".join(str(c) for c in chars) if isinstance(chars, list) else None
                    pid = item.get("productId") or group_pid
                    cached_meta = cached_titles.get(str(pid or "")) or {}
                    base_product_title = item.get("productTitle") or group_title or cached_meta.get("product_title") or ""
                    records.append({
                        "shop_id": str(item.get("shopId", shop_id)),
                        "period_from": chunk_start,
                        "period_to": chunk_end,
                        "sku_title": item.get("skuTitle", ""),
                        "sku_id": item.get("skuId"),
                        "product_id": pid,
                        "product_title": base_product_title,
                        "product_title_ru": ru_titles.get(str(pid or "")) or cached_meta.get("product_title_ru") or base_product_title,
                        "image_url": img_url or cached_meta.get("image_url") or None,
                        "characteristics": chars_str,
                        "amount": item.get("amount", 0) or 0,
                        "amount_returns": item.get("amountReturns", 0) or 0,
                        "sell_price": item.get("sellPrice", 0) or 0,
                        "purchase_price": item.get("purchasePrice", 0) or 0,
                        "seller_discount": item.get("sellerDiscountAmount", 0) or 0,
                        "seller_profit": item.get("sellerProfit", 0) or 0,
                        "commission": item.get("commission", 0) or 0,
                        "withdrawn_profit": item.get("withdrawnProfit", 0) or 0,
                        "logistics_fee": item.get("logisticDeliveryFee", 0) or 0,
                    })

            if len(items) < 500:
                break
            page += 1
            if page > 100:
                break

        # Deduplicate by sku_title — API sometimes returns same SKU in multiple groups
        seen: dict[str, int] = {}
        deduped: list[dict] = []
        for rec in records:
            key = rec["sku_title"]
            if key in seen:
                # Keep the one with higher amount (more complete data)
                idx = seen[key]
                if rec["amount"] > deduped[idx]["amount"]:
                    deduped[idx] = rec
            else:
                seen[key] = len(deduped)
                deduped.append(rec)
        records = deduped

        if verbose_sync and (not auto_sync or sync_type != "auto_today"):
            print(f"[FinanceSync] {shop_id}: {chunk_start}: {len(records)} records")
        return chunk_start, chunk_end, records

    # Fetch chunks in parallel. Keep this configurable so multi-user deployments
    # can tune throughput vs. API pressure without changing code.
    max_workers = FINANCE_AUTO_SYNC_WORKERS_PER_SHOP if auto_sync else max(1, int(os.getenv("FINANCE_SYNC_WORKERS", "50")))
    total_fetched = 0
    total_chunks = len(chunks)

    # For large syncs (1 500+ days), write to DB every WRITE_BATCH days
    # instead of buffering everything in memory.
    WRITE_BATCH = 30
    batch_buffer: list[tuple[date, date, list[dict]]] = []
    days_completed = 0

    def _flush_batch(buffer):
        nonlocal total_fetched
        if not buffer:
            return
        with SessionLocal() as db:
            for chunk_start, chunk_end, records in buffer:
                db.execute(
                    delete(FinanceOrder).where(
                        FinanceOrder.shop_id == shop_id,
                        FinanceOrder.period_from == chunk_start,
                        FinanceOrder.period_to == chunk_end,
                    )
                )
                db.execute(insert(FinanceOrder), records)
            batch_count = sum(len(r) for _, _, r in buffer)
            total_fetched += batch_count
            db.commit()

    with ThreadPoolExecutor(max_workers=min(max_workers, max(1, len(chunks)))) as pool:
        futures = {pool.submit(_fetch_chunk, cs, ce): (cs, ce) for cs, ce in chunks}
        for future in as_completed(futures):
            chunk_start, chunk_end, records = future.result()
            days_completed += 1
            if records:
                batch_buffer.append((chunk_start, chunk_end, records))

            # Flush to DB every WRITE_BATCH completed chunks
            if len(batch_buffer) >= WRITE_BATCH:
                _flush_batch(batch_buffer)
                batch_buffer = []

            # Report progress for async jobs
            if job_id and days_completed % 10 == 0:
                _update_sync_job(job_id, progress_days=days_completed,
                                 records_fetched=total_fetched)

    # Flush remaining
    _flush_batch(batch_buffer)

    # Write sync log
    if total_fetched or True:  # always log, even if 0 records
        with SessionLocal() as db:
            db.add(FinanceSyncLog(
                shop_id=shop_id,
                sync_type=sync_type,
                date_from=d_from,
                date_to=d_to,
                records_fetched=total_fetched,
            ))
            db.commit()

    return total_fetched


# ── FROM app.py:3897-3933 ───────────────────────────────────────────
def _prepare_finance_sync_state(shop_id: str) -> dict:
    """Normalize legacy finance cache state before syncing."""
    state = {
        "existing": 0,
        "upgraded_to_daily": False,
        "needs_full_rebuild": False,
    }

    with SessionLocal() as db:
        existing = db.execute(
            select(func.count(FinanceOrder.id))
            .where(FinanceOrder.shop_id == shop_id)
        ).scalar() or 0
        state["existing"] = existing

        has_non_daily = False
        if existing > 0:
            non_daily_count = db.execute(
                select(func.count(FinanceOrder.id))
                .where(
                    FinanceOrder.shop_id == shop_id,
                    FinanceOrder.period_from != FinanceOrder.period_to,
                )
            ).scalar() or 0
            has_non_daily = non_daily_count > 0

        # Old period-spanning rows cannot be mixed safely with daily rows.
        # A partial refresh would otherwise double-count overlapping dates.
        if has_non_daily:
            db.execute(delete(FinanceOrder).where(FinanceOrder.shop_id == shop_id))
            db.execute(delete(FinanceSyncLog).where(FinanceSyncLog.shop_id == shop_id))
            db.commit()
            state["existing"] = 0
            state["upgraded_to_daily"] = True
            state["needs_full_rebuild"] = True

    return state


# ── FROM app.py:3875-3879 ───────────────────────────────────────────
def _recent_finance_refresh_window(reference_day: date | None = None) -> tuple[date, date]:
    """Return the inclusive rolling finance refresh window."""
    end_day = reference_day or _today_app_tz()
    start_day = end_day - timedelta(days=FINANCE_REFRESH_DAYS - 1)
    return start_day, end_day


# ── FROM app.py:205-215 ─────────────────────────────────────────────
def _create_sync_job(shop_id: str, sync_type: str) -> str:
    job_id = _uuid.uuid4().hex[:12]
    with SessionLocal() as db:
        db.add(SyncJob(
            job_id=job_id,
            shop_id=shop_id,
            sync_type=sync_type,
            status="queued",
        ))
        db.commit()
    return job_id


# ── FROM app.py:231-234 ─────────────────────────────────────────────
def _get_sync_job(job_id: str) -> dict | None:
    with SessionLocal() as db:
        job = db.get(SyncJob, job_id)
        return _serialize_sync_job(job) if job else None


# ── FROM app.py:237-243 ─────────────────────────────────────────────
def _list_sync_jobs(*, statuses: tuple[str, ...] | None = None) -> list[dict]:
    with SessionLocal() as db:
        stmt = select(SyncJob)
        if statuses:
            stmt = stmt.where(SyncJob.status.in_(statuses))
        stmt = stmt.order_by(desc(SyncJob.created_at))
        return [_serialize_sync_job(job) for job in db.execute(stmt).scalars().all()]


# ── FROM app.py:218-228 ─────────────────────────────────────────────
def _update_sync_job(job_id: str, **fields):
    if not fields:
        return
    with SessionLocal() as db:
        job = db.get(SyncJob, job_id)
        if not job:
            return
        for key, value in fields.items():
            if hasattr(job, key):
                setattr(job, key, value)
        db.commit()


# ── FROM app.py:246-256 ─────────────────────────────────────────────
def _clean_old_sync_jobs():
    """Remove completed/failed jobs older than 1 hour from shared storage."""
    cutoff = datetime.utcnow() - timedelta(hours=1)
    with SessionLocal() as db:
        db.execute(
            delete(SyncJob).where(
                SyncJob.finished_at.is_not(None),
                SyncJob.finished_at < cutoff,
            )
        )
        db.commit()


# ── FROM app.py:3882-3890 ───────────────────────────────────────────
def _try_begin_finance_shop_sync(shop_id: str) -> bool:
    """Guard against overlapping finance syncs for the same shop.

    Backed by a Redis SET NX EX lock (see core/shop_lock.py) so the guard
    is cluster-wide: two Gunicorn workers or two VPS nodes cannot both
    start a sync for the same shop. No process-local mutex is needed —
    Redis SET NX is atomic on the server.
    """
    return shop_lock.try_acquire_shop_lock(shop_id)


# ── FROM app.py:3893-3894 ───────────────────────────────────────────
def _finish_finance_shop_sync(shop_id: str) -> None:
    shop_lock.release_shop_lock(shop_id)


# ── FROM app.py:188-202 (helper used by sync-job CRUD) ──────────────
def _serialize_sync_job(job: SyncJob) -> dict:
    return {
        "job_id": job.job_id,
        "shop_id": job.shop_id,
        "sync_type": job.sync_type,
        "status": job.status,
        "progress_days": int(job.progress_days or 0),
        "total_days": int(job.total_days or 0),
        "records_fetched": int(job.records_fetched or 0),
        "date_from": job.date_from,
        "date_to": job.date_to,
        "error": job.error,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


# ── FROM models.py:398-413 ──────────────────────────────────────────
class SyncJob(Base):
    """Cross-worker async sync job state stored in the database."""
    __tablename__ = "sync_jobs"

    job_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    shop_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    sync_type: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued", index=True)
    progress_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_fetched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    date_from: Mapped[str | None] = mapped_column(String(20), nullable=True)
    date_to: Mapped[str | None] = mapped_column(String(20), nullable=True)
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)


# ── FROM templates/finance_sync.html (manual-sync UI page; deleted) ─
# Embedded as a Python comment block. The page rendered the per-shop
# "Sync" / "Full sync" buttons + progress polling against
# /api/finance/job-status. With step 4 the page is unreachable; the new
# pipeline runs automatically.
#
# """
# {% extends "base.html" %}
# {% block content %}
# <div class="d-flex justify-content-between align-items-center mb-3">
#   <h4 id="pageTitle">Финансы</h4>
# </div>
#
# <!-- Sync section -->
# <div class="card mb-4">
#   <div class="card-body">
#     <h6 id="syncTitle">Синхронизация данных</h6>
#     <div class="row g-3 align-items-end">
#       <div class="col-auto">
#         <label class="form-label small" id="lblShop">Магазин</label>
#         <select id="shopSelect" class="form-select form-select-sm">
#           {% for shop in shops %}
#           <option value="{{ shop.uzum_id }}"
#             data-last="{{ sync_info[shop.uzum_id].last_sync or '' }}"
#             data-total="{{ sync_info[shop.uzum_id].total_records }}">
#             {{ shop.name or shop.uzum_id }}
#           </option>
#           {% endfor %}
#         </select>
#       </div>
#       <div class="col-auto">
#         <button class="btn btn-sm btn-dark" id="btnSync" onclick="doSync()">
#           <span id="syncBtnText">Синхронизировать</span>
#         </button>
#       </div>
#       <div class="col-auto">
#         <button class="btn btn-sm btn-outline-danger" id="btnFullSync" onclick="doFullSync()">
#           <span id="fullSyncBtnText">Полная пересинхронизация</span>
#         </button>
#       </div>
#       <div class="col-auto">
#         <span class="badge bg-secondary" id="syncInfo"></span>
#       </div>
#     </div>
#     <div id="syncStatus" class="small text-muted mt-2" style="display:none"></div>
#   </div>
# </div>
#
# (... admin Hourly Burst card, filter section, totals, data table,
#  loading spinner, ~570 more lines of HTML + JS for shop select,
#  date filters, sync trigger buttons calling /api/finance/sync and
#  /api/finance/sync-full, status polling via /api/finance/job-status,
#  data load via /api/finance/data, Excel export via
#  /api/finance/export, and the burst dashboard refresh loop ...)
# {% endblock %}
# """

# ─────────────────────────────────────────────────────────────────────
# STEP 4 CORRECTION — finance_sync.html + /finance route partially
# restored after over-aggressive deletion. The live data-table + Excel
# export blocks were kept; only the legacy "Синхронизация данных" and
# "Hourly Burst" admin sections (and their JS) were trimmed. The
# previously-archived finance_sync.html note remains accurate for
# what was REMOVED, but the page itself still exists at /finance.
# ─────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────
# STEP 5 — FinanceOrder + FinanceSyncLog tables + /admin/sales-diff
# (removed: <commit-pending>)
#
# Final retirement of the legacy /finance/orders pipeline. The parity
# verification page /admin/sales-diff and its _compute_sales_diff helper
# go away with the FinanceOrder table they read. After this commit
# there is no comparison surface between the old and new pipelines —
# trust transferred to sales_lines / expenses_ledger.
# Note: fetch_finance_sales_map() is still in app.py (still used by
# debug_routes.py and the orphan _fetch_exact_hour_sales_for_all_shops
# burst path). Coda cleanup tracks that.
# ─────────────────────────────────────────────────────────────────────


# ── FROM models.py:314-339 ──────────────────────────────────────────
class FinanceOrder(Base):
    """Cached grouped finance data from Uzum seller API (group=true)."""
    __tablename__ = "finance_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shop_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    period_from: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    period_to: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    sku_title: Mapped[str] = mapped_column(String(300), nullable=False, index=True)
    sku_id: Mapped[int] = mapped_column(Integer, nullable=True, index=True)
    product_id: Mapped[int] = mapped_column(Integer, nullable=True, index=True)
    product_title: Mapped[str] = mapped_column(String(500), nullable=True)      # Uzbek
    product_title_ru: Mapped[str] = mapped_column(String(500), nullable=True)  # Russian
    image_url: Mapped[str] = mapped_column(String(800), nullable=True)
    characteristics: Mapped[str] = mapped_column(String(300), nullable=True)        # e.g. "20, Синий"
    amount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    amount_returns: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sell_price: Mapped[int] = mapped_column(Integer, nullable=False, default=0)      # total for period
    purchase_price: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # total for period
    seller_discount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    seller_profit: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    commission: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    withdrawn_profit: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    logistics_fee: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


# ── FROM models.py:342-352 ──────────────────────────────────────────
class FinanceSyncLog(Base):
    """Tracks when finance data was last synced per shop."""
    __tablename__ = "finance_sync_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shop_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    sync_type: Mapped[str] = mapped_column(String(20), nullable=False)  # "full" or "refresh"
    date_from: Mapped[date] = mapped_column(Date, nullable=False)
    date_to: Mapped[date] = mapped_column(Date, nullable=False)
    records_fetched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


# ── FROM admin/routes.py:587-616 (admin_sales_diff route handler) ───
@admin_bp.get("/admin/sales-diff")
@login_required
def admin_sales_diff():
    if not _current_user_is_admin():
        return _json_response({"error": "admin only"}, 403)

    shop_id = (request.args.get("shop_id") or "").strip()
    day_str = (request.args.get("day") or "").strip()

    diff = None
    error = None
    if shop_id and day_str:
        try:
            day = datetime.strptime(day_str, "%Y-%m-%d").date()
        except ValueError:
            error = "day must be YYYY-MM-DD"
            day = None
        if day is not None:
            try:
                diff = _compute_sales_diff(shop_id, day)
            except Exception as e:
                error = f"diff failed: {e}"

    return render_template(
        "admin_sales_diff.html",
        shop_id=shop_id,
        day=day_str,
        diff=diff,
        error=error,
    )


# ── FROM admin/routes.py:619-696 (_compute_sales_diff helper) ───────
def _compute_sales_diff(shop_id: str, day: date) -> dict:
    """Aggregate FinanceOrder (old) vs sales_lines (new) for a Tashkent day.

    FinanceOrder.created_at is UTC-naive; shift by +5h so the resulting
    "day" aligns with the Tashkent calendar day used by sales_lines. For the
    old path we also rely on FinanceOrder.period_from/period_to which are
    already day-keyed — that's the simpler aggregation.
    """
    shop_id_int: int | None
    try:
        shop_id_int = int(shop_id)
    except (TypeError, ValueError):
        shop_id_int = None

    # sales_lines — Tashkent window [day 00:00, day+1 00:00).
    tz_start = datetime.combine(day, dt_time(0, 0, 0))
    tz_end = datetime.combine(day + timedelta(days=1), dt_time(0, 0, 0))

    new_totals = {"rows": 0, "revenue": 0, "seller_profit": 0}
    if shop_id_int is not None:
        with SessionLocal() as db:
            row = db.execute(
                select(
                    func.count().label("rows"),
                    func.coalesce(func.sum(SalesLine.revenue), 0).label("revenue"),
                    func.coalesce(func.sum(SalesLine.seller_profit), 0).label("seller_profit"),
                )
                .where(SalesLine.shop_id == shop_id_int)
                .where(SalesLine.created_at >= tz_start)
                .where(SalesLine.created_at < tz_end)
            ).one()
            new_totals = {
                "rows": int(row.rows or 0),
                "revenue": int(row.revenue or 0),
                "seller_profit": int(row.seller_profit or 0),
            }

    # FinanceOrder — day-keyed via period_from/period_to (both = `day`).
    # FinanceOrder.shop_id is a string in this codebase.
    with SessionLocal() as db:
        row = db.execute(
            select(
                func.count().label("rows"),
                func.coalesce(func.sum(FinanceOrder.sell_price), 0).label("sell_price"),
                func.coalesce(func.sum(FinanceOrder.seller_profit), 0).label("seller_profit"),
            )
            .where(FinanceOrder.shop_id == shop_id)
            .where(FinanceOrder.period_from == day)
            .where(FinanceOrder.period_to == day)
        ).one()
        old_totals = {
            "rows": int(row.rows or 0),
            "revenue": int(row.sell_price or 0),
            "seller_profit": int(row.seller_profit or 0),
        }

    def _pct(new_val: int, old_val: int) -> float | None:
        if old_val == 0:
            return None
        return round(abs(new_val - old_val) * 100.0 / abs(old_val), 2)

    return {
        "shop_id": shop_id,
        "day": day.isoformat(),
        "tashkent_window": {
            "from": tz_start.isoformat(),
            "to": tz_end.isoformat(),
        },
        "old": old_totals,     # FinanceOrder (UTC-keyed, shifted +5h via period_from/to).
        "new": new_totals,     # sales_lines (Tashkent window).
        "delta": {
            "rows": new_totals["rows"] - old_totals["rows"],
            "revenue": new_totals["revenue"] - old_totals["revenue"],
            "seller_profit": new_totals["seller_profit"] - old_totals["seller_profit"],
            "revenue_pct": _pct(new_totals["revenue"], old_totals["revenue"]),
            "seller_profit_pct": _pct(new_totals["seller_profit"], old_totals["seller_profit"]),
        },
    }


# ── FROM templates/admin_sales_diff.html (parity diff page; deleted) ─
# Embedded as a Python triple-quoted string so it is archived but never
# parsed. The page rendered a side-by-side totals table comparing
# FinanceOrder (old, day-keyed) vs sales_lines (new, Tashkent-window) for
# a single shop+day, with delta/percent columns and a 1%/5% pass/warn
# threshold. With step 5 the page is unreachable; sales_lines is the
# sole source of truth.

_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Admin: Sales Diff (FinanceOrder vs sales_lines)</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
  <style>
    body { background: #f4f6fa; padding: 28px 0 60px; font-family: system-ui, sans-serif; }
    .card { border: 1px solid #e2e8f0; border-radius: 12px; box-shadow: 0 1px 3px rgba(0,0,0,.04); }
    .totals-table th { background: #f8fafc; color: #475467; font-size: 0.8rem; }
    .totals-table td { font-variant-numeric: tabular-nums; }
    .delta-positive { color: #16a34a; }
    .delta-negative { color: #dc2626; }
    .delta-zero { color: #64748b; }
    .note { color: #64748b; font-size: 0.82rem; }
    .pct-ok { color: #16a34a; font-weight: 600; }
    .pct-warn { color: #d97706; font-weight: 600; }
    .pct-bad { color: #dc2626; font-weight: 600; }
  </style>
</head>
<body>
  <div class="container" style="max-width: 960px;">
    <div class="card p-4 mb-4">
      <h1 class="h4 mb-1">Sales diff: FinanceOrder (old) vs sales_lines (new)</h1>
      <p class="note mb-3">
        Phase 1 parity soak tool. FinanceOrder is UTC-keyed and aggregated by
        <code>period_from = period_to = day</code>; sales_lines is queried by
        Tashkent window <code>[day 00:00, day+1 00:00)</code>.
        This page is deleted in Phase 4.
      </p>

      <form method="get" action="/admin/sales-diff" class="row g-2 align-items-end">
        <div class="col-sm-5">
          <label class="form-label">Shop uzum_id</label>
          <input type="text" name="shop_id" class="form-control" value="{{ shop_id or '' }}" placeholder="e.g. 5983" required>
        </div>
        <div class="col-sm-4">
          <label class="form-label">Day (Tashkent)</label>
          <input type="date" name="day" class="form-control" value="{{ day or '' }}" required>
        </div>
        <div class="col-sm-3">
          <button type="submit" class="btn btn-primary w-100">Diff</button>
        </div>
      </form>
    </div>

    {% if error %}
      <div class="alert alert-danger">{{ error }}</div>
    {% endif %}

    {% if diff %}
      <div class="card p-4">
        <h2 class="h6 mb-3">Totals — shop <code>{{ diff.shop_id }}</code>, day <code>{{ diff.day }}</code></h2>
        <p class="note mb-3">
          Tashkent window: <code>{{ diff.tashkent_window.from }}</code> → <code>{{ diff.tashkent_window.to }}</code>
        </p>
        <table class="table totals-table mb-0">
          <thead>
            <tr>
              <th>Metric</th>
              <th class="text-end">Old (FinanceOrder)</th>
              <th class="text-end">New (sales_lines)</th>
              <th class="text-end">Delta</th>
              <th class="text-end">Delta %</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>Row count</td>
              <td class="text-end">{{ diff.old.rows }}</td>
              <td class="text-end">{{ diff.new.rows }}</td>
              <td class="text-end {% if diff.delta.rows == 0 %}delta-zero{% elif diff.delta.rows > 0 %}delta-positive{% else %}delta-negative{% endif %}">
                {{ diff.delta.rows }}
              </td>
              <td class="text-end note">—</td>
            </tr>
            <tr>
              <td>Revenue (сумы)</td>
              <td class="text-end">{{ diff.old.revenue }}</td>
              <td class="text-end">{{ diff.new.revenue }}</td>
              <td class="text-end {% if diff.delta.revenue == 0 %}delta-zero{% elif diff.delta.revenue > 0 %}delta-positive{% else %}delta-negative{% endif %}">
                {{ diff.delta.revenue }}
              </td>
              <td class="text-end
                {% if diff.delta.revenue_pct is none %}note
                {% elif diff.delta.revenue_pct < 1 %}pct-ok
                {% elif diff.delta.revenue_pct < 5 %}pct-warn
                {% else %}pct-bad{% endif %}">
                {% if diff.delta.revenue_pct is none %}—{% else %}{{ diff.delta.revenue_pct }}%{% endif %}
              </td>
            </tr>
            <tr>
              <td>Seller profit (сумы)</td>
              <td class="text-end">{{ diff.old.seller_profit }}</td>
              <td class="text-end">{{ diff.new.seller_profit }}</td>
              <td class="text-end {% if diff.delta.seller_profit == 0 %}delta-zero{% elif diff.delta.seller_profit > 0 %}delta-positive{% else %}delta-negative{% endif %}">
                {{ diff.delta.seller_profit }}
              </td>
              <td class="text-end
                {% if diff.delta.seller_profit_pct is none %}note
                {% elif diff.delta.seller_profit_pct < 1 %}pct-ok
                {% elif diff.delta.seller_profit_pct < 5 %}pct-warn
                {% else %}pct-bad{% endif %}">
                {% if diff.delta.seller_profit_pct is none %}—{% else %}{{ diff.delta.seller_profit_pct }}%{% endif %}
              </td>
            </tr>
          </tbody>
        </table>
        <p class="note mt-3 mb-0">
          Parity target: <code>|new − old| / old &lt; 1%</code> for revenue and profit; row-count delta &le; 2.
        </p>
      </div>
    {% endif %}
  </div>
</body>
</html>
"""
