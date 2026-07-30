import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app


class AdminAuthTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_pricing_health_accepts_bearer_admin_import_token(self) -> None:
        with patch("app.routers.admin_auth.settings") as auth_settings, patch(
            "app.routers.admin_pricing.PricingHealthService",
        ) as service:
            auth_settings.admin_import_token = "secret-token"
            service.return_value.health.return_value = {
                "success": True,
                "status": "healthy",
                "providers": [],
                "cache": {},
            }

            response = self.client.get(
                "/admin/pricing/health",
                headers={"Authorization": "Bearer secret-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "healthy")

    def test_pricing_health_rejects_missing_admin_import_token(self) -> None:
        with patch("app.routers.admin_auth.settings") as auth_settings:
            auth_settings.admin_import_token = "secret-token"

            response = self.client.get("/admin/pricing/health")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "unauthorized")

    def test_push_job_accepts_bearer_admin_job_token(self) -> None:
        with patch("app.routers.admin_auth.settings") as auth_settings, patch(
            "app.routers.push.PriceAlertPushService",
        ) as service:
            auth_settings.admin_job_token = "job-token"
            service.return_value.dispatch_triggered_alerts.return_value.to_dict.return_value = {
                "success": True,
                "scannedAlerts": 0,
                "attemptedDeliveries": 0,
            }

            response = self.client.post(
                "/admin/push/price-alerts/run?dryRun=true",
                headers={"Authorization": "Bearer job-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertTrue(response.json()["dryRun"])

    def test_push_job_rejects_invalid_admin_job_token(self) -> None:
        with patch("app.routers.admin_auth.settings") as auth_settings:
            auth_settings.admin_job_token = "job-token"

            response = self.client.post(
                "/admin/push/price-alerts/run",
                headers={"Authorization": "Bearer wrong-token"},
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "unauthorized")

    def test_push_job_reports_unconfigured_admin_job_token(self) -> None:
        with patch("app.routers.admin_auth.settings") as auth_settings:
            auth_settings.admin_job_token = ""

            response = self.client.post(
                "/admin/push/price-alerts/run",
                headers={"Authorization": "Bearer any-token"},
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "admin_job_not_configured")


if __name__ == "__main__":
    unittest.main()
