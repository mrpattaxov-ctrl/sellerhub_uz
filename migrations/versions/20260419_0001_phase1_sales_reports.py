"""Phase 1: sales_lines, expenses_ledger, shop_backfill_chunks, shop_sync_state.

Revision ID: 20260419_0001
Revises:
Create Date: 2026-04-19

Adds the four tables that back the Uzum Reports API migration (SELLS_REPORT
group=false + EXPENSES_REPORT). All four tables are brand-new; existing
tables are untouched and continue to be managed by Base.metadata.create_all()
until a follow-up migration brings them under Alembic control.

Business timestamps (`created_at`, `received_at`, `charged_at`) are stored as
naive Tashkent local time — verbatim from the CSV (NO tz shift). Infra
timestamps (`synced_at`, `last_*_at`, `last_attempt_at`) are naive UTC.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "20260419_0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── sales_lines ──────────────────────────────────────────────────
    # Per-order-line sales ledger (source of truth, SELLS_REPORT group=false).
    # PK = (shop_id, order_id, sku_id). NO `day` column — all range queries
    # use `created_at`.
    op.create_table(
        "sales_lines",
        sa.Column("shop_id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.String(length=64), nullable=False),
        sa.Column("sku_id", sa.String(length=64), nullable=False),
        sa.Column("sku_title", sa.String(length=500), nullable=True),
        sa.Column("barcode", sa.String(length=120), nullable=True),
        sa.Column("category", sa.String(length=300), nullable=True),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=True),
        # Business timestamps — naive Tashkent (verbatim CSV value, no shift).
        sa.Column("created_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=False), nullable=True),
        sa.Column("qty", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("qty_returns", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("revenue", sa.Numeric(18, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("seller_profit", sa.Numeric(18, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("commission", sa.Numeric(18, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("unit_price", sa.Numeric(18, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("promo_amount", sa.Numeric(18, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("purchase_price", sa.Numeric(18, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("logistics_fee", sa.Numeric(18, 2), nullable=False, server_default=sa.text("0")),
        # Infra timestamp — naive UTC (datetime.utcnow()).
        sa.Column("synced_at", sa.DateTime(timezone=False), nullable=False),
        sa.PrimaryKeyConstraint("shop_id", "order_id", "sku_id"),
    )
    op.create_index(
        "ix_sales_lines_shop_created",
        "sales_lines",
        ["shop_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_sales_lines_shop_sku_created",
        "sales_lines",
        ["shop_id", "sku_id", sa.text("created_at DESC")],
    )

    # ── expenses_ledger ──────────────────────────────────────────────
    # Per-operation expense rows (EXPENSES_REPORT). Stores ALL rows including
    # Логистика and Возврат — filtering happens at read/notification time.
    # `amount` is always positive; direction lives in `op_type`.
    op.create_table(
        "expenses_ledger",
        sa.Column("shop_id", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.String(length=80), nullable=False),
        # Business timestamp — naive Tashkent.
        sa.Column("charged_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("source", sa.String(length=120), nullable=True),
        sa.Column("service", sa.String(length=300), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=True),
        sa.Column("op_type", sa.String(length=40), nullable=True),
        sa.Column("unit_cost", sa.Numeric(18, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("qty", sa.Integer(), nullable=False, server_default=sa.text("0")),
        # Always positive; direction via op_type.
        sa.Column("amount", sa.Numeric(18, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("synced_at", sa.DateTime(timezone=False), nullable=False),
        sa.PrimaryKeyConstraint("shop_id", "operation_id"),
        sa.CheckConstraint("amount >= 0", name="ck_expenses_ledger_amount_nonneg"),
    )
    op.create_index(
        "ix_expenses_ledger_shop_day",
        "expenses_ledger",
        ["shop_id", "day"],
    )
    op.create_index(
        "ix_expenses_ledger_shop_charged",
        "expenses_ledger",
        ["shop_id", sa.text("charged_at DESC")],
    )

    # ── shop_backfill_chunks ─────────────────────────────────────────
    # NEW-SHOP initial backfill only (2022 → today), chunked 30-60 days.
    # NOT used for the nightly 45-day refetch.
    op.create_table(
        "shop_backfill_chunks",
        sa.Column("shop_id", sa.Integer(), nullable=False),
        sa.Column("chunk_start", sa.Date(), nullable=False),
        sa.Column("chunk_end", sa.Date(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        # Infra timestamp — naive UTC.
        sa.Column("last_attempt_at", sa.DateTime(timezone=False), nullable=True),
        sa.PrimaryKeyConstraint("shop_id", "chunk_start", "chunk_end"),
    )
    op.create_index(
        "ix_shop_backfill_chunks_status",
        "shop_backfill_chunks",
        ["status", "shop_id"],
    )

    # ── shop_sync_state ──────────────────────────────────────────────
    # Per-shop high-level scheduler state.
    op.create_table(
        "shop_sync_state",
        sa.Column("shop_id", sa.Integer(), nullable=False),
        sa.Column(
            "backfill_status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("backfill_through_date", sa.Date(), nullable=True),
        # Infra timestamps — naive UTC.
        sa.Column("last_hourly_at", sa.DateTime(timezone=False), nullable=True),
        sa.Column("last_nightly_refetch_at", sa.DateTime(timezone=False), nullable=True),
        sa.Column("last_expenses_at", sa.DateTime(timezone=False), nullable=True),
        sa.PrimaryKeyConstraint("shop_id"),
    )


def downgrade() -> None:
    op.drop_table("shop_sync_state")
    op.drop_index("ix_shop_backfill_chunks_status", table_name="shop_backfill_chunks")
    op.drop_table("shop_backfill_chunks")
    op.drop_index("ix_expenses_ledger_shop_charged", table_name="expenses_ledger")
    op.drop_index("ix_expenses_ledger_shop_day", table_name="expenses_ledger")
    op.drop_table("expenses_ledger")
    op.drop_index("ix_sales_lines_shop_sku_created", table_name="sales_lines")
    op.drop_index("ix_sales_lines_shop_created", table_name="sales_lines")
    op.drop_table("sales_lines")
