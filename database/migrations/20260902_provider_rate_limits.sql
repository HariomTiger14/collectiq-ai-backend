-- Account-wide rate limiting for external providers.
--
-- Why a shared table rather than per-script sleeps: a provider's limit applies
-- to the ACCOUNT (the token identifies the subscription, not the caller), but
-- pacing lived inside each script. Three jobs each sleeping the full interval
-- still breach the limit whenever they overlap -- and they do:
-- refresh_completed_pricecharting_categories runs ~3.8h from 04:45 while the
-- tier-3 rotation runs 2.7h out of every 3, so overlap is near-certain.
--
-- Measured 2026-09-02 against the published limits:
--   pricecharting/sportscardspro CSV : 1 call / 10 minutes  = 144/day
--   tier3 128 + categories 23 + backfill ~4                 = 155/day
-- Over budget even before counting concurrency, which doubles the effective
-- rate whenever two jobs are mid-run together.
--
-- acquire_rate_limit_slot() serialises callers on a single row with FOR
-- UPDATE, so two jobs asking at the same moment cannot both be granted.

create table if not exists public.provider_rate_limits (
  limit_key             text primary key,
  min_interval_seconds  numeric not null check (min_interval_seconds > 0),
  last_acquired_at      timestamptz,
  acquired_count        bigint not null default 0,
  note                  text,
  updated_at            timestamptz not null default now()
);

comment on table public.provider_rate_limits is
  'One row per external provider rate limit, shared across every job that '
  'calls it. limit_key is the provider+endpoint class, not the job.';
comment on column public.provider_rate_limits.min_interval_seconds is
  'Minimum seconds between calls, from the provider''s PUBLISHED limit. '
  'Changing this to make a job faster is how the previous block happened.';

insert into public.provider_rate_limits (limit_key, min_interval_seconds, note)
values
  ('pricecharting:csv', 600,
   'Published: "CSV calls are limited to one every 10 minutes" '
   '(sportscardspro.com/api-documentation). Shared by tier-3 rotation, '
   'completed-categories refresh, and the sets backfill.'),
  ('kicksdb:api', 1,
   'Published: 60 requests/minute per API key '
   '(docs.kicks.dev/rate-limiting-1376876m0).')
on conflict (limit_key) do nothing;

-- Returns 0 when the caller may proceed (and records the slot), or the number
-- of seconds it must wait before asking again. Callers sleep and retry rather
-- than being queued here, so a stuck caller holds no lock.
create or replace function public.acquire_rate_limit_slot(
  p_key text,
  p_min_interval_seconds numeric default null
) returns numeric
language plpgsql
security definer
set search_path = public
as $$
declare
  v_interval numeric;
  v_last     timestamptz;
  v_wait     numeric;
begin
  -- Unknown keys are an error, not an implicit "no limit": silently allowing
  -- an unregistered provider through is exactly the failure mode this exists
  -- to prevent.
  select min_interval_seconds, last_acquired_at
    into v_interval, v_last
    from public.provider_rate_limits
   where limit_key = p_key
     for update;

  if not found then
    raise exception 'unknown rate limit key: %', p_key
      using hint = 'Register it in provider_rate_limits with its PUBLISHED limit.';
  end if;

  -- A caller may only ever ask for a SLOWER pace than the registered one.
  v_interval := greatest(v_interval, coalesce(p_min_interval_seconds, 0));

  if v_last is not null then
    v_wait := v_interval - extract(epoch from (now() - v_last));
    if v_wait > 0 then
      return round(v_wait, 3);
    end if;
  end if;

  update public.provider_rate_limits
     set last_acquired_at = now(),
         acquired_count   = acquired_count + 1,
         updated_at       = now()
   where limit_key = p_key;
  return 0;
end;
$$;

revoke all on function public.acquire_rate_limit_slot(text, numeric) from public, anon, authenticated;
grant execute on function public.acquire_rate_limit_slot(text, numeric) to service_role;
grant select on public.provider_rate_limits to service_role;
