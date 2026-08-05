-- pricing_cache_entries has no product-name/title column today —
-- normalized_identity is a lowercased matching key, not a display title,
-- and display_string is a formatted price ("$45.00 USD"), not a name.
-- pricecharting_catalog.product_name is NOT NULL, so scan-derived catalog
-- promotion (docs/GLOBAL_CATALOG_ARCHITECTURE.md) has no usable title to
-- promote without this. Nullable: existing rows predate this column and
-- won't have one until they're refreshed by a later scan.

alter table public.pricing_cache_entries
    add column if not exists title text;
