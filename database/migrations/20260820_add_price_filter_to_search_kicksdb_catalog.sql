-- Adds price-range filtering to search_kicksdb_catalog(), so the public/
-- mobile Discover search can filter KicksDB (sneaker/streetwear) results
-- by price the same way it now can for PriceCharting (see
-- 20260820_add_filters_to_search_pricecharting_catalog.sql). KicksDB has
-- no category/platform taxonomy (nothing to mirror platform_group_filter
-- here), so this only adds min/max price.
--
-- kicksdb_catalog is ~11K rows -- a plain WHERE-clause price filter here
-- carries none of the risk the pricecharting_catalog RPC changes had to
-- account for (that table is ~12M rows with a real prior production
-- incident from index-maintenance overhead).
--
-- CREATE OR REPLACE only replaces a function with the exact same parameter
-- signature -- adding two new parameters makes this a distinct overload
-- from the existing 2-parameter version, not a replacement of it. Without
-- this drop, both versions exist simultaneously and any 2-argument call
-- becomes ambiguous (Postgres error 42725, same failure mode documented
-- for search_pricecharting_catalog's own migrations).
drop function if exists public.search_kicksdb_catalog(text, integer);

create or replace function public.search_kicksdb_catalog(
    search_query text,
    result_limit integer default 20,
    min_price_cents integer default null,
    max_price_cents integer default null
)
returns setof public.kicksdb_catalog
language sql
stable
as $$
    select c.*
    from public.kicksdb_catalog c
    where
        (c.title ilike '%' || search_query || '%'
            or c.brand ilike '%' || search_query || '%'
            or c.model ilike '%' || search_query || '%'
            or c.sku ilike '%' || search_query || '%')
        and (min_price_cents is null or c.avg_price_cents >= min_price_cents)
        and (max_price_cents is null or c.avg_price_cents <= max_price_cents)
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
