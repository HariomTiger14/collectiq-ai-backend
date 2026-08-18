-- Live-lookup + write-through cache for RAWG (api.rawg.io) video game cover
-- art. Unlike the other publisher-image sources (Funko/Pokemon/LEGO/Magic/
-- Yu-Gi-Oh/Lorcana/One Piece), RAWG's catalog is 500,000+ games -- far too
-- large to bulk-import into a reference table the way those are. Instead,
-- CatalogSearchService._enrich_with_video_games_image() looks up this table
-- first; on a miss it calls RAWG live (search endpoint, key-gated,
-- 20,000 req/month budget under RAWG's free commercial-use terms -- see
-- RAWG_API_KEY in .env) and writes the outcome back here, so the exact same
-- catalog item is never re-queried against RAWG again.
--
-- lookup_key is a deterministic, stable key derived from the normalized
-- PriceCharting title + mapped RAWG platform name (see
-- _video_game_cache_key in catalog_search_service.py) -- not the
-- pricecharting_id, so that distinct PriceCharting rows for the same real
-- game+platform (e.g. regional variants) share one cache entry instead of
-- each burning a separate RAWG request.
--
-- matched=false is a real, confirmed "RAWG responded, no valid match
-- found" result (including an exact title+platform match with no
-- background_image), cached deliberately to avoid re-querying RAWG for the
-- same unmatched item repeatedly. It is NEVER written for a network/HTTP
-- failure talking to RAWG -- those are treated as "try again next time",
-- not cached as a negative match.

create table if not exists public.rawg_video_game_cache (
    lookup_key text primary key,
    matched boolean not null default false,
    image_url text,
    rawg_slug text,
    checked_at timestamptz not null default now()
);

alter table public.rawg_video_game_cache enable row level security;
-- No policies: service-role only, same pattern as the other catalog
-- reference tables.

-- Video games joins the existing publisher-image kill switch (see
-- 20260818_create_catalog_image_source_flags.sql) so an admin can disable
-- RAWG-sourced images app-wide, instantly and without a deploy -- e.g. in
-- response to a RAWG rate-limit or ToS issue.
insert into public.catalog_image_source_flags (category, enabled)
values
    ('videogames', true)
on conflict (category) do nothing;
