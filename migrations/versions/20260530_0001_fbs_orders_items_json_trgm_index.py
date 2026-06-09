"""pg_trgm GIN index on fbs_orders.items_json for fast search (Bosqich A.5 #8).

Revision ID: 20260530_0001
Revises: 20260526_0001
Create Date: 2026-05-30

The FBS list search (``q`` param) matches a substring against the JSONB
``items_json`` column cast to text::

    cast(FbsOrder.items_json, String).ilike(f"%{q}%")
    -- core/fbs_data.py: _read_orders_from_db / get_fbs_orders_for_shops

Today that's a sequential scan over every row of ``fbs_orders``. A
trigram GIN index lets Postgres satisfy the ``ILIKE '%...%'`` from the
index instead, which matters once a busy seller's history climbs past
~10k rows.

⚠️ NOT auto-applied — review before running. This migration is written
   but intentionally left for manual ``alembic upgrade`` after the
   checks below. Reasons it needs a human:

   1. EXTENSION privilege: ``CREATE EXTENSION pg_trgm`` requires a
      superuser (or a role with CREATE on the database). On managed
      Postgres (e.g. the daymarket cluster) confirm the app role can
      create it, or have a DBA pre-create it once.

   2. Build lock on a large table: plain ``CREATE INDEX`` takes an
      ACCESS EXCLUSIVE-ish share lock that blocks writes for the build
      duration. On a big ``fbs_orders`` prefer ``CREATE INDEX
      CONCURRENTLY`` in a maintenance window — but CONCURRENTLY cannot
      run inside Alembic's transaction, so run it by hand:

          CREATE EXTENSION IF NOT EXISTS pg_trgm;
          CREATE INDEX CONCURRENTLY IF NOT EXISTS
            ix_fbs_orders_items_json_trgm
            ON fbs_orders USING gin ((items_json::text) gin_trgm_ops);

      and then ``alembic stamp 20260530_0001`` to mark it applied.

   3. Expression match: the planner only uses this index if the query's
      cast expression matches the index's. The index is on
      ``(items_json::text)`` (CAST AS text). SQLAlchemy ``cast(col,
      String)`` emits ``CAST(... AS VARCHAR)`` — Postgres may treat
      VARCHAR and TEXT as *different* expressions for index matching. If
      ``EXPLAIN ANALYZE`` on a real search still shows a Seq Scan, change
      the read-path filter to ``cast(FbsOrder.items_json, Text)`` so it
      lines up with the index, then re-check.

   Verify with::

       EXPLAIN ANALYZE
       SELECT * FROM fbs_orders
       WHERE items_json::text ILIKE '%foo%';

   Expect a Bitmap Index Scan on ix_fbs_orders_items_json_trgm.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "20260530_0001"
down_revision: Union[str, Sequence[str], None] = "20260526_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Idempotent: IF NOT EXISTS on both the extension and the index so a
    # re-run (or a manual CONCURRENTLY build already done by hand) is a
    # no-op rather than an error.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_fbs_orders_items_json_trgm "
        "ON fbs_orders USING gin ((items_json::text) gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_fbs_orders_items_json_trgm")
    # Leave the pg_trgm extension in place — other indexes/queries may
    # depend on it, and dropping an extension is rarely what a rollback
    # actually wants.
