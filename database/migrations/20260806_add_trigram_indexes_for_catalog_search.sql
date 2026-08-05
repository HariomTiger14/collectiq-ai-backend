-- The real bottleneck in Discover's catalog search, found via EXPLAIN
-- ANALYZE (not guessed): the existing ilike('%text%') filter in
-- catalog_search_service.py / search_pricecharting_catalog() cannot use any
-- existing index (leading wildcard) and forces a full sequential scan of
-- pricecharting_catalog (~433K rows) on every search — 9.5s for 'pokemon'
-- (92,347 of 433,620 rows matched). This was true before any of today's
-- ordering changes; likely explains much of the pre-existing
-- "catalog_search_unavailable" flakiness on broad queries.
--
-- pg_trgm lets Postgres use a GIN index for ILIKE '%text%' patterns
-- specifically (unlike a plain btree, which only helps prefix matches) —
-- this is the standard fix for fast substring search, and does not change
-- match semantics at all.

create extension if not exists pg_trgm;

create index if not exists pricecharting_catalog_product_name_trgm_idx
    on public.pricecharting_catalog using gin (product_name gin_trgm_ops);

create index if not exists pricecharting_catalog_console_name_trgm_idx
    on public.pricecharting_catalog using gin (console_name gin_trgm_ops);

create index if not exists pricecharting_catalog_category_trgm_idx
    on public.pricecharting_catalog using gin (category gin_trgm_ops);

create index if not exists pricecharting_catalog_normalized_identity_trgm_idx
    on public.pricecharting_catalog using gin (normalized_identity gin_trgm_ops);

-- Missed on the first pass: without this, the existing partial index
-- pricecharting_catalog_upc_idx (WHERE upc IS NOT NULL) gets used as a weak
-- substitute — every row with ANY upc value gets pulled in, then the real
-- ilike pattern is rechecked by hand against all of them. Confirmed live via
-- EXPLAIN ANALYZE: this alone was responsible for most of the remaining cost
-- after the other four trigram indexes were already working correctly.
create index if not exists pricecharting_catalog_upc_trgm_idx
    on public.pricecharting_catalog using gin (upc gin_trgm_ops);
