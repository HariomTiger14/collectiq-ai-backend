"""Keeps small (<100-item) sets fresh in the catalog even when nobody
tracks anything in them yet -- tier 1 of the tiered refresh design.

Why this exists: pricecharting_set_registry rows are permanently excluded
from backfill's claim query once last_fetch_status='success' (see
claim_rows() in backfill_pricecharting_sets.py) -- a deliberate design for
initial completeness, not recurring freshness. scripts/refresh_tracked_
catalog_items.py (tier 2) already keeps individually-tracked items fresh,
but an untracked item just sits at its one-time backfill snapshot forever.

For sets small enough to fit under PriceCharting's /api/products search cap
(confirmed elsewhere in this codebase to be unblocked on both
pricecharting.com and sportscardspro.com, unlike the CSV/console_uid
endpoints), a single search call re-fetches the WHOLE set in one shot --
cheap enough to do periodically for every already-backfilled small set, not
just ones someone owns. Large sets (>=100 items, or ones the search comes
back empty/ambiguous for) are left alone -- they stay on the slow CSV/
console_uid backfill path, which this script never touches.
"""

import argparse
import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from scripts._ops_run_recorder import dump_and_report, run_with_recorder
from scripts.backfill_pricecharting_sets import (
    API_SEARCH_RESULT_CAP,
    REQUEST_HEADERS,
    SOURCE_SITE_BASE_URLS,
    _search_products,
    write_catalog_rows,
)
from scripts.import_pricecharting_catalog import (
    SupabaseCatalogClient,
    dedupe_catalog_rows,
    to_catalog_row,
    to_catalog_row_from_api_product,
)


