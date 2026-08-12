from __future__ import annotations

from typing import Any

import httpx

from app.core.config import settings
from app.services.subscription.subscription_service import (
    SubscriptionService,
    SubscriptionServiceError,
)


class AdminUserServiceError(Exception):
    """Raised when admin user data cannot be read safely."""


class AdminUserService:
    def __init__(
        self,
        *,
        repository: "SupabaseAdminUserRepository | None" = None,
        subscription_service: SubscriptionService | None = None,
    ) -> None:
        self._repository = repository or SupabaseAdminUserRepository()
        self._subscription_service = subscription_service or SubscriptionService()

    def list_users(self, *, query: str | None = None, limit: int = 50) -> dict[str, Any]:
        if not self._repository.is_configured:
            raise AdminUserServiceError("Supabase admin configuration is missing.")
        users = self._repository.list_users(query=query, limit=limit)
        return {
            "success": True,
            "query": query or "",
            "count": len(users),
            "users": users,
        }

    def get_user_detail(self, user_id: str) -> dict[str, Any]:
        if not self._repository.is_configured:
            raise AdminUserServiceError("Supabase admin configuration is missing.")
        user = self._repository.get_user_detail(user_id)
        user["subscription"] = self._safe_entitlement(user_id)
        user["scanUsage"] = self._safe_scan_usage(user_id)
        return {
            "success": True,
            "user": user,
        }

    def override_subscription(self, *, user_id: str, plan: str) -> dict[str, Any]:
        if not self._repository.is_configured:
            raise AdminUserServiceError("Supabase admin configuration is missing.")
        try:
            entitlement = self._subscription_service.verify_and_grant(
                user_id=user_id,
                plan=plan,
                source="admin_override",
                purchase_token=None,
            )
        except SubscriptionServiceError as error:
            raise AdminUserServiceError(str(error)) from error
        return {"success": True, "userId": user_id, "subscription": entitlement}

    def reset_scan_usage(self, user_id: str) -> dict[str, Any]:
        if not self._repository.is_configured:
            raise AdminUserServiceError("Supabase admin configuration is missing.")
        try:
            result = self._subscription_service.reset_scan_usage(user_id)
        except SubscriptionServiceError as error:
            raise AdminUserServiceError(str(error)) from error
        return {
            "success": True,
            "scanUsage": {
                **result,
                "limit": settings.subscription_free_monthly_scan_limit,
            },
        }

    def _safe_entitlement(self, user_id: str) -> dict[str, Any]:
        try:
            return self._subscription_service.get_entitlement(user_id)
        except SubscriptionServiceError:
            return {"plan": "unknown", "status": "unknown", "source": "unknown", "currentPeriodEnd": None}

    def _safe_scan_usage(self, user_id: str) -> dict[str, Any]:
        try:
            return self._subscription_service.get_scan_usage(
                user_id,
                free_monthly_limit=settings.subscription_free_monthly_scan_limit,
            )
        except SubscriptionServiceError:
            return {
                "used": None,
                "limit": settings.subscription_free_monthly_scan_limit,
                "periodStart": None,
            }

    def resend_confirmation(self, *, email: str) -> dict[str, Any]:
        if not self._repository.is_configured:
            raise AdminUserServiceError("Supabase admin configuration is missing.")
        self._repository.resend_confirmation(email=email)
        return {"success": True, "email": email, "status": "confirmation_requested"}

    def force_logout(self, user_id: str) -> dict[str, Any]:
        if not self._repository.is_configured:
            raise AdminUserServiceError("Supabase admin configuration is missing.")
        self._repository.force_logout(user_id)
        return {"success": True, "userId": user_id, "status": "logout_requested"}

    def disable_user(self, user_id: str) -> dict[str, Any]:
        if not self._repository.is_configured:
            raise AdminUserServiceError("Supabase admin configuration is missing.")
        self._repository.update_auth_user(user_id, {"ban_duration": "876000h"})
        return {"success": True, "userId": user_id, "status": "disabled"}

    def enable_user(self, user_id: str) -> dict[str, Any]:
        if not self._repository.is_configured:
            raise AdminUserServiceError("Supabase admin configuration is missing.")
        self._repository.update_auth_user(user_id, {"ban_duration": "none"})
        return {"success": True, "userId": user_id, "status": "enabled"}

    def update_admin_role(self, *, user_id: str, role: str, is_admin: bool) -> dict[str, Any]:
        if not self._repository.is_configured:
            raise AdminUserServiceError("Supabase admin configuration is missing.")
        profile = self._repository.update_profile_role(user_id=user_id, role=role, is_admin=is_admin)
        return {
            "success": True,
            "userId": user_id,
            "role": profile.get("role") or role,
            "isAdmin": bool(profile.get("is_admin", is_admin)),
            "profile": profile,
        }


