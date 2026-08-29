"""Import Pokemon card product images from TCGCSV into tcgplayer_pokemon_catalog.

Source: tcgcsv.com, a free, no-key, daily-updated cache of TCGplayer's own
catalog export (categories -> groups -> products, each with a real product
photo). Confirmed live before this script was written: real, distinct
per-product images (verified via Content-Length, not just distinct URLs),
and -- critically -- confirmed NOT to generalize past Base Set for print-
variant distinction (Shadowless has its own group only for Base Set; named
error/misprint products like "Charizard (Black Dot Error)" are rare
exceptions, not a general pattern). See docs/GLOBAL_CATALOG_ARCHITECTURE.md
for the full investigation and catalog_search_service.py for how the
variant_tag this script writes is actually used.

TCGCSV's own usage guidelines (https://tcgcsv.com/docs) ask consumers to:
  - re-sync at most once every 24h (the data itself is rebuilt once daily)
  - send a real User-Agent identifying the application
  - pace requests (small delay between calls), not hammer the API
This script only pulls the Pokemon category (not all ~90 TCGplayer
categories), so a full run is on the order of ~100 requests, well under
their stated 10,000-requests-per-24h ceiling.

Unlike the Funko import, this is NOT a one-time static import: re-running
this script UPDATES existing rows (image URLs can change), keyed on
TCGplayer's own product id -- see the table migration
(20260817_create_tcgplayer_pokemon_catalog.sql) for why.
"""

import argparse
import os
import re
import time
from typing import Any

import httpx


DEFAULT_BASE_URL = "https://tcgcsv.com/tcgplayer"
DEFAULT_BATCH_SIZE = 500
DEFAULT_REQUEST_DELAY_SECONDS = 0.1
USER_AGENT = "CollectIQCatalogBuilder/1.0 (+https://packlox.com)"

_NUMBER_RE = re.compile(r"(\d+)")


def normalize_card_number(raw_number: str | None) -> str | None:
    if not raw_number:
        return None
    # TCGplayer's "Number" extended-data field looks like "004/102" or
    # "4/102" -- take the part before the slash, strip leading zeros, so it
    # matches _pokemon_card_number()'s "#4" -> "4" extraction from
    # PriceCharting titles in catalog_search_service.py.
    first_part = raw_number.split("/")[0].strip()
    match = _NUMBER_RE.fullmatch(first_part)
    if match:
        return str(int(match.group(1)))
    return first_part or None


def classify_variant(*, group_name: str, product_name: str) -> str | None:
    if "shadowless" in group_name.lower():
        return "shadowless"
    lowered_product_name = product_name.lower()
    if "error" in lowered_product_name or "misprint" in lowered_product_name:
        return "error"
    return None


def to_catalog_row(
    product: dict[str, Any], *, group_id: int, group_name: str
) -> dict[str, Any] | None:
    product_id = product.get("productId")
    product_name = str(product.get("name") or "").strip()
    image_url = str(product.get("imageUrl") or "").strip()
    if not product_id or not product_name or not image_url:
        return None
    extended_data = product.get("extendedData")
    card_number = None
    if isinstance(extended_data, list):
        for field in extended_data:
            if isinstance(field, dict) and field.get("name") == "Number":
                card_number = normalize_card_number(str(field.get("value") or ""))
                break
    return {
        "tcgplayer_product_id": product_id,
        "group_id": group_id,
        "group_name": group_name,
        "product_name": product_name,
        "card_number": card_number,
        "image_url": image_url,
        "variant_tag": classify_variant(group_name=group_name, product_name=product_name),
    }


class TCGCSVClient:
    def __init__(self, *, base_url: str, timeout_seconds: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def _get(self, path: str) -> list[dict[str, Any]]:
        with httpx.Client(timeout=self.timeout_seconds) as client:
            response = client.get(
                f"{self.base_url}{path}", headers={"User-Agent": USER_AGENT}
            )
            response.raise_for_status()
            payload = response.json()
        results = payload.get("results") if isinstance(payload, dict) else None
        return [row for row in results or [] if isinstance(row, dict)]

    def find_pokemon_category_id(self) -> int:
        for category in self._get("/categories"):
            if str(category.get("name") or "").strip().lower() == "pokemon":
                category_id = category.get("categoryId")
                if isinstance(category_id, int):
                    return category_id
        raise SystemExit("Could not find a 'Pokemon' category in TCGCSV categories.")

    def fetch_groups(self, category_id: int) -> list[dict[str, Any]]:
        return self._get(f"/{category_id}/groups")

    def fetch_products(self, category_id: int, group_id: int) -> list[dict[str, Any]]:
        return self._get(f"/{category_id}/{group_id}/products")


class TCGPlayerPokemonCatalogClient:
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
                    f"{self.supabase_url}/rest/v1/tcgplayer_pokemon_catalog",
                    params={"on_conflict": "tcgplayer_product_id"},
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
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--supabase-url", default="", help="Defaults to SUPABASE_URL.")
    parser.add_argument(
        "--service-role-key", default="", help="Defaults to SUPABASE_SERVICE_ROLE_KEY."
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--request-delay-seconds", type=float, default=DEFAULT_REQUEST_DELAY_SECONDS)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and transform the dataset but do not write to Supabase.",
    )
    args = parser.parse_args(argv)

    supabase_url = args.supabase_url or os.getenv("SUPABASE_URL", "")
    service_role_key = args.service_role_key or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

    tcgcsv = TCGCSVClient(base_url=args.base_url, timeout_seconds=args.timeout_seconds)

    print("Finding Pokemon category...", flush=True)
    category_id = tcgcsv.find_pokemon_category_id()
    print(f"Pokemon category id: {category_id}", flush=True)

    groups = tcgcsv.fetch_groups(category_id)
    print(f"Found {len(groups)} Pokemon groups.", flush=True)

    catalog_rows: list[dict[str, Any]] = []
    skipped = 0
    for group in groups:
        group_id = group.get("groupId")
        group_name = str(group.get("name") or "").strip()
        if not isinstance(group_id, int) or not group_name:
            continue
        products = tcgcsv.fetch_products(category_id, group_id)
        for product in products:
            row = to_catalog_row(product, group_id=group_id, group_name=group_name)
            if row is None:
                skipped += 1
                continue
            catalog_rows.append(row)
        time.sleep(args.request_delay_seconds)

    print(f"Fetched {len(catalog_rows)} products with images (skipped {skipped}).", flush=True)

    if args.dry_run:
        print("Dry run: would upsert. Sample:", flush=True)
        for row in catalog_rows[:5]:
            print(" ", row, flush=True)
        return 0

    client = TCGPlayerPokemonCatalogClient(
        supabase_url=supabase_url,
        service_role_key=service_role_key,
        timeout_seconds=args.timeout_seconds,
    )
    written = client.upsert_rows(catalog_rows, batch_size=args.batch_size)
    print(f"Done. Upserted {written}/{len(catalog_rows)} rows.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
