import unittest

import httpx

from app.services.ai.base_recognition_service import RecognitionResult
from app.services.pricing.base_pricing_provider import (
    EmptyMarketDataError,
    PricingProviderRateLimitError,
    PricingProviderTimeoutError,
    PricingProviderUnavailableError,
)
from app.services.pricing.kicksdb_pricing_provider import KicksDBPricingProvider


class KicksDBPricingProviderTest(unittest.TestCase):
    def test_successful_stockx_response_normalizes_market_prices(self) -> None:
        client = _FakeHttpClient(response=_FakeResponse(body=_stockx_payload()))
        provider = _provider(client=client)

        pricing = provider.price(_sneaker_recognition())

        self.assertEqual(client.call_count, 1)
        self.assertEqual(pricing.pricingSource, "KicksDB StockX API")
        self.assertEqual(pricing.pricingAge, "live")
        self.assertEqual(pricing.cacheStatus, "miss")
        self.assertEqual(pricing.currency, "USD")
        self.assertEqual(pricing.lowEstimate, 220)
        self.assertEqual(pricing.highEstimate, 310)
        self.assertEqual(pricing.estimatedMarketValue, 275)
        self.assertEqual(pricing.providerDiagnostics["provider"], "kicksdb")
        self.assertEqual(pricing.providerDiagnostics["matchedProductSku"], "DN3707-160")
        self.assertEqual(client.last_request["headers"]["Authorization"], "kicks-key")
        self.assertEqual(client.last_request["params"]["query"], "Nike Jordan 4 Military Black DN3707-160 2022")
        self.assertEqual(client.last_request["params"]["sku"], "DN3707-160")
        self.assertEqual(client.last_request["params"]["market"], "US")
        self.assertEqual(client.last_request["params"]["display[prices]"], "true")
        self.assertEqual(client.last_request["params"]["display[variants]"], "true")
        self.assertEqual(client.last_request["params"]["display[statistics]"], "true")

    def test_successful_response_accepts_nested_prices_map(self) -> None:
        client = _FakeHttpClient(response=_FakeResponse(body=_stockx_prices_payload()))
        provider = _provider(client=client)

        pricing = provider.price(_sneaker_recognition())

        self.assertEqual(pricing.pricingSource, "KicksDB StockX API")
        self.assertEqual(pricing.lowEstimate, 284.5)
        self.assertEqual(pricing.highEstimate, 314.99)
        self.assertEqual(pricing.estimatedMarketValue, 300)
        self.assertEqual(pricing.comparableSales[0].soldPrice, 299.99)

    def test_successful_response_accepts_stockx_snake_case_aggregate_prices(self) -> None:
        client = _FakeHttpClient(response=_FakeResponse(body=_stockx_aggregate_payload()))
        provider = _provider(client=client)

        pricing = provider.price(_sneaker_recognition())

        self.assertEqual(pricing.lowEstimate, 280.25)
        self.assertEqual(pricing.highEstimate, 320.5)
        self.assertEqual(pricing.estimatedMarketValue, 300)

    def test_retries_stockx_search_without_sku_when_sku_filter_returns_no_products(self) -> None:
        client = _FakeHttpClient(
            responses=[
                _FakeResponse(body={"products": []}),
                _FakeResponse(body=_stockx_aggregate_payload()),
            ]
        )
        provider = _provider(client=client)

        pricing = provider.price(_sneaker_recognition())

        self.assertEqual(client.call_count, 2)
        self.assertEqual(client.requests[0]["params"]["sku"], "DN3707-160")
        self.assertNotIn("sku", client.requests[1]["params"])
        self.assertEqual(pricing.estimatedMarketValue, 300)

    def test_fetches_product_detail_when_search_has_no_prices(self) -> None:
        client = _FakeHttpClient(
            responses=[
                _FakeResponse(body=_stockx_metadata_only_payload()),
                _FakeResponse(body=_stockx_detail_payload()),
            ]
        )
        provider = _provider(client=client)

        pricing = provider.price(_sneaker_recognition())

        self.assertEqual(client.call_count, 2)
        self.assertIn("/v3/stockx/products/stockx-1", client.requests[1]["url"])
        self.assertEqual(pricing.lowEstimate, 280)
        self.assertEqual(pricing.highEstimate, 315)
        self.assertEqual(pricing.estimatedMarketValue, 298)
        self.assertEqual(pricing.providerDiagnostics["matchedProductId"], "stockx-1")

    def test_uses_unified_sku_lookup_when_stockx_payload_has_no_price(self) -> None:
        client = _FakeHttpClient(
            responses=[
                _FakeResponse(body=_stockx_metadata_only_payload()),
                _FakeResponse(body=_stockx_detail_no_price_payload()),
                _FakeResponse(body=_unified_gtin_payload()),
            ]
        )
        provider = _provider(client=client)

        pricing = provider.price(_sneaker_recognition())

        self.assertEqual(client.call_count, 3)
        self.assertIn("/v3/unified/gtin", client.requests[2]["url"])
        self.assertEqual(client.requests[2]["params"]["sku"], "DN3707-160")
        self.assertEqual(client.requests[2]["params"]["source"], "stockx")
        self.assertEqual(pricing.lowEstimate, 299.99)
        self.assertEqual(pricing.highEstimate, 299.99)
        self.assertEqual(pricing.estimatedMarketValue, 300)
        self.assertEqual(pricing.comparableSales[0].currency, "USD")
        self.assertEqual(pricing.providerDiagnostics["matchedProductId"], "stockx-unified-1")

    def test_cache_hit_prevents_repeated_provider_request(self) -> None:
        client = _FakeHttpClient(response=_FakeResponse(body=_stockx_payload()))
        provider = _provider(client=client, cache_ttl_seconds=60)

        first = provider.price(_sneaker_recognition())
        second = provider.price(_sneaker_recognition())

        self.assertEqual(client.call_count, 1)
        self.assertEqual(first.cacheStatus, "miss")
        self.assertEqual(second.cacheStatus, "hit")

    def test_missing_key_maps_to_unavailable(self) -> None:
        provider = _provider(api_key="", client=_FakeHttpClient())

        with self.assertRaises(PricingProviderUnavailableError):
            provider.price(_sneaker_recognition())

    def test_timeout_maps_to_pricing_timeout(self) -> None:
        provider = _provider(
            client=_FakeHttpClient(exception=httpx.TimeoutException("slow")),
        )

        with self.assertRaises(PricingProviderTimeoutError):
            provider.price(_sneaker_recognition())

    def test_rate_limit_maps_to_pricing_rate_limit(self) -> None:
        provider = _provider(client=_FakeHttpClient(response=_FakeResponse(status_code=429)))

        with self.assertRaises(PricingProviderRateLimitError):
            provider.price(_sneaker_recognition())

    def test_no_results_maps_to_empty_market_data(self) -> None:
        provider = _provider(client=_FakeHttpClient(response=_FakeResponse(body={"products": []})))

        with self.assertRaises(EmptyMarketDataError):
            provider.price(_sneaker_recognition())

    def test_weak_product_match_is_rejected(self) -> None:
        provider = _provider(client=_FakeHttpClient(response=_FakeResponse(body=_weak_payload())))

        with self.assertRaises(EmptyMarketDataError):
            provider.price(_sneaker_recognition())

    def test_catalog_match_skips_the_live_api_entirely(self) -> None:
        client = _FakeHttpClient(response=_FakeResponse(body=_stockx_payload()))
        matcher = _FakeCatalogMatcher([_catalog_row()])
        provider = _provider(client=client, catalog_matcher=matcher)

        pricing = provider.price(_sneaker_recognition())

        self.assertEqual(client.call_count, 0)
        self.assertEqual(matcher.call_count, 1)
        self.assertEqual(pricing.pricingSource, "KicksDB Catalog (StockX)")
        self.assertEqual(pricing.pricingAge, "cached")
        self.assertEqual(pricing.lowEstimate, 220)
        self.assertEqual(pricing.highEstimate, 310)
        self.assertEqual(pricing.estimatedMarketValue, 268)
        self.assertEqual(pricing.providerDiagnostics["matchedProductSku"], "DN3707-160")

    def test_catalog_result_is_cached_like_a_live_result(self) -> None:
        client = _FakeHttpClient(response=_FakeResponse(body=_stockx_payload()))
        matcher = _FakeCatalogMatcher([_catalog_row()])
        provider = _provider(client=client, catalog_matcher=matcher, cache_ttl_seconds=60)

        first = provider.price(_sneaker_recognition())
        second = provider.price(_sneaker_recognition())

        self.assertEqual(client.call_count, 0)
        self.assertEqual(first.cacheStatus, "miss")
        self.assertEqual(second.cacheStatus, "hit")

    def test_no_catalog_candidates_falls_back_to_live_search(self) -> None:
        client = _FakeHttpClient(response=_FakeResponse(body=_stockx_payload()))
        matcher = _FakeCatalogMatcher([])
        provider = _provider(client=client, catalog_matcher=matcher)

        pricing = provider.price(_sneaker_recognition())

        self.assertEqual(client.call_count, 1)
        self.assertEqual(pricing.pricingSource, "KicksDB StockX API")

    def test_weak_catalog_match_falls_back_to_live_search(self) -> None:
        client = _FakeHttpClient(response=_FakeResponse(body=_stockx_payload()))
        unrelated_row = {**_catalog_row(), "title": "Adidas Samba OG", "brand": "Adidas", "sku": "B75807"}
        matcher = _FakeCatalogMatcher([unrelated_row])
        provider = _provider(client=client, catalog_matcher=matcher)

        pricing = provider.price(_sneaker_recognition())

        self.assertEqual(client.call_count, 1)
        self.assertEqual(pricing.pricingSource, "KicksDB StockX API")

    def test_no_catalog_matcher_configured_behaves_like_before(self) -> None:
        client = _FakeHttpClient(response=_FakeResponse(body=_stockx_payload()))
        provider = _provider(client=client, catalog_matcher=None)

        pricing = provider.price(_sneaker_recognition())

        self.assertEqual(client.call_count, 1)
        self.assertEqual(pricing.pricingSource, "KicksDB StockX API")


