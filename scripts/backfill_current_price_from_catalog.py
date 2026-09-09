"""One-time backfill: seed pricecharting_current_price from the catalog.

pricecharting_catalog still holds the current cents at the time this runs, so
the seed is a straight copy. After the writers change (PR 4) the catalog stops
being updated and this table becomes the only current-price source -- which is
why the seed has to happen before that switch, not after.

Reads pricecharting_catalog. Writes pricecharting_current_price. Nothing else.

Chunked over the same pricecharting_id ranges as the price-history backfill
(scripts/backfill_price_history_chunks.json) -- a half-open partition of the id
space, validated for gaps and overlaps on every run. The chunks were sized
against a larger table, so each covers fewer catalog rows than it did there;
that only makes the statements shorter.

Every insert ends in ON CONFLICT (pricecharting_id) DO NOTHING, so a redone
chunk is a no-op and an interrupted run resumes by re-running the same command.
DO NOTHING rather than DO UPDATE on purpose: a row already present was written
by the daily pipeline and is newer than the catalog copy this reads.

Default is dry-run. Pass --commit to write.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys
import time

import psycopg

from scripts.backfill_price_history_from_scd2 import (
    PLAN_PATH,
    load_checkpoint,
    load_plan,
    record_checkpoint,
)

CHECKPOINT_PATH = pathlib.Path(".backfill_current_price.checkpoint")
STATEMENT_TIMEOUT = "900s"

_BOUNDS = """
     where (%(lo)s::text is null or c.pricecharting_id >= %(lo)s)
       and (%(hi)s::text is null or c.pricecharting_id <  %(hi)s)
"""

INSERT_SQL = f"""
insert into public.pricecharting_current_price
    (pricecharting_id, loose_price_cents, cib_price_cents, new_price_cents,
     graded_price_cents, box_only_price_cents, manual_only_price_cents,
     currency, observed_at, source_file, category, platform_group)
select c.pricecharting_id, c.loose_price_cents, c.cib_price_cents,
       c.new_price_cents, c.graded_price_cents, c.box_only_price_cents,
       c.manual_only_price_cents, coalesce(c.currency, 'USD'),
       coalesce(c.source_downloaded_at, c.updated_at), c.source_file,
       c.category, c.platform_group
  from public.pricecharting_catalog c
{_BOUNDS}
on conflict (pricecharting_id) do nothing
"""

COUNT_SQL = f"""
select count(*)
  from public.pricecharting_catalog c
{_BOUNDS}
   and not exists (select 1 from public.pricecharting_current_price p
                    where p.pricecharting_id = c.pricecharting_id)
"""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true",
                        help="actually insert; without it nothing is written")
    parser.add_argument("--plan", type=pathlib.Path, default=PLAN_PATH)
    parser.add_argument("--checkpoint", type=pathlib.Path, default=CHECKPOINT_PATH)
    parser.add_argument(
        "--limit-chunks", type=int, default=None,
        help="process the next N UNPROCESSED chunks. Relative to the checkpoint, "
             "so re-running the same command continues past what it already did. "
             "For an absolute bound a re-run cannot exceed, use --stop-after-chunk.")
    parser.add_argument(
        "--stop-after-chunk", type=int, default=None,
        help="absolute ceiling: never touch a chunk with seq greater than N, "
             "however many times the command is re-run.")
    parser.add_argument("--start-at", type=int, default=1, help="first chunk seq")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL is not set")

    from scripts.backfill_price_history_from_scd2 import select_chunks

    plan = load_plan(args.plan)
    done = load_checkpoint(args.checkpoint)
    todo = select_chunks(plan, done, args)

    mode = "COMMIT" if args.commit else "DRY-RUN (nothing will be written)"
    print(f"{mode}: {len(todo)} of {len(plan)} chunks to process "
          f"({len(done)} already done)", flush=True)

    started = time.perf_counter()
    total = 0
    with psycopg.connect(dsn, connect_timeout=30, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(f"set statement_timeout='{STATEMENT_TIMEOUT}'")
            for chunk in todo:
                bounds = {"lo": chunk["lo"], "hi": chunk["hi"]}
                elapsed = time.perf_counter()
                if args.commit:
                    cur.execute(INSERT_SQL, bounds)
                    rows = cur.rowcount
                    record_checkpoint(args.checkpoint, chunk["seq"])
                    label = "inserted"
                else:
                    cur.execute(COUNT_SQL, bounds)
                    rows = cur.fetchone()[0]
                    label = "would insert"
                total += rows
                print(f"  chunk {chunk['seq']:>3}/{len(plan)} "
                      f"[{chunk['lo'] or '-':>4}..{chunk['hi'] or '-':<4}] "
                      f"{label} {rows:>8,}  {time.perf_counter()-elapsed:>6.1f}s  "
                      f"(total {total:>10,}, "
                      f"{(time.perf_counter()-started)/60:.1f} min)", flush=True)

    print(f"\n{'inserted' if args.commit else 'would insert'} {total:,} rows in "
          f"{(time.perf_counter()-started)/60:.1f} min", flush=True)
    if not args.commit:
        print("dry run: pass --commit to write")
    return 0


if __name__ == "__main__":
    sys.exit(main())
