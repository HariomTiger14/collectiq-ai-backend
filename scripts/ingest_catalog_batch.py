"""Ingest one validated catalog batch from storage into the catalog.

PR 3 of the staged pipeline, and the half that writes. It never fetches from
the vendor: its input is a CSV the downloader already paid a vendor slot for
and already validated, sitting in the private catalog-refresh-batches bucket.

WHY THAT SEPARATION EARNS ITS KEEP
----------------------------------
A failed ingest retries from the SAME stored object. Under the old one-pass
design a write failure meant re-downloading identical bytes, spending a
second slot out of 144/day -- and `completed-categories-refresh` failed 9-16
batches per run, so that was not hypothetical.

WHAT IT WILL NOT DO
-------------------
Stamp a registry row before the write lands. A set marked refreshed from data
that never arrived is indistinguishable from a real refresh until someone
reads the prices, which is the failure this whole pipeline exists to remove.
Stamping happens once, after a clean write, for exactly this batch's ids.

WRITE PATH
----------
REST, via the existing SupabaseCatalogClient: the 20s service_role timeout
plus #205's retry-with-halving moved 438k rows with 7 timeouts, 1,022 rows
recovered and 0 abandoned. COPY remains the escalation if rows_abandoned is
ever non-zero -- it needs DATABASE_URL, which Render crons do not carry.
"""

from __future__ import annotations

import argparse
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts._ops_run_recorder import dump_and_report, run_with_recorder
from scripts.backfill_pricecharting_sets import write_catalog_rows_with_retry
from scripts.catalog_batch_store import BatchStore
from scripts.catalog_batches import (
    CLASS_WRITE,
    INGEST_FAILED,
    INGESTED,
    INGESTING,
    VALIDATED,
    WRITE_REST,
    assert_transition,
    is_lease_stale,
    recovery_status,
)
from scripts.import_pricecharting_catalog import (
    SupabaseCatalogClient,
    chunked_iter,
    iter_rows_from_file,
    timeout_retry_summary,
    to_catalog_row,
)

DEFAULT_SOURCE = "sportscardspro"
SOURCE_FILE_TAG = "sportscardspro-tier3-refresh"


