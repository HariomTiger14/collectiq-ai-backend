-- Reference table for Magic: The Gathering card images, sourced from
-- Scryfall's free, public, no-key bulk export (api.scryfall.com/bulk-data
-- -> "default_cards"). This table has NO pricing data -- it exists purely
-- to enrich PriceCharting-sourced Magic catalog rows (which have real
-- pricing but no images) with a real, exact-print card photo.
--
-- Scryfall was chosen over TCGCSV/TCGplayer (used for Pokemon/LEGO) after
-- live comparison: Scryfall is purpose-built for Magic and models every
-- distinct printing -- including special treatments like Showcase and
-- Gilded Foil -- as its own card object with its own `collector_number`.
-- Verified live: PriceCharting's "Cabaretti Charm [Gilded Foil] #365"
-- lines up exactly with Scryfall's Gilded Showcase print of that card,
-- which has collector_number 365. Spot-checked against a real 400-row
-- PriceCharting sample: once the set itself resolves, EVERY card matched
-- (0 misses) -- the only real gap is set-name string differences
-- (apostrophes, punctuation) between PriceCharting's console_name and
-- Scryfall's set_name, which is why both `normalized_set_name` and
-- `normalized_name` are pre-computed at import time using the same
-- normalization catalog_search_service.py applies to PriceCharting's own
-- fields (see _normalize_magic_text there) -- both sides must stay in
-- sync, which is why the import script imports that function directly
-- rather than re-implementing it.
--
-- Matching in catalog_search_service.py: normalized_set_name +
-- collector_number when PriceCharting's title has a "#number" (true for
-- essentially every modern-set row, including every Showcase/Gilded Foil
-- row seen); normalized_set_name + normalized_name for older/vintage
-- rows that have no number, but ONLY when that resolves to exactly one
-- row -- an ambiguous multi-row name match (e.g. a reprinted basic land
-- with no distinguishing number) is treated as no confident match, same
-- "never guess" rule used everywhere else in this catalog work.
--
-- Re-running the import UPDATES existing rows (Scryfall's bulk export is
-- refreshed regularly), so the unique key is Scryfall's own card id, not
-- a content hash.

create table if not exists public.scryfall_magic_catalog (
    scryfall_id uuid primary key,
    set_name text not null,
    normalized_set_name text not null,
    collector_number text,
    name text not null,
    normalized_name text not null,
    image_url text not null,
    imported_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists scryfall_magic_catalog_set_number_idx
    on public.scryfall_magic_catalog (normalized_set_name, collector_number);

create index if not exists scryfall_magic_catalog_set_name_idx
    on public.scryfall_magic_catalog (normalized_set_name, normalized_name);

create or replace function public.touch_scryfall_magic_catalog_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists touch_scryfall_magic_catalog_updated_at on public.scryfall_magic_catalog;
create trigger touch_scryfall_magic_catalog_updated_at
before update on public.scryfall_magic_catalog
for each row execute function public.touch_scryfall_magic_catalog_updated_at();

alter table public.scryfall_magic_catalog enable row level security;
-- No policies: service-role only, same pattern as the other catalog reference tables.
