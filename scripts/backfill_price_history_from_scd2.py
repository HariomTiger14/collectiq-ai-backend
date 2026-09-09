"""One-time backfill: distinct price points from SCD2 -> the snapshot table.

`pricecharting_price_history` only starts 2026-08-28. `pricecharting_catalog_history`
holds price points going back further -- 90.8% of the price points in a sampled
window have no snapshot. Charts cannot move to the cheap table until those exist.

This reads the SCD2 table and writes ONLY `pricecharting_price_history`. Every
statement ends in `ON CONFLICT (pricecharting_id, observed_at) DO NOTHING`, so a
chunk that already ran is a no-op and an interrupted run is resumed by re-running
the same command. Measured on scratch: re-running a completed chunk inserted 0 rows.

A "distinct price point" is a version whose price tuple
(loose, cib, new, graded, box_only, manual_only, currency) differs from the
previous version of the same item ordered by valid_from. The first version of an
item always counts. Metadata-only versions (4.4% of the sample) are skipped --
they carry no new price and would be indistinguishable duplicates.

Chunks are ranges of `pricecharting_id` as TEXT, derived from 3-char prefix counts
and sized to ~150k versions so each statement stays around 30s. Bounds are
half-open [lo, hi); the first chunk is unbounded below and the last unbounded
above, so the 104 chunks cover every id exactly once with no gap and no overlap.
Text ordering is safe here: an id starting with prefix P sorts >= P and < any
3-char prefix greater than P.

Default is dry-run. Pass --commit to write.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time

import psycopg

PLAN_PATH = pathlib.Path(__file__).with_name("backfill_price_history_chunks.json")
CHECKPOINT_PATH = pathlib.Path(".backfill_price_history.checkpoint")
STATEMENT_TIMEOUT = "900s"
SOURCE_FILE_MARKER = "backfill-from-scd2"

_POINTS = """
    select h.pricecharting_id, h.valid_from,
           h.loose_price_cents, h.cib_price_cents, h.new_price_cents,
           h.graded_price_cents, h.box_only_price_cents, h.manual_only_price_cents,
           coalesce(h.currency, 'USD') as currency,
           lag((h.loose_price_cents, h.cib_price_cents, h.new_price_cents,
                h.graded_price_cents, h.box_only_price_cents,
                h.manual_only_price_cents, h.currency))
             over (partition by h.pricecharting_id order by h.valid_from) as prev
      from public.pricecharting_catalog_history h
     where (%(lo)s::text is null or h.pricecharting_id >= %(lo)s)
       and (%(hi)s::text is null or h.pricecharting_id <  %(hi)s)
"""

_CHANGED = """
     where p.prev is null
        or p.prev is distinct from (p.loose_price_cents, p.cib_price_cents,
                                    p.new_price_cents, p.graded_price_cents,
                                    p.box_only_price_cents,
                                    p.manual_only_price_cents, p.currency)
"""

INSERT_SQL = f"""
insert into public.pricecharting_price_history
    (pricecharting_id, observed_at, loose_price_cents, cib_price_cents,
     new_price_cents, graded_price_cents, box_only_price_cents,
     manual_only_price_cents, currency, source_file)
select p.pricecharting_id, p.valid_from, p.loose_price_cents, p.cib_price_cents,
       p.new_price_cents, p.graded_price_cents, p.box_only_price_cents,
       p.manual_only_price_cents, p.currency, '{SOURCE_FILE_MARKER}'
  from ({_POINTS}) p
{_CHANGED}
on conflict (pricecharting_id, observed_at) do nothing
"""

COUNT_SQL = f"""
with pts as (select p.pricecharting_id, p.valid_from from ({_POINTS}) p {_CHANGED})
select count(*),
       count(*) filter (where not exists (
         select 1 from public.pricecharting_price_history s
          where s.pricecharting_id = pts.pricecharting_id
            and s.observed_at = pts.valid_from))
  from pts
"""


def load_plan(path: pathlib.Path) -> list[dict]:
    plan = json.loads(path.read_text())
    if plan[0]["lo"] is not None or plan[-1]["hi"] is not None:
        raise SystemExit("plan must be unbounded at both ends or ids will be missed")
    for a, b in zip(plan, plan[1:]):
        if a["hi"] != b["lo"]:
            raise SystemExit(f"gap or overlap between chunk {a['seq']} and {b['seq']}")
    return plan


def load_checkpoint(path: pathlib.Path) -> set[int]:
    if not path.exists():
        return set()
    return {int(line) for line in path.read_text().split() if line.strip()}


def record_checkpoint(path: pathlib.Path, seq: int) -> None:
    with path.open("a") as handle:
        handle.write(f"{seq}\n")
        handle.flush()
        os.fsync(handle.fileno())


def select_chunks(plan: list[dict], done: set[int],
                  args: argparse.Namespace) -> list[dict]:
    """Which chunks this invocation may touch.

    --stop-after-chunk bounds by seq, so it means the same thing however many
    times the command is repeated. --limit-chunks bounds by COUNT relative to
    the checkpoint -- "the next N unprocessed" -- which is what makes resuming
    work, and is also how a bounded run silently grew from 3 chunks to 6 the
    first time this was used against production.

    Both filters take a prefix of an ascending list, so their order does not
    affect the result; the ceiling is applied first only because it reads
    better. Do not infer that the order is load-bearing.
    """
    todo = [c for c in plan if c["seq"] >= args.start_at and c["seq"] not in done]
    if args.stop_after_chunk is not None:
        todo = [c for c in todo if c["seq"] <= args.stop_after_chunk]
    if args.limit_chunks:
        todo = todo[: args.limit_chunks]
    return todo


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

    plan = load_plan(args.plan)
    done = load_checkpoint(args.checkpoint)
    todo = select_chunks(plan, done, args)

    mode = "COMMIT" if args.commit else "DRY-RUN (nothing will be written)"
    print(f"{mode}: {len(todo)} of {len(plan)} chunks to process "
          f"({len(done)} already done)", flush=True)

    started = time.perf_counter()
    inserted = counted = 0
    with psycopg.connect(dsn, connect_timeout=30, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(f"set statement_timeout='{STATEMENT_TIMEOUT}'")
            for chunk in todo:
                bounds = {"lo": chunk["lo"], "hi": chunk["hi"]}
                elapsed = time.perf_counter()
                if args.commit:
                    cur.execute(INSERT_SQL, bounds)
                    rows = cur.rowcount
                    inserted += rows
                    record_checkpoint(args.checkpoint, chunk["seq"])
                    label = "inserted"
                else:
                    cur.execute(COUNT_SQL, bounds)
                    _points, rows = cur.fetchone()
                    counted += rows
                    label = "would insert"
                took = time.perf_counter() - elapsed
                print(f"  chunk {chunk['seq']:>3}/{len(plan)} "
                      f"[{chunk['lo'] or '-':>4}..{chunk['hi'] or '-':<4}] "
                      f"{label} {rows:>8,}  {took:>6.1f}s  "
                      f"(total {inserted or counted:>10,}, "
                      f"{(time.perf_counter()-started)/60:.1f} min)", flush=True)

    total = inserted if args.commit else counted
    print(f"\n{'inserted' if args.commit else 'would insert'} {total:,} rows in "
          f"{(time.perf_counter()-started)/60:.1f} min", flush=True)
    if not args.commit:
        print("dry run: pass --commit to write")
    return 0


if __name__ == "__main__":
    sys.exit(main())
