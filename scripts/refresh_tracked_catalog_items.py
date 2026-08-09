"""Refresh individual catalog items that a user actually tracks, via
PriceCharting's /api/product (single-item lookup), regardless of which set
they belong to.

Why this exists: pricecharting_catalog rows for comic-books/coins/sports-cards
/lorcana-cards/funko-pops/lego-sets only ever get ONE write, from the
one-time discovery+backfill pipeline (backfill_pricecharting_sets.py) --
claim_rows() permanently excludes already-succeeded registry rows, so a set,
once backfilled, is never revisited. That's fine for initial catalog
completeness, but it means a tracked item's price -- and therefore its SCD2
history / portfolio detail chart -- never accumulates new data points after
the day it was first backfilled.

The 5 bulk categories (video games, Pokemon, Magic, YuGiOh, One Piece)
don't have this problem: refresh_pricecharting_catalog.py re-downloads their
full CSVs daily. Full-catalog daily refresh isn't reachable for the other
categories at their current scale (see PR discussion / project memory for
the throughput math), but a single /api/product?id=X call per tracked item
is cheap and only needs to happen for items someone actually owns -- so this
script closes the gap for exactly that population, on its own schedule,
completely independent of the backfill/discovery crons.
"""

import argparse
import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from scripts.backfill_pricecharting_sets import (
    REQUEST_HEADERS,
    SOURCE_SITE_BASE_URLS,
    chunked,
    write_catalog_rows,
)
from scripts.import_pricecharting_catalog import SupabaseCatalogClient, to_catalog_row


# source_file values written by refresh_pricecharting_catalog.py's own daily
# bulk CSV refresh (see DEFAULT_SOURCE_ORDER there) -- these categories are
# already fully refreshed every day, so re-fetching them one item at a time
# here would just waste rate-limited API calls on data that's already fresh.
BULK_REFRESHED_SOURCE_FILES = frozenset(
    f"{source}.csv"
    for source in ("video_games", "pokemon", "magic", "yugioh", "one_piece")
)

