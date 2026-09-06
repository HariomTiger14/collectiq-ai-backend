-- Register 'kicksdb' (Sneakers & Streetwear) as an administratively
-- toggleable image category.
--
-- Sneaker rows differ from every other category in the flags table: their
-- image_url arrives already populated on the kicksdb_catalog row, so there
-- is no enrichment call for the flag to skip. Disabling this category
-- instead SUPPRESSES the stored URL across all three surfaces the KicksDB
-- path can populate -- inline thumbnail, "View image" link, and the detail
-- gallery -- see _suppress_kicksdb_images in catalog_search_service.py.
--
-- Why it needs to exist at all: those URLs resolve to images.stockx.com.
-- StockX serves them through Cloudflare with no referer check today, but
-- hotlink protection there is a dashboard toggle on their side and can be
-- enabled globally without warning. Without a row here the admin portal
-- renders no switch for sneakers, leaving the category with no rollback.
--
-- Seeded enabled=true: this registers the control, it does not exercise it.
insert into public.catalog_image_source_flags (category, enabled)
values ('kicksdb', true)
on conflict (category) do nothing;
