-- STEP 3 of 3 -- run this statement ALONE, after step 2 has finished.
-- CREATE INDEX CONCURRENTLY cannot execute inside a transaction block.
--
-- Built after the backfill on purpose: an index on a column with real
-- distribution builds faster and cleaner than an unpopulated one, and
-- platform_group is functionally read-only after ingest (see
-- compute_platform_group() in import_pricecharting_catalog.py -- new rows
-- get it computed at write time), so ongoing write/index-maintenance cost
-- here should be minimal, unlike the columns this table has had incidents
-- over before.
create index concurrently if not exists pricecharting_catalog_platform_group_idx
    on public.pricecharting_catalog (platform_group)
    where platform_group is not null;
