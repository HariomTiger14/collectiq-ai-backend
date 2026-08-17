-- Reference table for Pokemon card product images, sourced from TCGCSV
-- (tcgcsv.com), a daily-updated, free, no-signup cache of TCGplayer's own
-- catalog export. This table has NO pricing data -- it exists purely to
-- enrich PriceCharting-sourced Pokemon catalog rows (which have real
-- pricing but no images) with a real product photo, replacing the
-- narrower TCGdex-based enrichment.
--
-- Unlike Funko, this is NOT a one-time static import: TCGCSV's own usage
-- guidelines say the data is rebuilt once per day and ask consumers to
-- re-sync at most once every 24h (see scripts/import_tcgplayer_pokemon_
-- catalog.py). Re-imports UPDATE existing rows (image URLs can change),
-- so the unique key is TCGplayer's own product id, not a content hash.
--
-- `group_name` matters beyond display: TCGplayer models a small number of
-- Pokemon print variants as genuinely separate, separately-photographed
-- products/groups -- most notably "Base Set (Shadowless)" as its own
-- group, and named error/misprint cards (e.g. "Charizard (Black Dot
-- Error)") as their own product within the normal group. `variant_tag`
-- records which of those two patterns a row belongs to ('shadowless' /
-- 'error'), so catalog_search_service.py can tell an exact print-variant
-- match apart from a merely-same-card-and-number generic match. Spot-
-- checked live across Base Set, Jungle, Fossil, Team Rocket, Gym Heroes,
-- Gym Challenge, Neo Genesis, and Neo Discovery before this table was
-- built: this separate-product treatment does NOT generalize past Base
-- Set, and individual-card "1st Edition" is never modeled as a distinct
-- product anywhere (only sealed booster boxes/packs get that tag) -- see
-- docs/GLOBAL_CATALOG_ARCHITECTURE.md for the full investigation. Rows
-- with no known variant treatment keep variant_tag null and are only
-- ever used as a generic, not-guaranteed-exact-print image.

create table if not exists public.tcgplayer_pokemon_catalog (
    tcgplayer_product_id bigint primary key,
    group_id integer not null,
    group_name text not null,
    product_name text not null,
    card_number text,
    image_url text not null,
    variant_tag text,
    imported_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- The lookup this table exists to serve: "give me the product(s) for this
-- TCGplayer group + card number", from catalog_search_service.py.
create index if not exists tcgplayer_pokemon_catalog_group_number_idx
    on public.tcgplayer_pokemon_catalog (group_name, card_number);

create or replace function public.touch_tcgplayer_pokemon_catalog_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists touch_tcgplayer_pokemon_catalog_updated_at on public.tcgplayer_pokemon_catalog;
create trigger touch_tcgplayer_pokemon_catalog_updated_at
before update on public.tcgplayer_pokemon_catalog
for each row execute function public.touch_tcgplayer_pokemon_catalog_updated_at();

alter table public.tcgplayer_pokemon_catalog enable row level security;
-- No policies: service-role only, same pattern as funko_pop_catalog/kicksdb_catalog.
