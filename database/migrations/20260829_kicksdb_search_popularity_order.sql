-- Break search-score ties by marketplace popularity instead of
-- alphabetically. A broad query like "nike" matches thousands of titles
-- at the same score, and the old `title asc` tie-break surfaced whatever
-- sorted first in the alphabet ("Nike A'ja Wilson ..." women's models
-- filled the whole first page). KicksDB's rank column is StockX
-- popularity (lower = more ordered), which is what a person typing a
-- brand name actually wants to see first. Title/id remain as stable
-- final tie-breaks.

create or replace function public.search_kicksdb_catalog(
    search_query text,
    result_limit integer default 20,
    min_price_cents integer default null::integer,
    max_price_cents integer default null::integer
)
returns setof kicksdb_catalog
language sql
stable
as $function$
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
        c.rank asc nulls last,
        c.title asc,
        c.kicksdb_id asc
    limit result_limit;
$function$;
