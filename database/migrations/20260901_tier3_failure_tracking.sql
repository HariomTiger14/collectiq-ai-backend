-- Tier-3 rotation: distinguish "not yet reached" from "cannot succeed".
--
-- Before this, refresh_sportscardspro_rotation.py stamped tier3_refreshed_at
-- only on success, so a set that can NEVER succeed was indistinguishable from
-- one the rotation simply had not reached yet: both are NULL, and the queue
-- orders by tier3_refreshed_at ASC NULLS FIRST. A permanently-failing set
-- therefore parks itself at the head of the queue and is retried first on
-- every single run, forever.
--
-- Measured 2026-09-01 against production: console_uid G37119
-- ("1993 Hoops Fifth Anniversary Gold") sat at queue position 3. Its console
-- page now 404s -- the set was removed upstream after we backfilled it on
-- 2026-08-13 -- and /price-guide/download-custom answers with a 503
-- "upstream connect error or disconnect/reset before headers". It cost every
-- run a full batch plus 90s of retries.
--
-- The damage is amplified by batching: download-custom fails the WHOLE
-- request, so one dead set destroys every healthy set batched with it. At a
-- ~1% dead rate, --batch-size 25 predicts 1 - 0.99^25 ~= 22% of batches
-- failing; runs on 2026-09-01 measured 25%. At the old --batch-size 3 the
-- same rate predicts ~3%, which is the floor under the "13% batch failure"
-- previously attributed entirely to throttling.

alter table public.pricecharting_set_registry
  add column if not exists tier3_failure_count integer not null default 0,
  add column if not exists tier3_last_error text,
  add column if not exists tier3_attempted_at timestamptz;

comment on column public.pricecharting_set_registry.tier3_failure_count is
  'Consecutive tier-3 fetch failures for this set. Reset to 0 on success. '
  'Sets at or above the rotation''s max-failures threshold are excluded from '
  'the queue instead of blocking its head.';
comment on column public.pricecharting_set_registry.tier3_last_error is
  'Short reason for the most recent tier-3 failure (e.g. "HTTP 503"), for '
  'telling a dead upstream set apart from a transient throttle.';
comment on column public.pricecharting_set_registry.tier3_attempted_at is
  'When tier-3 last ATTEMPTED this set, successful or not. tier3_refreshed_at '
  'records only successes, so the two differ exactly on failing sets.';

-- Matches the rotation's ORDER BY so the queue read stays an index scan
-- rather than a sort over ~17.7k filtered rows.
create index concurrently if not exists pricecharting_set_registry_tier3_queue_idx
  on public.pricecharting_set_registry (
    tier3_failure_count,
    tier3_refreshed_at nulls first,
    registry_id
  )
  where source_site = 'sportscardspro'
    and last_fetch_status = 'success'
    and console_uid is not null;

-- Atomic increment. A read-modify-write over PostgREST would lose failures
-- when two runs overlap, which is exactly when failures cluster.
create or replace function public.tier3_record_failure(
  p_registry_ids uuid[],
  p_error text
) returns void
language sql
security definer
set search_path = public
as $$
  update public.pricecharting_set_registry
     set tier3_failure_count = tier3_failure_count + 1,
         tier3_last_error    = left(p_error, 500),
         tier3_attempted_at  = now()
   where registry_id = any(p_registry_ids);
$$;

revoke all on function public.tier3_record_failure(uuid[], text) from public, anon, authenticated;
grant execute on function public.tier3_record_failure(uuid[], text) to service_role;

-- One place to answer "what is done, what is pending, what is stuck".
create or replace view public.tier3_rotation_status as
select
  case
    when tier3_failure_count >= 3          then 'poisoned'
    when tier3_failure_count > 0           then 'failing'
    when tier3_refreshed_at is not null    then 'done'
    else 'pending'
  end                                      as state,
  count(*)                                 as sets,
  min(tier3_refreshed_at)                  as oldest_refresh,
  max(tier3_refreshed_at)                  as newest_refresh
from public.pricecharting_set_registry
where source_site = 'sportscardspro'
  and last_fetch_status = 'success'
  and console_uid is not null
group by 1;

comment on view public.tier3_rotation_status is
  'Tier-3 rotation progress. "poisoned" sets are excluded from the queue by '
  'the rotation script; inspect them via tier3_last_error before deleting.';

grant select on public.tier3_rotation_status to service_role;
