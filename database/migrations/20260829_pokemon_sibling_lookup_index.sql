-- Fix for _has_sibling_pokemon_rows' query plan: the sibling-suppression
-- lookup (console_name = X AND product_name ILIKE '%#<n>') had no usable
-- index for its console_name equality -- only trigram indexes exist on
-- this table -- so the planner fell back to the product_name trigram
-- scan, which explodes on short card numbers ('%#27' -> 56K candidate
-- rows, measured 13s; '%#4' worse). Latent since the original 5-set
-- TCGplayer enrichment; on the hot path for every Pokemon detail view
-- now that TCGdex covers all sets.
--
-- Partial btree over Pokemon consoles only (~93K of the table's ~12M
-- rows, single-digit MB): equality on the leading column narrows to one
-- set's few hundred rows, and product_name being in the index lets the
-- suffix filter run without heap fetches.
-- Run with CONCURRENTLY outside a transaction, and with
-- statement_timeout=0: the build scans all ~12M rows even though the
-- resulting partial index is tiny, which exceeds the default 2-minute
-- timeout (a timed-out CONCURRENTLY build leaves an INVALID index behind
-- that must be dropped before retrying). Applied to production
-- 2026-08-29.
--
-- NOTE: the planner still refuses to combine this with a product_name
-- ILIKE '%#<n>' filter (its pattern-selectivity estimate is ~50x off, so
-- it picks the product_name trigram scan; '%#27' measured 13s). The
-- reader (_has_sibling_pokemon_rows) therefore queries console_name
-- equality ONLY and does the suffix check client-side.

create index concurrently if not exists pricecharting_catalog_pokemon_sibling_idx
    on public.pricecharting_catalog (console_name, product_name)
    where console_name like 'Pokemon%';
