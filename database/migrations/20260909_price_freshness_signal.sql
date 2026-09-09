-- Price-freshness signal for the ops board.
--
-- NOT YET APPLIED. Review before running.
--
-- Why this is needed
-- ------------------
-- admin_pipeline_health() decides whether a CSV source is ACTIVE by counting
-- rows written to pricecharting_catalog_history in the last 24 hours. The
-- comment on that probe says as much: "yugioh.csv with a 4-day-old
-- imported_at while writing 5k+ history rows a day".
--
-- Those 5k+ rows a day were price-only SCD2 versions. As of 2026-09-09 a
-- price-only change writes a 188-byte snapshot to pricecharting_price_history
-- and no SCD2 version at all, so historyRows24h now trends to zero for a
-- pipeline that is running perfectly. _freshness_for() turns that into
-- inactiveSources, and one "dead" source among five flags the whole job.
--
-- The board would report a stall that is not happening. This repoints the
-- probe at the table price refreshes actually write.
--
-- The index
-- ---------
-- The capped LIMIT 5000 count is only cheap with an index. For an ACTIVE
-- source Postgres stops after 5,000 matching rows either way, but for an
-- INACTIVE one -- exactly the case the board exists to detect -- there is
-- nothing to stop on, and without an index that is a sequential scan of a
-- 16.7M-row, 2.9 GB table on every call.
--
-- Cost: one more btree on a table the ingest pipeline appends to daily.
-- It is a cheap one -- source_file is low-cardinality and observed_at is
-- append-ordered, so inserts land at the right edge of the index rather
-- than scattering through it.

CREATE INDEX CONCURRENTLY IF NOT EXISTS
    pricecharting_price_history_source_observed_idx
    ON public.pricecharting_price_history (source_file, observed_at DESC);

-- Then repoint the probe. Everything else in the function is unchanged; the
-- key stays 'historyRows24h' so no caller has to change at the same time as
-- the meaning is corrected -- callers can migrate to 'priceRows24h' after.
--
-- NOTE: run the CREATE INDEX CONCURRENTLY above on its own first -- it
-- cannot run inside a transaction block.

CREATE OR REPLACE FUNCTION public.admin_pipeline_health()
RETURNS jsonb
LANGUAGE sql
STABLE
SET statement_timeout = '25s'
AS $$
SELECT jsonb_build_object(
    'generatedAt', now(),
    'csvSources', (
        SELECT jsonb_object_agg(src, jsonb_build_object(
            'latestImportedAt', latest,
            -- Same key, corrected source. Price refreshes write snapshots,
            -- not SCD2 versions, since 2026-09-09.
            'historyRows24h', price_rows,
            'priceRows24h', price_rows,
            'metadataRows24h', meta_rows
        ))
        FROM (
            SELECT s.src,
                (SELECT c.imported_at FROM public.pricecharting_catalog c
                 WHERE c.source_file = s.src
                 ORDER BY c.imported_at DESC LIMIT 1) AS latest,
                (SELECT count(*) FROM (
                    SELECT 1 FROM public.pricecharting_price_history h
                    WHERE h.source_file = s.src
                      AND h.observed_at > now() - interval '24 hours'
                    LIMIT 5000
                 ) capped) AS price_rows,
                -- Kept as a separate number rather than dropped: metadata
                -- versions still happen, and a source whose metadata never
                -- moves again is worth being able to see.
                (SELECT count(*) FROM (
                    SELECT 1 FROM public.pricecharting_catalog_history h
                    WHERE h.source_file = s.src
                      AND h.valid_from > now() - interval '24 hours'
                    LIMIT 5000
                 ) capped) AS meta_rows
            FROM unnest(ARRAY[
                'video_games.csv','pokemon.csv','magic.csv','yugioh.csv','one_piece.csv',
                'pricecharting-completed-category-refresh',
                'sportscardspro-tier3-refresh',
                'sportscardspro-tier1-refresh',
                'pricecharting-tier1-refresh'
            ]) AS s(src)
        ) per_source
    ),
    'tier3', (
        SELECT jsonb_build_object(
            'latestStampAt', max(tier3_refreshed_at),
            'stampedLastHour', count(*) FILTER (WHERE tier3_refreshed_at > now() - interval '1 hour'),
            'stampedTotal', count(*) FILTER (WHERE tier3_refreshed_at IS NOT NULL),
            'rotationSize', count(*)
        )
        FROM public.pricecharting_set_registry
        WHERE source_site = 'sportscardspro' AND last_fetch_status = 'success'
    ),
    'tier1', (
        SELECT jsonb_build_object('latestCheckAt', max(tier1_refreshed_at))
        FROM public.pricecharting_set_registry
    ),
    'backfillQueue', (
        SELECT jsonb_build_object(
            'neverFetched', count(*) FILTER (WHERE last_fetch_status IS NULL),
            'failed', count(*) FILTER (WHERE last_fetch_status = 'failed')
        )
        FROM public.pricecharting_set_registry
    ),
    'kicksdb', (
        SELECT jsonb_build_object(
            'latestUpdatedAt', max(updated_at),
            'rowsTouched24h', count(*) FILTER (WHERE updated_at > now() - interval '24 hours'),
            'totalRows', count(*)
        )
        FROM public.kicksdb_catalog
    ),
    'fxRates', (
        SELECT jsonb_build_object('latestRateDate', max(rate_date), 'latestFetchedAt', max(fetched_at))
        FROM public.fx_rates_daily
    )
);
$$;
