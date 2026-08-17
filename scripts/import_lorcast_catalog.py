"""Import Disney Lorcana card images from Lorcast into lorcana_catalog (fallback source).

Source: api.lorcast.com, a free, public, no-API-key, purpose-built Lorcana
card database. No bulk export exists, but the whole game fits in ~22
requests: GET /v0/sets lists every set, then GET /v0/sets/{code}/cards
returns that set's full card list in one call. Confirmed live before this
script was written: 3,192 real cards across 22 sets.

IMPORTANT -- run this BEFORE import_lorcana_api_catalog.py, not after.
Both scripts upsert into the same lorcana_catalog table keyed on
(normalized_set_name, card_number); running Lorcast first and lorcana-
api.com second means lorcana-api.com's (official Ravensburger CDN) data
naturally overwrites any (set, number) both sources have, while this
script's rows survive only for the handful lorcana-api.com's snapshot
doesn't have yet -- see database/migrations/20260817_create_lorcana_
catalog.sql for the full reasoning and the live comparison that picked
this order.

This imports _normalize_magic_text from catalog_search_service.py rather
than re-implementing it -- the same punctuation-stripping normalization
must be identical on both sides of every match (PriceCharting's fields at
match time, this script's set names at import time). It isn't Magic-
specific despite the name; it's the general normalizer this whole catalog
work reuses.
"""

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.pricing.catalog_search_service import _normalize_magic_text  # noqa: E402


DEFAULT_BASE_URL = "https://api.lorcast.com/v0"
DEFAULT_BATCH_SIZE = 1000
DEFAULT_REQUEST_DELAY_SECONDS = 0.1


def fetch_sets(base_url: str, timeout_seconds: float) -> list[dict[str, Any]]:
    with httpx.Client(timeout=timeout_seconds) as client:
        response = client.get(f"{base_url}/sets")
        response.raise_for_status()
        payload = response.json()
    results = payload.get("results") if isinstance(payload, dict) else None
    return [row for row in results or [] if isinstance(row, dict)]


def fetch_set_cards(base_url: str, set_code: str, timeout_seconds: float) -> list[dict[str, Any]]:
    with httpx.Client(timeout=timeout_seconds) as client:
        response = client.get(f"{base_url}/sets/{set_code}/cards")
        response.raise_for_status()
        payload = response.json()
    return [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []


def image_url_from_card(card: dict[str, Any]) -> str | None:
    image_uris = card.get("image_uris")
    if not isinstance(image_uris, dict):
        return None
    digital = image_uris.get("digital")
    if isinstance(digital, dict) and digital.get("normal"):
        return str(digital["normal"])
    return None


def to_catalog_row(card: dict[str, Any], *, set_name: str) -> dict[str, Any] | None:
    collector_number = str(card.get("collector_number") or "").strip()
    image_url = image_url_from_card(card)
    if not collector_number or not image_url:
        return None
    return {
        "normalized_set_name": _normalize_magic_text(set_name),
        "card_number": collector_number,
        "image_url": image_url,
        "source": "lorcast",
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

    sets = fetch_sets(args.base_url, args.timeout_seconds)
    print(f"Found {len(sets)} Lorcana sets.", flush=True)

    catalog_rows: dict[tuple[str, str], dict[str, Any]] = {}
    for index, lorcana_set in enumerate(sets):
        set_code = lorcana_set.get("code")
        set_name = str(lorcana_set.get("name") or "")
        if not set_code or not set_name:
            continue
        cards = fetch_set_cards(args.base_url, str(set_code), args.timeout_seconds)
        for card in cards:
            row = to_catalog_row(card, set_name=set_name)
            if row is None:
                continue
            key = (row["normalized_set_name"], row["card_number"])
            catalog_rows[key] = row
        print(f"{index + 1}/{len(sets)} sets processed ({set_name}): {len(cards)} cards", flush=True)
        time.sleep(args.request_delay_seconds)

    rows = list(catalog_rows.values())
    print(f"Built {len(rows)} (set, number) rows.", flush=True)

    if args.dry_run:
        print("Dry run: would upsert. Sample:", flush=True)
        for row in rows[:5]:
            print(" ", row, flush=True)
        return 0

    client = LorcanaCatalogClient(
        supabase_url=supabase_url,
        service_role_key=service_role_key,
        timeout_seconds=args.timeout_seconds,
    )
    written = client.upsert_rows(rows, batch_size=args.batch_size)
    print(f"Done. Upserted {written}/{len(rows)} rows.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
