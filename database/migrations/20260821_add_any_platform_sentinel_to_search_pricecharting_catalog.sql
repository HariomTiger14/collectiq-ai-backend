-- Adds an "any platform" mode to search_pricecharting_catalog()'s existing
-- platform_group_filter param, for the mobile Discover filter's new
-- Category/Subcategory split: selecting "Video Games" as the top-level
-- Category with no Subcategory picked means "any video game, regardless of
-- platform" -- previously there was no way to express that. platform_group
-- is not just a Video-Games-only column: compute_platform_group() runs
-- against every row with a console_name (see
-- 20260820_add_platform_group_step1_schema.sql), and its regexes only ever
-- match real video-game console names, so `platform_group is not null` is
-- already, incidentally, a correct "this row is a video game" predicate --
-- no new column or backfill needed to support this.
--
-- platform_group_filter = '__any_platform__' (a sentinel string, chosen
-- because it can never collide with a real platform_group value -- those
-- are all short lowercase-hyphen keys like 'playstation'/'retro-other')
-- now means "platform_group is not null" instead of an exact-match filter.
-- Any other non-null value keeps the existing exact-match behavior
-- unchanged. Passing null still means "no platform filter at all" (the
-- pre-existing "All categories" behavior), distinct from the new sentinel.
--
-- Same parameter signature as before (no new params) -- this only changes
-- the function body, so no Python-layer signature concern and no DROP
-- FUNCTION/overload-ambiguity risk (same reasoning as
-- 20260820_tokenize_search_pricecharting_catalog.sql).
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
    tokens text[];
    tok text;
    token_where text := '';
    token_score text := '0';
    filter_where text;
    order_by_clause text;
begin
    tokens := array_remove(regexp_split_to_array(lower(trim(search_query)), '\s+'), '');
    if tokens is null or array_length(tokens, 1) is null then
        return;
    end if;

    -- Every token independently OR'd across the same 5 columns as before,
    -- then AND'd together across tokens, for the WHERE clause (token_where)
    -- -- and a parallel per-token score contribution (token_score) that
    -- rewards matches actually landing in product_name over an incidental
    -- hit elsewhere (e.g. a UPC digit). Each token's pattern is a literal
    -- constant once format()/%L substitutes it in -- that's what lets
    -- Postgres treat token_where as plannable, index-backed conditions
    -- instead of the unnest()-per-row approach that forced a sequential
    -- scan.
    foreach tok in array tokens loop
        token_where := token_where || format(
            ' and (c.product_name ilike %L or c.console_name ilike %L
                or c.category ilike %L or c.upc ilike %L or c.normalized_identity ilike %L)',
            '%' || tok || '%', '%' || tok || '%', '%' || tok || '%', '%' || tok || '%', '%' || tok || '%'
        );
        token_score := token_score || format(
            ' + (case when c.product_name ilike %L then 2
                      when c.normalized_identity ilike %L then 1
                      else 0 end)',
            '%' || tok || '%', '%' || tok || '%'
        );
    end loop;
    token_where := substring(token_where from 6); -- strip the leading ' and '

    filter_where := format(
        '(%L::text[] is null or exists (
                select 1 from unnest(%L::text[]) kw where c.category ilike ''%%'' || kw || ''%%''
            ))
          and (%L::bigint is null or c.loose_price_cents >= %L::bigint)
          and (%L::bigint is null or c.loose_price_cents <= %L::bigint)
          and (
              %L::text is null
              or (%L::text = ''__any_platform__'' and c.platform_group is not null)
              or (%L::text <> ''__any_platform__'' and c.platform_group = %L::text)
          )',
        category_keywords, category_keywords,
        min_price_cents, min_price_cents,
        max_price_cents, max_price_cents,
        platform_group_filter, platform_group_filter, platform_group_filter, platform_group_filter
    );

    execute format(
        'explain (format json) select 1 from public.pricecharting_catalog c where %s and %s',
        token_where, filter_where
    ) into plan_json;

    estimated_rows := (plan_json->0->'Plan'->>'Plan Rows')::bigint;

    -- Ranking, in order: (1) the original phrase-based tiers -- an exact/
    -- prefix/substring match on the FULL phrase still ranks above anything
    -- token-based; (2) the new token_score -- among non-exact-phrase
    -- matches, more tokens landing in product_name (or at least
    -- normalized_identity) ranks higher than a coincidental single-column
    -- hit; (3) alphabetical, same final tiebreak as before.
    -- console_name/category are deliberately NOT given phrase-match tier
    -- priority (they were tiers 55 in an earlier version of this function)
    -- -- confirmed live this actively misranks results: a Funko figure
    -- whose category is literally labeled "God of War 4" (an informal
    -- franchise tag, not a real product identifier) outranked the actual
    -- "God of War" / Playstation 4 game, because the coarse category-
    -- phrase tier was checked before token_score and always won regardless
    -- of how well the game matched on product_name. Only product_name/upc/
    -- normalized_identity -- fields that actually identify a specific item
    -- -- earn tier priority; a console_name/category hit still counts for
    -- WHERE-clause recall (unchanged) but no longer skews ranking above a
    -- genuine product_name-concentrated token match.
    order_by_clause := format(
        'case
            when lower(c.product_name) = lower(%L) then 110
            when lower(c.upc) = lower(%L) then 110
            when lower(c.product_name) like lower(%L) || ''%%'' then 95
            when lower(c.product_name) like ''%%'' || lower(%L) || ''%%'' then 80
            when lower(c.normalized_identity) like ''%%'' || lower(%L) || ''%%'' then 70
            else 25
        end desc,
        (%s) desc,
        c.product_name asc,
        c.pricecharting_id asc',
        search_query, search_query, search_query, search_query, search_query,
        token_score
    );

    if estimated_rows <= broad_query_row_threshold then
        return query execute format(
            'select c.* from public.pricecharting_catalog c
             where %s and %s
             order by %s
             limit %L offset %L',
            token_where, filter_where, order_by_clause, result_limit, result_offset
        );
    else
        return query execute format(
            'select c.* from public.pricecharting_catalog c
             where %s and %s
             limit %L offset %L',
            token_where, filter_where, result_limit, result_offset
        );
    end if;
end;
$$;
