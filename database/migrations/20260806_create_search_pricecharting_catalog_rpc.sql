-- DB-side relevance ranking for Discover's catalog search, replacing the
-- app's "fetch an arbitrary subset, rank in Python" approach that caused
-- two separate bugs: non-determinism (no ORDER BY at all) and, in two
-- reverted attempts, either an unindexed-sort timeout or systematically
-- hiding scan-derived rows (see docs/GLOBAL_CATALOG_ARCHITECTURE.md).
--
-- This function filters + scores + sorts + limits in one query, so there is
-- no "which arbitrary subset landed in the fetch window" problem at all —
-- the true top-N by relevance is always returned, computed over the actual
-- matching set, not a truncated guess at it.
--
-- Match filter is identical to the existing app's OR/ilike conditions
-- (same fields, same substring semantics) — this does not change which
-- rows can match a query, only how they're ordered. Scoring buckets
-- (110/95/80/70/55/25) mirror _match_score() in catalog_search_service.py
-- exactly, so confidence/ranking behavior is unchanged for callers, just
-- computed correctly over the full match set instead of a fetch-window
-- subset.
--
-- BEFORE wiring the app to call this: run the EXPLAIN ANALYZE check in
-- docs/GLOBAL_CATALOG_ARCHITECTURE.md against real data and confirm it's
-- fast. Do not skip that step — that is exactly the step skipped twice
-- already on this table today.

create or replace function public.search_pricecharting_catalog(
    search_query text,
    result_limit integer default 20
)
returns setof public.pricecharting_catalog
language sql
stable
as $$
    select c.*
    from public.pricecharting_catalog c
    where
        c.product_name ilike '%' || search_query || '%'
        or c.console_name ilike '%' || search_query || '%'
        or c.category ilike '%' || search_query || '%'
        or c.upc ilike '%' || search_query || '%'
        or c.normalized_identity ilike '%' || search_query || '%'
    order by
        case
            when lower(c.product_name) = lower(search_query) then 110
            when lower(c.upc) = lower(search_query) then 110
            when lower(c.product_name) like lower(search_query) || '%' then 95
            when lower(c.product_name) like '%' || lower(search_query) || '%' then 80
            when lower(c.normalized_identity) like '%' || lower(search_query) || '%' then 70
            when lower(c.console_name) like '%' || lower(search_query) || '%' then 55
            when lower(c.category) like '%' || lower(search_query) || '%' then 55
            else 25
        end desc,
        c.product_name asc,
        c.pricecharting_id asc
    limit result_limit;
$$;
