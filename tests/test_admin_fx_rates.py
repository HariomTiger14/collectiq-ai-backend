import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.services.pricing.fx_rate_service import FxRateServiceError


class AdminFxRatesRefreshTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_requires_configured_admin_token(self) -> None:
        with patch("app.routers.admin_auth.settings") as settings:
            settings.admin_job_token = ""

            response = self.client.post("/admin/pricing/fx-rates/refresh")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "admin_job_not_configured")

    def test_rejects_invalid_admin_token(self) -> None:
        with patch("app.routers.admin_auth.settings") as settings:
            settings.admin_job_token = "secret-token"

            response = self.client.post(
                "/admin/pricing/fx-rates/refresh",
                headers={"X-Admin-Token": "wrong-token"},
            )

        self.assertEqual(response.status_code, 401)

    def test_returns_rows_written_on_success(self) -> None:
        with patch("app.routers.admin_auth.settings") as auth_settings, patch(
            "app.routers.admin_fx_rates.FxRateService"
        ) as service_cls:
            auth_settings.admin_job_token = "secret-token"
            service_cls.return_value.refresh_latest.return_value = 3

            response = self.client.post(
                "/admin/pricing/fx-rates/refresh",
                headers={"X-Admin-Token": "secret-token"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["rowsWritten"], 3)

    def test_returns_503_when_the_service_fails(self) -> None:
        with patch("app.routers.admin_auth.settings") as auth_settings, patch(
            "app.routers.admin_fx_rates.FxRateService"
        ) as service_cls:
            auth_settings.admin_job_token = "secret-token"
            service_cls.return_value.refresh_latest.side_effect = FxRateServiceError("boom")

            response = self.client.post(
                "/admin/pricing/fx-rates/refresh",
                headers={"X-Admin-Token": "secret-token"},
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "fx_rate_refresh_failed")


class AdminFxRatesBackfillTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_passes_the_date_range_through(self) -> None:
        with patch("app.routers.admin_auth.settings") as auth_settings, patch(
            "app.routers.admin_fx_rates.FxRateService"
        ) as service_cls:
            auth_settings.admin_job_token = "secret-token"
            service_cls.return_value.backfill_historical.return_value = 180

            response = self.client.post(
                "/admin/pricing/fx-rates/backfill"
                "?startDate=2026-01-01&endDate=2026-08-22",
                headers={"X-Admin-Token": "secret-token"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["rowsWritten"], 180)
        service_cls.return_value.backfill_historical.assert_called_once_with(
            start_date="2026-01-01", end_date="2026-08-22"
        )


if __name__ == "__main__":
    unittest.main()
