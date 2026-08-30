-- Re-hosting Yu-Gi-Oh images (scripts/rehost_yugioh_images.py): image_url
-- moves to our own public catalog-images bucket per YGOPRODeck's stated
-- policy ("download and re-host the images yourself"); the original
-- provider URL is preserved here for provenance and re-runs.
alter table public.yugioh_catalog
  add column if not exists source_image_url text;
