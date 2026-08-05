# PackLox Global Catalog — Architecture Proposal

**Status:** design proposal, not implemented. No schema or code changes have been
made yet. This doc is the reviewable plan before any of that happens.

## Problem

Discover's catalog search (`CatalogSearchService`, `/api/pricing/catalog/search`)
only queries `pricecharting_catalog`, which is populated exclusively by the
5 PriceCharting CSV imports: `video_games`, `pokemon`, `magic`, `yugioh`,
`one_piece` (see `docs/PRICECHARTING_CATALOG_IMPORT.md`). That means Discover
can only ever surface Cards and Video Games — Sneakers, Comics, Coins, LEGO,
Funko, Sports have no real searchable data, even though the app treats them
as supported categories (`_supportedCategories` in `home_page.dart`,
`category_visual.dart`).

Meanwhile, when a user scans an item, `/analyze` already prices it live
through a provider chosen by category — `KicksDBPricingProvider` for
sneakers/streetwear, `TCGPlayerPricingProvider`/`PriceChartingPricingProvider`
for cards, `EbayPricingProvider` as a general fallback (`provider_factory.py`).
That real pricing data is used once, shown to the one user who scanned it,
and then never becomes searchable in Discover — it's a completely separate
system from the catalog.

## Key finding: we already have the raw material for this

`app/services/pricing/shared_cache_repository.py` (`SharedPricingCacheRepository`)
already writes **every** live-priced scan result into a `pricing_cache_entries`
table — this exists today, in production, for a different reason (avoiding
repeat paid API calls), but it happens to be almost exactly the staging data
a global catalog needs:

- Deduped already: `cache_key = sha256(currency|normalized_identity)`, upserted
  with `resolution=merge-duplicates` — repeat scans of "the same" item
  (by category/title/brand/set/series/year/cardNumber/character/edition/condition)
  collapse into one row instead of creating duplicates.
- Has a built-in corroboration signal: `increment_pricing_cache_hit` fires on
  every cache **hit** (i.e. every time a *different* scan resolves to the same
  `normalized_identity`). A row that's been hit several times means several
  independent scans converged on the same product — a much stronger signal
  than one AI read + one live price quote.
- Already anonymized: the row shape (`_row_from_pricing` in
  `shared_cache_repository.py`) has no `user_id` or `item_id` — just
  category, normalized_identity, valuation fields, provider, confidence,
  evidence. It is safe to read from for a shared catalog without touching
  ownership data. (Contrast with `scan_analysis_events`, which *does* carry
  `user_id`/`item_id` — that table is for the admin scan-failure queue, not a
  source for this.)
- Already has category-aware freshness policy: `cache_policy.py` sets TTL by
  category — sneakers/cards at 24h ("fast-moving"), video games/comics/lego/
  funko/coins at 72h ("stable"). That's the exact staleness signal a
  promoted catalog row needs, and it's already tuned per category.

**Conclusion: this isn't "build a new pipeline that writes into the catalog
at scan time." It's "build a promotion job that reads already-corroborated
rows out of `pricing_cache_entries` into `pricecharting_catalog`."** No
changes to `/analyze` or the scan path are needed — it already logs
everything we need, today.

One gap: `pricing_cache_entries` and `increment_pricing_cache_hit` have no
tracked migration in `database/migrations/` — they exist directly in
Supabase. Before implementing, confirm the live column names/types match
what `shared_cache_repository.py` assumes (`cache_key`, `category`,
`normalized_identity`, `value_aud`, `low_estimate_aud`, `high_estimate_aud`,
`pricing_provider`, `confidence_score`, `valuation_status`, `checked_at`,
`expires_at`, `evidence_json`, and a hit-count column) and add a migration
file for it so it's no longer undocumented infrastructure.

## Proposed schema changes

Additive only — nothing about the existing PriceCharting import path changes.

```sql
-- New migration: extend pricecharting_catalog for multi-source rows
alter table public.pricecharting_catalog
    add column if not exists source_provider text not null default 'pricecharting_import',
    add column if not exists source_kind text not null default 'bulk_import',
    add column if not exists market_value_cents integer,
    add column if not exists low_estimate_cents integer,
    add column if not exists high_estimate_cents integer,
    add column if not exists promoted_from_cache_key text,
    add column if not exists verified_at timestamptz;

create index if not exists pricecharting_catalog_source_kind_idx
    on public.pricecharting_catalog (source_kind);
```

- `source_provider`: `pricecharting_import` (existing rows, unaffected) /
  `kicksdb` / `tcgplayer` / `ebay` — whatever `pricing_provider` says on the
  cache row being promoted.
- `source_kind`: `bulk_import` (existing rows) / `scan_derived` (new).
- `market_value_cents` / `low_estimate_cents` / `high_estimate_cents`:
  provider-neutral price fields every source can populate. PriceCharting's
  existing `loose/cib/new/graded_price_cents` stay as-is for its own rows;
  scan-derived rows populate only the generic three.
- `pricecharting_id` (the primary key) stays required — for scan-derived rows,
  synthesize it as `{provider}:{cache_key}` so it can't collide with a real
  PriceCharting product id.
