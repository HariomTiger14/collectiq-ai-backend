-- Admin portal operational tables and columns.
-- Run in Supabase SQL editor before enabling the matching portal features.

create table if not exists public.admin_import_jobs (
    id uuid primary key default gen_random_uuid(),
    source text not null,
    status text not null default 'queued',
    dry_run boolean not null default false,
    requested_by text,
    started_at timestamptz,
    finished_at timestamptz,
    total_rows integer not null default 0,
    inserted_rows integer not null default 0,
    updated_rows integer not null default 0,
    skipped_rows integer not null default 0,
    error_message text,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists admin_import_jobs_created_at_idx
    on public.admin_import_jobs (created_at desc);

create index if not exists admin_import_jobs_status_idx
    on public.admin_import_jobs (status);

alter table if exists public.pricecharting_catalog
    add column if not exists admin_note text;

alter table if exists public.pricecharting_catalog
    add column if not exists active boolean not null default true;

create table if not exists public.admin_notes (
    id uuid primary key default gen_random_uuid(),
    target_type text not null,
    target_id text not null,
    note text not null,
    actor text not null default 'admin',
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists admin_notes_target_idx
    on public.admin_notes (target_type, target_id, created_at desc);

create table if not exists public.admin_audit_events (
    id uuid primary key,
    created_at timestamptz not null default now(),
    actor text not null,
    action text not null,
    status text not null,
    target_type text,
    target_id text,
    metadata jsonb not null default '{}'::jsonb
);

create index if not exists admin_audit_events_created_at_idx
    on public.admin_audit_events (created_at desc);

create index if not exists admin_audit_events_target_idx
    on public.admin_audit_events (target_type, target_id);
