import unittest

import httpx

from app.services.ai.base_recognition_service import RecognitionResult
from app.services.pricing.shared_cache_repository import SharedPricingCacheRepository


class SharedPricingCacheRepositoryTest(unittest.TestCase):
    def test_cache_key_is_stable_for_same_collectible_identity(self) -> None:
        repository = SharedPricingCacheRepository(
            supabase_url="https://example.supabase.co",
            service_role_key="service-role",
        )
        first = _recognition(title="  Charizard  Base Set  ")
        second = _recognition(title="charizard base set")

        self.assertEqual(repository.cache_key(first), repository.cache_key(second))

    def test_get_returns_pricing_result_from_fresh_cache_row(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json=[
                        {
                            "cache_key": "pricing:test",
                            "valuation_status": "market_estimated",
                            "value_aud": 420,
                            "low_estimate_aud": 390,
                            "high_estimate_aud": 450,
                            "pricing_provider": "PriceCharting",
                            "confidence_score": 0.86,
                            "checked_at": "2026-07-24T22:06:00Z",
                            "match_reason": "Matched by card number and set.",
                            "evidence_json": {"sourceCount": 1},
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
        self.assertEqual(pricing.estimatedMarketValue, 420)
        self.assertEqual(pricing.cacheStatus, "shared_hit")
        self.assertEqual(pricing.valuationSource, "PriceCharting")


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


if __name__ == "__main__":
    unittest.main()
