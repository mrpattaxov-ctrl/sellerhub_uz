"""Add User.uzum_seller_id.

Revision ID: 20260525_0001
Revises: 20260521_0002
Create Date: 2026-05-25

Stores the Uzum seller account ID (visible as ?sId=<N> in the seller
cabinet URL) required by POST /v1/fbs/invoice's ``sellerId`` field. The
value is NOT exposed by /v1/shops or any other OpenAPI endpoint, so the
user pastes it manually once on the My Shops page.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260525_0001"
down_revision: Union[str, Sequence[str], None] = "20260521_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("uzum_seller_id", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "uzum_seller_id")
