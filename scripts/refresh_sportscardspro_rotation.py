"""Tier-3 rotation: keep every sports card's price moving through history.

The sportscardspro first pass is complete -- all ~36,700 sets fetched once,
~10.5M catalog rows -- but backfill_pricecharting_sets.py's claim_rows()
permanently excludes a set once last_fetch_status='success' (deliberate:
initial completeness, not recurring freshness). The two existing refresh
tiers only cover slices of it: tier 1 (refresh_small_sets.py) re-fetches
sets that fit under the /api/products 100-result cap, and tier 2
(refresh_tracked_catalog_items.py) covers individually-tracked items. A
sample showed ~0.03% of the bulk sports rows touched within 2 days --
for most sports cards, price "history" was a single backfill-time point.

This script closes that gap by rotating through EVERY completed
sportscardspro set, oldest-refreshed first, via the same console_uid +
download-custom CSV path the backfill used. No claim/lease machinery: the
population is fixed and known, ordering by tier3_refreshed_at (nulls
first) IS the queue, and each set is stamped after a successful write, so
concurrent runs at worst re-fetch a few sets whose writes are
content-hash-diffed no-ops (see SupabaseCatalogClient.upsert_rows).

Small sets are rotated too, not filtered out. Tier 1's
tier1_refreshed_at can't distinguish "refreshed" from "checked and
skipped as too large" (it stamps every attempted candidate), so there is
no cheap size signal in the registry -- and a small set's CSV is small,
so including them costs little and keeps this script's logic trivial.

Pacing is the hard constraint, inherited from the backfill's live
findings (see SPORTSCARDSPRO_DEFAULT_* there): sportscardspro.com's
Cloudflare throttling tolerates ~15s spacing sustained, batches must stay
tiny (a 20-set batch once OOM-killed a 512Mi instance -- sports sets run
huge), and a 429 breaker stops the run rather than digging in. At the
default --max-requests 220 x 3 sets, one run covers ~660 sets in ~55
minutes; hourly runs cycle the full registry in ~2.3 days, i.e. every
sports card gets a fresh price point (and a history row when it moved)
every 2-3 days.

The 15-minute backfill cron shares this throttle budget but has been a
no-op since the first pass completed; it only wakes when the weekly
discover job registers new sets. Both scripts carry the breaker, so a
collision degrades to a skipped run, not a ban.
"""

import argparse
import json
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from scripts.backfill_pricecharting_sets import (
    REQUEST_HEADERS,
    SOURCE_SITE_BASE_URLS,
    SPORTSCARDSPRO_DEFAULT_BATCH_SIZE,
    SPORTSCARDSPRO_DEFAULT_MAX_ATTEMPTS,
    SPORTSCARDSPRO_DEFAULT_SLEEP_SECONDS,
    _Counter,
    _RateLimitCircuitBreaker,
    fetch_batch_csv_with_retry,
    write_catalog_rows,
)
from scripts.import_pricecharting_catalog import (
    SupabaseCatalogClient,
    load_rows_from_text,
    to_catalog_row,
)

