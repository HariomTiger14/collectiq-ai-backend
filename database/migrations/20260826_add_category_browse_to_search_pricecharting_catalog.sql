-- Category browse for search_pricecharting_catalog.
--
-- Tapping a category in Discover could not simply list that category:
-- this function returned no rows at all for an empty search_query (the
-- token split produced an empty array and it returned early), so the app
-- had to type a representative search term into the box on the user's
-- behalf just to get results. That looked like the app typing for you,
-- and it isn't what "select a category" should mean.
--
-- With an empty query the function now browses instead of searching:
-- most valuable first (relevance tiers are meaningless with no search
-- term), priced rows only (an unpriced row is the least useful thing to
-- open a category with), pricecharting_id as tiebreak so paging is
-- stable.
--
-- HOW the browse executes is the hard-won part. Three designs were
-- measured live against this ~12M-row / 16GB table before this one:
--
--   1. Reuse the search path's category predicate with no tokens: the
--      exists(select ... from unnest(...)) form is a correlated subquery
--      the planner cannot serve from the trigram index, and with nothing
--      to narrow first it sequential-scanned the catalog -- did not
--      finish inside two minutes.
--   2. OR'd ilike + top-N sort over the category: the trigram index
--      finds the rows, but ranking a whole category means a bitmap heap
--      scan over every page it touches -- 45s for Comic Books (~276k
--      rows).
--   3. Walk the global price index and ilike-filter each row (covering
--      index, deferred join): first pages fine, but a sparse category's
--      matches hide among the priciest rows of the WHOLE catalog, and
--      every candidate needs a heap visibility check on a table whose
--      repricing churn keeps the visibility map permanently dirty --
--      80s for Yu-Gi-Oh at offset 200, i.e. the third scroll.
--
-- The design that works makes category part of the index key instead of
-- a filter: pricecharting_browse_category(category) collapses the
-- free-text category column to the canonical keyword the service layer
-- sends (see 20260826_add_pricecharting_browse_category_function.sql),
-- and the browse indexes (20260826_add_pricecharting_catalog_browse_
-- indexes.sql) key on that expression -- or on platform_group for the
-- Video Games paths -- followed by the price order. Browse then unions
-- one equality + ordered range scan per keyword, each capped at
-- limit+offset rows, and pages the union. Equality on the leading column
-- rather than = ANY() is what keeps each scan ordered; the union is
-- duplicate-free because the function maps every row to at most one
-- keyword. Cost is proportional to page depth, not catalog size.
--
-- Search behaviour with a non-empty query is unchanged, and no existing
-- caller passes an empty query (the service layer rejected it), so this
-- is additive. A short query with NO category filter still returns
-- nothing (service-layer guard), and an empty query with no filter at
-- all returns nothing here too -- browsing requires saying what to
-- browse, and the alternative is a full catalog sweep.

CREATE OR REPLACE FUNCTION public.search_pricecharting_catalog(search_query text, result_limit integer DEFAULT 20, broad_query_row_threshold integer DEFAULT 5000, result_offset integer DEFAULT 0, category_keywords text[] DEFAULT NULL::text[], min_price_cents bigint DEFAULT NULL::bigint, max_price_cents bigint DEFAULT NULL::bigint, platform_group_filter text DEFAULT NULL::text)
 RETURNS SETOF pricecharting_catalog
 LANGUAGE plpgsql
AS $function$
declare
    plan_json json;
    estimated_rows bigint;
    tokens text[];
    tok text;
    token_where text := '';
    token_score text := '0';
    filter_where text;
    is_browse boolean;
    browse_keys text[];
    browse_key_column text;
    page_sql text := '';
    order_by_clause text;
begin
    is_browse := coalesce(trim(search_query), '') = '';

    if is_browse then
        -- One equality + ordered range scan per keyword. Each subquery is
        -- served entirely by a browse index (equality on the leading
        -- expression/column, then the exact price sort), capped at
        -- limit+offset rows, so no scan ever reads past the page being
        -- fetched. The price bounds sit inside each subquery: with the
        -- leading column pinned they narrow the same index range instead
        -- of filtering afterwards.
        if category_keywords is not null then
            browse_key_column := 'public.pricecharting_browse_category(c.category)';
            browse_keys := category_keywords;
        elsif platform_group_filter is not null then
            browse_key_column := 'c.platform_group';
            if platform_group_filter = '__any_platform__' then
                -- "Any video game platform" has no single key to pin, so
                -- it unions all of them. Mirrors compute_platform_group()
                -- (20260820_add_platform_group_step1_schema.sql); a group
                -- added there must be added here or any-platform browse
                -- silently omits it.
                browse_keys := array['atari','nintendo','pc','playstation','retro-other','sega','xbox'];
            else
                browse_keys := array[platform_group_filter];
            end if;
        else
            -- Browse with nothing to browse would sweep the catalog.
            return;
        end if;

        foreach tok in array browse_keys loop
            page_sql := page_sql
                || case when page_sql = '' then '' else ' union all ' end
                || format(
                    '(select c.pricecharting_id, c.loose_price_cents
                      from public.pricecharting_catalog c
                      where %s = %L
                        and c.loose_price_cents is not null
                        and (%L::bigint is null or c.loose_price_cents >= %L::bigint)
                        and (%L::bigint is null or c.loose_price_cents <= %L::bigint)
                      order by c.loose_price_cents desc, c.pricecharting_id asc
                      limit %s)',
                    browse_key_column, tok,
                    min_price_cents, min_price_cents,
                    max_price_cents, max_price_cents,
                    result_limit + result_offset
                );
        end loop;

        -- The union is ordered and paged as a whole, then only the ~20
        -- surviving ids are joined back for their full rows -- selecting
        -- c.* inside the subqueries would drag 1KB-wide rows through the
        -- sort. The outer ORDER BY repeats the inner one because a join
        -- does not preserve row order.
        return query execute format(
            'select c.* from public.pricecharting_catalog c
             join (
                 select u.pricecharting_id, u.loose_price_cents
                 from (%s) u
                 order by u.loose_price_cents desc, u.pricecharting_id asc
                 limit %L offset %L
             ) page on page.pricecharting_id = c.pricecharting_id
             order by page.loose_price_cents desc, page.pricecharting_id asc',
            page_sql, result_limit, result_offset
        );
        return;
    end if;

    tokens := array_remove(regexp_split_to_array(lower(trim(search_query)), '\s+'), '');
    if tokens is null or array_length(tokens, 1) is null then
        return;
    end if;

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
    token_where := substring(token_where from 6);

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
$function$
