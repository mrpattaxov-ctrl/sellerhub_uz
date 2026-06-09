"""legacy/sales_lines_pipeline.py — archived 2026-05-23

Plain-text archive of the legacy SELLS_REPORT CSV pipeline, browser-token
finance fetcher, and the per-line sales_lines hourly/nightly/backfill loops
that were retired in favour of the OpenAPI finance_orders + FinanceHourly-
Snapshot pipeline.

This file is NOT imported anywhere. It's preserved as readable history so
a future maintainer can recover the implementation without git-archaeology.

Categories archived:
  1. Browser-token finance fetcher (fetch_finance_sales_map)
  2. Old SELLS_REPORT pipeline (hourly/nightly/onboarding loops + ingest)
  3. CSV fallback wrapper (_fetch_finance_data_for_shop) + driver
     (_fetch_sells_report_rows_for_shops)
  4. Browser products sync (_sync_products_for_shop) — depended on
     fetch_finance_sales_map
  5. Per-line OpenAPI fetcher (fetch_finance_orders_for_shop_window)
     from core/uzum_finance_openapi.py — only called by the CSV fallback
  6. Orphan _fetch_exact_hour_sales_for_all_shops
"""

# ─────────────────────────────────────────────────────────────────
# From app.py
# ─────────────────────────────────────────────────────────────────

# ── fetch_finance_sales_map ───────────────────────────────
def fetch_finance_sales_map(shop_id, api_key=None, days=30, date_from_ts=None, date_to_ts=None):
    """Helper to fetch sales from finance API for a given date range.
    Returns dict if successful, or None if API failed.
    """
    now_dt = _now_app_tz()
    now_ts = date_to_ts or int(now_dt.timestamp())
    past_ts = date_from_ts or int((now_dt - timedelta(days=days)).timestamp())
    
    auth_val = (api_key if api_key.startswith("Bearer ") else f"Bearer {api_key}") if api_key else None
    headers = {
        "Authorization": auth_val,
        "Origin": "https://seller.uzum.uz",
        "Referer": "https://seller.uzum.uz/",
    } if auth_val else None

    def _loop(param_name):
        local_map = {}  # {identifier: {"qty": int, "price": int}}
        s_page = 0
        success = False
        while True:
            s_url = f"https://api-seller.uzum.uz/api/seller/finance/orders?{param_name}={shop_id}&dateFrom={past_ts}&dateTo={now_ts}&group=true&page={s_page}&size=500"
            try:
                s_res = http_json(s_url, headers=headers)
                success = True
            except Exception as e:
                print(f"Finance fetch error ({param_name}): {e}")
                return None
            s_items = find_first_array(s_res, ["payload", "result", "content", "data", "items", "rows", "list", "orders", "orderItems"]) or []
            if not isinstance(s_items, list) or not s_items:
                break
            for row in s_items:
                # Check if row is an order containing items
                sub_items = find_first_array(row, ["items", "products", "rows", "lines", "positions", "orderItems"])
                loop_items = sub_items if sub_items else [row]

                for item in loop_items:
                    # Extract all possible identifiers to ensure a match
                    # Check item itself AND nested 'product' object for identifiers
                    sources = [item]
                    if "product" in item and isinstance(item["product"], dict):
                        sources.append(item["product"])
                    if "sku" in item and isinstance(item["sku"], dict):
                        sources.append(item["sku"])
                    
                    identifiers = set()
                    
                    for src in sources:
                        # 1. Try standard extraction
                        s_sku = _extract_sku(src)
                        if s_sku: identifiers.add(s_sku)
                        
                        # 2. Explicitly grab other common keys (including skuTitle as requested)
                        for k in ["skuFullTitle", "skuTitle", "sku", "offerId", "shopSku", "sellerSku", "barcode", "ean", "skuId", "id"]:
                            val = str(pick(src, [k], default="") or "").strip()
                            if val: identifiers.add(val)

                    # Qty extraction: Priority on explicit quantity keys
                    # We include "amount" as requested for finance data
                    q_keys = [
                        "amount", "quantity", "qty", "count", "productCount", "itemsCount", "itemCount",
                        "sold", "sales", "totalCount", "totalQuantity", "quantityToStock"
                    ]
                    
                    q_val = pick(item, q_keys)
                    if q_val is None and "product" in item and isinstance(item["product"], dict):
                        q_val = pick(item["product"], q_keys)
                    if q_val is None and "sku" in item and isinstance(item["sku"], dict):
                        q_val = pick(item["sku"], q_keys)

                    s_qty = int(_safe_qty(q_val or 0))

                    # sellPrice — actual sale price total for this order line
                    sell_val = pick(item, ["sellPrice", "sell_price", "salePrice", "price"])
                    s_sell_price = 0
                    if sell_val is not None:
                        try: s_sell_price = float(sell_val)
                        except: pass

                    # commission — total commission for this order line
                    comm_val = pick(item, ["commission", "commissionFee", "uzumCommission"])
                    s_commission = 0
                    if comm_val is not None:
                        try: s_commission = float(comm_val)
                        except: pass

                    # logisticDeliveryFee — total logistics fee for this order line
                    logi_val = pick(item, ["logisticDeliveryFee", "deliveryFee", "logisticFee", "logistics"])
                    s_logistics = 0
                    if logi_val is not None:
                        try: s_logistics = float(logi_val)
                        except: pass

                    # storageFee / warehouse cost
                    store_val = pick(item, [
                        "storageFee", "storage_fee", "warehouseFee", "warehouse_fee",
                        "storagePrice", "fulfillmentFee", "fulfillment_fee",
                        "keepingFee", "keeping_fee",
                    ])
                    if store_val is None and "product" in item and isinstance(item["product"], dict):
                        store_val = pick(item["product"], ["storageFee", "warehouseFee", "fulfillmentFee", "keepingFee"])
                    s_storage = 0
                    if store_val is not None:
                        try: s_storage = float(store_val)
                        except: pass

                    # purchasePrice (cost price)
                    p_val = pick(item, ["purchasePrice", "purchase_price"])
                    if p_val is None and "product" in item and isinstance(item["product"], dict):
                        p_val = pick(item["product"], ["purchasePrice", "purchase_price"])
                    if p_val is None and "sku" in item and isinstance(item["sku"], dict):
                        p_val = pick(item["sku"], ["purchasePrice", "purchase_price"])
                    s_price = 0
                    if p_val is not None:
                        try: s_price = float(p_val)
                        except: pass

                    # sellerProfit
                    sp_val = pick(item, ["sellerProfit", "seller_profit", "profit"])
                    if sp_val is None and "product" in item and isinstance(item["product"], dict):
                        sp_val = pick(item["product"], ["sellerProfit", "seller_profit", "profit"])
                    s_seller_profit = 0
                    if sp_val is not None:
                        try: s_seller_profit = float(sp_val)
                        except: pass

                    if s_qty > 0:
                        keys_to_update = set()
                        for ident in identifiers:
                            keys_to_update.add(ident)
                            keys_to_update.add(ident.upper())

                        for k in keys_to_update:
                            if k not in local_map:
                                local_map[k] = {"qty": 0, "price": 0,
                                                "sell_price": 0, "commission": 0,
                                                "logistics": 0, "seller_profit": 0,
                                                "storage": 0}
                            local_map[k]["qty"] += s_qty
                            # Per-unit values (divide totals by amount)
                            if s_price > 0:
                                local_map[k]["price"] = int(s_price / s_qty)
                            if s_sell_price > 0:
                                local_map[k]["sell_price"] = int(s_sell_price / s_qty)
                            if s_commission > 0:
                                local_map[k]["commission"] = int(s_commission / s_qty)
                            if s_logistics > 0:
                                local_map[k]["logistics"] = int(s_logistics / s_qty)
                            if s_seller_profit > 0:
                                local_map[k]["seller_profit"] = int(s_seller_profit / s_qty)
                            if s_storage > 0:
                                local_map[k]["storage"] = int(s_storage / s_qty)
            if len(s_items) < 500: break
            s_page += 1
            if s_page > 50: break
        return local_map if success else None

    sales_map = _loop("shopIds")
    
    # If error (None) or empty, try fallback parameter
    if sales_map is None or not sales_map:
        fallback = _loop("shopId")
        if fallback is not None:
            # If fallback succeeded (even if empty), use it
            sales_map = fallback
            
    return sales_map


