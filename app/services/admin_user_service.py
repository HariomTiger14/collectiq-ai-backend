from __future__ import annotations

from typing import Any

import httpx

from app.core.config import settings


class AdminUserServiceError(Exception):
    """Raised when admin user data cannot be read safely."""


class AdminUserService:
    def __init__(
        self,
        *,
        repository: "SupabaseAdminUserRepository | None" = None,
    ) -> None:
        self._repository = repository or SupabaseAdminUserRepository()

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

    def _admin_user_from_auth_user(self, user: dict[str, Any]) -> dict[str, Any]:
        user_id = str(user.get("id") or "")
        email = str(user.get("email") or "")
        profile = self._optional_profile(user_id)
        portfolio_count = self._optional_count("portfolio_items", user_id)
        scan_count = self._optional_count("scan_analysis_events", user_id)
        device_count = self._optional_count("push_device_registrations", user_id)
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
            "lastActivityAt": _latest_text(
                user.get("last_sign_in_at"),
                profile.get("updated_at"),
                profile.get("last_seen_at"),
            ),
        }

    def _optional_profile(self, user_id: str) -> dict[str, Any]:
        if not user_id:
            return {}
        try:
            payload = self._request(
                "GET",
                "/rest/v1/profiles",
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
        try:
            payload = self._request(
                "GET",
                f"/rest/v1/{table}",
                params={"user_id": f"eq.{user_id}", "select": "id", "limit": "1000"},
            )
        except AdminUserServiceError:
            return None
        return len(payload) if isinstance(payload, list) else None

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
    ):
        headers = {
            "apikey": self._service_role_key,
            "Authorization": f"Bearer {self._service_role_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        client = self._client or httpx.Client(timeout=self._timeout_seconds)
        should_close = self._client is None
        try:
            response = client.request(
                method,
                f"{self._supabase_url}{path}",
                headers=headers,
                params=params,
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
