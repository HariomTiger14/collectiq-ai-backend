"""Server-owned per-user subscription entitlement.

The entitlement in `public.user_subscriptions` is the source of truth. A client
can only READ its own row (RLS); writes happen here with the service role after
the caller's Supabase session is verified. Store-receipt validation is stubbed
("mock" trusts the token) and slots into `verify_and_grant` later.
"""

from datetime import datetime, timezone
from typing import Any

import httpx

from app.core.config import settings

_VALID_PLANS = {"free", "pro", "premium"}
_VALID_SOURCES = {"none", "mock", "google_play", "app_store"}
_DEFAULT_ENTITLEMENT = {
    "plan": "free",
    "status": "active",
    "source": "none",
    "currentPeriodEnd": None,
}


class SubscriptionServiceError(Exception):
    """Subscription backend could not complete the request."""


class SubscriptionNotConfiguredError(SubscriptionServiceError):
    """Supabase is not configured for subscription persistence."""


class SubscriptionUnauthorizedError(SubscriptionServiceError):
    """The caller's Supabase session is missing or invalid."""


class SubscriptionService:
    def __init__(
        self,
        *,
        supabase_url: str | None = None,
        service_role_key: str | None = None,
        anon_key: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._supabase_url = (
            supabase_url if supabase_url is not None else settings.supabase_url
        ).rstrip("/")
        self._service_role_key = (
            service_role_key
            if service_role_key is not None
            else settings.supabase_service_role_key
        )
        self._anon_key = (
            anon_key if anon_key is not None else settings.supabase_anon_key
        )
        self._client = client or httpx.Client(timeout=15)

    # -- public API ---------------------------------------------------------

    def user_id_from_token(self, access_token: str) -> str:
        """Resolve the Supabase user id from a bearer access token."""
        self._require_config()
        token = (access_token or "").strip()
        if not token:
            raise SubscriptionUnauthorizedError("Missing access token.")
        try:
            response = self._client.get(
                f"{self._supabase_url}/auth/v1/user",
                headers={
                    "apikey": self._anon_key,
                    "Authorization": f"Bearer {token}",
                },
            )
        except httpx.HTTPError as error:
            raise SubscriptionServiceError(
                f"Could not reach the auth service: {error}"
            ) from error
        if response.status_code != 200:
            raise SubscriptionUnauthorizedError("Invalid or expired session.")
        user_id = (response.json() or {}).get("id")
        if not user_id:
            raise SubscriptionUnauthorizedError("Could not resolve user.")
        return str(user_id)

    def get_entitlement(self, user_id: str) -> dict[str, Any]:
        """Return the user's current entitlement (defaults to free)."""
        self._require_config()
        response = self._supabase_request(
            "GET",
            "/rest/v1/user_subscriptions",
            params={
                "user_id": f"eq.{user_id}",
                "select": "plan,status,source,current_period_end",
                "limit": "1",
            },
        )
        rows = response.json()
        if isinstance(rows, list) and rows:
            return self._to_entitlement(rows[0])
        return dict(_DEFAULT_ENTITLEMENT)

    def verify_and_grant(
        self,
        *,
        user_id: str,
        plan: str,
        source: str,
        purchase_token: str | None,
    ) -> dict[str, Any]:
        """Record an entitlement for the user and return it.

        Mock mode trusts the reported plan/token. Real Google Play / App Store
        receipt validation belongs here, keyed on `source` + `purchase_token`,
        before the row is written.
        """
        self._require_config()
        normalized_plan = plan if plan in _VALID_PLANS else "free"
        normalized_source = source if source in _VALID_SOURCES else "mock"
        now_iso = datetime.now(timezone.utc).isoformat()
        row = {
            "user_id": user_id,
            "plan": normalized_plan,
            "status": "active",
            "source": normalized_source,
            "updated_at": now_iso,
        }
        response = self._supabase_request(
            "POST",
            "/rest/v1/user_subscriptions",
            params={"on_conflict": "user_id"},
            headers={
                "Prefer": "resolution=merge-duplicates,return=representation",
            },
            json=[row],
        )
        rows = response.json()
        if isinstance(rows, list) and rows:
            return self._to_entitlement(rows[0])
        # Fall back to a read if the store didn't return the representation.
        return self.get_entitlement(user_id)

    # -- internals ----------------------------------------------------------

    def _require_config(self) -> None:
        if not self._supabase_url or not self._service_role_key:
            raise SubscriptionNotConfiguredError(
                "Supabase is not configured for subscription persistence."
            )

    def _to_entitlement(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "plan": row.get("plan") or "free",
            "status": row.get("status") or "active",
            "source": row.get("source") or "none",
            "currentPeriodEnd": row.get("current_period_end"),
        }

    def _supabase_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        json: Any | None = None,
    ) -> httpx.Response:
        try:
            response = self._client.request(
                method,
                f"{self._supabase_url}{path}",
                params=params,
                headers={
                    "apikey": self._service_role_key,
                    "Authorization": f"Bearer {self._service_role_key}",
                    "Content-Type": "application/json",
                    **(headers or {}),
                },
                json=json,
            )
        except httpx.HTTPError as error:
            raise SubscriptionServiceError(
                f"Subscription store request failed: {error}"
            ) from error
        if response.status_code >= 400:
            raise SubscriptionServiceError(
                f"Subscription store returned {response.status_code}: {response.text}"
            )
        return response
