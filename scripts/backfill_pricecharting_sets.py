import argparse
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from scripts.import_pricecharting_catalog import (
    SupabaseCatalogClient,
    load_rows_from_text,
    to_catalog_row,
)


REQUEST_HEADERS = {
    "User-Agent": "PackLoxSetDiscoveryBot/1.0 (+https://packlox.com; Legendary subscriber)",
}

CONSOLE_UID_PATTERN = re.compile(r'VGPC\.console_uid = "([^"]+)"')

SOURCE_SITE_BASE_URLS = {
    "pricecharting": "https://www.pricecharting.com",
    "sportscardspro": "https://www.sportscardspro.com",
}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    token = args.api_token or os.getenv("PRICECHARTING_API_TOKEN", "")
    if not token:
        raise SystemExit(
            "PRICECHARTING_API_TOKEN is required (or --api-token) -- even for "
            "--dry-run, since this worker makes real download-custom requests "
            "and only skips writing results."
        )

    registry_client = SupabaseRegistryOpsClient(
        supabase_url=args.supabase_url or os.getenv("SUPABASE_URL", ""),
        service_role_key=args.service_role_key or os.getenv("SUPABASE_SERVICE_ROLE_KEY", ""),
        timeout_seconds=args.timeout_seconds,
    )
    catalog_client = (
        None
        if args.dry_run
        else SupabaseCatalogClient(
            supabase_url=args.supabase_url or os.getenv("SUPABASE_URL", ""),
            service_role_key=args.service_role_key or os.getenv("SUPABASE_SERVICE_ROLE_KEY", ""),
            timeout_seconds=args.timeout_seconds,
        )
    )

    claimed_rows = registry_client.claim_rows(
        limit=args.limit,
        lease_minutes=args.lease_minutes,
        worker_id=args.worker_id,
    )
    print(f"Claimed {len(claimed_rows)} registry rows.", flush=True)
    if not claimed_rows:
        print(json.dumps({"success": True, "claimed": 0}, indent=2), flush=True)
        return 0

    source_downloaded_at = datetime.now(timezone.utc).isoformat()
    succeeded_ids: list[str] = []
    failed_rows: list[dict[str, Any]] = []
    total_catalog_rows = 0

    with httpx.Client(
        timeout=args.timeout_seconds,
        follow_redirects=True,
        headers=REQUEST_HEADERS,
    ) as http:
        resolved, resolve_failed = resolve_console_uids(
            claimed_rows,
            http=http,
            registry_client=registry_client,
            sleep_seconds=args.sleep_between_requests_seconds,
            dry_run=args.dry_run,
        )
        failed_rows.extend(resolve_failed)

        for source_site, rows in group_by_site(resolved).items():
            base_url = SOURCE_SITE_BASE_URLS[source_site]
            for index, chunk in enumerate(chunked(rows, args.batch_size)):
                if index > 0 and args.sleep_between_requests_seconds > 0:
                    time.sleep(args.sleep_between_requests_seconds)
                console_uids = [row["console_uid"] for row in chunk]
                print(
                    f"Fetching {source_site} batch of {len(chunk)} sets...",
                    flush=True,
                )
                csv_text = fetch_batch_csv(
                    http,
                    base_url=base_url,
                    token=token,
                    console_uids=console_uids,
                )
                if csv_text is None:
                    failed_rows.extend(chunk)
                    continue

                catalog_rows = [
                    to_catalog_row(row, f"{source_site}-set-backfill", source_downloaded_at)
                    for row in load_rows_from_text(csv_text)
                ]
                catalog_rows = [row for row in catalog_rows if row is not None]
                total_catalog_rows += len(catalog_rows)
                print(
                    f"  Parsed {len(catalog_rows)} catalog rows from this batch.",
                    flush=True,
                )
                if not args.dry_run and catalog_rows:
                    assert catalog_client is not None
                    catalog_client.sync_scd2_history_rows(catalog_rows, batch_size=args.catalog_batch_size)
                    catalog_client.upsert_rows(catalog_rows, batch_size=args.catalog_batch_size)

                succeeded_ids.extend(row["registry_id"] for row in chunk)

    if not args.dry_run:
        if succeeded_ids:
            registry_client.mark_success(succeeded_ids)
        if failed_rows:
            registry_client.mark_failure(failed_rows)

    print(
        json.dumps(
            {
                "success": True,
                "dryRun": args.dry_run,
                "claimed": len(claimed_rows),
                "succeeded": len(succeeded_ids),
                "failed": len(failed_rows),
                "catalogRowsWritten": 0 if args.dry_run else total_catalog_rows,
                "catalogRowsParsed": total_catalog_rows,
            },
            indent=2,
        ),
        flush=True,
    )
    return 0


