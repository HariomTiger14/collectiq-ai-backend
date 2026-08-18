-- Per-category on/off switch for publisher-sourced catalog images
-- (Pokemon/Magic/Yu-Gi-Oh/Lorcana/One Piece/LEGO/Funko). Checked by
-- CatalogSearchService.detail() before each _enrich_with_*_image call, so
-- an admin can disable any one category's images app-wide, instantly and
-- without a deploy, from the admin portal -- e.g. in response to a
-- publisher takedown request. Defaults to enabled=true for every category
-- shipped today; missing/unknown categories are treated as enabled by the
-- service layer (fail-open, so a flags-fetch problem never blanks out
-- images that would otherwise be fine).

create table if not exists public.catalog_image_source_flags (
    category text primary key,
    enabled boolean not null default true,
    updated_at timestamptz not null default now()
);

insert into public.catalog_image_source_flags (category, enabled)
values
    ('funko', true),
    ('pokemon', true),
    ('lego', true),
    ('magic', true),
    ('yugioh', true),
    ('lorcana', true),
    ('onepiece', true)
on conflict (category) do nothing;

create or replace function public.touch_catalog_image_source_flags_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists touch_catalog_image_source_flags_updated_at
    on public.catalog_image_source_flags;
create trigger touch_catalog_image_source_flags_updated_at
before update on public.catalog_image_source_flags
for each row execute function public.touch_catalog_image_source_flags_updated_at();

alter table public.catalog_image_source_flags enable row level security;
-- No policies: service-role only, same pattern as the other catalog reference tables.
