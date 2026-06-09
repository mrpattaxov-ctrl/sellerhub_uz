from datetime import datetime, date
from decimal import Decimal
from sqlalchemy import BigInteger, String, Integer, Date, DateTime, ForeignKey, Boolean, Float, Text, Numeric, Index, CheckConstraint, LargeBinary, text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from flask_login import UserMixin


class Base(DeclarativeBase):
    pass


# --- Shops ---
class Shop(Base):
    __tablename__ = "shops"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    uzum_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=True)
    # Which app user (seller) owns this shop. NULL = unassigned (visible to admin only).
    owner_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True, index=True)


# --- New Uzum-aware tables ---
class ProductGroup(Base):
    __tablename__ = "product_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shop_id: Mapped[int] = mapped_column(Integer, ForeignKey("shops.id"), nullable=True)

    # Uzum "productId" (main product id) if available
    uzum_product_id: Mapped[str] = mapped_column(String(64), nullable=True, index=True)

    name: Mapped[str] = mapped_column(String(300), nullable=False)
    category: Mapped[str] = mapped_column(String(200), nullable=True)
    image_url: Mapped[str] = mapped_column(String(800), nullable=True)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    uzum_sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Fields from getProducts API (product level)
    viewers: Mapped[int] = mapped_column(Integer, nullable=True, default=0)
    conversion: Mapped[float] = mapped_column(Float, nullable=True, default=0.0)
    roi: Mapped[float] = mapped_column(Float, nullable=True, default=0.0)
    rating: Mapped[float] = mapped_column(Float, nullable=True, default=0.0)
    feedback_quantity: Mapped[int] = mapped_column(Integer, nullable=True, default=0)
    commission: Mapped[int] = mapped_column(Integer, nullable=True, default=0)       # from commissionDto (single value)
    rank: Mapped[str] = mapped_column(String(10), nullable=True)                     # e.g. "A", "B", "C"

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    variants: Mapped[list["Variant"]] = relationship(
        back_populates="group",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Variant(Base):
    __tablename__ = "variants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(Integer, ForeignKey("product_groups.id", ondelete="CASCADE"), index=True, nullable=False)

    # Uzum SKU / offer key
    sku: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    barcode: Mapped[str] = mapped_column(String(120), nullable=True, index=True)
    image_url: Mapped[str] = mapped_column(String(500), nullable=True)

    # Useful for UI (color/size)
    color: Mapped[str] = mapped_column(String(80), nullable=True)
    size: Mapped[str] = mapped_column(String(80), nullable=True)
    status: Mapped[str] = mapped_column(String(80), nullable=True)
    avg_daily_sales: Mapped[float] = mapped_column(Float, nullable=True, default=0.0)
    sales_30d_finance: Mapped[int] = mapped_column(Integer, nullable=True, default=0)

    price_sum: Mapped[int] = mapped_column(Integer, nullable=True)  # price in sum if available
    purchase_price: Mapped[int] = mapped_column(Integer, nullable=True, default=0)

    # Finance API fields (per-unit values from actual orders)
    sell_price_uzum: Mapped[int] = mapped_column(Integer, nullable=True, default=0)    # actual sell price per unit
    commission_per_unit: Mapped[int] = mapped_column(Integer, nullable=True, default=0) # Uzum commission per unit
    logistics_per_unit: Mapped[int] = mapped_column(Integer, nullable=True, default=0)  # logistics fee per unit

    uzum_sku_id: Mapped[str] = mapped_column(String(64), nullable=True, index=True)      # numeric Uzum skuId
    uzum_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)       # "На складе" in Uzum
    warehouse_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # local warehouse qty
    views_30d: Mapped[int] = mapped_column(Integer, nullable=True, default=0)            # views from analytics API

    # Fields from getProducts API (SKU level)
    turnover: Mapped[int] = mapped_column(Integer, nullable=True, default=0)             # days of stock remaining
    quantity_sold: Mapped[int] = mapped_column(Integer, nullable=True, default=0)
    quantity_returned: Mapped[int] = mapped_column(Integer, nullable=True, default=0)
    returned_percentage: Mapped[float] = mapped_column(Float, nullable=True, default=0.0)
    has_active_discount: Mapped[bool] = mapped_column(Boolean, nullable=True, default=False)
    rank: Mapped[str] = mapped_column(String(10), nullable=True)                         # e.g. "A", "B", "C"
    paid_storage_amount: Mapped[int] = mapped_column(Integer, nullable=True, default=0)
    paid_storage_dimensional_group: Mapped[str] = mapped_column(String(80), nullable=True)
    paid_storage_price_item: Mapped[int] = mapped_column(Integer, nullable=True, default=0)

    # ── Fields exclusive to the Seller OpenAPI /v1/product/shop/{shopId} ────────
    # Populated only when sync runs via the per-user uzum_openapi_token path
    # (see _sync_products_via_openapi). The browser/admin-token sync leaves
    # these untouched.
    product_title_ru: Mapped[str] = mapped_column(String(300), nullable=True)
    product_title_uz: Mapped[str] = mapped_column(String(300), nullable=True)
    quantity_created: Mapped[int] = mapped_column(Integer, nullable=True)
    quantity_fbs: Mapped[int] = mapped_column(Integer, nullable=True)
    quantity_additional: Mapped[int] = mapped_column(Integer, nullable=True)
    quantity_archived: Mapped[int] = mapped_column(Integer, nullable=True)
    quantity_pending: Mapped[int] = mapped_column(Integer, nullable=True)
    quantity_defected: Mapped[int] = mapped_column(Integer, nullable=True)
    quantity_missing: Mapped[int] = mapped_column(Integer, nullable=True)
    blocked: Mapped[bool] = mapped_column(Boolean, nullable=True)
    blocking_reason: Mapped[str] = mapped_column(String(500), nullable=True)
    sku_block_reason: Mapped[str] = mapped_column(Text, nullable=True)
    ikpu: Mapped[str] = mapped_column(String(80), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    group: Mapped["ProductGroup"] = relationship(back_populates="variants")

    sales: Mapped[list["VariantSale"]] = relationship(
        back_populates="variant",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class VariantSale(Base):
    __tablename__ = "variant_sales"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    variant_id: Mapped[int] = mapped_column(Integer, ForeignKey("variants.id", ondelete="CASCADE"), index=True, nullable=False)

    date: Mapped[date] = mapped_column(Date, nullable=False)
    qty_sold: Mapped[int] = mapped_column(Integer, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    variant: Mapped["Variant"] = relationship(back_populates="sales")


class PosActionLog(Base):
    __tablename__ = "pos_action_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    shop_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("shops.id"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    items_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    reverted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_pos_action_log_user_created", "user_id", "created_at"),
    )


class User(UserMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(200), nullable=False)
    api_key: Mapped[str] = mapped_column(String(500), nullable=True)
    # Per-user Uzum Seller OpenAPI token (https://api-seller.uzum.uz/api/seller-openapi).
    # Pasted by the user in My Shops to discover their owned shops; persisted
    # on first successful /v1/shops probe.
    uzum_openapi_token: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Uzum seller account ID — visible as ?sId=<N> in the seller cabinet URL
    # (e.g. seller.uzum.uz/seller/fbs/orders/.../order/XYZ?sId=95673). Required
    # by POST /v1/fbs/invoice's sellerId field; NOT exposed by /v1/shops or
    # any other OpenAPI endpoint, so the user must paste it manually once.
    # Account-wide (one value for all shops the seller owns).
    uzum_seller_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # ── Komitent (legal entity) fields for FBS "Akt отправки" PDF ─────
    # Uzum OpenAPI does NOT expose any of these (verified by 35-endpoint
    # probe — all 403 RBAC). The user fills them in once on the profile
    # page; the PDF generator reads them when building the act header.
    full_name_legal: Mapped[str | None] = mapped_column(String(300), nullable=True)
    contract_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pinfl: Mapped[str | None] = mapped_column(String(32), nullable=True)
    legal_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    inn: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # True for the platform admin (you). Only admins can set the Uzum token,
    # create other users, and assign shops.
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Uzum Seller credentials for automatic token refresh
    uzum_phone: Mapped[str] = mapped_column(String(100), nullable=True)
    uzum_password_plain: Mapped[str] = mapped_column(String(200), nullable=True)
    # Telegram account ID for login via Telegram widget
    telegram_id: Mapped[str] = mapped_column(String(64), nullable=True, index=True)
    # Phone number linked to this account (for Telegram approval login)
    phone: Mapped[str] = mapped_column(String(50), nullable=True, index=True)
    must_change_password: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        server_default=sql_text("false"),
    )
    trial_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    subscription_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    subscription_is_unlimited: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        server_default=sql_text("false"),
    )


