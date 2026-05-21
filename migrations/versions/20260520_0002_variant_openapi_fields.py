"""Add Variant columns populated by the Uzum Seller OpenAPI product sync.

Revision ID: 20260520_0002
Revises: 20260520_0001
Create Date: 2026-05-20

The legacy browser-token /api/seller/.../getProducts response did not expose
several fields that the new /v1/product/shop/{shopId} OpenAPI endpoint does:

  * productTitle  → stored separately per language (RU / UZ) by issuing two
                    requests with different Accept-Language headers.
  * quantityCreated, quantityFbs, quantityAdditional, quantityArchived,
    quantityPending, quantityDefected, quantityMissing — richer warehouse
    breakdown than the single quantityActive the browser endpoint returns.
  * blocked / blockingReason / skuBlockReason — surface *why* a SKU is
    unavailable instead of inferring from the status object.
  * ikpu — Uzbekistan ИКПУ tax code, required for fiscal integrations.

All columns are nullable; existing rows synced via the browser path keep
their old values and these new columns stay NULL until the OpenAPI sync
runs for that shop.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260520_0002"
down_revision: Union[str, Sequence[str], None] = "20260520_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_NEW_COLUMNS = [
    ("product_title_ru",     sa.String(length=300)),
    ("product_title_uz",     sa.String(length=300)),
    ("quantity_created",     sa.Integer()),
    ("quantity_fbs",         sa.Integer()),
    ("quantity_additional",  sa.Integer()),
    ("quantity_archived",    sa.Integer()),
    ("quantity_pending",     sa.Integer()),
    ("quantity_defected",    sa.Integer()),
    ("quantity_missing",     sa.Integer()),
    ("blocked",              sa.Boolean()),
    ("blocking_reason",      sa.String(length=500)),
    ("sku_block_reason",     sa.Text()),
    ("ikpu",                 sa.String(length=80)),
]


def upgrade() -> None:
    for name, col_type in _NEW_COLUMNS:
        op.add_column("variants", sa.Column(name, col_type, nullable=True))


def downgrade() -> None:
    for name, _ in reversed(_NEW_COLUMNS):
        op.drop_column("variants", name)
