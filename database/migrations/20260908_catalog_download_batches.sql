-- Staged catalog refresh: one row per fetch→validate→ingest batch.
--
-- Today a catalog refresh is one process that downloads, parses and writes in
-- a single pass, and a failure anywhere leaves nothing behind but a log line.
-- Measured 2026-09-07: `completed-categories-refresh` reported
-- `success: true` with `catalogRowsFailed: 2463` -- 2,463 rows dropped, with
-- no record of WHICH rows, which sets, or why. This table is the missing
-- memory: a durable record of every batch, what it contained, and exactly
-- where it stopped.
--
-- Render crons do not share a filesystem (14 services, no `disk:` on any of
-- them), so the downloaded CSV lands in the private `catalog-refresh-batches`
-- storage bucket and `storage_key` is the handoff between the two jobs.
--
-- DELIBERATELY SOURCE-AGNOSTIC. v1 carries sportscardspro only, but
-- `completed-categories` (3.7h, 6,707 sets, the biggest unmigrated writer) is
-- meant to become source #2 with no schema change -- hence `source` as a
-- plain text column rather than anything sportscardspro-shaped, and no
-- column here that assumes console_uids are the only way to name a batch.
--
-- Additive only: no existing table or column is touched. The pipeline this
-- serves does not exist yet, so nothing reads these columns until PR 2.

CREATE TABLE IF NOT EXISTS public.catalog_download_batches (
    batch_id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source             text NOT NULL,
    status             text NOT NULL DEFAULT 'pending',

    -- What was asked for. registry_ids is kept separate from console_uids
    -- because stamping is by registry row, and only ever after a clean
    -- ingest -- conflating the two is how sets get marked refreshed from
    -- data that never landed.
    console_uids       text[] NOT NULL DEFAULT '{}',
    registry_ids       uuid[] NOT NULL DEFAULT '{}',
    requested_count    integer NOT NULL DEFAULT 0,

    -- The handoff. Null before download and after cleanup; the object itself
    -- is the evidence for a failed batch, so retention outlives the row's
    -- usefulness (see the cleanup job in PR 5).
    storage_key        text,
    bytes              bigint,
    row_count          integer,
    family_count       integer,

    fetch_started_at   timestamptz,
    fetch_ms           integer,
    ingest_started_at  timestamptz,
    ingest_ms          integer,

    rows_written       integer,
    rows_skipped       integer,
    rows_failed        integer,
    -- Mirrors the #205 run-summary counters at batch granularity. A run
    -- summary is lost entirely if the process dies mid-write; the batch row
    -- survives, which is the whole point of having both.
    statement_timeouts integer NOT NULL DEFAULT 0,
    rows_recovered     integer NOT NULL DEFAULT 0,
    rows_abandoned     integer NOT NULL DEFAULT 0,
    write_path         text,                  -- 'rest' | 'copy' | 'none'

    attempts           integer NOT NULL DEFAULT 0,
    -- Lease fields. A run that dies without updating status would otherwise
    -- hold a batch forever: `ops_cron_runs` has carried a
    -- tier3-sportscardspro-rotation row marked 'running' since 2026-09-03,
    -- 118 hours, which made every "is anything running?" check unreliable.
    -- A lease with a reaper is what stops that recurring here.
    claimed_at         timestamptz,
    claimed_by         text,

    last_error         text,
    last_error_class   text,                  -- transient|validation|write|blocked

    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT catalog_download_batches_status_check CHECK (status IN (
        'pending', 'downloading', 'downloaded', 'validated',
        'ingesting', 'ingested',
        'fetch_failed', 'validation_failed', 'ingest_failed'
    )),
    CONSTRAINT catalog_download_batches_error_class_check CHECK (
        last_error_class IS NULL OR last_error_class IN (
            'transient', 'validation', 'write', 'blocked'
        )
    ),
    CONSTRAINT catalog_download_batches_write_path_check CHECK (
        write_path IS NULL OR write_path IN ('rest', 'copy', 'none')
    )
);

COMMENT ON TABLE public.catalog_download_batches IS
    'One row per staged catalog refresh batch: fetch -> validate -> ingest. '
    'Source-agnostic by design so completed-categories can join as source #2.';
COMMENT ON COLUMN public.catalog_download_batches.storage_key IS
    'Object key in the private catalog-refresh-batches bucket. Null before '
    'download and after retention cleanup.';
COMMENT ON COLUMN public.catalog_download_batches.registry_ids IS
    'Stamped into pricecharting_set_registry.tier3_refreshed_at ONLY after a '
    'clean ingest. Never on partial failure.';
COMMENT ON COLUMN public.catalog_download_batches.claimed_at IS
    'Ingester lease. A batch held past the reaper window returns to its '
    'previous state rather than being stuck forever.';

-- The ingester's queue: oldest validated batch first, nothing else scanned.
CREATE INDEX IF NOT EXISTS catalog_download_batches_queue_idx
    ON public.catalog_download_batches (created_at)
    WHERE status = 'validated';

-- In-flight duplicate guard and the stale-lease reaper.
CREATE INDEX IF NOT EXISTS catalog_download_batches_inflight_idx
    ON public.catalog_download_batches (source, status, claimed_at)
    WHERE status IN ('pending', 'downloading', 'downloaded', 'validated', 'ingesting');

-- Retention sweeper.
CREATE INDEX IF NOT EXISTS catalog_download_batches_cleanup_idx
    ON public.catalog_download_batches (status, updated_at)
    WHERE storage_key IS NOT NULL;

-- Service-role only, same posture as ops_cron_runs / ops_error_events: RLS
-- on with no policies, so anon/authenticated see nothing and the service
-- role bypasses it.
ALTER TABLE public.catalog_download_batches ENABLE ROW LEVEL SECURITY;