class SubscriptionSettings(Base):
    __tablename__ = "subscription_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False, default=1)
    trial_days: Mapped[int] = mapped_column(
        Integer,
        default=7,
        nullable=False,
        server_default=sql_text("7"),
    )
    monthly_price_sum: Mapped[int] = mapped_column(
        Integer,
        default=100000,
        nullable=False,
        server_default=sql_text("100000"),
    )
    max_shops_per_user: Mapped[int] = mapped_column(
        Integer,
        default=5,
        nullable=False,
        server_default=sql_text("5"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )


class SubscriptionCode(Base):
    __tablename__ = "subscription_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    duration_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_unlimited: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        server_default=sql_text("false"),
    )
    max_activations: Mapped[int] = mapped_column(
        Integer,
        default=1,
        nullable=False,
        server_default=sql_text("1"),
    )
    used_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        server_default=sql_text("0"),
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        server_default=sql_text("true"),
    )
    created_by_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class SubscriptionCodeActivation(Base):
    __tablename__ = "subscription_code_activations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("subscription_codes.id"), nullable=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    activated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    applied_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    was_unlimited: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        server_default=sql_text("false"),
    )


class SubscriptionOrder(Base):
    __tablename__ = "subscription_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    plan_key: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    plan_label: Mapped[str] = mapped_column(String(64), nullable=False)
    duration_days: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_sum: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_tiyin: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="pending",
        server_default=sql_text("'pending'"),
        index=True,
    )
    previous_subscription_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    previous_subscription_is_unlimited: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        server_default=sql_text("false"),
    )
    applied_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )


