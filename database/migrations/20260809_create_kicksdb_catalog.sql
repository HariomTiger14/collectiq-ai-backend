create table if not exists public.kicksdb_catalog (
    kicksdb_id text primary key,
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
    content_hash text,
    imported_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists kicksdb_catalog_title_idx
    on public.kicksdb_catalog (lower(title));

create index if not exists kicksdb_catalog_brand_idx
    on public.kicksdb_catalog (lower(brand));

create index if not exists kicksdb_catalog_sku_idx
    on public.kicksdb_catalog (sku)
    where sku is not null;

create index if not exists kicksdb_catalog_rank_idx
    on public.kicksdb_catalog (rank)
    where rank is not null;

create index if not exists kicksdb_catalog_search_idx
    on public.kicksdb_catalog using gin (
        to_tsvector(
            'simple',
            coalesce(title, '') || ' ' ||
            coalesce(brand, '') || ' ' ||
            coalesce(model, '') || ' ' ||
            coalesce(sku, '')
        )
    );

create or replace function public.touch_kicksdb_catalog_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists touch_kicksdb_catalog_updated_at on public.kicksdb_catalog;
create trigger touch_kicksdb_catalog_updated_at
before update on public.kicksdb_catalog
for each row execute function public.touch_kicksdb_catalog_updated_at();
