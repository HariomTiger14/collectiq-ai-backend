"""Import One Piece Card Game images from optcgapi.com into one_piece_catalog.

Source: optcgapi.com, a free, public, no-API-key bulk export, split across
three endpoints -- set cards, starter deck cards, and promo cards (~5,162
real cards combined at last count, confirmed live). No bulk "everything"
endpoint exists, so this script hits all three.

PriceCharting's One Piece titles embed Bandai's own set-code convention
(e.g. "Captain John OP07-082"), matching optcgapi's card_set_id field
directly. Unlike Yu-Gi-Oh, that code is NOT reliably unique per print:
verified live that 40% of codes in optcgapi's own data map to more than
one card, because promo reprints (championship prizes, tournament packs,
box toppers) routinely reuse the base card's code. This script therefore
imports every row rather than collapsing to one image per code -- the
matching/disambiguation logic lives in catalog_search_service.py's
_enrich_with_onepiece_image, using the `is_plain` flag this script
computes (whether a card's name has no parenthetical/bracket variant
suffix, i.e. it's the base unambiguous print) -- see
database/migrations/20260817_create_one_piece_catalog.sql for the full
reasoning.

Idempotent on content_hash (card_set_id + card_name + image_url) since
optcgapi.com has no stable per-row id of its own -- same pattern as the
Funko import.
"""

import argparse
import hashlib
import os
import re
from typing import Any

import httpx


DEFAULT_BASE_URL = "https://optcgapi.com/api"
SOURCE_ENDPOINTS = ("allSetCards", "allSTCards", "allPromos")
DEFAULT_BATCH_SIZE = 1000

_VARIANT_TAG_RE = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]\s*")


def is_plain_card_name(card_name: str) -> bool:
    # A "plain" print has no parenthetical/bracket suffix at all -- e.g.
    # "Perona" is plain; "Perona (Box Topper)" and "Perona [Winner]" are
    # not. Stripping every such suffix and comparing to the original
    # (after whitespace normalization) catches both bracket styles.
    stripped = _VARIANT_TAG_RE.sub("", card_name).strip()
    return stripped == card_name.strip()


def content_hash_for(row: dict[str, Any]) -> str:
    payload = "|".join([row["card_set_id"], row["card_name"], row["image_url"]])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def to_catalog_row(card: dict[str, Any]) -> dict[str, Any] | None:
    card_set_id = str(card.get("card_set_id") or "").strip()
    card_name = str(card.get("card_name") or "").strip()
    image_url = str(card.get("card_image") or "").strip()
    if not card_set_id or not card_name or not image_url:
        return None
    row = {
        "card_set_id": card_set_id,
        "card_name": card_name,
        "is_plain": is_plain_card_name(card_name),
        "image_url": image_url,
        "source": "optcgapi",
    }
    row["content_hash"] = content_hash_for(row)
    return row


def fetch_endpoint(base_url: str, endpoint: str, timeout_seconds: float) -> list[dict[str, Any]]:
    with httpx.Client(timeout=timeout_seconds) as client:
        response = client.get(f"{base_url}/{endpoint}/")
        response.raise_for_status()
        payload = response.json()
    return [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []


class OnePieceCatalogClient:
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
                    f"{self.supabase_url}/rest/v1/one_piece_catalog",
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
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
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

    catalog_rows: dict[str, dict[str, Any]] = {}
    skipped = 0
    for endpoint in SOURCE_ENDPOINTS:
        print(f"Fetching {args.base_url}/{endpoint}/ ...", flush=True)
        cards = fetch_endpoint(args.base_url, endpoint, args.timeout_seconds)
        print(f"  {len(cards)} cards.", flush=True)
        for card in cards:
            row = to_catalog_row(card)
            if row is None:
                skipped += 1
                continue
            catalog_rows[row["content_hash"]] = row

    rows = list(catalog_rows.values())
    print(f"Built {len(rows)} unique rows (skipped {skipped} incomplete cards).", flush=True)

    if args.dry_run:
        print("Dry run: would upsert. Sample:", flush=True)
        for row in rows[:5]:
            print(" ", row, flush=True)
        return 0

    client = OnePieceCatalogClient(
        supabase_url=supabase_url,
        service_role_key=service_role_key,
        timeout_seconds=args.timeout_seconds,
    )
    written = client.upsert_rows(rows, batch_size=args.batch_size)
    print(f"Done. Upserted {written}/{len(rows)} rows.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
