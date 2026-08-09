-- Admin dashboard visibility into the PriceCharting registry backfill and
-- KicksDB catalog refresh -- previously only checkable via hand-run SQL
-- queries during live tuning. GROUP BY aggregation happens server-side via
-- RPC rather than fetching every registry row over REST and aggregating in
-- Python: pricecharting_set_registry can hold tens of thousands of rows
-- across the sportscardspro categories alone, so a naive fetch-all would be
-- slow and wasteful for a dashboard summary that just needs per-category
-- counts. Run once in the Supabase SQL editor.

create or replace function public.admin_pricecharting_registry_summary()
returns table (
  source_site text,
  category text,
  total_sets bigint,
  completed_sets bigint,
  failed_sets bigint,
  pending_sets bigint,
  priority_tier integer
)
language sql
security definer
set search_path = public
as $$
  select
    source_site,
    category,
    count(*) as total_sets,
    count(*) filter (where last_fetch_status = 'success') as completed_sets,
    count(*) filter (where last_fetch_status = 'error') as failed_sets,
    count(*) filter (where last_fetch_status is null) as pending_sets,
    min(priority_tier) as priority_tier
  from public.pricecharting_set_registry
  group by source_site, category
  order by source_site, category;
$$;

create or replace function public.admin_kicksdb_catalog_summary()
returns table (
  total_items bigint,
  distinct_brands bigint,
  last_refreshed_at timestamptz
)
language sql
security definer
set search_path = public
as $$
  select
    count(*) as total_items,
    count(distinct brand) as distinct_brands,
    max(updated_at) as last_refreshed_at
  from public.kicksdb_catalog;
$$;
