"""Import Disney Lorcana card images from lorcana-api.com into lorcana_catalog (primary source).

Source: api.lorcana-api.com/cards/all, a free, public, no-API-key bulk
export (2,694 real cards at last count, confirmed live in one request).
Its Image field is hosted on api.lorcana.ravensburger.com -- Lorcana's
official publisher's own CDN, verified live to return real, working
images -- which is why this is the primary source over Lorcast (see
scripts/import_lorcast_catalog.py, the fallback).

IMPORTANT -- run this AFTER import_lorcast_catalog.py, not before. Both
scripts upsert into the same lorcana_catalog table keyed on
(normalized_set_name, card_number); running Lorcast first and this
second means this script's (official-CDN) data naturally overwrites any
(set, number) both sources have, while Lorcast's rows survive only for
the handful of (set, number) pairs this source doesn't have yet -- see
database/migrations/20260817_create_lorcana_catalog.sql for the live
comparison that picked this order (essentially tied: 494 vs 495 matches
on a real 500-row PriceCharting sample).

Imports _normalize_magic_text from catalog_search_service.py rather than
re-implementing it -- see import_lorcast_catalog.py's docstring for why.
"""

import argparse
import os
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.pricing.catalog_search_service import _normalize_magic_text  # noqa: E402


DEFAULT_SOURCE_URL = "https://api.lorcana-api.com/cards/all"
DEFAULT_BATCH_SIZE = 1000


def fetch_source_rows(source_url: str, timeout_seconds: float) -> list[dict[str, Any]]:
    with httpx.Client(timeout=timeout_seconds) as client:
        response = client.get(source_url)
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, list):
        raise SystemExit("Unexpected lorcana-api.com response shape: expected a JSON array.")
    return [row for row in payload if isinstance(row, dict)]


def to_catalog_row(card: dict[str, Any]) -> dict[str, Any] | None:
    set_name = str(card.get("Set_Name") or "").strip()
    card_number = card.get("Card_Num")
    image_url = str(card.get("Image") or "").strip()
    if not set_name or card_number is None or not image_url:
        return None
    return {
        "normalized_set_name": _normalize_magic_text(set_name),
        "card_number": str(card_number),
        "image_url": image_url,
        "source": "lorcana-api.com",
    }


class LorcanaCatalogClient:
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
                    f"{self.supabase_url}/rest/v1/lorcana_catalog",
                    params={"on_conflict": "normalized_set_name,card_number"},
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
    print(f"Fetched {len(source_rows)} source cards.", flush=True)

    deduped_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    skipped = 0
    for card in source_rows:
        row = to_catalog_row(card)
        if row is None:
            skipped += 1
            continue
        key = (row["normalized_set_name"], row["card_number"])
        deduped_by_key[key] = row

    catalog_rows = list(deduped_by_key.values())
    print(
        f"Built {len(catalog_rows)} (set, number) rows "
        f"(skipped {skipped} cards missing required fields).",
        flush=True,
    )

    if args.dry_run:
        print("Dry run: would upsert. Sample:", flush=True)
        for row in catalog_rows[:5]:
            print(" ", row, flush=True)
        return 0

    client = LorcanaCatalogClient(
        supabase_url=supabase_url,
        service_role_key=service_role_key,
        timeout_seconds=args.timeout_seconds,
    )
    written = client.upsert_rows(catalog_rows, batch_size=args.batch_size)
    print(f"Done. Upserted {written}/{len(catalog_rows)} rows.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
