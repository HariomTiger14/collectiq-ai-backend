-- Index backing category browse in Discover.
--
-- Browsing a category means "the most valuable things in it", which is an
-- ordered scan of the price index filtered by category. Nothing in the
-- table could serve that: pricecharting_catalog_loose_price_cents_idx
-- carries the ordering but not the category, so every candidate row had
-- to be fetched from the heap just to test it -- and this table is ~12M
-- rows / 16GB. Yu-Gi-Oh at offset 200 walks past ~12k more-expensive rows
-- to find its 220 matches, which measured 80s.
--
-- Carrying `category` and `platform_group` as INCLUDE columns makes that
-- walk an index-only scan: search_pricecharting_catalog's browse branch
-- selects nothing but pricecharting_id/loose_price_cents/category in its
-- inner page query, so the whole filter runs inside the index and only
-- the ~20 surviving ids are joined back for their full rows.
--
-- Column choices:
--   * `loose_price_cents desc, pricecharting_id asc` matches the browse
--     ORDER BY exactly, including NULLS FIRST (`desc`'s default). That is
--     moot given the partial predicate, but the pathkeys have to match
--     literally or the planner sorts instead of walking.
--   * `where loose_price_cents is not null` mirrors the browse filter, so
--     the index only holds rows browse can return. Unpriced rows are the
--     bulk of the catalog and the least useful thing to open a category
--     with, so excluding them shrinks the index substantially.
--   * `platform_group` is included so the Video Games browse path (which
--     filters on platform_group instead of category keywords) stays
--     index-only too.
--
-- ONE index rather than one per category. A partial index per browse
-- category would give an even tighter scan, but CREATE INDEX CONCURRENTLY
-- scans the whole table twice regardless of how selective the predicate
-- is, so 18 of them means 36 passes over 16GB. This is a single build that
-- serves every category, including Video Games and any future ones,
-- without another migration.
--
-- MUST NOT be run inside a transaction -- CONCURRENTLY is rejected there,
-- which is why this is not folded into a normal migration run. Use
-- scripts/build_catalog_browse_indexes.sh, which runs it in autocommit,
-- disables the statement timeout, reports progress, and cleans up an
-- invalid leftover before retrying. A killed build leaves an invalid index
-- behind; it is inert (the planner ignores it) but must be dropped before
-- the build is retried, which the script does for you.

CREATE INDEX CONCURRENTLY IF NOT EXISTS pricecharting_catalog_browse_price_idx
    ON public.pricecharting_catalog (loose_price_cents DESC, pricecharting_id ASC)
    INCLUDE (category, platform_group)
    WHERE loose_price_cents IS NOT NULL;
