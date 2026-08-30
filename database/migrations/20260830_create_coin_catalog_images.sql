-- Coin images, curated from Wikimedia Commons and re-hosted in our own
-- catalog-images bucket.
--
-- Why this shape (see packlox-sportscard-images-research memory,
-- 2026-08-30 coins sweep):
--   * US coin DESIGNS are federal government works = public domain, so
--     unlike card/comic art there is no rights-holder to license from.
--     Commons files carrying a PD (or CC) licence may be copied and
--     re-hosted outright -- hence storing our own copy rather than
--     hotlinking, which also removes any Wikimedia hotlink-etiquette
--     question and any dependency on their CDN.
--   * Coins need DESIGN-level images, not year+mint-mark ones: a 1970-D
--     and a 1970-S Roosevelt dime are the same picture. So the key is
--     the series (plus an optional design variant for multi-design
--     series like State Quarters, where each state is its own design),
--     NOT an individual catalogue row. 68 series => ~150 images total.
--   * Two views per design (obverse/reverse) is the whole reason the
--     multi-image `images[]` field exists.
--
-- Licence/credit are stored per image and MUST travel with it: most
-- files are public domain, but some are CC BY-SA where attribution is a
-- condition of use.

create table if not exists public.coin_catalog_images (
    id bigint generated always as identity primary key,
    -- Normalized PriceCharting console_name with the "Coins " prefix
    -- stripped, e.g. "mercury dime", "state quarter".
    series_key text not null,
    -- Design within a multi-design series (e.g. "louisiana" for State
    -- Quarters); empty string for single-design series so the unique
    -- index stays simple.
    design_key text not null default '',
    -- 'obverse' | 'reverse'
    view text not null,
    -- Our re-hosted copy in the catalog-images bucket (what we serve).
    image_url text not null,
    -- Provenance, kept for auditability and re-runs.
    source_url text,
    source_page text,
    license text,
    credit text,
    imported_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (series_key, design_key, view)
);

-- The enrichment lookup: series (+design) -> its views.
create index if not exists coin_catalog_images_lookup
    on public.coin_catalog_images (series_key, design_key);

alter table public.coin_catalog_images enable row level security;