# ── _fetch_exact_hour_sales_for_all_shops ───────────────────────────────
def _fetch_exact_hour_sales_for_all_shops(
    shop_ids: list[str],
    *,
    api_key: str,
    date_from_ts: int,
    date_to_ts: int,
    track_stats: bool = True,
) -> tuple[dict[str, dict], dict[str, str]]:
    """Fetch exact previous-hour finance data for every shop in parallel."""
    if not HOURLY_SALES_BURST_FETCH_ENABLED or not shop_ids:
        return {}, {}

    started_dt = _now_app_tz()
    started_at = time.monotonic()
    if track_stats:
        _mark_hourly_burst_started(
            started_at=started_dt,
            shop_ids=shop_ids,
            date_from_ts=date_from_ts,
            date_to_ts=date_to_ts,
        )
    workers = min(HOURLY_SALES_BURST_FETCH_WORKERS, max(1, len(shop_ids)))
    results: dict[str, dict] = {}
    errors: dict[str, str] = {}

    def _task(shop_id: str):
        try:
            hour_map = fetch_finance_sales_map(
                shop_id,
                api_key=api_key,
                date_from_ts=date_from_ts,
                date_to_ts=date_to_ts,
            ) or {}
            return shop_id, hour_map, None
        except Exception as exc:
            return shop_id, {}, str(exc)

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_task, shop_id): shop_id for shop_id in shop_ids}
            for future in as_completed(futures):
                shop_id, hour_map, err = future.result()
                if err:
                    errors[shop_id] = err
                else:
                    results[shop_id] = hour_map
    except Exception as exc:
        errors["system"] = str(exc)
        if track_stats:
            _mark_hourly_burst_finished(
                started_monotonic=started_at,
                finished_at=_now_app_tz(),
                total_shops=len(shop_ids),
                results=results,
                errors=errors,
                status="failed",
            )
        raise

    elapsed = time.monotonic() - started_at
    if track_stats:
        _mark_hourly_burst_finished(
            started_monotonic=started_at,
            finished_at=_now_app_tz(),
            total_shops=len(shop_ids),
            results=results,
            errors=errors,
            status="partial_failure" if errors else "completed",
        )
    print(
        f"[HourlySales] Exact-hour burst: shops={len(shop_ids)} workers={workers} "
        f"ok={len(results)} failed={len(errors)} elapsed={elapsed:.2f}s"
    )
    return results, errors


# ── _fetch_sales_for_shop_window ───────────────────────────────
def _fetch_sales_for_shop_window(
    shop_uzum_id: str | int,
    date_from_tashkent: datetime,
    date_to_tashkent: datetime,
) -> list[dict]:
    """Single-shop sales fetch — OpenAPI primary, browser CSV fallback.

    Returns canonical-key dicts (same shape as _SELLS_HEADER_MAP output) so
    the existing _ingest_sales_lines_window function consumes both sources
    uniformly. OpenAPI rows carry the extra keys `shop_id`, `product_image`,
    `qty_cancelled` and `product_id` which the ingest writes to the new
    sales_lines columns added in migration 20260520_0004.
    """
    token = _owner_openapi_token_for_shop(shop_uzum_id)
    if token:
        from core import uzum_finance_openapi as _ufo
        try:
            rows = _ufo.fetch_finance_orders_for_shop_window(
                token, shop_uzum_id, date_from_tashkent, date_to_tashkent,
            )
            print(f"[SalesFetch] OpenAPI shop={shop_uzum_id} rows={len(rows)}")
            return rows
        except Exception as e:
            print(f"[SalesFetch] OpenAPI failed shop={shop_uzum_id}: {e!r} — falling back to CSV")
    return _fetch_sells_report_rows_for_shops(
        [int(shop_uzum_id)], date_from_tashkent, date_to_tashkent,
    )


# ── _fetch_sells_report_rows_for_shops ───────────────────────────────
def _fetch_sells_report_rows_for_shops(
    shop_ids: list[int],
    date_from_tashkent: datetime,
    date_to_tashkent: datetime,
) -> list[dict]:
    """Run the Uzum 4-step SELLS_REPORT flow for ``shop_ids`` and return rows.

    Used by the hourly bulk loop AND the per-shop nightly / backfill / sync
    paths (latter pass ``[shop_id]``). Concurrent same-token creates are the
    cross-wired-fileUrl race (see project_uzum_race_bug_fix.md) — callers
    must serialize / chunk. Routing to per-shop rows happens at ingest time
    via ``Variant.sku → ProductGroup.shop_id``.
    """
    from core import uzum_reports as _ur

    def _to_ms(dt: datetime) -> int:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=APP_TZ)
        return int(dt.timestamp() * 1000)

    date_from_ms = _to_ms(date_from_tashkent)
    # dateTo is exclusive on our side; Uzum treats it inclusive-ish — subtract
    # 1 ms so re-run on the same [a,b) window doesn't pull the next bucket's
    # first millisecond.
    date_to_ms = _to_ms(date_to_tashkent) - 1

    request_id = _ur.create_report(
        shop_ids,
        "SELLS_REPORT",
        date_from_ms,
        date_to_ms,
        token_getter=_get_admin_token,
    )
    file_url = _ur.wait_for_report(request_id, token_getter=_get_admin_token)
    raw = _ur.download_csv(file_url, token_getter=_get_admin_token)
    return _ur.parse_sells_csv(raw)


