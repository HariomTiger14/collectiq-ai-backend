import json
import unittest

import httpx

from app.services.ai.base_recognition_service import RecognitionResult
from app.services.pricing.base_pricing_provider import PricingResult
from app.services.pricing.shared_cache_repository import SharedPricingCacheRepository


class SharedPricingCacheRepositoryTest(unittest.TestCase):
    def test_set_writes_the_recognized_title(self) -> None:
        # pricecharting_catalog.product_name is NOT NULL, so a scan-derived
        # catalog promotion has nothing usable to promote unless the cache
        # row carries the recognized title, not just normalized_identity
        # (a lowercased matching key) or display_string (a formatted price).
        captured_bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                captured_bodies.append(json.loads(request.content))
                return httpx.Response(201, json=[])
            return httpx.Response(204)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        repository = SharedPricingCacheRepository(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=client,
        )

        repository.set(_recognition(title="Charizard Base Set"), _pricing())

        self.assertEqual(len(captured_bodies), 1)
        self.assertEqual(captured_bodies[0]["title"], "Charizard Base Set")

    def test_cache_key_is_stable_for_same_collectible_identity(self) -> None:
        repository = SharedPricingCacheRepository(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
        )
        first = _recognition(title="  Charizard  Base Set  ")
        second = _recognition(title="charizard base set")

        self.assertEqual(repository.cache_key(first), repository.cache_key(second))

    def test_cache_key_is_separate_per_display_currency(self) -> None:
        repository = SharedPricingCacheRepository(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
        )
        recognition = _recognition()

        self.assertNotEqual(
            repository.cache_key(recognition, display_currency="AUD"),
            repository.cache_key(recognition, display_currency="GBP"),
        )

    def test_get_returns_pricing_result_from_fresh_cache_row(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json=[
                        {
                            "cache_key": "pricing:test",
                            "valuation_status": "market_estimated",
                            "value_aud": 638,
                            "low_estimate_aud": 593,
                            "high_estimate_aud": 684,
                            "pricing_provider": "PriceCharting",
                            "confidence_score": 0.86,
                            "checked_at": "2026-07-24T22:06:00Z",
                            "display_string": "$638.00 AUD",
                            "original_price": 420,
                            "original_currency": "USD",
                            "exchange_rate_used": 1.52,
                            "match_reason": "Matched by card number and set.",
                            "evidence_json": {
                                "sourceCount": 1,
                                "comparableSales": [
                                    {
                                        "source": "PriceCharting",
                                        "title": "Charizard Base Set sold comp",
                                        "soldPrice": 638,
                                        "currency": "AUD",
                                        "soldDate": "2026-07-24T22:06:00Z",
                                        "condition": "Near Mint",
                                    }
                                ],
                            },
                        }
                    ],
                )
            return httpx.Response(204)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        repository = SharedPricingCacheRepository(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
            client=client,
        )

        pricing = repository.get(_recognition())

        self.assertIsNotNone(pricing)
        assert pricing is not None
        self.assertEqual(pricing.estimatedMarketValue, 638)
        self.assertEqual(pricing.currency, "AUD")
        self.assertEqual(pricing.originalMarketValue, 420)
        self.assertEqual(pricing.originalCurrency, "USD")
        self.assertEqual(pricing.exchangeRateUsed, 1.52)
        self.assertEqual(pricing.cacheStatus, "shared_hit")
        self.assertEqual(pricing.valuationSource, "PriceCharting")
        self.assertEqual(len(pricing.comparableSales), 1)
        self.assertEqual(pricing.comparableSales[0].title, "Charizard Base Set sold comp")
        self.assertEqual(pricing.providerDiagnostics["comparableCount"], "1")


def _recognition(title: str = "Charizard Base Set") -> RecognitionResult:
    return RecognitionResult(
        title=title,
        category="Trading Card",
        brand="Pokemon",
        year="1999",
        series="Base Set",
        setName="Base Set",
        cardNumber="4/102",
        playerOrCharacter="Charizard",
        rarity="Holo Rare",
        condition="Near Mint",
        recommendation="Save with condition notes.",
        estimatedValue=0,
        confidence=90,
        description="",
        detectedObjects=[],
        aiProvider="test",
        processingTimeMs=1,
        primaryMatch=title,
        alternativeMatches=[],
        confidenceExplanation="Matched known card identifiers.",
        detectionQuality="Good",
        aiReasoning="Test fixture.",
    )


def _pricing() -> PricingResult:
    return PricingResult(
        estimatedMarketValue=638,
        lowEstimate=593,
        highEstimate=684,
        currency="AUD",
        pricingSource="PriceCharting",
        pricingConfidence=86,
        lastUpdated="2026-07-24T22:06:00Z",
        valuationStatus="market_estimated",
        valuationSource="PriceCharting",
    )


if __name__ == "__main__":
    unittest.main()
