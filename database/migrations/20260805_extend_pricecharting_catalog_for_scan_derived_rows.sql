-- Extends pricecharting_catalog to hold scan-derived rows (KicksDB/TCGPlayer/
-- eBay, promoted from pricing_cache_entries) alongside the existing
-- PriceCharting-imported rows. Additive only; existing rows/columns unaffected.
-- See docs/GLOBAL_CATALOG_ARCHITECTURE.md for the design this supports.

alter table public.pricecharting_catalog
    add column if not exists source_provider text not null default 'pricecharting_import',
    add column if not exists source_kind text not null default 'bulk_import',
    add column if not exists market_value_cents integer,
    add column if not exists low_estimate_cents integer,
    add column if not exists high_estimate_cents integer,
    add column if not exists promoted_from_cache_key text,
    add column if not exists verified_at timestamptz;

create index if not exists pricecharting_catalog_source_kind_idx
    on public.pricecharting_catalog (source_kind);

create unique index if not exists pricecharting_catalog_promoted_from_cache_key_idx
    on public.pricecharting_catalog (promoted_from_cache_key)
    where promoted_from_cache_key is not null;
