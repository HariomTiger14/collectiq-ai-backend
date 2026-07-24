import unittest
from unittest.mock import patch

from app.services.pricing.base_pricing_provider import PricingResult
from app.services.pricing.currency_conversion import convert_pricing_result


class CurrencyConversionTest(unittest.TestCase):
    def test_converts_usd_pricing_to_display_currency_and_keeps_original(self) -> None:
        pricing = PricingResult(
            estimatedMarketValue=100,
            lowEstimate=80,
            highEstimate=120,
            currency="USD",
            pricingSource="PriceCharting API",
            pricingConfidence=80,
            lastUpdated="2026-07-25T00:00:00Z",
            valuationSource="PriceCharting API",
        )

        with patch("app.services.pricing.currency_conversion.settings") as settings:
            settings.default_display_currency = "AUD"
            settings.fx_usd_to_aud = 1.5
            settings.fx_usd_to_cad = 1.35
            settings.fx_usd_to_gbp = 0.8

            converted = convert_pricing_result(pricing, target_currency="AUD")

        self.assertEqual(converted.estimatedMarketValue, 150)
        self.assertEqual(converted.lowEstimate, 120)
        self.assertEqual(converted.highEstimate, 180)
        self.assertEqual(converted.currency, "AUD")
        self.assertEqual(converted.originalMarketValue, 100)
        self.assertEqual(converted.originalCurrency, "USD")
        self.assertEqual(converted.exchangeRateUsed, 1.5)

    def test_leaves_usd_display_as_one_to_one(self) -> None:
        pricing = PricingResult(
            estimatedMarketValue=100,
            lowEstimate=80,
            highEstimate=120,
            currency="USD",
            pricingSource="PriceCharting API",
            pricingConfidence=80,
            lastUpdated="2026-07-25T00:00:00Z",
            valuationSource="PriceCharting API",
        )

        converted = convert_pricing_result(pricing, target_currency="USD")

        self.assertEqual(converted.estimatedMarketValue, 100)
        self.assertEqual(converted.currency, "USD")
        self.assertEqual(converted.originalMarketValue, 100)
        self.assertEqual(converted.originalCurrency, "USD")
        self.assertEqual(converted.exchangeRateUsed, 1)


if __name__ == "__main__":
    unittest.main()
