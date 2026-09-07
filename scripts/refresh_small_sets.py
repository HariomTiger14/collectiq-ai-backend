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
from typing import Any, NamedTuple

import httpx

from scripts._ops_run_recorder import dump_and_report, run_with_recorder
from scripts.tier1_eligibility import (
    OK as TIER1_OK,
    classify_search_result,
    plan_registry_update,
)
from scripts.backfill_pricecharting_sets import (
    API_SEARCH_RESULT_CAP,
    REQUEST_HEADERS,
    SOURCE_SITE_BASE_URLS,
    _search_products,
    write_catalog_rows,
)
from scripts.import_pricecharting_catalog import (
    timeout_retry_summary,
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
        result = refresh_small_sets(
            http,
            candidates,
            token=token,
            sleep_seconds=args.sleep_between_requests_seconds,
            source_downloaded_at=source_downloaded_at,
        )

    written = True
    if not args.dry_run and result.catalog_rows:
        assert catalog_client is not None
        written = write_catalog_rows(
            catalog_client, result.catalog_rows, batch_size=args.catalog_batch_size
        )

    # Every attempted candidate gets its check timestamp bumped regardless of
    # outcome (refreshed, too large, empty, or a transient error) -- this is
    # what throttles re-checking already-known-large sets to once per
    # staleness window instead of every run. A transient error just means
    # that set waits the full window before its next attempt too, an
    # acceptable tradeoff for a browsing-freshness nice-to-have, not
    # something tracking a user's own data.
    if not args.dry_run and result.checked_ids:
        reader.mark_tier1_checked(result.checked_ids)

    # Eligibility is recorded separately from the check timestamp: the stamp
    # says "we looked", this says "and text search cannot serve this set".
    if not args.dry_run and result.eligibility_updates:
        reader.apply_tier1_eligibility(result.eligibility_updates)

    excluded_count = reader.count_excluded(stale_before=stale_before)

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
                **timeout_retry_summary(catalog_client),
                "refreshedSets": len(result.refreshed_ids) if written else 0,
                "skippedNotEligible": result.skipped,
                # How many sets this run set aside, versus how many it
                # never had to ask about because a previous run did.
                "tier1MarkedIneligible": sum(
                    1 for update in result.eligibility_updates.values()
                    if update.get("tier1_refresh_eligible") is False
                ),
                "tier1SkippedIneligible": excluded_count,
                "tier1IneligibleReasons": result.ineligible_reasons,
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
    # registry_id -> tier1_* patch, applied by the caller. Collected rather
    # than written here so this function stays free of I/O and testable.
    eligibility_updates: dict[str, dict[str, Any]] = {}
    reasons: dict[str, int] = {}
    for index, row in enumerate(candidates):
        if index > 0 and sleep_seconds > 0:
            time.sleep(sleep_seconds)
        base_url = SOURCE_SITE_BASE_URLS[row["source_site"]]
        status_sink: list[int] = []
        products = _search_products(
            http,
            base_url=base_url,
            token=token,
            query=row.get("set_name") or "",
            status_sink=status_sink,
        )
        checked_ids.append(row["registry_id"])
        outcome = classify_search_result(
            products,
            set_name=row.get("set_name"),
            http_status=status_sink[-1] if status_sink else None,
        )
        if outcome != TIER1_OK:
            reasons[outcome] = reasons.get(outcome, 0) + 1
            update = plan_registry_update(
                outcome, current_miss_count=int(row.get("tier1_miss_count") or 0)
            )
            if update:
                eligibility_updates[row["registry_id"]] = update
        if products is None or not (0 < len(products) < API_SEARCH_RESULT_CAP):
            # Empty, errored, or hit the cap (ambiguous/truncated) -- not
            # safe to trust as a complete refresh. Leave this set's existing
            # catalog rows untouched; the slow CSV/console_uid backfill path
            # remains the source of truth for it.
            skipped += 1
            continue
        if outcome != TIER1_OK:
            # Every returned product belongs to some other set: the query
            # resolves elsewhere, so writing these would file another set's
            # prices under this one.
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
        cleared = plan_registry_update(
            TIER1_OK, current_miss_count=int(row.get("tier1_miss_count") or 0)
        )
        if cleared:
            eligibility_updates[row["registry_id"]] = cleared
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
    return SmallSetRefreshResult(
        catalog_rows, refreshed_ids, checked_ids, skipped, eligibility_updates, reasons
    )


class SmallSetRefreshResult(NamedTuple):
    """What one pass over the candidates produced.

    A NamedTuple rather than a bare tuple: this grew from four values to six
    when tier-1 eligibility was added, and positional unpacking of six things
    is a miscount waiting to happen.
    """

    catalog_rows: list[dict[str, Any]]
    refreshed_ids: list[str]
    checked_ids: list[str]
    skipped: int
    eligibility_updates: dict[str, dict[str, Any]]
    ineligible_reasons: dict[str, int]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
                    "select": "registry_id,source_site,set_name,tier1_miss_count",
                    "last_fetch_status": "eq.success",
                    "or": f"(tier1_refreshed_at.is.null,tier1_refreshed_at.lt.{stale_before})",
                    # Eligible, OR set aside but past its recheck date. A set
                    # is never excluded permanently: vendor catalogs change,
                    # so a set over the 100-item cap today may be searchable
                    # later, and a bad fuzzy match can be fixed upstream.
                    "and": (
                        "(or(tier1_refresh_eligible.is.true,"
                        f"tier1_recheck_after.lte.{_now_iso()}))"
                    ),
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

    def count_excluded(self, *, stale_before: str) -> int:
        """How many stale sets this run never had to ask about.

        Reported so the saving is visible: without it, a run that skips 200
        known-bad sets looks identical to one that had only a few candidates.
        """
        client, should_close = self._client_or_new()
        try:
            response = client.get(
                f"{self.supabase_url}/rest/v1/pricecharting_set_registry",
                params={
                    "select": "registry_id",
                    "last_fetch_status": "eq.success",
                    "tier1_refresh_eligible": "is.false",
                    "or": f"(tier1_recheck_after.is.null,tier1_recheck_after.gt.{_now_iso()})",
                    "limit": "1",
                },
                headers={**self._headers(), "Prefer": "count=exact"},
            )
            response.raise_for_status()
            content_range = response.headers.get("content-range", "")
            return int(content_range.split("/")[-1]) if "/" in content_range else 0
        except Exception:
            # A reporting nicety must never fail the run.
            return 0
        finally:
            if should_close:
                client.close()

    def apply_tier1_eligibility(self, updates: dict[str, dict[str, Any]]) -> None:
        """Write the tier1_* patches produced by the refresh loop.

        Grouped by identical patch so a run of 200 sets marked for the same
        reason costs a handful of PATCHes rather than 200. Tier-3 columns are
        never in these payloads -- plan_registry_update only emits tier1_*,
        and a test asserts it.
        """
        if not updates:
            return
        grouped: dict[str, list[str]] = {}
        payloads: dict[str, dict[str, Any]] = {}
        for registry_id, patch in updates.items():
            key = json.dumps(patch, sort_keys=True)
            grouped.setdefault(key, []).append(registry_id)
            payloads[key] = patch
        client, should_close = self._client_or_new()
        try:
            for key, ids in grouped.items():
                response = client.patch(
                    f"{self.supabase_url}/rest/v1/pricecharting_set_registry",
                    params={"registry_id": f"in.({','.join(ids)})"},
                    headers={**self._headers(), "Prefer": "return=minimal"},
                    json=payloads[key],
                )
                response.raise_for_status()
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
