-- Drop two indexes on pricecharting_catalog that nothing can use.
--
-- Both were created by hand rather than through a migration, so production
-- carried schema this directory did not describe. Recording the drop here
-- puts them back under version control, even though the drop itself was
-- already applied on 2026-09-02.
--
-- pg_stat_user_indexes showed 0 scans for each, and pg_stat_database.stats_reset
-- was NULL -- the counters had never been reset, so "0" means never used, not
-- "not used lately". Low scan counts elsewhere on this table are a different
-- matter and were deliberately left alone: with a single tester, a rarely-hit
-- index is expected to look idle and will be exercised at launch.
--
--   pricecharting_catalog_console_slug_idx (228 MB)
--     btree on regexp_replace(lower(console_name), '[^a-z0-9]+', '-', 'g').
--     Postgres only uses an expression index when a query contains that exact
--     expression, and no code in this repo produces it -- so it could never be
--     chosen regardless of traffic. It was still maintained on every write.
--
--   zz_proof_funko
--     Partial btree on (loose_price_cents desc, pricecharting_id) where
--     category ilike '%Funko%'. Named "proof", unreferenced, no matching
--     query shape. A leftover experiment.

drop index concurrently if exists public.pricecharting_catalog_console_slug_idx;
drop index concurrently if exists public.zz_proof_funko;
