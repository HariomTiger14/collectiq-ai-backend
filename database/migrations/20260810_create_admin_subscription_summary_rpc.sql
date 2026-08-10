-- Admin visibility into subscription plan distribution -- previously not
-- surfaced anywhere (admin_reports_service.py only counts users/pricing
-- queue/scan failures/audit events, not plan/status/source breakdown).
--
-- Run this once in the Supabase SQL editor.

create or replace function public.admin_subscription_summary()
returns table (
  plan text,
  status text,
  source text,
  total bigint
)
language sql
security definer
set search_path = public
as $$
  select plan, status, source, count(*) as total
  from public.user_subscriptions
  group by plan, status, source
  order by plan, status, source;
$$;
