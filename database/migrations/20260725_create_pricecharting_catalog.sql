create table if not exists public.pricecharting_catalog (
    pricecharting_id text primary key,
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
    imported_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists pricecharting_catalog_product_name_idx
    on public.pricecharting_catalog (lower(product_name));

create index if not exists pricecharting_catalog_console_name_idx
    on public.pricecharting_catalog (lower(console_name));

create index if not exists pricecharting_catalog_normalized_identity_idx
    on public.pricecharting_catalog (normalized_identity);

create index if not exists pricecharting_catalog_upc_idx
    on public.pricecharting_catalog (upc)
    where upc is not null;

create index if not exists pricecharting_catalog_search_idx
    on public.pricecharting_catalog using gin (
        to_tsvector(
            'simple',
            coalesce(product_name, '') || ' ' ||
            coalesce(console_name, '') || ' ' ||
            coalesce(category, '') || ' ' ||
            coalesce(upc, '')
        )
    );

create or replace function public.touch_pricecharting_catalog_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists touch_pricecharting_catalog_updated_at on public.pricecharting_catalog;
create trigger touch_pricecharting_catalog_updated_at
before update on public.pricecharting_catalog
for each row execute function public.touch_pricecharting_catalog_updated_at();
