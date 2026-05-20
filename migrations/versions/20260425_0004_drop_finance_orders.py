"""Step 5: drop finance_orders + finance_sync_log — legacy /finance/orders pipeline retired.

Revision ID: 20260425_0004
Revises: 20260425_0003
Create Date: 2026-04-25

Drops the legacy `finance_orders` and `finance_sync_log` tables and
their indexes. These tables backed the legacy
`/api/seller/finance/orders` pipeline (`_finance_sync_range` →
`FinanceOrder` per-day aggregates plus `FinanceSyncLog` audit rows).
After step 4 the writers were removed; the only remaining reader was
`/admin/sales-diff`, which is also deleted in this step. Trust now
sits entirely with `sales_lines` / `expenses_ledger`. Step 5 of the
legacy finance pipeline retirement.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "20260425_0004"
down_revision: Union[str, Sequence[str], None] = "20260425_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("ix_fo_shop_day_sku_id", table_name="finance_orders")
    op.drop_index("ix_fo_shop_sku", table_name="finance_orders")
    op.drop_index("ix_fo_shop_period", table_name="finance_orders")
    op.drop_index("ix_finance_orders_product_id", table_name="finance_orders")
    op.drop_index("ix_finance_orders_sku_id", table_name="finance_orders")
    op.drop_index("ix_finance_orders_sku_title", table_name="finance_orders")
    op.drop_index("ix_finance_orders_period_to", table_name="finance_orders")
    op.drop_index("ix_finance_orders_period_from", table_name="finance_orders")
    op.drop_index("ix_finance_orders_shop_id", table_name="finance_orders")
    op.drop_table("finance_orders")

    op.drop_index("ix_finance_sync_log_shop_id", table_name="finance_sync_log")
    op.drop_table("finance_sync_log")


def downgrade() -> None:
    op.create_table(
        "finance_orders",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("shop_id", sa.String(length=64), nullable=False),
        sa.Column("period_from", sa.Date(), nullable=False),
        sa.Column("period_to", sa.Date(), nullable=False),
        sa.Column("sku_title", sa.String(length=300), nullable=False),
        sa.Column("sku_id", sa.Integer(), nullable=True),
        sa.Column("product_id", sa.Integer(), nullable=True),
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
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=False),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_finance_orders_shop_id", "finance_orders", ["shop_id"])
    op.create_index("ix_finance_orders_period_from", "finance_orders", ["period_from"])
    op.create_index("ix_finance_orders_period_to", "finance_orders", ["period_to"])
    op.create_index("ix_finance_orders_sku_title", "finance_orders", ["sku_title"])
    op.create_index("ix_finance_orders_sku_id", "finance_orders", ["sku_id"])
    op.create_index("ix_finance_orders_product_id", "finance_orders", ["product_id"])
    op.create_index(
        "ix_fo_shop_period",
        "finance_orders",
        ["shop_id", "period_from", "period_to"],
    )
    op.create_index("ix_fo_shop_sku", "finance_orders", ["shop_id", "sku_title"])
    op.create_index(
        "ix_fo_shop_day_sku_id",
        "finance_orders",
        ["shop_id", "period_from", "sku_id"],
    )

    op.create_table(
        "finance_sync_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("shop_id", sa.String(length=64), nullable=False),
        sa.Column("sync_type", sa.String(length=20), nullable=False),
        sa.Column("date_from", sa.Date(), nullable=False),
        sa.Column("date_to", sa.Date(), nullable=False),
        sa.Column("records_fetched", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=False),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_finance_sync_log_shop_id", "finance_sync_log", ["shop_id"])
