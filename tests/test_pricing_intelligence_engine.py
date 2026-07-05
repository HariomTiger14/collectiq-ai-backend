import unittest

from app.services.ai.base_recognition_service import RecognitionResult
from app.services.pricing.base_pricing_provider import MarketComparableSale
from app.services.pricing.pricing_intelligence_engine import PricingConfidenceEngine


class PricingIntelligenceEngineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = PricingConfidenceEngine()
        self.recognition = RecognitionResult(
            title="1999 Pokemon Charizard",
            category="Trading Card",
            confidence=94,
            estimatedValue=1850,
            condition="Near Mint",
            recommendation="Consider grading before selling.",
            description="Base Set Charizard trading card.",
            detectedObjects=["card", "charizard"],
            aiProvider="mock",
            processingTimeMs=120,
            primaryMatch="1999 Pokemon Charizard",
            alternativeMatches=[],
            confidenceExplanation="High confidence from title and card metadata.",
            detectionQuality="Good",
            aiReasoning="Card artwork and metadata match Charizard.",
            year="1999",
            brand="Pokemon",
            setName="Base Set",
            cardNumber="4/102",
            rarity="Holo Rare",
        )

    def test_confidence_engine_scores_stronger_data_higher(self) -> None:
        strong = self.engine.analyze(
            recognition=self.recognition,
            comparable_sales=[
                _sale(1000, source="eBay", title="1999 Pokemon Charizard 4/102 PSA 8"),
                _sale(1040, source="TCGplayer", title="1999 Pokemon Charizard 4/102"),
                _sale(980, source="eBay", title="Pokemon Charizard Base Set 4/102"),
                _sale(1010, source="TCGplayer", title="Charizard Base Set Holo 4/102"),
            ],
            provider_count=2,
        )
        weak = self.engine.analyze(
            recognition=self.recognition,
            comparable_sales=[
                _sale(500, source="eBay", title="Generic fire card"),
                _sale(2500, source="eBay", title="Unknown collectible"),
            ],
            provider_count=1,
        )

        self.assertGreater(strong.confidence.confidence, weak.confidence.confidence)
        self.assertIn("2 providers", strong.confidence.reason)
        self.assertGreaterEqual(strong.confidence.provider_agreement_percent, 90)

    def test_outlier_detection_removes_extreme_and_broken_listings(self) -> None:
        result = self.engine.detect_outliers(
            [
                _sale(100),
                _sale(110),
                _sale(120),
                _sale(10000, title="Suspicious buyout"),
                _sale(0, title="Broken listing"),
                _sale(5, title="Incomplete listing"),
            ]
        )

        self.assertEqual([sale.soldPrice for sale in result.kept_sales], [100, 110, 120])
        self.assertEqual(result.median_price, 110)
        self.assertEqual(len(result.removed_sales), 3)
        self.assertGreater(result.variance_percent, 0)

    def test_trend_classification_returns_directional_labels(self) -> None:
        uptrend = self.engine.classify_trend(
            [
                _sale(100, sold_date="2026-01-01T00:00:00Z"),
                _sale(110, sold_date="2026-02-01T00:00:00Z"),
                _sale(140, sold_date="2026-03-01T00:00:00Z"),
                _sale(160, sold_date="2026-04-01T00:00:00Z"),
            ]
        )
        downtrend = self.engine.classify_trend(
            [
                _sale(200, sold_date="2026-01-01T00:00:00Z"),
                _sale(190, sold_date="2026-02-01T00:00:00Z"),
                _sale(140, sold_date="2026-03-01T00:00:00Z"),
                _sale(130, sold_date="2026-04-01T00:00:00Z"),
            ]
        )
        stable = self.engine.classify_trend(
            [
                _sale(100, sold_date="2026-01-01T00:00:00Z"),
                _sale(103, sold_date="2026-02-01T00:00:00Z"),
                _sale(101, sold_date="2026-03-01T00:00:00Z"),
            ]
        )

        self.assertEqual(uptrend, "Strong Uptrend")
        self.assertEqual(downtrend, "Strong Downtrend")
        self.assertEqual(stable, "Stable")

    def test_comparable_sale_quality_scores_metadata_matches(self) -> None:
        excellent_title = " ".join(
            value
            for value in [
                self.recognition.title,
                self.recognition.setName,
                self.recognition.cardNumber,
                "PSA graded",
            ]
            if value
        )
        excellent = self.engine.score_comparable(
            _sale(
                1000,
                title=excellent_title,
                condition=self.recognition.condition,
            ),
            self.recognition,
        )
        poor = self.engine.score_comparable(
            _sale(1000, title="Random trading card lot", condition="Damaged"),
            self.recognition,
        )

        self.assertEqual(excellent.label, "Excellent")
        self.assertIn("card number match", excellent.reasons)
        self.assertEqual(poor.label, "Poor")

    def test_price_explanation_mentions_sales_providers_confidence_and_agreement(self) -> None:
        intelligence = self.engine.analyze(
            recognition=self.recognition,
            comparable_sales=[
                _sale(1000, source="eBay"),
                _sale(1060, source="TCGplayer"),
                _sale(1030, source="eBay"),
                _sale(1050, source="TCGplayer"),
            ],
            provider_count=2,
        )

        self.assertIn("4 comparable sales", intelligence.price_explanation)
        self.assertIn("2 providers", intelligence.price_explanation)
        self.assertIn("confidence", intelligence.price_explanation)
        self.assertIn("agreement", intelligence.price_explanation)

    def test_provider_agreement_reflects_cross_provider_alignment(self) -> None:
        aligned = self.engine.provider_agreement(
            [
                _sale(1000, source="eBay"),
                _sale(1010, source="eBay"),
                _sale(1030, source="TCGplayer"),
                _sale(1040, source="TCGplayer"),
            ]
        )
        divergent = self.engine.provider_agreement(
            [
                _sale(1000, source="eBay"),
                _sale(1010, source="eBay"),
                _sale(1800, source="TCGplayer"),
                _sale(1900, source="TCGplayer"),
            ]
        )

        self.assertGreater(aligned, divergent)
        self.assertGreater(aligned, 95)


def _sale(
    price: int,
    *,
    source: str = "eBay",
    title: str = "1999 Pokemon Charizard Base Set 4/102",
    condition: str = "Near Mint",
    sold_date: str = "2026-06-30T00:00:00Z",
) -> MarketComparableSale:
    return MarketComparableSale(
        source=source,
        title=title,
        soldPrice=price,
        currency="AUD",
        soldDate=sold_date,
        condition=condition,
        url=None,
    )


if __name__ == "__main__":
    unittest.main()
