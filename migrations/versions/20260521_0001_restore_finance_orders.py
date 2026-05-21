"""Restore the legacy FinanceOrder + FinanceHourlySnapshot tables.

Revision ID: 20260521_0001
Revises: 20260520_0004
Create Date: 2026-05-21

After the OpenAPI products + finance migrations, the user opted to revert
the per-order-line sales_lines + chunked-queue design back to the legacy
per-(shop, day, sku) daily-aggregate layout. This migration creates the
two tables that drive that:

  * finance_orders            — daily aggregates per (shop, day, sku).
                                Source: /v1/finance/orders?group=true.
  * finance_hourly_snapshots  — cumulative-today totals captured every
                                HH:00. Hourly notification computes
                                delta = current row - previous row.
                                Source: pure DB math from finance_orders;
                                no API call needed.

The newer tables (sales_lines, expenses_ledger, shop_backfill_chunks,
shop_sync_state) are NOT dropped here. They stay around as data-bearing
deprecated tables until a follow-up cleanup migration. The new code
stops writing to sales_lines / shop_backfill_chunks but keeps writing
to expenses_ledger via the existing OpenAPI expenses flow (unchanged).
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260521_0001"
down_revision: Union[str, Sequence[str], None] = "20260520_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Defensive cleanup: some dev DBs still have orphaned legacy tables
    # (the original drop migration `20260425_0004` didn't take effect on
    # every environment). DROP IF EXISTS + CASCADE is safe because by
    # this point the Phase 1 code stopped writing to them ~30 days ago,
    # so anything left is stale. If you need to keep that data, snapshot
    # it before running this migration.
    op.execute("DROP TABLE IF EXISTS finance_hourly_snapshots CASCADE")
    op.execute("DROP TABLE IF EXISTS finance_orders CASCADE")

    op.create_table(
        "finance_orders",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("shop_id", sa.String(length=64), nullable=False, index=True),
        sa.Column("period_from", sa.Date(), nullable=False, index=True),
        sa.Column("period_to", sa.Date(), nullable=False, index=True),
        sa.Column("sku_title", sa.String(length=300), nullable=False, index=True),
        sa.Column("sku_id", sa.Integer(), nullable=True, index=True),
        sa.Column("product_id", sa.Integer(), nullable=True, index=True),
        sa.Column("product_title", sa.String(length=500), nullable=True),
        sa.Column("product_title_ru", sa.String(length=500), nullable=True),
        sa.Column("image_url", sa.String(length=800), nullable=True),
        sa.Column("characteristics", sa.String(length=300), nullable=True),
        sa.Column("amount", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("amount_returns", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("sell_price", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("purchase_price", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("seller_discount", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("seller_profit", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("commission", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("withdrawn_profit", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("logistics_fee", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("synced_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index(
        "ix_finance_orders_shop_period",
        "finance_orders",
        ["shop_id", "period_from", "period_to"],
    )
    op.create_index(
        "ix_finance_orders_shop_period_sku",
        "finance_orders",
        ["shop_id", "period_from", "sku_title"],
    )

    op.create_table(
        "finance_hourly_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("shop_id", sa.String(length=64), nullable=False, index=True),
        sa.Column("sku_title", sa.String(length=300), nullable=False),
        sa.Column("snapshot_hour", sa.DateTime(), nullable=False, index=True),
        sa.Column("amount", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("sell_price", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("purchase_price", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("seller_profit", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("commission", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("logistics_fee", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.create_index(
        "ix_finance_hourly_shop_hour",
        "finance_hourly_snapshots",
        ["shop_id", "snapshot_hour"],
    )


def downgrade() -> None:
    op.drop_index("ix_finance_hourly_shop_hour", table_name="finance_hourly_snapshots")
    op.drop_table("finance_hourly_snapshots")
    op.drop_index("ix_finance_orders_shop_period_sku", table_name="finance_orders")
    op.drop_index("ix_finance_orders_shop_period", table_name="finance_orders")
    op.drop_table("finance_orders")
