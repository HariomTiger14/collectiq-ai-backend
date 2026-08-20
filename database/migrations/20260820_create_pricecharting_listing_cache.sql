-- Caches real, currently-available PriceCharting Marketplace listings per
-- catalog item, mirroring ebay_listing_cache.sql's pattern for the same
-- reason (listings change constantly, so this is a real staleness-windowed
-- cache, not a one-time import).
--
-- Simpler primary key than ebay_listing_cache (catalog_id alone, not
-- catalog_id + marketplace_id): PriceCharting's Marketplace API is a
-- single, US-based marketplace -- there's no per-region marketplace
-- selection the way eBay has (EBAY_US/EBAY_GB/EBAY_AU/EBAY_CA), so a
-- currency other than USD is converted at read time by the caller
-- (CatalogSearchService._fetch_pricecharting_listings), not by caching a
-- separate copy per display currency.
create table if not exists public.pricecharting_listing_cache (
    catalog_id text primary key references public.pricecharting_catalog(pricecharting_id) on delete cascade,
    listings jsonb not null default '[]'::jsonb,
    fetched_at timestamptz not null default now()
);

create index if not exists pricecharting_listing_cache_fetched_at_idx
    on public.pricecharting_listing_cache (fetched_at);

alter table public.pricecharting_listing_cache enable row level security;
-- No policies: service-role only, same pattern as the other catalog reference tables.

-- Adds the second marketplace-listing source to the same kill-switch table
-- ebay_listing_cache's migration created (catalog_marketplace_source_flags)
-- -- one shared on/off-switch table for every marketplace-listing source,
-- not a new table per source.
insert into public.catalog_marketplace_source_flags (source, enabled)
values
    ('pricecharting', true)
on conflict (source) do nothing;
