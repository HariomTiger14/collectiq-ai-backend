-- A single place to answer "what refreshes this, and when did it last run?"
--
-- Motivated by a real false alarm on 2026-09-02. tier3_rotation_status is
-- scoped to tier-3 (source_site='sportscardspro'), which is correct for what
-- it reports, but it is the only refresh-coverage view that exists. Reading
-- the registry directly instead, tier3_refreshed_at is NULL for all 6,705
-- pricecharting.com sets -- which looks exactly like "never refreshed" and was
-- read that way, prompting a proposal to build a rotation job that already
-- existed.
--
-- Those sets are in fact re-downloaded in full every day by
-- refresh_completed_pricecharting_categories.py. It uses a read-only registry
-- client and deliberately stamps nothing: it processes a fixed, known
-- population unconditionally, so it needs no per-set bookkeeping. Correct for
-- the job, invisible to anything looking for a per-set timestamp.
--
-- Rather than add stamping the job does not need, this maps each population to
-- the job responsible for it and reports that job's real last run from
-- ops_cron_runs. The job mapping is static knowledge that previously lived only
-- in render.yaml comments and script docstrings.

create or replace view public.catalog_refresh_coverage as
with population as (
  select
    source_site,
    category,
    -- The split that decides which job can serve a set: anything over the
    -- /api/products 100-result cap needed the console_uid + CSV path at
    -- backfill, which is exactly when console_uid gets resolved.
    case
      when source_site = 'sportscardspro' and console_uid is not null
        then 'tier3-sportscardspro-rotation'
      when source_site = 'sportscardspro'
        then 'small-sets-refresh'
      when source_site = 'pricecharting'
           and category in ('coins','comic-books','funko-pops','lego-sets','lorcana-cards')
        then 'completed-categories-refresh'
      else 'small-sets-refresh'
    end as refreshed_by,
    tier3_refreshed_at
  from public.pricecharting_set_registry
  where last_fetch_status = 'success'
),
job_last_run as (
  select job_name, max(started_at) as last_success
    from public.ops_cron_runs
   where status = 'succeeded'
   group by job_name
)
select
  p.source_site,
  p.category,
  p.refreshed_by,
  count(*)                                                as sets,
  j.last_success                                          as job_last_succeeded,
  -- Only meaningful where the covering job stamps per set (tier-3 today).
  -- NULL elsewhere means "this job does not track per-set", NOT "stale" --
  -- which is precisely the misreading this view exists to prevent.
  max(p.tier3_refreshed_at)                               as newest_per_set_stamp,
  count(p.tier3_refreshed_at)                             as sets_with_per_set_stamp
from population p
left join job_last_run j on j.job_name = p.refreshed_by
group by p.source_site, p.category, p.refreshed_by, j.last_success
order by p.source_site, count(*) desc;

comment on view public.catalog_refresh_coverage is
  'Which cron refreshes each registry population, and when that cron last '
  'succeeded. newest_per_set_stamp is NULL for jobs that do not stamp per '
  'set (they process a fixed population unconditionally) -- read '
  'job_last_succeeded for those, not the NULL.';

comment on view public.tier3_rotation_status is
  'Tier-3 rotation progress -- sportscardspro sets with a console_uid ONLY. '
  'For coverage across every population and job, use catalog_refresh_coverage: '
  'a NULL tier3_refreshed_at outside this view''s scope means "not refreshed '
  'by tier-3", not "not refreshed".';

grant select on public.catalog_refresh_coverage to service_role;
