"""Cache table for FBS order shipping-label (jo'natma yorlig'i) PDFs.

Revision ID: 20260612_0001
Revises: 20260609_0001
Create Date: 2026-06-12

Uzum's ``/v1/fbs/order/{id}/labels/print`` is a PER-ORDER call paced 1s/token
(burst → HTTP 429), so bulk-printing 50 fresh labels costs ~50s. The background
sync worker pre-fetches each active FBS order's label into this table (paced, no
burst), so the seller's bulk "Yorliq" / "Yorliq + QR" print reads from the DB —
instant, zero Uzum calls, never 429. The label is immutable once the order is
confirmed (verified 2026-06-12), so no version stamp is needed. See
models.py::FbsOrderLabel and core/fbs_label_cache.py.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260612_0001"
down_revision: Union[str, Sequence[str], None] = "20260609_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "fbs_order_labels",
        sa.Column("order_id", sa.String(length=64), primary_key=True, autoincrement=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("size", sa.String(length=8), nullable=False, server_default="LARGE"),
        sa.Column("pdf", sa.LargeBinary(), nullable=False),
        sa.Column(
            "synced_at", sa.DateTime(), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index("ix_fbs_order_labels_user_id", "fbs_order_labels", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_fbs_order_labels_user_id", table_name="fbs_order_labels")
    op.drop_table("fbs_order_labels")
