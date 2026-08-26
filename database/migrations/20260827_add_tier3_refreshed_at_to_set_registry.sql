-- Rotation stamp for the tier-3 sportscardspro refresh
-- (scripts/refresh_sportscardspro_rotation.py).
--
-- tier1_refreshed_at can't serve this: refresh_small_sets.py bumps it on
-- every ATTEMPTED candidate, including sets it checked and skipped as too
-- large for the /api/products cap, so it means "looked at", not
-- "refreshed". Tier 3 stamps only after a set's CSV was fetched and its
-- catalog rows written, and orders its queue by this column
-- (nulls-first, oldest-first) -- the stamp IS the rotation cursor.

ALTER TABLE public.pricecharting_set_registry
    ADD COLUMN IF NOT EXISTS tier3_refreshed_at timestamptz;

-- The rotation query every run: the oldest-stamped success rows for one
-- site. Partial + tiny (registry is ~43k rows), keeps the hourly claim
-- from scanning the table.
CREATE INDEX IF NOT EXISTS pricecharting_set_registry_tier3_rotation_idx
    ON public.pricecharting_set_registry (tier3_refreshed_at ASC NULLS FIRST, registry_id ASC)
    WHERE source_site = 'sportscardspro'
      AND last_fetch_status = 'success'
      AND console_uid IS NOT NULL;