class PaymeTransaction(Base):
    __tablename__ = "payme_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    payme_transaction_id: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    order_id: Mapped[int] = mapped_column(Integer, ForeignKey("subscription_orders.id"), nullable=False, index=True)
    amount_tiyin: Mapped[int] = mapped_column(Integer, nullable=False)
    account_order_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    payme_time_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    create_time_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    perform_time_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=sql_text("0"))
    cancel_time_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=sql_text("0"))
    state: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=sql_text("1"), index=True)
    reason: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )


class NotificationSettings(Base):
    __tablename__ = "notification_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, unique=True, index=True)
    hourly_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        server_default=sql_text("true"),
    )
    window_from_hour: Mapped[int] = mapped_column(
        Integer,
        default=8,
        nullable=False,
        server_default=sql_text("8"),
    )
    window_to_hour: Mapped[int] = mapped_column(
        Integer,
        default=20,
        nullable=False,
        server_default=sql_text("20"),
    )
    is_24h: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        server_default=sql_text("false"),
    )
    interval_hours: Mapped[int] = mapped_column(
        Integer,
        default=1,
        nullable=False,
        server_default=sql_text("1"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )


class TelegramPending(Base):
    """Pending Telegram login requests — stored in DB so all gunicorn workers can access."""
    __tablename__ = "telegram_pending"

    token: Mapped[str] = mapped_column(String(100), primary_key=True)
    type: Mapped[str] = mapped_column(String(20), nullable=False, default="code")  # "code" or "approval"
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tg_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tg_username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


# ─────────────────────────────────────────────────────────────────────
# Phase 1 — Uzum Reports API migration (SELLS_REPORT group=false +
# EXPENSES_REPORT). See migrations/versions/20260419_0001_phase1_sales_reports.py
# and project_sales_reports_implementation_plan.md for the full design.
#
# Business timestamps (created_at, received_at, charged_at) are NAIVE
# Tashkent local time — verbatim from the CSV, NO tz shift.
# Infra timestamps (synced_at, last_*_at, last_attempt_at) are NAIVE UTC
# (datetime.utcnow()).
# ─────────────────────────────────────────────────────────────────────


class ExpensesLedger(Base):
    """Per-operation expenses from EXPENSES_REPORT.

    Stores ALL rows including `Логистика` and `Возврат` — filtering happens
    at read/notification time. `amount` is ALWAYS positive; direction lives
    in `op_type` (`Оплата` = outflow, `Возврат` = income).
    """
    __tablename__ = "expenses_ledger"

    shop_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(80), primary_key=True)

    # Business timestamp — naive Tashkent (verbatim CSV).
    charged_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    day: Mapped[date] = mapped_column(Date, nullable=False)

    source: Mapped[str | None] = mapped_column(String(120), nullable=True)
    service: Mapped[str | None] = mapped_column(String(300), nullable=True)
    status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    op_type: Mapped[str | None] = mapped_column(String(40), nullable=True)

    unit_cost: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=0, server_default=sql_text("0"))
    qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=sql_text("0"))
    # ALWAYS positive; direction via op_type.
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=0, server_default=sql_text("0"))

    # ── OpenAPI-only fields (NULL for rows from the browser CSV path).
    # dateCreated / dateUpdated are Uzum's audit stamps on the payment
    # record itself — when the row was created in their system and last
    # touched. charged_at (the existing column) is dateService.
    date_created: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    date_updated: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Seller ID (distinct from shop_id; one seller can own many shops).
    seller_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Free-form identifiers Uzum returns for some payment types — verbatim.
    external_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    code: Mapped[str | None] = mapped_column(String(80), nullable=True)

    # Infra timestamp — naive UTC.
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        CheckConstraint("amount >= 0", name="ck_expenses_ledger_amount_nonneg"),
        Index("ix_expenses_ledger_shop_day", "shop_id", "day"),
        Index(
            "ix_expenses_ledger_shop_charged",
            "shop_id",
            sql_text("charged_at DESC"),
        ),
    )


