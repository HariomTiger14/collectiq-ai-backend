import time
import unittest

import httpx

from app.services.ai.mock_recognition_service import MockRecognitionProvider
from app.services.pricing.aggregation_service import PricingAggregationService
from app.services.pricing.base_pricing_provider import (
    PricingProviderRateLimitError,
    PricingProviderTimeoutError,
    PricingProviderUnavailableError,
)
from app.services.pricing.ebay_pricing_provider import EbayPricingProvider
from app.services.pricing.mock_pricing_provider import MockPricingProvider


_SOLD_COMPS_URL = (
    "https://api.ebay.com/buy/marketplace_insights/v1_beta/item_sales/search"
)


class EbayPricingProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.recognition = MockRecognitionProvider().recognize("uploads/card.png")

    def test_browse_provider_is_unavailable_without_sold_comps_access(self) -> None:
        client = _FakeHttpClient(response=_FakeResponse(body=_ebay_payload()))
        provider = _provider(client=client)

        with self.assertRaisesRegex(
            PricingProviderUnavailableError,
            "partner access not granted",
        ):
            provider.price(self.recognition)

        self.assertEqual(client.call_count, 0)

    def test_marketplace_insights_response_is_sold_comps(self) -> None:
        client = _FakeHttpClient(response=_FakeResponse(body=_ebay_sold_payload()))
        provider = _provider(
            client=client,
            marketplace_insights_api_url=_SOLD_COMPS_URL,
            partner_access_granted=True,
        )

        pricing = provider.price(self.recognition)

        self.assertEqual(client.call_count, 1)
        self.assertIn("marketplace_insights", client.last_request["url"])
        self.assertEqual(pricing.pricingSource, "eBay sold comps")
        self.assertEqual(pricing.pricingAge, "sold_comps")
        self.assertEqual(pricing.providerDiagnostics["marketDataType"], "sold_comps")
        self.assertEqual(pricing.comparableSales[0].source, "eBay sold comps")

    def test_timeout_maps_to_pricing_timeout(self) -> None:
        provider = _provider(
            client=_FakeHttpClient(exception=httpx.TimeoutException("slow")),
            marketplace_insights_api_url=_SOLD_COMPS_URL,
            partner_access_granted=True,
        )

        with self.assertRaises(PricingProviderTimeoutError):
            provider.price(self.recognition)

    def test_rate_limit_maps_to_pricing_rate_limit(self) -> None:
        provider = _provider(
            client=_FakeHttpClient(response=_FakeResponse(status_code=429)),
            marketplace_insights_api_url=_SOLD_COMPS_URL,
            partner_access_granted=True,
        )

        with self.assertRaises(PricingProviderRateLimitError):
            provider.price(self.recognition)

    def test_cache_hit_prevents_repeated_provider_request(self) -> None:
        client = _FakeHttpClient(response=_FakeResponse(body=_ebay_payload()))
        provider = _provider(
            client=client,
            cache_ttl_seconds=60,
            marketplace_insights_api_url=_SOLD_COMPS_URL,
            partner_access_granted=True,
        )

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
            marketplace_insights_api_url=_SOLD_COMPS_URL,
            partner_access_granted=True,
        )

        provider.price(self.recognition)
        time.sleep(1.05)
        provider.price(self.recognition)

        self.assertEqual(client.call_count, 2)

    def test_client_credentials_fetch_and_cache_oauth_token(self) -> None:
        client = _FakeHttpClient(
            response=_FakeResponse(body=_ebay_payload()),
            post_response=_FakeResponse(
                body={"access_token": "oauth-token", "expires_in": 7200},
            ),
        )
        provider = _provider(
            access_token="",
            client_id="client-id",
            client_secret="client-secret",
            client=client,
            marketplace_insights_api_url=_SOLD_COMPS_URL,
            partner_access_granted=True,
        )

        first = provider.price(self.recognition)
        second = provider.price(self.recognition)

        self.assertEqual(first.cacheStatus, "miss")
        self.assertEqual(second.cacheStatus, "hit")
        self.assertEqual(client.post_count, 1)
        self.assertEqual(client.call_count, 1)
        self.assertEqual(
            client.last_post["data"]["scope"],
            "https://api.ebay.com/oauth/api_scope",
        )
        self.assertEqual(
            client.last_request["headers"]["Authorization"],
            "Bearer oauth-token",
        )

    def test_aggregator_falls_back_to_mock_when_ebay_unavailable(self) -> None:
        provider = _provider(access_token="", client=_FakeHttpClient())

        pricing = PricingAggregationService(
            [provider],
            fallback_provider=MockPricingProvider(),
        ).price(self.recognition)

        self.assertTrue(pricing.fallbackUsed)
        self.assertEqual(pricing.cacheStatus, "fallback")
        self.assertIn("EBAY_CLIENT_ID", pricing.providerDiagnostics["fallbackReason"])
        self.assertGreater(pricing.estimatedMarketValue, 0)


def _provider(
    *,
    access_token: str = "test-token",
    client_id: str = "",
    client_secret: str = "",
    marketplace_insights_api_url: str = "",
    partner_access_granted: bool = False,
    client=None,
    cache_ttl_seconds: int = 900,
    min_interval_ms: int = 0,
) -> EbayPricingProvider:
    return EbayPricingProvider(
        access_token=access_token,
        client_id=client_id,
        client_secret=client_secret,
        oauth_token_url="https://api.ebay.com/identity/v1/oauth2/token",
        oauth_scope="https://api.ebay.com/oauth/api_scope",
        marketplace_insights_api_url=marketplace_insights_api_url,
        partner_access_granted=partner_access_granted,
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


def _ebay_sold_payload() -> dict:
    return {
        "itemSales": [
            {
                "title": "1999 Pokemon Charizard Holo PSA 8 sold",
                "itemSoldPrice": {"value": "1750.00", "currency": "AUD"},
                "condition": "Graded",
                "itemSoldDate": "2026-06-25T00:00:00Z",
                "itemWebUrl": "https://example.test/sold/1",
            },
            {
                "title": "Pokemon Charizard Base Set Holo sold",
                "itemSoldPrice": {"value": "1900.00", "currency": "AUD"},
                "condition": "Near Mint",
                "itemSoldDate": "2026-06-26T00:00:00Z",
                "itemWebUrl": "https://example.test/sold/2",
            },
            {
                "title": "Charizard Holo Pokemon Card sold",
                "itemSoldPrice": {"value": "2050.00", "currency": "AUD"},
                "condition": "Excellent",
                "itemSoldDate": "2026-06-27T00:00:00Z",
                "itemWebUrl": "https://example.test/sold/3",
            },
        ],
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
        post_response: _FakeResponse | None = None,
        exception: Exception | None = None,
    ) -> None:
        self.response = response or _FakeResponse()
        self.post_response = post_response or _FakeResponse()
        self.exception = exception
        self.call_count = 0
        self.post_count = 0
        self.last_request: dict | None = None
        self.last_post: dict | None = None

    def get(self, url: str, **kwargs) -> _FakeResponse:
        self.call_count += 1
        self.last_request = {"url": url, **kwargs}
        if self.exception is not None:
            raise self.exception
        return self.response

    def post(self, url: str, **kwargs) -> _FakeResponse:
        self.post_count += 1
        self.last_post = {"url": url, **kwargs}
        if self.exception is not None:
            raise self.exception
        return self.post_response


if __name__ == "__main__":
    unittest.main()
