import base64
import json
import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, status
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token
from pydantic import BaseModel

from app.core.config import settings
from app.services.subscription.apple_verification_service import (
    AppleTransactionInvalidError,
    AppleVerificationNotConfiguredError,
    AppleVerificationService,
)
from app.services.subscription.subscription_service import (
    SubscriptionPurchaseInvalidError,
    SubscriptionService,
    SubscriptionServiceError,
)

router = APIRouter(prefix="/subscription/webhooks", tags=["Subscription Webhooks"])
_logger = logging.getLogger(__name__)


class ApplePushNotificationRequest(BaseModel):
    signedPayload: str


@router.post("/google")
async def google_play_rtdn(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Pub/Sub push endpoint for Google Play Real-Time Developer
    Notifications. Never trusts the notification body for subscription
    state -- it's only a "something changed" signal -- always re-fetches
    the authoritative record from the Play Developer API before writing.
    """
    _verify_pubsub_oidc_token(authorization)
    try:
        body = await request.json()
    except (ValueError, TypeError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON body.")

    purchase_token = _purchase_token_from_pubsub_message(body)
    if not purchase_token:
        # Not a subscription notification (e.g. a one-time-product or test
        # message) -- ack so Pub/Sub doesn't retry something we'll never
        # act on anyway.
        return {"success": True, "skipped": True}

    try:
        SubscriptionService().resync_from_google_play_token(purchase_token)
    except SubscriptionPurchaseInvalidError:
        # Google itself says this token is no longer valid -- nothing a
        # retry would fix. Ack.
        return {"success": True, "skipped": True}
    except SubscriptionServiceError as error:
        _logger.warning("Google Play RTDN resync failed, will retry: %s", error)
        # A transient failure (Supabase briefly down, etc.) -- do NOT ack,
        # so Pub/Sub retries with backoff instead of the update being lost.
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error)) from error
    return {"success": True}


@router.post("/apple")
async def apple_server_notification(payload: ApplePushNotificationRequest) -> dict[str, Any]:
    """App Store Server Notifications V2 endpoint. Apple posts one signed
    JWS payload per subscription lifecycle event (renewal, cancellation,
    grace period, refund, etc.)."""
    verifier = AppleVerificationService()
    try:
        notification = verifier.verify_notification(payload.signedPayload)
    except AppleVerificationNotConfiguredError as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error
    except AppleTransactionInvalidError as error:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(error)) from error

    if not notification.signed_transaction_info:
        # Some notification types (e.g. CONSUMPTION_REQUEST) carry no
        # transaction -- nothing for a subscription entitlement to sync.
        return {"success": True, "skipped": True, "notificationType": notification.notification_type}

    try:
        transaction = verifier.verify_signed_transaction(notification.signed_transaction_info)
    except AppleTransactionInvalidError as error:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(error)) from error
    if not transaction.original_transaction_id:
        return {"success": True, "skipped": True, "notificationType": notification.notification_type}

    try:
        SubscriptionService().resync_from_apple_original_transaction_id(
            transaction.original_transaction_id,
            signed_transaction=notification.signed_transaction_info,
        )
    except SubscriptionServiceError as error:
        _logger.warning("Apple Server Notification resync failed: %s", error)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error)) from error
    return {"success": True, "notificationType": notification.notification_type}


def _purchase_token_from_pubsub_message(body: dict[str, Any]) -> str | None:
    message = body.get("message") if isinstance(body, dict) else None
    data_b64 = message.get("data") if isinstance(message, dict) else None
    if not data_b64:
        return None
    try:
        decoded = json.loads(base64.b64decode(data_b64))
    except (ValueError, TypeError):
        return None
    subscription_notification = decoded.get("subscriptionNotification") if isinstance(decoded, dict) else None
    if not isinstance(subscription_notification, dict):
        return None
    purchase_token = subscription_notification.get("purchaseToken")
    return str(purchase_token) if purchase_token else None


def _verify_pubsub_oidc_token(authorization: str | None) -> None:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing Pub/Sub OIDC token.")
    token = authorization.split(" ", 1)[1].strip()
    try:
        id_token.verify_oauth2_token(token, GoogleAuthRequest(), audience=settings.google_play_rtdn_audience)
    except Exception as error:  # noqa: BLE001 - any verification failure means "reject this request"
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid Pub/Sub OIDC token: {error}",
        ) from error