def _provider(
    *,
    api_key: str = "kicks-key",
    client=None,
    cache_ttl_seconds: int = 0,
    catalog_matcher=None,
) -> KicksDBPricingProvider:
    return KicksDBPricingProvider(
        api_key=api_key,
        api_base="https://api.kicks.dev",
        timeout_seconds=10,
        cache_ttl_seconds=cache_ttl_seconds,
        min_interval_ms=0,
        client=client,
        catalog_matcher=catalog_matcher,
    )


def _sneaker_recognition() -> RecognitionResult:
    return RecognitionResult(
        title="Jordan 4 Military Black",
        category="Sneakers",
        confidence=82,
        estimatedValue=0,
        condition="New",
        recommendation="Use sneaker market pricing.",
        description="Nike Air Jordan 4 Retro Military Black sneakers.",
        detectedObjects=["sneaker"],
        aiProvider="test",
        processingTimeMs=10,
        primaryMatch="Nike Jordan 4 Military Black",
        alternativeMatches=[],
        confidenceExplanation="Matched sneaker shape and branding.",
        detectionQuality="good",
        aiReasoning="Sneaker detected.",
        brand="Nike",
        cardNumber="DN3707-160",
        year="2022",
    )


def _stockx_payload() -> dict:
    return {
        "products": [
            {
                "id": "stockx-1",
                "title": "Nike Air Jordan 4 Retro Military Black",
                "brand": "Nike",
                "styleId": "DN3707-160",
                "category": "sneakers",
                "currency": "USD",
                "market": {
                    "lastSale": 295,
                    "lowestAsk": 310,
                    "highestBid": 220,
                },
                "url": "https://stockx.com/air-jordan-4-retro-military-black",
            }
        ]
    }


