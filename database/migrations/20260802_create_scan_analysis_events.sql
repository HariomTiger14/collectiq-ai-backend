-- Scan analysis event table used by the admin scan-failure queue.
-- Safe to run more than once in Supabase SQL editor.

create table if not exists public.scan_analysis_events (
    id uuid primary key default gen_random_uuid(),
    user_id uuid,
    item_id uuid,
    title text,
    category text,
    status text,
    ai_provider text,
    confidence numeric,
    detection_quality text,
    error_code text,
    error_message text,
    raw_error jsonb,
    image_url text,
    needs_review boolean not null default false,
    review_status text,
    reviewed_at timestamptz,
    retry_requested_at timestamptz,
    resolved_at timestamptz,
    triage_category text,
    resolution_note text,
    data jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists scan_analysis_events_created_at_idx
    on public.scan_analysis_events (created_at desc);

create index if not exists scan_analysis_events_user_id_idx
    on public.scan_analysis_events (user_id);

create index if not exists scan_analysis_events_item_id_idx
    on public.scan_analysis_events (item_id);

create index if not exists scan_analysis_events_needs_review_idx
    on public.scan_analysis_events (needs_review);

create index if not exists scan_analysis_events_review_status_idx
    on public.scan_analysis_events (review_status);

create index if not exists scan_analysis_events_status_idx
    on public.scan_analysis_events (status);
