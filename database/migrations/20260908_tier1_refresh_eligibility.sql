-- Tier-1 (small-sets-refresh) eligibility pruning.
--
-- small-sets-refresh refreshes a set by TEXT SEARCH against /api/products,
-- which only works when the search returns a complete, unambiguous set under
-- the vendor's hard 100-result cap. Plenty of sets can never satisfy that:
-- they are too large, or the fuzzy match resolves to a different set
-- entirely. Verified live 2026-09-07, searching "2023 Panini Certified 2023"
-- returned exactly 100 products whose console-name was
-- "Football Cards 2023 Panini Select" -- a different set, truncated at the
-- cap.
--
-- The job already skips those correctly, but it re-checks them every single
-- run: an /api/products call, a 1.2s pace wait, and a scary-looking log line
-- per set per hour, forever. These columns let it stop asking.
--
-- This is a TIER-1 signal only. Such a set is still perfectly serviceable by
-- tier-3, which fetches by console_uid + CSV and does not care about the
-- search cap -- so nothing here touches tier3_* columns, and marking a set
-- tier-1-ineligible is explicitly NOT marking it failed.

ALTER TABLE public.pricecharting_set_registry
    ADD COLUMN IF NOT EXISTS tier1_refresh_eligible boolean NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS tier1_ineligible_reason text,
    ADD COLUMN IF NOT EXISTS tier1_ineligible_at timestamptz,
    ADD COLUMN IF NOT EXISTS tier1_recheck_after timestamptz,
    -- Consecutive "might be transient" misses (404 / empty). Deterministic
    -- reasons do not use this: a set over the 100-result cap will be over it
    -- again tomorrow, so waiting for three identical answers only wastes
    -- three days of API calls to learn the same thing.
    ADD COLUMN IF NOT EXISTS tier1_miss_count integer NOT NULL DEFAULT 0;

COMMENT ON COLUMN public.pricecharting_set_registry.tier1_refresh_eligible IS
    'False when /api/products cannot serve this set safely (over the 100 cap, '
    'or the search resolves to a different set). Excluded from '
    'small-sets-refresh until tier1_recheck_after. Says nothing about tier-3, '
    'which fetches by console_uid + CSV and is unaffected by the search cap.';
COMMENT ON COLUMN public.pricecharting_set_registry.tier1_ineligible_reason IS
    'One of: api_404, api_empty, api_capped_100, api_ambiguous_or_wrong_family.';
COMMENT ON COLUMN public.pricecharting_set_registry.tier1_miss_count IS
    'Consecutive transient-capable misses (404/empty). Reset to 0 on any '
    'successful tier-1 refresh. Deterministic reasons mark ineligible at once '
    'and do not touch this.';

-- Candidate selection filters on eligibility first, then staleness. Partial
-- index because the eligible rows are the ones the query wants and, in the
-- steady state, the ineligible set is the smaller half of the table.
CREATE INDEX IF NOT EXISTS pricecharting_set_registry_tier1_candidates_idx
    ON public.pricecharting_set_registry (tier1_refreshed_at NULLS FIRST, registry_id)
    WHERE last_fetch_status = 'success'
      AND tier1_refresh_eligible = true;

-- Rows waiting on a recheck window, so the sweeper that restores eligibility
-- does not scan the whole table.
CREATE INDEX IF NOT EXISTS pricecharting_set_registry_tier1_recheck_idx
    ON public.pricecharting_set_registry (tier1_recheck_after)
    WHERE tier1_refresh_eligible = false;