- `raw_payload` (already exists, jsonb) stores the full cache row
  (`evidence_json`, `confidence_score`, etc.) for scan-derived rows — same
  role it already plays for the raw CSV row on imported ones.

No new table needed for staging — `pricing_cache_entries` already *is* the
staging table. The only new table is job bookkeeping, and
`admin_import_jobs` (used by `AdminImportJobService`) already covers that
shape generically enough to reuse as-is.

## Promotion job

New service, same shape as `scripts/import_pricecharting_catalog.py` /
`CatalogSearchService` — reuses `SupabaseCatalogClient.upsert_rows` and
`sync_scd2_history_rows` against `pricecharting_catalog`/
`pricecharting_catalog_history` unchanged, just fed by a different query:

```python
# app/services/pricing/promote_scan_derived_catalog.py (proposed)

def find_promotion_candidates(min_hit_count: int = 2) -> list[dict]:
    # SELECT * FROM pricing_cache_entries
    # WHERE hit_count >= :min_hit_count
    #   AND valuation_status = 'market_estimated'
    #   AND expires_at > now()          -- still within its category's TTL, i.e. not stale
    #   AND cache_key NOT IN (SELECT promoted_from_cache_key FROM pricecharting_catalog
    #                         WHERE promoted_from_cache_key IS NOT NULL)
    ...

def to_promoted_catalog_row(cache_row: dict) -> dict:
    # maps pricing_cache_entries columns -> pricecharting_catalog columns,
    # mirrors to_catalog_row() in import_pricecharting_catalog.py
    ...
```

Exposed the same way the existing import is:

```
POST /admin/catalog/promote-scan-derived?dryRun=true&minHitCount=2
```

mirroring `POST /admin/pricecharting/import` — dry-run first, same
`AdminImportJobService` job tracking, same `X-Admin-Token` auth. Re-running
it is safe/idempotent: a cache row already promoted is excluded via
`promoted_from_cache_key`, and if its underlying price changed since,
`content_hash` (already used for change-detection on every existing row)
naturally picks that up as an update, not a duplicate.

**On auto-promote vs. manual review:** I'd start with an automatic threshold
(`min_hit_count >= 2`, i.e. at least one independent corroborating scan
beyond the first) rather than a manual admin-review queue. Reasoning: the
corroboration signal is already fairly strong (two independent AI
recognitions + live provider prices agreeing on the same normalized
identity), and a manual-approve-every-item queue won't scale once scan
volume grows. If early results show junk slipping through (bad AI title
normalization creating false "corroboration"), raise the threshold or add a
lightweight admin review screen at that point — cheaper to add reactively
than to build a review UI pre-emptively for a problem that might not
materialize.

## Search-side changes

`CatalogSearchService._rank_rows`/`_match_score` in
`catalog_search_service.py` should weight `source_kind = 'bulk_import'`
above `scan_derived` when scores are close, so a single-provider scan guess
never outranks curated PriceCharting data. Minor addition to the existing
scoring function, not a rewrite.

For staleness: since promoted rows carry `verified_at`, results whose
`verified_at` is older than that category's `cache_policy.py` TTL should
either be excluded from search or surfaced with a "price may be outdated"
signal — reusing the exact TTL table that already exists, not a new policy.

## What this fixes, concretely

Once shipped, "Nike Air Force 1" becomes searchable in Discover the first
time enough real users scan Nike Air Force 1 shoes and KicksDB prices them —
without any admin CSV import ever needing to cover sneakers. Same for
Comics/Coins/LEGO/Funko once real scans accumulate. The catalog grows from
actual usage instead of staying capped at 5 manually-imported CSVs.

## Rollout phases

1. **Schema**: run the additive migration above; add the missing
   `pricing_cache_entries` migration file (documenting what's already live).
2. **Promotion job**: `promote_scan_derived_catalog.py` service +
   `POST /admin/catalog/promote-scan-derived` endpoint, dry-run first against
   real SIT data to sanity-check candidate quality before writing anything.
3. **Search ranking + staleness**: update `_rank_rows`/result filtering in
   `catalog_search_service.py`.
4. **Scheduling**: a Render cron (same pattern as
   `collectiq-pricecharting-refresh-sit`) to run the promotion job daily.
5. **Optional later**: admin portal screen listing recent promotions /
   pending-but-below-threshold candidates, only if phase 2's automatic
   threshold turns out to need human oversight in practice.

Each phase ships independent value — phase 2 alone (even run manually via
curl, no cron yet) already starts filling in Sneakers/Comics/Coins/etc.

## Open questions before implementation

- Confirm `pricing_cache_entries`' actual live column names/types in
  Supabase (no tracked migration exists — verify against what
  `shared_cache_repository.py` assumes, especially the hit-count column
  name).
- Decide the `min_hit_count` starting threshold (proposed: 2).
- Decide whether scan-derived rows should ever get their own
  `pricecharting_catalog_history` SCD2 tracking, or whether that's overkill
  for data that's inherently more volatile than a bulk import (leaning
  toward: yes, reuse it — it's already generic per-row, not
  PriceCharting-specific).
