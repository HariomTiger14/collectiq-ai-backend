-- DB-side relevance ranking for kicksdb_catalog search, mirroring
-- search_pricecharting_catalog() (20260806_create_search_pricecharting_catalog_rpc.sql)
-- so Discover's catalog search can include real sneaker/streetwear results
-- (with real product images from kicksdb_catalog.image_url) alongside
-- PriceCharting results.
--
-- kicksdb_catalog is ~11K rows (vs ~8M for pricecharting_catalog), so the
-- "broad query forces an unindexed sequential scan at unacceptable cost"
-- problem that motivated the adaptive two-path version of the PriceCharting
-- RPC (20260806_make_search_pricecharting_catalog_adaptive.sql) does not
-- apply here — a single ranked query is safe at this table size. If the
-- catalog grows dramatically, re-run the same EXPLAIN ANALYZE check
-- documented for the PriceCharting RPC before trusting this at scale.
--
-- Scoring buckets (110/95/80/55/25) mirror _kicksdb_match_score() in
-- catalog_search_service.py.

create or replace function public.search_kicksdb_catalog(
    search_query text,
    result_limit integer default 20
)
returns setof public.kicksdb_catalog
language sql
stable
as $$
    select c.*
    from public.kicksdb_catalog c
    where
        c.title ilike '%' || search_query || '%'
        or c.brand ilike '%' || search_query || '%'
        or c.model ilike '%' || search_query || '%'
        or c.sku ilike '%' || search_query || '%'
    order by
        case
            when lower(c.title) = lower(search_query) then 110
            when lower(c.sku) = lower(search_query) then 110
            when lower(c.title) like lower(search_query) || '%' then 95
            when lower(c.title) like '%' || lower(search_query) || '%' then 80
            when lower(c.brand) like '%' || lower(search_query) || '%' then 55
            when lower(c.model) like '%' || lower(search_query) || '%' then 55
            else 25
        end desc,
        c.title asc,
        c.kicksdb_id asc
    limit result_limit;
$$;