def _stockx_prices_payload() -> dict:
    return {
        "data": {
            "products": [
                {
                    "id": "stockx-1",
                    "title": "Nike Air Jordan 4 Retro Military Black",
                    "brand": "Nike",
                    "styleId": "DN3707-160",
                    "category": "sneakers",
                    "currency": "USD",
                    "prices": {
                        "last_sale": {"amount": 299.99, "currency": "USD"},
                        "lowest_ask": {"amount": 314.99, "currency": "USD"},
                        "highest_bid": {"amount": 284.5, "currency": "USD"},
                        "retail_price": {"amount": 210, "currency": "USD"},
                    },
                }
            ]
        }
    }


def _stockx_aggregate_payload() -> dict:
    return {
        "products": [
            {
                "id": "stockx-1",
                "title": "Nike Air Jordan 4 Retro Military Black",
                "brand": "Nike",
                "styleId": "DN3707-160",
                "category": "sneakers",
                "currency": "USD",
                "min_price": 280.25,
                "max_price": 320.5,
                "avg_price": 300,
                "retail_price": 210,
            }
        ]
    }


def _stockx_metadata_only_payload() -> dict:
    return {
        "products": [
            {
                "id": "stockx-1",
                "title": "Nike Air Jordan 4 Retro Military Black",
                "brand": "Nike",
                "styleId": "DN3707-160",
                "category": "sneakers",
            }
        ]
    }


