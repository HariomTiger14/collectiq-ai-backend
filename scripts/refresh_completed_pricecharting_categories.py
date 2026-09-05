"""Daily refresh for already-completed PriceCharting registry categories
(coins, comic-books, funko-pops, lego-sets, lorcana-cards).

backfill_pricecharting_sets.py's claim_rows() permanently excludes any
registry row once last_fetch_status='success' -- correct for the huge,
heavily-throttled sportscardspro.com categories (still mid first-pass;
re-touching a completed set there would just steal cron capacity from
sets that have never been attempted), but wrong for these 5 small
pricecharting.com categories, which are already fully backfilled
(~6,600 sets total combined) and completely unthrottled (pricecharting.com
has no Cloudflare pacing requirement, unlike sportscardspro.com).

Rather than complicate the shared claim/priority-tier logic the
sportscardspro categories depend on, this is a small standalone script:
unconditionally re-download every set in these categories every day via
the console_uid + download-custom CSV path already saved in the registry
from the original backfill. No claim/lease needed -- it always processes
the same fixed, known population, and catalog writes are content-hash
-diffed (see SupabaseCatalogClient.upsert_rows), so a repeat run over
unchanged data is a cheap no-op rather than wasted work.
"""

import argparse
import json
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from scripts._shared_rate_limiter import (
    CLASS_ESSENTIAL_CATEGORIES,
    PRICECHARTING_CSV,
    SharedRateLimiter,
)
from scripts._ops_run_recorder import dump_and_report, run_with_recorder
from scripts.backfill_pricecharting_sets import (
    CSV_DOWNLOAD_MIN_INTERVAL_SECONDS,
    REQUEST_HEADERS,
    SOURCE_SITE_BASE_URLS,
    chunked,
    cleanup_csv_downloads,
    fetch_batch_csv_file,
    write_catalog_rows,
)
from scripts.import_pricecharting_catalog import (
    SupabaseCatalogClient,
    chunked_iter,
    iter_rows_from_file,
    to_catalog_row,
)


# The 5 pricecharting.com categories that aren't covered by the 5-bulk-CSV
# daily refresh (refresh_pricecharting_catalog.py, which pulls video games/
# Pokemon/Magic/YuGiOh/One Piece from fixed per-category CSV URLs instead of
# the per-set registry). All 5 here are already 100% backfilled.
TARGET_CATEGORIES = ["coins", "comic-books", "funko-pops", "lego-sets", "lorcana-cards"]
REGISTRY_PAGE_SIZE = 1000


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    token = args.api_token or os.getenv("PRICECHARTING_API_TOKEN", "")
    if not token:
        raise SystemExit(
            "PRICECHARTING_API_TOKEN is required (or --api-token) -- even for "
            "--dry-run, since this worker makes real download-custom requests "
            "and only skips writing results."
        )

    supabase_url = args.supabase_url or os.getenv("SUPABASE_URL", "")
    service_role_key = args.service_role_key or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

    registry_reader = SupabaseRegistryReader(
        supabase_url=supabase_url,
        service_role_key=service_role_key,
        timeout_seconds=args.timeout_seconds,
    )
    catalog_client = (
        None
        if args.dry_run
        else SupabaseCatalogClient(
            supabase_url=supabase_url,
            service_role_key=service_role_key,
            timeout_seconds=args.timeout_seconds,
        )
    )

    rows = registry_reader.fetch_refreshable_rows()
    print(
        f"Found {len(rows)} completed set(s) across {', '.join(TARGET_CATEGORIES)}.",
        flush=True,
    )
    if not rows:
        print(dump_and_report({"success": True, "setsConsidered": 0}, indent=2), flush=True)
        return 0

    source_downloaded_at = datetime.now(timezone.utc).isoformat()
    base_url = SOURCE_SITE_BASE_URLS["pricecharting"]
    total_catalog_rows = 0
    failed_batches = 0
    succeeded_sets = 0

    with httpx.Client(
        timeout=args.timeout_seconds,
        follow_redirects=True,
        headers=REQUEST_HEADERS,
    ) as http:
        csv_limiter = SharedRateLimiter(
            PRICECHARTING_CSV,
            slot_class=CLASS_ESSENTIAL_CATEGORIES,
            fallback_interval_seconds=args.sleep_between_requests_seconds,
        )
        for index, chunk in enumerate(chunked(rows, args.batch_size)):
            # Shared with the tier-3 rotation and the sets backfill: this job
            # runs ~3.8h from 04:45 and overlaps them.
            csv_limiter.acquire()
            console_uids = [row["console_uid"] for row in chunk]
            print(f"Fetching batch of {len(chunk)} sets...", flush=True)
            csv_download = fetch_batch_csv_file(
                http, base_url=base_url, token=token, console_uids=console_uids
            )
            if csv_download is None:
                failed_batches += 1
                continue

            def _iter_catalog_rows(download=csv_download):
                for raw in iter_rows_from_file(
                    download.path, encoding=download.encoding
                ):
                    catalog_row = to_catalog_row(
                        raw,
                        "pricecharting-completed-category-refresh",
                        source_downloaded_at,
                    )
                    if catalog_row is not None:
                        yield catalog_row

            # Chunked off disk rather than one list per batch: at
            # --batch-size 300 a sports-sized batch is ~228,000 rows, and
            # holding those as dicts alongside the response body is what put
            # a 256 MB container at a 229 MB peak.
            batch_row_count = 0
            write_ok = True
            try:
                if args.dry_run:
                    batch_row_count = sum(1 for _ in _iter_catalog_rows())
                else:
                    assert catalog_client is not None
                    for ingest_chunk in chunked_iter(
                        _iter_catalog_rows(), args.ingest_chunk_rows
                    ):
                        batch_row_count += len(ingest_chunk)
                        if not write_catalog_rows(
                            catalog_client,
                            ingest_chunk,
                            batch_size=args.catalog_batch_size,
                        ):
                            write_ok = False
                            break
            finally:
                cleanup_csv_downloads([csv_download])

            total_catalog_rows += batch_row_count
            print(f"  Parsed {batch_row_count} catalog rows from this batch.", flush=True)
            if not write_ok:
                failed_batches += 1
                continue

            succeeded_sets += len(chunk)

    catalog_write_stats = (
        catalog_client.catalog_write_stats
        if catalog_client is not None
        else {"written": 0, "skippedUnchanged": 0, "failed": 0}
    )
    print(
        dump_and_report(
            {
                "success": True,
                "dryRun": args.dry_run,
                "setsConsidered": len(rows),
                "setsRefreshed": succeeded_sets,
                "failedBatches": failed_batches,
                "catalogRowsParsed": total_catalog_rows,
                # Real split from the client accumulator; rowsWritten used
                # to echo rowsParsed, hiding how much of each run was a
                # cheap unchanged-row read versus an actual write.
                "catalogRowsWritten": (
                    0 if args.dry_run else catalog_write_stats["written"]
                ),
                "catalogRowsSkippedUnchanged": (
                    0 if args.dry_run else catalog_write_stats["skippedUnchanged"]
                ),
                "catalogRowsFailed": (
                    0 if args.dry_run else catalog_write_stats["failed"]
                ),
            },
            indent=2,
        ),
        flush=True,
    )
    return 0


