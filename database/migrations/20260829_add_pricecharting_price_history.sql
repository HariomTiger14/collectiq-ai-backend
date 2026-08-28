-- Compact price observations for PriceCharting catalog items (Step 2 of
-- the SCD2 remediation, per the 2026-08-29 audit).
--
-- The audit measured that 82.2% of SCD2 history versions are price-only
-- changes -- ~30 bytes of new information stored as a ~1.35 KB full
-- catalog row copy (raw_payload included). This table is the compact
-- destination for those observations: one row per (item, observation
-- time) carrying exactly what the price-history reader contract needs --
-- _fetch_history_rows() selects the validity window, the six price
-- columns, currency, and source_file, and nothing else.
--
-- SHADOW PHASE: the SCD2 writer dual-writes here while the legacy
-- history table keeps receiving every write and remains the only thing
-- readers consume. Reader migration is a separate, later step gated on
-- observed equivalence between the two.
--
-- Identity: pricecharting_id is the provider's stable business key (PK
-- of pricecharting_catalog; never reassigned, and catalog rows are never
-- deleted -- n_tup_del = 0 lifetime). Deliberately NO foreign key,
-- matching pricecharting_catalog_history's existing loose coupling: an
-- FK would add a per-insert lookup against a 12M-row table and couple
-- ingestion ordering (history batches write before catalog upserts) for
-- zero practical integrity gain given the no-delete invariant.
--
-- Idempotency: UNIQUE (pricecharting_id, observed_at). observed_at is
-- the provider-download timestamp of the observation, so a retried
-- ingestion of the same input maps to the same key and conflicts
-- instead of duplicating -- the database enforces this, not application
-- memory. The same composite btree is exactly the reader access pattern
-- (item + time ordering/range), so no additional index is needed;
-- deliberately none created.
--
-- Estimated row cost: ~110 B heap + ~40 B index =~ 150 B/observation
-- vs ~1,350 B for a legacy full-width version (~89% reduction).

CREATE TABLE IF NOT EXISTS public.pricecharting_price_history (
    price_history_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    pricecharting_id text NOT NULL,
    observed_at timestamptz NOT NULL,
    loose_price_cents integer,
    cib_price_cents integer,
    new_price_cents integer,
    graded_price_cents integer,
    box_only_price_cents integer,
    manual_only_price_cents integer,
    currency text NOT NULL DEFAULT 'USD',
    source_file text,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pricecharting_price_history_item_observation_unique
        UNIQUE (pricecharting_id, observed_at)
);

-- Service-role-only, same posture as the ops tables: RLS on, no
-- policies -- anon/authenticated see nothing, service role bypasses.
ALTER TABLE public.pricecharting_price_history ENABLE ROW LEVEL SECURITY;