class ShopSyncState(Base):
    """Per-shop high-level scheduler state for the Reports pipeline."""
    __tablename__ = "shop_sync_state"

    shop_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    backfill_status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="pending",
        server_default=sql_text("'pending'"),
    )
    backfill_through_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Infra timestamps — naive UTC.
    last_hourly_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_nightly_refetch_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_expenses_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Stage 4: FBS/DBS background sync. `fbs_active` gates which shops the
    # worker polls — set to True for shops that have stock with
    # quantity_fbs > 0. `last_fbs_sync_at` is the wall-clock of the most
    # recent successful list+detail refresh; the worker uses it for the
    # "every 10 min" scheduler and the UI shows "Oxirgi yangilangan: N min".
    fbs_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=sql_text("false"),
    )
    last_fbs_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# ─────────────────────────────────────────────────────────────────────
# Legacy-style finance shape, restored 2026-05-21.
#
# Schema returns to the original pre-Phase-1 layout: per-(shop, day, sku)
# daily aggregates in `FinanceOrder` + hourly cumulative snapshots in
# `FinanceHourlySnapshot`. Source changed from admin-token /finance/orders
# (paginated GET) to per-user OpenAPI /v1/finance/orders?group=true (also
# paginated GET — same shape, instant). The chunked-queue + per-line
# sales_lines layer is being retired.
#
# Hourly Telegram notifications use snapshot delta math (no API call) —
# DB-only diff of cumulative totals between two snapshot_hour rows.
# ─────────────────────────────────────────────────────────────────────


