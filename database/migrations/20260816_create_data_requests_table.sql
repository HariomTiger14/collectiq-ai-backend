-- GDPR/CCPA-style data requests: a user's own "export my data" / "delete my
-- account" requests, tracked from open through completion. Backs the admin
-- console's Data requests page (previously a static mockup) and two new
-- Settings entries in the mobile app.
--
-- Access model: service-role-only, matching the zero-policy style used for
-- other backend-owned tables (see 20260811_enable_rls_on_catalog_and_admin_
-- tables.sql). The mobile app never writes this table directly via
-- PostgREST -- both request creation (POST /data-requests) and status
-- lookup (GET /data-requests/mine) go through the FastAPI backend using the
-- user's own bearer token, so there is no need for a client-facing RLS
-- policy here.
--
-- Run this once in the Supabase SQL editor.

create table if not exists public.data_requests (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null,
  type text not null check (type in ('export', 'deletion')),
  status text not null default 'open' check (status in ('open', 'processing', 'completed', 'failed')),
  requested_at timestamptz not null default now(),
  completed_at timestamptz,
  export_path text,
  export_expires_at timestamptz,
  notes text,
  processed_by text,
  raw_json jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists data_requests_user_id_idx on public.data_requests (user_id);
create index if not exists data_requests_status_idx on public.data_requests (status);

alter table public.data_requests enable row level security;
