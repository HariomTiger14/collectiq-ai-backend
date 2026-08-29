-- Adds pagination to search_pricecharting_catalog() (originally created in
-- 20260806_create_search_pricecharting_catalog_rpc.sql, made adaptive in
-- 20260806_make_search_pricecharting_catalog_adaptive.sql). Search results
-- were always a single unpaginated batch (result_limit rows, no offset) --
-- fine for a "top few matches" UI, but it meant a franchise with many
-- editions/reprints ranking ahead of it (e.g. "Uncharted 2" variants
-- outranking "Uncharted 4") could push a real match past the cutoff with
-- no way to page further and find it. This adds result_offset so the admin
-- catalog search can page through the full ranked result set instead of
-- only ever seeing the first result_limit rows.
--
-- Applies to both branches of the adaptive design (narrow-query relevance-
-- ranked path AND broad-query early-exit path) -- the broad-query branch's
-- ordering is still not fully deterministic across calls (documented in
-- the adaptive migration's own comment: "non-deterministic-but-fast"), so
-- paging through a >5000-match broad query can in principle skip or repeat
-- a row at a page boundary. That's an existing, already-accepted tradeoff
-- of the broad-query fast path, not a new one introduced here -- and it
-- only affects queries broad enough to hit that branch at all (~21% of the
-- table matching a single word, e.g. "pokemon"), not specific-item
-- searches like the "Uncharted 4" case this was written for.
--
-- CREATE OR REPLACE only replaces a function with the exact same parameter
-- signature -- adding result_offset makes this a distinct overload from
-- the existing 3-parameter version, not a replacement of it. Without this
-- drop, both versions exist simultaneously and any 3-argument call becomes
-- ambiguous (Postgres error 42725, same failure mode documented in the
-- adaptive migration when it added its own new parameter).
drop function if exists public.search_pricecharting_catalog(text, integer, integer);

create or replace function public.search_pricecharting_catalog(
    search_query text,
    result_limit integer default 20,
    broad_query_row_threshold integer default 5000,
    result_offset integer default 0
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
        limit result_limit
        offset result_offset;
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
        limit result_limit
        offset result_offset;
    end if;
end;
$$;
