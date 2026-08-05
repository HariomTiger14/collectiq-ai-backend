-- Documents pricing_cache_entries as it already exists in production Supabase.
-- This table predates any tracked migration (created ad hoc); this file adds
-- it to source control without changing its live shape. Verified against the
-- live SIT schema on 2026-08-05 via information_schema.columns.
-- Safe to run more than once (create table if not exists / add column if not exists).
--
-- Written and used by app/services/pricing/shared_cache_repository.py
-- (SharedPricingCacheRepository). Every /analyze scan reads/writes this table
-- to avoid repeat paid pricing-provider calls for the same recognized item.
-- See docs/GLOBAL_CATALOG_ARCHITECTURE.md — this table is also the proposed
-- source for scan-derived catalog promotion (hit_count as corroboration
-- signal; cache_key/normalized_identity already dedupe by identity).

create table if not exists public.pricing_cache_entries (
    cache_key text primary key,
    category text not null,
    normalized_identity text not null,
    condition_label text,
    valuation_status text not null,
    value_aud numeric,
    low_estimate_aud numeric,
    high_estimate_aud numeric,
    display_string text,
    valuation_strategy text,
    pricing_provider text,
    attribution_text text,
    confidence_score numeric,
    reason_code text,
    match_reason text,
    original_price numeric,
    original_currency text,
    exchange_rate_used numeric,
    exchange_rate_date timestamptz,
    checked_at timestamptz not null default now(),
    expires_at timestamptz not null,
    hit_count integer not null default 0,
    evidence_json jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists pricing_cache_entries_normalized_identity_idx
    on public.pricing_cache_entries (normalized_identity);

create index if not exists pricing_cache_entries_category_idx
    on public.pricing_cache_entries (category);

create index if not exists pricing_cache_entries_expires_at_idx
    on public.pricing_cache_entries (expires_at);

-- Supports the global-catalog promotion job's candidate query:
-- WHERE hit_count >= :threshold AND valuation_status = 'market_estimated'
--   AND expires_at > now()
create index if not exists pricing_cache_entries_promotion_candidates_idx
    on public.pricing_cache_entries (hit_count, valuation_status, expires_at);
