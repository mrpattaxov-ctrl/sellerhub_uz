"""Drop sales_lines and shop_backfill_chunks tables.

Revision ID: 20260523_0001
Revises: 20260521_0001
Create Date: 2026-05-23

The legacy SELLS_REPORT CSV pipeline (and its OpenAPI per-line fallback)
has been retired in favour of finance_orders + finance_hourly_snapshots.
With Telegram + Unit Economics + the manual variant-sync endpoint all
reading from finance_orders now, no live code writes to or reads from
sales_lines / shop_backfill_chunks anymore.

The full implementations of the retired pipeline are archived in
``legacy/sales_lines_pipeline.py`` (plain-text snapshot, not imported)
so a future maintainer can recover them without git-archaeology.

downgrade() recreates the tables empty — the actual data is not
recoverable once dropped, but the schema can be restored to allow
re-importing the archived code if needed.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260523_0001"
down_revision: Union[str, Sequence[str], None] = "20260521_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS sales_lines CASCADE")
    op.execute("DROP TABLE IF EXISTS shop_backfill_chunks CASCADE")


def downgrade() -> None:
    # Recreate empty tables matching the schema we just dropped. Useful
    # only as a structural rollback target — historical data is gone.
    op.create_table(
        "sales_lines",
        sa.Column("shop_id", sa.Integer(), primary_key=True),
        sa.Column("order_id", sa.String(length=64), primary_key=True),
        sa.Column("sku_id", sa.String(length=64), primary_key=True),
        sa.Column("sku_title", sa.String(length=500), nullable=True),
        sa.Column("barcode", sa.String(length=120), nullable=True),
        sa.Column("category", sa.String(length=300), nullable=True),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("received_at", sa.DateTime(), nullable=True),
        sa.Column("qty", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("qty_returns", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("revenue", sa.Numeric(18, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("seller_profit", sa.Numeric(18, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("commission", sa.Numeric(18, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("unit_price", sa.Numeric(18, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("promo_amount", sa.Numeric(18, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("purchase_price", sa.Numeric(18, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("logistics_fee", sa.Numeric(18, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("product_image", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("qty_cancelled", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("synced_at", sa.DateTime(), nullable=False),
    )
    op.execute(
        "CREATE INDEX ix_sales_lines_shop_created ON sales_lines (shop_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX ix_sales_lines_shop_sku_created "
        "ON sales_lines (shop_id, sku_id, created_at DESC)"
    )

    op.create_table(
        "shop_backfill_chunks",
        sa.Column("shop_id", sa.Integer(), primary_key=True),
        sa.Column("chunk_start", sa.Date(), primary_key=True),
        sa.Column("chunk_end", sa.Date(), primary_key=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_shop_backfill_chunks_status",
        "shop_backfill_chunks",
        ["status", "shop_id"],
    )
