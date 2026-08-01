from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.config import settings
from app.services.admin_audit_service import AdminAuditService
from app.services.admin_scan_failure_service import AdminScanFailureService
from app.services.admin_user_service import AdminUserService
from app.services.pricing.admin_review_queue_service import AdminPricingReviewQueueService


class AdminReportsService:
    def overview(
        self,
        *,
        days: int = 30,
        since: str | None = None,
        until: str | None = None,
    ) -> dict[str, Any]:
        date_range = _date_range(days=days, since=since, until=until)
        sources: dict[str, Any] = {}
        summary = {
            "users": self._safe_count("users", lambda: AdminUserService().list_users(limit=100)),
            "pricingReview": self._safe_count(
                "pricingReview",
                lambda: AdminPricingReviewQueueService().list_queue(limit=200),
            ),
            "scanFailures": self._safe_count(
                "scanFailures",
                lambda: AdminScanFailureService().list_failures(limit=200),
            ),
            "auditEvents": self._safe_count(
                "auditEvents",
                lambda: AdminAuditService().list_events(
                    since=date_range["since"],
                    until=date_range["until"],
                    limit=200,
                ),
            ),
        }
        for key, value in summary.items():
            sources[key] = {
                "available": value is not None,
                "count": value,
            }
        return {
            "success": True,
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "environment": settings.environment,
            "dateRange": date_range,
            "summary": summary,
            "sources": sources,
            "trends": {
                "auditEvents": self._audit_event_trend(
                    since=date_range["since"],
                    until=date_range["until"],
                ),
                "queuePressure": [
                    {"label": "Pricing", "value": summary["pricingReview"] or 0},
                    {"label": "Scans", "value": summary["scanFailures"] or 0},
                    {"label": "Users", "value": summary["users"] or 0},
                ],
            },
        }

    def _safe_count(self, key: str, loader) -> int | None:
        try:
            payload = loader()
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        value = payload.get("totalCount", payload.get("count"))
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _audit_event_trend(self, *, since: str, until: str) -> list[dict[str, Any]]:
        try:
            events = AdminAuditService().list_events(since=since, until=until, limit=200).get("events", [])
        except Exception:
            return []
        buckets: dict[str, int] = {}
        for event in events:
            day = str(event.get("createdAt") or "")[:10]
            if day:
                buckets[day] = buckets.get(day, 0) + 1
        return [{"date": day, "count": count} for day, count in sorted(buckets.items())]


def _date_range(*, days: int, since: str | None, until: str | None) -> dict[str, str | int]:
    end = _parse_datetime(until) or datetime.now(timezone.utc)
    start = _parse_datetime(since) or (end - timedelta(days=max(1, min(days, 365))))
    return {
        "days": max(1, min(days, 365)),
        "since": start.isoformat(),
        "until": end.isoformat(),
    }


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
