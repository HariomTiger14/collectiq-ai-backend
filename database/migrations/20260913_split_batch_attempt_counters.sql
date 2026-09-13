-- Separate the download and ingest attempt counters.
--
-- NOT YET APPLIED.
--
-- catalog_download_batches.attempts is bumped by two different writers:
--
--   download_catalog_batch.py   PENDING   -> DOWNLOADING
--   claim_ingestable_batch      VALIDATED -> INGESTING
--
-- and claim_ingestable_batch gates on that same column. So --max-attempts 5,
-- documented as an INGEST retry limit, actually allows four ingest claims
-- after a successful download, and fewer if the download was retried.
--
-- Observed live on two clean sports cycles (2026-09-12): both batches ended
-- at attempts=2 having been downloaded once and ingested once, with no
-- failure of any kind.
--
-- The consequence is not cosmetic. A Storage object that is perfectly
-- retryable gets skipped by claim_ingestable_batch once the shared counter
-- reaches the ceiling, and then sits VALIDATED forever, invisible to the
-- ingester -- which is precisely the failure staging exists to prevent.
--
-- Additive. `attempts` is NOT dropped here: the five-CSV mark_ingesting
-- helper, existing tests and any dashboard select keep reading it for one
-- release. It becomes an INGEST ALIAS -- download stops touching it -- and is
-- never the sum of the two, because a sum would put the shared ceiling back
-- on everything still reading it.

ALTER TABLE public.catalog_download_batches
    ADD COLUMN IF NOT EXISTS download_attempts integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS ingest_attempts   integer NOT NULL DEFAULT 0;

-- Backfill: every historical bump is credited to INGEST, and download starts
-- at zero.
--
-- Deliberately not "attempts minus one for the download". That heuristic is
-- wrong for any batch whose download was retried, and it would lower
-- ingest_attempts below the old ceiling -- reopening batches that had already
-- been abandoned, which is the opposite of what this migration is for.
-- Historical accounting stays slightly coarse; the GATE is correct from this
-- deploy forward, and that is the part that matters.
UPDATE public.catalog_download_batches
   SET ingest_attempts = attempts
 WHERE ingest_attempts = 0 AND attempts > 0;

COMMENT ON COLUMN public.catalog_download_batches.download_attempts IS
    'Times this batch entered DOWNLOADING. Gates the downloader own --max-attempts, and is never consulted by the ingester.';
COMMENT ON COLUMN public.catalog_download_batches.ingest_attempts IS
    'Times this batch was claimed for ingest. The only counter claim_ingestable_batch gates on.';
COMMENT ON COLUMN public.catalog_download_batches.attempts IS
    'Ingest alias, kept for one release. Mirrors ingest_attempts. Read the two explicit columns instead. Their SUM is a report, never a gate.';

NOTIFY pgrst, 'reload schema';
