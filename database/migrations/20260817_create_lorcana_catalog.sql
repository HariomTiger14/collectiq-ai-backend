-- Reference table for Disney Lorcana card images, sourced from two free,
-- public, no-key APIs: lorcana-api.com (primary -- images hosted on
-- api.lorcana.ravensburger.com, the official Lorcana publisher's own CDN,
-- verified live) and Lorcast/api.lorcast.com (fallback -- a purpose-built
-- third-party Lorcana database). This table has NO pricing data -- it
-- exists purely to enrich PriceCharting-sourced Lorcana catalog rows
-- (which have real pricing but no images) with a real card photo.
--
-- Unlike Yu-Gi-Oh, Lorcana card numbers are only unique WITHIN a set, not
-- globally, so the lookup key here is (normalized_set_name,
-- card_number) -- same shape as Magic's number-based match, and for the
-- same reason safe without an extra ambiguity check: Lorcana card
-- numbers are assigned once per set by the publisher, no reused-number
-- risk was found (unlike LEGO). `normalized_set_name` uses the exact
-- same punctuation-stripping normalization as Magic's
-- normalized_set_name (see catalog_search_service.py's
-- _normalize_magic_text, reused here rather than duplicated) --
-- PriceCharting's "Lorcana Attack of the Vine" and Lorcast's "Attack of
-- the Vine!" only differ by that trailing punctuation, verified live.
--
-- Both sources were compared live on a real 500-row PriceCharting sample:
-- lorcana-api.com matched 494/497 real numbered rows, Lorcast matched
-- 495/497, with 494 overlapping -- essentially tied, so the official-CDN
-- source was chosen as primary and the other kept as a fallback for the
-- single card it covers that lorcana-api.com's snapshot didn't have yet.
-- The only real gap left (both sources) is PriceCharting's "Lorcana
-- Promo" console_name, which doesn't map to one specific promo set
-- (Lorcana has several: P1/P2/P3/D23/etc) -- left unmatched rather than
-- guessed at.
--
-- Import order matters and is documented in each script -- Lorcast first
-- (scripts/import_lorcast_catalog.py), then lorcana-api.com second
-- (scripts/import_lorcana_api_catalog.py) so the official-CDN data
-- naturally overwrites any (set, number) both sources have.
--
-- Re-running either import UPDATES existing rows for that source (both
-- source APIs are live and periodically updated with new sets), so the
-- unique key is the (set, number) pair itself, not a content hash.

create table if not exists public.lorcana_catalog (
    normalized_set_name text not null,
    card_number text not null,
    image_url text not null,
    source text not null,
    imported_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    primary key (normalized_set_name, card_number)
);

create or replace function public.touch_lorcana_catalog_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists touch_lorcana_catalog_updated_at on public.lorcana_catalog;
create trigger touch_lorcana_catalog_updated_at
before update on public.lorcana_catalog
for each row execute function public.touch_lorcana_catalog_updated_at();

alter table public.lorcana_catalog enable row level security;
-- No policies: service-role only, same pattern as the other catalog reference tables.
