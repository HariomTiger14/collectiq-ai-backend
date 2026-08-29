-- Reference table for Yu-Gi-Oh card images, sourced from two free, public,
-- no-key bulk exports: YGOPRODeck (db.ygoprodeck.com/api/v7/cardinfo.php,
-- primary) and TCGCSV (tcgcsv.com/tcgplayer/2, fallback). This table has
-- NO pricing data -- it exists purely to enrich PriceCharting-sourced
-- Yu-Gi-Oh catalog rows (which have real pricing but no images) with a
-- real card photo.
--
-- Unlike Pokemon/Magic, this needs no per-set name resolution at all:
-- PriceCharting's Yu-Gi-Oh titles already embed Yu-Gi-Oh's own globally
-- unique "set code" convention (e.g. "Where Arf Thou? SD40-JP033"), and
-- both source databases key printings by that exact same code (e.g.
-- YGOPRODeck's card_sets[].set_code, TCGCSV's extendedData "Number"
-- field) -- so `set_code` alone is the lookup key, primary key, and the
-- entire matching problem.
--
-- YGOPRODeck was chosen as primary after a live side-by-side comparison
-- against TCGCSV on a real 566-row PriceCharting sample: YGOPRODeck
-- matched 532 codes vs TCGCSV's 294, and matched nearly everything TCGCSV
-- did plus 243 more. TCGCSV only led on a handful of very recent (2026)
-- set releases that looked like a data-freshness lag, not a structural
-- gap -- which is why TCGCSV is kept as a fallback rather than dropped:
-- see scripts/import_tcgcsv_yugioh_catalog.py and
-- scripts/import_ygoprodeck_catalog.py. Import order matters and is
-- documented in each script -- TCGCSV first, then YGOPRODeck overwrites
-- any code both sources have, so YGOPRODeck's (better) data always wins
-- on overlap while TCGCSV's rows survive only for codes YGOPRODeck lacks.
--
-- Neither source has meaningful coverage of Japanese-exclusive (OCG) sets
-- -- confirmed live as a real, universal gap, not something either
-- import script tries to paper over.
--
-- YGOPRODeck's own data rarely has more than one photo for a card (only
-- 124/14,515 cards, ~0.85%, checked live) -- those rare alternate-art
-- cards are skipped entirely at import time rather than guessed at,
-- since there's no reliable way to tell which image belongs to which of
-- a card's several set_codes.

create table if not exists public.yugioh_catalog (
    set_code text primary key,
    card_name text not null,
    image_url text not null,
    source text not null,
    imported_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create or replace function public.touch_yugioh_catalog_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists touch_yugioh_catalog_updated_at on public.yugioh_catalog;
create trigger touch_yugioh_catalog_updated_at
before update on public.yugioh_catalog
for each row execute function public.touch_yugioh_catalog_updated_at();

alter table public.yugioh_catalog enable row level security;
-- No policies: service-role only, same pattern as the other catalog reference tables.
