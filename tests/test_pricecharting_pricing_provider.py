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
        self.assertEqual(pricing.comparableSales[0].soldPrice, 1200)
        self.assertEqual(pricing.comparableSales[1].soldPrice, 16)
        self.assertEqual(pricing.comparableSales[2].soldPrice, 18)
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

    def test_shared_throttle_allows_provider_request_after_cache_miss(self) -> None:
        client = _FakeHttpClient(response=_FakeResponse(body=_pricecharting_payload()))
        throttle = _FakeThrottle()
        provider = _provider(client=client, throttle=throttle)

        pricing = provider.price(self.recognition)

        self.assertEqual(client.call_count, 1)
        self.assertEqual(throttle.acquired_for, ["pricecharting"])
        self.assertEqual(pricing.cacheStatus, "miss")

    def test_shared_throttle_blocks_provider_request_before_upstream_call(self) -> None:
        client = _FakeHttpClient(response=_FakeResponse(body=_pricecharting_payload()))
        provider = _provider(
            client=client,
            throttle=_FakeThrottle(exception=PricingProviderRateLimitError("busy")),
        )

        with self.assertRaises(PricingProviderRateLimitError):
            provider.price(self.recognition)

        self.assertEqual(client.call_count, 0)

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

    def test_lego_direct_api_match_is_allowed_with_category_identity(self) -> None:
        recognition = _recognition(
            title="LEGO 75192 Millennium Falcon",
            category="LEGO",
            condition="New",
        )
        provider = _provider(
            client=_FakeHttpClient(response=_FakeResponse(body=_lego_payload())),
        )

        pricing = provider.price(recognition)

        self.assertEqual(pricing.pricingSource, "PriceCharting API")
        self.assertEqual(pricing.providerDiagnostics["matchedProductCategory"], "LEGO")
        self.assertGreater(pricing.estimatedMarketValue, 0)

    def test_funko_direct_api_match_is_allowed_with_category_identity(self) -> None:
        recognition = _recognition(
            title="Funko Pop Spider-Man",
            category="Funko Pop",
            condition="New",
        )
        provider = _provider(
            client=_FakeHttpClient(response=_FakeResponse(body=_funko_payload())),
        )

        pricing = provider.price(recognition)

        self.assertEqual(pricing.providerDiagnostics["matchedProductCategory"], "Funko Pop")
        self.assertGreater(pricing.estimatedMarketValue, 0)

    def test_coin_direct_api_match_is_allowed_with_category_identity(self) -> None:
        recognition = _recognition(
            title="1909-S VDB Lincoln Cent",
            category="Coins",
            condition="Ungraded",
        )
        provider = _provider(
            client=_FakeHttpClient(response=_FakeResponse(body=_coin_payload())),
        )

        pricing = provider.price(recognition)

        self.assertEqual(pricing.providerDiagnostics["matchedProductCategory"], "Coins")
        self.assertGreater(pricing.estimatedMarketValue, 0)

    def test_direct_api_rejects_wrong_category_family_match(self) -> None:
        recognition = _recognition(
            title="LEGO 75192 Millennium Falcon",
            category="LEGO",
            condition="New",
        )
        provider = _provider(
            client=_FakeHttpClient(response=_FakeResponse(body=_funko_payload())),
        )

        with self.assertRaises(EmptyMarketDataError):
            provider.price(recognition)

    def test_direct_api_rejects_weak_title_overlap_for_supported_direct_category(self) -> None:
        recognition = _recognition(
            title="Funko Pop Spider-Man",
            category="Funko Pop",
            condition="New",
        )
        provider = _provider(
            client=_FakeHttpClient(
                response=_FakeResponse(body=_weak_funko_payload()),
            ),
        )

        with self.assertRaises(EmptyMarketDataError):
            provider.price(recognition)

    def test_comic_direct_api_rejects_homage_match(self) -> None:
        recognition = _recognition(
            title="Amazing Spider-Man 300",
            category="Comics",
            condition="Ungraded",
            set_name="Amazing Spider-Man",
            card_number="#300",
            year="1988",
        )
        provider = _provider(
            client=_FakeHttpClient(response=_FakeResponse(body=_comic_homage_payload())),
        )

        with self.assertRaises(EmptyMarketDataError):
            provider.price(recognition)

    def test_comic_direct_api_match_is_allowed_with_issue_identity(self) -> None:
        recognition = _recognition(
            title="Amazing Spider-Man 300",
            category="Comics",
            condition="Ungraded",
            set_name="Amazing Spider-Man",
            card_number="#300",
            year="1988",
        )
        provider = _provider(
            client=_FakeHttpClient(response=_FakeResponse(body=_comic_payload())),
        )

        pricing = provider.price(recognition)

        self.assertEqual(pricing.providerDiagnostics["matchedProductCategory"], "Comics")
        self.assertGreater(pricing.estimatedMarketValue, 0)

    def test_sports_card_direct_api_rejects_wrong_card_match(self) -> None:
        recognition = _recognition(
            title="1986 Fleer Michael Jordan",
            category="Sports Card",
            condition="Ungraded",
            set_name="Fleer Basketball",
            card_number="57",
            year="1986",
            player_or_character="Michael Jordan",
        )
        provider = _provider(
            client=_FakeHttpClient(
                response=_FakeResponse(body=_wrong_sports_card_payload()),
            ),
        )

        with self.assertRaises(EmptyMarketDataError):
            provider.price(recognition)

    def test_sports_card_direct_api_match_is_allowed_with_card_identity(self) -> None:
        recognition = _recognition(
            title="1986 Fleer Michael Jordan",
            category="Sports Card",
            condition="Ungraded",
            set_name="Fleer Basketball",
            card_number="57",
            year="1986",
            player_or_character="Michael Jordan",
        )
        provider = _provider(
            client=_FakeHttpClient(response=_FakeResponse(body=_sports_card_payload())),
        )

        pricing = provider.price(recognition)

        self.assertEqual(
            pricing.providerDiagnostics["matchedProductCategory"],
            "Basketball Cards",
        )
        self.assertGreater(pricing.estimatedMarketValue, 0)


