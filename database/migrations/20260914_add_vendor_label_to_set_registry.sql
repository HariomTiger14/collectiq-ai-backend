-- The vendor's own name for a set, stored beside ours.
--
-- 2026-09-13: the sportscardspro rotation jammed for 90 minutes, re-downloading
-- the same 350 uids every ten minutes. One family in the CSV matched no
-- requested set:
--
--   registry set_name : '2015 Topps Platinum Autograph Rookies'   (G9533)
--   vendor console-name: 'football cards 2015 topps platinum autographed rookie
--                         refractor'
--
-- The vendor had renamed the set. Our name came from an HTML crawl on
-- 2026-08-15 and never moved. validate_csv_families is fail-closed, so one
-- unmatched family refuses the whole file, the sets are never stamped, and
-- claim_due_sets (tier3_refreshed_at NULLS FIRST) hands back the identical 350
-- on the next tick.
--
-- set_name is NOT overwritten, deliberately. G58581 is why: our row is called
-- '1993 Classic' and the vendor calls it '1993 classic four sport'. A bare
-- '1993 classic' really does exist -- as G218 (basketball), G15788 (football)
-- and G53812 (baseball) -- just not in hockey, which is where our row sits. Our
-- shorter name is not wrong, only less specific, and it is what search and
-- display have been using. Both names are true; the guard needs to accept
-- either, which means keeping both.
--
-- Nullable with no default: a row without a label simply contributes nothing
-- extra to the expected set, which is exactly today's behaviour. Populated by
-- scripts/fill_sports_vendor_labels.py from /consoles-autocomplete, matched on
-- console_uid. Measured coverage at the time of writing: 35,647 of 35,649
-- claimable sportscardspro uids appear in autocomplete.

alter table public.pricecharting_set_registry
  add column if not exists vendor_label text;

comment on column public.pricecharting_set_registry.vendor_label is
  'The vendor''s own name for this set, from /consoles-autocomplete, matched on '
  'console_uid. Read by the downloader to widen the expected console-name '
  'families for a batch. Never a substitute for set_name: both are kept because '
  'either can be the label a CSV comes back under.';

-- Read by console_uid for a batch's 350 uids at a time, alongside set_name.
create index if not exists pricecharting_set_registry_uid_label_idx
  on public.pricecharting_set_registry (console_uid)
  where vendor_label is not null;
