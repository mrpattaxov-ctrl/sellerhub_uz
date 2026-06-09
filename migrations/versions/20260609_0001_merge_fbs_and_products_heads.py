"""Merge the FBS/DBS chain with the products/finance (drop_sales_lines) chain.

Revision ID: 20260609_0001
Revises: 20260523_0001, 20260608_0001
Create Date: 2026-06-09

After merging the ``astic/products`` branch into ``feat/dark-mode-and-env-token``
the repository ended up with two parallel Alembic heads:

  • …→ 20260521_0001 → 20260523_0001
      (products/finance: drop the legacy ``sales_lines`` pipeline tables)
  • …→ 20260521_0002 → 20260525_… → 20260530_0001 → 20260608_0001
      (FBS/DBS: fbs_orders, shop sync-state, seller-id, komitent, drop-off
      points catalog, items-json trgm index, invoice akts)

Both branches are independent. This empty merge revision unifies them into
a single head so ``alembic upgrade head`` works regardless of which branch
the live DB is currently on.
"""
from __future__ import annotations

from typing import Sequence, Union

revision: str = "20260609_0001"
down_revision: Union[str, Sequence[str], None] = ("20260523_0001", "20260608_0001")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
