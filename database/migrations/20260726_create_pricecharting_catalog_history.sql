create table if not exists public.pricecharting_catalog_history (
    history_id uuid primary key default gen_random_uuid(),
    pricecharting_id text not null,
    product_name text not null,
    console_name text,
    category text,
    upc text,
    asin text,
    epid text,
    release_date date,
    loose_price_cents integer,
    cib_price_cents integer,
    new_price_cents integer,
    graded_price_cents integer,
    box_only_price_cents integer,
    manual_only_price_cents integer,
    currency text not null default 'USD',
    product_url text,
    normalized_identity text not null,
    raw_payload jsonb not null default '{}'::jsonb,
    source_file text,
    source_downloaded_at timestamptz,
    valid_from timestamptz not null,
    valid_to timestamptz,
    is_current boolean not null default true,
    change_hash text not null,
    recorded_at timestamptz not null default now(),
    constraint pricecharting_catalog_history_valid_window_check
        check (valid_to is null or valid_to >= valid_from)
);

alter table public.pricecharting_catalog_history enable row level security;

create unique index if not exists pricecharting_catalog_history_current_unique_idx
    on public.pricecharting_catalog_history (pricecharting_id)
    where is_current;

create index if not exists pricecharting_catalog_history_item_window_idx
    on public.pricecharting_catalog_history (pricecharting_id, valid_from desc);

create index if not exists pricecharting_catalog_history_current_hash_idx
    on public.pricecharting_catalog_history (pricecharting_id, change_hash)
    where is_current;

create index if not exists pricecharting_catalog_history_source_idx
    on public.pricecharting_catalog_history (source_file, valid_from desc);
