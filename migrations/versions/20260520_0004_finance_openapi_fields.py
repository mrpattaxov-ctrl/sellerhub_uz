"""Add columns populated by the Uzum Seller OpenAPI finance migration.

Revision ID: 20260520_0004
Revises: 20260520_0003
Create Date: 2026-05-20

Companion to the earlier products-side OpenAPI migration (0002). Adds:

  * ``sales_lines.product_image`` (JSONB)
      The ``productImage`` object from /v1/finance/orders: a multi-resolution
      photo dict (keys 60/80/120/240/480/540/720/800/original/24034 → {high,
      low} URL pairs) plus ``photoKey``, ``color``, ``hasVerticalPhoto``.
      Stored verbatim so we don't lose data when Uzum adds new resolutions.
  * ``sales_lines.qty_cancelled`` (Integer)
      OpenAPI exposes the per-line cancelled-count separately from sold-count
      (``cancelled`` field). The browser SELLS_REPORT encodes cancellations
      as their own ``Отменен`` rows that we drop at ingest, so this column
      stays at its default (0) for any row whose source was the CSV.

  * ``expenses_ledger.date_created`` / ``date_updated`` (DateTime)
      OpenAPI's ``dateCreated`` and ``dateUpdated`` audit timestamps for a
      payment record (when Uzum created the row, when it was last touched).
      Separate from ``charged_at`` which is ``dateService``.
  * ``expenses_ledger.seller_id`` (BigInteger)
      Uzum's seller id (distinct from ``shop_id``). One seller can own many
      shops; useful for cross-shop financial reporting later.
  * ``expenses_ledger.external_id`` (String 120) / ``code`` (String 80)
      Free-form identifiers Uzum returns for some payment types. Saved
      verbatim — never parsed.

All columns are nullable / 0-defaulted. Rows ingested via the legacy CSV path
keep their old values and these new columns stay NULL or 0 until an OpenAPI
ingest overwrites them via DELETE+INSERT (sales) or UPSERT (expenses).
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260520_0004"
down_revision: Union[str, Sequence[str], None] = "20260520_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # sales_lines new columns
    op.add_column(
        "sales_lines",
        sa.Column("product_image", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "sales_lines",
        sa.Column(
            "qty_cancelled",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )

    # expenses_ledger new columns
    op.add_column("expenses_ledger", sa.Column("date_created", sa.DateTime(), nullable=True))
    op.add_column("expenses_ledger", sa.Column("date_updated", sa.DateTime(), nullable=True))
    op.add_column("expenses_ledger", sa.Column("seller_id", sa.BigInteger(), nullable=True))
    op.add_column("expenses_ledger", sa.Column("external_id", sa.String(length=120), nullable=True))
    op.add_column("expenses_ledger", sa.Column("code", sa.String(length=80), nullable=True))


def downgrade() -> None:
    op.drop_column("expenses_ledger", "code")
    op.drop_column("expenses_ledger", "external_id")
    op.drop_column("expenses_ledger", "seller_id")
    op.drop_column("expenses_ledger", "date_updated")
    op.drop_column("expenses_ledger", "date_created")
    op.drop_column("sales_lines", "qty_cancelled")
    op.drop_column("sales_lines", "product_image")
