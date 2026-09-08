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
from scripts.import_pricecharting_catalog import iter_rows_from_file

DEFAULT_SOURCE = "sportscardspro"
# 350: 500 works but sits near the vendor's cliff -- 500 returns in 38s, 1000
# fails with a 503 at 26s, and that cliff is a server-side TIME budget that
# moves with their load. 350 gives margin and still refreshes all 36,962 sets
# in ~17.7h at 106 requests/day (74% of the 144-slot budget).
DEFAULT_BATCH_SIZE = 350
# Two validated batches waiting is already a signal the ingester is behind.
DEFAULT_MAX_QUEUE_DEPTH = 2


class BatchStore:
    """PostgREST access to catalog_download_batches and the registry."""

    def __init__(self, *, supabase_url: str, service_role_key: str, timeout_seconds: float):
        self.base = supabase_url.rstrip("/")
        self.key = service_role_key
        self.timeout = timeout_seconds
        if not self.base or not self.key:
            raise SystemExit("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required.")

    def _headers(self, **extra: str) -> dict[str, str]:
        return {"apikey": self.key, "Authorization": f"Bearer {self.key}", **extra}

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self.timeout)

    def claim_due_sets(self, *, source: str, limit: int) -> list[dict[str, Any]]:
        """The rotation's own queue predicate, unchanged.

        Ordering by tier3_refreshed_at NULLS FIRST is what makes the rotation
        a rotation: never-refreshed sets lead, then oldest.
        """
        with self._client() as client:
            response = client.get(
                f"{self.base}/rest/v1/pricecharting_set_registry",
                params={
                    "select": "registry_id,console_uid,set_name",
                    "source_site": f"eq.{source}",
                    "console_uid": "not.is.null",
                    "last_fetch_status": "eq.success",
                    "tier3_failure_count": "lt.3",
                    "order": "tier3_refreshed_at.asc.nullsfirst,registry_id.asc",
                    "limit": str(limit),
                },
                headers=self._headers(),
            )
            response.raise_for_status()
            return [row for row in response.json() if isinstance(row, dict)]

    def batches(self, *, source: str, statuses: list[str]) -> list[dict[str, Any]]:
        with self._client() as client:
            response = client.get(
                f"{self.base}/rest/v1/catalog_download_batches",
                params={
                    "select": "*",
                    "source": f"eq.{source}",
                    "status": f"in.({','.join(statuses)})",
                    "order": "created_at.asc",
                },
                headers=self._headers(),
            )
            response.raise_for_status()
            return response.json()

    def insert(self, row: dict[str, Any]) -> dict[str, Any]:
        with self._client() as client:
            response = client.post(
                f"{self.base}/rest/v1/catalog_download_batches",
                headers=self._headers(**{"Content-Type": "application/json",
                                         "Prefer": "return=representation"}),
                json=row,
            )
            response.raise_for_status()
            return response.json()[0]

    def update(self, batch_id: str, patch: dict[str, Any]) -> None:
        patch = {**patch, "updated_at": datetime.now(timezone.utc).isoformat()}
        with self._client() as client:
            response = client.patch(
                f"{self.base}/rest/v1/catalog_download_batches",
                params={"batch_id": f"eq.{batch_id}"},
                headers=self._headers(**{"Content-Type": "application/json",
                                         "Prefer": "return=minimal"}),
                json=patch,
            )
            response.raise_for_status()

    def upload(self, key: str, path: Path) -> None:
        """Upload the CSV to the private bucket.

        Same service-role storage API the codebase already uses for coin
        images and data-request exports.
        """
        with open(path, "rb") as handle:
            with httpx.Client(timeout=max(self.timeout, 300)) as client:
                response = client.post(
                    f"{self.base}/storage/v1/object/{BUCKET}/{key}",
                    headers=self._headers(**{"Content-Type": "text/csv",
                                             "x-upsert": "true"}),
                    content=handle.read(),
                )
                response.raise_for_status()


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

    # Backpressure: a growing validated queue means the ingester is behind,
    # and downloading more only fills the bucket and burns vendor slots.
    queued = store.batches(source=args.source, statuses=[DOWNLOADED, VALIDATED])
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
        rows = None
        print(f"resuming pending batch {batch['batch_id']} "
              f"({batch['requested_count']} sets)", flush=True)
    else:
        rows = store.claim_due_sets(source=args.source, limit=args.batch_size)
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
    set_names = [r["set_name"] for r in rows] if rows else []
    summary.update(batchId=batch_id, setsRequested=len(uids))

    assert_transition(PENDING, DOWNLOADING)
    store.update(batch_id, {
        "status": DOWNLOADING, "attempts": int(batch.get("attempts") or 0) + 1,
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
            return 0

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
                        help="Skip the run when this many batches already await ingest.")
    parser.add_argument("--csv-sleep-seconds", type=float, default=600.0)
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
