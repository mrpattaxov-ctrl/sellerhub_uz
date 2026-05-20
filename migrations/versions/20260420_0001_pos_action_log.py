"""Phase: pos_action_log — POS terminal audit + undo.

Revision ID: 20260420_0001
Revises: 20260419_0001
Create Date: 2026-04-20

Adds pos_action_log to power the "Recent actions" panel and per-entry undo
on the POS terminal. Each row captures one committed POS transaction (mode
= sale | stock_in), with an items_json snapshot sufficient to reverse the
mutation (variant_id, qty_before, qty_after, variant_sale_id when the
action was a sale).

Per-user retention is enforced by pos/routes.py (keep 20 newest rows per
user on insert); no DB-side trigger.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260420_0001"
down_revision: Union[str, Sequence[str], None] = "20260419_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pos_action_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("shop_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("items_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column("reverted_at", sa.DateTime(timezone=False), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["shop_id"], ["shops.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_pos_action_log_user_id",
        "pos_action_log",
        ["user_id"],
    )
    op.create_index(
        "ix_pos_action_log_shop_id",
        "pos_action_log",
        ["shop_id"],
    )
    op.create_index(
        "ix_pos_action_log_created_at",
        "pos_action_log",
        ["created_at"],
    )
    op.create_index(
        "ix_pos_action_log_user_created",
        "pos_action_log",
        ["user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_pos_action_log_user_created", table_name="pos_action_log")
    op.drop_index("ix_pos_action_log_created_at", table_name="pos_action_log")
    op.drop_index("ix_pos_action_log_shop_id", table_name="pos_action_log")
    op.drop_index("ix_pos_action_log_user_id", table_name="pos_action_log")
    op.drop_table("pos_action_log")
