alter table public.pricecharting_catalog
    add column if not exists content_hash text;
