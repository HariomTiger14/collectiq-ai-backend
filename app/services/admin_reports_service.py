from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.core.config import settings
from app.services.admin_audit_service import AdminAuditService
from app.services.admin_scan_failure_service import AdminScanFailureService
from app.services.admin_user_service import AdminUserService
from app.services.pricing.admin_review_queue_service import AdminPricingReviewQueueService


class AdminReportsService:
    def overview(self) -> dict[str, Any]:
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
                lambda: AdminAuditService().list_events(limit=200),
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
            "summary": summary,
            "sources": sources,
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