DEFAULT_STALE_AFTER_HOURS = 24.0
DEFAULT_LIMIT = 200
# PriceCharting's documented API limit is 1 call/sec, shared per subscriber
# token across both pricecharting.com and sportscardspro.com (same
# subscription) -- pace conservatively under that ceiling rather than
# assume the limit is tracked independently per domain.
DEFAULT_SLEEP_SECONDS = 1.5
PORTFOLIO_ITEMS_PAGE_SIZE = 1000
# Keeps each catalog lookup's "in.(...)" query string comfortably short.
CATALOG_LOOKUP_CHUNK_SIZE = 200


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    token = args.api_token or os.getenv("PRICECHARTING_API_TOKEN", "")
    if not token:
        raise SystemExit(
            "PRICECHARTING_API_TOKEN is required (or --api-token) -- even for "
            "--dry-run, since this worker makes real /api/product requests "
            "and only skips writing results."
        )

    supabase_url = args.supabase_url or os.getenv("SUPABASE_URL", "")
    service_role_key = args.service_role_key or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    reader = TrackedCatalogReader(
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

    tracked_ids = reader.fetch_tracked_pricecharting_ids()
    print(f"Found {len(tracked_ids)} distinct tracked pricecharting_id(s).", flush=True)

    stale_before = _stale_cutoff_iso(args.stale_after_hours)
    candidates = reader.fetch_stale_catalog_rows(
        tracked_ids,
        exclude_source_files=BULK_REFRESHED_SOURCE_FILES,
        stale_before=stale_before,
        limit=args.limit,
    )
    print(
        f"{len(candidates)} candidate(s) due for refresh (stale before {stale_before}).",
        flush=True,
    )

    source_downloaded_at = datetime.now(timezone.utc).isoformat()
    with httpx.Client(
        timeout=args.timeout_seconds, follow_redirects=True, headers=REQUEST_HEADERS
    ) as http:
        catalog_rows, failed = refresh_candidates(
            http,
            candidates,
            token=token,
            sleep_seconds=args.sleep_between_requests_seconds,
            source_downloaded_at=source_downloaded_at,
        )

    written = True
    if not args.dry_run and catalog_rows:
        assert catalog_client is not None
        written = write_catalog_rows(
            catalog_client, catalog_rows, batch_size=args.catalog_batch_size
        )

    print(
        json.dumps(
            {
                "success": True,
                "dryRun": args.dry_run,
                "tracked": len(tracked_ids),
                "candidates": len(candidates),
                "refreshed": len(catalog_rows) if written else 0,
                "failed": failed + (0 if written else len(catalog_rows)),
            },
            indent=2,
        ),
        flush=True,
    )
    return 0


def refresh_candidates(
    http: httpx.Client,
    candidates: list[dict[str, Any]],
    *,
    token: str,
    sleep_seconds: float,
    source_downloaded_at: str,
) -> tuple[list[dict[str, Any]], int]:
    catalog_rows: list[dict[str, Any]] = []
    failed = 0
    for index, candidate in enumerate(candidates):
        if index > 0 and sleep_seconds > 0:
            time.sleep(sleep_seconds)
        source_site = source_site_for(candidate.get("source_file") or "")
        product = fetch_product(
            http,
            base_url=SOURCE_SITE_BASE_URLS[source_site],
            token=token,
            pricecharting_id=candidate["pricecharting_id"],
        )
        if product is None:
            failed += 1
            continue
        catalog_row = to_catalog_row(product, f"{source_site}-tracked-refresh", source_downloaded_at)
        if catalog_row is None:
            failed += 1
            continue
        catalog_rows.append(catalog_row)
    return catalog_rows, failed


def source_site_for(source_file: str) -> str:
    return "sportscardspro" if source_file.startswith("sportscardspro") else "pricecharting"


def fetch_product(
    http: httpx.Client, *, base_url: str, token: str, pricecharting_id: str
) -> dict[str, Any] | None:
    try:
        response = http.get(
            f"{base_url}/api/product", params={"t": token, "id": pricecharting_id}
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        print(f"  Failed to refresh {pricecharting_id}: {exc}", flush=True)
        return None
    if payload.get("status") != "success":
        print(
            f"  Refresh failed for {pricecharting_id}: "
            f"{payload.get('error-message', 'unknown error')}",
            flush=True,
        )
        return None
    return payload


def _stale_cutoff_iso(hours: float, *, now: datetime | None = None) -> str:
    reference = now or datetime.now(timezone.utc)
    return (reference - timedelta(hours=hours)).isoformat()


class TrackedCatalogReader:
    def __init__(
        self,
        *,
        supabase_url: str,
        service_role_key: str,
        timeout_seconds: float,
        client: httpx.Client | None = None,
    ) -> None:
        self.supabase_url = supabase_url.strip().rstrip("/")
        self.service_role_key = service_role_key.strip()
        self.timeout_seconds = timeout_seconds
        self._client = client
        if not self.supabase_url or not self.service_role_key:
            raise SystemExit("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required.")

    def fetch_tracked_pricecharting_ids(self) -> list[str]:
        ids: set[str] = set()
        offset = 0
        client, should_close = self._client_or_new()
        try:
            while True:
                response = client.get(
                    f"{self.supabase_url}/rest/v1/portfolio_items",
                    params={
                        "select": "pricecharting_id",
                        "pricecharting_id": "not.is.null",
                        "limit": str(PORTFOLIO_ITEMS_PAGE_SIZE),
                        "offset": str(offset),
                    },
                    headers=self._headers(),
                )
                response.raise_for_status()
                page = response.json()
                if not isinstance(page, list) or not page:
                    break
                for row in page:
                    pricecharting_id = row.get("pricecharting_id") if isinstance(row, dict) else None
                    if pricecharting_id:
                        ids.add(str(pricecharting_id))
                if len(page) < PORTFOLIO_ITEMS_PAGE_SIZE:
                    break
                offset += PORTFOLIO_ITEMS_PAGE_SIZE
        finally:
            if should_close:
                client.close()
        return sorted(ids)

    def fetch_stale_catalog_rows(
        self,
        tracked_ids: list[str],
        *,
        exclude_source_files: frozenset[str],
        stale_before: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        if not tracked_ids or limit <= 0:
            return []
        exclude_clause = ",".join(sorted(exclude_source_files))
        results: list[dict[str, Any]] = []
        client, should_close = self._client_or_new()
        try:
            # Each chunk's "stalest first" ordering only applies within that
            # chunk, not globally across chunks -- an acceptable tradeoff for
            # a background refresh that converges over repeated runs, not a
            # correctness requirement to get the single globally-stalest
            # item first on every run.
            for chunk in chunked(tracked_ids, CATALOG_LOOKUP_CHUNK_SIZE):
                remaining = limit - len(results)
                if remaining <= 0:
                    break
                response = client.get(
                    f"{self.supabase_url}/rest/v1/pricecharting_catalog",
                    params={
                        "select": "pricecharting_id,source_file,source_downloaded_at",
                        "pricecharting_id": f"in.({','.join(chunk)})",
                        "source_file": f"not.in.({exclude_clause})",
                        "or": f"(source_downloaded_at.is.null,source_downloaded_at.lt.{stale_before})",
                        "order": "source_downloaded_at.asc.nullsfirst",
                        "limit": str(remaining),
                    },
                    headers=self._headers(),
                )
                response.raise_for_status()
                page = response.json()
                if isinstance(page, list):
                    results.extend(row for row in page if isinstance(row, dict))
        finally:
            if should_close:
                client.close()
        return results[:limit]

    def _client_or_new(self) -> tuple[httpx.Client, bool]:
        if self._client is not None:
            return self._client, False
        return httpx.Client(timeout=self.timeout_seconds), True

    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self.service_role_key,
            "Authorization": f"Bearer {self.service_role_key}",
        }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Refresh individually-tracked pricecharting_catalog items via "
            "/api/product, for categories the daily bulk CSV refresh doesn't cover."
        )
    )
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--stale-after-hours", type=float, default=DEFAULT_STALE_AFTER_HOURS)
    parser.add_argument(
        "--sleep-between-requests-seconds", type=float, default=DEFAULT_SLEEP_SECONDS
    )
    parser.add_argument("--catalog-batch-size", type=int, default=100)
    parser.add_argument("--timeout-seconds", type=float, default=30)
    parser.add_argument("--api-token", default="", help="Defaults to PRICECHARTING_API_TOKEN.")
    parser.add_argument("--supabase-url", default="", help="Defaults to SUPABASE_URL.")
    parser.add_argument(
        "--service-role-key", default="", help="Defaults to SUPABASE_SERVICE_ROLE_KEY."
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
