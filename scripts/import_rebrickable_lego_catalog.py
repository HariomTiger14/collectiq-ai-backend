"""Import LEGO set product images from Rebrickable into rebrickable_lego_catalog.

Source: rebrickable.com/downloads -- a free, public, no-API-key bulk CSV
export (sets.csv.gz) of Rebrickable's entire LEGO set database. Confirmed
live before this script was written: 28,099 real sets, 100% image coverage
(every row has a real, working img_url), no signup required.

Unlike Pokemon cards, a LEGO set number is a unique retail product
identifier, not a card+print-run pair -- there's no "which print variant"
ambiguity to guard against here. There IS a different real risk, spot-
checked live: LEGO has reused old set numbers across unrelated product
lines over the decades (e.g. PriceCharting's "Roof Bricks #445" collides
on number with Rebrickable's unrelated "Police Units" set). Matching on
set number alone measured ~96% but included real false positives; this is
why catalog_search_service.py's LEGO enrichment also requires the
PriceCharting title's own words to overlap with the matched Rebrickable
set name, not just the number -- see that file's _enrich_with_lego_image.
This script only stores the raw ingredients (base_number, name, image_url)
for that check to run against; it does no matching itself.

Re-running this script UPDATES existing rows (Rebrickable's own export is
periodically refreshed with new sets), keyed on Rebrickable's own set_num.
"""

import argparse
import csv
import gzip
import io
import os
import re
from typing import Any

import httpx


DEFAULT_SOURCE_URL = "https://cdn.rebrickable.com/media/downloads/sets.csv.gz"
DEFAULT_BATCH_SIZE = 500
_LEADING_ZEROS_RE = re.compile(r"^0+(?=\d)")


def base_number_from_set_num(set_num: str) -> str | None:
    # Rebrickable set_num looks like "7322-1" (set number, dash, variant).
    # PriceCharting embeds only the bare set number ("#7322"), no variant
    # suffix and no leading zeros, so both sides are normalized to match.
    first_part = set_num.split("-")[0].strip()
    if not first_part.isdigit():
        return None
    return _LEADING_ZEROS_RE.sub("", first_part) or "0"


def to_catalog_row(source_row: dict[str, Any]) -> dict[str, Any] | None:
    set_num = str(source_row.get("set_num") or "").strip()
    name = str(source_row.get("name") or "").strip()
    image_url = str(source_row.get("img_url") or "").strip()
    if not set_num or not name or not image_url:
        return None
    base_number = base_number_from_set_num(set_num)
    if base_number is None:
        return None
    year_raw = str(source_row.get("year") or "").strip()
    row: dict[str, Any] = {
        "set_num": set_num,
        "base_number": base_number,
        "name": name,
        "image_url": image_url,
    }
    if year_raw.isdigit():
        row["year"] = int(year_raw)
    return row


def fetch_source_rows(source_url: str, timeout_seconds: float) -> list[dict[str, Any]]:
    with httpx.Client(timeout=timeout_seconds) as client:
        response = client.get(source_url)
        response.raise_for_status()
        raw_bytes = response.content
    decompressed = gzip.decompress(raw_bytes)
    text = io.StringIO(decompressed.decode("utf-8"))
    return list(csv.DictReader(text))


class RebrickableLegoCatalogClient:
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
                    f"{self.supabase_url}/rest/v1/rebrickable_lego_catalog",
                    params={"on_conflict": "set_num"},
                    headers={
                        **self._headers(),
                        "Prefer": "resolution=merge-duplicates,return=minimal",
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
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
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
        print(f"Skipped {skipped} source rows missing required fields.", flush=True)

    if args.dry_run:
        print(f"Dry run: would upsert {len(catalog_rows)} rows. Sample:", flush=True)
        for row in catalog_rows[:5]:
            print(" ", row, flush=True)
        return 0

    client = RebrickableLegoCatalogClient(
        supabase_url=supabase_url,
        service_role_key=service_role_key,
        timeout_seconds=args.timeout_seconds,
    )
    written = client.upsert_rows(catalog_rows, batch_size=args.batch_size)
    print(f"Done. Upserted {written}/{len(catalog_rows)} rows.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
