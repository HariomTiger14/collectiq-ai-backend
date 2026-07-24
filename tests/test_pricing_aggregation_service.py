import unittest

from app.services.ai.mock_recognition_service import MockRecognitionProvider
from app.services.pricing.aggregation_service import PricingAggregationService
from app.services.pricing.base_pricing_provider import (
    EmptyMarketDataError,
    MarketComparableSale,
    PricingProviderTimeoutError,
    PricingProviderUnavailableError,
    PricingResult,
    utc_timestamp,
)
from app.services.pricing.mock_pricing_provider import MockPricingProvider
from app.services.pricing.provider_factory import get_pricing_provider


class PricingAggregationServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.recognition = MockRecognitionProvider().recognize("uploads/card.png")

    def test_aggregator_normalizes_market_data(self) -> None:
        provider = _StaticPricingProvider(
            "test-market",
            [
                _sale(100),
                _sale(120),
                _sale(140),
            ],
            confidence=80,
        )

        pricing = PricingAggregationService([provider]).price(self.recognition)

        self.assertEqual(pricing.estimatedMarketValue, 120)
        self.assertEqual(pricing.lowEstimate, 100)
        self.assertEqual(pricing.highEstimate, 140)
        self.assertEqual(pricing.sourceCount, 1)
        self.assertFalse(pricing.fallbackUsed)
        self.assertGreaterEqual(pricing.pricingConfidence, 65)
        self.assertEqual(pricing.providerDiagnostics["medianPrice"], "120")
        self.assertEqual(pricing.providerDiagnostics["outliersRemoved"], "0")
        self.assertEqual(pricing.providerDiagnostics["comparableCount"], "3")
        self.assertIn("agreement at", pricing.providerDiagnostics["priceExplanation"])

    def test_aggregator_removes_obvious_outliers(self) -> None:
        provider = _StaticPricingProvider(
            "test-market",
            [
                _sale(100),
                _sale(110),
                _sale(120),
                _sale(10000),
            ],
            confidence=82,
        )

        pricing = PricingAggregationService([provider]).price(self.recognition)

        self.assertLess(pricing.highEstimate, 10000)
        self.assertEqual(pricing.estimatedMarketValue, 110)
        self.assertEqual(pricing.providerDiagnostics["outliersRemoved"], "1")

    def test_aggregator_uses_mock_fallback_on_provider_error(self) -> None:
        provider = _FailingPricingProvider(
            "ebay",
            PricingProviderUnavailableError("eBay credentials are not configured."),
        )

        pricing = PricingAggregationService(
            [provider],
            fallback_provider=MockPricingProvider(),
        ).price(self.recognition)

        self.assertTrue(pricing.fallbackUsed)
        self.assertEqual(pricing.cacheStatus, "fallback")
        self.assertIn("ebay", pricing.providerDiagnostics["errors"])
        self.assertGreater(pricing.estimatedMarketValue, 0)

    def test_aggregator_requires_explicit_fallback_on_timeout(self) -> None:
        provider = _FailingPricingProvider(
            "tcgplayer",
            PricingProviderTimeoutError("TCGplayer request timed out."),
        )

        with self.assertRaises(EmptyMarketDataError):
            PricingAggregationService([provider]).price(self.recognition)

    def test_future_provider_factory_reports_missing_provider_configuration(self) -> None:
        with self.assertRaises(PricingProviderUnavailableError):
            get_pricing_provider("pricecharting").price(self.recognition)



class _StaticPricingProvider:
    def __init__(
        self,
        provider_name: str,
        sales: list[MarketComparableSale],
        *,
        confidence: int,
    ) -> None:
        self.provider_name = provider_name
        self._sales = sales
        self._confidence = confidence

    def price(self, recognition) -> PricingResult:
        prices = [sale.soldPrice for sale in self._sales]
        return PricingResult(
            estimatedMarketValue=round(sum(prices) / len(prices)),
            lowEstimate=min(prices),
            highEstimate=max(prices),
            currency="AUD",
            pricingSource=self.provider_name,
            pricingConfidence=self._confidence,
            lastUpdated=utc_timestamp(),
            marketTrend="Stable",
            sourceCount=1,
            pricingAge="fresh",
            comparableSales=self._sales,
            cacheStatus="miss",
        )


class _FailingPricingProvider:
    def __init__(self, provider_name: str, exception: Exception) -> None:
        self.provider_name = provider_name
        self._exception = exception

    def price(self, recognition) -> PricingResult:
        raise self._exception


def _sale(price: int) -> MarketComparableSale:
    return MarketComparableSale(
        source="Test comps",
        title=f"Comparable sale {price}",
        soldPrice=price,
        currency="AUD",
        soldDate="2026-06-30T00:00:00Z",
        condition="Near Mint",
        url=None,
    )


if __name__ == "__main__":
    unittest.main()
