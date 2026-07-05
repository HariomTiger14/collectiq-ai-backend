import time
import unittest

import httpx

from app.services.ai.mock_recognition_service import MockRecognitionProvider
from app.services.pricing.aggregation_service import PricingAggregationService
from app.services.pricing.base_pricing_provider import (
    EmptyMarketDataError,
    PricingProviderRateLimitError,
    PricingProviderTimeoutError,
)
from app.services.pricing.mock_pricing_provider import MockPricingProvider
from app.services.pricing.tcgplayer_pricing_provider import TCGPlayerPricingProvider


class TCGPlayerPricingProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.recognition = MockRecognitionProvider().recognize("uploads/card.png")

    def test_oauth_search_and_pricing_normalize_to_pricing_result(self) -> None:
        client = _FakeTCGClient(
            post_responses=[_FakeResponse(body=_token_payload())],
            get_responses=[
                _FakeResponse(body=_search_payload()),
                _FakeResponse(body=_pricing_payload()),
            ],
        )
        provider = _provider(client=client)

        pricing = provider.price(self.recognition)

        self.assertEqual(client.post_count, 1)
        self.assertEqual(client.get_count, 2)
        self.assertEqual(pricing.pricingSource, "TCGplayer API")
        self.assertEqual(pricing.pricingAge, "live")
        self.assertEqual(pricing.cacheStatus, "miss")
        self.assertEqual(pricing.currency, "USD")
        self.assertEqual(len(pricing.comparableSales), 3)
        self.assertEqual(pricing.providerDiagnostics["provider"], "tcgplayer")
        self.assertEqual(
            client.post_requests[0]["data"]["grant_type"],
            "client_credentials",
        )
        self.assertIn("Bearer tcg-token", client.get_requests[0]["headers"]["Authorization"])
        self.assertIn("productName", client.get_requests[0]["params"])

    def test_cache_hit_prevents_repeated_oauth_and_provider_calls(self) -> None:
        client = _FakeTCGClient(
            post_responses=[_FakeResponse(body=_token_payload())],
            get_responses=[
                _FakeResponse(body=_search_payload()),
                _FakeResponse(body=_pricing_payload()),
            ],
        )
        provider = _provider(client=client, cache_ttl_seconds=60)

        first = provider.price(self.recognition)
        second = provider.price(self.recognition)

        self.assertEqual(client.post_count, 1)
        self.assertEqual(client.get_count, 2)
        self.assertEqual(first.cacheStatus, "miss")
        self.assertEqual(second.cacheStatus, "hit")

    def test_cache_expiry_allows_refresh(self) -> None:
        client = _FakeTCGClient(
            post_responses=[_FakeResponse(body=_token_payload())],
            get_responses=[
                _FakeResponse(body=_search_payload()),
                _FakeResponse(body=_pricing_payload()),
                _FakeResponse(body=_search_payload()),
                _FakeResponse(body=_pricing_payload()),
            ],
        )
        provider = _provider(client=client, cache_ttl_seconds=1, min_interval_ms=0)

        provider.price(self.recognition)
        time.sleep(1.05)
        provider.price(self.recognition)

        self.assertEqual(client.post_count, 1)
        self.assertEqual(client.get_count, 4)

    def test_timeout_maps_to_pricing_timeout(self) -> None:
        provider = _provider(
            client=_FakeTCGClient(post_exception=httpx.TimeoutException("slow")),
        )

        with self.assertRaises(PricingProviderTimeoutError):
            provider.price(self.recognition)

    def test_unauthorized_search_refreshes_oauth_token(self) -> None:
        client = _FakeTCGClient(
            post_responses=[
                _FakeResponse(body=_token_payload("old-token")),
                _FakeResponse(body=_token_payload("new-token")),
            ],
            get_responses=[
                _FakeResponse(status_code=401),
                _FakeResponse(body=_search_payload()),
                _FakeResponse(body=_pricing_payload()),
            ],
        )
        provider = _provider(client=client)

        pricing = provider.price(self.recognition)

        self.assertEqual(pricing.pricingSource, "TCGplayer API")
        self.assertEqual(client.post_count, 2)
        self.assertIn(
            "Bearer new-token",
            client.get_requests[1]["headers"]["Authorization"],
        )

    def test_404_maps_to_empty_market_data(self) -> None:
        provider = _provider(
            client=_FakeTCGClient(
                post_responses=[_FakeResponse(body=_token_payload())],
                get_responses=[_FakeResponse(status_code=404)],
            ),
        )

        with self.assertRaises(EmptyMarketDataError):
            provider.price(self.recognition)

    def test_rate_limit_maps_to_pricing_rate_limit(self) -> None:
        provider = _provider(
            client=_FakeTCGClient(
                post_responses=[_FakeResponse(body=_token_payload())],
                get_responses=[_FakeResponse(status_code=429)],
            ),
        )

        with self.assertRaises(PricingProviderRateLimitError):
            provider.price(self.recognition)

    def test_aggregation_combines_tcgplayer_and_falls_back_safely(self) -> None:
        provider = _provider(
            client=_FakeTCGClient(
                post_responses=[_FakeResponse(body=_token_payload())],
                get_responses=[
                    _FakeResponse(body=_search_payload()),
                    _FakeResponse(body=_pricing_payload()),
                ],
            ),
        )

        pricing = PricingAggregationService(
            [provider],
            fallback_provider=MockPricingProvider(),
        ).price(self.recognition)

        self.assertFalse(pricing.fallbackUsed)
        self.assertEqual(pricing.sourceCount, 1)
        self.assertIn("TCGplayer API", pricing.pricingSource)
        self.assertGreater(pricing.estimatedMarketValue, 0)

    def test_aggregation_falls_back_to_mock_when_credentials_missing(self) -> None:
        provider = _provider(client_id="", client_secret="", client=_FakeTCGClient())

        pricing = PricingAggregationService(
            [provider],
            fallback_provider=MockPricingProvider(),
        ).price(self.recognition)

        self.assertTrue(pricing.fallbackUsed)
        self.assertEqual(pricing.cacheStatus, "fallback")
        self.assertIn("TCGPLAYER_CLIENT_ID", pricing.providerDiagnostics["fallbackReason"])


