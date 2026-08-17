-- Reference table for LEGO set product images, sourced from Rebrickable's
-- free, public, no-key-required bulk export (rebrickable.com/downloads,
-- sets.csv.gz). This table has NO pricing data -- it exists purely to
-- enrich PriceCharting-sourced LEGO catalog rows (which have real pricing
-- but no images) with a real product photo.
--
-- Verified live before this table was built: 28,099 real LEGO sets, 100%
-- image coverage (0 rows missing img_url), real distinct photos (spot-
-- checked via HTTP HEAD + Content-Length). PriceCharting's LEGO product
-- names embed the official LEGO set number (e.g. "Altair #7322"), which
-- matches Rebrickable's set_num column directly -- see
-- scripts/import_rebrickable_lego_catalog.py.
--
-- Unlike Pokemon cards, LEGO set numbers are unique retail product
-- identifiers, not a card+print-run pair with multiple ambiguous variants
-- -- so there's no print-variant safety problem here. There IS a real
-- match-safety problem of a different kind, though: LEGO has reused old
-- set numbers across unrelated product lines over the decades (spot-
-- checked live: PriceCharting's "Roof Bricks #445" collides on number
-- with Rebrickable's unrelated "Police Units" set). Matching purely on
-- set number alone measured ~96% "matches" but included real false
-- positives; requiring the PriceCharting title's own words to overlap
-- with Rebrickable's set name as well brought that down to a safe ~88%
-- with the false positives eliminated -- see
-- catalog_search_service.py's _enrich_with_lego_image for where that
-- check actually happens. `base_number` (digits only, no leading zeros,
-- no "-N" variant suffix) is the lookup key this table exists to serve;
-- `name` is what the word-overlap safety check runs against.
--
-- Re-running the import UPDATES existing rows (Rebrickable's own export
-- is periodically refreshed with new sets), so the unique key is
-- Rebrickable's own set_num, not a content hash.

create table if not exists public.rebrickable_lego_catalog (
    set_num text primary key,
    base_number text not null,
    name text not null,
    year integer,
    image_url text not null,
    imported_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists rebrickable_lego_catalog_base_number_idx
    on public.rebrickable_lego_catalog (base_number);

create or replace function public.touch_rebrickable_lego_catalog_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists touch_rebrickable_lego_catalog_updated_at on public.rebrickable_lego_catalog;
create trigger touch_rebrickable_lego_catalog_updated_at
before update on public.rebrickable_lego_catalog
for each row execute function public.touch_rebrickable_lego_catalog_updated_at();

alter table public.rebrickable_lego_catalog enable row level security;
-- No policies: service-role only, same pattern as the other catalog reference tables.
