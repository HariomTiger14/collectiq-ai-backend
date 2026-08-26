-- Normalized browse category for pricecharting_catalog rows.
--
-- Discover's category browse needs "the most valuable rows in a category,
-- at any page depth" to be an index lookup. The category column can't
-- back one directly: it is messy free text ("YuGiOh Cards Yugioh Cards",
-- "Comic Books The Stand: Captain Trips", "One Piece. Funko Shop
-- Exclusive. Limited Edition"), matched today by ilike against the
-- keyword lists in PRICECHARTING_CATEGORY_GROUPS /
-- PRICECHARTING_SUBCATEGORY_GROUPS (app/services/pricing/
-- catalog_search_service.py). An ilike filter can only be applied WHILE
-- walking some other index, and walking the global price index was
-- measured at 80s for Yu-Gi-Oh at offset 200: its 220 matches hide among
-- the ~142k priciest rows of a 12M-row catalog, each needing a heap
-- visibility check on a table whose repricing churn keeps the visibility
-- map permanently dirty.
--
-- This function collapses that free text to the canonical keyword it
-- matches, so an expression index over it turns browse into btree
-- equality. Return values are EXACTLY the keyword strings the service
-- layer already sends as category_keywords -- that is the contract:
-- pricecharting_browse_category(c.category) = kw must hold precisely for
-- the rows `c.category ilike '%kw%'` selects today, keyword lists and
-- this CASE ladder kept in sync by hand (both sides carry a comment).
--
-- Where a category matches several keywords the ladder picks one, and
-- its order is deliberate:
--   * Comic first: comic book categories embed their series title, so
--     "Comic Books Magic ..." must stay a comic, not leak into Magic.
--   * Funko before the card games: "One Piece. Funko Shop Exclusive" is
--     a figure, not a card.
-- A multi-match row therefore browses under exactly one category where
-- the ilike OR showed it under each; searching WITH a filter still uses
-- the ilike OR, so nothing is unreachable.
--
-- IMMUTABLE is required for the expression index and holds: pure
-- pattern tests against constants, no catalog or locale lookups beyond
-- the database's fixed collation.

CREATE OR REPLACE FUNCTION public.pricecharting_browse_category(category text)
RETURNS text
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT CASE
        WHEN category ILIKE '%comic%'      THEN 'Comic'
        WHEN category ILIKE '%funko%'      THEN 'Funko'
        WHEN category ILIKE '%lego%'       THEN 'Lego'
        WHEN category ILIKE '%coin%'       THEN 'Coin'
        WHEN category ILIKE '%baseball%'   THEN 'Baseball'
        WHEN category ILIKE '%basketball%' THEN 'Basketball'
        WHEN category ILIKE '%football%'   THEN 'Football'
        WHEN category ILIKE '%hockey%'     THEN 'Hockey'
        WHEN category ILIKE '%soccer%'     THEN 'Soccer'
        WHEN category ILIKE '%magic%'      THEN 'Magic'
        WHEN category ILIKE '%pokemon%'    THEN 'Pokemon'
        WHEN category ILIKE '%yugioh%'     THEN 'Yugioh'
        WHEN category ILIKE '%lorcana%'    THEN 'Lorcana'
        WHEN category ILIKE '%one piece%'  THEN 'One Piece'
        ELSE NULL
    END
$$;
