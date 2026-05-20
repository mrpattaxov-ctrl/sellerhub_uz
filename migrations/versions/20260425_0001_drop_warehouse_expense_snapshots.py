"""Step 1: drop warehouse_expense_snapshots — orphan-write table retired.

Revision ID: 20260425_0001
Revises: 20260420_0001
Create Date: 2026-04-25

Drops the legacy `warehouse_expense_snapshots` table and its
`ix_wes_day_shop` index. The table was populated by a daily 23:30
background loop hitting /api/seller/finance/expenses, but nothing in the
live codebase reads it — the expenses page now reads from
`expenses_ledger` (core/sales_reads.py:355-498). Step 1 of the legacy
finance pipeline retirement.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "20260425_0001"
down_revision: Union[str, Sequence[str], None] = "20260420_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("ix_wes_day_shop", table_name="warehouse_expense_snapshots")
    op.drop_table("warehouse_expense_snapshots")


def downgrade() -> None:
    op.create_table(
        "warehouse_expense_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("expense_date", sa.Date(), nullable=False),
        sa.Column("shop_id", sa.String(length=64), nullable=False),
        sa.Column("items_json", sa.Text(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("total_amount", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "captured_at",
            sa.DateTime(timezone=False),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_warehouse_expense_snapshots_expense_date",
        "warehouse_expense_snapshots",
        ["expense_date"],
    )
    op.create_index(
        "ix_warehouse_expense_snapshots_shop_id",
        "warehouse_expense_snapshots",
        ["shop_id"],
    )
    op.create_index(
        "ix_warehouse_expense_snapshots_captured_at",
        "warehouse_expense_snapshots",
        ["captured_at"],
    )
    op.create_index(
        "ix_wes_day_shop",
        "warehouse_expense_snapshots",
        ["expense_date", "shop_id"],
    )
