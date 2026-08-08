"""Read-only diagnostic: find which catalog item(s) tripped the price
overflow that broke the backfill cron (see fix/catalog-price-overflow-validation).

Finds registry rows currently marked as failed, re-fetches their CSV from
the source site, and prints any row whose raw price field would exceed the
new MAX_PLAUSIBLE_PRICE_CENTS sanity ceiling -- so we can eyeball whether
it's genuinely a malformed field (e.g. a barcode) or a real price the
ceiling is too conservative for.

Makes no writes to Supabase or the catalog tables -- GET/read only.

Usage:
    export SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... PRICECHARTING_API_TOKEN=...
    python scripts/diagnose_price_overflow.py [--limit 200]
"""

import argparse
import os

import httpx

from scripts.backfill_pricecharting_sets import (
    SOURCE_SITE_BASE_URLS,
    chunked,
    fetch_batch_csv,
    group_by_site,
)
from scripts.import_pricecharting_catalog import (
    MAX_PLAUSIBLE_PRICE_CENTS,
    PRICE_FIELDS,
    TEXT_FIELDS,
    load_rows_from_text,
    pick_text,
)


def fetch_failed_registry_rows(
    *, supabase_url: str, service_role_key: str, limit: int, timeout_seconds: float
) -> list[dict[str, str]]:
    with httpx.Client(timeout=timeout_seconds) as client:
        response = client.get(
            f"{supabase_url.rstrip('/')}/rest/v1/pricecharting_set_registry",
            params={
                "select": "console_uid,source_site,url",
                "last_fetch_status": "eq.error",
                "order": "last_fetched_at.desc",
                "limit": str(limit),
            },
            headers={
                "apikey": service_role_key,
                "Authorization": f"Bearer {service_role_key}",
            },
        )
        response.raise_for_status()
        return response.json()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--batch-size", type=int, default=150)
    parser.add_argument("--timeout-seconds", type=float, default=60)
    args = parser.parse_args()

    supabase_url = os.getenv("SUPABASE_URL", "")
    service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    token = os.getenv("PRICECHARTING_API_TOKEN", "")
    if not supabase_url or not service_role_key or not token:
        raise SystemExit(
            "SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, and PRICECHARTING_API_TOKEN "
            "are all required."
        )

    failed_rows = fetch_failed_registry_rows(
        supabase_url=supabase_url,
        service_role_key=service_role_key,
        limit=args.limit,
        timeout_seconds=args.timeout_seconds,
    )
    print(f"Found {len(failed_rows)} registry rows marked last_fetch_status=error.")
    if not failed_rows:
        return 0

    hits = 0
    with httpx.Client(timeout=args.timeout_seconds, follow_redirects=True) as http:
        for source_site, rows in group_by_site(failed_rows).items():
            base_url = SOURCE_SITE_BASE_URLS[source_site]
            for chunk in chunked(rows, args.batch_size):
                console_uids = [row["console_uid"] for row in chunk]
                csv_text = fetch_batch_csv(
                    http, base_url=base_url, token=token, console_uids=console_uids
                )
                if csv_text is None:
                    continue
                for csv_row in load_rows_from_text(csv_text):
                    for target, aliases in PRICE_FIELDS.items():
                        raw = pick_text(csv_row, aliases)
                        if not raw:
                            continue
                        # Re-derive the pre-sanity-check cents value the old
                        # code would have produced, to see which field(s)
                        # actually exceed the new ceiling.
                        cleaned = raw.replace(",", "").strip()
                        try:
                            cents = (
                                round(float(cleaned.replace("$", "")) * 100)
                                if cleaned.startswith("$") or "." in cleaned
                                else int(float(cleaned))
                            )
                        except ValueError:
                            continue
                        if cents > MAX_PLAUSIBLE_PRICE_CENTS:
                            hits += 1
                            print(
                                f"  [{target}] raw={raw!r} (~${cents / 100:,.2f}) "
                                f"product={pick_text(csv_row, TEXT_FIELDS['product_name'])!r} "
                                f"console={pick_text(csv_row, TEXT_FIELDS['console_name'])!r} "
                                f"id={pick_text(csv_row, TEXT_FIELDS['pricecharting_id'])!r} "
                                f"upc={pick_text(csv_row, TEXT_FIELDS['upc'])!r} "
                                f"url={pick_text(csv_row, TEXT_FIELDS['product_url'])!r}",
                                flush=True,
                            )

    print(f"\n{hits} field(s) found exceeding the ${MAX_PLAUSIBLE_PRICE_CENTS / 100:,.0f} ceiling.")
    if hits == 0:
        print(
            "No offending value found in the currently-failed sets -- it may "
            "belong to a set that already got reclaimed/retried by another "
            "worker since this failure, or the source data has since changed."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