def resolve_console_uids(
    rows: list[dict[str, Any]],
    *,
    http: httpx.Client,
    registry_client: "SupabaseRegistryOpsClient",
    sleep_seconds: float,
    dry_run: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    resolved: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    newly_resolved: list[dict[str, Any]] = []

    for index, row in enumerate(rows):
        if row.get("console_uid"):
            resolved.append(row)
            continue
        if index > 0 and sleep_seconds > 0:
            time.sleep(sleep_seconds)
        try:
            response = http.get(row["url"])
            response.raise_for_status()
        except httpx.HTTPError as exc:
            print(f"  Failed to resolve console_uid for {row['url']}: {exc}", flush=True)
            failed.append(row)
            continue
        match = CONSOLE_UID_PATTERN.search(response.text)
        if not match:
            print(f"  No console_uid found on {row['url']}.", flush=True)
            failed.append(row)
            continue
        row["console_uid"] = match.group(1)
        resolved.append(row)
        newly_resolved.append(row)

    if newly_resolved and not dry_run:
        registry_client.update_console_uids(newly_resolved)

    return resolved, failed


def group_by_site(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["source_site"], []).append(row)
    return grouped


def chunked(items: list[Any], size: int) -> list[list[Any]]:
    if size <= 0:
        raise ValueError("size must be greater than zero")
    return [items[i : i + size] for i in range(0, len(items), size)]


def fetch_batch_csv(
    http: httpx.Client,
    *,
    base_url: str,
    token: str,
    console_uids: list[str],
) -> str | None:
    try:
        response = http.get(
            f"{base_url}/price-guide/download-custom",
            params={"t": token, "console-uids": ",".join(console_uids)},
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"  Batch download failed for {len(console_uids)} sets: {exc}", flush=True)
        return None
    return response.text


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill PriceCharting/SportsCardsPro comics/coins/sports-card sets "
            "from pricecharting_set_registry into the shared pricecharting_catalog tables."
        )
    )
    parser.add_argument("--limit", type=int, default=200, help="Registry rows to claim this run.")
    parser.add_argument("--batch-size", type=int, default=150, help="Sets per download-custom request.")
    parser.add_argument(
        "--catalog-batch-size",
        type=int,
        default=100,
        help=(
            "Rows per pricecharting_catalog upsert request. Kept smaller than "
            "the history-insert batches: the upsert does a hash lookup plus a "
            "conflict-resolving write per row, so it hits Postgres's statement "
            "timeout at a much lower row count than a plain insert does."
        ),
    )
    parser.add_argument("--lease-minutes", type=float, default=30)
    parser.add_argument("--worker-id", default=os.getenv("RENDER_INSTANCE_ID", "backfill-worker"))
    parser.add_argument("--timeout-seconds", type=float, default=60)
    parser.add_argument(
        "--sleep-between-requests-seconds",
        type=float,
        default=2.0,
        help="Self-imposed pace between set-page resolves and batch CSV requests.",
    )
    parser.add_argument("--api-token", default="", help="Defaults to PRICECHARTING_API_TOKEN.")
    parser.add_argument("--supabase-url", default="", help="Defaults to SUPABASE_URL.")
    parser.add_argument("--service-role-key", default="", help="Defaults to SUPABASE_SERVICE_ROLE_KEY.")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


