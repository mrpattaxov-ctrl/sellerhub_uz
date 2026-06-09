"""Cache table for FBS invoice akt (Акт отправки) PDFs.

Revision ID: 20260608_0001
Revises: 20260530_0001
Create Date: 2026-06-08

Uzum's ``/v1/fbs/invoice/{id}/print`` endpoint rate-limits hard (~4 quick
calls → HTTP 429). The background sync worker pre-fetches each active
invoice's akt PDF into this table (paced, no burst), so the seller's bulk
"Akt отправки (PDF)" print reads from the DB — instant, zero Uzum calls,
never 429. See models.py::FbsInvoiceAkt.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260608_0001"
down_revision: Union[str, Sequence[str], None] = "20260530_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "fbs_invoice_akts",
        sa.Column("invoice_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("date_updated", sa.BigInteger(), nullable=True),
        sa.Column("pdf", sa.LargeBinary(), nullable=False),
        sa.Column(
            "synced_at", sa.DateTime(), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index("ix_fbs_invoice_akts_user_id", "fbs_invoice_akts", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_fbs_invoice_akts_user_id", table_name="fbs_invoice_akts")
    op.drop_table("fbs_invoice_akts")
