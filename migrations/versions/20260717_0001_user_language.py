"""Add User.language (Telegram bot preferred language).

Revision ID: 20260717_0001
Revises: 20260612_0001
Create Date: 2026-07-17

Stores the user's chosen bot language ("ru" | "uz"), selected via a
bottom keyboard picker shown right after the contact-share step in the
Telegram registration flow. Null means the user has not chosen yet.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260717_0001"
down_revision: Union[str, Sequence[str], None] = "20260612_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("language", sa.String(2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "language")