class SupabaseRegistryOpsClient:
    def __init__(self, *, supabase_url: str, service_role_key: str, timeout_seconds: float) -> None:
        self.supabase_url = supabase_url.strip().rstrip("/")
        self.service_role_key = service_role_key.strip()
        self.timeout_seconds = timeout_seconds
        if not self.supabase_url or not self.service_role_key:
            raise SystemExit("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required.")

    def claim_rows(self, *, limit: int, lease_minutes: float, worker_id: str) -> list[dict[str, Any]]:
        lease_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=lease_minutes)).isoformat()
        with httpx.Client(timeout=self.timeout_seconds) as client:
            response = client.get(
                f"{self.supabase_url}/rest/v1/pricecharting_set_registry",
                params={
                    "select": "registry_id,source_site,url,console_uid,failure_count",
                    "or": f"(claimed_at.is.null,claimed_at.lt.{lease_cutoff})",
                    "order": "priority_tier.asc,last_fetched_at.asc.nullsfirst",
                    "limit": str(limit),
                },
                headers=self._headers(),
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise SystemExit(
                    f"Supabase registry claim query failed with HTTP {response.status_code}: {response.text}"
                ) from exc
            rows = response.json()
            if not isinstance(rows, list) or not rows:
                return []

            registry_ids = [row["registry_id"] for row in rows]
            patch_response = client.patch(
                f"{self.supabase_url}/rest/v1/pricecharting_set_registry",
                params={"registry_id": f"in.({','.join(registry_ids)})"},
                headers={**self._headers(), "Prefer": "return=minimal"},
                json={"claimed_at": datetime.now(timezone.utc).isoformat(), "claimed_by": worker_id},
            )
            try:
                patch_response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise SystemExit(
                    f"Supabase registry claim patch failed with HTTP {patch_response.status_code}: {patch_response.text}"
                ) from exc
            return rows

    def update_console_uids(self, rows: list[dict[str, Any]]) -> None:
        # PATCH per row, not a batched upsert: PostgREST's upsert builds a full
        # candidate row to check NOT NULL constraints before it even looks at
        # the conflict, so a partial-column payload like {registry_id,
        # console_uid} fails against this table's NOT NULL columns even though
        # the row already exists. PATCH only ever touches the columns sent.
        with httpx.Client(timeout=self.timeout_seconds) as client:
            for row in rows:
                response = client.patch(
                    f"{self.supabase_url}/rest/v1/pricecharting_set_registry",
                    params={"registry_id": f"eq.{row['registry_id']}"},
                    headers={**self._headers(), "Prefer": "return=minimal"},
                    json={"console_uid": row["console_uid"]},
                )
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise SystemExit(
                        f"Supabase console_uid update failed for {row['registry_id']} "
                        f"with HTTP {response.status_code}: {response.text}"
                    ) from exc

    def mark_success(self, registry_ids: list[str]) -> None:
        with httpx.Client(timeout=self.timeout_seconds) as client:
            response = client.patch(
                f"{self.supabase_url}/rest/v1/pricecharting_set_registry",
                params={"registry_id": f"in.({','.join(registry_ids)})"},
                headers={**self._headers(), "Prefer": "return=minimal"},
                json={
                    "last_fetched_at": datetime.now(timezone.utc).isoformat(),
                    "last_fetch_status": "success",
                    "failure_count": 0,
                    "claimed_at": None,
                    "claimed_by": None,
                },
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise SystemExit(
                    f"Supabase mark-success failed with HTTP {response.status_code}: {response.text}"
                ) from exc

    def mark_failure(self, rows: list[dict[str, Any]]) -> None:
        # PATCH per row (see update_console_uids for why a batched upsert
        # can't be used here) -- each row needs its own failure_count value.
        with httpx.Client(timeout=self.timeout_seconds) as client:
            for row in rows:
                response = client.patch(
                    f"{self.supabase_url}/rest/v1/pricecharting_set_registry",
                    params={"registry_id": f"eq.{row['registry_id']}"},
                    headers={**self._headers(), "Prefer": "return=minimal"},
                    json={
                        "last_fetched_at": datetime.now(timezone.utc).isoformat(),
                        "last_fetch_status": "error",
                        "failure_count": int(row.get("failure_count") or 0) + 1,
                        "claimed_at": None,
                        "claimed_by": None,
                    },
                )
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise SystemExit(
                        f"Supabase mark-failure update failed for {row['registry_id']} "
                        f"with HTTP {response.status_code}: {response.text}"
                    ) from exc

    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self.service_role_key,
            "Authorization": f"Bearer {self.service_role_key}",
            "Content-Type": "application/json",
        }


if __name__ == "__main__":
    raise SystemExit(main())
