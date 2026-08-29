from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx
from google.auth.transport.requests import Request
from google.oauth2 import service_account

from app.core.config import settings

_ANDROID_PUBLISHER_SCOPE = "https://www.googleapis.com/auth/androidpublisher"
_ANDROID_PUBLISHER_BASE = "https://androidpublisher.googleapis.com/androidpublisher/v3"

# https://developer.android.com/google/play/billing/subscriptions#lifecycle --
# these three are the only states where the subscriber currently has access;
# everything else (paused/on hold/pending/canceled-but-expired) means "no".
_ACTIVE_SUBSCRIPTION_STATES = {
    "SUBSCRIPTION_STATE_ACTIVE",
    "SUBSCRIPTION_STATE_IN_GRACE_PERIOD",
    # A canceled subscription still has access through its current paid
    # period -- Google doesn't flip it to EXPIRED until that period ends.
    "SUBSCRIPTION_STATE_CANCELED",
}

class GooglePlayVerificationError(RuntimeError):
    """Raised when a Google Play purchase token cannot be verified safely."""


class GooglePlayVerificationNotConfiguredError(GooglePlayVerificationError):
    """Raised when the service account / package name isn't configured."""


class GooglePlayPurchaseInvalidError(GooglePlayVerificationError):
    """Raised when Google reports the token as invalid/not found (a forged
    or already-consumed token) -- distinct from a transient API failure so
    callers can 401 instead of 502."""


@dataclass(frozen=True)
class GooglePlayEntitlement:
    plan: str
    is_active: bool
    subscription_state: str
    product_id: str
    expires_at: str | None
    order_id: str | None


class GooglePlayVerificationService:
    def __init__(
        self,
        *,
        package_name: str | None = None,
        service_account_json: str | None = None,
        pro_product_id: str | None = None,
        premium_product_id: str | None = None,
        timeout_seconds: float | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._package_name = (
            package_name if package_name is not None else settings.google_play_package_name
        ).strip()
        self._service_account_json = (
            service_account_json
            if service_account_json is not None
            else settings.google_play_service_account_json
        )
        self._product_id_to_plan = {
            (pro_product_id if pro_product_id is not None else settings.google_play_pro_product_id): "pro",
            (
                premium_product_id
                if premium_product_id is not None
                else settings.google_play_premium_product_id
            ): "premium",
        }
        self._timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else settings.google_play_timeout_seconds
        )
        self._client = client

    @property
    def is_configured(self) -> bool:
        return bool(self._package_name and self._service_account_json)

    def verify_purchase_token(self, purchase_token: str) -> GooglePlayEntitlement:
        if not self.is_configured:
            raise GooglePlayVerificationNotConfiguredError(
                "Google Play verification is not configured (package name or service account missing)."
            )
        if not purchase_token or not purchase_token.strip():
            raise GooglePlayPurchaseInvalidError("Empty purchase token.")

        headers = {
            "Authorization": f"Bearer {self._bearer_token()}",
            "Accept": "application/json",
        }
        url = (
            f"{_ANDROID_PUBLISHER_BASE}/applications/{self._package_name}"
            f"/purchases/subscriptionsv2/tokens/{purchase_token}"
        )
        client = self._client or httpx.Client(timeout=self._timeout_seconds)
        should_close = self._client is None
        try:
            response = client.get(url, headers=headers)
        except httpx.HTTPError as error:
            raise GooglePlayVerificationError("Google Play Developer API request failed.") from error
        finally:
            if should_close:
                client.close()

        if response.status_code in (400, 404):
            raise GooglePlayPurchaseInvalidError(
                f"Google Play rejected this purchase token (status {response.status_code})."
            )
        if response.status_code != 200:
            raise GooglePlayVerificationError(
                f"Google Play Developer API returned status {response.status_code}: {response.text[:300]}"
            )
        try:
            payload = response.json()
        except ValueError as error:
            raise GooglePlayVerificationError("Google Play Developer API response was not valid JSON.") from error

        return self._entitlement_from_payload(payload)

    def _entitlement_from_payload(self, payload: dict[str, Any]) -> GooglePlayEntitlement:
        subscription_state = str(payload.get("subscriptionState") or "")
        line_items = payload.get("lineItems") or []
        line_item = line_items[0] if line_items and isinstance(line_items[0], dict) else {}
        product_id = str(line_item.get("productId") or "")
        expiry_time = line_item.get("expiryTime")
        plan = self._product_id_to_plan.get(product_id, "free")
        return GooglePlayEntitlement(
            plan=plan,
            is_active=subscription_state in _ACTIVE_SUBSCRIPTION_STATES and plan != "free",
            subscription_state=subscription_state,
            product_id=product_id,
            expires_at=str(expiry_time) if expiry_time else None,
            order_id=str(payload.get("latestOrderId")) if payload.get("latestOrderId") else None,
        )

    def _bearer_token(self) -> str:
        try:
            service_account_info = json.loads(self._service_account_json)
        except (TypeError, ValueError) as error:
            raise GooglePlayVerificationError("GOOGLE_PLAY_SERVICE_ACCOUNT_JSON is not valid JSON.") from error
        try:
            credentials = service_account.Credentials.from_service_account_info(
                service_account_info,
                scopes=[_ANDROID_PUBLISHER_SCOPE],
            )
            credentials.refresh(Request())
        except Exception as error:  # noqa: BLE001 - any auth failure should surface as a verification error
            raise GooglePlayVerificationError("Failed to mint a Google Play Developer API access token.") from error
        if not credentials.token:
            raise GooglePlayVerificationError("Google returned no access token for the service account.")
        return credentials.token
