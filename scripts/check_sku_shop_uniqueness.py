"""Pre-flight check: every Variant.sku maps to at most one ProductGroup.shop_id.

Path C routes Uzum SELLS_REPORT rows by ``Variant.sku → ProductGroup.shop_id``.
That's only safe if the mapping is a function — otherwise a SKU shared across
two shops would be written to whichever shop the dict iteration hit last.
``Variant.sku`` is indexed but NOT unique in models.py, so verify here before
deploy. Adding a UNIQUE constraint is a separate migration.

Exit code:
  0 — zero conflicts, safe to deploy Path C
  1 — at least one SKU maps to >1 shop; investigate the catalog first

Usage:
    python scripts/check_sku_shop_uniqueness.py
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sqlalchemy import text

from extensions import SessionLocal


_QUERY = text("""
    SELECT v.sku, COUNT(DISTINCT pg.shop_id) AS n_shops
    FROM variants v
    JOIN product_groups pg ON v.group_id = pg.id
    WHERE v.sku IS NOT NULL
    GROUP BY v.sku
    HAVING COUNT(DISTINCT pg.shop_id) > 1
    ORDER BY n_shops DESC, v.sku ASC
""")


def main() -> int:
    with SessionLocal() as db:
        rows = db.execute(_QUERY).all()

    n = len(rows)
    if n == 0:
        print("OK — 0 SKUs map to more than one shop. Path C routing is safe.")
        return 0

    print(f"FAIL — {n} SKU(s) map to more than one shop.")
    print("First 20 examples (sku, n_shops):")
    for sku, n_shops in rows[:20]:
        print(f"  {sku!r}\t{n_shops}")
    print(
        "\nDo NOT deploy Path C until these are resolved. "
        "Routing would silently send rows to the wrong shop for these SKUs."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
