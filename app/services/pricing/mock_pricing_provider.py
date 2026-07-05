from app.services.ai.base_recognition_service import RecognitionResult
from app.services.pricing.base_pricing_provider import (
    MarketComparableSale,
    PricingProvider,
    PricingResult,
    utc_timestamp,
)


class MockPricingProvider(PricingProvider):
    provider_name = "mock"

    def price(self, recognition: RecognitionResult) -> PricingResult:
        value = max(1, int(recognition.estimatedValue))
        spread = self._spread_for(recognition.category)
        low_estimate = max(1, round(value * (1 - spread)))
        high_estimate = max(low_estimate, round(value * (1 + spread)))
        source = self._source_for(recognition.category)
        confidence = self._confidence_for(recognition.confidence)
        comparable_sales = self._comparable_sales(
            recognition=recognition,
            source=source,
            value=value,
            low_estimate=low_estimate,
            high_estimate=high_estimate,
        )

        return PricingResult(
            estimatedMarketValue=value,
            lowEstimate=low_estimate,
            highEstimate=high_estimate,
            currency="AUD",
            pricingSource=source,
            pricingConfidence=confidence,
            lastUpdated=utc_timestamp(),
            marketTrend=self._trend_for(recognition.category),
            sourceCount=2,
            pricingAge="fresh",
            comparableSales=comparable_sales,
            cacheStatus="mock",
            providerDiagnostics={
                "provider": self.provider_name,
                "mode": "deterministic",
                "fallbackUsed": "false",
            },
        )

    def _spread_for(self, category: str) -> float:
        normalized = category.lower()
        if "coin" in normalized:
            return 0.18
        if "comic" in normalized:
            return 0.28
        if "toy" in normalized or "figure" in normalized:
            return 0.25
        return 0.22

    def _source_for(self, category: str) -> str:
        normalized = category.lower()
        if "pokemon" in normalized or "trading card" in normalized:
            return "Mock market blend: TCGplayer + eBay comps"
        if "sports" in normalized:
            return "Mock market blend: eBay comps + PSA guide"
        if "coin" in normalized:
            return "Mock market blend: auction comps + coin guide"
        if "comic" in normalized:
            return "Mock market blend: comic guide + eBay comps"
        return "Mock market blend"

    def _confidence_for(self, recognition_confidence: int) -> int:
        return max(45, min(95, round(recognition_confidence * 0.9)))

    def _trend_for(self, category: str) -> str:
        normalized = category.lower()
        if "pokemon" in normalized or "sports" in normalized:
            return "Rising"
        if "comic" in normalized:
            return "Watchlist"
        return "Stable"

    def _comparable_sales(
        self,
        *,
        recognition: RecognitionResult,
        source: str,
        value: int,
        low_estimate: int,
        high_estimate: int,
    ) -> list[MarketComparableSale]:
        category = recognition.category.lower()
        source_a = "Mock eBay sold comp"
        source_b = "Mock marketplace guide"
        if "pokemon" in category or "trading card" in category:
            source_b = "Mock TCGplayer market"
        elif "coin" in category:
            source_b = "Mock coin guide"
        elif "comic" in category:
            source_b = "Mock comic guide"

        return [
            MarketComparableSale(
                source=source_a,
                title=f"{recognition.title} recent sold comp",
                soldPrice=max(1, low_estimate),
                currency="AUD",
                soldDate="2026-06-20T00:00:00Z",
                condition=recognition.condition,
                url=None,
            ),
            MarketComparableSale(
                source=source_b,
                title=f"{recognition.title} market reference",
                soldPrice=max(1, value),
                currency="AUD",
                soldDate="2026-06-24T00:00:00Z",
                condition=recognition.condition,
                url=None,
            ),
            MarketComparableSale(
                source=source,
                title=f"{recognition.title} premium comp",
                soldPrice=max(1, high_estimate),
                currency="AUD",
                soldDate="2026-06-28T00:00:00Z",
                condition=recognition.condition,
                url=None,
            ),
        ]
