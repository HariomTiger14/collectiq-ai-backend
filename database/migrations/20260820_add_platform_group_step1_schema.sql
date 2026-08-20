-- STEP 1 of 3 for Video Games platform filtering -- run this statement
-- block ALONE (its own "Run" in the SQL editor), before step 2. This part
-- has no CONCURRENTLY/COMMIT restrictions, so it's fine as one batch, but
-- keep it separate from steps 2-3 which do.
--
-- Adds a precomputed platform_group column so the admin Catalog products
-- filter (and eventually mobile Discover) can filter Video Games by
-- platform (PlayStation/Xbox/Nintendo/etc). This was deliberately left out
-- of PRICECHARTING_CATEGORY_GROUPS before now: video-games rows use
-- `category` for a real per-game genre (31+ distinct values), not a fixed
-- platform taxonomy, and a console_name-based ilike-OR filter (~24
-- keywords) was tried and reverted -- confirmed live TODAY it still hits a
-- statement timeout (57014, 8.3s) even with console_name's existing
-- trigram index, because OR-ing that many patterns against ~12M rows and
-- then sorting/paginating just isn't viable at this scale regardless of
-- indexing approach. An exact-match filter against a precomputed column is
-- the actual fix.
--
-- console_name is NOT video-games-specific -- it's reused across every
-- category as a general "set/platform" field (confirmed live: sampled
-- rows include "Baseball Cards 2019 Panini Donruss Optic", "Comic Books
-- Superman", "Funko POP NFL" alongside "Playstation 4", "JP Xbox 360").
-- platform_group is computed from console_name but only matches real
-- platform names -- non-video-game rows simply get NULL, which is correct
-- (they're not filterable by platform because they aren't a platform).
--
-- Real observed console_name values include region prefixes ("JP
-- Playstation 4", "PAL Playstation 5") and bare/short platform names
-- ("Playstation", "PSP", "Wii") -- word-boundary regex matching (\m...\M)
-- is used instead of plain ilike substring matching specifically to avoid
-- a repeat of a documented prior bug (a bare "nes" ilike pattern matched
-- inside "Finest", pulling sports cards into a video-games filter).
-- Word-boundary matching only matches whole tokens, so short platform
-- codes (ds, wii, pc) are safe to include here where they weren't before.
--
-- Not exhaustive -- covers the major, well-known platform families backed
-- by real sampled data. Rare/long-tail platforms simply stay NULL (not
-- filterable by platform, same as any other unmapped console_name would
-- be under the old approach) rather than guessing at unseen naming.

alter table public.pricecharting_catalog
    add column if not exists platform_group text;

-- Single source of truth for the console_name -> platform_group mapping.
-- Mirrored in Python (scripts/import_pricecharting_catalog.py's
-- compute_platform_group()) so new/refreshed rows get the same value at
-- ingest time without a repeat of this backfill -- keep both in sync if
-- this mapping changes.
create or replace function public.compute_platform_group(console_name text)
returns text
language sql
immutable
as $$
    select case
        when console_name is null then null
        when console_name ~* '\mplaystation\M|\mps1\M|\mps2\M|\mps3\M|\mps4\M|\mps5\M|\mpsp\M|\mvita\M'
            then 'playstation'
        when console_name ~* '\mxbox\M'
            then 'xbox'
        when console_name ~* '\mnintendo\M|\mgamecube\M|\mwii\M|\mswitch\M|\mgameboy\M|\mnes\M|\msnes\M|\mn64\M|\m3ds\M|\mds\M'
            then 'nintendo'
        when console_name ~* '\msega\M|\mgenesis\M|\msaturn\M|\mdreamcast\M|\m32x\M'
            then 'sega'
        when console_name ~* '\matari\M|\mjaguar\M|\mlynx\M|\m2600\M|\m5200\M|\m7800\M'
            then 'atari'
        when console_name ~* '\mpc\M|\mwindows\M|\mcommodore\M|\mamiga\M|\mmsx\M|\mtrs-80\M|\mapple\M'
            then 'pc'
        when console_name ~* '\m3do\M|\mneo\s*geo\M|\mcolecovision\M|\mintellivision\M|\mvectrex\M|\mturbografx\M|\mturbo\s*grafx\M'
            then 'retro-other'
        else null
    end;
$$;

-- One-time batched backfill -- a single UPDATE across ~12M rows would risk
-- the same class of statement-timeout incident this table already had
-- (20260808_drop_unused_pricecharting_catalog_indexes.sql). Batches of
-- 5,000 with a COMMIT between each keep every individual UPDATE well
-- under any statement timeout, using a PROCEDURE (not a DO block/function)
-- specifically because only procedures can COMMIT mid-execution.
-- FOR UPDATE SKIP LOCKED lets this run safely alongside concurrent writes
-- from the ingest crons without blocking on or being blocked by them.
--
-- max_batches bounds each CALL so it returns well before Supabase's SQL
-- editor gateway timeout (confirmed live: an unbounded call over ~12M rows
-- got killed by an upstream timeout) -- each committed batch stays
-- committed even when the connection is later cut, so this is always safe
-- to just re-run; it resumes from wherever it left off (only ever touches
-- rows still NULL). 200 batches * 5,000 rows = up to 1M rows per call --
-- call it repeatedly until it reports 0 rows left. RAISE NOTICE surfaces
-- progress in the SQL editor's message log so you can see it advancing.
--
-- CREATE OR REPLACE only replaces a procedure with the exact same
-- parameter signature -- adding max_batches makes this a distinct overload
-- from the original zero-argument version, not a replacement of it.
-- Without this drop, both versions exist simultaneously and a zero-argument
-- CALL becomes ambiguous (Postgres error 42725, same failure mode already
-- documented for search_pricecharting_catalog's own migrations).
drop procedure if exists public.backfill_pricecharting_platform_group();

create or replace procedure public.backfill_pricecharting_platform_group(max_batches integer default 200)
language plpgsql
as $$
declare
    affected integer;
    batches_run integer := 0;
    total_updated integer := 0;
begin
    loop
        with batch as (
            select pricecharting_id
            from public.pricecharting_catalog
            where platform_group is null
              and console_name is not null
            limit 5000
            for update skip locked
        )
        update public.pricecharting_catalog c
        set platform_group = public.compute_platform_group(c.console_name)
        from batch
        where c.pricecharting_id = batch.pricecharting_id;

        get diagnostics affected = row_count;
        total_updated := total_updated + affected;
        batches_run := batches_run + 1;
        commit;
        raise notice 'backfill_pricecharting_platform_group: % rows updated so far this call (batch %)', total_updated, batches_run;
        exit when affected = 0;
        exit when batches_run >= max_batches;
    end loop;
    if affected = 0 then
        raise notice 'backfill_pricecharting_platform_group: DONE -- no rows left to update.';
    else
        raise notice 'backfill_pricecharting_platform_group: stopped at max_batches (%) -- re-run this same CALL to continue.', max_batches;
    end if;
end;
$$;
