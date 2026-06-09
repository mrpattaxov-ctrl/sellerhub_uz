"""Add User komitent (legal entity) fields for FBS Akt отправки PDF.

Revision ID: 20260525_0002
Revises: 20260525_0001
Create Date: 2026-05-25

Adds five legal-entity fields needed to populate the FBS invoice transfer
act (Акт приёма-передачи заказов) PDF:

  - full_name_legal: ФИО / IP nomi
  - contract_number: договор № (with Uzum Market)
  - pinfl: ПИНФЛ
  - legal_address: юридический адрес
  - inn: ИНН

Uzum Seller OpenAPI does not expose these (verified via 35-endpoint
probe — all 403 RBAC), so users fill them in via the profile page.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260525_0002"
down_revision: Union[str, Sequence[str], None] = "20260525_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("full_name_legal", sa.String(length=300), nullable=True))
    op.add_column("users", sa.Column("contract_number", sa.String(length=64), nullable=True))
    op.add_column("users", sa.Column("pinfl", sa.String(length=32), nullable=True))
    op.add_column("users", sa.Column("legal_address", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("inn", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "inn")
    op.drop_column("users", "legal_address")
    op.drop_column("users", "pinfl")
    op.drop_column("users", "contract_number")
    op.drop_column("users", "full_name_legal")
