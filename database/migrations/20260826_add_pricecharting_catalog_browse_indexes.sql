-- Indexes backing category browse in Discover.
--
-- Browse means "the most valuable rows in a category, at any page depth".
-- These two btrees make that an equality + ordered range scan, so paging
-- to offset 200 in Yu-Gi-Oh costs the same as offset 0 -- the previous
-- design (walk the global price index, ilike-filter each row) measured
-- 80s there, because a sparse category's matches hide among the priciest
-- rows of the whole 12M-row catalog and every candidate needed a heap
-- visibility check on a table whose repricing churn keeps the visibility
-- map permanently dirty.
--
--   * browse_category_price: keyword categories. Keyed on
--     pricecharting_browse_category(category) -- see
--     20260826_add_pricecharting_browse_category_function.sql -- which
--     collapses the free-text category column to the canonical keyword
--     the service layer sends. The partial predicate keeps out rows
--     browse can never return (no price, or no recognized category), so
--     the index holds only browseable rows.
--   * browse_platform_price: the Video Games paths, which filter on the
--     precomputed platform_group column instead of category keywords.
--
-- The sort keys match the browse ORDER BY exactly (`desc` = NULLS FIRST
-- is moot under the not-null predicate, but the pathkeys must match
-- literally or the planner sorts instead of scanning).
--
-- MUST NOT run inside a transaction (CONCURRENTLY). Apply with
-- scripts/build_catalog_browse_indexes.sh, which disables the statement
-- timeout in-session (the Supabase pooler drops PGOPTIONS), reports
-- progress, drops invalid leftovers from cancelled builds, and is safe to
-- re-run until everything is valid.

CREATE INDEX CONCURRENTLY IF NOT EXISTS pricecharting_catalog_browse_category_price_idx
    ON public.pricecharting_catalog (
        public.pricecharting_browse_category(category),
        loose_price_cents DESC,
        pricecharting_id ASC
    )
    WHERE loose_price_cents IS NOT NULL
      AND public.pricecharting_browse_category(category) IS NOT NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS pricecharting_catalog_browse_platform_price_idx
    ON public.pricecharting_catalog (
        platform_group,
        loose_price_cents DESC,
        pricecharting_id ASC
    )
    WHERE loose_price_cents IS NOT NULL
      AND platform_group IS NOT NULL;

-- The first attempt at browse support: a covering index over the global
-- price order, INCLUDE (category, platform_group), filtered while
-- walking. Superseded by the two above -- see the header for why it
-- could not make deep pages fast -- and dropped rather than kept, since
-- at 556MB it charges every repricing write for an index nothing reads.
DROP INDEX CONCURRENTLY IF EXISTS pricecharting_catalog_browse_price_idx;
