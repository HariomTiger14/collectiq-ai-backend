-- search_pricecharting_catalog() (20260806_create_search_pricecharting_catalog_rpc.sql)
-- fixed relevance ranking for specific/narrow queries ('pikachu v':
-- 15.7s -> 83ms once pg_trgm indexes were added) but was never wired into
-- the app, because a broader check found a real regression it would have
-- introduced: the app's CURRENT unordered fetch can early-exit a scan once
-- it's found enough matches (no ORDER BY forces considering every row), so
-- a broad single-word query like 'pokemon' (~21% of ~433K rows) is
-- actually fast today (~0.5-1s, confirmed live). The ranked version can't
-- early-exit — it must consider the true full matching set to find the
-- correct top-N — so the same query took ~15s through it. Shipping the
-- simple ranked version would trade "narrow queries: broken -> fast" for
-- "broad queries: fast -> broken."
--
-- This replaces it with an adaptive version: estimate how many rows a
-- query would match BEFORE deciding how to answer it, using Postgres's own
-- planner estimate (EXPLAIN, not a real COUNT — a real count of a broad
-- ilike match would cost as much as the problem it's trying to avoid).
-- Below the threshold: full relevance-ranked query (the fix). At or above
-- it: the original cheap, early-exit-capable, non-deterministic-but-fast
-- unordered query (today's existing behavior, unregressed).
--
-- broad_query_row_threshold=5000 is a judgment call, not derived from
-- data — the only two real measurements are ~109 matches ('pikachu v',
-- fast either way) and ~92,347 matches ('pokemon', where ranking becomes
-- unacceptably slow). 5000 sits with wide margin below the slow case and
-- wide margin above realistic specific-item match counts, but the actual
-- cost curve between those two points is unverified. Test a few queries
-- in that middle range via EXPLAIN ANALYZE before trusting this blindly at
-- higher search volumes than tested today.

-- CREATE OR REPLACE only replaces a function with the exact same parameter
-- signature — adding broad_query_row_threshold below makes this a distinct
-- overload from the original 2-parameter version, not a replacement of it.
-- Without this drop, both versions exist simultaneously and any 2-argument
-- call becomes ambiguous (Postgres error 42725, confirmed live).
drop function if exists public.search_pricecharting_catalog(text, integer);

create or replace function public.search_pricecharting_catalog(
    search_query text,
    result_limit integer default 20,
    broad_query_row_threshold integer default 5000
)
returns setof public.pricecharting_catalog
language plpgsql
volatile -- EXPLAIN is not permitted inside a STABLE/IMMUTABLE function
         -- (confirmed live: Postgres error 0A000) — and volatile is the
         -- semantically correct marking anyway, since this does dynamic
         -- plan introspection, not a pure read Postgres can cache/reorder.
as $$
declare
    plan_json json;
    estimated_rows bigint;
    pattern text := '%' || search_query || '%';
begin
    execute format(
        $q$explain (format json)
        select 1 from public.pricecharting_catalog c
        where c.product_name ilike %L or c.console_name ilike %L
           or c.category ilike %L or c.upc ilike %L
           or c.normalized_identity ilike %L$q$,
        pattern, pattern, pattern, pattern, pattern
    ) into plan_json;

    estimated_rows := (plan_json->0->'Plan'->>'Plan Rows')::bigint;

    if estimated_rows <= broad_query_row_threshold then
        return query
        select c.*
        from public.pricecharting_catalog c
        where
            c.product_name ilike pattern
            or c.console_name ilike pattern
            or c.category ilike pattern
            or c.upc ilike pattern
            or c.normalized_identity ilike pattern
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
    else
        return query
        select c.*
        from public.pricecharting_catalog c
        where
            c.product_name ilike pattern
            or c.console_name ilike pattern
            or c.category ilike pattern
            or c.upc ilike pattern
            or c.normalized_identity ilike pattern
        limit result_limit;
    end if;
end;
$$;
