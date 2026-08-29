-- STEP 2 of 2 for price-sort support -- run this statement ALONE, after
-- step 1 has already been run and finished. See step 1 for why these can't
-- be batched together.
--
-- kicksdb_catalog has no comparable incident history and is a much
-- smaller, lower-write table (~7.9k rows/day on a daily refresh cron, vs
-- pricecharting_catalog's ~12M rows across a 15-min cron) -- a plain index
-- here is low-risk, included for the same "sort by price" admin feature.
create index concurrently if not exists kicksdb_catalog_avg_price_cents_idx
    on public.kicksdb_catalog (avg_price_cents);
