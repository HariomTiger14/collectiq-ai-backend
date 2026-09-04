-- Priority arbitration for the shared provider slot.
--
-- 20260902 made pacing account-wide, which stopped the breach but created a
-- second problem: acquire_rate_limit_slot() is a race, not a queue. Whoever
-- polls first after the interval expires wins. Two jobs that must finish
-- daily compete against ~120 bulk runs, so they win about 2% of races. A
-- 24h simulation against the real schedules (fair race, 12 seeds) had the
-- 23-call categories refresh completing 4.5 of its calls per day -- a daily
-- job that would take five days -- once the tier-3 rotation was resumed.
--
-- The fix is to let the transaction decide WHICH CLASS may take the next
-- slot, rather than letting wake-up order decide.
--
-- Deliberately NOT a queue. These are ephemeral Render cron containers that
-- can be killed mid-wait. Tickets or leases would need expiry and reaping;
-- the only state here is per-class aggregate counters plus a self-expiring
-- "I asked recently" timestamp. Nothing is owned, so nothing leaks.
--
-- An essential job announces itself simply by ASKING. While it is working it
-- asks about once per interval, which keeps its activity fresh and holds
-- bulk off. When it finishes -- or dies -- the timestamp goes stale within
-- ESSENTIAL_ACTIVE_WINDOW and bulk resumes on its own.

alter table public.provider_rate_limits
  add column if not exists note_policy text;

create table if not exists public.provider_slot_classes (
  limit_key          text not null references public.provider_rate_limits(limit_key) on delete cascade,
  class              text not null,
  kind               text not null check (kind in ('essential', 'bulk')),
  daily_entitlement  integer check (daily_entitlement is null or daily_entitlement >= 0),
  used_today         integer not null default 0,
  granted_count      bigint  not null default 0,
  quota_date         date,
  last_request_at    timestamptz,
  note               text,
  primary key (limit_key, class)
);

comment on table public.provider_slot_classes is
  'Allocation policy for one provider limit. The 600s+ gate in '
  'provider_rate_limits is the SAFETY rule and is always authoritative; '
  'these rows only decide which class may take the next slot.';

comment on column public.provider_slot_classes.daily_entitlement is
  'Meaning depends on kind. essential: a SOFT priority reservation -- the '
  'class gets priority for this many calls per day, and is still allowed '
  'beyond it, just without priority. Denying an essential job its last call '
  'because a set count grew would break the daily refresh it exists to '
  'guarantee. bulk: a HARD daily cap; null means uncapped (the residual '
  'consumer).';

comment on column public.provider_slot_classes.last_request_at is
  'Updated on EVERY ask, including refused ones. This is the liveness '
  'signal, deliberately separate from actually getting a slot: an essential '
  'job that is waiting out the interval is still alive and must keep bulk '
  'blocked, or bulk would take the slot it is waiting for.';

insert into public.provider_slot_classes (limit_key, class, kind, daily_entitlement, note) values
  ('pricecharting:csv', 'essential_categories', 'essential', 23,
   'refresh_completed_pricecharting_categories, daily 04:45 UTC. 6,705 sets / batch 300 = 23 calls.'),
  ('pricecharting:csv', 'essential_catalog', 'essential', 5,
   'refresh_pricecharting_catalog, daily 14:30 UTC. Five category CSVs.'),
  ('pricecharting:csv', 'backfill', 'bulk', 30,
   'backfill_pricecharting_sets, every 15 min. HARD cap: its CSV path is a '
   'fallback -- /api/products handles anything under the 100-result cap -- so '
   'a denied CSV slot costs the run nothing. Uncapped, its 96 runs/day won '
   '~64 of the ~106 leftover slots and starved tier-3.'),
  ('pricecharting:csv', 'tier3', 'bulk', null,
   'refresh_sportscardspro_rotation. Residual consumer: takes whatever the '
   'others leave. 177 calls per full cycle over 17,691 sets.')
on conflict (limit_key, class) do nothing;

-- The provider's wording is "one call every 10 minutes". 610 rather than 600
-- buys ten seconds against the gap between our grant timestamp and when
-- their CDN actually sees the request. 141 calls/day instead of 144; the
-- tier-3 full cycle moves from 2.06 to 2.10 days. Cheap insurance given they
-- have already blocked us once.
update public.provider_rate_limits
   set min_interval_seconds = 610,
       note_policy = 'Classes in provider_slot_classes decide allocation; this interval is the safety gate.'
 where limit_key = 'pricecharting:csv';


