-- Caches real, currently-available eBay listings per (catalog item, eBay
-- marketplace) pair, so the catalog detail screen's "Buy on eBay" section
-- doesn't call eBay's Buy Browse API live on every page view. eBay listings
-- change constantly (sold, delisted, new ones appear), so unlike RAWG's
-- static cover-art catalog this is a real cache with a staleness window,
-- not a one-time import -- see CatalogSearchService's ebay enrichment for
-- the refresh-if-older-than-24h logic.
--
-- Primary key is (catalog_id, marketplace_id), not just catalog_id: the
-- same item has genuinely different listings, prices, and currencies per
-- eBay marketplace (EBAY_US/EBAY_GB/EBAY_AU/EBAY_CA), matching PackLox's
-- own 4 supported display currencies (CollectorProfile.preferredCurrency)
-- -- see app/services/pricing/ebay_listing_service.py for the currency ->
-- marketplace map.
--
-- listings is a small JSON array (top few results: title, price, currency,
-- condition, url) rather than a normalized table -- this is a display
-- cache, not a dataset queried by its own fields.
create table if not exists public.ebay_listing_cache (
    catalog_id text not null references public.pricecharting_catalog(pricecharting_id) on delete cascade,
    marketplace_id text not null,
    listings jsonb not null default '[]'::jsonb,
    fetched_at timestamptz not null default now(),
    primary key (catalog_id, marketplace_id)
);

create index if not exists ebay_listing_cache_fetched_at_idx
    on public.ebay_listing_cache (fetched_at);

alter table public.ebay_listing_cache enable row level security;
-- No policies: service-role only, same pattern as the other catalog reference tables.

-- Per-source on/off switch for marketplace-listing features, mirroring
-- catalog_image_source_flags.sql's exact pattern (fail-open to enabled on
-- any read error -- a flags-table hiccup should never blank out listings
-- that would otherwise show fine). Kept as its own table rather than
-- folded into catalog_image_source_flags: that table's own name and
-- comments are specifically scoped to publisher-sourced card/product
-- IMAGES, and eBay listings are a different kind of feature (live
-- marketplace data, not static catalog art) -- reusing it would blur that
-- table's meaning for anyone reading it later.
create table if not exists public.catalog_marketplace_source_flags (
    source text primary key,
    enabled boolean not null default true,
    updated_at timestamptz not null default now()
);

insert into public.catalog_marketplace_source_flags (source, enabled)
values
    ('ebay', true)
on conflict (source) do nothing;

create or replace function public.touch_catalog_marketplace_source_flags_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists touch_catalog_marketplace_source_flags_updated_at
    on public.catalog_marketplace_source_flags;
create trigger touch_catalog_marketplace_source_flags_updated_at
before update on public.catalog_marketplace_source_flags
for each row execute function public.touch_catalog_marketplace_source_flags_updated_at();

alter table public.catalog_marketplace_source_flags enable row level security;
-- No policies: service-role only, same pattern as catalog_image_source_flags.
