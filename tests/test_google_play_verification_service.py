import json
import unittest
from unittest.mock import Mock, patch

import httpx

from app.services.subscription.google_play_verification_service import (
    GooglePlayPurchaseInvalidError,
    GooglePlayVerificationError,
    GooglePlayVerificationNotConfiguredError,
    GooglePlayVerificationService,
)

_FAKE_SERVICE_ACCOUNT = json.dumps(
    {
        "type": "service_account",
        "project_id": "packlox-test",
        "private_key_id": "fake",
        "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n",
        "client_email": "play-verifier@packlox-test.iam.gserviceaccount.com",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
)


def _service(client: httpx.Client, **overrides) -> GooglePlayVerificationService:
    kwargs = {
        "package_name": "com.collectiq.ai",
        "service_account_json": _FAKE_SERVICE_ACCOUNT,
        "pro_product_id": "collectiq_pro_monthly_test",
        "premium_product_id": "collectiq_premium_monthly_test",
        "client": client,
        **overrides,
    }
    return GooglePlayVerificationService(**kwargs)


class GooglePlayVerificationServiceTest(unittest.TestCase):
    def test_not_configured_when_package_name_missing(self) -> None:
        service = GooglePlayVerificationService(package_name="", service_account_json=_FAKE_SERVICE_ACCOUNT)

        self.assertFalse(service.is_configured)
        with self.assertRaises(GooglePlayVerificationNotConfiguredError):
            service.verify_purchase_token("some-token")

    def test_not_configured_when_service_account_missing(self) -> None:
        service = GooglePlayVerificationService(package_name="com.collectiq.ai", service_account_json="")

        self.assertFalse(service.is_configured)

    def test_empty_purchase_token_is_rejected(self) -> None:
        client = _FakePlayClient()
        service = _service(client)

        with self.assertRaises(GooglePlayPurchaseInvalidError):
            service.verify_purchase_token("")

    @patch("app.services.subscription.google_play_verification_service.service_account")
    @patch("app.services.subscription.google_play_verification_service.Request")
    def test_active_subscription_maps_pro_product_id_to_pro_plan(self, _mock_request, mock_service_account) -> None:
        mock_service_account.Credentials.from_service_account_info.return_value = _fake_credentials()
        client = _FakePlayClient(
            response_body={
                "subscriptionState": "SUBSCRIPTION_STATE_ACTIVE",
                "latestOrderId": "order-1",
                "lineItems": [{"productId": "collectiq_pro_monthly_test", "expiryTime": "2026-09-16T00:00:00Z"}],
            }
        )
        service = _service(client)

        entitlement = service.verify_purchase_token("real-token")

        self.assertEqual(entitlement.plan, "pro")
        self.assertTrue(entitlement.is_active)
        self.assertEqual(entitlement.expires_at, "2026-09-16T00:00:00Z")
        self.assertEqual(entitlement.order_id, "order-1")
        self.assertTrue(client.last_url.endswith("/purchases/subscriptionsv2/tokens/real-token"))
        self.assertEqual(client.last_headers["Authorization"], "Bearer fake-access-token")

    @patch("app.services.subscription.google_play_verification_service.service_account")
    @patch("app.services.subscription.google_play_verification_service.Request")
    def test_grace_period_is_still_active(self, _mock_request, mock_service_account) -> None:
        mock_service_account.Credentials.from_service_account_info.return_value = _fake_credentials()
        client = _FakePlayClient(
            response_body={
                "subscriptionState": "SUBSCRIPTION_STATE_IN_GRACE_PERIOD",
                "lineItems": [{"productId": "collectiq_pro_monthly_test", "expiryTime": "2026-08-01T00:00:00Z"}],
            }
        )
        service = _service(client)

        entitlement = service.verify_purchase_token("real-token")

        self.assertTrue(entitlement.is_active)
        self.assertEqual(entitlement.subscription_state, "SUBSCRIPTION_STATE_IN_GRACE_PERIOD")

    @patch("app.services.subscription.google_play_verification_service.service_account")
    @patch("app.services.subscription.google_play_verification_service.Request")
    def test_expired_subscription_is_not_active(self, _mock_request, mock_service_account) -> None:
        mock_service_account.Credentials.from_service_account_info.return_value = _fake_credentials()
        client = _FakePlayClient(
            response_body={
                "subscriptionState": "SUBSCRIPTION_STATE_EXPIRED",
                "lineItems": [{"productId": "collectiq_pro_monthly_test", "expiryTime": "2026-01-01T00:00:00Z"}],
            }
        )
        service = _service(client)

        entitlement = service.verify_purchase_token("real-token")

        self.assertFalse(entitlement.is_active)

    @patch("app.services.subscription.google_play_verification_service.service_account")
    @patch("app.services.subscription.google_play_verification_service.Request")
    def test_unknown_product_id_maps_to_free(self, _mock_request, mock_service_account) -> None:
        mock_service_account.Credentials.from_service_account_info.return_value = _fake_credentials()
        client = _FakePlayClient(
            response_body={
                "subscriptionState": "SUBSCRIPTION_STATE_ACTIVE",
                "lineItems": [{"productId": "some_other_apps_product", "expiryTime": "2026-09-16T00:00:00Z"}],
            }
        )
        service = _service(client)

        entitlement = service.verify_purchase_token("real-token")

        self.assertEqual(entitlement.plan, "free")
        self.assertFalse(entitlement.is_active)

    @patch("app.services.subscription.google_play_verification_service.service_account")
    @patch("app.services.subscription.google_play_verification_service.Request")
    def test_404_from_google_raises_purchase_invalid(self, _mock_request, mock_service_account) -> None:
        mock_service_account.Credentials.from_service_account_info.return_value = _fake_credentials()
        client = _FakePlayClient(status_code=404, response_body={"error": {"message": "not found"}})
        service = _service(client)

        with self.assertRaises(GooglePlayPurchaseInvalidError):
            service.verify_purchase_token("forged-token")

    @patch("app.services.subscription.google_play_verification_service.service_account")
    @patch("app.services.subscription.google_play_verification_service.Request")
    def test_500_from_google_raises_generic_verification_error_not_invalid(
        self, _mock_request, mock_service_account,
    ) -> None:
        # A transient Google-side failure must be distinguishable from "this
        # token is genuinely invalid" -- the caller needs to retry the
        # former, not permanently reject the purchase.
        mock_service_account.Credentials.from_service_account_info.return_value = _fake_credentials()
        client = _FakePlayClient(status_code=500, response_body={"error": "internal"})
        service = _service(client)

        with self.assertRaises(GooglePlayVerificationError):
            service.verify_purchase_token("real-token")
        with self.assertRaises(Exception) as ctx:
            service.verify_purchase_token("real-token")
        self.assertNotIsInstance(ctx.exception, GooglePlayPurchaseInvalidError)


def _fake_credentials():
    creds = Mock()
    creds.token = "fake-access-token"
    creds.refresh = Mock()
    return creds


class _FakePlayClient:
    def __init__(self, *, status_code: int = 200, response_body: dict | None = None) -> None:
        self.status_code = status_code
        self.response_body = response_body or {}
        self.last_url: str | None = None
        self.last_headers: dict | None = None

    def get(self, url: str, headers: dict | None = None, **kwargs):
        self.last_url = url
        self.last_headers = headers
        return httpx.Response(
            status_code=self.status_code,
            json=self.response_body,
            request=httpx.Request("GET", url),
        )

    def close(self) -> None:
        pass


if __name__ == "__main__":
    unittest.main()