# ── _run_hourly_bulk_chunk ───────────────────────────────
def _run_hourly_bulk_chunk(
    chunk_shop_ids: list[int],
    hour_start: datetime,
    hour_end: datetime,
) -> bool:
    """Hourly fetch for ``chunk_shop_ids``, per-shop with OpenAPI primary.

    Path C's bulk SELLS_REPORT was a workaround for the cross-wired-fileUrl
    race specific to the shared admin token (see project_uzum_race_bug_fix).
    Per-user OpenAPI tokens have their own rate-limit buckets per token, so
    that race doesn't apply — we can fan out per-shop concurrently here.
    Shops whose owner hasn't set an OpenAPI token transparently fall back
    to per-shop SELLS_REPORT inside ``_fetch_sales_for_shop_window``.

    Returns True if at least one shop's fetch+ingest succeeded.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    held: list[str] = []
    try:
        for sid in chunk_shop_ids:
            sid_str = str(sid)
            if shop_lock.try_acquire_shop_lock(sid_str):
                held.append(sid_str)
        if not held:
            print(f"[SalesHourly] chunk size={len(chunk_shop_ids)} skipped — no locks acquired")
            return False

        # Fan out per-shop fetches. Workers bounded so we don't stampede
        # any single owner's 2-burst rate budget if many of their shops
        # land in the same chunk.
        all_rows: list[dict] = []
        success_shops: list[str] = []

        def _one(sid_str: str):
            try:
                return sid_str, _fetch_sales_for_shop_window(sid_str, hour_start, hour_end), None
            except Exception as exc:
                return sid_str, [], exc

        max_workers = max(1, min(16, len(held)))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futs = [pool.submit(_one, s) for s in held]
            for fut in as_completed(futs):
                sid_str, rows, err = fut.result()
                if err is not None:
                    print(f"[SalesHourly] fetch ERROR shop={sid_str}: {err!r}")
                    continue
                success_shops.append(sid_str)
                all_rows.extend(rows or [])

        if not success_shops:
            print(f"[SalesHourly] chunk size={len(held)} all fetches failed")
            return False

        try:
            n = _ingest_sales_lines_window(all_rows, hour_start, hour_end)
        except Exception as e:
            print(f"[SalesHourly] chunk size={len(success_shops)} ingest ERROR: {e}")
            return False

        for sid_str in success_shops:
            try:
                _update_shop_sync_state(int(sid_str), last_hourly_at=datetime.utcnow())
            except Exception as e:
                print(f"[SalesHourly] sync_state update failed shop={sid_str}: {e}")

        print(
            f"[SalesHourly] chunk shops={len(success_shops)} "
            f"window=[{hour_start.isoformat()},{hour_end.isoformat()}) rows={n}"
        )
        return True
    finally:
        for sid_str in held:
            shop_lock.release_shop_lock(sid_str)


# ── _hourly_sales_reports_loop ───────────────────────────────
# Window strategy: at every HH:00 we re-fetch the FULL day-so-far
# ([00:00, HH:00)), not just the closed hour. Reason: an order fetched
# earlier today as 'В обработке' may flip to 'Отменен' later in Uzum;
# a narrow [HH-1, HH) window would never see that flip and the stale
# row would sit until the 00:30 nightly refetch (~24h drift).
# _ingest_sales_lines_window does DELETE+INSERT scoped to the window
# AND the shops present in the rows-set. Cancelled rows DO appear in
# the CSV (status='Отменен'), so the cancelled order's shop is in
# rows-set, the DELETE wipes today's stale rows for that shop, and
# INSERT proceeds with cancellations filtered out (coder rule §2).
# At the midnight tick we still wrap up the previous full day in one
# 24h window, then resume today-so-far at 01:00.
def _hourly_sales_reports_loop():
    """HH:00 Tashkent — bulk-fetch today's day-so-far for ALL active shops.

    Window semantics:
      * Mid-day tick (HH != 0): ``[today 00:00, today HH:00)`` — re-fetch
        all of today so cancellations on earlier-hour orders self-correct.
      * Midnight tick (HH == 0): ``[yesterday 00:00, today 00:00)`` — wrap
        up the previous full day's truth in one 24h window.

    Path C: replaces the per-shop ThreadPoolExecutor fan-out (cross-wired
    fileUrl race; see project_uzum_race_bug_fix.md). Issues one
    ``create_report(shop_ids=chunk)`` per chunk of ``SHARD_BY_K`` shops,
    sequentially, with ``_BULK_CREATE_BETWEEN_CHUNKS_S`` between chunks to
    dodge Uzum's 60s exact-args dedup. Per-row routing happens at ingest.
    Status drift is corrected by ``_ingest_sales_lines_window``'s
    DELETE+INSERT, scoped to (window, shops-in-rows-set).
    """
    import time as _t

    next_hour = _now_app_tz().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

    while True:
        try:
            now = _now_app_tz()
            sleep_seconds = (next_hour - now).total_seconds()
            if sleep_seconds > 0:
                _t.sleep(sleep_seconds)

            while _now_app_tz() >= next_hour:
                snap_hour_tz = next_hour  # save TZ-aware boundary BEFORE we mutate
                hour_end = next_hour.replace(tzinfo=None)
                if hour_end.hour == 0:
                    # Midnight tick — wrap up the previous full day.
                    hour_start = hour_end - timedelta(days=1)
                else:
                    # Mid-day tick — re-fetch from today 00:00 so cancellations
                    # on earlier-hour orders get caught (status drift correction).
                    hour_start = datetime.combine(hour_end.date(), dt_time(0, 0, 0))
                shops = _active_shop_ids_for_sales()
                bulk_ok = True
                if shops:
                    try:
                        # Convert to ints, drop unparseable.
                        shop_ints: list[int] = []
                        for s in shops:
                            try:
                                shop_ints.append(int(str(s).strip()))
                            except (TypeError, ValueError):
                                continue

                        # Chunk into SHARD_BY_K groups. Sequential — never concurrent
                        # under the same token (race condition).
                        chunks = [
                            shop_ints[i:i + SHARD_BY_K]
                            for i in range(0, len(shop_ints), SHARD_BY_K)
                        ]
                        print(
                            f"[SalesHourly] bulk window=[{hour_start.isoformat()},"
                            f"{hour_end.isoformat()}) shops={len(shop_ints)} "
                            f"chunks={len(chunks)} shard_by_k={SHARD_BY_K}"
                        )
                        for idx, chunk in enumerate(chunks):
                            if not _run_hourly_bulk_chunk(chunk, hour_start, hour_end):
                                bulk_ok = False
                            if idx < len(chunks) - 1:
                                _t.sleep(_BULK_CREATE_BETWEEN_CHUNKS_S)
                    except Exception as e:
                        bulk_ok = False
                        print(
                            f"[SalesHourly] bulk fetch failed for "
                            f"{hour_end.isoformat()}: {e!r}"
                        )

                # Dispatch the per-user Telegram notification for this closed hour.
                # Folded in here (was a separate _hourly_sales_loop thread) so the
                # TG read happens AFTER the bulk INSERT completes — fixes the
                # HH:00 race where TG reported stale/empty sales_lines.
                # Gated on bulk_ok: if bulk failed, sales_lines holds prior data
                # (or nothing); dispatching would re-send stale numbers, which is
                # the exact bug we're fixing. Skip TG for that hour and log.
                # Pass the TZ-aware boundary — _run_scheduled_hourly_sales_check
                # uses .hour for per-user gating (smoke-test verified).
                if bulk_ok:
                    try:
                        _run_scheduled_hourly_sales_check(snap_hour_tz)
                    except Exception as e:
                        print(
                            f"[SalesHourly] Telegram dispatch failed for "
                            f"{snap_hour_tz.isoformat()}: {e!r}"
                        )
                else:
                    print(
                        f"[SalesHourly] skipping Telegram dispatch for "
                        f"{snap_hour_tz.isoformat()} — bulk fetch failed"
                    )
                next_hour += timedelta(hours=1)
        except Exception as e:
            print(f"[SalesHourly] Unexpected error: {e}")
            _t.sleep(60)


# ── _run_nightly_refetch_for_shop ───────────────────────────────
def _run_nightly_refetch_for_shop(shop_id: str, today_tashkent: date) -> None:
    """ONE API call per shop for [today-45d, today-1d]. NOT chunked (coder rule §6).

    Path C: single-shop is just a 1-element bulk call — same code path. The
    nightly loop already runs shops sequentially (see ``_nightly_refetch_loop``),
    so the same-token race is not triggered.
    """
    if not shop_lock.try_acquire_shop_lock(shop_id):
        return
    try:
        start_day = today_tashkent - timedelta(days=FINANCE_REFRESH_DAYS)
        end_day = today_tashkent - timedelta(days=1)
        window_from = datetime.combine(start_day, dt_time(0, 0, 0))
        # End-of-day boundary for end_day — [start_day 00:00, today 00:00).
        window_to = datetime.combine(today_tashkent, dt_time(0, 0, 0))
        rows = _fetch_sales_for_shop_window(shop_id, window_from, window_to)
        n = _ingest_sales_lines_window(rows, window_from, window_to)
        _update_shop_sync_state(int(shop_id), last_nightly_refetch_at=datetime.utcnow())
        print(f"[SalesNightly] shop={shop_id} window=[{window_from.date()},{end_day}] rows={n}")
    except Exception as e:
        print(f"[SalesNightly] shop={shop_id} ERROR: {e}")
    finally:
        shop_lock.release_shop_lock(shop_id)


# ── _nightly_refetch_loop ───────────────────────────────
def _nightly_refetch_loop():
    """00:30 Tashkent — per shop, ONE API call covering the full 45-day range."""
    import time as _t

    def _next_run() -> datetime:
        now = _now_app_tz()
        target = now.replace(hour=0, minute=30, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        return target

    next_run = _next_run()
    while True:
        try:
            now = _now_app_tz()
            sleep_seconds = (next_run - now).total_seconds()
            if sleep_seconds > 0:
                _t.sleep(sleep_seconds)

            today_tashkent = _now_app_tz().date()
            for s in _active_shop_ids_for_sales():
                # Run sequentially — nightly is big (~5MB CSV) and we don't
                # want to stampede Uzum. Each call still hits shop_lock.
                _run_nightly_refetch_for_shop(s, today_tashkent)
            next_run = _next_run()
        except Exception as e:
            print(f"[SalesNightly] Unexpected error: {e}")
            _t.sleep(60)


# ── _enqueue_backfill_chunks_for_shop ───────────────────────────────
def _enqueue_backfill_chunks_for_shop(shop_id_int: int, today_tashkent: date) -> int:
    """Seed shop_backfill_chunks rows covering [FINANCE_BACKFILL_START_DATE, today].

    Last chunk ends at ``today`` (NOT today-45d) per plan §5 rationale —
    overlap with nightly is safe (DELETE+INSERT + shop_lock). Returns the
    number of chunks newly inserted.
    """
    from config import SALES_BACKFILL_CHUNK_DAYS
    from models import ShopBackfillChunk, ShopSyncState

    start = _parse_backfill_start_date()
    if start > today_tashkent:
        return 0

    chunks: list[tuple[date, date]] = []
    cur = start
    while cur <= today_tashkent:
        end = min(cur + timedelta(days=SALES_BACKFILL_CHUNK_DAYS - 1), today_tashkent)
        chunks.append((cur, end))
        cur = end + timedelta(days=1)

    added = 0
    with SessionLocal() as db:
        with db.begin():
            existing_rows = db.execute(
                select(ShopBackfillChunk.chunk_start, ShopBackfillChunk.chunk_end)
                .where(ShopBackfillChunk.shop_id == shop_id_int)
            ).all()
            existing = {(cs, ce) for cs, ce in existing_rows}
            for cs, ce in chunks:
                if (cs, ce) in existing:
                    continue
                db.add(ShopBackfillChunk(
                    shop_id=shop_id_int,
                    chunk_start=cs,
                    chunk_end=ce,
                    status="pending",
                ))
                added += 1

            state = db.get(ShopSyncState, shop_id_int)
            if state is None:
                db.add(ShopSyncState(
                    shop_id=shop_id_int,
                    backfill_status="running" if added else "done",
                ))
    return added


# ── _claim_next_backfill_chunk ───────────────────────────────
def _claim_next_backfill_chunk():
    """Lock and return one pending chunk via SELECT ... FOR UPDATE SKIP LOCKED.

    Returns the row dict (or None). Caller must mark status=done/failed in a
    later transaction and refresh last_attempt_at + attempts.
    """
    with SessionLocal() as db:
        with db.begin():
            row = db.execute(
                text(
                    "SELECT shop_id, chunk_start, chunk_end, attempts "
                    "FROM shop_backfill_chunks "
                    "WHERE status = 'pending' "
                    "ORDER BY shop_id, chunk_start "
                    "LIMIT 1 "
                    "FOR UPDATE SKIP LOCKED"
                )
            ).first()
            if row is None:
                return None
            db.execute(
                text(
                    "UPDATE shop_backfill_chunks SET status='running', "
                    "attempts = attempts + 1, last_attempt_at = :now "
                    "WHERE shop_id=:sid AND chunk_start=:cs AND chunk_end=:ce"
                ),
                {
                    "now": datetime.utcnow(),
                    "sid": row.shop_id,
                    "cs": row.chunk_start,
                    "ce": row.chunk_end,
                },
            )
            return {
                "shop_id": row.shop_id,
                "chunk_start": row.chunk_start,
                "chunk_end": row.chunk_end,
                "attempts": row.attempts + 1,
            }


# ── _mark_backfill_chunk ───────────────────────────────
def _mark_backfill_chunk(shop_id_int: int, cs: date, ce: date, *, status: str, error: str | None = None) -> None:
    with SessionLocal() as db:
        with db.begin():
            db.execute(
                text(
                    "UPDATE shop_backfill_chunks SET status=:st, last_error=:err, "
                    "last_attempt_at=:now "
                    "WHERE shop_id=:sid AND chunk_start=:cs AND chunk_end=:ce"
                ),
                {
                    "st": status,
                    "err": (error or "")[:500] or None,
                    "now": datetime.utcnow(),
                    "sid": shop_id_int,
                    "cs": cs,
                    "ce": ce,
                },
            )


# ── _onboarding_backfill_loop ───────────────────────────────
def _onboarding_backfill_loop():
    """Watch for shops without backfill, enqueue 60-day chunks, drain them.

    Chunks are drained via SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1 so two
    worker processes never claim the same chunk. Each drained chunk runs the
    same _ingest_sales_lines_window path as hourly / nightly.
    """
    import time as _t
    from models import ShopSyncState

    while True:
        try:
            today_tashkent = _now_app_tz().date()
            # 1. Seed chunks for any new shop that has no sync_state yet.
            shop_ids = _active_shop_ids_for_sales()
            with SessionLocal() as db:
                have_state = {
                    r[0] for r in db.execute(select(ShopSyncState.shop_id)).all()
                }
            for s in shop_ids:
                try:
                    sid = int(s)
                except (TypeError, ValueError):
                    continue
                if sid in have_state:
                    continue
                added = _enqueue_backfill_chunks_for_shop(sid, today_tashkent)
                if added:
                    print(f"[SalesBackfill] shop={sid} enqueued {added} chunks")

            # 2. Drain one chunk per tick.
            chunk = _claim_next_backfill_chunk()
            if chunk is None:
                _t.sleep(30)
                continue

            sid = int(chunk["shop_id"])
            cs = chunk["chunk_start"]
            ce = chunk["chunk_end"]
            shop_id_str = str(sid)

            if not shop_lock.try_acquire_shop_lock(shop_id_str):
                # Some other loop owns it — mark pending again, try later.
                _mark_backfill_chunk(sid, cs, ce, status="pending")
                _t.sleep(5)
                continue
            try:
                window_from = datetime.combine(cs, dt_time(0, 0, 0))
                window_to = datetime.combine(ce + timedelta(days=1), dt_time(0, 0, 0))
                rows = _fetch_sales_for_shop_window(
                    shop_id_str, window_from, window_to
                )
                n = _ingest_sales_lines_window(rows, window_from, window_to)
                _mark_backfill_chunk(sid, cs, ce, status="done")
                print(f"[SalesBackfill] shop={sid} chunk=[{cs},{ce}] rows={n}")
            except Exception as e:
                _mark_backfill_chunk(sid, cs, ce, status="failed", error=str(e))
                print(f"[SalesBackfill] shop={sid} chunk=[{cs},{ce}] ERROR: {e}")
            finally:
                shop_lock.release_shop_lock(shop_id_str)
        except Exception as e:
            print(f"[SalesBackfill] Unexpected error: {e}")
            _t.sleep(60)


# ── _ingest_sales_lines_window ───────────────────────────────
def _ingest_sales_lines_window(
    rows: list[dict],
    date_from_tashkent: datetime,
    date_to_tashkent: datetime,
) -> int:
    """Ingest pre-parsed SELLS_REPORT rows for [from, to) Tashkent.

    Path C: rows may originate from a bulk create covering many shops.
    Routing is done per-row via a ``Variant.sku → ProductGroup.shop_id``
    catalog dict (NOT ``Variant.uzum_sku_id`` — see project_uzum_race_bug_fix.md
    correction 2026-04-21: CSV's "SKU" column is the seller code).

    Flow:
      1. Build sku→shop and sku→product_id dicts ONCE (per call, not per row).
      2. DROP rows where status == "Отменен" BEFORE INSERT (coder rule §2).
      3. Per-row route: ``shop_id = sku_to_shop.get(sku_id)``. If ``None``
         (unknown SKU), skip + count for the WARN log; never invent a shop.
      4. DELETE + bulk INSERT inside ONE ``with session.begin():`` block
         (coder rule §9). DELETE scopes to the shops that actually appeared
         in this window's rows so we stay idempotent without touching shops
         the caller didn't intend to refresh.

    Caller is responsible for create → poll → download → parse. Move that
    work outside so concurrent same-token creates can be batched into one
    bulk request (cross-wired ``fileUrl`` race; see memory).

    Returns the number of rows INSERTed (post-filter).
    """
    import logging as _logging
    from decimal import Decimal, InvalidOperation
    from models import SalesLine

    _logger = _logging.getLogger(__name__)

    if date_from_tashkent >= date_to_tashkent:
        return 0

    rows = rows or []

    def _parse_dt(val) -> datetime | None:
        """Parse 'YYYY-MM-DD HH:MM:SS' style CSV timestamp to naive Tashkent."""
        if not val:
            return None
        s = str(val).strip()
        if not s:
            return None
        for fmt in (
            "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y",
            "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
        ):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        return None

    def _parse_int(val) -> int:
        if val is None:
            return 0
        s = str(val).strip().replace(" ", "").replace("\u00a0", "")
        if not s:
            return 0
        # CSV may use "," as decimal separator; treat as decimal then truncate.
        s = s.replace(",", ".")
        try:
            return int(float(s))
        except (ValueError, InvalidOperation):
            return 0

    def _parse_dec(val) -> Decimal:
        if val is None:
            return Decimal("0")
        s = str(val).strip().replace(" ", "").replace("\u00a0", "").replace(",", ".")
        if not s:
            return Decimal("0")
        try:
            return Decimal(s)
        except (ValueError, InvalidOperation):
            return Decimal("0")

    # ── 1. Build sku→shop and sku→product_id dicts ONCE (per call) ─
    # IMPORTANT: route on Variant.sku (the seller code, VARCHAR), NOT
    # Variant.uzum_sku_id (Uzum internal numeric id). The CSV's "SKU" column
    # holds the seller code — verified 2026-04-21 dry-run.
    sku_to_shop: dict[str, int] = {}
    product_by_sku: dict[str, int] = {}
    with SessionLocal() as db:
        catalog_rows = db.execute(
            select(Variant.sku, Shop.uzum_id, ProductGroup.uzum_product_id)
            .join(ProductGroup, Variant.group_id == ProductGroup.id)
            .join(Shop, ProductGroup.shop_id == Shop.id)
            .where(Variant.sku.isnot(None))
            .where(Shop.uzum_id.isnot(None))
        ).all()
    for sku_raw, uzum_id_raw, pid_raw in catalog_rows:
        sku_key = str(sku_raw or "").strip()
        if not sku_key:
            continue
        try:
            sku_to_shop[sku_key] = int(str(uzum_id_raw).strip())
        except (TypeError, ValueError):
            continue
        try:
            pid_int = int(str(pid_raw).strip()) if pid_raw is not None else None
        except (TypeError, ValueError):
            pid_int = None
        if pid_int is not None:
            product_by_sku[sku_key] = pid_int

    # ── 2. Drop Отменен, validate, route, build payload ───────────
    n_in = len(rows)
    n_dropped_cancelled = 0
    n_skipped_unknown_sku = 0
    n_skipped_malformed = 0

    now_utc = datetime.utcnow()  # infra timestamp — naive UTC (coder rule §8).
    payload: list[dict] = []

    for r in rows:
        status = (r.get("status") or "").strip()
        if status == "Отменен":
            n_dropped_cancelled += 1
            continue

        order_id = (r.get("order_id") or "").strip()
        sku_key = str(r.get("sku_id") or "").strip()
        # `created_at` may already be a datetime (OpenAPI normalizer pre-parses)
        # or a string (CSV path). Accept either.
        raw_created = r.get("created_at")
        created = raw_created if isinstance(raw_created, datetime) else _parse_dt(raw_created)
        if not order_id or not sku_key or created is None:
            n_skipped_malformed += 1
            continue  # drop malformed rows; Uzum occasionally returns blanks.

        # ── 3. Per-row routing ────────────────────────────────────
        # OpenAPI rows carry shop_id directly. The browser CSV doesn't, so
        # we fall back to the SKU→shop catalog dict built above.
        row_shop_id = r.get("shop_id")
        if row_shop_id is not None:
            try:
                shop_id_int = int(row_shop_id)
            except (TypeError, ValueError):
                shop_id_int = None
        else:
            shop_id_int = sku_to_shop.get(sku_key)
        if shop_id_int is None:
            n_skipped_unknown_sku += 1
            continue

        # received_at may also be a datetime (OpenAPI) or string (CSV).
        raw_received = r.get("received_at")
        received = (
            raw_received if isinstance(raw_received, datetime)
            else _parse_dt(raw_received)
        )

        # product_id: OpenAPI surfaces it directly; CSV path resolves via catalog.
        raw_pid = r.get("product_id")
        try:
            row_pid = int(raw_pid) if raw_pid is not None else product_by_sku.get(sku_key)
        except (TypeError, ValueError):
            row_pid = product_by_sku.get(sku_key)

        # New OpenAPI-only fields (None / 0 for CSV-sourced rows).
        prod_image = r.get("product_image")
        if not isinstance(prod_image, dict):
            prod_image = None

        payload.append({
            "shop_id": shop_id_int,
            "order_id": order_id,
            "sku_id": sku_key,
            "sku_title": (r.get("sku_title") or "")[:500] or None,
            "barcode": (r.get("barcode") or "")[:120] or None,
            "category": (r.get("category") or "")[:300] or None,
            "product_id": row_pid,
            "status": (status or "")[:40] or None,
            "created_at": created,            # naive Tashkent — verbatim CSV.
            "received_at": received,
            "qty": _parse_int(r.get("qty")),
            "qty_returns": _parse_int(r.get("qty_returns")),
            "revenue": _parse_dec(r.get("revenue")),
            "seller_profit": _parse_dec(r.get("seller_profit")),
            "commission": _parse_dec(r.get("commission")),
            "unit_price": _parse_dec(r.get("unit_price")),
            "promo_amount": _parse_dec(r.get("promo_amount")),
            "purchase_price": _parse_dec(r.get("purchase_price")),
            "logistics_fee": _parse_dec(r.get("logistics_fee")),
            "product_image": prod_image,
            "qty_cancelled": _parse_int(r.get("qty_cancelled")),
            "synced_at": now_utc,
        })

    # ── 4. DELETE + INSERT in ONE transaction (coder rule §9) ─────
    # Scope DELETE to shops that actually appear in this batch's payload, so
    # idempotent re-runs don't nuke unrelated shops on a partial-coverage call.
    shops_in_payload: set[int] = {row["shop_id"] for row in payload}
    from models import SalesLine as _SalesLine
    with SessionLocal() as db:
        with db.begin():
            if shops_in_payload:
                db.execute(
                    delete(_SalesLine).where(
                        _SalesLine.shop_id.in_(list(shops_in_payload)),
                        _SalesLine.created_at >= date_from_tashkent,
                        _SalesLine.created_at < date_to_tashkent,
                    )
                )
            if payload:
                db.execute(insert(_SalesLine), payload)

    if n_skipped_unknown_sku:
        _logger.warning(
            "uzum sales ingest unknown_sku skipped window=[%s,%s) rows_in=%s "
            "written=%s skipped_unknown_sku=%s dropped_cancelled=%s skipped_malformed=%s",
            date_from_tashkent.isoformat(), date_to_tashkent.isoformat(),
            n_in, len(payload), n_skipped_unknown_sku,
            n_dropped_cancelled, n_skipped_malformed,
        )
    print(
        f"[SalesIngest] window=[{date_from_tashkent.isoformat()},"
        f"{date_to_tashkent.isoformat()}) rows_in={n_in} written={len(payload)} "
        f"skipped_unknown_sku={n_skipped_unknown_sku} "
        f"dropped_cancelled={n_dropped_cancelled} "
        f"skipped_malformed={n_skipped_malformed}"
    )

    return len(payload)


# ── _sync_products_for_shop ───────────────────────────────
def _sync_products_for_shop(shop_uzum_id: str, size: int = 100,
                             sync_all: bool = True, max_pages: int = 500,
                             skip_pass2: bool = False) -> dict:
    """
    Single-endpoint sync using /api/seller/shop/{shopId}/product/getProducts.
    Fetches all products + nested SKUs in one loop. No separate steps needed.
    Finance data (avg_daily_sales, purchase_price, sell_price_uzum) still from finance API.
    """
    api_key = _get_admin_token()
    if not api_key:
        raise RuntimeError("Uzum token not configured.")

    auth = f"Bearer {api_key}" if not api_key.startswith("Bearer ") else api_key
    headers = {"Authorization": auth, "Origin": "https://seller.uzum.uz", "Referer": "https://seller.uzum.uz/"}

    # Ensure Shop record exists
    with SessionLocal() as db:
        shop_obj = db.execute(
            select(Shop).where(Shop.uzum_id == shop_uzum_id)
        ).scalar_one_or_none()
        is_new_shop = shop_obj is None
        if is_new_shop:
            shop_obj = Shop(uzum_id=shop_uzum_id,
                            name=f"Shop {shop_uzum_id}",
                            owner_id=None)
            db.add(shop_obj)
            db.commit()
            db.refresh(shop_obj)
        current_shop_pk = shop_obj.id

    if is_new_shop:
        import threading as _t
        def _seed(uzum_id=shop_uzum_id, pk=current_shop_pk):
            try:
                _sync_finance_for_shop(uzum_id, pk)
            except Exception as _e:
                print(f"[FetchSync] Finance seed (variants) failed for {uzum_id}: {_e}")
        _t.Thread(target=_seed, daemon=True).start()

    # Fetch finance sales map (avg_daily_sales, purchase_price, sell_price come from here)
    sales_map = None
    try:
        sales_map = fetch_finance_sales_map(shop_uzum_id, api_key=api_key)
    except Exception as e:
        print(f"[Sync] finance fetch error (non-fatal): {e}")

    # ── Page through getProducts endpoint ────────────────────────────────────────
    print(f"[Sync] Fetching products for shop {shop_uzum_id} via getProducts ...")
    page = 0
    total_products_amount = None
    product_counter = 0
    total_variants = 0
    active_group_ids: set[int] = set()

    with SessionLocal() as db:
        while True:
            url = (f"https://api-seller.uzum.uz/api/seller/shop/{shop_uzum_id}"
                   f"/product/getProducts?page={page}&size={size}"
                   f"&filter=ALL&sortBy=id&order=descending")
            try:
                raw = http_json(url, headers=headers)
            except Exception as e:
                print(f"[Sync] getProducts page {page} error: {e}")
                break

            products = raw.get("productList") or []
            if total_products_amount is None:
                total_products_amount = raw.get("totalProductsAmount") or 0
                print(f"[Sync] totalProductsAmount={total_products_amount}")

            if not products:
                break

            for p in products:
                prod_id = str(p.get("productId") or "").strip()
                if not prod_id or prod_id in ("0", "0.0"):
                    continue

                product_counter += 1
                title = p.get("title") or f"Product {prod_id}"
                image = p.get("image") or p.get("previewImg") or None
                if image and "images.uzum.uz" in image and "/t_" not in image:
                    image = image.rstrip("/") + "/t_product_540_high.jpg"
                p_category = p.get("category") or None
                # Check product status from API (e.g. ARCHIVE, BLOCKED, etc.)
                p_status_obj = p.get("status") or {}
                p_status_val = (p_status_obj.get("value")
                                if isinstance(p_status_obj, dict)
                                else str(p_status_obj or "")).upper()
                p_is_archived = p_status_val in ("ARCHIVE", "ARCHIVED", "BLOCKED", "REMOVED")

                # ── Product-level fields ─────────────────────────────────────
                p_viewers = p.get("viewers")
                p_conversion = p.get("conversion")
                p_roi = p.get("roi")                    # string like "308.89..."
                p_rating = p.get("rating")              # string like "4.6"
                p_feedback_qty = p.get("feedbackQuantity")
                # commission: product-level `commission` is often null,
                # use commissionDto.minCommission instead
                commission_dto = p.get("commissionDto") or {}
                p_commission = (commission_dto.get("minCommission")
                                or commission_dto.get("maxCommission")
                                or p.get("commission"))
                # rank from product-level rankInfo
                p_rank_info = p.get("rankInfo") or {}
                p_rank = p_rank_info.get("rank") or p_rank_info.get("rankValue") or None

                # ── Upsert ProductGroup ──────────────────────────────────────
                group = db.execute(
                    select(ProductGroup).where(
                        ProductGroup.uzum_product_id == prod_id,
                        ProductGroup.shop_id == current_shop_pk,
                    )
                ).scalar_one_or_none()

                if group is None:
                    group = ProductGroup(
                        uzum_product_id=prod_id,
                        name=title,
                        image_url=image,
                        shop_id=current_shop_pk,
                        is_archived=p_is_archived,
                        uzum_sort_order=product_counter,
                    )
                    db.add(group)
                    db.flush()
                else:
                    group.name = title
                    group.uzum_sort_order = product_counter
                    group.is_archived = p_is_archived
                    if image:
                        group.image_url = image

                if p_category is not None:
                    group.category = p_category
                if p_viewers is not None:
                    try: group.viewers = int(p_viewers)
                    except Exception: pass
                if p_conversion is not None:
                    try: group.conversion = float(p_conversion)
                    except Exception: pass
                if p_roi is not None:
                    try: group.roi = float(p_roi)
                    except Exception: pass
                if p_rating is not None:
                    try: group.rating = float(p_rating)
                    except Exception: pass
                if p_feedback_qty is not None:
                    try: group.feedback_quantity = int(p_feedback_qty)
                    except Exception: pass
                if p_commission is not None:
                    try: group.commission = int(p_commission)
                    except Exception: pass
                if p_rank is not None:
                    group.rank = str(p_rank)

                if not p_is_archived:
                    active_group_ids.add(group.id)

                # ── Process nested SKUs ──────────────────────────────────────
                # Preload all existing variants for this group to avoid N+1 DB queries
                existing_variants = db.execute(
                    select(Variant).where(Variant.group_id == group.id)
                ).scalars().all()
                _v_by_uzum_id = {v.uzum_sku_id: v for v in existing_variants if v.uzum_sku_id}
                _v_by_barcode = {v.barcode: v for v in existing_variants if v.barcode}
                _v_by_sku = {v.sku: v for v in existing_variants if v.sku}

                sku_list = p.get("skuList") or []
                for s in sku_list:
                    # Prefer skuFullTitle (e.g. "LUXUZ-RING31-ЧЕРН-17") over skuTitle ("ЧЕРН-17")
                    sku_title = str(s.get("skuFullTitle") or s.get("skuTitle") or "").strip()
                    if not sku_title:
                        continue

                    barcode = str(s.get("barcode") or "").strip() or None
                    uzum_sku_id = str(s.get("skuId") or "").strip() or None
                    uz_qty = s.get("quantityActive")
                    characteristics = str(s.get("characteristics") or "").strip() or None
                    price = s.get("price") or s.get("marketPrice")
                    sku_image = s.get("previewImage") or None
                    # Ensure image URL has proper suffix for rendering
                    if sku_image and "images.uzum.uz" in sku_image and "/t_" not in sku_image:
                        sku_image = sku_image.rstrip("/") + "/t_product_540_high.jpg"
                    # status is an object: {value: "IN_STOCK", title: "...", color: "..."}
                    status_obj = s.get("status")
                    status_val = (status_obj.get("value")
                                  if isinstance(status_obj, dict)
                                  else str(status_obj or ""))

                    # SKU-level new fields (different from product-level!)
                    s_turnover = s.get("turnover")
                    s_qty_sold = s.get("quantitySold")
                    s_qty_returned = s.get("quantityReturned")
                    s_returned_pct = s.get("returnedPercentage")
                    s_has_discount = s.get("hasActiveDiscount")
                    s_rank_info = s.get("rankInfo") or {}
                    s_rank = s_rank_info.get("rank") or s_rank_info.get("rankValue") or None
                    s_paid_storage = s.get("paidStorageAmount")
                    s_paid_dim_group = s.get("paidStorageDimensionalGroup")
                    s_paid_price_item = s.get("paidStoragePriceItem")

                    # ── Upsert Variant (match by uzum_sku_id, then barcode, then sku) ──
                    # Uses preloaded dicts to avoid N+1 DB queries
                    v = None
                    if uzum_sku_id:
                        v = _v_by_uzum_id.get(uzum_sku_id)
                    if v is None and barcode:
                        v = _v_by_barcode.get(barcode)
                    if v is None:
                        v = _v_by_sku.get(sku_title)
                    if v is None:
                        v = Variant(group_id=group.id, sku=sku_title)
                        db.add(v)
                        _v_by_sku[sku_title] = v
                    else:
                        v.sku = sku_title  # Update SKU to latest from API

                    if uzum_sku_id:
                        v.uzum_sku_id = uzum_sku_id
                        _v_by_uzum_id[uzum_sku_id] = v
                    if barcode:
                        v.barcode = barcode
                        _v_by_barcode[barcode] = v
                    if sku_image:
                        v.image_url = sku_image
                    if characteristics:
                        v.color = characteristics
                    if status_val:
                        v.status = status_val
                    if uz_qty is not None:
                        try: v.uzum_quantity = int(uz_qty)
                        except Exception: pass
                    if price is not None:
                        try: v.price_sum = int(price)
                        except Exception: pass

                    # New getProducts fields
                    if s_turnover is not None:
                        try: v.turnover = int(s_turnover)
                        except Exception: pass
                    if s_qty_sold is not None:
                        try: v.quantity_sold = int(s_qty_sold)
                        except Exception: pass
                    if s_qty_returned is not None:
                        try: v.quantity_returned = int(s_qty_returned)
                        except Exception: pass
                    if s_returned_pct is not None:
                        try: v.returned_percentage = float(s_returned_pct)
                        except Exception: pass
                    if s_has_discount is not None:
                        v.has_active_discount = bool(s_has_discount)
                    if s_rank is not None:
                        v.rank = str(s_rank)
                    if s_paid_storage is not None:
                        try: v.paid_storage_amount = int(s_paid_storage)
                        except Exception: pass
                    if s_paid_dim_group is not None:
                        v.paid_storage_dimensional_group = str(s_paid_dim_group)
                    if s_paid_price_item is not None:
                        try: v.paid_storage_price_item = int(s_paid_price_item)
                        except Exception: pass

                    # Finance data (avg_daily_sales, purchase_price, sell_price from finance API)
                    qty_val = 0
                    if sales_map is not None:
                        data_fin = sales_map.get(sku_title) or sales_map.get(sku_title.upper())
                        if data_fin is None and barcode:
                            data_fin = (sales_map.get(barcode) or
                                        sales_map.get(barcode.upper()))
                        if data_fin is None and uzum_sku_id:
                            data_fin = sales_map.get(uzum_sku_id)
                        if data_fin:
                            qty_val = data_fin.get("qty") or 0
                            v.sales_30d_finance = qty_val
                            v.avg_daily_sales = qty_val / 30.0
                            if data_fin.get("sell_price"):
                                v.sell_price_uzum = int(data_fin["sell_price"])
                            if data_fin.get("commission"):
                                v.commission_per_unit = int(data_fin["commission"])
                            if data_fin.get("logistics"):
                                v.logistics_per_unit = int(data_fin["logistics"])
                            if data_fin.get("price"):
                                v.purchase_price = int(data_fin["price"])
                        else:
                            v.sales_30d_finance = 0
                            v.avg_daily_sales = 0.0

                    db.add(v)
                    # Upsert today's VariantSale
                    if sales_map is not None:
                        db.flush()
                        _today = date.today()
                        db.execute(delete(VariantSale).where(
                            VariantSale.variant_id == v.id,
                            VariantSale.date == _today
                        ))
                        if qty_val > 0:
                            db.add(VariantSale(variant_id=v.id, date=_today, qty_sold=qty_val))
                    total_variants += 1

            db.commit()

            if len(products) < size:
                break
            if max_pages and page >= max_pages:
                break
            page += 1

    # ── Archive reconciliation ───────────────────────────────────────────────────
    print(f"[Sync] Reconciling is_archived for shop_pk={current_shop_pk} ...")
    with SessionLocal() as db:
        if active_group_ids:
            active_list = list(active_group_ids)
            db.execute(
                update(ProductGroup)
                .where(ProductGroup.id.in_(active_list))
                .values(is_archived=False)
            )
            db.execute(
                update(ProductGroup)
                .where(ProductGroup.shop_id == current_shop_pk)
                .where(~ProductGroup.id.in_(active_list))
                .values(is_archived=True)
            )
        else:
            print(f"[Sync] WARNING — no active products found for shop_pk={current_shop_pk}, "
                  f"skipping archive reconciliation")
        db.commit()

    print(f"[Sync] Done for shop {shop_uzum_id}: {product_counter} products, {total_variants} variants")

    # Populate sales_lines:
    #   1. Seed backfill chunks from FINANCE_BACKFILL_START_DATE (2022-01-01) → today.
    #      The onboarding backfill loop drains them in the background.
    #   2. Synchronously fetch the last 45 days so the economics page has recent
    #      data immediately after the button returns. shop_lock serializes with
    #      the hourly / nightly / backfill loops.
    sales_lines_rows = 0
    backfill_chunks_enqueued = 0
    try:
        _today_tashkent = _now_app_tz().date()
        try:
            backfill_chunks_enqueued = _enqueue_backfill_chunks_for_shop(
                int(shop_uzum_id), _today_tashkent
            )
            if backfill_chunks_enqueued:
                print(f"[Sync] backfill enqueued shop={shop_uzum_id} "
                      f"chunks={backfill_chunks_enqueued} (drained in background)")
        except Exception as _e:
            print(f"[Sync] backfill enqueue failed for shop={shop_uzum_id}: {_e}")

        _start = datetime.combine(_today_tashkent - timedelta(days=45), dt_time(0, 0, 0))
        _end = datetime.combine(_today_tashkent + timedelta(days=1), dt_time(0, 0, 0))
        if shop_lock.try_acquire_shop_lock(shop_uzum_id):
            try:
                sales_rows = _fetch_sales_for_shop_window(
                    shop_uzum_id, _start, _end
                )
                sales_lines_rows = _ingest_sales_lines_window(sales_rows, _start, _end)
                print(f"[Sync] sales_lines ingest shop={shop_uzum_id} rows={sales_lines_rows} "
                      f"window=[{_start}, {_end})")
            finally:
                shop_lock.release_shop_lock(shop_uzum_id)
        else:
            print(f"[Sync] sales_lines ingest skipped for shop={shop_uzum_id} — lock held by another loop")
    except Exception as _e:
        print(f"[Sync] sales_lines ingest failed for shop={shop_uzum_id}: {_e}")

    return {
        "pages_synced": page + 1,
        "fetched": total_variants,
        "active_groups": len(active_group_ids),
        "total_products": product_counter,
        "sales_lines_rows": sales_lines_rows,
        "backfill_chunks_enqueued": backfill_chunks_enqueued,
    }


# ─────────────────────────────────────────────────────────────────
# From core/uzum_finance_openapi.py
# ─────────────────────────────────────────────────────────────────

# ── fetch_finance_orders_for_shop_window ───────────────────────────────
def fetch_finance_orders_for_shop_window(
    token: str,
    shop_uzum_id: str | int,
    date_from_tashkent: datetime,
    date_to_tashkent: datetime,
    *,
    size: int = _PAGE_SIZE_DEFAULT,
) -> list[dict]:
    """Walk /v1/finance/orders pages for the given window, return canonical rows.

    Window is **inclusive** on dateFrom and exclusive-ish on dateTo (we shave
    1 second off dateTo because Uzum's filter is inclusive on both sides;
    matches the convention the CSV path uses).
    """
    if date_from_tashkent >= date_to_tashkent:
        return []
    if not token:
        raise RuntimeError("fetch_finance_orders_for_shop_window: empty token")

    shop_int = int(shop_uzum_id)
    date_from_sec = _tashkent_naive_to_epoch_sec(date_from_tashkent)
    date_to_sec = _tashkent_naive_to_epoch_sec(date_to_tashkent) - 1

    out: list[dict] = []
    page = 0
    while page < _MAX_PAGES:
        try:
            body = _api.fetch_finance_orders_page(
                token, shop_int,
                date_from_sec=date_from_sec, date_to_sec=date_to_sec,
                page=page, size=size, group=False,
            )
        except Exception as e:
            print(f"[FinanceOpenAPI] orders fetch failed shop={shop_int} page={page}: {e}")
            raise
        items = body.get("orderItems") if isinstance(body, dict) else None
        items = items if isinstance(items, list) else []
        if page == 0:
            total = body.get("totalElements") if isinstance(body, dict) else None
            print(f"[FinanceOpenAPI] orders shop={shop_int} window=["
                  f"{date_from_tashkent.isoformat()},{date_to_tashkent.isoformat()}) "
                  f"totalElements={total}")

        for raw in items:
            if not isinstance(raw, Mapping):
                continue
            canon = _openapi_order_to_canonical(raw, shop_int)
            if canon is not None:
                out.append(canon)

        if len(items) < size:
            break
        page += 1
        time.sleep(_BETWEEN_PAGE_SLEEP_S)

    return out


