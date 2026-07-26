import argparse
import csv
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from scripts.import_pricecharting_catalog import PRICECHARTING_CSV_ENV_VARS
from scripts.import_pricecharting_catalog import SupabaseCatalogClient
from scripts.import_pricecharting_catalog import to_catalog_row


DEFAULT_SOURCE_ORDER = ("video_games", "pokemon", "magic", "yugioh", "one_piece")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    selected_sources = _selected_sources(args.sources)
    source_downloaded_at = datetime.now(timezone.utc).isoformat()
    client = None
    if not args.dry_run:
        client = SupabaseCatalogClient(
            supabase_url=args.supabase_url or os.getenv("SUPABASE_URL", ""),
            service_role_key=args.service_role_key
            or os.getenv("SUPABASE_SERVICE_ROLE_KEY", ""),
            timeout_seconds=args.timeout_seconds,
        )

    summaries: list[dict[str, Any]] = []
    for index, source in enumerate(selected_sources):
        if index > 0 and args.sleep_between_sources_seconds > 0:
            print(
                f"Waiting {args.sleep_between_sources_seconds}s before next CSV download...",
                flush=True,
            )
            time.sleep(args.sleep_between_sources_seconds)

        summary = refresh_source(
            source=source,
            source_downloaded_at=source_downloaded_at,
            batch_size=args.batch_size,
            timeout_seconds=args.timeout_seconds,
            dry_run=args.dry_run,
            client=client,
        )
        summaries.append(summary)

    print(
        json.dumps(
            {
                "success": True,
                "dryRun": args.dry_run,
                "sources": summaries,
                "inputRows": sum(summary["inputRows"] for summary in summaries),
                "validRows": sum(summary["validRows"] for summary in summaries),
                "importedRows": sum(summary["importedRows"] for summary in summaries),
            },
            indent=2,
        ),
        flush=True,
    )
    return 0


def refresh_source(
    *,
    source: str,
    source_downloaded_at: str,
    batch_size: int,
    timeout_seconds: float,
    dry_run: bool,
    client: SupabaseCatalogClient | None,
) -> dict[str, Any]:
    print(f"Refreshing PriceCharting source: {source}", flush=True)
    source_name = f"{source}.csv"
    temp_path = download_source_to_temp_file(
        source=source,
        source_name=source_name,
        timeout_seconds=timeout_seconds,
    )
    try:
        return import_source_file(
            source_name=source_name,
            path=temp_path,
            source_downloaded_at=source_downloaded_at,
            batch_size=batch_size,
            dry_run=dry_run,
            client=client,
        )
    finally:
        temp_path.unlink(missing_ok=True)


def download_source_to_temp_file(
    *,
    source: str,
    source_name: str,
    timeout_seconds: float,
) -> Path:
    env_name = PRICECHARTING_CSV_ENV_VARS[source]
    url = os.getenv(env_name, "").strip()
    if not url:
        raise SystemExit(f"{env_name} is not configured.")

    print(f"Downloading {source} CSV...", flush=True)
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=f"-{source_name}")
    temp_path = Path(handle.name)
    handle.close()

    try:
        with httpx.Client(timeout=timeout_seconds, follow_redirects=True) as http:
            with http.stream("GET", url, headers={"Accept": "text/csv,*/*"}) as response:
                response.raise_for_status()
                with temp_path.open("wb") as output:
                    for chunk in response.iter_bytes():
                        output.write(chunk)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise

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
                imported_rows += import_batch(
                    batch=batch,
                    batch_size=batch_size,
                    dry_run=dry_run,
                    client=client,
                    imported_rows=imported_rows,
                )
                batch = []
                seen_in_batch = set()

    if batch:
        imported_rows += import_batch(
            batch=batch,
            batch_size=batch_size,
            dry_run=dry_run,
            client=client,
            imported_rows=imported_rows,
        )

    print(f"Processed {source_name} with {input_rows} rows.", flush=True)
    return {
        "source": source_name,
        "inputRows": input_rows,
        "validRows": valid_rows,
        "importedRows": imported_rows,
    }


def import_batch(
    *,
    batch: list[dict[str, Any]],
    batch_size: int,
    dry_run: bool,
    client: SupabaseCatalogClient | None,
    imported_rows: int,
) -> int:
    if dry_run:
        return 0
    if client is None:
        raise SystemExit("Supabase client is required for non-dry-run refresh.")
    total = client.upsert_rows(batch, batch_size=batch_size)
    print(f"Imported {imported_rows + total} rows for current source...", flush=True)
    return total


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


if __name__ == "__main__":
    raise SystemExit(main())