class FinanceOrder(Base):
    """Cached daily aggregates per (shop, day, sku) from /v1/finance/orders?group=true.

    For SHRINKING-vs-GROWING totals: amount/sell_price/purchase_price are
    fixed snapshots of the day's totals (not deltas vs. previous query —
    DELETE+INSERT for the (shop_id, period_from, period_to) range makes
    re-runs idempotent).
    """
    __tablename__ = "finance_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # String to match Shop.uzum_id convention (varchar in DB, even though
    # values are numeric).
    shop_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    period_from: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    period_to: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    sku_title: Mapped[str] = mapped_column(String(300), nullable=False, index=True)
    sku_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    product_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # Russian title and Uzbek title (Uzum exposes both via Accept-Language).
    product_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    product_title_ru: Mapped[str | None] = mapped_column(String(500), nullable=True)
    image_url: Mapped[str | None] = mapped_column(String(800), nullable=True)
    # Size/colour string e.g. "20, Чёрный" — verbatim from
    # SkuGroupedSellerItemDto.characteristics joined by ", ".
    characteristics: Mapped[str | None] = mapped_column(String(300), nullable=True)
    amount: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=sql_text("0"))
    amount_returns: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=sql_text("0"))
    sell_price: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=sql_text("0"))
    purchase_price: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=sql_text("0"))
    seller_discount: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=sql_text("0"))
    seller_profit: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=sql_text("0"))
    commission: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=sql_text("0"))
    withdrawn_profit: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=sql_text("0"))
    logistics_fee: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=sql_text("0"))

    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        # Idempotent DELETE+INSERT key.
        Index("ix_finance_orders_shop_period", "shop_id", "period_from", "period_to"),
        # Per-shop "today's data" lookup (snapshot delta + telegram).
        Index("ix_finance_orders_shop_period_sku", "shop_id", "period_from", "sku_title"),
    )


class FinanceHourlySnapshot(Base):
    """Cumulative snapshot of today's sales taken at every HH:00 boundary.

    Hourly notification "За час" delta = current snapshot row totals minus
    previous snapshot row totals (per shop, per sku). Pure DB math, no API
    call. Retention 25h (a sliding window of yesterday-end + today).
    """
    __tablename__ = "finance_hourly_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shop_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    sku_title: Mapped[str] = mapped_column(String(300), nullable=False)
    # Naive UTC, matching legacy convention. snap_hour is the HH:00 mark in
    # Tashkent converted to UTC before storage so subscription/checkpoint
    # comparisons remain TZ-neutral.
    snapshot_hour: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    amount: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=sql_text("0"))
    sell_price: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=sql_text("0"))
    purchase_price: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=sql_text("0"))
    seller_profit: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=sql_text("0"))
    commission: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=sql_text("0"))
    logistics_fee: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=sql_text("0"))

    __table_args__ = (
        Index("ix_finance_hourly_shop_hour", "shop_id", "snapshot_hour"),
    )


# ─────────────────────────────────────────────────────────────────────
# Stage 4 — FBS/DBS order cache (2026-05-21).
#
# Read path through ``core.fbs_data`` will switch from "every call hits
# Uzum" to "DB first, ?refresh=1 hits Uzum + invalidates row" once the
# background worker (Stage 4b) starts populating this table. This file
# only declares the schema — no writer yet.
#
# FBS and DBS orders share one table; ``order_type`` differentiates.
# Stage 1 enum from ``core/uzum_openapi.py``:
#   FBS_ORDER_STATUSES = (CREATED, PACKING, PENDING_DELIVERY,
#       DELIVERING, DELIVERED, ACCEPTED_AT_DP,
#       DELIVERED_TO_CUSTOMER_DELIVERY_POINT, COMPLETED, CANCELED,
#       PENDING_CANCELLATION, RETURNED)
# The CHECK constraint hardcodes these so a runtime drift in the Python
# enum doesn't quietly accept invalid rows; bump the constraint when
# updating FBS_ORDER_STATUSES.
# ─────────────────────────────────────────────────────────────────────


