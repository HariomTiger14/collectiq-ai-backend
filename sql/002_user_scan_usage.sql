-- Phase 1b: server-side daily scan counter for /analyze quota enforcement.
--
-- Protects the Gemini/pricing budget from a modified client that bypasses the
-- app's client-side scan cap. Enforcement is fail-open: the backend only blocks
-- when it can positively confirm a signed-in FREE user is over the daily limit.
--
-- Run once in the Supabase SQL editor.

create table if not exists public.user_scan_usage (
  user_id uuid not null references auth.users (id) on delete cascade,
  usage_date date not null default (now() at time zone 'utc')::date,
  scans_used integer not null default 0,
  updated_at timestamptz not null default now(),
  primary key (user_id, usage_date)
);

alter table public.user_scan_usage enable row level security;

-- A user may read only their own usage; the backend service role (and the
-- security-definer RPC below) handle writes.
drop policy if exists "read own scan usage" on public.user_scan_usage;
create policy "read own scan usage"
  on public.user_scan_usage
  for select
  to authenticated
  using (auth.uid() = user_id);

-- Atomic check-and-bump. Increments today's counter only when it is under
-- p_limit, and reports whether this scan is allowed plus the resulting count.
create or replace function public.check_and_bump_scan_usage(
  p_user_id uuid,
  p_limit integer
)
returns table (allowed boolean, used integer)
language plpgsql
security definer
set search_path = public
as $$
declare
  v_today date := (now() at time zone 'utc')::date;
  v_used integer;
begin
  insert into public.user_scan_usage (user_id, usage_date, scans_used)
    values (p_user_id, v_today, 0)
    on conflict (user_id, usage_date) do nothing;

  select scans_used into v_used
    from public.user_scan_usage
    where user_id = p_user_id and usage_date = v_today
    for update;

  if v_used >= p_limit then
    return query select false, v_used;
  else
    update public.user_scan_usage
      set scans_used = scans_used + 1, updated_at = now()
      where user_id = p_user_id and usage_date = v_today;
    return query select true, v_used + 1;
  end if;
end;
$$;