class SupabaseRegistryReader:
    def __init__(self, *, supabase_url: str, service_role_key: str, timeout_seconds: float) -> None:
        self.supabase_url = supabase_url.strip().rstrip("/")
        self.service_role_key = service_role_key.strip()
        self.timeout_seconds = timeout_seconds
        if not self.supabase_url or not self.service_role_key:
            raise SystemExit("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required.")

    def fetch_refreshable_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        with httpx.Client(timeout=self.timeout_seconds) as client:
            while True:
                response = client.get(
                    f"{self.supabase_url}/rest/v1/pricecharting_set_registry",
                    params={
                        "select": "registry_id,url,console_uid,set_name,category",
                        "source_site": "eq.pricecharting",
                        "category": f"in.({','.join(TARGET_CATEGORIES)})",
                        "console_uid": "not.is.null",
                        "order": "registry_id.asc",
                        "limit": str(REGISTRY_PAGE_SIZE),
                        "offset": str(offset),
                    },
                    headers=self._headers(),
                )
                response.raise_for_status()
                page = response.json()
                if not isinstance(page, list) or not page:
                    break
                rows.extend(page)
                if len(page) < REGISTRY_PAGE_SIZE:
                    break
                offset += REGISTRY_PAGE_SIZE
        return rows

    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self.service_role_key,
            "Authorization": f"Bearer {self.service_role_key}",
            "Content-Type": "application/json",
        }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Daily refresh of already-completed PriceCharting registry categories "
            "(coins, comic-books, funko-pops, lego-sets, lorcana-cards) via their "
            "saved console_uid and the download-custom CSV endpoint."
        )
    )
    parser.add_argument("--batch-size", type=int, default=150, help="Sets per download-custom request.")
    parser.add_argument("--catalog-batch-size", type=int, default=500)
    parser.add_argument("--timeout-seconds", type=float, default=30)
    parser.add_argument(
        "--ingest-chunk-rows",
        type=int,
        default=10_000,
        help="Rows held in memory before writing, independent of --batch-size.",
    )
    parser.add_argument(
        "--sleep-between-requests-seconds",
        type=float,
        default=CSV_DOWNLOAD_MIN_INTERVAL_SECONDS,
        help="Pacing between download-custom batches (pricecharting.com is unthrottled).",
    )
    parser.add_argument("--api-token", default="", help="Defaults to PRICECHARTING_API_TOKEN.")
    parser.add_argument("--supabase-url", default="", help="Defaults to SUPABASE_URL.")
    parser.add_argument(
        "--service-role-key", default="", help="Defaults to SUPABASE_SERVICE_ROLE_KEY."
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(run_with_recorder("completed-categories-refresh", main))
