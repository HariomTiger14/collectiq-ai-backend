create table if not exists public.kicksdb_catalog_history (
    history_id uuid primary key default gen_random_uuid(),
    kicksdb_id text not null,
    sku text,
    slug text,
    title text not null,
    brand text,
    model text,
    gender text,
    product_type text,
    category text,
    secondary_category text,
    image_url text,
    rank integer,
    weekly_orders integer,
    release_date date,
    currency text not null default 'USD',
    min_price_cents integer,
    max_price_cents integer,
    avg_price_cents integer,
    variants jsonb not null default '[]'::jsonb,
    product_url text,
    raw_payload jsonb not null default '{}'::jsonb,
    source_query text,
    source_downloaded_at timestamptz,
    valid_from timestamptz not null,
    valid_to timestamptz,
    is_current boolean not null default true,
    change_hash text not null,
    recorded_at timestamptz not null default now(),
    constraint kicksdb_catalog_history_valid_window_check
        check (valid_to is null or valid_to >= valid_from)
);

alter table public.kicksdb_catalog_history enable row level security;

create unique index if not exists kicksdb_catalog_history_current_unique_idx
    on public.kicksdb_catalog_history (kicksdb_id)
    where is_current;

create index if not exists kicksdb_catalog_history_item_window_idx
    on public.kicksdb_catalog_history (kicksdb_id, valid_from desc);

create index if not exists kicksdb_catalog_history_current_hash_idx
    on public.kicksdb_catalog_history (kicksdb_id, change_hash)
    where is_current;

create index if not exists kicksdb_catalog_history_source_idx
    on public.kicksdb_catalog_history (source_query, valid_from desc);
