-- Tokenizes search_pricecharting_catalog()'s text matching: previously the
-- whole query was matched as ONE literal phrase (e.g. "god of war 4" had
-- to appear verbatim somewhere), so any query with an extra word not
-- literally in the title/set/category returned zero results -- confirmed
-- live: "god of war 4" and "god of war 2018" both returned nothing for a
-- real, correctly-catalogued PS4 game titled just "God of War" (console_
-- name "Playstation 4" -- the "4" the user expected to match a sequel
-- number instead happens to match the platform name, which tokenized
-- matching now picks up correctly).
--
-- Each word in the query is now matched independently (still against the
-- same 5 columns as before, OR'd), and ALL words must match somewhere
-- (AND'd across words) -- so "god of war 4" now matches because "god"/
-- "of"/"war" hit product_name and "4" hits console_name ("Playstation
-- 4"), even though no single column contains the literal phrase.
--
-- A naive first attempt at this (unnest() + NOT EXISTS, tokens compared
-- per-row via a correlated subquery) was tested live and rejected: it
-- forced a full sequential scan (confirmed via EXPLAIN ANALYZE -- Nested
-- Loop Anti Join, 232s, 11.7M rows touched) because Postgres can't push a
-- per-row dynamic pattern through the trigram indexes. This version
-- instead builds N static, literal AND'd OR-blocks via format()/%L (one
-- per token) -- confirmed live via EXPLAIN ANALYZE this uses proper
-- Bitmap Index/Heap Scans against the existing trigram indexes, ~200ms
-- for a 4-token query, not a sequential scan.
--
-- A second live-confirmed issue in the first version of this migration:
-- once a query doesn't match as one literal phrase, EVERY matching row
-- falls to the same lowest relevance tier (the old CASE was phrase-based),
-- so the actual best match tied with dozens of coincidental matches (e.g.
-- a UPC that happens to contain "4") and lost the alphabetical tie-break
-- to earlier product names -- "God of War" (id 45800) didn't appear in
-- the top 50 results for "god of war 4" despite genuinely matching all
-- four tokens. Fixed by adding a token-match-count score (weighted:
-- product_name hits count double a normalized_identity-only hit) as a
-- secondary sort key, ahead of the alphabetical fallback.
--
-- A third live-confirmed issue: console_name/category phrase-match tiers
-- (55 in an earlier version) outranked the token score entirely -- a Funko
-- figure whose category is literally "God of War 4" (an informal franchise
-- tag) still beat the real game, because tier is checked before token
-- score and a coarse category-phrase hit always won regardless of how the
-- game matched on product_name. Removed: only product_name/upc/
-- normalized_identity (fields that actually identify a specific item)
-- earn tier priority now; console_name/category still count for WHERE-
-- clause recall (unchanged), just not ranking priority.
--
-- Known residual limitation, not fixed here: a single-character/digit
-- token (e.g. the "4" in "god of war 4") is inherently ambiguous -- it
-- matches just as easily inside an unrelated card number ("#104") as a
-- real platform name ("Playstation 4"), so a handful of genuinely-
-- plausible alternate matches (a Lorcana/Magic card literally named
-- "...God of War...#104") can still narrowly outscore the intended game.
-- The core bug (zero results for any multi-word query not matching
-- verbatim) is fixed -- perfect disambiguation of bare single-digit tokens
-- is a separate, harder problem not chased further here.
--
-- Same parameter signature as before (no new params) -- this only changes
-- the function body, so no Python-layer changes are needed, and no
-- DROP FUNCTION/overload-ambiguity concern like the two prior migrations
-- to this function.
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
          and (%L::text is null or c.platform_group = %L::text)',
        category_keywords, category_keywords,
        min_price_cents, min_price_cents,
        max_price_cents, max_price_cents,
        platform_group_filter, platform_group_filter
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
