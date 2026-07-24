import logging
import statistics
import time

from app.services.ai.base_recognition_service import RecognitionResult
from app.services.pricing.base_pricing_provider import (
    EmptyMarketDataError,
    MarketComparableSale,
    PricingProvider,
    PricingProviderError,
    PricingResult,
    utc_timestamp,
)
from app.services.pricing.pricing_intelligence_engine import PricingConfidenceEngine


logger = logging.getLogger("collectiq.pricing")


class PricingAggregationService:
    provider_name = "aggregate"

    def __init__(
        self,
        providers: list[PricingProvider],
        *,
        fallback_provider: PricingProvider | None = None,
        confidence_engine: PricingConfidenceEngine | None = None,
    ) -> None:
        self._providers = providers
        self._fallback_provider = fallback_provider
        self._confidence_engine = confidence_engine or PricingConfidenceEngine()

    def price(self, recognition: RecognitionResult) -> PricingResult:
        started_at = time.perf_counter()
        provider_results: list[PricingResult] = []
        provider_errors: list[str] = []

        for provider in self._providers:
            provider_started_at = time.perf_counter()
            try:
                result = provider.price(recognition)
                if result.comparableSales:
                    provider_results.append(result)
                else:
                    provider_errors.append(f"{provider.provider_name}: empty market data")
            except PricingProviderError as exc:
                provider_errors.append(f"{provider.provider_name}: {exc}")
            except Exception as exc:
                provider_errors.append(f"{provider.provider_name}: {exc}")
            finally:
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "Pricing provider completed provider=%s responseTimeMs=%s",
                        provider.provider_name,
                        int((time.perf_counter() - provider_started_at) * 1000),
                    )

        fallback_used = False
        if not provider_results:
            if self._fallback_provider is None:
                details = " | ".join(provider_errors)
                message = "No pricing provider returned usable market data."
                if details:
                    message = f"{message} {details}"
                raise EmptyMarketDataError(message)
            fallback_used = True
            try:
                provider_results.append(self._fallback_provider.price(recognition))
            except Exception as exc:
                raise EmptyMarketDataError(
                    "No pricing provider returned usable market data."
                ) from exc

        aggregate = self._aggregate_results(
            recognition=recognition,
            provider_results=provider_results,
            fallback_used=fallback_used,
            provider_errors=provider_errors,
            response_time_ms=int((time.perf_counter() - started_at) * 1000),
        )

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Pricing aggregation completed providerCount=%s fallbackUsed=%s "
                "cacheStatus=%s responseTimeMs=%s sourceCount=%s",
                len(self._providers),
                aggregate.fallbackUsed,
                aggregate.cacheStatus,
                aggregate.providerDiagnostics.get("responseTimeMs", "0"),
                aggregate.sourceCount,
            )

        return aggregate

    def _aggregate_results(
        self,
        *,
        recognition: RecognitionResult,
        provider_results: list[PricingResult],
        fallback_used: bool,
        provider_errors: list[str],
        response_time_ms: int,
    ) -> PricingResult:
        comps = self._normalize_sales(
            sale
            for result in provider_results
            for sale in result.comparableSales
        )
        provider_count = self._provider_count(provider_results, comps)
        intelligence = self._confidence_engine.analyze(
            recognition=recognition,
            comparable_sales=comps,
            provider_count=provider_count,
            fallback_used=fallback_used,
        )
        comps = intelligence.comparable_sales

        if comps:
            prices = [sale.soldPrice for sale in comps]
            estimated_value = intelligence.median_price or max(
                1,
                round(statistics.median(prices)),
            )
            low_estimate = max(1, min(prices))
            high_estimate = max(low_estimate, max(prices))
        else:
            source_value = max(1, provider_results[0].estimatedMarketValue)
            estimated_value = source_value
            low_estimate = provider_results[0].lowEstimate
            high_estimate = provider_results[0].highEstimate

        confidence = intelligence.confidence.confidence or self._confidence(
            recognition=recognition,
            provider_results=provider_results,
            comparable_sales=comps,
            fallback_used=fallback_used,
        )
        sources = sorted(
            {
                sale.source.strip()
                for sale in comps
                if sale.source and sale.source.strip()
            }
            or {
                result.pricingSource
                for result in provider_results
                if result.pricingSource
            }
        )
        trend = intelligence.market_trend or self._trend(
            provider_results,
            recognition.category,
        )
        cache_status = self._cache_status(provider_results, fallback_used)
        fallback_reason = " | ".join(provider_errors) if fallback_used else ""
        provider_diagnostics = self._provider_diagnostics(provider_results)

        return PricingResult(
            estimatedMarketValue=estimated_value,
            lowEstimate=low_estimate,
            highEstimate=high_estimate,
            currency=provider_results[0].currency or "AUD",
            pricingSource=", ".join(sources) if sources else "Mock pricing fallback",
            pricingConfidence=confidence,
            lastUpdated=utc_timestamp(),
            valuationStatus="market_estimated",
            valuationSource=", ".join(sources) if sources else "market",
            aiEstimatedValue=recognition.estimatedValue
            if recognition.estimatedValue > 0
            else None,
            marketTrend=trend,
            sourceCount=len(sources),
            pricingAge=self._pricing_age(provider_results),
            comparableSales=comps,
            fallbackUsed=fallback_used,
            cacheStatus=cache_status,
            providerDiagnostics={
                "providerCount": str(len(self._providers)),
                "providers": provider_diagnostics.get("providers", ""),
                "fallbackUsed": str(fallback_used).lower(),
                "fallbackReason": fallback_reason,
                "cacheStatus": cache_status,
                "responseTimeMs": str(response_time_ms),
                "providerResponseLatencyMs": provider_diagnostics.get(
                    "providerResponseLatencyMs",
                    "",
                ),
                "pricingFreshness": self._pricing_age(provider_results),
                "errors": " | ".join(provider_errors),
                "providerAgreement": str(
                    intelligence.confidence.provider_agreement_percent
                ),
                "priceVariance": str(intelligence.variance_percent),
                "medianPrice": str(intelligence.median_price),
                "trimmedMean": str(intelligence.trimmed_mean),
                "outliersRemoved": str(len(intelligence.removed_outliers)),
                "comparableCount": str(len(comps)),
                "confidenceCalculation": intelligence.confidence.reason,
                "priceExplanation": intelligence.price_explanation,
                "comparableQuality": self._quality_summary(
                    intelligence.comparable_quality,
                ),
            },
        )

    def _provider_count(
        self,
        provider_results: list[PricingResult],
        comparable_sales: list[MarketComparableSale],
    ) -> int:
        sources = {
            sale.source.strip()
            for sale in comparable_sales
            if sale.source and sale.source.strip()
        }
        return len(sources) or len(provider_results) or 1

    def _quality_summary(self, quality_scores) -> str:
        if not quality_scores:
            return ""
        counts: dict[str, int] = {}
        for score in quality_scores:
            counts[score.label] = counts.get(score.label, 0) + 1
        return ", ".join(
            f"{label}:{counts[label]}" for label in sorted(counts)
        )

    def _normalize_sales(self, sales) -> list[MarketComparableSale]:
        normalized: list[MarketComparableSale] = []
        for sale in sales:
            price = self._normalize_price(sale.soldPrice)
            if price is None:
                continue
            normalized.append(
                MarketComparableSale(
                    source=(sale.source or "Unknown source").strip(),
                    title=(sale.title or "Comparable sale").strip(),
                    soldPrice=price,
                    currency=(sale.currency or "AUD").strip().upper(),
                    soldDate=(sale.soldDate or utc_timestamp()).strip(),
                    condition=(sale.condition or "Unknown").strip(),
                    url=sale.url,
                )
            )
        return normalized

    def _remove_outliers(self, sales: list[MarketComparableSale]) -> list[MarketComparableSale]:
        if len(sales) < 4:
            return sales

        prices = sorted(sale.soldPrice for sale in sales)
        median_price = statistics.median(prices)
        if median_price <= 0:
            return sales

        lower_bound = median_price * 0.25
        upper_bound = median_price * 4
        filtered = [
            sale
            for sale in sales
            if lower_bound <= sale.soldPrice <= upper_bound
        ]
        return filtered or sales

    def _normalize_price(self, value) -> int | None:
        try:
            price = int(round(float(value)))
        except (TypeError, ValueError):
            return None
        if price <= 0:
            return None
        return price

    def _confidence(
        self,
        *,
        recognition: RecognitionResult,
        provider_results: list[PricingResult],
        comparable_sales: list[MarketComparableSale],
        fallback_used: bool,
    ) -> int:
        if fallback_used:
            return min(provider_results[0].pricingConfidence, 70)
        base_confidence = round(
            statistics.mean(result.pricingConfidence for result in provider_results)
        )
        comp_bonus = min(15, len(comparable_sales) * 3)
        recognition_penalty = 0 if recognition.confidence >= 80 else 10
        return max(35, min(95, base_confidence + comp_bonus - recognition_penalty))

    def _trend(self, provider_results: list[PricingResult], category: str) -> str:
        for result in provider_results:
            if result.marketTrend and result.marketTrend != "Stable":
                return result.marketTrend

        normalized = category.lower()
        if "pokemon" in normalized or "sports" in normalized:
            return "Rising"
        if "comic" in normalized:
            return "Watchlist"
        return "Stable"

    def _pricing_age(self, provider_results: list[PricingResult]) -> str:
        if any(result.pricingAge == "live" for result in provider_results):
            return "live"
        if any(result.pricingAge == "fresh" for result in provider_results):
            return "fresh"
        if any(result.pricingAge == "recent" for result in provider_results):
            return "recent"
        return provider_results[0].pricingAge or "unknown"

    def _cache_status(
        self,
        provider_results: list[PricingResult],
        fallback_used: bool,
    ) -> str:
        if fallback_used:
            return "fallback"
        if any(result.cacheStatus == "hit" for result in provider_results):
            return "hit"
        if any(result.cacheStatus == "miss" for result in provider_results):
            return "miss"
        return provider_results[0].cacheStatus or "unknown"

    def _provider_diagnostics(
        self,
        provider_results: list[PricingResult],
    ) -> dict[str, str]:
        providers = []
        latency_values = []
        for result in provider_results:
            provider = result.providerDiagnostics.get("provider")
            if provider:
                providers.append(provider)
            latency = result.providerDiagnostics.get("responseLatencyMs")
            if latency:
                latency_values.append(latency)
        return {
            "providers": ", ".join(sorted(set(providers))),
            "providerResponseLatencyMs": ", ".join(latency_values),
        }
