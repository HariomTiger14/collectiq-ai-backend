import argparse
import csv
import hashlib
import json
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from scripts._ops_run_recorder import dump_and_report, run_with_recorder
from scripts._shared_rate_limiter import (
    CLASS_ESSENTIAL_CATALOG,
    PRICECHARTING_CSV,
    SharedRateLimiter,
)
from scripts.backfill_pricecharting_sets import REQUEST_HEADERS
from scripts.import_pricecharting_catalog import PRICECHARTING_CSV_ENV_VARS
from scripts.import_pricecharting_catalog import SupabaseCatalogClient
from scripts.import_pricecharting_catalog import to_catalog_row


DEFAULT_SOURCE_ORDER = ("video_games", "pokemon", "magic", "yugioh", "one_piece")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    selected_sources = _selected_sources(args.sources)
    client = None
    if not args.dry_run:
        client = SupabaseCatalogClient(
            supabase_url=args.supabase_url or os.getenv("SUPABASE_URL", ""),
            service_role_key=args.service_role_key
            or os.getenv("SUPABASE_SERVICE_ROLE_KEY", ""),
            timeout_seconds=args.timeout_seconds,
        )

    # Pacing goes through the account-wide limiter rather than a local
    # sleep. A local sleep only spaces THIS run's downloads; it cannot see
    # the tier-3 rotation or the sets backfill, which hit the same CSV
    # endpoint on the same account and can land inside this run's window.
    csv_limiter = SharedRateLimiter(
        PRICECHARTING_CSV,
        slot_class=CLASS_ESSENTIAL_CATALOG,
        fallback_interval_seconds=args.sleep_between_sources_seconds,
    )

    summaries: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for source in selected_sources:
        # Per source, not once per run: sources are 610s apart and this job
        # averages 60 minutes, so a run-wide stamp would try to close rows
        # that tier-1 rewrote in the meantime (23514, valid_to < valid_from).
        source_downloaded_at = datetime.now(timezone.utc).isoformat()
        # One source's failure must not cost the others their daily
        # refresh: before this, a single 503 on the first CSV aborted the
        # whole run and every remaining category went stale for a day
        # (observed live 2026-08-29). Failures are recorded and the run
        # still reports failure at the end -- partial progress is kept,
        # but a partly-failed run never reports success.
        try:
            summary = refresh_source(
                source=source,
                source_downloaded_at=source_downloaded_at,
                archive_dir=args.archive_dir,
                batch_size=args.batch_size,
                timeout_seconds=args.timeout_seconds,
                dry_run=args.dry_run,
                client=client,
                csv_limiter=csv_limiter,
            )
            summaries.append(summary)
        except Exception as error:  # noqa: BLE001 - reported, not swallowed
            print(
                f"Source {source} failed: {type(error).__name__}: {error}",
                flush=True,
            )
            failures.append({"source": source, "error": f"{type(error).__name__}: {error}"})

    print(
        dump_and_report(
            {
                # False when any source failed, so a partly-failed run is
                # never recorded as a success in the ops ledger.
                "success": not failures,
                "dryRun": args.dry_run,
                "sources": summaries,
                "failedSources": failures,
                "inputRows": sum(summary["inputRows"] for summary in summaries),
                "validRows": sum(summary["validRows"] for summary in summaries),
                "importedRows": sum(summary["importedRows"] for summary in summaries),
                "historyRows": sum(summary["historyRows"] for summary in summaries),
                "archivedFiles": [
                    summary["archivePath"]
                    for summary in summaries
                    if summary.get("archivePath")
                ],
            },
            indent=2,
        ),
        flush=True,
    )
    # Non-zero so the ops ledger records the run as failed and the
    # Scheduled-jobs board shows it -- the successful sources' data is
    # already imported either way.
    return 1 if failures else 0


