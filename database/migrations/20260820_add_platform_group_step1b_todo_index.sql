-- STEP 1b (NEW, insert between step 1 and step 2) -- run this statement
-- ALONE. CREATE INDEX CONCURRENTLY cannot execute inside a transaction
-- block.
--
-- Without this, the backfill procedure's batch query (WHERE platform_group
-- IS NULL AND console_name IS NOT NULL LIMIT 5000) has to sequentially
-- scan past every row ALREADY backfilled to find the next 5,000 still-NULL
-- ones -- and that scan gets more expensive with every batch, since the
-- "already done" prefix only grows. Confirmed live: this is why even a
-- bounded 200-batch call (~1M rows) still hit the SQL editor's gateway
-- timeout twice.
--
-- A partial index on exactly this predicate turns that into a fast index
-- scan straight to the remaining rows, and self-maintains as the backfill
-- progresses: every row the backfill updates to non-null naturally drops
-- OUT of this index (its own WHERE clause no longer matches), so the index
-- shrinks as the work gets done rather than needing separate cleanup.
-- Harmless to leave in place afterward -- once the backfill finishes it
-- matches zero rows, costing next to nothing to maintain.
create index concurrently if not exists pricecharting_catalog_platform_group_todo_idx
    on public.pricecharting_catalog (pricecharting_id)
    where platform_group is null and console_name is not null;
