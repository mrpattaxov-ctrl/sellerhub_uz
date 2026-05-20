-- ─────────────────────────────────────────────────────────────────────
-- One-time cleanup of contaminated sales_lines rows written under the
-- wrong shop_id by the pre-Path-C race bug (project_uzum_race_bug_fix.md).
--
-- Routing source-of-truth: Variant.sku → ProductGroup.shop_id → Shop.uzum_id.
-- sales_lines.shop_id stores int(Shop.uzum_id), NOT the local PK
-- (see core/sales_reads.py docstring + finance/routes.py readers).
--
-- DO NOT run this script automatically. The user runs prod SQL by hand.
-- Run the COUNT first, eyeball the number, then run the DELETE.
-- ─────────────────────────────────────────────────────────────────────


-- ── 1. DRY RUN: how many rows would be deleted ────────────────────────
-- Join through shops so we compare uzum_id space to uzum_id space.
-- Shop.uzum_id is String(64) per models.py → explicit cast to BIGINT.

SELECT COUNT(*) AS contaminated_rows
FROM sales_lines sl
JOIN variants v        ON v.sku = sl.sku_id
JOIN product_groups pg ON pg.id = v.group_id
JOIN shops s           ON s.id  = pg.shop_id
WHERE s.uzum_id IS NOT NULL
  AND CAST(s.uzum_id AS BIGINT) <> sl.shop_id;


-- ── 2. DESTRUCTIVE: only run after reviewing the count above ──────────
-- This deletes every sales_lines row whose stored shop_id disagrees with
-- the SKU→shop catalog. Wrap in a transaction if you want a safety net.

-- BEGIN;
DELETE FROM sales_lines sl
USING variants v, product_groups pg, shops s
WHERE v.sku = sl.sku_id
  AND v.group_id = pg.id
  AND pg.shop_id = s.id
  AND s.uzum_id IS NOT NULL
  AND CAST(s.uzum_id AS BIGINT) <> sl.shop_id;
-- COMMIT;
