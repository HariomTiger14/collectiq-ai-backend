-- PROPOSAL -- NOT SAFE TO RUN AS-IS. Read the notes, then run one step at a
-- time and measure between them. Every destructive statement is commented
-- out deliberately; uncomment only the step you have decided to take.
--
-- Context (Task 7A, measured 2026-09-07 against the live database):
--
--   pricecharting_catalog: 12,528,913 rows, 25 GB total
--     heap     16 GB
--     indexes   9,173 MB across 15 indexes
--     of which  6,969 MB (76%) is five GIN trigram indexes
--
--   24,113,955 updates, only 13.6% HOT -> ~20.8M updates rewrote EVERY
--   index entry for their row, including all five GIN indexes.
--
--   176 recorded PostgresError57014 events, ALL on catalog_upsert, ALL
--   firing between 8.15s and 8.67s -- the PostgREST `authenticator` role's
--   fixed 8s statement_timeout -- at batch sizes 100, 200 and 1000, on
--   batches as small as 5 rows, while the mean upsert is 88-153ms.
--
-- The cost is live: one small-sets-refresh run on 2026-09-07 wrote 1,445
-- rows and dropped 300 to three timeouts. The application-side retry has
-- landed separately; this file is the database half.


-- ---------------------------------------------------------------------------
-- STEP 1 (recommended first, lowest risk, reversible)
-- Raise the writer's statement timeout.
-- ---------------------------------------------------------------------------
-- Current settings, verified 2026-09-07:
--     anon           statement_timeout = 3s
--     authenticated  statement_timeout = 8s
--     authenticator  statement_timeout = 8s   <- every cron writes through this
--     supabase_admin statement_timeout = 0
--
-- Every observed failure sat 0.15-0.67s past that 8s line, so a modest raise
-- converts most of them into slow successes rather than lost rows. This does
-- NOT fix the amplification underneath; it stops the bleeding while the rest
-- is measured.
--
-- Caveat to verify before relying on it: role settings apply at LOGIN, and
-- PostgREST logs in as `authenticator` then SET ROLE. Confirm the change
-- actually reaches the session -- after applying, reload the PostgREST schema
-- cache and check with:
--     select current_setting('statement_timeout');
-- through the REST API (e.g. a trivial RPC), not in a psql session.

-- ALTER ROLE authenticator SET statement_timeout = '30s';
-- NOTIFY pgrst, 'reload config';


-- ---------------------------------------------------------------------------
-- STEP 2 (measure BEFORE deciding -- this one has a real trade-off)
-- The three indexes on loose_price_cents, which are what block HOT updates.
-- ---------------------------------------------------------------------------
-- Index usage measured 2026-09-07 (compare against pkey's 46,511,921 scans):
--
--     pricecharting_catalog_loose_price_cents_idx      153 MB      24 scans
--     pricecharting_catalog_browse_category_price_idx  472 MB      41 scans
--     pricecharting_catalog_browse_platform_price_idx  5.6 MB      16 scans
--
-- A HOT update requires that NO indexed column changed. Price refresh is the
-- dominant write and changes loose_price_cents, so while ANY index references
-- that column the update is non-HOT and rewrites all 15 index entries.
--
-- Therefore dropping only the first one does NOT restore HOT. It reduces
-- index maintenance slightly and nothing else. The HOT win requires all three.
--
-- THE TRADE-OFF: the two `browse_*` indexes back Discover's browse-by-category
-- and browse-by-platform ordering by price. Their scan counts are low, but low
-- is not zero, and "unused" here may mean "used by a feature with few users
-- today" rather than "dead". Before dropping either, check what the browse
-- queries plan to without them:
--
--     EXPLAIN (ANALYZE, BUFFERS)
--     SELECT pricecharting_id, product_name, loose_price_cents
--       FROM public.pricecharting_catalog
--      WHERE pricecharting_browse_category(category) = '<a real category>'
--        AND loose_price_cents IS NOT NULL
--      ORDER BY loose_price_cents DESC
--      LIMIT 50;
--
-- Run that with the index present, then again inside a transaction that drops
-- it and rolls back, so the comparison costs nothing:
--
--     BEGIN;
--     DROP INDEX public.pricecharting_catalog_browse_category_price_idx;
--     EXPLAIN (ANALYZE, BUFFERS) <the same query>;
--     ROLLBACK;
--
-- If the seq-scan cost is unacceptable, keep the browse indexes and accept
-- that HOT stays off -- then Step 1 plus the application retry is the whole
-- fix, which may well be enough.

-- DROP INDEX CONCURRENTLY IF EXISTS public.pricecharting_catalog_loose_price_cents_idx;
-- DROP INDEX CONCURRENTLY IF EXISTS public.pricecharting_catalog_browse_category_price_idx;
-- DROP INDEX CONCURRENTLY IF EXISTS public.pricecharting_catalog_browse_platform_price_idx;


-- ---------------------------------------------------------------------------
-- STEP 3 (only meaningful if Step 2 was taken in full)
-- Leave room on the page for HOT updates.
-- ---------------------------------------------------------------------------
-- fillfactor is currently the default 100, so a page has no free space and an
-- update must go to another page even when it is otherwise HOT-eligible.
--
-- This applies to newly written/rewritten pages only -- existing pages keep
-- their packing until they are rewritten, so the benefit arrives gradually
-- rather than at ALTER time. Do NOT reach for VACUUM FULL to force it: it
-- takes an ACCESS EXCLUSIVE lock on a 25 GB table.

-- ALTER TABLE public.pricecharting_catalog SET (fillfactor = 90);


-- ---------------------------------------------------------------------------
-- STEP 4 (independent of the above; cheap)
-- GIN pending-list behaviour.
-- ---------------------------------------------------------------------------
-- gin_pending_list_limit is the default 4 MB and all five GIN indexes have
-- fastupdate ON. Most writes append cheaply to a pending list; the unlucky
-- writer that trips the limit pays the merge, which is a plausible source of
-- the multi-second tail -- it fits the evidence that batch size does not
-- predict the timeout.
--
-- fastupdate = off trades that occasional spike for a small constant cost on
-- every insert. Worth testing on ONE index first (upc is only 56 MB) and
-- watching the 57014 rate for 24h before doing the rest.

-- ALTER INDEX public.pricecharting_catalog_upc_trgm_idx SET (fastupdate = off);


-- ---------------------------------------------------------------------------
-- STEP 5 (housekeeping)
-- ---------------------------------------------------------------------------
-- Last autovacuum was 2026-09-01 with 1,066,914 dead tuples (7.8%). The
-- default scale factor of 0.2 means it will not run again until ~2.5M dead
-- rows on this table, so bloat accumulates between runs on the table that can
-- least afford it.

-- ALTER TABLE public.pricecharting_catalog
--     SET (autovacuum_vacuum_scale_factor = 0.02,
--          autovacuum_vacuum_threshold = 50000);


-- ---------------------------------------------------------------------------
-- Measuring afterwards
-- ---------------------------------------------------------------------------
-- HOT ratio (was 13.6%):
--     SELECT n_tup_upd, n_tup_hot_upd,
--            round(100.0 * n_tup_hot_upd / nullif(n_tup_upd, 0), 1) AS hot_pct
--       FROM pg_stat_user_tables
--      WHERE relname = 'pricecharting_catalog';
--
-- 57014 rate over 24h (was ~90/day):
--     SELECT date_trunc('hour', occurred_at) AS hour, count(*)
--       FROM public.ops_error_events
--      WHERE error_class = 'PostgresError57014'
--        AND occurred_at > now() - interval '24 hours'
--      GROUP BY 1 ORDER BY 1;
