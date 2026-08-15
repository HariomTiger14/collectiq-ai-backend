import base64
import json
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app.main import app
from app.services.subscription.apple_verification_service import (
    AppleNotification,
    AppleTransactionInvalidError,
    AppleVerificationNotConfiguredError,
)
from app.services.subscription.apple_verification_service import AppleEntitlement
from app.services.subscription.subscription_service import (
    SubscriptionPurchaseInvalidError,
    SubscriptionServiceError,
)


def _pubsub_body(*, purchase_token: str | None = "real-token") -> dict:
    inner = {"subscriptionNotification": {"purchaseToken": purchase_token, "subscriptionId": "collectiq_pro"}}
    data = base64.b64encode(json.dumps(inner).encode()).decode()
    return {"message": {"data": data, "messageId": "1"}, "subscription": "projects/p/subscriptions/s"}


class GooglePlayRtdnWebhookTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_missing_authorization_header_is_rejected(self) -> None:
        response = self.client.post("/subscription/webhooks/google", json=_pubsub_body())

        self.assertEqual(response.status_code, 401)

    @patch("app.routers.subscription_webhooks.id_token")
    def test_invalid_oidc_token_is_rejected(self, mock_id_token) -> None:
        mock_id_token.verify_oauth2_token.side_effect = ValueError("bad token")

        response = self.client.post(
            "/subscription/webhooks/google",
            json=_pubsub_body(),
            headers={"Authorization": "Bearer forged"},
        )

        self.assertEqual(response.status_code, 401)

    @patch("app.routers.subscription_webhooks.SubscriptionService")
    @patch("app.routers.subscription_webhooks.id_token")
    def test_valid_notification_resyncs_the_purchase_token(self, mock_id_token, mock_service_cls) -> None:
        mock_id_token.verify_oauth2_token.return_value = {"email": "pubsub@google.com"}
        mock_service = Mock()
        mock_service_cls.return_value = mock_service

        response = self.client.post(
            "/subscription/webhooks/google",
            json=_pubsub_body(purchase_token="real-token"),
            headers={"Authorization": "Bearer valid"},
        )

        self.assertEqual(response.status_code, 200)
        mock_service.resync_from_google_play_token.assert_called_once_with("real-token")

    @patch("app.routers.subscription_webhooks.SubscriptionService")
    @patch("app.routers.subscription_webhooks.id_token")
    def test_non_subscription_message_is_skipped_without_calling_the_service(
        self, mock_id_token, mock_service_cls,
    ) -> None:
        mock_id_token.verify_oauth2_token.return_value = {}
        mock_service = Mock()
        mock_service_cls.return_value = mock_service

        response = self.client.post(
            "/subscription/webhooks/google",
            json=_pubsub_body(purchase_token=None),
            headers={"Authorization": "Bearer valid"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json().get("skipped"))
        mock_service.resync_from_google_play_token.assert_not_called()

    @patch("app.routers.subscription_webhooks.SubscriptionService")
    @patch("app.routers.subscription_webhooks.id_token")
    def test_invalid_token_is_acked_not_retried(self, mock_id_token, mock_service_cls) -> None:
        mock_id_token.verify_oauth2_token.return_value = {}
        mock_service = Mock()
        mock_service.resync_from_google_play_token.side_effect = SubscriptionPurchaseInvalidError("nope")
        mock_service_cls.return_value = mock_service

        response = self.client.post(
            "/subscription/webhooks/google",
            json=_pubsub_body(),
            headers={"Authorization": "Bearer valid"},
        )

        self.assertEqual(response.status_code, 200)

    @patch("app.routers.subscription_webhooks.SubscriptionService")
    @patch("app.routers.subscription_webhooks.id_token")
    def test_transient_failure_is_not_acked_so_pubsub_retries(self, mock_id_token, mock_service_cls) -> None:
        mock_id_token.verify_oauth2_token.return_value = {}
        mock_service = Mock()
        mock_service.resync_from_google_play_token.side_effect = SubscriptionServiceError("supabase down")
        mock_service_cls.return_value = mock_service

        response = self.client.post(
            "/subscription/webhooks/google",
            json=_pubsub_body(),
            headers={"Authorization": "Bearer valid"},
        )

        self.assertEqual(response.status_code, 502)


class AppleServerNotificationWebhookTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    @patch("app.routers.subscription_webhooks.SubscriptionService")
    @patch("app.routers.subscription_webhooks.AppleVerificationService")
    def test_valid_notification_resyncs_the_original_transaction_id(
        self, mock_verifier_cls, mock_service_cls,
    ) -> None:
        mock_verifier = Mock()
        mock_verifier.verify_notification.return_value = AppleNotification(
            notification_type="DID_RENEW", signed_transaction_info="nested-jws",
        )
        mock_verifier.verify_signed_transaction.return_value = AppleEntitlement(
            plan="pro", is_active=True, product_id="collectiq_pro_monthly_test",
            expires_at="1234567890000", original_transaction_id="orig-txn-1", revoked=False,
        )
        mock_verifier_cls.return_value = mock_verifier
        mock_service = Mock()
        mock_service_cls.return_value = mock_service

        response = self.client.post("/subscription/webhooks/apple", json={"signedPayload": "outer-jws"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["notificationType"], "DID_RENEW")
        mock_service.resync_from_apple_original_transaction_id.assert_called_once_with(
            "orig-txn-1", signed_transaction="nested-jws",
        )

    @patch("app.routers.subscription_webhooks.AppleVerificationService")
    def test_invalid_signature_is_rejected(self, mock_verifier_cls) -> None:
        mock_verifier = Mock()
        mock_verifier.verify_notification.side_effect = AppleTransactionInvalidError("bad signature")
        mock_verifier_cls.return_value = mock_verifier

        response = self.client.post("/subscription/webhooks/apple", json={"signedPayload": "forged"})

        self.assertEqual(response.status_code, 401)

    @patch("app.routers.subscription_webhooks.AppleVerificationService")
    def test_not_configured_returns_503(self, mock_verifier_cls) -> None:
        mock_verifier = Mock()
        mock_verifier.verify_notification.side_effect = AppleVerificationNotConfiguredError("not configured")
        mock_verifier_cls.return_value = mock_verifier

        response = self.client.post("/subscription/webhooks/apple", json={"signedPayload": "whatever"})

        self.assertEqual(response.status_code, 503)

    @patch("app.routers.subscription_webhooks.SubscriptionService")
    @patch("app.routers.subscription_webhooks.AppleVerificationService")
    def test_notification_with_no_transaction_data_is_skipped(self, mock_verifier_cls, mock_service_cls) -> None:
        mock_verifier = Mock()
        mock_verifier.verify_notification.return_value = AppleNotification(
            notification_type="CONSUMPTION_REQUEST", signed_transaction_info=None,
        )
        mock_verifier_cls.return_value = mock_verifier
        mock_service = Mock()
        mock_service_cls.return_value = mock_service

        response = self.client.post("/subscription/webhooks/apple", json={"signedPayload": "outer-jws"})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json().get("skipped"))
        mock_service.resync_from_apple_original_transaction_id.assert_not_called()


if __name__ == "__main__":
    unittest.main()