class SupabaseAdminUserRepository:
    def __init__(
        self,
        *,
        supabase_url: str | None = None,
        service_role_key: str | None = None,
        timeout_seconds: float = 5,
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

    def list_users(self, *, query: str | None, limit: int) -> list[dict[str, Any]]:
        normalized_query = (query or "").strip().lower()
        params = {"per_page": str(limit)}
        if normalized_query and "@" in normalized_query:
            params["email"] = normalized_query

        payload = self._request("GET", "/auth/v1/admin/users", params=params)
        raw_users = _users_from_payload(payload)
        if normalized_query and "@" not in normalized_query:
            raw_users = [
                user
                for user in raw_users
                if normalized_query in str(user.get("id") or "").lower()
                or normalized_query in str(user.get("email") or "").lower()
            ]
        return [self._admin_user_from_auth_user(user) for user in raw_users[:limit]]

    def get_user_detail(self, user_id: str) -> dict[str, Any]:
        auth_user = self._get_auth_user(user_id)
        if auth_user is None:
            raise AdminUserServiceError(f"Admin user {user_id} was not found.")
        summary = self._admin_user_from_auth_user(auth_user)
        portfolio_items = self._optional_rows("portfolio_items", user_id, limit=10)
        scan_events = self._optional_rows("scan_analysis_events", user_id, limit=10)
        price_alerts = self._optional_rows("price_alerts", user_id, limit=20)
        push_devices = self._optional_rows("push_device_registrations", user_id, limit=20)
        pricing_review_items = [
            item
            for item in portfolio_items
            if bool(item.get("needs_review") or item.get("requires_review"))
            or _pricing_confidence(item) < 70
            or _pricing_value(item) <= 0
        ]
        total_value = sum(_pricing_value(item) for item in portfolio_items)
        return {
            **summary,
            "profile": self._optional_profile(user_id),
            "portfolioValue": round(total_value, 2),
            "recentPortfolioItems": [_compact_portfolio_item(item) for item in portfolio_items],
            "recentScans": [_compact_scan_event(event) for event in scan_events],
            "pricingReviewItems": [_compact_portfolio_item(item) for item in pricing_review_items[:10]],
            "priceAlerts": [_compact_price_alert(alert) for alert in price_alerts],
            "pushDevices": [_compact_push_device(device) for device in push_devices],
        }

    def _admin_user_from_auth_user(self, user: dict[str, Any]) -> dict[str, Any]:
        user_id = str(user.get("id") or "")
        email = str(user.get("email") or "")
        profile = self._optional_profile(user_id)
        portfolio_count = self._optional_count("portfolio_items", user_id)
        scan_count = self._optional_count("scan_analysis_events", user_id)
        device_count = self._optional_count("push_device_registrations", user_id)
        subscription_rows = self._optional_rows("user_subscriptions", user_id, limit=1)
        subscription_row = subscription_rows[0] if subscription_rows else {}
        return {
            "id": user_id,
            "email": email,
            "createdAt": user.get("created_at"),
            "lastSignInAt": user.get("last_sign_in_at"),
            "emailConfirmedAt": user.get("email_confirmed_at") or user.get("confirmed_at"),
            "authStatus": _auth_status(user),
            "displayName": profile.get("display_name") or profile.get("name") or "",
            "portfolioCount": portfolio_count,
            "scanCount": scan_count,
            "pushDeviceCount": device_count,
            "plan": subscription_row.get("plan") or "free",
            "planStatus": subscription_row.get("status") or "active",
            # Console access (role/isAdmin) is unrelated to the subscription
            # plan above — an admin can be on any plan. Sourced from the same
            # profile row already fetched for displayName, so this costs no
            # extra request.
            "role": profile.get("role") or "user",
            "isAdmin": bool(profile.get("is_admin", False)),
            "lastActivityAt": _latest_text(
                user.get("last_sign_in_at"),
                profile.get("updated_at"),
                profile.get("last_seen_at"),
            ),
        }

    def _get_auth_user(self, user_id: str) -> dict[str, Any] | None:
        payload = self._request("GET", f"/auth/v1/admin/users/{user_id}")
        if isinstance(payload, dict) and payload.get("id"):
            return payload
        return None

    def resend_confirmation(self, *, email: str) -> None:
        self._request(
            "POST",
            "/auth/v1/resend",
            json_payload={"type": "signup", "email": email},
        )

    def force_logout(self, user_id: str) -> None:
        self._request("POST", f"/auth/v1/admin/users/{user_id}/logout")

    def update_auth_user(self, user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._request(
            "PUT",
            f"/auth/v1/admin/users/{user_id}",
            json_payload=payload,
        )
        if not isinstance(response, dict):
            raise AdminUserServiceError("Supabase admin user update response shape was invalid.")
        return response

    def update_profile_role(self, *, user_id: str, role: str, is_admin: bool) -> dict[str, Any]:
        payload = self._request(
            "PATCH",
            f"/rest/v1/{settings.admin_profile_table or 'profiles'}",
            params={"id": f"eq.{user_id}", "select": "*"},
            json_payload={"role": role, "is_admin": is_admin},
            extra_headers={"Prefer": "return=representation"},
        )
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            return payload[0]
        raise AdminUserServiceError("Supabase admin profile update response shape was invalid.")

    def _optional_profile(self, user_id: str) -> dict[str, Any]:
        if not user_id:
            return {}
        try:
            payload = self._request(
                "GET",
                f"/rest/v1/{settings.admin_profile_table or 'profiles'}",
                params={"id": f"eq.{user_id}", "select": "*", "limit": "1"},
            )
        except AdminUserServiceError:
            return {}
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            return payload[0]
        return {}

    def _optional_count(self, table: str, user_id: str) -> int | None:
        if not user_id:
            return None
        rows = self._optional_rows(table, user_id, limit=1000)
        return len(rows) if isinstance(rows, list) else None

    def _optional_rows(self, table: str, user_id: str, *, limit: int) -> list[dict[str, Any]]:
        if not user_id:
            return []
        try:
            payload = self._request(
                "GET",
                f"/rest/v1/{table}",
                params={
                    "user_id": f"eq.{user_id}",
                    "select": "*",
                    "limit": str(limit),
                    "order": "updated_at.desc.nullslast,created_at.desc.nullslast",
                },
            )
        except AdminUserServiceError:
            return []
        if not isinstance(payload, list):
            return []
        return [row for row in payload if isinstance(row, dict)]

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_payload: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ):
        headers = {
            "apikey": self._service_role_key,
            "Authorization": f"Bearer {self._service_role_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)
        client = self._client or httpx.Client(timeout=self._timeout_seconds)
        should_close = self._client is None
        try:
            response = client.request(
                method,
                f"{self._supabase_url}{path}",
                headers=headers,
                params=params,
                json=json_payload,
            )
            response.raise_for_status()
            if not response.content:
                return None
            return response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise AdminUserServiceError("Supabase admin user request failed.") from error
        finally:
            if should_close:
                client.close()


def _users_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        raw_users = payload.get("users", [])
    elif isinstance(payload, list):
        raw_users = payload
    else:
        raise AdminUserServiceError("Supabase admin users response shape was invalid.")
    if not isinstance(raw_users, list):
        raise AdminUserServiceError("Supabase admin users response shape was invalid.")
    return [user for user in raw_users if isinstance(user, dict)]


def _auth_status(user: dict[str, Any]) -> str:
    if user.get("deleted_at"):
        return "deleted"
    if user.get("banned_until"):
        return "banned"
    if user.get("email_confirmed_at") or user.get("confirmed_at"):
        return "confirmed"
    return "unconfirmed"


def _latest_text(*values: Any) -> str | None:
    strings = [str(value) for value in values if value]
    return max(strings) if strings else None


def _compact_portfolio_item(row: dict[str, Any]) -> dict[str, Any]:
    data = row.get("data") if isinstance(row.get("data"), dict) else {}
    pricing = row.get("pricing") if isinstance(row.get("pricing"), dict) else data.get("pricing")
    if not isinstance(pricing, dict):
        pricing = {}
    return {
        "id": row.get("id") or data.get("id"),
        "title": (
            row.get("title")
            or row.get("item_name")
            or data.get("title")
            or data.get("itemName")
            or "Untitled"
        ),
        "category": row.get("category") or data.get("category") or "Unknown",
        "price": _pricing_value(row),
        "currency": pricing.get("currency") or row.get("currency") or data.get("currency") or "USD",
        "confidence": _pricing_confidence(row),
        "needsReview": bool(row.get("needs_review") or data.get("needsReview") or data.get("requiresReview")),
        "updatedAt": row.get("updated_at") or data.get("updatedAt") or data.get("lastUpdated"),
    }


def _compact_scan_event(row: dict[str, Any]) -> dict[str, Any]:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else row
    return {
        "id": row.get("id") or payload.get("id"),
        "title": row.get("title") or payload.get("title") or payload.get("itemName") or "Unknown scan",
        "status": row.get("status") or payload.get("status") or "unknown",
        "provider": row.get("provider") or payload.get("provider") or payload.get("aiProvider") or "Unknown",
        "confidence": row.get("confidence") or payload.get("confidence"),
        "createdAt": row.get("created_at") or payload.get("createdAt"),
    }


def _compact_price_alert(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "itemTitle": row.get("item_title") or "Item",
        "portfolioItemId": row.get("portfolio_item_id"),
        "ruleType": row.get("rule_type"),
        "targetAmount": row.get("target_amount"),
        "enabled": bool(row.get("enabled")),
        "status": row.get("status") or "unknown",
        "triggeredAt": row.get("triggered_at"),
        "updatedAt": row.get("updated_at"),
    }


def _compact_push_device(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "platform": row.get("platform") or row.get("device_type") or "unknown",
        "enabled": bool(row.get("enabled")),
        "status": row.get("status") or ("enabled" if row.get("enabled") else "disabled"),
        "lastSeenAt": row.get("last_seen_at"),
        "updatedAt": row.get("updated_at"),
    }


def _pricing_value(row: dict[str, Any]) -> float:
    data = row.get("data") if isinstance(row.get("data"), dict) else {}
    pricing = row.get("pricing") if isinstance(row.get("pricing"), dict) else data.get("pricing")
    if not isinstance(pricing, dict):
        pricing = {}
    value = (
        pricing.get("estimatedMarketValue")
        or pricing.get("marketValue")
        or pricing.get("value")
        or row.get("estimated_value")
        or data.get("estimatedMarketValue")
    )
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def _pricing_confidence(row: dict[str, Any]) -> int:
    data = row.get("data") if isinstance(row.get("data"), dict) else {}
    pricing = row.get("pricing") if isinstance(row.get("pricing"), dict) else data.get("pricing")
    if not isinstance(pricing, dict):
        pricing = {}
    value = (
        pricing.get("pricingConfidence")
        or pricing.get("confidence")
        or row.get("pricing_confidence")
        or data.get("pricingConfidence")
    )
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0
    if 0 <= numeric <= 1:
        numeric *= 100
    return max(0, min(100, round(numeric)))
