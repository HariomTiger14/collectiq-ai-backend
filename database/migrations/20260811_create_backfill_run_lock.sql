create table if not exists public.backfill_run_lock (
    lock_name text primary key,
    locked_by text,
    locked_at timestamptz,
    expires_at timestamptz
);

alter table public.backfill_run_lock enable row level security;

-- Same acquire-throttle pattern as pricing_provider_throttle (see
-- 20260728_create_pricing_provider_throttle.sql): a pg_advisory_xact_lock
-- makes the read-check-write atomic even though PostgREST never holds a
-- persistent connection across HTTP calls, and a real row carries the
-- lock's state between calls. Used to stop two backfill_pricecharting_sets.py
-- runs from writing to pricecharting_catalog/pricecharting_catalog_history
-- at the same time -- Render cron jobs can genuinely overlap (a manually
-- triggered run starting while the previous scheduled run is still
-- executing is not prevented by Render itself), and two runs both hammering
-- the same tables' upsert/SCD2 paths concurrently was a suspected
-- contributor to a live-observed ~30 rows/sec catalog-write slowdown.
--
-- lease_seconds_arg is a dead-man's-switch, not the primary release
-- mechanism -- the caller releases explicitly via release_backfill_run_lock
-- in a finally block on every normal exit (including on error), so the
-- lease only matters if the process is killed outright (e.g. an OOM kill)
-- before it can run that finally block.
create or replace function public.acquire_backfill_run_lock(
    lock_name_arg text,
    worker_id_arg text,
    lease_seconds_arg integer
)
returns table(acquired boolean, locked_by text, expires_at timestamptz)
language plpgsql
security definer
set search_path = public
as $$
declare
    now_value timestamptz;
    row_locked_by text;
    row_expires_at timestamptz;
    new_expires_at timestamptz;
begin
    if lock_name_arg is null or btrim(lock_name_arg) = '' then
        return query select false, null::text, null::timestamptz;
        return;
    end if;

    perform pg_advisory_xact_lock(hashtext('backfill_run_lock:' || lock_name_arg));

    insert into public.backfill_run_lock (lock_name, locked_by, locked_at, expires_at)
    values (lock_name_arg, null, null, null)
    on conflict (lock_name) do nothing;

    now_value := clock_timestamp();

    select b.locked_by, b.expires_at
    into row_locked_by, row_expires_at
    from public.backfill_run_lock b
    where b.lock_name = lock_name_arg
    for update;

    if row_expires_at is not null and row_expires_at > now_value then
        return query select false, row_locked_by, row_expires_at;
        return;
    end if;

    new_expires_at := now_value + make_interval(secs => greatest(coalesce(lease_seconds_arg, 1), 1));

    update public.backfill_run_lock
    set locked_by = worker_id_arg,
        locked_at = now_value,
        expires_at = new_expires_at
    where lock_name = lock_name_arg;

    return query select true, worker_id_arg, new_expires_at;
end;
$$;

-- Only releases if worker_id_arg still matches the current holder -- a run
-- whose lease already expired and was reclaimed by a newer run must not be
-- able to release the NEW run's lock out from under it once it finally
-- reaches its own cleanup step.
create or replace function public.release_backfill_run_lock(
    lock_name_arg text,
    worker_id_arg text
)
returns void
language sql
security definer
set search_path = public
as $$
    update public.backfill_run_lock
    set locked_by = null,
        locked_at = null,
        expires_at = null
    where lock_name = lock_name_arg
      and locked_by = worker_id_arg;
$$;
