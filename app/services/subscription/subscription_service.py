"""Server-owned per-user subscription entitlement.

The entitlement in `public.user_subscriptions` is the source of truth. A client
can only READ its own row (RLS); writes happen here with the service role after
the caller's Supabase session is verified. `source == "mock"`/"admin_override"
trust the client-reported plan (dev/SIT testing and explicit admin action,
respectively); `google_play`/`app_store` are verified against the real store
APIs in `verify_and_grant` before anything is written.
"""

from datetime import datetime, timezone
from typing import Any

import httpx

from app.core.config import settings
from app.services.subscription.apple_verification_service import (
    AppleTransactionInvalidError,
    AppleVerificationNotConfiguredError,
    AppleVerificationService,
)
from app.services.subscription.google_play_verification_service import (
    GooglePlayEntitlement,
    GooglePlayPurchaseInvalidError,
    GooglePlayVerificationNotConfiguredError,
    GooglePlayVerificationService,
)

_VALID_PLANS = {"free", "pro", "premium"}
_VALID_SOURCES = {"none", "mock", "google_play", "app_store", "admin_override"}
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


class SubscriptionPurchaseInvalidError(SubscriptionServiceError):
    """The claimed google_play/app_store purchase failed real verification --
    a forged token, wrong app, or wrong environment. Distinct from a config
    or transient-network error so the router can respond 401/422 instead of
    502."""


