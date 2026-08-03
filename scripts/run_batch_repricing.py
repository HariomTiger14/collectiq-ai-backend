"""Scheduled batch re-pricing.

Re-values every ``portfolio_items`` row through the pricing engine and persists
refreshed values (``estimated_value_*`` columns + ``raw_json`` valuation fields).
Unavailable re-prices are a no-op, so a mock/unconfigured provider never wipes
existing values.

Runs daily; the price-alert pipeline (every 6h) then evaluates alerts against
whatever the latest persisted values are — no tight scheduling coupling needed.
"""

from __future__ import annotations

import argparse

from app.services.pricing.batch_repricing_service import BatchRepricingService


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch re-price portfolio items")
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--page-size", type=int, default=200)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    summary = BatchRepricingService().reprice_all(
        limit=args.limit,
        page_size=args.page_size,
        dry_run=args.dry_run,
    )
    payload = summary.to_dict()
    print(
        f"[reprice] scanned={payload['scanned']} repriced={payload['repriced']} "
        f"unavailable={payload['unavailable']} skipped={payload['skipped']} "
        f"rateLimited={payload['rateLimited']} errors={len(payload['errors'])} "
        f"dry_run={args.dry_run}"
    )


if __name__ == "__main__":
    main()
