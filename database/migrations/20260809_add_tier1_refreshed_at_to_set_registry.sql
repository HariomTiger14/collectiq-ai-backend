-- Tracks the last time scripts/refresh_small_sets.py checked a set,
-- independent of backfill's own last_fetched_at/last_fetch_status/
-- claimed_at/failure_count columns -- those drive the one-time completeness
-- backfill's claim/lease/retry logic and must never be touched by the tier-1
-- refresh (which only ever operates on already-successful rows and must not
-- affect their claim eligibility). A set that turns out to be too large for
-- the /api/products search cap still gets this column bumped so tier-1
-- doesn't re-check it every run -- only sets that stay small get their
-- catalog rows actually refreshed.
alter table public.pricecharting_set_registry
    add column if not exists tier1_refreshed_at timestamptz;

create index if not exists pricecharting_set_registry_tier1_refresh_idx
    on public.pricecharting_set_registry (tier1_refreshed_at nulls first)
    where last_fetch_status = 'success';
