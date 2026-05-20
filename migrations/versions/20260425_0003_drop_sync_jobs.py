"""Step 4: drop sync_jobs — manual full-sync pipeline retired.

Revision ID: 20260425_0003
Revises: 20260425_0002
Create Date: 2026-04-25

Drops the legacy `sync_jobs` table and its indexes. The table tracked
async manual / onboarding finance-sync job state for the legacy
`/api/seller/finance/orders` pipeline (`_run_manual_sync_job` →
`_finance_sync_range`). That pipeline is retired in step 4 of the
legacy retirement plan: `_onboarding_backfill_loop` now seeds
`sales_lines` from 2022→today and `_sync_finance_for_shop` covers the
last 30 days at shop add. Step 4 of the legacy finance pipeline
retirement.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "20260425_0003"
down_revision: Union[str, Sequence[str], None] = "20260425_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("ix_sync_jobs_finished_at", table_name="sync_jobs")
    op.drop_index("ix_sync_jobs_created_at", table_name="sync_jobs")
    op.drop_index("ix_sync_jobs_status", table_name="sync_jobs")
    op.drop_index("ix_sync_jobs_shop_id", table_name="sync_jobs")
    op.drop_table("sync_jobs")


def downgrade() -> None:
    op.create_table(
        "sync_jobs",
        sa.Column("job_id", sa.String(length=32), nullable=False),
        sa.Column("shop_id", sa.String(length=64), nullable=False),
        sa.Column("sync_type", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default=sa.text("'queued'")),
        sa.Column("progress_days", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("total_days", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("records_fetched", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("date_from", sa.String(length=20), nullable=True),
        sa.Column("date_to", sa.String(length=20), nullable=True),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=False),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=False), nullable=True),
        sa.PrimaryKeyConstraint("job_id"),
    )
    op.create_index("ix_sync_jobs_shop_id", "sync_jobs", ["shop_id"])
    op.create_index("ix_sync_jobs_status", "sync_jobs", ["status"])
    op.create_index("ix_sync_jobs_created_at", "sync_jobs", ["created_at"])
    op.create_index("ix_sync_jobs_finished_at", "sync_jobs", ["finished_at"])