def refresh_source(
    *,
    source: str,
    source_downloaded_at: str,
    archive_dir: str,
    batch_size: int,
    timeout_seconds: float,
    dry_run: bool,
    client: SupabaseCatalogClient | None,
    csv_limiter: SharedRateLimiter | None = None,
) -> dict[str, Any]:
    print(f"Refreshing PriceCharting source: {source}", flush=True)
    source_name = f"{source}.csv"
    temp_path = download_source_to_temp_file(
        source=source,
        source_name=source_name,
        timeout_seconds=timeout_seconds,
        csv_limiter=csv_limiter,
    )
    try:
        archive_path = archive_source_file(
            source_name=source_name,
            path=temp_path,
            source_downloaded_at=source_downloaded_at,
            archive_dir=archive_dir,
            dry_run=dry_run,
        )
        summary = import_source_file(
            source_name=source_name,
            path=temp_path,
            source_downloaded_at=source_downloaded_at,
            batch_size=batch_size,
            dry_run=dry_run,
            client=client,
        )
        summary["archivePath"] = str(archive_path) if archive_path else None
        return summary
    finally:
        temp_path.unlink(missing_ok=True)


def download_source_to_temp_file(
    *,
    source: str,
    source_name: str,
    timeout_seconds: float,
    csv_limiter: SharedRateLimiter | None = None,
) -> Path:
    env_name = PRICECHARTING_CSV_ENV_VARS[source]
    url = os.getenv(env_name, "").strip()
    if not url:
        raise SystemExit(f"{env_name} is not configured.")

    print(f"Downloading {source} CSV...", flush=True)
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=f"-{source_name}")
    temp_path = Path(handle.name)
    handle.close()

    # PriceCharting generates these CSVs on demand, and the generator
    # intermittently 503s on the larger ones (observed live 2026-08-29:
    # one 503 aborted the whole daily refresh). Retry transient failures
    # -- 5xx, 429 and network/timeout errors -- with a widening backoff,
    # since the next scheduled attempt is a whole day away. 4xx other
    # than 429 is a configuration/auth problem and is raised immediately.
    last_error: Exception | None = None
    for attempt, backoff_seconds in enumerate((10, 30, 90, 0), start=1):
        # Every attempt is a CSV call, retries included. The old 10/30/90s
        # backoff re-requested well inside the one-per-10-minutes limit.
        if csv_limiter is not None:
            csv_limiter.acquire()
        try:
            with httpx.Client(timeout=timeout_seconds, follow_redirects=True) as http:
                with http.stream(
                    "GET", url, headers={**REQUEST_HEADERS, "Accept": "text/csv,*/*"}
                ) as response:
                    response.raise_for_status()
                    with temp_path.open("wb") as output:
                        for chunk in response.iter_bytes():
                            output.write(chunk)
            break
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            if status < 500 and status != 429:
                temp_path.unlink(missing_ok=True)
                raise
            last_error = error
        except httpx.HTTPError as error:
            last_error = error
        if not backoff_seconds:
            temp_path.unlink(missing_ok=True)
            raise last_error
        print(
            f"  {source_name} download attempt {attempt} failed "
            f"({type(last_error).__name__}); retrying",
            flush=True,
        )
        if csv_limiter is None:
            time.sleep(backoff_seconds)

    print(f"Downloaded {source_name}; starting streamed import.", flush=True)
    return temp_path


def import_source_file(
    *,
    source_name: str,
    path: Path,
    source_downloaded_at: str,
    batch_size: int,
    dry_run: bool,
    client: SupabaseCatalogClient | None,
) -> dict[str, Any]:
    if batch_size <= 0:
        raise ValueError("batch_size must be greater than zero")
    if not dry_run and client is None:
        raise SystemExit("Supabase client is required for non-dry-run refresh.")

    input_rows = 0
    valid_rows = 0
    imported_rows = 0
    history_rows = 0
    batch: list[dict[str, Any]] = []
    seen_in_batch: set[str] = set()

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            input_rows += 1
            catalog_row = to_catalog_row(row, source_name, source_downloaded_at)
            if catalog_row is None:
                continue
            product_id = str(catalog_row.get("pricecharting_id") or "").strip()
            if product_id in seen_in_batch:
                continue
            seen_in_batch.add(product_id)
            batch.append(catalog_row)
            valid_rows += 1
            if len(batch) >= batch_size:
                result = import_batch(
                    batch=batch,
                    batch_size=batch_size,
                    dry_run=dry_run,
                    client=client,
                    imported_rows=imported_rows,
                )
                imported_rows += result["importedRows"]
                history_rows += result["historyRows"]
                batch = []
                seen_in_batch = set()

    if batch:
        result = import_batch(
            batch=batch,
            batch_size=batch_size,
            dry_run=dry_run,
            client=client,
            imported_rows=imported_rows,
        )
        imported_rows += result["importedRows"]
        history_rows += result["historyRows"]

    print(f"Processed {source_name} with {input_rows} rows.", flush=True)
    return {
        "source": source_name,
        "inputRows": input_rows,
        "validRows": valid_rows,
        "importedRows": imported_rows,
        "historyRows": history_rows,
    }


