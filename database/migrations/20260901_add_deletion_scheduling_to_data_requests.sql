-- User-initiated account deletion with a grace period.
--
-- Deletion used to be admin-only: the app filed a request and a human ran it
-- from the console. Apple's guideline 5.1.1(v) requires an account to be
-- deletable from inside the app without a human approving it, so the app now
-- schedules its own deletion and a cron purges it once the grace period ends.
--
-- 'scheduled' is the window during which the user can still cancel by signing
-- back in; 'cancelled' is what a cancellation leaves behind, kept rather than
-- deleted so the request history stays auditable.

alter table public.data_requests
  add column if not exists scheduled_for timestamptz,
  add column if not exists cancelled_at timestamptz;

comment on column public.data_requests.scheduled_for is
  'When a scheduled deletion becomes eligible for purge. Null for exports and for admin-run deletions.';
comment on column public.data_requests.cancelled_at is
  'When the user cancelled a scheduled deletion by signing back in.';

alter table public.data_requests
  drop constraint if exists data_requests_status_check;

alter table public.data_requests
  add constraint data_requests_status_check
  check (status in ('open', 'scheduled', 'processing', 'completed', 'failed', 'cancelled'));

-- The purge cron's only query: scheduled rows whose window has elapsed.
-- Partial index so it stays small no matter how much request history builds up.
create index if not exists data_requests_scheduled_for_idx
  on public.data_requests (scheduled_for)
  where status = 'scheduled';