def _stockx_detail_payload() -> dict:
    return {
        "data": {
            "id": "stockx-1",
            "title": "Nike Air Jordan 4 Retro Military Black",
            "brand": "Nike",
            "styleId": "DN3707-160",
            "currency": "USD",
            "variants": [
                {
                    "size": "10",
                    "prices": {
                        "last_sale": 298,
                        "lowest_ask": 315,
                        "highest_bid": 280,
                    },
                }
            ],
        }
    }


def _stockx_detail_no_price_payload() -> dict:
    return {
        "data": {
            "id": "stockx-1",
            "title": "Nike Air Jordan 4 Retro Military Black",
            "brand": "Nike",
            "styleId": "DN3707-160",
            "currency": "USD",
        }
    }


def _unified_gtin_payload() -> dict:
    return {
        "data": [
            {
                "id": "stockx-unified-1",
                "name": "Nike Air Jordan 4 Retro Military Black",
                "brand": "Nike",
                "sku": "DN3707-160",
                "source": "stockx",
                "price": 299.99,
                "retail_price": 210,
                "currency": "USD",
                "link": "https://stockx.com/air-jordan-4-retro-military-black",
                "updated_at": "2026-07-29T00:00:00Z",
            }
        ]
    }


def _weak_payload() -> dict:
    return {
        "products": [
            {
                "id": "stockx-2",
                "title": "Adidas Samba OG",
                "brand": "Adidas",
                "styleId": "B75807",
                "category": "sneakers",
                "market": {"lastSale": 120},
            }
        ]
    }


def _catalog_row() -> dict:
    return {
        "kicksdb_id": "abc-123",
        "sku": "DN3707-160",
        "slug": "air-jordan-4-military-black",
        "title": "Nike Air Jordan 4 Retro Military Black",
        "brand": "Nike",
        "model": "Air Jordan 4",
        "product_url": "https://stockx.com/air-jordan-4-retro-military-black",
        "currency": "USD",
        "min_price_cents": 22000,
        "max_price_cents": 31000,
        "avg_price_cents": 27500,
        "variants": [],
    }


class _FakeCatalogMatcher:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.call_count = 0

    def candidates(self) -> list[dict]:
        self.call_count += 1
        return self._rows


class _FakeHttpClient:
    def __init__(self, response=None, exception=None, responses=None) -> None:
        self.response = response or _FakeResponse()
        self.responses = list(responses or [])
        self.exception = exception
        self.call_count = 0
        self.last_request = {}
        self.requests = []

    def get(self, url, **kwargs):
        self.call_count += 1
        self.last_request = {"url": url, **kwargs}
        self.requests.append(self.last_request)
        if self.exception is not None:
            raise self.exception
        if self.responses:
            return self.responses.pop(0)
        return self.response


class _FakeResponse:
    def __init__(self, *, status_code: int = 200, body=None) -> None:
        self.status_code = status_code
        self.body = body if body is not None else {}

    def json(self):
        return self.body


if __name__ == "__main__":
    unittest.main()