def _provider(
    *,
    api_key: str = "pc-key",
    client=None,
    cache_ttl_seconds: int = 900,
    min_interval_ms: int = 0,
    throttle=None,
) -> PriceChartingPricingProvider:
    return PriceChartingPricingProvider(
        api_key=api_key,
        api_base="https://pricecharting.test",
        timeout_seconds=1,
        cache_ttl_seconds=cache_ttl_seconds,
        min_interval_ms=min_interval_ms,
        client=client,
        throttle=throttle,
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


def _lego_payload() -> dict:
    return {
        "products": [
            {
                "id": "lego-75192",
                "product-name": "Millennium Falcon #75192",
                "console-name": "LEGO",
                "loose-price": "$250.00",
                "cib-price": "$603.00",
                "new-price": "$728.00",
                "currency": "USD",
            }
        ]
    }


def _funko_payload() -> dict:
    return {
        "products": [
            {
                "id": "funko-3",
                "product-name": "Spider-Man [Metallic] #3",
                "console-name": "Funko Pop",
                "loose-price": "$1559.00",
                "cib-price": "$2275.00",
                "new-price": "$2600.00",
                "currency": "USD",
            }
        ]
    }


def _weak_funko_payload() -> dict:
    return {
        "products": [
            {
                "id": "funko-999",
                "product-name": "Batman [Metallic] #1",
                "console-name": "Funko Pop",
                "loose-price": "$50.00",
                "currency": "USD",
            }
        ]
    }


def _coin_payload() -> dict:
    return {
        "products": [
            {
                "id": "coin-1909-s-vdb",
                "product-name": "1909 S VDB",
                "console-name": "Coins",
                "loose-price": "$1161.00",
                "new-price": "$1850.00",
                "graded-price": "$2135.00",
                "currency": "USD",
            }
        ]
    }


def _comic_homage_payload() -> dict:
    return {
        "products": [
            {
                "id": "comic-homage",
                "product-name": "Mark Spears Monsters [Amazing Spider-Man 300 Homage] #4",
                "console-name": "Comics",
                "loose-price": "$8.00",
                "new-price": "$12.00",
                "currency": "USD",
            }
        ]
    }


def _comic_payload() -> dict:
    return {
        "products": [
            {
                "id": "asm-300",
                "product-name": "Amazing Spider-Man #300",
                "console-name": "Comics",
                "loose-price": "$300.00",
                "new-price": "$450.00",
                "graded-price": "$900.00",
                "currency": "USD",
            }
        ]
    }


def _wrong_sports_card_payload() -> dict:
    return {
        "products": [
            {
                "id": "mj-222",
                "product-name": "Michael Jordan 3 Times In A Row #222",
                "console-name": "Basketball Cards",
                "loose-price": "$19.00",
                "new-price": "$25.00",
                "currency": "USD",
            }
        ]
    }


def _sports_card_payload() -> dict:
    return {
        "products": [
            {
                "id": "mj-57",
                "product-name": "Michael Jordan #57",
                "console-name": "Basketball Cards",
                "series": "1986 Fleer Basketball",
                "loose-price": "$6500.00",
                "new-price": "$9000.00",
                "graded-price": "$15000.00",
                "currency": "USD",
            }
        ]
    }


def _recognition(
    *,
    title: str,
    category: str,
    condition: str,
    set_name: str | None = None,
    card_number: str | None = None,
    year: str | None = None,
    player_or_character: str | None = None,
) -> object:
    base = MockRecognitionProvider().recognize("uploads/card.png")
    return base.__class__(
        title=title,
        category=category,
        confidence=90,
        estimatedValue=0,
        condition=condition,
        recommendation=base.recommendation,
        description=base.description,
        detectedObjects=[],
        aiProvider="test",
        processingTimeMs=0,
        primaryMatch=title,
        alternativeMatches=[],
        confidenceExplanation="Test recognition",
        detectionQuality="Reviewed",
        aiReasoning="Test recognition",
        year=year,
        setName=set_name,
        cardNumber=card_number,
        playerOrCharacter=player_or_character,
    )


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


class _FakeThrottle:
    def __init__(self, exception: Exception | None = None) -> None:
        self.exception = exception
        self.acquired_for: list[str] = []

    def acquire(self, provider_name: str) -> None:
        self.acquired_for.append(provider_name)
        if self.exception is not None:
            raise self.exception


if __name__ == "__main__":
    unittest.main()
