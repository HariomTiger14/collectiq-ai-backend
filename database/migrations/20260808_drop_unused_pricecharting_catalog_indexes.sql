-- pricecharting_catalog carries 13 indexes; every write (daily bulk refresh,
-- 15-min comics/coins/sports backfill, hourly portfolio matching) has to
-- maintain all of them, and this is the direct cause of the statement
-- timeouts hit on 2026-08-08 (57014, collectiq-pricecharting-refresh-sit
-- crashed writing just 51 rows). See scripts/refresh_pricecharting_catalog.py
-- and app/services/pricing/catalog_search_service.py for the write/read
-- paths this affects.
--
-- Confirmed via pg_stat_user_indexes (idx_scan — Postgres's own lifetime
-- per-index usage counter), not guessed:
--
--   index                                          idx_scan   size
--   pricecharting_catalog_console_name_idx         0          11 MB
--   pricecharting_catalog_normalized_identity_idx  0          69 MB
--   pricecharting_catalog_product_name_idx         0          40 MB
--   pricecharting_catalog_search_idx               0          40 MB
--   pricecharting_catalog_upc_idx                  1          7.5 MB
--
-- These five predate the 2026-08-06 trigram-index migration
-- (20260806_add_trigram_indexes_for_catalog_search.sql), which replaced the
-- query pattern they served: search_pricecharting_catalog() now filters via
-- ilike '%text%' backed by pg_trgm GIN indexes (247-253 real idx_scans each,
-- confirmed live), not lower(column) equality/prefix or full-text @@
-- matching. pricecharting_catalog_search_idx (the to_tsvector/GIN full-text
-- index) was explicitly built for a full-text relevance-ranking fix that was
-- never shipped (see the comment in catalog_search_service.py:_fetch_rows) —
-- a full repo grep (this backend + the admin portal) turns up zero
-- to_tsvector/@@/textSearch call sites anywhere.
-- pricecharting_catalog_upc_idx is superseded by pricecharting_catalog_upc_
-- trgm_idx for the same reason (its own migration's comment already noted
-- Postgres was only using it as a "weak substitute" before the trigram
-- index existed).
--
-- None of these five are unique constraints or FK targets, so dropping them
-- is a pure performance change with zero data-integrity impact. This frees
-- ~167 MB of index maintenance cost from every single INSERT/UPDATE on this
-- table.

drop index if exists public.pricecharting_catalog_console_name_idx;
drop index if exists public.pricecharting_catalog_normalized_identity_idx;
drop index if exists public.pricecharting_catalog_product_name_idx;
drop index if exists public.pricecharting_catalog_search_idx;
drop index if exists public.pricecharting_catalog_upc_idx;