DEFAULT_STALE_AFTER_HOURS = 24.0
DEFAULT_LIMIT = 300
# PriceCharting's documented API limit is 1 call/sec, shared per subscriber
# token across both sites -- same conservative pacing as the tier-2 tracked-
# item refresh script.
DEFAULT_SLEEP_SECONDS = 1.2


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    token = args.api_token or os.getenv("PRICECHARTING_API_TOKEN", "")
    if not token:
        raise SystemExit(
            "PRICECHARTING_API_TOKEN is required (or --api-token) -- even for "
            "--dry-run, since this worker makes real /api/products requests "
            "and only skips writing results."
        )

    supabase_url = args.supabase_url or os.getenv("SUPABASE_URL", "")
    service_role_key = args.service_role_key or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    reader = SmallSetRegistryReader(
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

    stale_before = _stale_cutoff_iso(args.stale_after_hours)
    candidates = reader.fetch_stale_success_rows(stale_before=stale_before, limit=args.limit)
    print(
        f"{len(candidates)} candidate set(s) due for a freshness check (stale before {stale_before}).",
        flush=True,
    )

    source_downloaded_at = datetime.now(timezone.utc).isoformat()
    with httpx.Client(
        timeout=args.timeout_seconds, follow_redirects=True, headers=REQUEST_HEADERS
    ) as http:
        catalog_rows, refreshed_ids, checked_ids, skipped = refresh_small_sets(
            http,
            candidates,
            token=token,
            sleep_seconds=args.sleep_between_requests_seconds,
            source_downloaded_at=source_downloaded_at,
        )

    written = True
    if not args.dry_run and catalog_rows:
        assert catalog_client is not None
        written = write_catalog_rows(catalog_client, catalog_rows, batch_size=args.catalog_batch_size)

    # Every attempted candidate gets its check timestamp bumped regardless of
    # outcome (refreshed, too large, empty, or a transient error) -- this is
    # what throttles re-checking already-known-large sets to once per
    # staleness window instead of every run. A transient error just means
    # that set waits the full window before its next attempt too, an
    # acceptable tradeoff for a browsing-freshness nice-to-have, not
    # something tracking a user's own data.
    if not args.dry_run and checked_ids:
        reader.mark_tier1_checked(checked_ids)

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
                "candidates": len(candidates),
                "refreshedSets": len(refreshed_ids) if written else 0,
                "skippedNotEligible": skipped,
                # Was len(catalog_rows) -- every parsed row, not the rows
                # actually written. The accumulator reports the real split.
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


def refresh_small_sets(
    http: httpx.Client,
    candidates: list[dict[str, Any]],
    *,
    token: str,
    sleep_seconds: float,
    source_downloaded_at: str,
) -> tuple[list[dict[str, Any]], list[str], list[str], int]:
    catalog_rows: list[dict[str, Any]] = []
    refreshed_ids: list[str] = []
    checked_ids: list[str] = []
    skipped = 0
    for index, row in enumerate(candidates):
        if index > 0 and sleep_seconds > 0:
            time.sleep(sleep_seconds)
        base_url = SOURCE_SITE_BASE_URLS[row["source_site"]]
        products = _search_products(
            http, base_url=base_url, token=token, query=row.get("set_name") or ""
        )
        checked_ids.append(row["registry_id"])
        if products is None or not (0 < len(products) < API_SEARCH_RESULT_CAP):
            # Empty, errored, or hit the cap (ambiguous/truncated) -- not
            # safe to trust as a complete refresh. Leave this set's existing
            # catalog rows untouched; the slow CSV/console_uid backfill path
            # remains the source of truth for it.
            skipped += 1
            continue
        set_catalog_rows = [
            to_catalog_row_from_api_product(product, f"{row['source_site']}-tier1-refresh", source_downloaded_at)
            for product in products
        ]
        set_catalog_rows = [catalog_row for catalog_row in set_catalog_rows if catalog_row is not None]
        if not set_catalog_rows:
            skipped += 1
            continue
        catalog_rows.extend(set_catalog_rows)
        refreshed_ids.append(row["registry_id"])
    # Unlike backfill's per-set CSV (scoped to exactly one set), tier 1
    # searches by text -- PriceCharting's fuzzy /api/products?q= match can
    # return an item that actually belongs to a DIFFERENT set (e.g.
    # searching "Creepshow" surfaced a "Stray Dogs: Dog Days [Creepshow]"
    # crossover item). If that other set is also a candidate in this same
    # run, the same pricecharting_id lands in catalog_rows twice, and the
    # SCD2 history table's one-current-row-per-item unique constraint
    # rejects the second insert (live-confirmed: 23505 duplicate key).
    # Dedupe by pricecharting_id before returning -- both occurrences
    # describe the same real item fetched moments apart, so either is fine
    # to keep.
    catalog_rows = dedupe_catalog_rows(catalog_rows)
    return catalog_rows, refreshed_ids, checked_ids, skipped


def _stale_cutoff_iso(hours: float, *, now: datetime | None = None) -> str:
    reference = now or datetime.now(timezone.utc)
    return (reference - timedelta(hours=hours)).isoformat()


class SmallSetRegistryReader:
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

    def fetch_stale_success_rows(self, *, stale_before: str, limit: int) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        client, should_close = self._client_or_new()
        try:
            response = client.get(
                f"{self.supabase_url}/rest/v1/pricecharting_set_registry",
                params={
                    "select": "registry_id,source_site,set_name",
                    "last_fetch_status": "eq.success",
                    "or": f"(tier1_refreshed_at.is.null,tier1_refreshed_at.lt.{stale_before})",
                    "order": "tier1_refreshed_at.asc.nullsfirst",
                    "limit": str(limit),
                },
                headers=self._headers(),
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                return []
            return [row for row in payload if isinstance(row, dict)]
        finally:
            if should_close:
                client.close()

    def mark_tier1_checked(self, registry_ids: list[str]) -> None:
        if not registry_ids:
            return
        client, should_close = self._client_or_new()
        try:
            response = client.patch(
                f"{self.supabase_url}/rest/v1/pricecharting_set_registry",
                params={"registry_id": f"in.({','.join(registry_ids)})"},
                headers={**self._headers(), "Prefer": "return=minimal"},
                json={"tier1_refreshed_at": datetime.now(timezone.utc).isoformat()},
            )
            response.raise_for_status()
        finally:
            if should_close:
                client.close()

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
            "Refresh small (<100-item) pricecharting_set_registry sets via "
            "/api/products search, regardless of tracking status."
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
    raise SystemExit(run_with_recorder("small-sets-refresh", main))
