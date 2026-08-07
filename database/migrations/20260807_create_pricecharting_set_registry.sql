create table if not exists public.pricecharting_set_registry (
    registry_id uuid primary key default gen_random_uuid(),
    source_site text not null,
    category text not null,
    brand text not null,
    slug text not null,
    set_name text not null,
    url text not null,
    priority_tier integer not null default 3,
    last_fetched_at timestamptz,
    last_fetch_status text,
    content_hash text,
    failure_count integer not null default 0,
    claimed_at timestamptz,
    claimed_by text,
    discovered_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint pricecharting_set_registry_source_site_check
        check (source_site in ('pricecharting', 'sportscardspro')),
    constraint pricecharting_set_registry_priority_tier_check
        check (priority_tier between 1 and 5)
);

alter table public.pricecharting_set_registry enable row level security;

create unique index if not exists pricecharting_set_registry_source_slug_unique_idx
    on public.pricecharting_set_registry (source_site, slug);

create index if not exists pricecharting_set_registry_backfill_queue_idx
    on public.pricecharting_set_registry (source_site, category, priority_tier, last_fetched_at nulls first);

create index if not exists pricecharting_set_registry_claimed_idx
    on public.pricecharting_set_registry (claimed_at)
    where claimed_at is not null;
