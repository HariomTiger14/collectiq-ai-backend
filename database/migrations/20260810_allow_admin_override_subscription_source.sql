-- The admin subscription-override endpoint (AdminUserService.override_subscription)
-- writes source='admin_override' to public.user_subscriptions, but the table's
-- original check constraint (sql/001_user_subscriptions.sql) only allowed
-- ('none', 'mock', 'google_play', 'app_store'). Every admin override write has
-- been failing with a check-constraint violation. Widen the constraint.
--
-- Run this once in the Supabase SQL editor.

alter table public.user_subscriptions
  drop constraint if exists user_subscriptions_source_check;

alter table public.user_subscriptions
  add constraint user_subscriptions_source_check
  check (source in ('none', 'mock', 'google_play', 'app_store', 'admin_override'));
