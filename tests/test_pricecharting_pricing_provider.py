import unittest

import httpx

from app.services.ai.mock_recognition_service import MockRecognitionProvider
from app.services.pricing.aggregation_service import PricingAggregationService
from app.services.pricing.base_pricing_provider import (
    EmptyMarketDataError,
    PricingProviderError,
    PricingProviderRateLimitError,
    PricingProviderTimeoutError,
    PricingProviderUnavailableError,
)
from app.services.pricing.mock_pricing_provider import MockPricingProvider
from app.services.pricing.pricecharting_pricing_provider import (
    PriceChartingPricingProvider,
)


class PriceChartingPricingProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.recognition = MockRecognitionProvider().recognize("uploads/card.png")

    def test_successful_provider_response_normalizes_guide_prices(self) -> None:
        client = _FakeHttpClient(response=_FakeResponse(body=_pricecharting_payload()))
        provider = _provider(client=client)

        pricing = provider.price(self.recognition)

        self.assertEqual(client.call_count, 1)
        self.assertEqual(pricing.pricingSource, "PriceCharting API")
        self.assertEqual(pricing.pricingAge, "guide")
        self.assertEqual(pricing.cacheStatus, "miss")
        self.assertEqual(pricing.currency, "USD")
        self.assertEqual(len(pricing.comparableSales), 4)
        self.assertEqual(pricing.providerDiagnostics["provider"], "pricecharting")
        self.assertEqual(client.last_request["params"]["t"], "pc-key")
        self.assertIn("Bearer pc-key", client.last_request["headers"]["Authorization"])

    def test_cache_hit_prevents_repeated_provider_request(self) -> None:
        client = _FakeHttpClient(response=_FakeResponse(body=_pricecharting_payload()))
        provider = _provider(client=client, cache_ttl_seconds=60)

        first = provider.price(self.recognition)
        second = provider.price(self.recognition)

        self.assertEqual(client.call_count, 1)
        self.assertEqual(first.cacheStatus, "miss")
        self.assertEqual(second.cacheStatus, "hit")

    def test_missing_api_key_maps_to_unavailable(self) -> None:
        provider = _provider(api_key="", client=_FakeHttpClient())

        with self.assertRaises(PricingProviderUnavailableError):
            provider.price(self.recognition)

    def test_timeout_maps_to_pricing_timeout(self) -> None:
        provider = _provider(
            client=_FakeHttpClient(exception=httpx.TimeoutException("slow")),
        )

        with self.assertRaises(PricingProviderTimeoutError):
            provider.price(self.recognition)

    def test_unauthorized_maps_to_unavailable(self) -> None:
        provider = _provider(client=_FakeHttpClient(response=_FakeResponse(status_code=401)))

        with self.assertRaises(PricingProviderUnavailableError):
            provider.price(self.recognition)

    def test_no_result_maps_to_empty_market_data(self) -> None:
        provider = _provider(client=_FakeHttpClient(response=_FakeResponse(status_code=404)))

        with self.assertRaises(EmptyMarketDataError):
            provider.price(self.recognition)

    def test_rate_limit_maps_to_pricing_rate_limit(self) -> None:
        provider = _provider(client=_FakeHttpClient(response=_FakeResponse(status_code=429)))

        with self.assertRaises(PricingProviderRateLimitError):
            provider.price(self.recognition)

    def test_malformed_response_maps_to_pricing_error(self) -> None:
        provider = _provider(
            client=_FakeHttpClient(
                response=_FakeResponse(json_exception=ValueError("bad json")),
            ),
        )

        with self.assertRaises(PricingProviderError):
            provider.price(self.recognition)

    def test_empty_payload_maps_to_empty_market_data(self) -> None:
        provider = _provider(client=_FakeHttpClient(response=_FakeResponse(body={})))

        with self.assertRaises(EmptyMarketDataError):
            provider.price(self.recognition)

    def test_aggregation_uses_pricecharting_result(self) -> None:
        provider = _provider(
            client=_FakeHttpClient(response=_FakeResponse(body=_pricecharting_payload())),
        )

        pricing = PricingAggregationService(
            [provider],
            fallback_provider=MockPricingProvider(),
        ).price(self.recognition)

        self.assertFalse(pricing.fallbackUsed)
        self.assertEqual(pricing.sourceCount, 1)
        self.assertIn("PriceCharting API", pricing.pricingSource)
        self.assertGreater(pricing.estimatedMarketValue, 0)
        self.assertEqual(pricing.providerDiagnostics["providers"], "pricecharting")

    def test_aggregation_falls_back_to_mock_when_credentials_missing(self) -> None:
        provider = _provider(api_key="", client=_FakeHttpClient())

        pricing = PricingAggregationService(
            [provider],
            fallback_provider=MockPricingProvider(),
        ).price(self.recognition)

        self.assertTrue(pricing.fallbackUsed)
        self.assertEqual(pricing.cacheStatus, "fallback")
        self.assertIn(
            "PRICECHARTING_API_KEY",
            pricing.providerDiagnostics["fallbackReason"],
        )


def _provider(
    *,
    api_key: str = "pc-key",
    client=None,
    cache_ttl_seconds: int = 900,
    min_interval_ms: int = 0,
) -> PriceChartingPricingProvider:
    return PriceChartingPricingProvider(
        api_key=api_key,
        api_base="https://pricecharting.test",
        timeout_seconds=1,
        cache_ttl_seconds=cache_ttl_seconds,
        min_interval_ms=min_interval_ms,
        client=client,
    )


def _pricecharting_payload() -> dict:
    return {
        "products": [
            {
                "id": "pc-123",
                "product-name": "1999 Pokemon Charizard Holo",
                "console-name": "Pokemon Cards",
                "loose-price": "$1200.00",
                "cib-price": "1550",
                "new-price": 1800,
                "graded-price": 2400,
                "currency": "USD",
                "lastUpdated": "2026-06-28T00:00:00Z",
                "url": "https://example.test/charizard",
            }
        ]
    }


class _FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        body: dict | None = None,
        json_exception: Exception | None = None,
    ) -> None:
        self.status_code = status_code
        self._body = body or {}
        self._json_exception = json_exception

    def json(self) -> dict:
        if self._json_exception is not None:
            raise self._json_exception
        return self._body


class _FakeHttpClient:
    def __init__(
        self,
        *,
        response: _FakeResponse | None = None,
        exception: Exception | None = None,
    ) -> None:
        self.response = response or _FakeResponse()
        self.exception = exception
        self.call_count = 0
        self.last_request: dict | None = None

    def get(self, url: str, **kwargs) -> _FakeResponse:
        self.call_count += 1
        self.last_request = {"url": url, **kwargs}
        if self.exception is not None:
            raise self.exception
        return self.response


if __name__ == "__main__":
    unittest.main()
