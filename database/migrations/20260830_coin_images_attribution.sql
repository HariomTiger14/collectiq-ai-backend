-- Attribution metadata for coin images.
--
-- The original table stored licence + a free-text credit, which was enough
-- while every image was public domain. The cleared launch set includes one
-- CC BY-SA 4.0 image, and attribution for a CC BY/BY-SA work is a CONDITION
-- OF THE LICENCE, not a courtesy: shipping it without attribution is
-- infringement.
--
-- Relying on someone remembering to add it by hand is exactly the failure
-- mode to design out, so the obligation travels with the row and the app
-- renders it automatically wherever the image appears:
--
--   * attribution_required -- the app MUST display attribution for this image
--   * attribution_text     -- what to display (author + licence)
--   * attribution_url      -- where it links (the source page)
--
-- Public-domain rows carry credit for provenance but leave
-- attribution_required false, so the UI shows them without a required notice.

alter table public.coin_catalog_images
    add column if not exists attribution_required boolean not null default false,
    add column if not exists attribution_text text,
    add column if not exists attribution_url text;

-- An image that requires attribution is useless to us without the text to
-- show, so make that unrepresentable rather than a runtime surprise.
alter table public.coin_catalog_images
    drop constraint if exists coin_catalog_images_attribution_complete;

alter table public.coin_catalog_images
    add constraint coin_catalog_images_attribution_complete
    check (
        not attribution_required
        or (attribution_text is not null and length(btrim(attribution_text)) > 0)
    );

comment on column public.coin_catalog_images.attribution_required is
    'True for CC BY / CC BY-SA images, where displaying attribution is a licence condition.';