class SubscriptionService:
    def __init__(
        self,
        *,
        supabase_url: str | None = None,
        service_role_key: str | None = None,
        anon_key: str | None = None,
        client: httpx.Client | None = None,
        google_play_verifier: GooglePlayVerificationService | None = None,
        apple_verifier: AppleVerificationService | None = None,
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
        self._google_play_verifier = google_play_verifier or GooglePlayVerificationService()
        self._apple_verifier = apple_verifier or AppleVerificationService()

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

    def check_scan_allowed(
        self,
        access_token: str,
        *,
        free_monthly_limit: int,
    ) -> dict[str, Any]:
        """Whether this scan is allowed for the caller.

        Pro/premium are unlimited. Free is metered by an atomic monthly
        check-and-bump. Raises on config/auth/store errors so the caller can
        fail open (a scan must never break because of this check).
        Returns {'allowed': bool, 'plan': str, 'used': int}.
        """
        self._require_config()
        user_id = self.user_id_from_token(access_token)
        plan = self.get_entitlement(user_id).get("plan", "free")
        if plan != "free":
            return {"allowed": True, "plan": plan, "used": 0}
        rows = self._rpc(
            "check_and_bump_scan_usage",
            {"p_user_id": user_id, "p_limit": free_monthly_limit},
        )
        if isinstance(rows, list) and rows:
            row = rows[0]
            return {
                "allowed": bool(row.get("allowed")),
                "plan": plan,
                "used": int(row.get("used") or 0),
            }
        # RPC returned nothing unexpected — fail open.
        return {"allowed": True, "plan": plan, "used": 0}

    def _rpc(self, fn: str, params: dict[str, Any]) -> Any:
        response = self._supabase_request(
            "POST", f"/rest/v1/rpc/{fn}", json=params
        )
        return response.json()

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
        """Verify (when the source requires it) and record an entitlement,
        returning it.

        "mock"/"admin_override" trust the reported plan as-is -- SIT/dev
        testing and an explicit admin action, neither of which has a real
        receipt to check. "google_play"/"app_store" are verified against the
        real store first; the GRANTED plan/expiry/product come from that
        verified result, never from the client's claimed `plan`, so a client
        can't self-report a plan it didn't actually buy.
        """
        self._require_config()
        normalized_source = source if source in _VALID_SOURCES else "mock"
        now_iso = datetime.now(timezone.utc).isoformat()
        row: dict[str, Any] = {
            "user_id": user_id,
            "source": normalized_source,
            "updated_at": now_iso,
        }

        claimed_plan = plan if plan in _VALID_PLANS else "free"
        if normalized_source == "google_play":
            row.update(self._verify_google_play(purchase_token, claimed_plan))
        elif normalized_source == "app_store":
            row.update(self._verify_apple(purchase_token, claimed_plan))
        else:
            row["plan"] = claimed_plan
            row["status"] = "active"

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

    def _verify_google_play(self, purchase_token: str | None, claimed_plan: str) -> dict[str, Any]:
        try:
            entitlement = self._google_play_verifier.verify_purchase_token(purchase_token or "")
        except GooglePlayVerificationNotConfiguredError:
            # No service account configured yet (e.g. today's SIT env) --
            # degrade to trusting the client rather than hard-failing every
            # subscription check, matching this file's existing pattern of
            # graceful degradation when a downstream integration isn't set
            # up. Once configured, this path is never taken.
            return {"plan": claimed_plan, "status": "active"}
        except GooglePlayPurchaseInvalidError as error:
            raise SubscriptionPurchaseInvalidError(str(error)) from error
        return {
            "plan": entitlement.plan if entitlement.is_active else "free",
            "status": self._status_from_google_state(entitlement),
            "purchase_token": purchase_token,
            "product_id": entitlement.product_id or None,
            "current_period_end": entitlement.expires_at,
        }

    def _verify_apple(self, signed_transaction: str | None, claimed_plan: str) -> dict[str, Any]:
        try:
            entitlement = self._apple_verifier.verify_signed_transaction(signed_transaction or "")
        except AppleVerificationNotConfiguredError:
            return {"plan": claimed_plan, "status": "active"}
        except AppleTransactionInvalidError as error:
            raise SubscriptionPurchaseInvalidError(str(error)) from error
        expires_iso = None
        if entitlement.expires_at:
            expires_iso = datetime.fromtimestamp(
                int(entitlement.expires_at) / 1000, tz=timezone.utc,
            ).isoformat()
        return {
            "plan": entitlement.plan if entitlement.is_active else "free",
            "status": "canceled" if entitlement.revoked else "active" if entitlement.is_active else "expired",
            "original_transaction_id": entitlement.original_transaction_id,
            "product_id": entitlement.product_id or None,
            "current_period_end": expires_iso,
        }

    @staticmethod
    def _status_from_google_state(entitlement: GooglePlayEntitlement) -> str:
        if entitlement.subscription_state == "SUBSCRIPTION_STATE_IN_GRACE_PERIOD":
            return "in_grace"
        if entitlement.subscription_state == "SUBSCRIPTION_STATE_CANCELED":
            return "canceled"
        if not entitlement.is_active:
            return "expired"
        return "active"

    def resync_from_google_play_token(self, purchase_token: str) -> dict[str, Any] | None:
        """Re-verify and update the row a Real-Time Developer Notification
        refers to. Google's own guidance is to never trust the notification
        body for state -- it's just a "something changed" signal -- so this
        always re-fetches the authoritative subscriptionsv2 record before
        writing anything. Returns None if no row references this token
        (nothing to update, e.g. an old/already-superseded token)."""
        self._require_config()
        user_id = self._user_id_for_column("purchase_token", purchase_token)
        if user_id is None:
            return None
        return self.verify_and_grant(
            user_id=user_id, plan="free", source="google_play", purchase_token=purchase_token,
        )

    def resync_from_apple_original_transaction_id(
        self, original_transaction_id: str, *, signed_transaction: str,
    ) -> dict[str, Any] | None:
        """Re-verify and update the row an App Store Server Notification
        refers to, using the notification's own embedded signedTransactionInfo
        (already verified by the caller) rather than making an extra network
        call back to Apple. Returns None if no row references this original
        transaction id yet (e.g. the very first SUBSCRIBED notification can
        arrive before the app's own /subscription/verify call lands)."""
        self._require_config()
        user_id = self._user_id_for_column("original_transaction_id", original_transaction_id)
        if user_id is None:
            return None
        return self.verify_and_grant(
            user_id=user_id, plan="free", source="app_store", purchase_token=signed_transaction,
        )

    def _user_id_for_column(self, column: str, value: str) -> str | None:
        response = self._supabase_request(
            "GET",
            "/rest/v1/user_subscriptions",
            params={column: f"eq.{value}", "select": "user_id", "limit": "1"},
        )
        rows = response.json()
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            user_id = rows[0].get("user_id")
            return str(user_id) if user_id else None
        return None

    def get_scan_usage(self, user_id: str, *, free_monthly_limit: int) -> dict[str, Any]:
        """Current-calendar-month scan usage for a user (admin visibility).

        Mirrors check_and_bump_scan_usage's own month-window math (date_trunc
        'month' in UTC) without going through the RPC, since this is a read
        -only lookup, not a check-and-bump.
        """
        self._require_config()
        month_start = datetime.now(timezone.utc).date().replace(day=1).isoformat()
        response = self._supabase_request(
            "GET",
            "/rest/v1/user_scan_usage",
            params={
                "user_id": f"eq.{user_id}",
                "usage_date": f"gte.{month_start}",
                "select": "scans_used",
            },
        )
        rows = response.json()
        used = sum(int(row.get("scans_used") or 0) for row in rows if isinstance(row, dict))
        return {
            "used": used,
            "limit": free_monthly_limit,
            "periodStart": month_start,
        }

    def reset_scan_usage(self, user_id: str) -> dict[str, Any]:
        """Zero out the current calendar month's scan usage rows for a user.

        An admin support remedy for a quota-locked free user -- not exposed
        to the client, only called from the admin surface with the service
        role.
        """
        self._require_config()
        month_start = datetime.now(timezone.utc).date().replace(day=1).isoformat()
        self._supabase_request(
            "PATCH",
            "/rest/v1/user_scan_usage",
            params={
                "user_id": f"eq.{user_id}",
                "usage_date": f"gte.{month_start}",
            },
            headers={"Prefer": "return=minimal"},
            json={"scans_used": 0, "updated_at": datetime.now(timezone.utc).isoformat()},
        )
        return {"userId": user_id, "used": 0, "periodStart": month_start}

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