class FbsOrder(Base):
    """Cached FBS/DBS orders synced from Uzum every ~10 min by the
    background worker (Stage 4b).

    Write strategy: UPSERT by ``order_id`` (Bosqich 4f) — every tick
    fetches all 11 statuses with no date filter and INSERTs new orders
    + UPDATEs existing rows. No DELETE: historical orders accumulate
    in the DB indefinitely (subject to the Stage 4d cleanup loop).

    FBS and DBS orders share this table; ``order_type`` differentiates.
    Only shops with ``ShopSyncState.fbs_active=True`` are synced.

    Notes on shop_id type
    ---------------------
    ``shop_id`` is ``String(64)`` to match ``Shop.uzum_id`` (and the
    sibling ``FinanceOrder.shop_id``) — Uzum's shop identifier is a
    numeric string, not a foreign-key int. Same convention across the
    OpenAPI-cache tables.
    """

    __tablename__ = "fbs_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # ── Identity ───────────────────────────────────────────────────
    shop_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # Uzum's orderId is int64 — stored as string for headroom and to match
    # the pattern other Uzum identifiers use here. Unique because Uzum's
    # space is global; one orderId never belongs to two shops.
    order_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    # "FBS" | "DBS" — CHECK constraint enforced below.
    order_type: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    # One of the 11 FBS_ORDER_STATUSES values — CHECK constraint enforced.
    status: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # ── Customer ───────────────────────────────────────────────────
    customer_fullname: Mapped[str | None] = mapped_column(String(300), nullable=True)
    customer_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    delivery_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivery_comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Money (UZS, integer — Uzum returns whole-soum integers) ────
    price: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sql_text("0"),
    )

    # ── Life-cycle dates — naive UTC; parsed from Uzum ISO strings ─
    date_created: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    accept_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deliver_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    accepted_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    delivering_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    delivery_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    delivered_to_dp_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancelled_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    return_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # ── Misc ───────────────────────────────────────────────────────
    cancel_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    identifier_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sql_text("false"),
    )
    stock_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stock_title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    drop_off_point_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    drop_off_point_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    invoice_number: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # ── JSONB — order items + full Uzum payload for forward compat ─
    # `items_json` holds the orderItems array verbatim; the worker won't
    # bother projecting every nested field onto its own column. `raw_json`
    # keeps the whole Uzum response for debugging or surfacing a future
    # field without a migration.
    items_json: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sql_text("'[]'::jsonb"),
    )
    raw_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sql_text("'{}'::jsonb"),
    )

    # ── Sync infra ─────────────────────────────────────────────────
    synced_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        server_default=sql_text("CURRENT_TIMESTAMP"),
        index=True,
    )

    __table_args__ = (
        # Primary listing query — /fbs page filters by shop_id + status and
        # orders by date_created DESC (newest first). The composite index
        # serves both the WHERE filter and the ORDER BY in one B-tree pass.
        Index(
            "ix_fbs_orders_shop_status_date",
            "shop_id", "status", sql_text("date_created DESC"),
        ),
        # Worker iteration order — find rows older than X by shop, regardless
        # of status (e.g. for stale-detection or shop-wide purge).
        Index("ix_fbs_orders_shop_synced", "shop_id", "synced_at"),
        # ── CHECK constraints ──
        # Hardcoded enum values mirror core/uzum_openapi.py constants.
        # Bumping either enum requires a migration that drops + recreates
        # the constraint with the new value list.
        CheckConstraint(
            "order_type IN ('FBS', 'DBS')",
            name="ck_fbs_orders_order_type",
        ),
        CheckConstraint(
            "status IN ("
            "'CREATED', 'PACKING', 'PENDING_DELIVERY', 'DELIVERING', "
            "'DELIVERED', 'ACCEPTED_AT_DP', "
            "'DELIVERED_TO_CUSTOMER_DELIVERY_POINT', 'COMPLETED', "
            "'CANCELED', 'PENDING_CANCELLATION', 'RETURNED'"
            ")",
            name="ck_fbs_orders_status",
        ),
    )


