"""Import Yu-Gi-Oh card images from YGOPRODeck into yugioh_catalog (primary source).

Source: db.ygoprodeck.com/api/v7/cardinfo.php, a free, public, no-API-key
bulk export of YGOPRODeck's entire card database (14,515 real cards at
last count). Confirmed live before this script was written to be the
stronger of the two sources checked for Yu-Gi-Oh: a side-by-side
comparison against TCGCSV on a real 566-row PriceCharting sample found
YGOPRODeck matched 532 codes vs TCGCSV's 294, and matched nearly
everything TCGCSV did plus 243 more -- see
database/migrations/20260817_create_yugioh_catalog.sql for the full
reasoning, and import_tcgcsv_yugioh_catalog.py for the fallback source.

IMPORTANT -- after BOTH catalog imports, run
scripts/rehost_yugioh_images.py: it mirrors every provider-hosted image
into our own catalog-images bucket (YGOPRODeck's policy requires
re-hosting instead of hotlinking) and rewrites image_url accordingly.
New rows land here with provider URLs and stay un-served-to-users until
that pass runs.

IMPORTANT -- run this AFTER import_tcgcsv_yugioh_catalog.py, not before.
Both scripts upsert into the same yugioh_catalog table keyed on set_code;
running TCGCSV first and this second means this script's (better) data
naturally overwrites any code both sources have, while TCGCSV's rows
survive only for the handful of codes this source doesn't have yet.

Each card can have more than one set_code (one per printing/set it
appeared in -- e.g. reprints), which is why PriceCharting's own embedded
set code is the right lookup key, not the card name. A small number of
cards (124/14,515 at last count, ~0.85%) have more than one photo in
YGOPRODeck's own data -- these are genuine alternate-art printings, and
since there's no reliable way to tell from this API alone which photo
belongs to which of a card's several set_codes, ALL of that card's
set_codes are skipped entirely rather than guessed at.
"""

import argparse
import os
from typing import Any

import httpx


DEFAULT_SOURCE_URL = "https://db.ygoprodeck.com/api/v7/cardinfo.php"
DEFAULT_BATCH_SIZE = 1000


def fetch_source_rows(source_url: str, timeout_seconds: float) -> list[dict[str, Any]]:
    with httpx.Client(timeout=timeout_seconds) as client:
        response = client.get(source_url)
        response.raise_for_status()
        payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise SystemExit("Unexpected YGOPRODeck response shape: no 'data' array.")
    return [row for row in data if isinstance(row, dict)]


def to_catalog_rows(card: dict[str, Any]) -> list[dict[str, Any]]:
    images = card.get("card_images")
    if not isinstance(images, list) or len(images) != 1:
        # Zero images, or more than one (ambiguous alternate art) -- skip
        # this card's set_codes entirely rather than guess.
        return []
    image_url = images[0].get("image_url") if isinstance(images[0], dict) else None
    if not image_url:
        return []
    card_name = str(card.get("name") or "").strip()
    card_sets = card.get("card_sets")
    if not isinstance(card_sets, list) or not card_name:
        return []
    rows = []
    for card_set in card_sets:
        if not isinstance(card_set, dict):
            continue
        set_code = str(card_set.get("set_code") or "").strip()
        if not set_code:
            continue
        rows.append(
            {
                "set_code": set_code,
                "card_name": card_name,
                "image_url": str(image_url),
                "source": "ygoprodeck",
            }
        )
    return rows


class YugiohCatalogClient:
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
                    f"{self.supabase_url}/rest/v1/yugioh_catalog",
                    params={"on_conflict": "set_code"},
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
    print(f"Fetched {len(source_rows)} source cards.", flush=True)

    catalog_rows: list[dict[str, Any]] = []
    skipped_cards = 0
    for card in source_rows:
        rows = to_catalog_rows(card)
        if not rows:
            skipped_cards += 1
            continue
        catalog_rows.extend(rows)

    # YGOPRODeck's own data has a handful of duplicate card entries (the
    # same card id listed twice, verified live -- not a real cross-card
    # collision), which produces duplicate set_code rows here. A batch
    # containing the same set_code twice makes PostgREST's ON CONFLICT DO
    # UPDATE fail outright ("cannot affect row a second time"), so this
    # must dedupe before upserting, not just rely on merge-duplicates
    # across separate requests.
    deduped_by_code: dict[str, dict[str, Any]] = {row["set_code"]: row for row in catalog_rows}
    duplicate_count = len(catalog_rows) - len(deduped_by_code)
    catalog_rows = list(deduped_by_code.values())

    print(
        f"Built {len(catalog_rows)} set_code rows from "
        f"{len(source_rows) - skipped_cards} cards (skipped {skipped_cards} cards "
        f"with zero or ambiguous images; deduped {duplicate_count} repeated set_codes).",
        flush=True,
    )

    if args.dry_run:
        print("Dry run: would upsert. Sample:", flush=True)
        for row in catalog_rows[:5]:
            print(" ", row, flush=True)
        return 0

    client = YugiohCatalogClient(
        supabase_url=supabase_url,
        service_role_key=service_role_key,
        timeout_seconds=args.timeout_seconds,
    )
    written = client.upsert_rows(catalog_rows, batch_size=args.batch_size)
    print(f"Done. Upserted {written}/{len(catalog_rows)} rows.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
