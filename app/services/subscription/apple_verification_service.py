from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from appstoreserverlibrary.models.Environment import Environment
from appstoreserverlibrary.signed_data_verifier import SignedDataVerifier, VerificationException

from app.core.config import settings

# Apple's own root CA, downloaded from https://www.apple.com/certificateauthority/
# (AppleRootCA-G3.cer) -- this is what SignedDataVerifier walks the JWS's x5c
# certificate chain up to. It's Apple's public root, not a secret; committing
# it avoids the backend needing outbound access to apple.com just to verify a
# purchase, and avoids re-implementing X.509 chain verification by hand.
_ROOT_CERT_PATH = Path(__file__).parent / "certs" / "AppleRootCA-G3.cer"

_ENVIRONMENT_BY_NAME = {
    "sandbox": Environment.SANDBOX,
    "production": Environment.PRODUCTION,
    "xcode": Environment.XCODE,
    "local_testing": Environment.LOCAL_TESTING,
}


class AppleVerificationError(RuntimeError):
    """Raised when an Apple signed transaction cannot be verified safely."""


class AppleVerificationNotConfiguredError(AppleVerificationError):
    """Raised when the bundle id isn't configured."""


class AppleTransactionInvalidError(AppleVerificationError):
    """Raised when Apple's signature/chain/bundle/environment check fails --
    a forged or wrong-app transaction, distinct from a transient failure so
    callers can 401 instead of 502."""


@dataclass(frozen=True)
class AppleEntitlement:
    plan: str
    is_active: bool
    product_id: str
    expires_at: str | None
    original_transaction_id: str | None
    revoked: bool


@dataclass(frozen=True)
class AppleNotification:
    notification_type: str
    signed_transaction_info: str | None


def _load_root_certificates() -> list[bytes]:
    return [_ROOT_CERT_PATH.read_bytes()]


class AppleVerificationService:
    def __init__(
        self,
        *,
        bundle_id: str | None = None,
        environment_name: str | None = None,
        app_apple_id: str | None = None,
        pro_product_id: str | None = None,
        premium_product_id: str | None = None,
        enable_online_checks: bool = False,
        root_certificates: list[bytes] | None = None,
        verifier: SignedDataVerifier | None = None,
    ) -> None:
        self._bundle_id = (bundle_id if bundle_id is not None else settings.apple_bundle_id).strip()
        self._environment_name = (
            environment_name if environment_name is not None else settings.apple_storekit_environment
        ).strip().lower()
        self._app_apple_id_raw = (
            app_apple_id if app_apple_id is not None else settings.apple_app_apple_id
        ).strip()
        self._pro_product_id = pro_product_id if pro_product_id is not None else settings.apple_pro_product_id
        self._premium_product_id = (
            premium_product_id if premium_product_id is not None else settings.apple_premium_product_id
        )
        self._enable_online_checks = enable_online_checks
        self._root_certificates = root_certificates if root_certificates is not None else _load_root_certificates()
        self._verifier = verifier

    @property
    def is_configured(self) -> bool:
        return bool(self._bundle_id)

    def verify_signed_transaction(self, signed_transaction: str) -> AppleEntitlement:
        if not self.is_configured:
            raise AppleVerificationNotConfiguredError("Apple verification is not configured (bundle id missing).")
        if not signed_transaction or not signed_transaction.strip():
            raise AppleTransactionInvalidError("Empty signed transaction.")

        try:
            decoded = self._verifier_instance().verify_and_decode_signed_transaction(signed_transaction)
        except VerificationException as error:
            raise AppleTransactionInvalidError(f"Apple rejected this transaction: {error}") from error

        product_id = str(decoded.productId or "")
        if product_id == self._premium_product_id:
            plan = "premium"
        elif product_id == self._pro_product_id:
            plan = "pro"
        else:
            plan = "free"

        now_ms = int(time.time() * 1000)
        expires_ms = decoded.expiresDate or 0
        revoked = decoded.revocationDate is not None
        is_active = plan != "free" and not revoked and expires_ms > now_ms

        return AppleEntitlement(
            plan=plan,
            is_active=is_active,
            product_id=product_id,
            expires_at=str(expires_ms) if expires_ms else None,
            original_transaction_id=decoded.originalTransactionId,
            revoked=revoked,
        )

    def verify_notification(self, signed_payload: str) -> AppleNotification:
        """Verify + decode an App Store Server Notification V2 payload.

        Only pulls out the notificationType and the nested signedTransactionInfo
        JWS -- the caller re-verifies that nested JWS itself via
        verify_signed_transaction() rather than trusting it just because it
        arrived inside an already-verified outer envelope, since the two are
        signed and checked independently by design.
        """
        if not self.is_configured:
            raise AppleVerificationNotConfiguredError("Apple verification is not configured (bundle id missing).")
        if not signed_payload or not signed_payload.strip():
            raise AppleTransactionInvalidError("Empty signed notification payload.")
        try:
            decoded = self._verifier_instance().verify_and_decode_notification(signed_payload)
        except VerificationException as error:
            raise AppleTransactionInvalidError(f"Apple rejected this notification: {error}") from error
        notification_type = decoded.notificationType.value if decoded.notificationType else "UNKNOWN"
        signed_transaction_info = decoded.data.signedTransactionInfo if decoded.data else None
        return AppleNotification(notification_type=notification_type, signed_transaction_info=signed_transaction_info)

    def _verifier_instance(self) -> SignedDataVerifier:
        if self._verifier is not None:
            return self._verifier
        environment = _ENVIRONMENT_BY_NAME.get(self._environment_name, Environment.SANDBOX)
        app_apple_id = int(self._app_apple_id_raw) if self._app_apple_id_raw.isdigit() else None
        self._verifier = SignedDataVerifier(
            root_certificates=self._root_certificates,
            enable_online_checks=self._enable_online_checks,
            environment=environment,
            bundle_id=self._bundle_id,
            app_apple_id=app_apple_id,
        )
        return self._verifier
