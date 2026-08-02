"""Scheduled price-alert pipeline.

Runs the two halves of price alerts on a schedule:
  1. Evaluate saved alerts against current portfolio values and flip any that
     meet their condition to ``status='triggered'``.
  2. Dispatch FCM pushes for triggered alerts.

Evaluation always runs; push dispatch is best-effort (skipped with a log line if
Firebase push config is missing) so alerts still flip to triggered regardless.
"""

from __future__ import annotations

import argparse

from app.services.alerts.price_alert_evaluation_service import (
    PriceAlertEvaluationService,
)
from app.services.push.price_alert_push_service import (
    PriceAlertPushService,
    PushNotificationError,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate + dispatch price alerts")
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--push-limit", type=int, default=200)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    evaluation = PriceAlertEvaluationService().evaluate_and_flag(
        limit=args.limit,
        dry_run=args.dry_run,
    )
    print(
        f"[price-alerts] evaluated={evaluation.evaluated} "
        f"triggered={evaluation.triggered} dry_run={args.dry_run}"
    )

    try:
        summary = PriceAlertPushService().dispatch_triggered_alerts(
            limit=args.push_limit,
            dry_run=args.dry_run,
        )
        print(f"[price-alerts] push={summary.to_dict()}")
    except PushNotificationError as error:
        print(f"[price-alerts] push skipped: {error}")


if __name__ == "__main__":
    main()
