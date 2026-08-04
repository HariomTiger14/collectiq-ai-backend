-- Phase 1a: server-owned, per-user subscription entitlement (source of truth).
--
-- A signed-in user can READ only their own row (RLS). There are NO write
-- policies for the anon/authenticated roles, so a client cannot grant itself
-- Pro. All writes happen from the backend using the service role, which
-- bypasses RLS after it has verified the store receipt (mock-trusted for now).
--
-- Run this once in the Supabase SQL editor.

create table if not exists public.user_subscriptions (
  user_id uuid primary key references auth.users (id) on delete cascade,
  plan text not null default 'free'
    check (plan in ('free', 'pro', 'premium')),
  status text not null default 'active'
    check (status in ('active', 'expired', 'canceled', 'in_grace')),
  source text not null default 'none'
    check (source in ('none', 'mock', 'google_play', 'app_store')),
  current_period_end timestamptz,
  updated_at timestamptz not null default now(),
  created_at timestamptz not null default now()
);

alter table public.user_subscriptions enable row level security;

-- A signed-in user can read only their own entitlement.
drop policy if exists "read own subscription" on public.user_subscriptions;
create policy "read own subscription"
  on public.user_subscriptions
  for select
  to authenticated
  using (auth.uid() = user_id);

-- Intentionally no insert/update/delete policies: only the backend service
-- role (which bypasses RLS) may write, so the plan cannot be spoofed client-side.
