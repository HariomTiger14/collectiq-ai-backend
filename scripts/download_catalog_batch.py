"""Stage one catalog refresh batch: claim sets, fetch the CSV, validate, upload.

PR 2 of the staged pipeline. This job DOWNLOADS ONLY. It never writes to
pricecharting_catalog, never writes history, and never stamps a registry row
-- stamping belongs to the ingester (PR 3) and only after a clean write.

WHY A SEPARATE DOWNLOAD STAGE
-----------------------------
Today's refresh downloads, parses and writes in one pass, so a failure
anywhere loses everything before it. Measured 2026-09-07:
completed-categories-refresh reported success:true alongside
catalogRowsFailed:2463 -- 2,463 rows dropped, with no record of which rows or
why. Splitting the stages means a fetch that succeeded stays succeeded: the
CSV is in storage, and a failed ingest retries from there without spending
another vendor slot (144 a day, and sports alone wants ~106 of them).

Render crons do not share a filesystem, so the handoff is the private
catalog-refresh-batches bucket rather than a local path.

BACKPRESSURE
------------
If validated batches are piling up, the ingester is behind and downloading
more only fills the bucket and wastes vendor slots. The queue depth check is
what stops a stuck ingester from turning into a storage bill.

DEFAULT-SAFE
------------
Without --commit this claims nothing, fetches nothing and writes nothing; it
reports what it WOULD do. The cron start command carries --commit explicitly.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from scripts._ops_run_recorder import dump_and_report, run_with_recorder
from scripts._shared_rate_limiter import (
    BULK_MAX_SLOT_WAIT_SECONDS,
    CLASS_TIER3,
    PRICECHARTING_CSV,
    SharedRateLimiter,
)
from scripts.catalog_batches import (
    BUCKET,
    CLASS_BLOCKED,
    CLASS_VALIDATION,
    DOWNLOADED,
    DOWNLOADING,
    FETCH_FAILED,
    PENDING,
    QUEUE_DEPTH_STATUSES,
    VALIDATED,
    VALIDATION_FAILED,
    assert_transition,
    classify_http_failure,
    is_lease_stale,
    new_batch_row,
    recovery_status,
    storage_key,
)
from scripts.csv_source_policy import (
    CsvFamilyMismatch,
    csv_base_url,
    families_in_rows,
    mismatch_detail,
    validate_csv_families,
)
from scripts.backfill_pricecharting_sets import REQUEST_HEADERS
from scripts.catalog_batch_store import BatchStore, dedupe_by_console_uid
from scripts.import_pricecharting_catalog import iter_rows_from_file

DEFAULT_SOURCE = "sportscardspro"
# 350: 500 works but sits near the vendor's cliff -- 500 returns in 38s, 1000
# fails with a 503 at 26s, and that cliff is a server-side TIME budget that
# moves with their load. 350 gives margin and still refreshes all 36,962 sets
# in ~17.7h at 106 requests/day (74% of the 144-slot budget).
DEFAULT_BATCH_SIZE = 350
# Two batches in flight is already a signal the ingester is behind. Left at 2 so
# an interactive Shell run is unchanged; the sports cron passes 1 explicitly,
# because at a 10-minute cadence "two in flight" means two concurrent disk
# writers rather than a queue.
DEFAULT_MAX_QUEUE_DEPTH = 2


def reap_stale_leases(store: BatchStore, *, source: str, commit: bool) -> int:
    """Return abandoned downloads to a state a later run can pick up.

    A run that dies mid-claim would otherwise hold its batch forever, and the
    in-flight guard would refuse every subsequent run. ops_cron_runs has
    carried a tier3-sportscardspro-rotation row marked 'running' since
    2026-09-03 -- 118 hours -- for exactly this reason.
    """
    reaped = 0
    for batch in store.batches(source=source, statuses=[DOWNLOADING]):
        claimed_at = batch.get("claimed_at")
        moment = datetime.fromisoformat(claimed_at.replace("Z", "+00:00")) if claimed_at else None
        if not is_lease_stale(DOWNLOADING, moment):
            continue
        target = recovery_status(DOWNLOADING)
        assert_transition(DOWNLOADING, target)
        print(f"  reaping stale download lease {batch['batch_id']} -> {target}", flush=True)
        if commit:
            store.update(batch["batch_id"], {
                "status": target, "claimed_at": None, "claimed_by": None,
                "last_error": "download lease expired", "last_error_class": "transient",
            })
        reaped += 1
    return reaped


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    token = args.api_token or os.getenv("PRICECHARTING_API_TOKEN", "") or os.getenv(
        "PRICECHARTING_API_KEY", "")
    if not token:
        raise SystemExit("PRICECHARTING_API_TOKEN is required (or --api-token).")

    store = BatchStore(
        supabase_url=args.supabase_url or os.getenv("SUPABASE_URL", ""),
        service_role_key=args.service_role_key or os.getenv("SUPABASE_SERVICE_ROLE_KEY", ""),
        timeout_seconds=args.timeout_seconds,
    )
    summary: dict[str, Any] = {
        "success": True, "commit": args.commit, "source": args.source,
        "batchId": None, "status": None, "setsRequested": 0,
        "staleLeasesReaped": 0, "queueDepth": 0, "skippedReason": None,
    }

    summary["staleLeasesReaped"] = reap_stale_leases(
        store, source=args.source, commit=args.commit)

    # Backpressure: more work already in the pipe means the ingester is behind,
    # and downloading more only fills the bucket and burns vendor slots.
    #
    # QUEUE_DEPTH_STATUSES includes INGESTING and DOWNLOADING, not just the
    # files sitting on disk -- see the note there. The short version: on a
    # 10-minute schedule an ingest outlives the tick that fed it, so counting
    # only DOWNLOADED+VALIDATED lets tick N+1 write a second 37 MB file while
    # tick N's is still being read.
    queued = store.batches(source=args.source,
                           statuses=sorted(QUEUE_DEPTH_STATUSES))
    summary["queueDepth"] = len(queued)
    if len(queued) >= args.max_queue_depth:
        summary["skippedReason"] = "queue_full"
        print(f"{len(queued)} batch(es) awaiting ingest (limit {args.max_queue_depth}) "
              "-- skipping this run.", flush=True)
        print(dump_and_report(summary, indent=2), flush=True)
        return 0

    # Resume an abandoned batch before claiming new sets: its uids are already
    # spoken for, and re-claiming them would double-book the same rows.
    resumable = store.batches(source=args.source, statuses=[PENDING])
    if resumable:
        batch = resumable[0]
        spent = int(batch.get("download_attempts") or 0)
        if spent >= args.max_attempts:
            # STOP -- do not fall through to claim_due_sets.
            #
            # This row still holds its 350 console_uids. Skipping it and
            # claiming fresh sets would book those uids a second time: two
            # vendor CSVs for the same sets and two stamps later. The
            # rotation halting until a human resets download_attempts is the
            # lesser failure, and it is visible in the table rather than in a
            # log nobody reads.
            #
            # success=false and exit 1, not a quiet skip. no_csv_slot and
            # no_ingestable_batch mean "nothing to do, next cycle proceeds";
            # this means "the rotation is stopped". A green cron carrying only
            # a skippedReason is the #217 failure shape, and #224 exists
            # because the ledger did not record that distinction either.
            summary.update(success=False, batchId=batch["batch_id"],
                           status=PENDING,
                           skippedReason="download_attempts_exhausted",
                           downloadAttempts=spent,
                           maxAttempts=args.max_attempts)
            print(f"STOPPING: pending batch {batch['batch_id']} has used "
                  f"{spent} of {args.max_attempts} download attempts. Its "
                  f"{batch.get('requested_count')} sets stay booked to it, so "
                  "no new batch is claimed. Reset download_attempts or the row "
                  "to let those sets re-enter the rotation.", flush=True)
            print(dump_and_report(summary, indent=2), flush=True)
            return 1
        batch = resumable[0]
        rows = None
        print(f"resuming pending batch {batch['batch_id']} "
              f"({batch['requested_count']} sets, "
              f"download attempt {spent + 1}/{args.max_attempts})", flush=True)
    else:
        # Deduped HERE, not inside the store. It is batch-construction policy,
        # not part of the rotation's queue predicate -- and a test double for
        # the store would otherwise return raw rows and hide the whole thing,
        # which is exactly what happened on the first attempt.
        rows = dedupe_by_console_uid(
            store.claim_due_sets(source=args.source, limit=args.batch_size))
        if not rows:
            summary["skippedReason"] = "no_due_sets"
            print("no sets due for refresh.", flush=True)
            print(dump_and_report(summary, indent=2), flush=True)
            return 0
        payload = new_batch_row(
            source=args.source,
            console_uids=[r["console_uid"] for r in rows],
            registry_ids=[r["registry_id"] for r in rows],
        )
        if not args.commit:
            summary.update(setsRequested=len(rows), status="(dry-run: no batch created)")
            print(f"DRY RUN -- would claim {len(rows)} sets and create a batch. "
                  "Pass --commit to act.", flush=True)
            print(dump_and_report(summary, indent=2), flush=True)
            return 0
        batch = store.insert(payload)
        print(f"created batch {batch['batch_id']} with {len(rows)} sets", flush=True)

    batch_id = batch["batch_id"]
    uids = batch["console_uids"]
    # Widened to every registry name sharing a requested console_uid. The
    # vendor answers a uid with ITS canonical family name, which need not be
    # the name of the row we claimed -- G9157 is registered as both
    # '2015 Panini Donrus' and '2015 Panini Donruss'. Without this, a batch
    # claiming one can be answered with the other, refused as a wrong-catalog
    # response, and the same 350 sets reclaimed next run: a permanent jam.
    #
    # Not a weakening of the guard. A family matching a sibling of a REQUESTED
    # uid is a requested set under a different label; a genuinely wrong catalog
    # still matches no name of any requested uid.
    # Derived from the BATCH's console_uids, not from the claimed rows, so it
    # is populated on both paths. A resumed PENDING batch has no rows -- and
    # with an empty expected list validate_csv_families returns early and
    # performs NO family check at all, so the wrong-catalog guard was simply
    # absent on every retry. That is worse than the jam this widening fixes:
    # a jam refuses a good batch loudly, an absent guard accepts a bad one
    # quietly.
    claimed_names = {r["set_name"] for r in rows if r.get("set_name")} if rows else set()
    set_names = sorted(
        claimed_names | set(store.sibling_set_names(source=args.source, uids=uids))
    )
    summary.update(batchId=batch_id, setsRequested=len(uids))

    assert_transition(PENDING, DOWNLOADING)
    store.update(batch_id, {
        # download_attempts only. `attempts` is an INGEST alias now: bumping it
        # here is what let a download spend the ingester's retry budget, so a
        # perfectly retryable Storage object could be abandoned early.
        "status": DOWNLOADING,
        "download_attempts": int(batch.get("download_attempts") or 0) + 1,
        "claimed_at": datetime.now(timezone.utc).isoformat(),
        "claimed_by": os.getenv("RENDER_SERVICE_NAME", "local"),
        "fetch_started_at": datetime.now(timezone.utc).isoformat(),
    })

    # Account-wide, not per-run: the bulk refresh and completed-categories draw
    # on the same published 1-per-10-minutes CSV budget.
    limiter = SharedRateLimiter(
        PRICECHARTING_CSV, slot_class=CLASS_TIER3,
        fallback_interval_seconds=args.csv_sleep_seconds,
    )
    if not limiter.acquire(max_wait_seconds=BULK_MAX_SLOT_WAIT_SECONDS):
        store.update(batch_id, {"status": PENDING, "claimed_at": None, "claimed_by": None,
                                "last_error": "no CSV slot available",
                                "last_error_class": "transient"})
        summary.update(status=PENDING, skippedReason="no_csv_slot")
        print("no CSV slot available -- batch left pending.", flush=True)
        print(dump_and_report(summary, indent=2), flush=True)
        return 0

    base_url = csv_base_url(args.source)
    temp_path = Path(tempfile.mkstemp(prefix="catalog-batch-", suffix=".csv")[1])
    started = time.perf_counter()
    try:
        # Streamed to disk, never materialised: measured 4.1 MB peak streaming
        # 354k rows versus 2,028 MB holding them.
        with httpx.Client(timeout=args.timeout_seconds, follow_redirects=True,
                          headers=REQUEST_HEADERS) as http:
            with http.stream("GET", f"{base_url}/price-guide/download-custom",
                             params={"t": token, "console-uids": ",".join(uids)}) as response:
                if response.status_code != 200:
                    response.read()
                    error_class = classify_http_failure(response.status_code)
                    store.update(batch_id, {
                        "status": FETCH_FAILED, "claimed_at": None, "claimed_by": None,
                        "fetch_ms": int((time.perf_counter() - started) * 1000),
                        "last_error": f"HTTP {response.status_code}",
                        "last_error_class": error_class,
                    })
                    summary.update(success=False, status=FETCH_FAILED,
                                   httpStatus=response.status_code, errorClass=error_class)
                    print(f"fetch failed: HTTP {response.status_code} ({error_class})", flush=True)
                    print(dump_and_report(summary, indent=2), flush=True)
                    # A block is not a throttle: stop rather than harden it.
                    return 1 if error_class == CLASS_BLOCKED else 0
                with temp_path.open("wb") as out:
                    for chunk in response.iter_bytes():
                        out.write(chunk)
        fetch_ms = int((time.perf_counter() - started) * 1000)
        size = temp_path.stat().st_size

        families = families_in_rows(iter_rows_from_file(temp_path, encoding="utf-8"))
        row_count = sum(1 for _ in iter_rows_from_file(temp_path, encoding="utf-8"))
        store.update(batch_id, {
            "status": DOWNLOADED, "bytes": size, "row_count": row_count,
            "family_count": len(families), "fetch_ms": fetch_ms,
        })
        summary.update(bytes=size, rowCount=row_count, familyCount=len(families),
                       fetchMs=fetch_ms, host=base_url)
        print(f"downloaded {size:,} bytes / {row_count:,} rows / {len(families)} families "
              f"in {fetch_ms/1000:.1f}s", flush=True)

        # Validate BEFORE upload, so a wrong-catalog file never enters the
        # ingest queue at all. A mistyped filter returned HTTP 200 with
        # 123,166 rows of the wrong catalog (measured 2026-09-07).
        try:
            validate_csv_families(families, expected_set_names=set_names,
                                  requested_uid_count=len(uids))
        except CsvFamilyMismatch as exc:
            store.update(batch_id, {
                "status": VALIDATION_FAILED, "claimed_at": None, "claimed_by": None,
                "last_error": json.dumps(mismatch_detail(exc))[:2000],
                "last_error_class": CLASS_VALIDATION,
            })
            summary.update(success=False, status=VALIDATION_FAILED,
                           errorClass=CLASS_VALIDATION,
                           mismatch=mismatch_detail(exc))
            print(f"REFUSING BATCH: {exc}", flush=True)
            print(dump_and_report(summary, indent=2), flush=True)
            # Non-zero: a refusal is not backpressure. The file is a
            # wrong-catalog response and the same sets will be claimed again
            # next run, so it repeats until someone looks -- which is what
            # happened on 2026-09-09, twice in two minutes, both recorded
            # green. A vendor 503 stays a quiet 0 below; this does not.
            return 1

        key = storage_key(args.source, batch_id)
        store.upload(key, temp_path)
        assert_transition(DOWNLOADED, VALIDATED)
        store.update(batch_id, {"status": VALIDATED, "storage_key": key,
                                "claimed_at": None, "claimed_by": None})
        summary.update(status=VALIDATED, storageKey=key)
        print(f"uploaded to {BUCKET}/{key}; batch is ready for ingest", flush=True)
    finally:
        temp_path.unlink(missing_ok=True)

    print(dump_and_report(summary, indent=2), flush=True)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download one catalog refresh batch into the staging bucket. "
                    "Downloads only -- no catalog writes, no registry stamping.")
    parser.add_argument("--source", default=DEFAULT_SOURCE)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--max-queue-depth", type=int, default=DEFAULT_MAX_QUEUE_DEPTH,
                        help="Skip the run when this many batches are already in "
                             "flight (downloading, downloaded, validated or "
                             "ingesting). Default 2 keeps a manual Shell run "
                             "behaving as before; the 10-minute sports cron "
                             "passes 1, which is what makes it single-writer.")
    parser.add_argument("--csv-sleep-seconds", type=float, default=600.0)
    parser.add_argument(
        "--max-attempts", type=int, default=5,
        help="Abandon a PENDING batch after this many download attempts. NEW "
             "in this release -- the downloader previously counted attempts "
             "and never checked them, so a batch that could not be fetched "
             "was retried forever, one vendor slot per cycle.")
    parser.add_argument("--timeout-seconds", type=float, default=900)
    parser.add_argument("--api-token", default="")
    parser.add_argument("--supabase-url", default="")
    parser.add_argument("--service-role-key", default="")
    parser.add_argument(
        "--commit", action="store_true",
        help="Actually claim, fetch and upload. Without it nothing is written.")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(run_with_recorder("catalog-batch-download", main))
