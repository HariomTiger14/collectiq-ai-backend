-- Static reference table for Funko Pop product images, sourced from the
-- open-source, MIT-licensed community dataset at
-- github.com/kennymkchan/funko-pop-data (23,940 entries at import time,
-- confirmed 100% image coverage). This table has NO pricing data — it
-- exists purely to enrich PriceCharting-sourced Funko Pop catalog rows
-- (which have real pricing but no images) with a real product photo.
--
-- `handle` and `title` are NOT unique keys in the source data: the same
-- character/figure (e.g. "Black Panther") can appear multiple times
-- across different product types (vinyl figure, pin, apparel, sticker),
-- each with its own image. `row_id` is a synthetic surrogate key; matching
-- against pricecharting_catalog happens on normalized_title with a
-- preference for actual Pop! vinyl figures over pins/apparel/accessories
-- when a title has multiple candidates (see catalog_search_service.py).

create table if not exists public.funko_pop_catalog (
    row_id uuid primary key default gen_random_uuid(),
    handle text not null,
    title text not null,
    normalized_title text not null,
    image_url text not null,
    series jsonb not null default '[]'::jsonb,
    content_hash text not null,
    imported_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists funko_pop_catalog_normalized_title_idx
    on public.funko_pop_catalog (normalized_title);

create index if not exists funko_pop_catalog_handle_idx
    on public.funko_pop_catalog (handle);

-- Dedup safety net for repeat imports: same handle+image should not
-- duplicate on re-run.
create unique index if not exists funko_pop_catalog_content_hash_idx
    on public.funko_pop_catalog (content_hash);

create or replace function public.touch_funko_pop_catalog_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists touch_funko_pop_catalog_updated_at on public.funko_pop_catalog;
create trigger touch_funko_pop_catalog_updated_at
before update on public.funko_pop_catalog
for each row execute function public.touch_funko_pop_catalog_updated_at();

alter table public.funko_pop_catalog enable row level security;
-- No policies: service-role only, same pattern as kicksdb_catalog.