class FbsDropoffPoint(Base):
    """Global catalog of Uzum drop-off (qabul) points the platform has
    seen, accumulated across every seller.

    Why global, not per-user: Uzum's drop-off network is shared — every
    seller can ship to any point in principle. The per-call endpoint
    (``GET /v1/fbs/invoice/dop/drop-off-points``) only returns points
    matching the caller's *current* orders' dimensional groups + capacity,
    so a seller with few active orders sees few points. Pooling every
    point any seller has ever fetched into one table gives newer/quieter
    sellers an immediate full picker.

    Refresh strategy: every successful Uzum call upserts each returned
    point here (updates ``last_seen_at`` + metadata). The picker UI
    serves Uzum's live response merged with this catalog. Points whose
    ``last_seen_at`` is older than ~30 days are considered stale (likely
    disabled by Uzum) and filtered out at query time.
    """

    __tablename__ = "fbs_dropoff_points_catalog"

    # Uzum's uuid is the natural primary key — globally unique, stable.
    uuid: Mapped[str] = mapped_column(String(64), primary_key=True)
    address: Mapped[str] = mapped_column(Text, nullable=False)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Uzum returns workingHours as {"MONDAY": {"start": "09:00", "end": "21:00"}, ...}
    working_hours_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sql_text("'{}'::jsonb"),
    )
    dimensional_group_is_large: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sql_text("false"),
    )
    point_type: Mapped[str | None] = mapped_column(String(64), nullable=True)

    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False,
        default=datetime.utcnow, server_default=sql_text("CURRENT_TIMESTAMP"),
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False,
        default=datetime.utcnow, server_default=sql_text("CURRENT_TIMESTAMP"),
        index=True,
    )


class FbsInvoiceAkt(Base):
    """Cached "Акт приема-передачи" (akt отправки) PDF per FBS invoice.

    Why cache: Uzum's ``/v1/fbs/invoice/{id}/print`` endpoint rate-limits
    hard (~4 quick calls → HTTP 429, ~4s recovery — see the akt-print rate
    -limit reference). Bulk-printing many akts at hand-off time burst-trips
    that limit. The background sync worker PRE-FETCHES each active invoice's
    akt here (paced, no burst), so the seller's "Akt отправки (PDF)" print
    reads straight from the DB — instant, zero Uzum calls, never 429.

    Freshness: keyed by ``invoice_id`` with ``date_updated`` (the invoice's
    Uzum ``dateUpdated`` epoch-ms) as the version stamp. The akt content can
    change while an invoice is still CREATED (e.g. the seller changes the
    drop-off point / time slot), which bumps ``dateUpdated`` — the prefetch
    re-fetches when it differs, and the pickup-change endpoint deletes the
    row outright. ``user_id`` scopes reads so one seller can't pull another's
    cached akt by guessing an invoice id.
    """

    __tablename__ = "fbs_invoice_akts"

    # Uzum's invoice id is globally unique → natural primary key.
    invoice_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    # Invoice's Uzum ``dateUpdated`` (epoch ms) at fetch time — the cache
    # version. NULL only for legacy rows fetched before we tracked it.
    date_updated: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    pdf: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    synced_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False,
        default=datetime.utcnow, server_default=sql_text("CURRENT_TIMESTAMP"),
    )