def import_batch(
    *,
    batch: list[dict[str, Any]],
    batch_size: int,
    dry_run: bool,
    client: SupabaseCatalogClient | None,
    imported_rows: int,
) -> dict[str, int]:
    if dry_run:
        return {"importedRows": 0, "historyRows": 0}
    if client is None:
        raise SystemExit("Supabase client is required for non-dry-run refresh.")
    # A single slow/failing write (e.g. a Postgres statement timeout on the
    # growing catalog table) must not crash the whole 5-source refresh and
    # cost every other already-processed source its import. Whatever landed
    # before the failure is already committed (idempotent upserts), so the
    # only cost of treating this batch as failed is a retry next cycle.
    try:
        history_total = client.sync_scd2_history_rows(batch, batch_size=batch_size)
        total = client.upsert_rows(batch, batch_size=batch_size)
    except (SystemExit, Exception) as exc:
        print(f"  Batch write failed, will retry next cycle: {exc}", flush=True)
        return {"importedRows": 0, "historyRows": 0}
    print(f"Imported {imported_rows + total} rows for current source...", flush=True)
    return {"importedRows": total, "historyRows": history_total}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Refresh PackLox PriceCharting catalog from configured CSV URLs."
    )
    parser.add_argument(
        "--sources",
        default=",".join(DEFAULT_SOURCE_ORDER),
        help="Comma-separated source keys to refresh.",
    )
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--timeout-seconds", type=float, default=600)
    parser.add_argument(
        "--sleep-between-sources-seconds",
        type=float,
        default=600,
        help="PriceCharting CSV calls are limited to one every 10 minutes.",
    )
    parser.add_argument("--supabase-url", default="", help="Defaults to SUPABASE_URL.")
    parser.add_argument(
        "--service-role-key",
        default="",
        help="Defaults to SUPABASE_SERVICE_ROLE_KEY.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--archive-dir",
        default=os.getenv("PRICECHARTING_CSV_ARCHIVE_DIR", ""),
        help=(
            "Optional directory for keeping raw daily CSV files. "
            "Defaults to PRICECHARTING_CSV_ARCHIVE_DIR."
        ),
    )
    return parser.parse_args(argv)


def _selected_sources(raw_sources: str) -> list[str]:
    selected = [
        source.strip().lower().replace("-", "_")
        for source in raw_sources.split(",")
        if source.strip()
    ]
    if not selected:
        raise SystemExit("At least one source is required.")
    unsupported = [
        source for source in selected if source not in PRICECHARTING_CSV_ENV_VARS
    ]
    if unsupported:
        allowed = ", ".join(sorted(PRICECHARTING_CSV_ENV_VARS))
        raise SystemExit(
            f"Unsupported source(s): {', '.join(unsupported)}. Use one of: {allowed}."
        )
    return selected


def archive_source_file(
    *,
    source_name: str,
    path: Path,
    source_downloaded_at: str,
    archive_dir: str,
    dry_run: bool,
) -> Path | None:
    root = archive_dir.strip()
    if not root:
        return None

    archive_root = Path(root)
    archive_date = _archive_date(source_downloaded_at)
    destination = archive_root / archive_date / source_name
    checksum_path = destination.with_suffix(destination.suffix + ".sha256")

    if dry_run:
        print(f"Would archive {source_name} to {destination}.", flush=True)
        return destination

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, destination)
    checksum_path.write_text(
        f"{_sha256_file(destination)}  {source_name}\n",
        encoding="utf-8",
    )
    print(f"Archived {source_name} to {destination}.", flush=True)
    return destination


def _archive_date(source_downloaded_at: str) -> str:
    value = source_downloaded_at.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc).date().isoformat()
    except ValueError:
        return datetime.now(timezone.utc).date().isoformat()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(run_with_recorder("pricecharting-csv-refresh", main))
