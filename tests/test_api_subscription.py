import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app.main import app
from app.services.subscription.subscription_service import SubscriptionPurchaseInvalidError


class VerifySubscriptionEndpointTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    @patch("app.routers.api_subscription._service")
    def test_invalid_purchase_returns_422_not_502(self, mock_service) -> None:
        mock_service.user_id_from_token.return_value = "user-1"
        mock_service.verify_and_grant.side_effect = SubscriptionPurchaseInvalidError("forged token")

        response = self.client.post(
            "/subscription/verify",
            json={"plan": "pro", "source": "google_play", "purchaseToken": "forged"},
            headers={"Authorization": "Bearer session-token"},
        )

        self.assertEqual(response.status_code, 422)

    @patch("app.routers.api_subscription._service")
    def test_valid_verification_returns_the_granted_entitlement(self, mock_service) -> None:
        mock_service.user_id_from_token.return_value = "user-1"
        mock_service.verify_and_grant.return_value = {
            "plan": "pro", "status": "active", "source": "google_play", "currentPeriodEnd": None,
        }

        response = self.client.post(
            "/subscription/verify",
            json={"plan": "premium", "source": "google_play", "purchaseToken": "real-token"},
            headers={"Authorization": "Bearer session-token"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["plan"], "pro")
        mock_service.verify_and_grant.assert_called_once_with(
            user_id="user-1", plan="premium", source="google_play", purchase_token="real-token",
        )


if __name__ == "__main__":
    unittest.main()
