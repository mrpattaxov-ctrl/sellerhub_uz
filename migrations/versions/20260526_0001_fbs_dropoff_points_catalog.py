"""Global drop-off (qabul) point catalog for FBS picker.

Revision ID: 20260526_0001
Revises: 20260525_0002
Create Date: 2026-05-26

Uzum's ``GET /v1/fbs/invoice/dop/drop-off-points`` only returns points
matching the caller's *current* orders' dimensional groups + capacity,
so a seller with few active orders sees few points (2-7 typically).

This table pools every point any seller has ever fetched into one
catalog, so the picker can serve a comprehensive list even when the
live Uzum response is narrow. The endpoint upserts each returned point
on every call; stale entries (last_seen_at > 30 days) are filtered out
at query time, not deleted.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260526_0001"
down_revision: Union[str, Sequence[str], None] = "20260525_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Idempotent: app.py runs ``Base.metadata.create_all`` on startup
    # which can race ahead of this migration. Skip CREATE if the table
    # already exists; downgrade still works (it just becomes a no-op
    # against a missing table, which the SQL has IF EXISTS for).
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "fbs_dropoff_points_catalog" not in inspector.get_table_names():
        op.create_table(
            "fbs_dropoff_points_catalog",
            sa.Column("uuid", sa.String(length=64), primary_key=True, nullable=False),
            sa.Column("address", sa.Text(), nullable=False),
            sa.Column("latitude", sa.Float(), nullable=True),
            sa.Column("longitude", sa.Float(), nullable=True),
            sa.Column(
                "working_hours_json",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
            sa.Column(
                "dimensional_group_is_large",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
            sa.Column("point_type", sa.String(length=64), nullable=True),
            sa.Column(
                "first_seen_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.Column(
                "last_seen_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
        )
    # Index is also idempotent — re-inspect after potential create.
    existing_indexes = {
        ix["name"]
        for ix in inspector.get_indexes("fbs_dropoff_points_catalog")
    } if "fbs_dropoff_points_catalog" in inspector.get_table_names() else set()
    if "ix_fbs_dropoff_points_catalog_last_seen_at" not in existing_indexes:
        op.create_index(
            "ix_fbs_dropoff_points_catalog_last_seen_at",
            "fbs_dropoff_points_catalog",
            ["last_seen_at"],
        )


def downgrade() -> None:
    op.drop_index(
        "ix_fbs_dropoff_points_catalog_last_seen_at",
        table_name="fbs_dropoff_points_catalog",
    )
    op.drop_table("fbs_dropoff_points_catalog")
