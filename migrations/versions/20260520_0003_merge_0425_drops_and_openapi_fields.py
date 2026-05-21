"""Merge the 0425 finance-drops chain with the 0520 OpenAPI-fields chain.

Revision ID: 20260520_0003
Revises: 20260425_0004, 20260520_0002
Create Date: 2026-05-20

The repository ended up with two parallel Alembic heads:

  • 20260420_0001 → 20260425_0001 → 0002 → 0003 → 20260425_0004
      (drops legacy finance_orders / finance_sync_log / sync_jobs / etc.)
  • 20260420_0001 → 20260520_0001 → 20260520_0002
      (adds users.uzum_openapi_token and Variant.* OpenAPI columns)

Both branches are independent — one only drops legacy tables, the other
only adds new columns. This empty merge revision unifies them into a
single head so ``alembic upgrade head`` works regardless of which branch
the live DB is currently on.
"""
from __future__ import annotations

from typing import Sequence, Union

revision: str = "20260520_0003"
down_revision: Union[str, Sequence[str], None] = ("20260425_0004", "20260520_0002")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
