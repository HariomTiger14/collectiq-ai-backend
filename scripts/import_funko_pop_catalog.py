"""Import the open-source Funko Pop dataset into funko_pop_catalog.

Source: github.com/kennymkchan/funko-pop-data (MIT licensed), a single
static JSON file -- no API, no key, no rate limit. Confirmed live at
23,940 entries with 100% image coverage before this script was written.

Unlike the PriceCharting/KicksDB pipelines, this is NOT a pricing source:
funko_pop_catalog exists purely so catalog_search_service.py can enrich
PriceCharting-sourced Funko Pop rows (which have real pricing but no
images) with a real product photo. See the table migration
(20260816_create_funko_pop_catalog.sql) and the enrichment lookup in
catalog_search_service.py for how this is actually used.

`handle`/`title` are not unique in the source data -- the same character
can appear across multiple product types (vinyl figure, pin, apparel).
Every row is imported as-is; disambiguating which one to use for a given
PriceCharting row happens at lookup time, not at import time.
"""

import argparse
import hashlib
import json
import os
import re
from typing import Any

import httpx


DEFAULT_SOURCE_URL = (
    "https://raw.githubusercontent.com/kennymkchan/funko-pop-data/"
    "master/funko_pop.json"
)
DEFAULT_BATCH_SIZE = 500


def normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", title or "").strip().lower()


def content_hash_for(row: dict[str, Any]) -> str:
    payload = "|".join(
        [
            str(row.get("handle") or ""),
            str(row.get("title") or ""),
            str(row.get("imageName") or ""),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def fetch_source_rows(source_url: str, timeout_seconds: float) -> list[dict[str, Any]]:
    with httpx.Client(timeout=timeout_seconds) as client:
        response = client.get(source_url)
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, list):
        raise SystemExit("Unexpected dataset shape: expected a JSON array at the top level.")
    return [row for row in payload if isinstance(row, dict)]


def to_catalog_row(source_row: dict[str, Any]) -> dict[str, Any] | None:
    title = str(source_row.get("title") or "").strip()
    image_url = str(source_row.get("imageName") or "").strip()
    handle = str(source_row.get("handle") or "").strip()
    if not title or not image_url or not handle:
        return None
    series = source_row.get("series")
    if not isinstance(series, list):
        series = []
    row = {
        "handle": handle,
        "title": title,
        "normalized_title": normalize_title(title),
        "image_url": image_url,
        "series": series,
    }
    row["content_hash"] = content_hash_for(source_row)
    return row


class FunkoPopCatalogClient:
    def __init__(self, *, supabase_url: str, service_role_key: str, timeout_seconds: float) -> None:
        self.supabase_url = supabase_url.strip().rstrip("/")
        self.service_role_key = service_role_key.strip()
        self.timeout_seconds = timeout_seconds
        if not self.supabase_url or not self.service_role_key:
            raise SystemExit("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required.")

    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self.service_role_key,
            "Authorization": f"Bearer {self.service_role_key}",
            "Content-Type": "application/json",
        }

    def upsert_rows(self, rows: list[dict[str, Any]], *, batch_size: int) -> int:
        if batch_size <= 0:
            raise ValueError("batch_size must be greater than zero")
        total = 0
        with httpx.Client(timeout=self.timeout_seconds) as client:
            for index in range(0, len(rows), batch_size):
                batch = rows[index : index + batch_size]
                response = client.post(
                    f"{self.supabase_url}/rest/v1/funko_pop_catalog",
                    params={"on_conflict": "content_hash"},
                    headers={
                        **self._headers(),
                        "Prefer": "resolution=ignore-duplicates,return=minimal",
                    },
                    json=batch,
                )
                if response.status_code >= 400:
                    print(
                        f"  Batch {index}-{index + len(batch)} failed: "
                        f"HTTP {response.status_code} {response.text[:300]}",
                        flush=True,
                    )
                    continue
                total += len(batch)
                print(f"Upserted {total}/{len(rows)} rows...", flush=True)
        return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-url", default=DEFAULT_SOURCE_URL)
    parser.add_argument("--supabase-url", default="", help="Defaults to SUPABASE_URL.")
    parser.add_argument(
        "--service-role-key", default="", help="Defaults to SUPABASE_SERVICE_ROLE_KEY."
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and transform the dataset but do not write to Supabase.",
    )
    args = parser.parse_args(argv)

    supabase_url = args.supabase_url or os.getenv("SUPABASE_URL", "")
    service_role_key = args.service_role_key or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

    print(f"Fetching {args.source_url} ...", flush=True)
    source_rows = fetch_source_rows(args.source_url, args.timeout_seconds)
    print(f"Fetched {len(source_rows)} source rows.", flush=True)

    catalog_rows = [row for row in (to_catalog_row(r) for r in source_rows) if row is not None]
    skipped = len(source_rows) - len(catalog_rows)
    if skipped:
        print(f"Skipped {skipped} source rows missing title/image/handle.", flush=True)

    if args.dry_run:
        print(f"Dry run: would upsert {len(catalog_rows)} rows. Sample:", flush=True)
        for row in catalog_rows[:3]:
            print(" ", json.dumps(row), flush=True)
        return 0

    client = FunkoPopCatalogClient(
        supabase_url=supabase_url,
        service_role_key=service_role_key,
        timeout_seconds=args.timeout_seconds,
    )
    written = client.upsert_rows(catalog_rows, batch_size=args.batch_size)
    print(f"Done. Upserted {written}/{len(catalog_rows)} rows.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
