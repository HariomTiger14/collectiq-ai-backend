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

**Verified 2026-08-05** against the live SIT schema (no tracked migration
existed for this table before now — added in
`20260805_document_pricing_cache_entries.sql`). Confirmed: `cache_key` is
the primary key (required — the app upserts with `on_conflict=cache_key`),
`hit_count integer not null default 0` (increments only on a cache **hit**,
i.e. a second-or-later scan matching the same `normalized_identity` — the
row that creates the entry never touches it, so `hit_count >= 1` already
means at least one independent corroborating scan beyond the original, not
`>= 2` as an earlier draft of this doc assumed), `expires_at timestamptz not
null` (always explicitly set, no default).

### Currency: promote from `original_price`/`original_currency`, not `value_aud`

`pricing_cache_entries.value_aud`/`low_estimate_aud`/`high_estimate_aud` are
**display-currency-converted** — normalized to AUD (or whatever
`display_currency` was for that request) via `convert_pricing_result()`,
frozen at whatever FX rate applied on the day of that scan. Meanwhile
`pricecharting_catalog` stores **native provider currency** per row today
(PriceCharting rows are hardcoded `currency: "USD"` in
`to_catalog_row()`, no conversion) — Discover's UI displays that currency
directly (e.g. "USD $161").

Promoting `value_aud` would make every scan-derived row a currency
conversion frozen at scan time, inconsistent with existing native-currency
rows and drifting from real market price as FX rates move. The fix: promote
from `original_price`/`original_currency` instead. Verified this is safe —
traced `api_analyze.py:978`, `convert_pricing_result()` always runs
**before** `_shared_pricing_cache.set()` (line 980), and it unconditionally
captures `originalMarketValue`/`originalCurrency` as the pre-conversion,
native-provider values (`currency_conversion.py:30,39`) before doing any FX
math. So `original_price`/`original_currency` on every cache row is
reliably genuine native currency, not an AUD fallback — safe to promote
directly into `market_value_cents`/`currency`.

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
  existing `loose/cib/new/graded_price_cents` stay as-is for its own rows.
  For scan-derived rows: **only `market_value_cents` has a real native-currency
  source today** — `pricing_cache_entries.original_price` (paired with
  `original_currency` → `currency`). `low_estimate_cents`/`high_estimate_cents`
  have **no native-currency equivalent stored anywhere currently** —
  `_row_from_pricing()` in `shared_cache_repository.py` only ever persists
  `original_price`, never an original low/high (even though the
  `PricingResult` dataclass itself already has `originalLowEstimate`/
  `originalHighEstimate` fields, populated by `convert_pricing_result()` —
  they're just dropped on the floor before the cache write). So: leave
  `low_estimate_cents`/`high_estimate_cents` **null** for scan-derived rows
  at first (not unprecedented — PriceCharting rows already leave tiers like
  `box_only_price_cents` null when unavailable). A follow-up enhancement
  could add `original_low_estimate`/`original_high_estimate` columns to
  `pricing_cache_entries` and start capturing them, to give scan-derived
  Discover results the same Low/High range PriceCharting-backed ones show —
  worth doing, but a separate, smaller change from this proposal.
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

def find_promotion_candidates(min_hit_count: int = 1) -> list[dict]:
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
POST /admin/catalog/promote-scan-derived?dryRun=true&minHitCount=1
```

mirroring `POST /admin/pricecharting/import` — dry-run first, same
`AdminImportJobService` job tracking, same `X-Admin-Token` auth. Re-running
it is safe/idempotent: a cache row already promoted is excluded via
`promoted_from_cache_key`, and if its underlying price changed since,
`content_hash` (already used for change-detection on every existing row)
naturally picks that up as an update, not a duplicate.

**On auto-promote vs. manual review:** I'd start with an automatic threshold
(`min_hit_count >= 1`, i.e. at least one independent corroborating scan
beyond the first — `hit_count` starts at 0 and only increments on a repeat
hit, confirmed against the live schema) rather than a manual admin-review
queue. Reasoning: the
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

1. ✅ **Schema** — additive migration + the missing `pricing_cache_entries`
   migration file, done 2026-08-05.
2. ✅ **Promotion job** — `promote_scan_derived_catalog.py` +
   `POST /admin/catalog/promote-scan-derived`. Verified end-to-end against
   real SIT data 2026-08-05: scanned a real card twice, `hit_count` went
   0→1, dry-run found it, live run promoted it, confirmed searchable in
   Discover with a correct price.
3. ✅ **Search fixes found via that verification, not planned upfront**:
   - `_fetch_rows()`/`_fetch_catalog_row()`/`_pricing_from_row()` never read
     `market_value_cents`/`low_estimate_cents`/`high_estimate_cents` —
     promoted rows searched fine but showed `marketValue: null`. Fixed.
   - `source`/`attribution` were hardcoded to `"PriceCharting"` regardless
     of the row's real provider. Fixed via `source_provider` with a
     graceful fallback to `"PriceCharting"` for unrecognized/multi-provider
     strings.
   - Separate, pre-existing bug also found here (not scan-derived-specific
     — affects all catalog search): `_fetch_rows()` had no `order`, so
     PostgreSQL didn't guarantee which rows landed in the fetch window
     before Python-side ranking ran — a query matching many rows (e.g.
     "Pikachu V", 60+ real variants) could non-deterministically omit the
     true best match. Mitigated with `order=product_name.asc` (deterministic
     fetch, and as a side effect tends to surface short/exact titles before
     longer variants sharing their prefix) plus a wider over-fetch window
     (`limit*5` capped at 200, up from `limit*3` capped at 100). This does
     **not** fully solve relevance ranking for very large result sets — a
     real fix needs DB-side ranking (e.g. the existing
     `pricecharting_catalog_search_idx` GIN/tsvector index via an RPC),
     which is a separate, larger, not-yet-scoped change.
4. ✅ **Scheduling** — `packlox-catalog-promote-scan-derived-sit` Render
   cron added to `render.yaml` (daily, 17:00 UTC, `minHitCount=1`,
   `dryRun=false`). Remember: `render.yaml` is a documentation mirror, not
   the source of truth — this still needs creating by hand in the Render
   dashboard to actually take effect.
5. **Optional later**: admin portal screen listing recent promotions /
   pending-but-below-threshold candidates, only if the automatic
   `minHitCount=1` threshold turns out to need human oversight in practice.

All of phases 1-4 are done and verified against real SIT data as of
2026-08-05 — this is no longer just a design proposal.

## Open questions before implementation

- ~~Confirm `pricing_cache_entries`' actual live column names/types~~ —
  done 2026-08-05, captured in `20260805_document_pricing_cache_entries.sql`.
- Decide the `min_hit_count` starting threshold (proposed: 1).
- Decide whether to extend `pricing_cache_entries` with
  `original_low_estimate`/`original_high_estimate` columns (capturing
  fields the `PricingResult` dataclass already carries but the cache write
  currently drops) so promoted rows can show a real Low/High range instead
  of leaving it null.
- Decide whether scan-derived rows should ever get their own
  `pricecharting_catalog_history` SCD2 tracking, or whether that's overkill
  for data that's inherently more volatile than a bulk import (leaning
  toward: yes, reuse it — it's already generic per-row, not
  PriceCharting-specific).
