"""Import Yu-Gi-Oh card images from TCGCSV into yugioh_catalog (fallback source).

Source: tcgcsv.com/tcgplayer/2 (Yu-Gi-Oh's TCGplayer category id), a free,
public, no-key cache of TCGplayer's own catalog export. Each product's
extendedData carries a "Number" field in the exact same set-code format
PriceCharting embeds in its own Yu-Gi-Oh titles (e.g. "LOB-027") -- see
database/migrations/20260817_create_yugioh_catalog.sql for the full
reasoning behind that shared key, and why YGOPRODeck (not this script) is
the primary source: a live side-by-side comparison found YGOPRODeck
matches ~80% more real PriceCharting rows than TCGCSV does.

IMPORTANT -- run this BEFORE import_ygoprodeck_catalog.py, not after.
Both scripts upsert into the same yugioh_catalog table keyed on set_code;
running TCGCSV first and YGOPRODeck second means YGOPRODeck's (better)
data naturally overwrites any code both sources have, while this script's
rows survive only for the handful of codes YGOPRODeck's snapshot doesn't
have yet (mostly very recent set releases). Running them in the opposite
order would let TCGCSV's weaker data win on overlap -- silently worse,
not obviously wrong, so this only works if the order is respected.

Same safety rule as everywhere else in this catalog work: a set_code that
resolves to more than one distinct image within TCGCSV itself (a data
inconsistency, not expected but checked for) is skipped rather than
guessed at.

This iterates every Yu-Gi-Oh group (~660 at last count) fetching its
products -- unavoidable since TCGCSV has no bulk "all products" export
the way its categories/groups endpoints do, unlike Scryfall/Rebrickable/
YGOPRODeck. Paced with a small delay between requests per TCGCSV's own
usage guidelines (https://tcgcsv.com/docs).
"""

import argparse
import os
import time
from typing import Any

import httpx


DEFAULT_BASE_URL = "https://tcgcsv.com/tcgplayer"
YUGIOH_CATEGORY_ID = 2
DEFAULT_BATCH_SIZE = 1000
DEFAULT_REQUEST_DELAY_SECONDS = 0.05
USER_AGENT = "CollectIQCatalogBuilder/1.0 (+https://packlox.com)"


def set_code_from_product(product: dict[str, Any]) -> str | None:
    extended_data = product.get("extendedData")
    if not isinstance(extended_data, list):
        return None
    for field in extended_data:
        if isinstance(field, dict) and field.get("name") == "Number":
            value = str(field.get("value") or "").strip()
            return value or None
    return None


class TCGCSVClient:
    def __init__(self, *, base_url: str, timeout_seconds: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def _get(self, path: str) -> list[dict[str, Any]]:
        with httpx.Client(timeout=self.timeout_seconds) as client:
            response = client.get(f"{self.base_url}{path}", headers={"User-Agent": USER_AGENT})
            response.raise_for_status()
            payload = response.json()
        results = payload.get("results") if isinstance(payload, dict) else None
        return [row for row in results or [] if isinstance(row, dict)]

    def fetch_groups(self) -> list[dict[str, Any]]:
        return self._get(f"/{YUGIOH_CATEGORY_ID}/groups")

    def fetch_products(self, group_id: int) -> list[dict[str, Any]]:
        return self._get(f"/{YUGIOH_CATEGORY_ID}/{group_id}/products")


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

    groups = tcgcsv.fetch_groups()
    print(f"Found {len(groups)} Yu-Gi-Oh groups.", flush=True)

    images_by_code: dict[str, set[str]] = {}
    name_by_code: dict[str, str] = {}
    for index, group in enumerate(groups):
        group_id = group.get("groupId")
        if not isinstance(group_id, int):
            continue
        products = tcgcsv.fetch_products(group_id)
        for product in products:
            code = set_code_from_product(product)
            image_url = product.get("imageUrl")
            if not code or not image_url:
                continue
            images_by_code.setdefault(code, set()).add(str(image_url))
            name_by_code.setdefault(code, str(product.get("name") or ""))
        if index % 100 == 0:
            print(f"{index}/{len(groups)} groups processed, {len(images_by_code)} codes so far", flush=True)
        time.sleep(args.request_delay_seconds)

    catalog_rows = []
    ambiguous = 0
    for code, images in images_by_code.items():
        if len(images) != 1:
            ambiguous += 1
            continue
        catalog_rows.append(
            {
                "set_code": code,
                "card_name": name_by_code.get(code, ""),
                "image_url": next(iter(images)),
                "source": "tcgcsv",
            }
        )

    print(
        f"Built {len(catalog_rows)} unambiguous set_code rows "
        f"(skipped {ambiguous} codes with conflicting images).",
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