SOURCE_FILE_TAG = "sportscardspro-tier3-refresh"
REGISTRY_PAGE_SIZE = 1000
# Trip after this many consecutive rate-limited batches. Low on purpose:
# the backfill's live testing showed 429s recur in clusters once the
# sustained-volume limit is hit, and continuing just extends the cluster.
RATE_LIMIT_BREAKER_THRESHOLD = 3


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
    registry = Tier3RegistryClient(
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

    set_budget = args.max_requests * args.batch_size
    rows = registry.fetch_rotation_rows(limit=set_budget)
    print(
        f"Rotating {len(rows)} of the oldest-refreshed sportscardspro set(s) "
        f"(budget: {args.max_requests} requests x {args.batch_size} sets).",
        flush=True,
    )
    if not rows:
        print(json.dumps({"success": True, "setsConsidered": 0}, indent=2), flush=True)
        return 0

    source_downloaded_at = datetime.now(timezone.utc).isoformat()
    base_url = SOURCE_SITE_BASE_URLS["sportscardspro"]
    breaker = _RateLimitCircuitBreaker(RATE_LIMIT_BREAKER_THRESHOLD)
    rate_limit_counter = _Counter()
    total_catalog_rows = 0
    failed_batches = 0
    refreshed_ids: list[str] = []

    with httpx.Client(
        timeout=args.timeout_seconds,
        follow_redirects=True,
        headers=REQUEST_HEADERS,
    ) as http:
        for index in range(0, len(rows), args.batch_size):
            if breaker.tripped:
                print(
                    "Rate-limit breaker tripped -- stopping this run; the "
                    "unstamped sets stay at the front of the rotation.",
                    flush=True,
                )
                break
            chunk = rows[index : index + args.batch_size]
            if index > 0:
                time.sleep(args.sleep_between_requests_seconds)
            console_uids = [row["console_uid"] for row in chunk]
            before_429s = rate_limit_counter.value
            csv_text = fetch_batch_csv_with_retry(
                http,
                base_url=base_url,
                token=token,
                console_uids=console_uids,
                max_attempts=args.max_attempts,
                retry_sleep_seconds=args.sleep_between_requests_seconds,
                rate_limit_counter=rate_limit_counter,
            )
            if csv_text is None:
                failed_batches += 1
                if rate_limit_counter.value > before_429s:
                    breaker.record_rate_limited()
                continue
            breaker.record_success()

            catalog_rows = [
                to_catalog_row(row, SOURCE_FILE_TAG, source_downloaded_at)
                for row in load_rows_from_text(csv_text)
            ]
            catalog_rows = [row for row in catalog_rows if row is not None]
            total_catalog_rows += len(catalog_rows)

            if not args.dry_run and catalog_rows:
                assert catalog_client is not None
                if not write_catalog_rows(
                    catalog_client, catalog_rows, batch_size=args.catalog_batch_size
                ):
                    failed_batches += 1
                    continue
            # Stamp only after the write lands: a failed batch keeps its
            # sets at the front of the rotation for the next run.
            refreshed_ids.extend(row["registry_id"] for row in chunk)

    if not args.dry_run and refreshed_ids:
        registry.mark_tier3_refreshed(refreshed_ids)

    print(
        json.dumps(
            {
                "success": True,
                "dryRun": args.dry_run,
                "setsConsidered": len(rows),
                "setsRefreshed": len(refreshed_ids),
                "failedBatches": failed_batches,
                "rateLimited429s": rate_limit_counter.value,
                "breakerTripped": breaker.tripped,
                "catalogRowsParsed": total_catalog_rows,
                "catalogRowsWritten": 0 if args.dry_run else total_catalog_rows,
            },
            indent=2,
        ),
        flush=True,
    )
    return 0


class Tier3RegistryClient:
    def __init__(self, *, supabase_url: str, service_role_key: str, timeout_seconds: float) -> None:
        self.supabase_url = supabase_url.strip().rstrip("/")
        self.service_role_key = service_role_key.strip()
        self.timeout_seconds = timeout_seconds
        if not self.supabase_url or not self.service_role_key:
            raise SystemExit("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required.")

    def fetch_rotation_rows(self, *, limit: int) -> list[dict[str, Any]]:
        """Oldest-refreshed completed sportscardspro sets, never-refreshed
        first. nullsfirst makes the initial full cycle drain every
        never-stamped set before any second visit happens."""
        rows: list[dict[str, Any]] = []
        offset = 0
        with httpx.Client(timeout=self.timeout_seconds) as client:
            while len(rows) < limit:
                page_limit = min(REGISTRY_PAGE_SIZE, limit - len(rows))
                response = client.get(
                    f"{self.supabase_url}/rest/v1/pricecharting_set_registry",
                    params={
                        "select": "registry_id,console_uid,set_name",
                        "source_site": "eq.sportscardspro",
                        "last_fetch_status": "eq.success",
                        "console_uid": "not.is.null",
                        "order": "tier3_refreshed_at.asc.nullsfirst,registry_id.asc",
                        "limit": str(page_limit),
                        "offset": str(offset),
                    },
                    headers=self._headers(),
                )
                response.raise_for_status()
                page = response.json()
                if not isinstance(page, list) or not page:
                    break
                rows.extend(page)
                if len(page) < page_limit:
                    break
                offset += len(page)
        return rows

    def mark_tier3_refreshed(self, registry_ids: list[str]) -> None:
        stamped_at = datetime.now(timezone.utc).isoformat()
        with httpx.Client(timeout=self.timeout_seconds) as client:
            for index in range(0, len(registry_ids), REGISTRY_PAGE_SIZE):
                chunk = registry_ids[index : index + REGISTRY_PAGE_SIZE]
                response = client.patch(
                    f"{self.supabase_url}/rest/v1/pricecharting_set_registry",
                    params={"registry_id": f"in.({','.join(chunk)})"},
                    json={"tier3_refreshed_at": stamped_at},
                    headers={**self._headers(), "Prefer": "return=minimal"},
                )
                response.raise_for_status()

    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self.service_role_key,
            "Authorization": f"Bearer {self.service_role_key}",
            "Content-Type": "application/json",
        }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Tier-3 rotation: re-download every completed sportscardspro set "
            "oldest-refreshed-first via console_uid + download-custom CSV, so "
            "all sports cards accumulate price history (full cycle ~2-3 days)."
        )
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        default=220,
        help="download-custom requests per run; x batch-size = sets per run.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=SPORTSCARDSPRO_DEFAULT_BATCH_SIZE,
        help="Sets per download-custom request. Keep tiny: a 20-set batch has "
        "OOM-killed a 512Mi instance before (sports sets run huge).",
    )
    parser.add_argument(
        "--catalog-batch-size",
        type=int,
        default=150,
        help="Rows per catalog upsert. 500 (the completed-categories "
        "default) was observed hitting the database statement timeout "
        "(57014) here -- each upserted row updates five GIN trigram "
        "indexes plus the browse btrees, and sports-card batches skew "
        "large. 150 keeps every sub-batch comfortably under it.",
    )
    parser.add_argument("--timeout-seconds", type=float, default=60)
    parser.add_argument(
        "--sleep-between-requests-seconds",
        type=float,
        default=SPORTSCARDSPRO_DEFAULT_SLEEP_SECONDS,
        help="sportscardspro.com Cloudflare pacing -- see backfill_pricecharting_sets.py.",
    )
    parser.add_argument("--max-attempts", type=int, default=SPORTSCARDSPRO_DEFAULT_MAX_ATTEMPTS)
    parser.add_argument("--api-token", default="", help="Defaults to PRICECHARTING_API_TOKEN.")
    parser.add_argument("--supabase-url", default="", help="Defaults to SUPABASE_URL.")
    parser.add_argument(
        "--service-role-key", default="", help="Defaults to SUPABASE_SERVICE_ROLE_KEY."
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
