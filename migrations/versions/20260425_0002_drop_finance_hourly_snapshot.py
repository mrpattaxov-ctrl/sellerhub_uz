"""Step 2: drop finance_hourly_snapshots — writer-only table retired.

Revision ID: 20260425_0002
Revises: 20260425_0001
Create Date: 2026-04-25

Drops the legacy `finance_hourly_snapshots` table and its
`ix_fhs_shop_hour` index. Reads were removed in Phase 3 — "За час" /
"С 00:00" Telegram numbers come straight from `sales_lines` now; only
the per-hour writer remained. Step 2 of the legacy finance pipeline
retirement.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "20260425_0002"
down_revision: Union[str, Sequence[str], None] = "20260425_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("ix_fhs_shop_hour", table_name="finance_hourly_snapshots")
    op.drop_table("finance_hourly_snapshots")


def downgrade() -> None:
    op.create_table(
        "finance_hourly_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("shop_id", sa.String(length=64), nullable=False),
        sa.Column("sku_title", sa.String(length=300), nullable=False),
        sa.Column("snapshot_hour", sa.DateTime(timezone=False), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("sell_price", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("purchase_price", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("seller_profit", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("commission", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("logistics_fee", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_finance_hourly_snapshots_shop_id",
        "finance_hourly_snapshots",
        ["shop_id"],
    )
    op.create_index(
        "ix_finance_hourly_snapshots_snapshot_hour",
        "finance_hourly_snapshots",
        ["snapshot_hour"],
    )
    op.create_index(
        "ix_fhs_shop_hour",
        "finance_hourly_snapshots",
        ["shop_id", "snapshot_hour"],
    )
