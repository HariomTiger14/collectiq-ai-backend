-- Reads move to pricecharting_current_price.
--
-- NOT YET APPLIED. Requires 20260909_create_pricecharting_current_price.sql
-- (table + backfill + at least the category browse index) to be live first.
--
-- Browse: the inner scan now runs against pricecharting_current_price, whose
-- browse indexes have the same shape the catalog's had -- equality on the
-- browse key, then loose_price_cents DESC, stopping at limit+offset. It is NOT
-- a catalog scan with the price overlaid afterwards; that would sort a whole
-- category. Only the ~20 ids that survive the page are joined back to the
-- catalog for identity.
--
-- Keyword: the catalog still leads, because trigram narrowing is what makes
-- that path fast. current_price is joined after, and the price filters read
-- the joined row. The planner-estimate probe joins too -- estimating a
-- different query than the one that runs is how the adaptive threshold starts
-- choosing the wrong branch.
--
-- The return type stays SETOF pricecharting_catalog, so no caller changes.
-- jsonb_populate_record keeps every catalog column and replaces only the
-- price fields, which means it is column-order independent -- it will not
-- break the next time a column is added to the catalog.
--
-- LEFT JOIN, not JOIN: an id with no current_price row must return with null
-- prices rather than vanish or 500. The overlay is applied even then --
-- deliberately. Letting the catalog's own cents show through would serve
-- silently stale prices once PR 4 stops maintaining them, and a wrong price is
-- worse than an absent one.

CREATE OR REPLACE FUNCTION public.pricecharting_price_overlay(
    loose_price_cents       integer,
    cib_price_cents         integer,
    new_price_cents         integer,
    graded_price_cents      integer,
    box_only_price_cents    integer,
    manual_only_price_cents integer,
    currency                text
)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
-- NOT STRICT, and that is load-bearing: a left-join miss passes nulls, and a
-- STRICT function would return NULL, which jsonb_populate_record treats as
-- "change nothing" -- exactly the stale-price fallback this avoids.
AS $$
    SELECT jsonb_build_object(
        'loose_price_cents',       loose_price_cents,
        'cib_price_cents',         cib_price_cents,
        'new_price_cents',         new_price_cents,
        'graded_price_cents',      graded_price_cents,
        'box_only_price_cents',    box_only_price_cents,
        'manual_only_price_cents', manual_only_price_cents,
        'currency',                coalesce(currency, 'USD')
    )
$$;

COMMENT ON FUNCTION public.pricecharting_price_overlay IS
    'The price fields of a pricecharting_catalog row, as jsonb, for '
    'jsonb_populate_record. Scalar arguments rather than the composite so a '
    'left-join miss is unambiguously seven nulls.';

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
            browse_key_column := 'public.pricecharting_browse_category(p.category)';
            browse_keys := category_keywords;
        elsif platform_group_filter is not null then
            browse_key_column := 'p.platform_group';
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
                    '(select p.pricecharting_id, p.loose_price_cents
                      from public.pricecharting_current_price p
                      where %s = %L
                        and p.loose_price_cents is not null
                        and (%L::bigint is null or p.loose_price_cents >= %L::bigint)
                        and (%L::bigint is null or p.loose_price_cents <= %L::bigint)
                      order by p.loose_price_cents desc, p.pricecharting_id asc
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
            'select (jsonb_populate_record(c, public.pricecharting_price_overlay(cp.loose_price_cents, cp.cib_price_cents, cp.new_price_cents, cp.graded_price_cents, cp.box_only_price_cents, cp.manual_only_price_cents, cp.currency))).*
             from public.pricecharting_catalog c
             join (
                 select u.pricecharting_id, u.loose_price_cents
                 from (%s) u
                 order by u.loose_price_cents desc, u.pricecharting_id asc
                 limit %L offset %L
             ) page on page.pricecharting_id = c.pricecharting_id
             left join public.pricecharting_current_price cp
                    on cp.pricecharting_id = c.pricecharting_id
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
          and (%L::bigint is null or cp.loose_price_cents >= %L::bigint)
          and (%L::bigint is null or cp.loose_price_cents <= %L::bigint)
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
        'explain (format json) select 1 from public.pricecharting_catalog c
           left join public.pricecharting_current_price cp
                  on cp.pricecharting_id = c.pricecharting_id
          where %s and %s',
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
            'select (jsonb_populate_record(c, public.pricecharting_price_overlay(cp.loose_price_cents, cp.cib_price_cents, cp.new_price_cents, cp.graded_price_cents, cp.box_only_price_cents, cp.manual_only_price_cents, cp.currency))).*
             from public.pricecharting_catalog c
             left join public.pricecharting_current_price cp
                    on cp.pricecharting_id = c.pricecharting_id
             where %s and %s
             order by %s
             limit %L offset %L',
            token_where, filter_where, order_by_clause, result_limit, result_offset
        );
    else
        return query execute format(
            'select (jsonb_populate_record(c, public.pricecharting_price_overlay(cp.loose_price_cents, cp.cib_price_cents, cp.new_price_cents, cp.graded_price_cents, cp.box_only_price_cents, cp.manual_only_price_cents, cp.currency))).*
             from public.pricecharting_catalog c
             left join public.pricecharting_current_price cp
                    on cp.pricecharting_id = c.pricecharting_id
             where %s and %s
             limit %L offset %L',
            token_where, filter_where, result_limit, result_offset
        );
    end if;
end;
$function$

