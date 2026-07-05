import unittest

from app.services.ai.mock_recognition_service import MockRecognitionProvider
from app.services.pricing.aggregation_service import PricingAggregationService
from app.services.pricing.base_pricing_provider import PricingProviderUnavailableError
from app.services.pricing.mock_pricing_provider import MockPricingProvider
from app.services.pricing.provider_factory import get_pricing_provider


class MockPricingProviderTest(unittest.TestCase):
    def test_mock_pricing_returns_market_range(self) -> None:
        recognition = MockRecognitionProvider().recognize("uploads/card.png")

        pricing = MockPricingProvider().price(recognition)

        self.assertEqual(pricing.estimatedMarketValue, recognition.estimatedValue)
        self.assertLess(pricing.lowEstimate, pricing.estimatedMarketValue)
        self.assertGreater(pricing.highEstimate, pricing.estimatedMarketValue)
        self.assertEqual(pricing.currency, "AUD")
        self.assertTrue(pricing.pricingSource)
        self.assertGreaterEqual(pricing.pricingConfidence, 0)
        self.assertLessEqual(pricing.pricingConfidence, 100)
        self.assertTrue(pricing.lastUpdated.endswith("Z"))
        self.assertTrue(pricing.comparableSales)
        self.assertEqual(pricing.cacheStatus, "mock")

    def test_pricing_provider_factory_defaults_to_mock(self) -> None:
        provider = get_pricing_provider()

        self.assertIsInstance(provider, PricingAggregationService)

    def test_pricing_provider_factory_supports_future_provider_names(self) -> None:
        recognition = MockRecognitionProvider().recognize("uploads/card.png")

        pricing = get_pricing_provider("ebay").price(recognition)

        self.assertGreater(pricing.estimatedMarketValue, 0)
        self.assertTrue(pricing.fallbackUsed)
        self.assertEqual(pricing.cacheStatus, "fallback")

    def test_pricing_provider_factory_rejects_unknown_provider(self) -> None:
        with self.assertRaises(PricingProviderUnavailableError):
            get_pricing_provider("unknown")


if __name__ == "__main__":
    unittest.main()
