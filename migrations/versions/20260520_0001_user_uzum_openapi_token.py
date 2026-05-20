"""Add users.uzum_openapi_token — per-user Uzum Seller OpenAPI token.

Revision ID: 20260520_0001
Revises: 20260420_0001
Create Date: 2026-05-20

NOTE: This migration is intentionally rebased on ``20260420_0001`` (the DB
head at the time of writing) rather than ``20260425_0004``. The 0425 chain
drops legacy finance tables (``finance_orders``, ``finance_sync_log``,
``sync_jobs``, ...) which were still populated when this column was added;
that retirement is tracked separately. Once the 0425 drops are applied,
this migration's parent should be updated (or a merge revision authored).

Adds a nullable VARCHAR(500) column to the ``users`` table that stores the
user's personal Uzum Seller OpenAPI token (separate from the existing
admin/browser ``api_key``). Populated after the first successful
``GET /api/seller-openapi/v1/shops`` probe from the My Shops page.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260520_0001"
down_revision: Union[str, Sequence[str], None] = "20260420_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("uzum_openapi_token", sa.String(length=500), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "uzum_openapi_token")
