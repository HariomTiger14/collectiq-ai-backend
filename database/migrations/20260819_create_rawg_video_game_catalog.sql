-- Static reference table for video game cover art, bulk-imported from the
-- RAWG API (api.rawg.io) -- the only image source in this project with
-- explicit written commercial-use permission (RAWG's Terms of Service:
-- free for commercial use under their free tier, in exchange for
-- attribution -- see the "Legal" section of the mobile app's About screen).
--
-- Unlike the live-lookup + cache approach this replaces
-- (rawg_video_game_cache, see 20260819_create_rawg_video_game_cache.sql),
-- this table is a real bulk-imported reference catalog, same pattern as
-- funko_pop_catalog/rebrickable_lego_catalog/etc: RAWG's list endpoint
-- (not their search endpoint) supports filtering by platform id, and a
-- single platform (e.g. PlayStation 4) has ~7,000 games -- confirmed live
-- -- so importing every game across the ~24 mainstream platforms
-- PriceCharting's console_name values map to (see
-- _VIDEO_GAME_PLATFORM_RAWG_MAP in catalog_search_service.py) fits
-- comfortably inside RAWG's 20,000 requests/month free-tier budget
-- (roughly 1,000-2,000 requests at 40 games/page), unlike trying to
-- import RAWG's entire 500,000+ game catalog.
--
-- One row per (rawg game, platform) pair, since the same title can
-- release on multiple platforms with the same cover art but PriceCharting
-- prices each platform's release as a separate catalog row -- matching
-- happens on (normalized_title, rawg_platform) together, mirroring how
-- catalog_search_service.py's _video_game_cache_key already combines
-- title+platform for the interim cache-based approach.

create table if not exists public.rawg_video_game_catalog (
    row_id uuid primary key default gen_random_uuid(),
    rawg_id bigint not null,
    rawg_slug text not null,
    name text not null,
    normalized_name text not null,
    rawg_platform text not null,
    image_url text,
    released date,
    content_hash text not null,
    imported_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists rawg_video_game_catalog_lookup_idx
    on public.rawg_video_game_catalog (normalized_name, rawg_platform);

create index if not exists rawg_video_game_catalog_rawg_id_idx
    on public.rawg_video_game_catalog (rawg_id);

-- Dedup safety net for repeat/resumed imports: same game+platform+image
-- should not duplicate on re-run.
create unique index if not exists rawg_video_game_catalog_content_hash_idx
    on public.rawg_video_game_catalog (content_hash);

create or replace function public.touch_rawg_video_game_catalog_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists touch_rawg_video_game_catalog_updated_at
    on public.rawg_video_game_catalog;
create trigger touch_rawg_video_game_catalog_updated_at
before update on public.rawg_video_game_catalog
for each row execute function public.touch_rawg_video_game_catalog_updated_at();

alter table public.rawg_video_game_catalog enable row level security;
-- No policies: service-role only, same pattern as the other catalog reference tables.