def reap_stale_ingest_leases(store: BatchStore, *, source: str, commit: bool) -> int:
    """Return abandoned ingests to validated so a later run can retry.

    Back to VALIDATED, not PENDING: the object is still in storage, so the
    retry costs no vendor slot. A batch whose claim is never released would
    otherwise sit forever, which is what left an ops_cron_runs row marked
    'running' since 2026-09-03.
    """
    reaped = 0
    for batch in store.batches(source=source, statuses=[INGESTING]):
        claimed_at = batch.get("claimed_at")
        moment = datetime.fromisoformat(claimed_at.replace("Z", "+00:00")) if claimed_at else None
        if not is_lease_stale(INGESTING, moment):
            continue
        target = recovery_status(INGESTING)
        assert_transition(INGESTING, target)
        print(f"  reaping stale ingest lease {batch['batch_id']} -> {target}", flush=True)
        if commit:
            store.update(batch["batch_id"], {
                "status": target, "claimed_at": None, "claimed_by": None,
                "last_error": "ingest lease expired", "last_error_class": "transient",
            })
        reaped += 1
    return reaped


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    store = BatchStore(
        supabase_url=args.supabase_url or os.getenv("SUPABASE_URL", ""),
        service_role_key=args.service_role_key or os.getenv("SUPABASE_SERVICE_ROLE_KEY", ""),
        timeout_seconds=args.timeout_seconds,
    )
    summary: dict[str, Any] = {
        "success": True, "commit": args.commit, "source": args.source,
        "batchId": None, "status": None, "staleLeasesReaped": 0, "skippedReason": None,
    }
    summary["staleLeasesReaped"] = reap_stale_ingest_leases(
        store, source=args.source, commit=args.commit)

    if not args.commit:
        waiting = store.batches(source=args.source, statuses=[VALIDATED, INGEST_FAILED])
        summary.update(skippedReason="dry_run", queueDepth=len(waiting))
        print(f"DRY RUN -- {len(waiting)} validated batch(es) waiting. "
              "Pass --commit to ingest one.", flush=True)
        if waiting:
            print(f"  next: {waiting[0]['batch_id']} "
                  f"({waiting[0].get('row_count')} rows, {waiting[0].get('requested_count')} sets)",
                  flush=True)
        print(dump_and_report(summary, indent=2), flush=True)
        return 0

    batch = store.claim_ingestable_batch(
        source=args.source,
        claimed_by=os.getenv("RENDER_SERVICE_NAME", "local"),
        max_attempts=args.max_attempts,
    )
    if batch is None:
        summary["skippedReason"] = "no_ingestable_batch"
        print("no validated or retryable batch waiting.", flush=True)
        print(dump_and_report(summary, indent=2), flush=True)
        return 0

    batch_id = batch["batch_id"]
    registry_ids = batch.get("registry_ids") or []
    summary.update(batchId=batch_id, status=INGESTING,
                   setsInBatch=len(registry_ids), storageKey=batch.get("storage_key"))
    print(f"claimed batch {batch_id} ({batch.get('row_count')} rows, "
          f"{len(registry_ids)} sets)", flush=True)

    catalog_client = SupabaseCatalogClient(
        supabase_url=store.base, service_role_key=store.key,
        timeout_seconds=args.timeout_seconds,
    )
    temp_path = Path(tempfile.mkstemp(prefix="catalog-ingest-", suffix=".csv")[1])
    started = time.perf_counter()
    # Where a 13.7-minute ingest actually goes. #213 cut SCD2 versions by
    # 97.8% and moved the wall clock by 3% per row, which told us the SCD2
    # write was never the bottleneck -- but not what is. The client already
    # accumulated phase_seconds and nothing ever printed them, so the split
    # was being inferred rather than measured. These three timers cover what
    # the client cannot see: storage, parsing, and registry stamping.
    timings: dict[str, float] = {"storage_download": 0.0, "parse": 0.0,
                                 "registry_stamp": 0.0}
    try:
        download_started_at = time.perf_counter()
        size = store.download_object(batch["storage_key"], temp_path)
        timings["storage_download"] = time.perf_counter() - download_started_at
        print(f"  downloaded {size:,} bytes from storage", flush=True)

        # Row-at-a-time off disk, chunked into the writer. Never materialised:
        # holding a 350-set batch as dicts cost 2,028 MB when measured.
        stamp = datetime.now(timezone.utc).isoformat()

        def rows():
            # Timed inside the generator because parsing is interleaved with
            # writing -- the chunk loop pulls rows as it needs them, so there
            # is no wall-clock window that contains only parsing.
            while True:
                parse_started_at = time.perf_counter()
                try:
                    for raw in iter_rows_from_file(temp_path, encoding="utf-8"):
                        row = to_catalog_row(raw, SOURCE_FILE_TAG, stamp)
                        timings["parse"] += time.perf_counter() - parse_started_at
                        if row is not None:
                            yield row
                        parse_started_at = time.perf_counter()
                finally:
                    timings["parse"] += time.perf_counter() - parse_started_at
                return

        parsed = 0
        wrote = True
        retries = 0
        for chunk in chunked_iter(rows(), args.ingest_chunk_rows):
            parsed += len(chunk)
            chunk_wrote, chunk_retries = write_catalog_rows_with_retry(
                catalog_client, chunk,
                batch_size=args.catalog_batch_size,
                attempts=args.write_attempts,
                backoff_seconds=args.write_retry_seconds,
            )
            retries += chunk_retries
            if not chunk_wrote:
                wrote = False
                break

        ingest_ms = int((time.perf_counter() - started) * 1000)
        stats = catalog_client.catalog_write_stats
        timeouts = timeout_retry_summary(catalog_client)
        common = {
            "ingest_ms": ingest_ms,
            "rows_written": stats["written"],
            "rows_skipped": stats["skippedUnchanged"],
            "rows_failed": stats["failed"],
            "write_path": WRITE_REST,
            "statement_timeouts": timeouts["statementTimeouts"],
            "rows_recovered": timeouts["statementTimeoutRowsRecovered"],
            "rows_abandoned": timeouts["statementTimeoutRowsAbandoned"],
        }
        file_rows = int(batch.get("row_count") or 0)
        phases = {**catalog_client.phase_seconds, **timings}
        accounted = sum(phases.values())
        summary.update(rowsParsed=parsed, fileRowCount=file_rows,
                       fileComplete=(file_rows == 0 or parsed >= file_rows),
                       writeRetries=retries, ingestMs=ingest_ms,
                       rowsWritten=stats["written"], rowsSkipped=stats["skippedUnchanged"],
                       rowsFailed=stats["failed"],
                       phaseSeconds={name: round(value, 2)
                                     for name, value in sorted(
                                         phases.items(), key=lambda kv: -kv[1])},
                       # Anything the timers did not claim: retry backoff
                       # sleeps, chunking, JSON encoding, interpreter time.
                       # Printed rather than hidden, so a large residual is
                       # visible as a gap instead of read as "nothing else".
                       phaseUnaccountedSeconds=round(
                           max(ingest_ms / 1000 - accounted, 0.0), 2),
                       priceHistory=dict(catalog_client.price_history_stats),
                       **timeouts)

        if not wrote:
            # The object stays in storage, so the retry costs no vendor slot.
            store.update(batch_id, {
                **common, "status": INGEST_FAILED, "claimed_at": None, "claimed_by": None,
                "last_error": (
                    f"catalog write failed after {parsed:,} of {file_rows:,} "
                    "rows; the rest of the file was not attempted"
                ),
                "last_error_class": CLASS_WRITE,
            })
            summary.update(success=False, status=INGEST_FAILED,
                           rowsParsedBeforeFailure=parsed)
            print(f"catalog write failed after {parsed:,} of {file_rows:,} rows "
                  "-- the rest of the file was NOT attempted. Batch left "
                  "retryable from storage, registry NOT stamped.", flush=True)
            print(dump_and_report(summary, indent=2), flush=True)
            return 0

        # Only now. A set marked refreshed from data that never landed looks
        # exactly like a real refresh until someone reads the prices.
        stamp_started_at = time.perf_counter()
        store.stamp_registry_refreshed(registry_ids)
        timings["registry_stamp"] = time.perf_counter() - stamp_started_at
        assert_transition(INGESTING, INGESTED)
        store.update(batch_id, {**common, "status": INGESTED,
                                "claimed_at": None, "claimed_by": None})
        summary.update(status=INGESTED, setsStamped=len(registry_ids))
        total = max(ingest_ms / 1000, 0.001)
        print("  where the time went:", flush=True)
        for name, value in sorted(phases.items(), key=lambda kv: -kv[1]):
            if value >= 0.005:
                print(f"    {name:24} {value:8.1f}s  {value / total * 100:5.1f}%",
                      flush=True)
        unaccounted = max(total - accounted, 0.0)
        print(f"    {'(unaccounted)':24} {unaccounted:8.1f}s  "
              f"{unaccounted / total * 100:5.1f}%", flush=True)
        print(f"ingested: {stats['written']:,} written, "
              f"{stats['skippedUnchanged']:,} unchanged, {stats['failed']:,} failed; "
              f"stamped {len(registry_ids)} sets", flush=True)
    finally:
        temp_path.unlink(missing_ok=True)

    print(dump_and_report(summary, indent=2), flush=True)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ingest one validated catalog batch from storage. "
                    "Never fetches from the vendor.")
    parser.add_argument("--source", default=DEFAULT_SOURCE)
    parser.add_argument(
        "--max-attempts", type=int, default=5,
        help="Stop retrying a batch after this many ingest attempts. It stays "
             "visible as ingest_failed rather than being dropped.")
    parser.add_argument("--ingest-chunk-rows", type=int, default=5000)
    # 1000, not 500: at 500 a 292k-row batch made 590 sub-batches and up to
    # 3,540 REST calls, and round-trip latency dominated the run (78% of a
    # 20-minute ingest). 1000 halves that.
    #
    # And not 2000, which was tried and broke: PostgREST silently caps a
    # response at 1,000 rows, so the current-history lookup came back
    # truncated, 1,000 ids looked like they had no current row, and the
    # writer hit 23505 inserting a second current row. The write batch and
    # the lookup batch are the same number here, so the ceiling is
    # strictly UNDER 1,000 -- a full-cap reply cannot be told apart from a
    # truncated one. 900 leaves headroom and still cuts sub-batches 586 -> 186.
    parser.add_argument("--catalog-batch-size", type=int, default=900)
    parser.add_argument("--write-attempts", type=int, default=3)
    parser.add_argument("--write-retry-seconds", type=float, default=5.0)
    parser.add_argument("--timeout-seconds", type=float, default=900)
    parser.add_argument("--supabase-url", default="")
    parser.add_argument("--service-role-key", default="")
    parser.add_argument(
        "--commit", action="store_true",
        help="Actually claim and ingest. Without it nothing is claimed or written.")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(run_with_recorder("catalog-batch-ingest", main))
