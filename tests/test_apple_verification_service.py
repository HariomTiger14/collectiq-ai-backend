import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from appstoreserverlibrary.models.NotificationTypeV2 import NotificationTypeV2
from appstoreserverlibrary.signed_data_verifier import VerificationException, VerificationStatus

from app.services.subscription.apple_verification_service import (
    AppleTransactionInvalidError,
    AppleVerificationNotConfiguredError,
    AppleVerificationService,
)

# Apple's own library requires certificate extensions ("1.2.840.113635.100.6.11.1"
# on the leaf, "...6.2.1" on the intermediate) that only Apple's real PKI ever
# issues -- there is no way to fabricate a self-signed test chain that passes
# real signature/chain verification, by design (that's exactly what stops
# anyone forging a fake "Apple-verified" receipt, tests included). So these
# tests inject a fake verifier matching SignedDataVerifier's public surface
# to exercise THIS service's own logic (plan mapping, active/expired/revoked
# derivation, error handling) -- the deep crypto itself is Apple's library's
# own well-tested responsibility, not this file's. The one thing tested for
# real, end-to-end with zero mocking, is that a garbage JWS string is
# rejected -- that genuinely fails real signature verification either way.


class AppleVerificationServiceTest(unittest.TestCase):
    def test_garbage_signed_transaction_is_rejected_for_real(self) -> None:
        # No injected verifier -- this exercises the real SignedDataVerifier
        # and its real (committed) Apple root cert against total nonsense.
        service = AppleVerificationService(bundle_id="com.hariom.collectiqai", environment_name="sandbox")

        with self.assertRaises(AppleTransactionInvalidError):
            service.verify_signed_transaction("not.a.jws")

    def test_empty_signed_transaction_is_rejected(self) -> None:
        service = AppleVerificationService(bundle_id="com.hariom.collectiqai")

        with self.assertRaises(AppleTransactionInvalidError):
            service.verify_signed_transaction("")

    def test_not_configured_raises_when_bundle_id_missing(self) -> None:
        service = AppleVerificationService(bundle_id="")

        with self.assertRaises(AppleVerificationNotConfiguredError):
            service.verify_signed_transaction("whatever")

    def test_active_pro_subscription_maps_to_pro_plan(self) -> None:
        future_ms = int((time.time() + 3600) * 1000)
        fake_verifier = Mock()
        fake_verifier.verify_and_decode_signed_transaction.return_value = SimpleNamespace(
            productId="collectiq_pro_monthly_test",
            expiresDate=future_ms,
            revocationDate=None,
            originalTransactionId="original-txn-1",
        )
        service = AppleVerificationService(
            bundle_id="com.hariom.collectiqai",
            pro_product_id="collectiq_pro_monthly_test",
            premium_product_id="collectiq_premium_monthly_test",
            verifier=fake_verifier,
        )

        entitlement = service.verify_signed_transaction("signed-jws")

        self.assertEqual(entitlement.plan, "pro")
        self.assertTrue(entitlement.is_active)
        self.assertFalse(entitlement.revoked)
        self.assertEqual(entitlement.original_transaction_id, "original-txn-1")
        fake_verifier.verify_and_decode_signed_transaction.assert_called_once_with("signed-jws")

    def test_premium_product_id_maps_to_premium_plan(self) -> None:
        future_ms = int((time.time() + 3600) * 1000)
        fake_verifier = Mock()
        fake_verifier.verify_and_decode_signed_transaction.return_value = SimpleNamespace(
            productId="collectiq_premium_monthly_test",
            expiresDate=future_ms,
            revocationDate=None,
            originalTransactionId="original-txn-2",
        )
        service = AppleVerificationService(
            bundle_id="com.hariom.collectiqai",
            pro_product_id="collectiq_pro_monthly_test",
            premium_product_id="collectiq_premium_monthly_test",
            verifier=fake_verifier,
        )

        entitlement = service.verify_signed_transaction("signed-jws")

        self.assertEqual(entitlement.plan, "premium")

    def test_expired_transaction_is_not_active(self) -> None:
        past_ms = int((time.time() - 3600) * 1000)
        fake_verifier = Mock()
        fake_verifier.verify_and_decode_signed_transaction.return_value = SimpleNamespace(
            productId="collectiq_pro_monthly_test",
            expiresDate=past_ms,
            revocationDate=None,
            originalTransactionId="original-txn-3",
        )
        service = AppleVerificationService(
            bundle_id="com.hariom.collectiqai", pro_product_id="collectiq_pro_monthly_test", verifier=fake_verifier,
        )

        entitlement = service.verify_signed_transaction("signed-jws")

        self.assertFalse(entitlement.is_active)

    def test_revoked_transaction_is_not_active_even_if_unexpired(self) -> None:
        # A refund revokes access immediately, regardless of expiresDate --
        # this is exactly the case a naive "expiresDate > now" check alone
        # would miss.
        future_ms = int((time.time() + 3600) * 1000)
        fake_verifier = Mock()
        fake_verifier.verify_and_decode_signed_transaction.return_value = SimpleNamespace(
            productId="collectiq_pro_monthly_test",
            expiresDate=future_ms,
            revocationDate=int(time.time() * 1000),
            originalTransactionId="original-txn-4",
        )
        service = AppleVerificationService(
            bundle_id="com.hariom.collectiqai", pro_product_id="collectiq_pro_monthly_test", verifier=fake_verifier,
        )

        entitlement = service.verify_signed_transaction("signed-jws")

        self.assertTrue(entitlement.revoked)
        self.assertFalse(entitlement.is_active)

    def test_unknown_product_id_maps_to_free(self) -> None:
        future_ms = int((time.time() + 3600) * 1000)
        fake_verifier = Mock()
        fake_verifier.verify_and_decode_signed_transaction.return_value = SimpleNamespace(
            productId="some_other_apps_product",
            expiresDate=future_ms,
            revocationDate=None,
            originalTransactionId="original-txn-5",
        )
        service = AppleVerificationService(
            bundle_id="com.hariom.collectiqai", pro_product_id="collectiq_pro_monthly_test", verifier=fake_verifier,
        )

        entitlement = service.verify_signed_transaction("signed-jws")

        self.assertEqual(entitlement.plan, "free")
        self.assertFalse(entitlement.is_active)

    def test_verification_exception_from_apple_library_is_wrapped(self) -> None:
        fake_verifier = Mock()
        fake_verifier.verify_and_decode_signed_transaction.side_effect = VerificationException(
            VerificationStatus.INVALID_APP_IDENTIFIER
        )
        service = AppleVerificationService(bundle_id="com.hariom.collectiqai", verifier=fake_verifier)

        with self.assertRaises(AppleTransactionInvalidError):
            service.verify_signed_transaction("signed-jws")

    def test_verify_notification_extracts_type_and_nested_transaction(self) -> None:
        fake_verifier = Mock()
        fake_verifier.verify_and_decode_notification.return_value = SimpleNamespace(
            notificationType=NotificationTypeV2.DID_RENEW,
            data=SimpleNamespace(signedTransactionInfo="nested-jws"),
        )
        service = AppleVerificationService(bundle_id="com.hariom.collectiqai", verifier=fake_verifier)

        notification = service.verify_notification("outer-jws")

        self.assertEqual(notification.notification_type, "DID_RENEW")
        self.assertEqual(notification.signed_transaction_info, "nested-jws")

    def test_verify_notification_handles_missing_data(self) -> None:
        fake_verifier = Mock()
        fake_verifier.verify_and_decode_notification.return_value = SimpleNamespace(
            notificationType=NotificationTypeV2.CONSUMPTION_REQUEST,
            data=None,
        )
        service = AppleVerificationService(bundle_id="com.hariom.collectiqai", verifier=fake_verifier)

        notification = service.verify_notification("outer-jws")

        self.assertIsNone(notification.signed_transaction_info)


if __name__ == "__main__":
    unittest.main()