create or replace function public.acquire_provider_slot(
  p_key   text,
  p_class text default null
) returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  -- How long after its last ask an essential class still counts as alive.
  --
  -- The invariant this must satisfy:
  --
  --     active_window > global_interval + worst-case wake-up jitter
  --
  -- An essential caller that is refused sleeps until the next slot and asks
  -- again about one interval later. If the window were shorter than that it
  -- would go stale between its own asks and lose the slot it was waiting
  -- for -- the exact failure the liveness signal exists to prevent.
  --
  -- 900 = 610s interval + 290s allowance for scheduler, network and
  -- container-runtime jitter. Two consequences are deliberate: an essential
  -- worker stalled for more than ~290s beyond its expected wake-up loses
  -- priority (at that point it is not healthy), and one doing non-CSV work
  -- for over 900s between legitimate CSV calls also lapses (it is not
  -- waiting for CSV capacity during that time, so bulk should have it).
  c_active_window  constant interval := interval '900 seconds';

  v_interval   numeric;
  v_last       timestamptz;
  v_today      date;
  v_kind       text;
  v_entitle    integer;
  v_used       integer;
  v_wait       numeric;
  v_essential_active boolean;
begin
  select min_interval_seconds, last_acquired_at
    into v_interval, v_last
    from public.provider_rate_limits
   where limit_key = p_key
     for update;

  if not found then
    raise exception 'unknown rate limit key: %', p_key
      using hint = 'Register it in provider_rate_limits with its PUBLISHED limit.';
  end if;

  v_today := (now() at time zone 'utc')::date;

  -- Roll every class for this key, not just the caller's: the policy below
  -- reads other classes' used_today, and a stale counter would wrongly
  -- report an essential job as already satisfied.
  update public.provider_slot_classes
     set used_today = 0, quota_date = v_today
   where limit_key = p_key
     and (quota_date is distinct from v_today);

  if p_class is null then
    -- Keys with no allocation policy (kicksdb) get the safety gate only.
    if v_last is not null then
      v_wait := v_interval - extract(epoch from (now() - v_last));
      if v_wait > 0 then
        return jsonb_build_object('granted', false, 'reason', 'RATE_LIMITED',
                                  'retry_after_seconds', round(v_wait, 3));
      end if;
    end if;
    update public.provider_rate_limits
       set last_acquired_at = now(), acquired_count = acquired_count + 1, updated_at = now()
     where limit_key = p_key;
    return jsonb_build_object('granted', true, 'reason', 'GRANTED', 'retry_after_seconds', 0);
  end if;

  select kind, daily_entitlement, used_today
    into v_kind, v_entitle, v_used
    from public.provider_slot_classes
   where limit_key = p_key and class = p_class;

  if not found then
    raise exception 'unknown slot class %.%', p_key, p_class
      using hint = 'Register it in provider_slot_classes.';
  end if;

  -- Liveness first, and unconditionally: this must be recorded even when the
  -- ask is about to be refused. See the column comment.
  update public.provider_slot_classes
     set last_request_at = now()
   where limit_key = p_key and class = p_class;

  -- Safety gate. Always authoritative, checked before any policy.
  if v_last is not null then
    v_wait := v_interval - extract(epoch from (now() - v_last));
    if v_wait > 0 then
      return jsonb_build_object('granted', false, 'reason', 'RATE_LIMITED',
                                'retry_after_seconds', round(v_wait, 3));
    end if;
  end if;

  if v_kind = 'bulk' then
    if v_entitle is not null and v_used >= v_entitle then
      -- Out of budget for the day. The caller should skip its CSV work, not
      -- sleep until midnight, so this is reported distinctly from a wait.
      return jsonb_build_object(
        'granted', false, 'reason', 'QUOTA_EXHAUSTED',
        'retry_after_seconds',
        round(extract(epoch from ((v_today + 1)::timestamp - (now() at time zone 'utc'))), 3));
    end if;

    select exists (
      select 1 from public.provider_slot_classes
       where limit_key = p_key
         and kind = 'essential'
         and last_request_at is not null
         and last_request_at > now() - c_active_window
         and (daily_entitlement is null or used_today < daily_entitlement)
    ) into v_essential_active;

    if v_essential_active then
      -- Come back after the next slot has been taken. Bounded by the
      -- activity window so a crashed essential job cannot park bulk forever.
      return jsonb_build_object(
        'granted', false, 'reason', 'POLICY_BLOCKED',
        'retry_after_seconds', least(900, greatest(60, coalesce(round(v_wait, 3), 0))));
    end if;
  end if;

  update public.provider_rate_limits
     set last_acquired_at = now(), acquired_count = acquired_count + 1, updated_at = now()
   where limit_key = p_key;

  update public.provider_slot_classes
     set used_today = used_today + 1,
         granted_count = granted_count + 1,
         quota_date = v_today
   where limit_key = p_key and class = p_class;

  return jsonb_build_object('granted', true, 'reason', 'GRANTED', 'retry_after_seconds', 0);
end;
$$;

revoke all on function public.acquire_provider_slot(text, text) from public, anon, authenticated;
grant execute on function public.acquire_provider_slot(text, text) to service_role;
grant select on public.provider_slot_classes to service_role;
