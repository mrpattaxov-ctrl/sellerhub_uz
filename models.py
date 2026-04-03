from datetime import datetime, date
from sqlalchemy import String, Integer, Date, DateTime, ForeignKey, Boolean, Float, Text, text as sql_text
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


class User(UserMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(200), nullable=False)
    api_key: Mapped[str] = mapped_column(String(500), nullable=True)
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


class WarehouseExpenseSnapshot(Base):
    """Daily warehouse-expense snapshot per shop for Telegram summaries."""
    __tablename__ = "warehouse_expense_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    expense_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    shop_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    items_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    total_amount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)


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
