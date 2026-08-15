"""Admin visibility into subscription plan distribution -- previously not
surfaced anywhere; admin_reports_service.py only counts users/pricing
queue/scan failures/audit events, not plan/status/source breakdown.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.core.config import settings
from app.services.admin_audit_service import AdminAuditService
from app.services.admin_user_service import SupabaseAdminUserRepository

# Best-effort MRR estimate -- there's no price column anywhere in the
# database (user_subscriptions only stores plan/status/source), so this
# mirrors the price actually charged in the app (Settings/paywall copy,
# SitDummyBillingRepository's product listing: "AUD 9.99 / month"). Premium
# is deliberately excluded: the admin console's own "2 legacy" label on the
# Plan mix card already signals it's not a currently-sold tier with a known
# price, so estimating revenue from it would be a guess dressed up as data.
PRO_MONTHLY_PRICE_AUD = 9.99


class AdminSubscriptionMetricsError(Exception):
    """Raised when subscription metrics cannot be read safely."""


class AdminSubscriptionMetricsService:
    def __init__(
        self,
        *,
        repository: "SupabaseSubscriptionMetricsRepository | None" = None,
        audit_service: AdminAuditService | None = None,
        user_repository: SupabaseAdminUserRepository | None = None,
    ) -> None:
        self._repository = repository or SupabaseSubscriptionMetricsRepository()
        self._audit_service = audit_service or AdminAuditService()
        self._user_repository = user_repository or SupabaseAdminUserRepository()

    def get_summary(self) -> dict[str, Any]:
        if not self._repository.is_configured:
            raise AdminSubscriptionMetricsError("Supabase admin configuration is missing.")
        rows = self._repository.subscription_summary()
        total_users = sum(int(row.get("total") or 0) for row in rows)
        by_plan = _grouped(rows, "plan", total_users)
        by_source = _grouped(rows, "source", total_users)
        pro_count = next((bucket["count"] for bucket in by_plan if bucket["label"] == "pro"), 0)
        admin_override_count = next(
            (bucket["count"] for bucket in by_source if bucket["label"] == "admin_override"), 0,
        )
        return {
            "success": True,
            "totalUsers": total_users,
            "byPlan": by_plan,
            "byStatus": _grouped(rows, "status", total_users),
            "bySource": by_source,
            "recentAdminOverrides": self._recent_admin_overrides(),
            "mrrAud": round(pro_count * PRO_MONTHLY_PRICE_AUD, 2),
            "proConversionPercent": round((pro_count / total_users) * 100, 1) if total_users else 0.0,
            "activeAdminOverrides": admin_override_count,
            "upgradesThisMonth": self._upgrades_this_month(),
            "recentEntitlementChanges": self._recent_entitlement_changes(),
        }

    def _recent_admin_overrides(self, *, days: int = 30) -> dict[str, Any]:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        try:
            payload = self._audit_service.list_events(
                action="admin_users.subscription_overridden",
                status="success",
                since=since,
                limit=1000,
            )
        except Exception:
            return {"days": days, "count": None}
        return {"days": days, "count": payload.get("count")}

    def _upgrades_this_month(self) -> int:
        # "Upgrade" specifically = free -> a paid plan, not every entitlement
        # change (a lateral admin correction or a downgrade shouldn't count).
        month_start = datetime.now(timezone.utc).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0,
        ).isoformat()
        try:
            payload = self._audit_service.list_events(
                action="subscription.entitlement_changed",
                status="success",
                since=month_start,
                limit=1000,
            )
        except Exception:
            return 0
        events = payload.get("events") or []
        return sum(
            1
            for event in events
            if event.get("metadata", {}).get("fromPlan") == "free"
            and event.get("metadata", {}).get("toPlan") in ("pro", "premium")
        )

    def _recent_entitlement_changes(self, *, limit: int = 10) -> list[dict[str, Any]]:
        try:
            payload = self._audit_service.list_events(
                action="subscription.entitlement_changed", status="success", limit=limit,
            )
        except Exception:
            return []
        events = payload.get("events") or []
        changes = []
        for event in events:
            user_id = event.get("targetId")
            metadata = event.get("metadata") or {}
            auth_user = self._user_repository._get_auth_user(user_id) if user_id else None
            changes.append(
                {
                    "userId": user_id,
                    "userLabel": (auth_user or {}).get("email") or user_id or "Unknown",
                    "fromPlan": metadata.get("fromPlan"),
                    "toPlan": metadata.get("toPlan"),
                    "source": metadata.get("source"),
                    "changedAt": event.get("createdAt"),
                }
            )
        return changes


def _grouped(rows: list[dict[str, Any]], key: str, total_users: int) -> list[dict[str, Any]]:
    buckets: dict[str, int] = {}
    for row in rows:
        bucket_key = str(row.get(key) or "unknown")
        buckets[bucket_key] = buckets.get(bucket_key, 0) + int(row.get("total") or 0)
    return [
        {
            "label": label,
            "count": count,
            "percent": round((count / total_users) * 100, 1) if total_users else 0.0,
        }
        for label, count in sorted(buckets.items(), key=lambda item: item[1], reverse=True)
    ]


class SupabaseSubscriptionMetricsRepository:
    def __init__(
        self,
        *,
        supabase_url: str | None = None,
        service_role_key: str | None = None,
        timeout_seconds: float = 15,
        client: httpx.Client | None = None,
    ) -> None:
        self._supabase_url = (
            supabase_url if supabase_url is not None else settings.supabase_url
        ).strip().rstrip("/")
        self._service_role_key = (
            service_role_key
            if service_role_key is not None
            else settings.supabase_service_role_key
        ).strip()
        self._timeout_seconds = timeout_seconds
        self._client = client

    @property
    def is_configured(self) -> bool:
        return bool(self._supabase_url and self._service_role_key)

    def subscription_summary(self) -> list[dict[str, Any]]:
        headers = {
            "apikey": self._service_role_key,
            "Authorization": f"Bearer {self._service_role_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        client = self._client or httpx.Client(timeout=self._timeout_seconds)
        should_close = self._client is None
        try:
            response = client.post(
                f"{self._supabase_url}/rest/v1/rpc/admin_subscription_summary",
                headers=headers,
                json={},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise AdminSubscriptionMetricsError(
                "Supabase RPC admin_subscription_summary request failed."
            ) from error
        finally:
            if should_close:
                client.close()
        if not isinstance(payload, list):
            raise AdminSubscriptionMetricsError(
                "Supabase subscription summary response shape was invalid."
            )
        return [row for row in payload if isinstance(row, dict)]
