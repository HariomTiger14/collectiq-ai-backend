import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.services.ai.base_recognition_service import RecognitionResult
from app.services.pricing.base_pricing_provider import MarketComparableSale


@dataclass(frozen=True)
class ComparableQualityScore:
    sale: MarketComparableSale
    score: int
    label: str
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class OutlierDetectionResult:
    kept_sales: list[MarketComparableSale]
    removed_sales: list[MarketComparableSale]
    median_price: int
    trimmed_mean: int
    variance_percent: int


@dataclass(frozen=True)
class PricingConfidenceResult:
    confidence: int
    reason: str
    provider_agreement_percent: int


@dataclass(frozen=True)
class PricingIntelligenceResult:
    comparable_sales: list[MarketComparableSale]
    removed_outliers: list[MarketComparableSale]
    comparable_quality: list[ComparableQualityScore]
    median_price: int
    trimmed_mean: int
    variance_percent: int
    market_trend: str
    confidence: PricingConfidenceResult
    price_explanation: str


class PricingConfidenceEngine:
    """Turns raw comparable sales into explainable pricing intelligence."""

    def analyze(
        self,
        *,
        recognition: RecognitionResult,
        comparable_sales: list[MarketComparableSale],
        provider_count: int,
        fallback_used: bool = False,
    ) -> PricingIntelligenceResult:
        outliers = self.detect_outliers(comparable_sales)
        quality_scores = [
            self.score_comparable(sale, recognition)
            for sale in outliers.kept_sales
        ]
        trend = self.classify_trend(outliers.kept_sales)
        confidence = self.calculate_confidence(
            comparable_sales=outliers.kept_sales,
            quality_scores=quality_scores,
            provider_count=provider_count,
            variance_percent=outliers.variance_percent,
            fallback_used=fallback_used,
        )
        explanation = self.explain_price(
            comparable_sales=outliers.kept_sales,
            provider_count=provider_count,
            confidence=confidence,
        )

        return PricingIntelligenceResult(
            comparable_sales=outliers.kept_sales,
            removed_outliers=outliers.removed_sales,
            comparable_quality=quality_scores,
            median_price=outliers.median_price,
            trimmed_mean=outliers.trimmed_mean,
            variance_percent=outliers.variance_percent,
            market_trend=trend,
            confidence=confidence,
            price_explanation=explanation,
        )

    def detect_outliers(
        self,
        comparable_sales: list[MarketComparableSale],
    ) -> OutlierDetectionResult:
        valid_sales = [
            sale
            for sale in comparable_sales
            if sale.soldPrice > 0 and sale.title.strip() and sale.source.strip()
        ]
        if not valid_sales:
            return OutlierDetectionResult([], comparable_sales, 0, 0, 0)

        prices = sorted(sale.soldPrice for sale in valid_sales)
        median_price = max(1, round(statistics.median(prices)))

        lower_bound = median_price * 0.35
        upper_bound = median_price * 3.0
        if len(prices) >= 6:
            q1 = statistics.median(prices[: len(prices) // 2])
            q3 = statistics.median(prices[(len(prices) + 1) // 2 :])
            iqr = q3 - q1
            if iqr > 0:
                lower_bound = max(lower_bound, q1 - 1.5 * iqr)
                upper_bound = min(upper_bound, q3 + 1.5 * iqr)

        kept = [
            sale
            for sale in valid_sales
            if lower_bound <= sale.soldPrice <= upper_bound
        ]
        kept = kept or valid_sales
        removed = [sale for sale in comparable_sales if sale not in kept]
        kept_prices = [sale.soldPrice for sale in kept]

        return OutlierDetectionResult(
            kept_sales=kept,
            removed_sales=removed,
            median_price=max(1, round(statistics.median(kept_prices))),
            trimmed_mean=self._trimmed_mean(kept_prices),
            variance_percent=self._variance_percent(kept_prices),
        )

    def calculate_confidence(
        self,
        *,
        comparable_sales: list[MarketComparableSale],
        quality_scores: list[ComparableQualityScore],
        provider_count: int,
        variance_percent: int,
        fallback_used: bool = False,
    ) -> PricingConfidenceResult:
        comparable_count = len(comparable_sales)
        provider_score = min(20, max(1, provider_count) * 10)
        comparable_score = min(20, comparable_count * 3)
        freshness_score = self._freshness_score(comparable_sales)
        variance_score = max(0, 20 - min(20, variance_percent // 3))
        agreement = self.provider_agreement(comparable_sales)
        agreement_score = round(agreement * 0.2)
        quality_score = round(
            statistics.mean(score.score for score in quality_scores) * 0.15
        ) if quality_scores else 0
        penalty = 15 if fallback_used else 0
        confidence = max(
            25,
            min(
                98,
                provider_score
                + comparable_score
                + freshness_score
                + variance_score
                + agreement_score
                + quality_score
                - penalty,
            ),
        )

        reason = (
            f"{comparable_count} comparable sales, {max(1, provider_count)} providers, "
            f"{variance_percent}% price variance, {agreement}% provider agreement, "
            f"{self._freshness_label(comparable_sales)} market data"
        )
        return PricingConfidenceResult(
            confidence=confidence,
            reason=reason,
            provider_agreement_percent=agreement,
        )

    def classify_trend(self, comparable_sales: list[MarketComparableSale]) -> str:
        if len(comparable_sales) < 3:
            return "Stable"

        ordered = sorted(comparable_sales, key=lambda sale: sale.soldDate)
        midpoint = len(ordered) // 2
        early_prices = [sale.soldPrice for sale in ordered[:midpoint]]
        recent_prices = [sale.soldPrice for sale in ordered[midpoint:]]
        if not early_prices or not recent_prices:
            return "Stable"

        early = statistics.median(early_prices)
        recent = statistics.median(recent_prices)
        if early <= 0:
            return "Stable"
        change = (recent - early) / early
        if change >= 0.15:
            return "Strong Uptrend"
        if change >= 0.05:
            return "Moderate Uptrend"
        if change <= -0.15:
            return "Strong Downtrend"
        if change <= -0.05:
            return "Moderate Downtrend"
        return "Stable"

    def score_comparable(
        self,
        sale: MarketComparableSale,
        recognition: RecognitionResult,
    ) -> ComparableQualityScore:
        score = 25
        reasons: list[str] = []
        haystack = f"{sale.title} {sale.condition}".lower()

        title_tokens = [
            token
            for token in recognition.title.lower().replace("/", " ").split()
            if len(token) >= 4
        ]
        if title_tokens:
            matched = sum(1 for token in title_tokens if token in haystack)
            token_score = round(25 * matched / len(title_tokens))
            score += token_score
            if token_score >= 15:
                reasons.append("title match")

        if recognition.cardNumber and recognition.cardNumber.lower() in haystack:
            score += 20
            reasons.append("card number match")
        if recognition.setName and recognition.setName.lower() in haystack:
            score += 15
            reasons.append("set match")
        if recognition.condition and recognition.condition.lower() in haystack:
            score += 10
            reasons.append("condition match")
        if any(term in haystack for term in ["psa", "bgs", "cgc", "graded"]):
            score += 10
            reasons.append("graded listing")

        score = max(0, min(100, score))
        return ComparableQualityScore(
            sale=sale,
            score=score,
            label=self._quality_label(score),
            reasons=reasons or ["partial metadata match"],
        )

    def provider_agreement(self, comparable_sales: list[MarketComparableSale]) -> int:
        provider_prices: dict[str, list[int]] = {}
        for sale in comparable_sales:
            provider_prices.setdefault(sale.source, []).append(sale.soldPrice)
        provider_medians = [
            statistics.median(prices)
            for prices in provider_prices.values()
            if prices
        ]
        if len(provider_medians) <= 1:
            return 100

        blended_median = statistics.median(provider_medians)
        if blended_median <= 0:
            return 100
        max_deviation = max(
            abs(provider_median - blended_median) / blended_median
            for provider_median in provider_medians
        )
        return max(0, min(100, round((1 - max_deviation) * 100)))

    def explain_price(
        self,
        *,
        comparable_sales: list[MarketComparableSale],
        provider_count: int,
        confidence: PricingConfidenceResult,
    ) -> str:
        comparable_count = len(comparable_sales)
        sources = sorted({sale.source for sale in comparable_sales})
        source_label = " + ".join(sources) if sources else "provider data"
        return (
            f"Estimated value is based on {comparable_count} comparable sales, "
            f"{max(1, provider_count)} providers, {self._freshness_label(comparable_sales)} "
            f"market data, {confidence.confidence}% confidence, and {source_label} "
            f"agreement at {confidence.provider_agreement_percent}%."
        )

    def _trimmed_mean(self, prices: list[int]) -> int:
        if not prices:
            return 0
        ordered = sorted(prices)
        if len(ordered) >= 5:
            trim_count = max(1, math.floor(len(ordered) * 0.1))
            ordered = ordered[trim_count:-trim_count] or ordered
        return max(1, round(statistics.mean(ordered)))

    def _variance_percent(self, prices: list[int]) -> int:
        if len(prices) < 2:
            return 0
        mean = statistics.mean(prices)
        if mean <= 0:
            return 0
        return round(statistics.pstdev(prices) / mean * 100)

    def _freshness_score(self, comparable_sales: list[MarketComparableSale]) -> int:
        label = self._freshness_label(comparable_sales)
        return {
            "today": 15,
            "fresh": 12,
            "recent": 8,
            "stale": 3,
            "unknown": 0,
        }[label]

    def _freshness_label(self, comparable_sales: list[MarketComparableSale]) -> str:
        ages = [
            age
            for sale in comparable_sales
            if (age := self._age_days(sale.soldDate)) is not None
        ]
        if not ages:
            return "unknown"
        newest_age = min(ages)
        if newest_age <= 1:
            return "today"
        if newest_age <= 7:
            return "fresh"
        if newest_age <= 30:
            return "recent"
        return "stale"

    def _age_days(self, value: str) -> int | None:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        now = datetime.now(timezone.utc)
        return max(0, (now - parsed.astimezone(timezone.utc)).days)

    def _quality_label(self, score: int) -> str:
        if score >= 80:
            return "Excellent"
        if score >= 60:
            return "Good"
        if score >= 40:
            return "Fair"
        return "Poor"
