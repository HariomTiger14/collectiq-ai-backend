-- RUN ORDER: this references pricecharting_catalog.platform_group, so run
-- 20260820_add_platform_group_step1_schema.sql FIRST (it adds that column)
-- -- this migration will fail with "column c.platform_group does not
-- exist" otherwise.
--
-- Adds category-group, platform-group, and price-range filters to search_pricecharting_catalog()
-- so the admin Catalog products screen can combine a text search with the
-- category/price filters it already offers -- today typing a query silently
-- drops whatever category/min/max the admin had selected (AdminCatalogService
-- .list_items() only applies those on the plain-browse path, never the
-- search-RPC path). Confirmed live this is safe to add: category filtering
-- reuses the already-indexed `category` column (pricecharting_catalog_
-- category_trgm_idx, see 20260806_add_trigram_indexes_for_catalog_search.sql)
-- and price filtering is the same loose_price_cents range check already
-- shipped and working in browse mode (20260814 category+price filters PR) --
-- applied here as an extra AND on top of the trigram-narrowed search
-- candidates, not a fresh full-table scan.
--
-- category_keywords is a text[] of already-resolved keywords (e.g.
-- ARRAY['Baseball','Basketball',...] for 'sports-cards'), not a group key --
-- PRICECHARTING_CATEGORY_GROUPS stays defined once, in Python
-- (admin_catalog_service.py), and the caller resolves group -> keywords
-- before calling this RPC. Keeps the taxonomy in one place.
--
-- CREATE OR REPLACE only replaces a function with the exact same parameter
-- signature -- adding three new parameters makes this a distinct overload
-- from the existing 4-parameter version, not a replacement of it. Without
-- this drop, both versions exist simultaneously and any 4-argument call
-- becomes ambiguous (Postgres error 42725, same failure mode documented in
-- the two prior migrations to this function).
drop function if exists public.search_pricecharting_catalog(text, integer, integer, integer);

create or replace function public.search_pricecharting_catalog(
    search_query text,
    result_limit integer default 20,
    broad_query_row_threshold integer default 5000,
    result_offset integer default 0,
    category_keywords text[] default null,
    min_price_cents bigint default null,
    max_price_cents bigint default null,
    platform_group_filter text default null
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
    -- The estimate itself is filtered too -- otherwise a highly selective
    -- category/price filter could still get routed down the broad-query
    -- (unordered, non-deterministic) path just because the UNFILTERED text
    -- match alone looks broad, when the actually-returned set is small.
    execute format(
        $q$explain (format json)
        select 1 from public.pricecharting_catalog c
        where (c.product_name ilike %L or c.console_name ilike %L
           or c.category ilike %L or c.upc ilike %L
           or c.normalized_identity ilike %L)
          and (%L::text[] is null or exists (
                select 1 from unnest(%L::text[]) kw where c.category ilike '%%' || kw || '%%'
              ))
          and (%L::bigint is null or c.loose_price_cents >= %L::bigint)
          and (%L::bigint is null or c.loose_price_cents <= %L::bigint)
          and (%L::text is null or c.platform_group = %L::text)$q$,
        pattern, pattern, pattern, pattern, pattern,
        category_keywords, category_keywords,
        min_price_cents, min_price_cents,
        max_price_cents, max_price_cents,
        platform_group_filter, platform_group_filter
    ) into plan_json;

    estimated_rows := (plan_json->0->'Plan'->>'Plan Rows')::bigint;

    if estimated_rows <= broad_query_row_threshold then
        return query
        select c.*
        from public.pricecharting_catalog c
        where
            (c.product_name ilike pattern
                or c.console_name ilike pattern
                or c.category ilike pattern
                or c.upc ilike pattern
                or c.normalized_identity ilike pattern)
            and (category_keywords is null or exists (
                select 1 from unnest(category_keywords) kw where c.category ilike '%' || kw || '%'
            ))
            and (min_price_cents is null or c.loose_price_cents >= min_price_cents)
            and (max_price_cents is null or c.loose_price_cents <= max_price_cents)
            and (platform_group_filter is null or c.platform_group = platform_group_filter)
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
            (c.product_name ilike pattern
                or c.console_name ilike pattern
                or c.category ilike pattern
                or c.upc ilike pattern
                or c.normalized_identity ilike pattern)
            and (category_keywords is null or exists (
                select 1 from unnest(category_keywords) kw where c.category ilike '%' || kw || '%'
            ))
            and (min_price_cents is null or c.loose_price_cents >= min_price_cents)
            and (max_price_cents is null or c.loose_price_cents <= max_price_cents)
            and (platform_group_filter is null or c.platform_group = platform_group_filter)
        limit result_limit
        offset result_offset;
    end if;
end;
$$;
