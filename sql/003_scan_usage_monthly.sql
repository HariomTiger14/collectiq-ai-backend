-- Phase 1b tweak: enforce the free scan cap **per calendar month** instead of
-- per day. Daily rows are still recorded (useful granularity), but the limit is
-- checked against the current month's total, giving a hard, predictable monthly
-- cost ceiling that scales linearly with users.
--
-- Run once in the Supabase SQL editor (safe to re-run; it only replaces the
-- function — the user_scan_usage table from 002 is unchanged).

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
  v_month_start date := date_trunc('month', (now() at time zone 'utc'))::date;
  v_used integer;
begin
  -- Ensure today's row exists and lock it so concurrent scans from the same
  -- user serialize (avoids double-spending the cap under races).
  insert into public.user_scan_usage (user_id, usage_date, scans_used)
    values (p_user_id, v_today, 0)
    on conflict (user_id, usage_date) do nothing;

  perform 1
    from public.user_scan_usage
    where user_id = p_user_id and usage_date = v_today
    for update;

  -- Total scans so far this calendar month.
  select coalesce(sum(scans_used), 0) into v_used
    from public.user_scan_usage
    where user_id = p_user_id and usage_date >= v_month_start;

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
