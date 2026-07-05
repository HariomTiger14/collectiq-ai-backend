import time
import unittest

import httpx

from app.services.ai.mock_recognition_service import MockRecognitionProvider
from app.services.pricing.aggregation_service import PricingAggregationService
from app.services.pricing.base_pricing_provider import (
    PricingProviderRateLimitError,
    PricingProviderTimeoutError,
)
from app.services.pricing.ebay_pricing_provider import EbayPricingProvider
from app.services.pricing.mock_pricing_provider import MockPricingProvider


class EbayPricingProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.recognition = MockRecognitionProvider().recognize("uploads/card.png")

    def test_successful_provider_response_normalizes_comparable_sales(self) -> None:
        client = _FakeHttpClient(response=_FakeResponse(body=_ebay_payload()))
        provider = _provider(client=client)

        pricing = provider.price(self.recognition)

        self.assertEqual(client.call_count, 1)
        self.assertEqual(pricing.pricingSource, "eBay Browse API")
        self.assertEqual(pricing.pricingAge, "live")
        self.assertEqual(pricing.cacheStatus, "miss")
        self.assertEqual(len(pricing.comparableSales), 3)
        self.assertGreater(pricing.estimatedMarketValue, 0)
        self.assertGreaterEqual(pricing.highEstimate, pricing.lowEstimate)
        self.assertEqual(client.last_request["headers"]["X-EBAY-C-MARKETPLACE-ID"], "EBAY_AU")

    def test_timeout_maps_to_pricing_timeout(self) -> None:
        provider = _provider(
            client=_FakeHttpClient(exception=httpx.TimeoutException("slow")),
        )

        with self.assertRaises(PricingProviderTimeoutError):
            provider.price(self.recognition)

    def test_rate_limit_maps_to_pricing_rate_limit(self) -> None:
        provider = _provider(client=_FakeHttpClient(response=_FakeResponse(status_code=429)))

        with self.assertRaises(PricingProviderRateLimitError):
            provider.price(self.recognition)

    def test_cache_hit_prevents_repeated_provider_request(self) -> None:
        client = _FakeHttpClient(response=_FakeResponse(body=_ebay_payload()))
        provider = _provider(client=client, cache_ttl_seconds=60)

        first = provider.price(self.recognition)
        second = provider.price(self.recognition)

        self.assertEqual(client.call_count, 1)
        self.assertEqual(first.cacheStatus, "miss")
        self.assertEqual(second.cacheStatus, "hit")

    def test_cache_expiry_allows_refresh(self) -> None:
        client = _FakeHttpClient(response=_FakeResponse(body=_ebay_payload()))
        provider = _provider(
            client=client,
            cache_ttl_seconds=1,
            min_interval_ms=0,
        )

        provider.price(self.recognition)
        time.sleep(1.05)
        provider.price(self.recognition)

        self.assertEqual(client.call_count, 2)

    def test_aggregator_falls_back_to_mock_when_ebay_unavailable(self) -> None:
        provider = _provider(access_token="", client=_FakeHttpClient())

        pricing = PricingAggregationService(
            [provider],
            fallback_provider=MockPricingProvider(),
        ).price(self.recognition)

        self.assertTrue(pricing.fallbackUsed)
        self.assertEqual(pricing.cacheStatus, "fallback")
        self.assertIn("EBAY_ACCESS_TOKEN", pricing.providerDiagnostics["fallbackReason"])
        self.assertGreater(pricing.estimatedMarketValue, 0)


def _provider(
    *,
    access_token: str = "test-token",
    client=None,
    cache_ttl_seconds: int = 900,
    min_interval_ms: int = 0,
) -> EbayPricingProvider:
    return EbayPricingProvider(
        access_token=access_token,
        browse_api_url="https://api.ebay.com/buy/browse/v1/item_summary/search",
        marketplace_id="EBAY_AU",
        timeout_seconds=1,
        cache_ttl_seconds=cache_ttl_seconds,
        min_interval_ms=min_interval_ms,
        client=client,
    )


def _ebay_payload() -> dict:
    return {
        "itemSummaries": [
            {
                "title": "1999 Pokemon Charizard Holo PSA 8",
                "price": {"value": "1800.00", "currency": "AUD"},
                "condition": "Graded",
                "itemCreationDate": "2026-06-25T00:00:00Z",
                "itemWebUrl": "https://example.test/item/1",
            },
            {
                "title": "Pokemon Charizard Base Set Holo",
                "price": {"value": "1950.00", "currency": "AUD"},
                "condition": "Near Mint",
                "itemCreationDate": "2026-06-26T00:00:00Z",
                "itemWebUrl": "https://example.test/item/2",
            },
            {
                "title": "Charizard Holo Pokemon Card",
                "price": {"value": "2100.00", "currency": "AUD"},
                "condition": "Excellent",
                "itemCreationDate": "2026-06-27T00:00:00Z",
                "itemWebUrl": "https://example.test/item/3",
            },
        ]
    }


class _FakeResponse:
    def __init__(self, *, status_code: int = 200, body: dict | None = None) -> None:
        self.status_code = status_code
        self._body = body or {}

    def json(self) -> dict:
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
