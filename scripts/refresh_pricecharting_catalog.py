import argparse
import json
import os
import time
from datetime import datetime, timezone
from typing import Any

from scripts.import_pricecharting_catalog import PRICECHARTING_CSV_ENV_VARS
from scripts.import_pricecharting_catalog import SupabaseCatalogClient
from scripts.import_pricecharting_catalog import dedupe_catalog_rows
from scripts.import_pricecharting_catalog import download_env_sources
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
    catalog_sources = download_env_sources(
        timeout_seconds=timeout_seconds,
        source_filter=source,
    )
    if len(catalog_sources) != 1:
        raise SystemExit(f"Expected one CSV source for {source}, got {len(catalog_sources)}.")

    catalog_source = catalog_sources[0]
    rows = [
        to_catalog_row(row, catalog_source.name, source_downloaded_at)
        for row in catalog_source.rows
    ]
    rows = dedupe_catalog_rows([row for row in rows if row is not None])
    imported_rows = 0
    if not dry_run:
        if client is None:
            raise SystemExit("Supabase client is required for non-dry-run refresh.")
        imported_rows = client.upsert_rows(rows, batch_size=batch_size)

    return {
        "source": catalog_source.name,
        "inputRows": len(catalog_source.rows),
        "validRows": len(rows),
        "importedRows": imported_rows,
    }


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
