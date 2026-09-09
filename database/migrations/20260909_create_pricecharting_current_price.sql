-- Current price moves off pricecharting_catalog.
--
-- NOT YET APPLIED. Steps 2 and 3 must run outside a transaction.
--
-- Why
-- ---
-- pricecharting_catalog is ~12.5M rows, ~25 GB, ~15 indexes, ~7 GB of GIN /
-- trigram. It carries the six *_price_cents columns, so a price-only change
-- rewrites the search document: GIN entries are rebuilt although no search
-- text moved, and the price indexes make the update non-HOT (~13.6% HOT).
--
-- Measured on a 350-set staged ingest of 265,130 rows after #214:
--   catalog_upsert   388.8s   49.7%
--   scd2_comparison  297.6s   38.0%
-- catalog_upsert is that write amplification. This table is where the daily
-- cents go instead: one row per id, no GIN, no trigram, a heap in the
-- hundreds of MB rather than 25 GB.
--
-- Why category and platform_group live here too
-- ---------------------------------------------
-- Not denormalisation for its own sake. Discover's browse path is served
-- ENTIRELY by two composite indexes whose leading key is a catalog column and
-- whose second key is the price:
--
--   (pricecharting_browse_category(category), loose_price_cents DESC, id)
--   (platform_group,                          loose_price_cents DESC, id)
--
-- search_pricecharting_catalog pins the leading key, range-scans the price in
-- order, and stops at limit+offset -- it never reads past the page. Split the
-- two keys across two tables and no single index can serve that; browse
-- degrades to a join plus a sort over the category. So the browse keys travel
-- with the price, and the index shapes are preserved exactly.
--
-- These two columns are the only sync obligation this table creates. The
-- daily writer has category in hand from the CSV row, so it rewrites them on
-- every price upsert and they self-heal within a day of any rename.
-- pricecharting_browse_category is IMMUTABLE, so it is usable as an index
-- expression here exactly as it is on the catalog.
--
-- No foreign key to pricecharting_catalog, matching pricecharting_price_history:
-- the catalog is never deleted from, and a 12.5M-row FK check on every write is
-- a cost with no failure to prevent.

-- Step 1 (transactional): the table.
CREATE TABLE IF NOT EXISTS public.pricecharting_current_price (
    pricecharting_id        text PRIMARY KEY,
    loose_price_cents       integer,
    cib_price_cents         integer,
    new_price_cents         integer,
    graded_price_cents      integer,
    box_only_price_cents    integer,
    manual_only_price_cents integer,
    currency                text NOT NULL DEFAULT 'USD',
    -- When the vendor observed this price, carried from the CSV download.
    -- Same meaning as pricecharting_price_history.observed_at, so a current
    -- row and its newest snapshot agree.
    observed_at             timestamptz,
    source_file             text,
    -- Browse keys. See the note above.
    category                text,
    platform_group          text,
    recorded_at             timestamptz NOT NULL DEFAULT now()
);

-- Service-role only, matching pricecharting_price_history: RLS on with no
-- policies, so PostgREST's anon/authenticated roles cannot read it directly.
-- Reads reach it through search_pricecharting_catalog and the API.
ALTER TABLE public.pricecharting_current_price ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE public.pricecharting_current_price IS
    'One row per pricecharting_id: the current vendor price. Written daily by '
    'the catalog refreshers. pricecharting_catalog keeps identity, search and '
    'metadata and no longer carries daily cents.';
COMMENT ON COLUMN public.pricecharting_current_price.category IS
    'Copied from pricecharting_catalog so the browse-by-price indexes can live '
    'on this table. Rewritten on every price upsert.';
COMMENT ON COLUMN public.pricecharting_current_price.platform_group IS
    'Copied from pricecharting_catalog for the platform browse index. '
    'Rewritten on every price upsert.';

-- Step 2 (NOT transactional): backfill from the catalog.
-- Idempotent -- ON CONFLICT DO NOTHING -- so it can be re-run, and safe to run
-- while the catalog is being written: a row that changes after it is copied is
-- corrected by the next daily upsert.
--
--   INSERT INTO public.pricecharting_current_price (
--       pricecharting_id, loose_price_cents, cib_price_cents, new_price_cents,
--       graded_price_cents, box_only_price_cents, manual_only_price_cents,
--       currency, observed_at, source_file, category, platform_group)
--   SELECT c.pricecharting_id, c.loose_price_cents, c.cib_price_cents,
--          c.new_price_cents, c.graded_price_cents, c.box_only_price_cents,
--          c.manual_only_price_cents, coalesce(c.currency, 'USD'),
--          coalesce(c.source_downloaded_at, c.updated_at), c.source_file,
--          c.category, c.platform_group
--     FROM public.pricecharting_catalog c
--   ON CONFLICT (pricecharting_id) DO NOTHING;
--
-- 12.5M rows: run it chunked by pricecharting_id range, the way
-- scripts/backfill_price_history_from_scd2.py does, rather than as one
-- statement.

-- Step 3 (NOT transactional): the read indexes.
-- Shapes copied verbatim from the catalog's, so the browse plans are the same
-- ones that are running today. CREATE INDEX CONCURRENTLY cannot run inside a
-- transaction block; run each on its own.
--
-- The catalog's equivalents are NOT dropped here. They stay until reads have
-- moved (PR 3) and writers have stopped maintaining them (PR 4); a brief
-- period of both is the cost of not having a window where neither serves.

CREATE INDEX CONCURRENTLY IF NOT EXISTS
    pricecharting_current_price_browse_category_idx
    ON public.pricecharting_current_price
       (public.pricecharting_browse_category(category), loose_price_cents DESC, pricecharting_id)
    WHERE loose_price_cents IS NOT NULL
      AND public.pricecharting_browse_category(category) IS NOT NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS
    pricecharting_current_price_browse_platform_idx
    ON public.pricecharting_current_price
       (platform_group, loose_price_cents DESC, pricecharting_id)
    WHERE loose_price_cents IS NOT NULL
      AND platform_group IS NOT NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS
    pricecharting_current_price_loose_price_idx
    ON public.pricecharting_current_price (loose_price_cents);

-- Step 4 (transactional): tell PostgREST the table exists.
--
-- PostgREST caches the schema and will 404 or 503 on a table it has not
-- reloaded. Applied late on 2026-09-10 after catalog detail briefly failed for
-- exactly this reason -- the table was live and the API could not see it. Run
-- this after ANY new table, column or RPC, not just this one.

NOTIFY pgrst, 'reload schema';
