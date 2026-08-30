-- Sneaker search: full-text token matching instead of one literal
-- substring, using the GIN index the table already had.
--
-- The old WHERE was `title ilike '%' || search_query || '%'`, so the
-- whole query had to appear contiguously and in order inside a single
-- field. Measured live before this change:
--     'new balance 574'  -> 5 results
--     '574 new balance'  -> 0        (word order mattered)
--     'new balance grey' -> 0        (words had to be adjacent, though
--                                     "New Balance 574 Off White Grey"
--                                     contains all three)
-- Cards never had this problem: search_pricecharting_catalog already
-- splits the query into tokens and ANDs them.
--
-- Implementation: kicksdb_catalog_search_idx is a GIN index over
-- to_tsvector('simple', title || brand || model || sku) that nothing
-- was querying. Matching that expression exactly lets the planner use
-- it. Every token is required (AND) and every token is a PREFIX match
-- (`:*`), so search-as-you-type finds results while a word is still
-- half-typed -- "new bala" matches "New Balance" -- which plain
-- to_tsquery equality would not do.
--
-- Tokens are stripped to [a-z0-9] before being assembled into the
-- tsquery: user input reaches to_tsquery() directly, and characters
-- like ' & | ! : ( ) are tsquery OPERATORS that would otherwise raise a
-- syntax error on ordinary queries such as "Dunk Low (GS)".
--
-- Ranking keeps the popularity tie-break from
-- 20260829_kicksdb_search_popularity_order.sql (broad queries like
-- "nike" score thousands of titles identically, and alphabetical
-- tie-breaking front-loaded the A's), with phrase/prefix matches
-- scoring above token-only matches.

create or replace function public.search_kicksdb_catalog(
    search_query text,
    result_limit integer default 20,
    min_price_cents integer default null::integer,
    max_price_cents integer default null::integer
)
returns setof kicksdb_catalog
language plpgsql
stable
as $function$
-- kicksdb_catalog has its own min_price_cents/max_price_cents COLUMNS,
-- which collide with the same-named function parameters under plpgsql
-- (the old SQL-language version had no such conflict). Resolve
-- unqualified references to the parameters; column references below are
-- all explicitly qualified with `c.`.
#variable_conflict use_variable
declare
    tokens text[];
    ts_query tsquery := null;
begin
    tokens := array_remove(
        array(
            select regexp_replace(word, '[^a-z0-9]', '', 'g')
            from unnest(
                regexp_split_to_array(lower(trim(coalesce(search_query, ''))), '\s+')
            ) as word
        ),
        ''
    );

    if tokens is not null and array_length(tokens, 1) is not null then
        -- Prefix-match every token: "new bala" should find "New Balance".
        ts_query := to_tsquery('simple', array_to_string(tokens, ':* & ') || ':*');
    end if;

    return query
    select c.*
    from public.kicksdb_catalog c
    where
        (
            -- Empty query = browse (the category chips rely on this).
            ts_query is null
            -- Matches kicksdb_catalog_search_idx's expression exactly so
            -- the GIN index is used rather than a sequential scan.
            or to_tsvector(
                   'simple',
                   coalesce(c.title, '') || ' ' || coalesce(c.brand, '') || ' '
                   || coalesce(c.model, '') || ' ' || coalesce(c.sku, '')
               ) @@ ts_query
        )
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
            -- Token-only match (all words present, not as a phrase):
            -- below every phrase match, above the unranked floor.
            else 40
        end desc,
        c.rank asc nulls last,
        c.title asc,
        c.kicksdb_id asc
    limit result_limit;
end;
$function$;
