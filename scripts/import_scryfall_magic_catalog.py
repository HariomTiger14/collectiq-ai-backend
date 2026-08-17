"""Import Magic: The Gathering card images from Scryfall into scryfall_magic_catalog.

Source: api.scryfall.com/bulk-data -> "default_cards", a free, public,
no-API-key bulk JSONL export of every card object on Scryfall in English
(or its only printed language). Confirmed live before this script was
written: 116,712 real English cards, and -- critically, spot-checked
against a real 400-row PriceCharting Magic sample -- once the set itself
resolves, every card matched (0 misses). See catalog_search_service.py's
_enrich_with_magic_image for why Scryfall (not TCGCSV, used for Pokemon/
LEGO) was chosen for Magic specifically: it models every distinct print,
including special treatments like Showcase and Gilded Foil, as its own
card object with its own collector_number, which lines up exactly with
the "#number" PriceCharting already embeds in these rows' titles.

This script imports _normalize_magic_text from catalog_search_service.py
rather than re-implementing it -- the normalization MUST be identical on
both sides of every match (PriceCharting's fields at match time, Scryfall's
fields here at import time), so importing the one real implementation is
the only way to guarantee that instead of hoping two copies stay in sync.

Per Scryfall's API etiquette (https://scryfall.com/docs/api), requests
identify the application via User-Agent and Accept headers, and this
script makes only two HTTP requests total: one to resolve the current
bulk-data download URL (the exact URL changes on every Scryfall refresh),
and one to fetch the file itself.

Re-running this script UPDATES existing rows (Scryfall's bulk export is
refreshed regularly), keyed on Scryfall's own card id.
"""

import argparse
import gzip
import io
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.pricing.catalog_search_service import _normalize_magic_text  # noqa: E402


DEFAULT_BULK_DATA_INDEX_URL = "https://api.scryfall.com/bulk-data/default_cards"
DEFAULT_BATCH_SIZE = 1000
SCRYFALL_HEADERS = {
    "User-Agent": "CollectIQCatalogBuilder/1.0 (+https://packlox.com)",
    "Accept": "application/json;q=0.9,*/*;q=0.8",
}


def resolve_download_url(bulk_data_index_url: str, timeout_seconds: float) -> str:
    with httpx.Client(timeout=timeout_seconds, headers=SCRYFALL_HEADERS) as client:
        response = client.get(bulk_data_index_url)
        response.raise_for_status()
        payload = response.json()
    download_url = payload.get("jsonl_download_uri") or payload.get("download_uri")
    if not download_url:
        raise SystemExit(f"Scryfall bulk-data response had no download URL: {payload}")
    return str(download_url)


def fetch_source_rows(download_url: str, timeout_seconds: float) -> list[dict[str, Any]]:
    with httpx.Client(timeout=timeout_seconds, headers=SCRYFALL_HEADERS) as client:
        response = client.get(download_url)
        response.raise_for_status()
        raw_bytes = response.content
    if download_url.endswith(".gz"):
        raw_bytes = gzip.decompress(raw_bytes)
    text = io.StringIO(raw_bytes.decode("utf-8"))
    rows = []
    for line in text:
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def image_url_from_card(source_row: dict[str, Any]) -> str | None:
    image_uris = source_row.get("image_uris")
    if isinstance(image_uris, dict) and image_uris.get("normal"):
        return str(image_uris["normal"])
    # Double-faced/split cards have no top-level image_uris -- use the
    # front face's image as a reasonable representative photo.
    faces = source_row.get("card_faces")
    if isinstance(faces, list) and faces:
        front_face = faces[0]
        if isinstance(front_face, dict):
            face_images = front_face.get("image_uris")
            if isinstance(face_images, dict) and face_images.get("normal"):
                return str(face_images["normal"])
    return None


def to_catalog_row(source_row: dict[str, Any]) -> dict[str, Any] | None:
    if source_row.get("lang") != "en":
        return None
    scryfall_id = str(source_row.get("id") or "").strip()
    name = str(source_row.get("name") or "").strip()
    set_name = str(source_row.get("set_name") or "").strip()
    image_url = image_url_from_card(source_row)
    if not scryfall_id or not name or not set_name or not image_url:
        return None
    collector_number = str(source_row.get("collector_number") or "").strip() or None
    return {
        "scryfall_id": scryfall_id,
        "set_name": set_name,
        "normalized_set_name": _normalize_magic_text(set_name),
        "collector_number": collector_number,
        "name": name,
        "normalized_name": _normalize_magic_text(name),
        "image_url": image_url,
    }


class ScryfallMagicCatalogClient:
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
                    f"{self.supabase_url}/rest/v1/scryfall_magic_catalog",
                    params={"on_conflict": "scryfall_id"},
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
    parser.add_argument("--bulk-data-index-url", default=DEFAULT_BULK_DATA_INDEX_URL)
    parser.add_argument("--supabase-url", default="", help="Defaults to SUPABASE_URL.")
    parser.add_argument(
        "--service-role-key", default="", help="Defaults to SUPABASE_SERVICE_ROLE_KEY."
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and transform the dataset but do not write to Supabase.",
    )
    args = parser.parse_args(argv)

    supabase_url = args.supabase_url or os.getenv("SUPABASE_URL", "")
    service_role_key = args.service_role_key or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

    print(f"Resolving current bulk-data download URL from {args.bulk_data_index_url} ...", flush=True)
    download_url = resolve_download_url(args.bulk_data_index_url, args.timeout_seconds)
    print(f"Fetching {download_url} ...", flush=True)
    source_rows = fetch_source_rows(download_url, args.timeout_seconds)
    print(f"Fetched {len(source_rows)} source rows.", flush=True)

    catalog_rows = [row for row in (to_catalog_row(r) for r in source_rows) if row is not None]
    skipped = len(source_rows) - len(catalog_rows)
    if skipped:
        print(f"Skipped {skipped} source rows (non-English or missing required fields).", flush=True)

    if args.dry_run:
        print(f"Dry run: would upsert {len(catalog_rows)} rows. Sample:", flush=True)
        for row in catalog_rows[:5]:
            print(" ", row, flush=True)
        return 0

    client = ScryfallMagicCatalogClient(
        supabase_url=supabase_url,
        service_role_key=service_role_key,
        timeout_seconds=args.timeout_seconds,
    )
    written = client.upsert_rows(catalog_rows, batch_size=args.batch_size)
    print(f"Done. Upserted {written}/{len(catalog_rows)} rows.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
