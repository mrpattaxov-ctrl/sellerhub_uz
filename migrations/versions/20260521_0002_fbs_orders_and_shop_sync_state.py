"""Add fbs_orders table + ShopSyncState.fbs_active/last_fbs_sync_at.

Revision ID: 20260521_0002
Revises: 20260521_0001
Create Date: 2026-05-21

Stage 4a of the FBS/DBS initiative. Creates the cache table that the
Stage 4b worker will populate every ~10 min, and adds two columns to
``shop_sync_state`` so the scheduler can tell which shops are eligible
(``fbs_active``) and when they were last refreshed (``last_fbs_sync_at``).

No backfill. The table starts empty; until the worker lands, route
``core.fbs_data`` continues to call Uzum directly on every request.

Schema highlights
-----------------
* Single table for both FBS and DBS — differentiated by ``order_type``.
  CHECK constraint locks values to ``('FBS', 'DBS')``.
* ``status`` CHECK constraint mirrors ``core.uzum_openapi.FBS_ORDER_STATUSES``
  (11 values). Bumping the Python enum requires a follow-up migration
  to drop and recreate this constraint.
* Composite index ``(shop_id, status, date_created DESC)`` is the
  workhorse — every /fbs list view filters by those two and orders by
  ``date_created DESC``, so the index serves WHERE + ORDER BY in one
  B-tree pass.
* ``items_json`` and ``raw_json`` are JSONB. We keep the full Uzum
  payload in ``raw_json`` so any new field can be surfaced without a
  migration during the worker's stabilisation period.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "20260521_0002"
down_revision: Union[str, Sequence[str], None] = "20260521_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Create fbs_orders ──────────────────────────────────────────
    op.create_table(
        "fbs_orders",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        # Identity
        sa.Column("shop_id", sa.String(length=64), nullable=False),
        sa.Column("order_id", sa.String(length=64), nullable=False, unique=True),
        sa.Column("order_type", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        # Customer
        sa.Column("customer_fullname", sa.String(length=300), nullable=True),
        sa.Column("customer_phone", sa.String(length=32), nullable=True),
        sa.Column("delivery_address", sa.Text(), nullable=True),
        sa.Column("delivery_comment", sa.Text(), nullable=True),
        # Money
        sa.Column("price", sa.Integer(), nullable=False, server_default=sa.text("0")),
        # Life-cycle dates
        sa.Column("date_created", sa.DateTime(), nullable=True),
        sa.Column("accept_until", sa.DateTime(), nullable=True),
        sa.Column("deliver_until", sa.DateTime(), nullable=True),
        sa.Column("accepted_date", sa.DateTime(), nullable=True),
        sa.Column("delivering_date", sa.DateTime(), nullable=True),
        sa.Column("delivery_date", sa.DateTime(), nullable=True),
        sa.Column("delivered_to_dp_date", sa.DateTime(), nullable=True),
        sa.Column("completed_date", sa.DateTime(), nullable=True),
        sa.Column("cancelled_date", sa.DateTime(), nullable=True),
        sa.Column("return_date", sa.DateTime(), nullable=True),
        # Misc
        sa.Column("cancel_reason", sa.String(length=200), nullable=True),
        sa.Column(
            "identifier_required", sa.Boolean(),
            nullable=False, server_default=sa.text("false"),
        ),
        sa.Column("stock_id", sa.String(length=64), nullable=True),
        sa.Column("stock_title", sa.String(length=300), nullable=True),
        sa.Column("drop_off_point_uuid", sa.String(length=64), nullable=True),
        sa.Column("drop_off_point_address", sa.Text(), nullable=True),
        sa.Column("invoice_number", sa.String(length=64), nullable=True),
        # JSONB
        sa.Column(
            "items_json", JSONB(),
            nullable=False, server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "raw_json", JSONB(),
            nullable=False, server_default=sa.text("'{}'::jsonb"),
        ),
        # Sync infra
        sa.Column(
            "synced_at", sa.DateTime(),
            nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        # CHECK constraints — enum values mirror
        # core/uzum_openapi.py:FBS_ORDER_STATUSES + FBS_ORDER_SCHEMES.
        sa.CheckConstraint(
            "order_type IN ('FBS', 'DBS')",
            name="ck_fbs_orders_order_type",
        ),
        sa.CheckConstraint(
            "status IN ("
            "'CREATED', 'PACKING', 'PENDING_DELIVERY', 'DELIVERING', "
            "'DELIVERED', 'ACCEPTED_AT_DP', "
            "'DELIVERED_TO_CUSTOMER_DELIVERY_POINT', 'COMPLETED', "
            "'CANCELED', 'PENDING_CANCELLATION', 'RETURNED'"
            ")",
            name="ck_fbs_orders_status",
        ),
    )
    # Single-column indexes (mirror the SQLAlchemy model's index=True flags).
    op.create_index("ix_fbs_orders_shop_id", "fbs_orders", ["shop_id"])
    op.create_index("ix_fbs_orders_order_id", "fbs_orders", ["order_id"])
    op.create_index("ix_fbs_orders_order_type", "fbs_orders", ["order_type"])
    op.create_index("ix_fbs_orders_status", "fbs_orders", ["status"])
    op.create_index("ix_fbs_orders_date_created", "fbs_orders", ["date_created"])
    op.create_index("ix_fbs_orders_synced_at", "fbs_orders", ["synced_at"])
    # Composite indexes from __table_args__.
    op.create_index(
        "ix_fbs_orders_shop_status_date",
        "fbs_orders",
        ["shop_id", "status", sa.text("date_created DESC")],
    )
    op.create_index(
        "ix_fbs_orders_shop_synced",
        "fbs_orders",
        ["shop_id", "synced_at"],
    )

    # ── Extend shop_sync_state ─────────────────────────────────────
    # `fbs_active` defaults to FALSE so existing shops are off until
    # someone (or a backfill in Stage 4b) flips them on.
    op.add_column(
        "shop_sync_state",
        sa.Column(
            "fbs_active", sa.Boolean(),
            nullable=False, server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "shop_sync_state",
        sa.Column("last_fbs_sync_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    # Reverse order: drop shop_sync_state columns first (they have no
    # dependencies), then drop the fbs_orders table + indexes.
    op.drop_column("shop_sync_state", "last_fbs_sync_at")
    op.drop_column("shop_sync_state", "fbs_active")

    op.drop_index("ix_fbs_orders_shop_synced", table_name="fbs_orders")
    op.drop_index("ix_fbs_orders_shop_status_date", table_name="fbs_orders")
    op.drop_index("ix_fbs_orders_synced_at", table_name="fbs_orders")
    op.drop_index("ix_fbs_orders_date_created", table_name="fbs_orders")
    op.drop_index("ix_fbs_orders_status", table_name="fbs_orders")
    op.drop_index("ix_fbs_orders_order_type", table_name="fbs_orders")
    op.drop_index("ix_fbs_orders_order_id", table_name="fbs_orders")
    op.drop_index("ix_fbs_orders_shop_id", table_name="fbs_orders")
    op.drop_table("fbs_orders")
