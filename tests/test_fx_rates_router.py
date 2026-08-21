import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.services.pricing.fx_rate_service import FxRateServiceError


class FxRatesEndpointTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_returns_static_fallback_when_not_configured(self) -> None:
        with patch("app.routers.pricing.FxRateService") as service_cls:
            service_cls.return_value.is_configured = False

            response = self.client.get("/api/pricing/fx-rates")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["current"]["USD"], 1.0)
        self.assertIn("AUD", payload["current"])
        self.assertEqual(payload["rates"], [])
        self.assertIn("AUD", payload["supportedCurrencies"])

    def test_returns_stored_rates_and_current_rates_when_configured(self) -> None:
        with patch("app.routers.pricing.FxRateService") as service_cls:
            instance = service_cls.return_value
            instance.is_configured = True
            instance.current_rates.return_value = {"USD": 1.0, "AUD": 1.53}
            instance.rates_for_range.return_value = [
                {"rate_date": "2026-08-22", "currency": "AUD", "usd_rate": 1.53}
            ]

            response = self.client.get(
                "/api/pricing/fx-rates?fromDate=2026-08-01&toDate=2026-08-22"
            )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["current"]["AUD"], 1.53)
        self.assertEqual(
            payload["rates"],
            [{"date": "2026-08-22", "currency": "AUD", "usdRate": 1.53}],
        )
        instance.rates_for_range.assert_called_once()
        _, kwargs = instance.rates_for_range.call_args
        self.assertEqual(kwargs["from_date"], "2026-08-01")
        self.assertEqual(kwargs["to_date"], "2026-08-22")

    def test_falls_back_gracefully_when_the_service_errors(self) -> None:
        with patch("app.routers.pricing.FxRateService") as service_cls:
            instance = service_cls.return_value
            instance.is_configured = True
            instance.current_rates.side_effect = FxRateServiceError("boom")
            instance.rates_for_range.side_effect = FxRateServiceError("boom")

            response = self.client.get("/api/pricing/fx-rates")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["current"]["USD"], 1.0)
        self.assertEqual(payload["rates"], [])


if __name__ == "__main__":
    unittest.main()