def _provider(
    *,
    client_id: str = "client-id",
    client_secret: str = "client-secret",
    client=None,
    cache_ttl_seconds: int = 900,
    min_interval_ms: int = 0,
) -> TCGPlayerPricingProvider:
    return TCGPlayerPricingProvider(
        client_id=client_id,
        client_secret=client_secret,
        api_base="https://api.tcgplayer.test",
        timeout_seconds=1,
        cache_ttl_seconds=cache_ttl_seconds,
        min_interval_ms=min_interval_ms,
        client=client,
    )


def _token_payload(token: str = "tcg-token") -> dict:
    return {"access_token": token, "expires_in": 3600, "token_type": "bearer"}


def _search_payload() -> dict:
    return {
        "data": [
            {
                "productId": 12345,
                "name": "1999 Pokemon Charizard Holo",
                "cleanName": "1999 Pokemon Charizard Holo",
                "groupName": "Base Set",
                "extendedData": [
                    {"name": "Number", "value": "4/102"},
                    {"name": "Rarity", "value": "Holo Rare"},
                ],
            }
        ]
    }


def _pricing_payload() -> dict:
    return {
        "data": [
            {
                "productId": 12345,
                "subTypeName": "Near Mint",
                "marketPrice": 1550.00,
                "lowPrice": 1300.00,
                "midPrice": 1500.00,
                "currency": "USD",
                "lastUpdated": "2026-06-28T00:00:00Z",
            },
            {
                "productId": 12345,
                "subTypeName": "Lightly Played",
                "marketPrice": 1250.00,
                "currency": "USD",
                "lastUpdated": "2026-06-27T00:00:00Z",
            },
            {
                "productId": 12345,
                "subTypeName": "Near Mint Foil",
                "midPrice": 1700.00,
                "currency": "USD",
                "lastUpdated": "2026-06-26T00:00:00Z",
            },
        ]
    }


class _FakeResponse:
    def __init__(self, *, status_code: int = 200, body: dict | None = None) -> None:
        self.status_code = status_code
        self._body = body or {}

    def json(self) -> dict:
        return self._body


class _FakeTCGClient:
    def __init__(
        self,
        *,
        post_responses: list[_FakeResponse] | None = None,
        get_responses: list[_FakeResponse] | None = None,
        post_exception: Exception | None = None,
        get_exception: Exception | None = None,
    ) -> None:
        self.post_responses = post_responses or [_FakeResponse()]
        self.get_responses = get_responses or [_FakeResponse()]
        self.post_exception = post_exception
        self.get_exception = get_exception
        self.post_count = 0
        self.get_count = 0
        self.post_requests: list[dict] = []
        self.get_requests: list[dict] = []

    def post(self, url: str, **kwargs) -> _FakeResponse:
        self.post_count += 1
        self.post_requests.append({"url": url, **kwargs})
        if self.post_exception is not None:
            raise self.post_exception
        return self.post_responses.pop(0)

    def get(self, url: str, **kwargs) -> _FakeResponse:
        self.get_count += 1
        self.get_requests.append({"url": url, **kwargs})
        if self.get_exception is not None:
            raise self.get_exception
        return self.get_responses.pop(0)


if __name__ == "__main__":
    unittest.main()
