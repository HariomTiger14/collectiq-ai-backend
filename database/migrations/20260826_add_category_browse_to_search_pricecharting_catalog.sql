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
--   * token_where becomes 'true' -- there are no tokens to match, so the
--     category/price/platform predicates do all the work;
--   * ordering switches to priced-first, most-valuable, then alphabetical.
--     Relevance tiers are meaningless with no search term, and a large
--     share of the catalog has no price at all, so an unpriced row is the
--     least useful thing to open a category with;
--   * the category predicate is built as direct OR'd ilike terms rather
--     than exists(select ... from unnest(...)). That subquery form is a
--     correlated scan the planner cannot serve from
--     pricecharting_catalog_category_trgm_idx; with no token_where to
--     narrow first it degenerated into a sequential scan of the whole
--     catalog and did not finish inside two minutes when measured live.
--     The direct form uses the index and returns in ~1.5s;
--   * browsing always takes the ordered path, since the unordered fast
--     path exists only to skip ranking a huge token match -- with no
--     query there is nothing to skip, and skipping the order would hand
--     back an arbitrary slice of the category.
--
-- Search behaviour with a non-empty query is unchanged, and no existing
-- caller passes an empty query (the service layer rejected it), so this
-- is additive.

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
    cat_where text;
    order_by_clause text;
begin
    is_browse := coalesce(trim(search_query), '') = '';
    tokens := array_remove(regexp_split_to_array(lower(trim(search_query)), '\s+'), '');
    if not is_browse and (tokens is null or array_length(tokens, 1) is null) then
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
    -- Browsing has no tokens, so there is nothing to strip and nothing to
    -- match on -- 'true' lets filter_where (the category/price/platform
    -- predicates) do all the work on its own.
    if is_browse then
        token_where := 'true';
    else
        token_where := substring(token_where from 6);
    end if;

    -- Browsing needs an index-friendly category predicate. The
    -- exists(select ... from unnest(...)) form below is a correlated
    -- subquery the planner cannot serve from the trigram index, so with no
    -- token_where to narrow first it degenerates into a sequential scan of
    -- the whole catalog -- measured live, that did not finish inside two
    -- minutes. OR-ing the keywords directly lets the same query use
    -- pricecharting_catalog_category_trgm_idx and return in ~1.5s.
    if is_browse and category_keywords is not null then
        cat_where := '';
        foreach tok in array category_keywords loop
            cat_where := cat_where || format(' or c.category ilike %L', '%' || tok || '%');
        end loop;
        cat_where := '(' || substring(cat_where from 5) || ')'
            || ' and c.loose_price_cents is not null';
    else
        cat_where := null;
    end if;

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

    if cat_where is not null then
        -- Swap the unnest-based category test for the direct OR'd form;
        -- price/platform predicates below it are untouched.
        filter_where := cat_where || substring(filter_where from position('
          and (' in filter_where));
    end if;

    execute format(
        'explain (format json) select 1 from public.pricecharting_catalog c where %s and %s',
        token_where, filter_where
    ) into plan_json;

    estimated_rows := (plan_json->0->'Plan'->>'Plan Rows')::bigint;

    -- Relevance tiers are meaningless with no search term, so browsing
    -- gets its own order: most valuable first, with the id as a tiebreak
    -- so paging is stable between requests.
    --
    -- The shape here is chosen to be servable straight from
    -- pricecharting_catalog_loose_price_cents_idx, which matters a lot on
    -- a 12M-row/16GB table: ranking a whole category by price means a
    -- bitmap heap scan over every page that category touches, and for
    -- Comic Books (~276k rows) that measured 45s. Walking the price index
    -- backwards and stopping at the first 20 matches instead measured 5ms.
    -- Two details keep the planner on that path: browsing is restricted to
    -- priced rows (an unpriced row is the least useful thing to open a
    -- category with anyway, and `is null` as a leading sort key forces a
    -- sort), and product_name is left out of the sort keys so ties resolve
    -- by an incremental sort over a handful of rows rather than a full one.
    if is_browse then
        order_by_clause := 'c.loose_price_cents desc, c.pricecharting_id asc';
    else
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
    end if;

    -- Browsing always takes the ordered path. The unordered fast path
    -- exists to avoid ranking a huge token match, but with no query there
    -- is no ranking to skip -- and skipping the order here would hand back
    -- an arbitrary slice of the category (Comics alone is ~276k rows),
    -- which is worse than the sort costs. limit is <= 50, so Postgres does
    -- a cheap top-N sort backed by the loose_price_cents index.
    if is_browse or estimated_rows <= broad_query_row_threshold then
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

