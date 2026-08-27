-- In-house backend observability: run ledger + error capture + freshness.
--
-- The failure mode that has actually hurt this project is SILENT: the
-- KicksDB quota exhausted and sneaker prices sat stale for weeks with no
-- exception thrown anywhere; catalog upserts hit statement timeouts that
-- only surfaced because someone happened to scroll Render logs. Render's
-- logs rotate away and nothing alerts. These three pieces give the admin
-- portal's (previously mocked) Scheduled-jobs page real answers:
--
--   * ops_cron_runs -- one row per cron run, carrying the JSON summary
--     every cron script already prints. Answers "what ran, how long, and
--     what did it do".
--   * ops_error_events -- unhandled exceptions from the API (middleware)
--     and the cron scripts (run recorder), fingerprinted so 500
--     occurrences of one bug group into one issue. Answers "what broke".
--   * admin_pipeline_health() -- per-pipeline data freshness computed
--     from the catalog/registry tables themselves. Answers "is the
--     output still moving", which catches the quota-exhaustion class of
--     failure no error tracker can see (nothing errored -- the API just
--     returned less).

CREATE TABLE IF NOT EXISTS public.ops_cron_runs (
    run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_name text NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    status text NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'succeeded', 'failed')),
    -- The structured summary the script itself reports (rows written,
    -- sets refreshed, 429s seen, breaker state, ...). Free-form on
    -- purpose: each job's summary shape is its own.
    summary jsonb,
    error text,
    -- Where it ran, for debugging region-specific behavior (sportscardspro
    -- 403-blocks Oregon IPs): Render's service name + git commit.
    context jsonb
);

CREATE INDEX IF NOT EXISTS ops_cron_runs_job_started_idx
    ON public.ops_cron_runs (job_name, started_at DESC);
CREATE INDEX IF NOT EXISTS ops_cron_runs_started_idx
    ON public.ops_cron_runs (started_at DESC);

CREATE TABLE IF NOT EXISTS public.ops_error_events (
    event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    occurred_at timestamptz NOT NULL DEFAULT now(),
    source text NOT NULL CHECK (source IN ('api', 'cron')),
    -- Cron job name, or the API route template that raised.
    job_name text,
    error_class text NOT NULL,
    message text,
    -- Tail of the traceback, capped by the writer -- enough to locate the
    -- raise site without storing megabytes per event.
    stack text,
    context jsonb,
    -- md5 over (source, job_name, error_class): the grouping key. Message
    -- is deliberately excluded -- messages embed ids/values that would
    -- shatter one bug into a thousand "distinct" errors.
    fingerprint text NOT NULL
);

CREATE INDEX IF NOT EXISTS ops_error_events_fingerprint_idx
    ON public.ops_error_events (fingerprint, occurred_at DESC);
CREATE INDEX IF NOT EXISTS ops_error_events_occurred_idx
    ON public.ops_error_events (occurred_at DESC);

-- Service-role-only tables, same posture as pcg_backfill_cursor
-- (20260826_enable_rls_on_pcg_backfill_cursor.sql): RLS on, no policies,
-- so anon/authenticated see nothing and the service role bypasses RLS.
ALTER TABLE public.ops_cron_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.ops_error_events ENABLE ROW LEVEL SECURITY;

-- Data-freshness board. Every branch is a narrow indexed probe:
--   * per-source_file latest imported_at rides
--     pricecharting_catalog_source_imported_idx (source_file, imported_at
--     DESC) -- an ORDER BY ... LIMIT 1, never an aggregate scan;
--   * history-rows-in-24h counts ride
--     pricecharting_catalog_history_source_idx (source_file, valid_from
--     DESC);
--   * registry stamps are small-table max()es (~43k rows);
--   * kicksdb freshness scans its ~44k-row table directly.
-- Returned as one jsonb so the portal gets the whole board in a single
-- round trip.
CREATE OR REPLACE FUNCTION public.admin_pipeline_health()
RETURNS jsonb
LANGUAGE sql
STABLE
-- Function-scoped timeout override: the history-count probes walk index
-- ranges over a churn-heavy table whose visibility map is perpetually
-- dirty, so a cold call measured ~9s -- past the 8s statement timeout
-- PostgREST's role carries, which killed the RPC with 57014 while the
-- same call succeeded from psql. The portal fetches this on demand (no
-- background polling), so a rare slow cold call is acceptable; being
-- killed mid-flight is not.
SET statement_timeout = '25s'
AS $$
SELECT jsonb_build_object(
    'generatedAt', now(),
    'csvSources', (
        SELECT jsonb_object_agg(src, jsonb_build_object(
            'latestImportedAt', latest,
            'historyRows24h', hist
        ))
        FROM (
            SELECT s.src,
                (SELECT c.imported_at FROM public.pricecharting_catalog c
                 WHERE c.source_file = s.src
                 ORDER BY c.imported_at DESC LIMIT 1) AS latest,
                -- Capped count: the tier-3 rotation alone writes 200k+
                -- history rows a day, and exact-counting that through a
                -- churn-dirty visibility map measured 8.6s -- which blew
                -- PostgREST's statement timeout. Freshness only needs
                -- "is it moving and roughly how much"; 5000+ says plenty.
                (SELECT count(*) FROM (
                    SELECT 1 FROM public.pricecharting_catalog_history h
                    WHERE h.source_file = s.src
                      AND h.valid_from > now() - interval '24 hours'
                    LIMIT 5000
                 ) capped) AS hist
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
