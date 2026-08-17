-- Reference table for One Piece Card Game images, sourced from
-- optcgapi.com, a free, public, no-API-key bulk export (set cards +
-- starter deck cards + promo cards, ~5,162 real cards at last count).
-- This table has NO pricing data -- it exists purely to enrich
-- PriceCharting-sourced One Piece catalog rows (which have real pricing
-- but no images) with a real card photo.
--
-- PriceCharting's One Piece titles embed Bandai's own set-code convention
-- (e.g. "Captain John OP07-082"), which matches optcgapi.com's
-- card_set_id field directly. Unlike Yu-Gi-Oh (where a set code is
-- essentially globally unique per print), One Piece promo reprints
-- routinely REUSE the base card's set code -- verified live: 40% of
-- codes in optcgapi's own data map to more than one card (championship
-- prizes, tournament packs, box toppers, etc. sharing "OP01-077" with
-- the plain base card). This table therefore keeps every row rather than
-- collapsing to one image per code, with `is_plain` marking whether a
-- row's card_name has no parenthetical/bracket variant suffix (the base,
-- unambiguous print) -- see catalog_search_service.py's
-- _enrich_with_onepiece_image for how that's used: a PriceCharting row
-- with no bracket tag matches the single plain row for its code (if
-- exactly one exists); a row with a bracket tag (e.g. "[Alternate Art]")
-- requires a word-overlap match against a non-plain row's name; anything
-- else is left unmatched rather than guessed at. Spot-checked live
-- against a real 500-row PriceCharting sample: 63% real match rate:
-- (a) sealed products and DON!! cards (a separate card type with no set
-- code at all) are correctly never matched, (b) Japanese-exclusive prints
-- are only partially covered (the same gap found in every other card
-- game category so far), and (c) a real, deliberately small set of
-- specific tournament-only promo prints simply aren't in optcgapi's data.
--
-- Re-running the import is idempotent on `content_hash` (card_set_id +
-- card_name + image_url) since optcgapi.com has no stable per-row id of
-- its own to key on -- same pattern as the Funko import.

create table if not exists public.one_piece_catalog (
    row_id uuid primary key default gen_random_uuid(),
    card_set_id text not null,
    card_name text not null,
    is_plain boolean not null,
    image_url text not null,
    source text not null,
    content_hash text not null,
    imported_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists one_piece_catalog_card_set_id_idx
    on public.one_piece_catalog (card_set_id);

create unique index if not exists one_piece_catalog_content_hash_idx
    on public.one_piece_catalog (content_hash);

create or replace function public.touch_one_piece_catalog_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists touch_one_piece_catalog_updated_at on public.one_piece_catalog;
create trigger touch_one_piece_catalog_updated_at
before update on public.one_piece_catalog
for each row execute function public.touch_one_piece_catalog_updated_at();

alter table public.one_piece_catalog enable row level security;
-- No policies: service-role only, same pattern as the other catalog reference tables.
